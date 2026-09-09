"""Headless background controls, using no server, profile or model calls."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, Input, Static
from lc_factory.background_panel import background_subagent_panel_class
from lc_factory.upstream_cli import app_module

from lc_factory.background_ui import (
    BackgroundPanel, TaskReview, allows, approval_responses, background_request,
    client_background_tasks, plain,
)


def native_text(panel):
    return "\n".join(str(w.render()) for w in panel.presentation().query(Static))


def job(status="running", task_id="child-1", interrupts=None):
    return {"task_id": task_id, "name": "[bold]child[/bold]", "description": "Do the work",
            "status": status, "result": None, "outcome": None, "interrupts": interrupts or []}


def approval(interrupt_id="pause-1", decisions=None):
    return {"id": interrupt_id, "value": {"action_requests": [
        {"name": "write_file", "args": {"file_path": "report.txt", "content": "Exact content"}},
    ], "review_configs": [{"action_name": "write_file", "allowed_decisions": decisions or ["approve", "reject"]}]}}


class Agent:
    def __init__(self, jobs):
        self.jobs = jobs
        self.calls = []
        self.graph = SimpleNamespace(client=SimpleNamespace(http=self))
        self.workspace_gate = None
        self.post_gate = None
        self.error = None

    def _get_graph(self):
        return self.graph

    async def _workspace_for_thread(self, config):
        if self.workspace_gate:
            await self.workspace_gate.wait()
        return {"root": "/local/workspace", "thread": config["configurable"]["thread_id"]}

    async def post(self, path, json):
        self.calls.append((path, deepcopy(json)))
        if self.post_gate:
            await self.post_gate.wait()
        if self.error:
            raise self.error
        return {"tasks": deepcopy(self.jobs), "enabled": True}


class Harness(App):
    def __init__(self, agent, hooks=None):
        super().__init__()
        self._agent = agent
        self._lc_thread_id = "parent/one"
        self._hooks = hooks
        self.prompts = []

    def compose(self) -> ComposeResult:
        with Vertical(id="bottom-app-container"):
            yield background_subagent_panel_class(app_module.SubagentPanel)(id="subagent-panel")
            yield BackgroundPanel()
            yield Input(id="main-prompt")

    def on_input_submitted(self, event):
        self.prompts.append(event.value)
        event.input.value = ""


async def settle(pilot, panel):
    for _ in range(30):
        await pilot.pause(0.02)
        if not panel.polling:
            return
    assert not panel.polling


async def test_panel_updates_while_main_input_remains_usable():
    agent = Agent([job()])
    app = Harness(agent)
    async with app.run_test(size=(100, 35)) as pilot:
        panel = app.query_one(BackgroundPanel)
        await settle(pilot, panel)
        panel.timer.stop()
        assert not panel.display
        assert panel.presentation().has_class("-visible")
        assert "running" in native_text(panel).lower()
        await pilot.click("#main-prompt")
        await pilot.press("h", "i", "enter")
        assert app.prompts == ["hi"]
        agent.jobs[0]["status"] = "completed"
        agent.jobs[0]["result"] = "Finished"
        await panel.refresh_tasks()
        assert "done" in native_text(panel).lower()
        assert panel.jobs[0]["result"] == "Finished"
        assert len(app.screen_stack) == 1  # Status changes never steal focus.
        assert all(body["operation"] == "list" for _, body in agent.calls)
        await pilot.press("m", "o", "r", "e", "enter")
        assert app.prompts == ["hi", "more"]


@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_review_sends_exact_pause_ids_and_actions(decision):
    agent = Agent([job("needs_approval", interrupts=[approval("pause-a"), approval("pause-b")])])
    app = Harness(agent)
    async with app.run_test(size=(120, 40)) as pilot:
        panel = app.query_one(BackgroundPanel)
        await settle(pilot, panel)
        panel.timer.stop()
        panel.review_task(next(iter(panel.details)))
        await pilot.pause()
        assert isinstance(app.screen, TaskReview)
        assert "write_file" in app.screen.detail
        await pilot.click(f"#background-{decision}")
        await pilot.pause()
        changes = [body for _, body in agent.calls if body["operation"] != "list"]
        assert changes == [{"workspace": {"root": "/local/workspace", "thread": "parent/one"},
                            "operation": "resume", "task_id": "child-1", "responses": {
                                "pause-a": {"decisions": [{"type": decision}]},
                                "pause-b": {"decisions": [{"type": decision}]},
                            }}]
        assert all(path == "/lc-factory/threads/parent%2Fone/background" for path, _ in agent.calls)


async def test_cancel_is_child_only_and_unknown_input_remains_blocked():
    agent = Agent([job("needs_input", interrupts=[{"id": "q", "value": {"question": "[bold]Choose?"}}])])
    app = Harness(agent)
    async with app.run_test(size=(120, 40)) as pilot:
        panel = app.query_one(BackgroundPanel)
        await settle(pilot, panel)
        panel.timer.stop()
        panel.review_task(next(iter(panel.details)))
        await pilot.pause()
        assert app.screen.query_one("#background-approve", Button).disabled
        assert app.screen.query_one("#background-reject", Button).disabled
        assert "remains blocked" in str(app.screen.query_one("#background-review-error", Static).render())
        assert "Choose?" in app.screen.detail
        await pilot.click("#background-cancel")
        await pilot.pause()
        changes = [body for _, body in agent.calls if body["operation"] != "list"]
        assert len(changes) == 1
        assert changes[0]["operation"] == "cancel" and changes[0]["task_id"] == "child-1"


async def test_stale_review_cannot_approve_another_conversation():
    agent = Agent([job("needs_approval", interrupts=[approval()])])
    app = Harness(agent)
    async with app.run_test(size=(120, 40)) as pilot:
        panel = app.query_one(BackgroundPanel)
        await settle(pilot, panel)
        panel.timer.stop()
        panel.review_task(next(iter(panel.details)))
        await pilot.pause()
        app._lc_thread_id = "different"
        await pilot.click("#background-approve")
        await pilot.pause()
        assert not any(body["operation"] == "resume" for _, body in agent.calls)
        assert isinstance(app.screen, TaskReview)
        assert "not confirmed" in str(app.screen.query_one("#background-review-error", Static).render())
        assert app.screen.query_one("#background-approve", Button).disabled
        assert not app.screen.query_one("#background-back", Button).disabled


async def test_workspace_resolution_switch_and_reconnect_reject_decisions():
    agent = Agent([])
    app = Harness(agent)
    async with app.run_test() as pilot:
        panel = app.query_one(BackgroundPanel)
        await settle(pilot, panel)
        panel.timer.stop()
        identity = panel.current()
        agent.workspace_gate = asyncio.Event()
        pending = asyncio.create_task(panel.submit(identity, operation="cancel", task_id="child-1"))
        await asyncio.sleep(0)
        agent.graph = SimpleNamespace(client=SimpleNamespace(http=agent))
        agent.workspace_gate.set()
        with pytest.raises(ValueError, match="Conversation changed"):
            await pending
        assert not any(body["operation"] == "cancel" for _, body in agent.calls)


async def test_uncertain_request_and_duplicate_events_never_retry():
    calls = []
    started, release = asyncio.Event(), asyncio.Event()

    async def submit(**operation):
        calls.append(operation)
        started.set()
        await release.wait()
        raise RuntimeError("sensitive server text")

    app = Harness(Agent([]))
    async with app.run_test(size=(120, 40)) as pilot:
        screen = TaskReview(job("needs_approval", interrupts=[approval()]), submit)
        app.push_screen(screen)
        await pilot.pause()
        button = screen.query_one("#background-approve", Button)
        pending = asyncio.create_task(screen.decide(Button.Pressed(button)))
        await started.wait()
        await screen.decide(Button.Pressed(button))
        screen.action_back()
        assert app.screen is screen
        release.set()
        await pending
        await screen.decide(Button.Pressed(button))
        assert len(calls) == 1
        error = str(screen.query_one("#background-review-error", Static).render())
        assert "not confirmed" in error and "sensitive" not in error
        screen.action_back()
        await pilot.pause()
        assert app.screen is not screen


async def test_slow_hooks_do_not_block_poll_or_input_and_resume_exact_transport(fake_hook_adapter):
    started, release = asyncio.Event(), asyncio.Event()
    values = []

    async def fulfill(value):
        values.append(value)
        started.set()
        await release.wait()
        return {"type": "hook_response", "request_id": "hook-1", "result": {"ok": True}}

    value = {"type": "hook_invocation", "request_id": "hook-1", "event": "before_tool"}
    agent = Agent([job("needs_input", interrupts=[{"id": "hook-pause", "value": value}])])
    app = Harness(agent, SimpleNamespace(fulfill_interrupt=fulfill))
    async with app.run_test(size=(120, 40)) as pilot:
        panel = app.query_one(BackgroundPanel)
        await started.wait()
        await settle(pilot, panel)
        panel.timer.stop()
        await pilot.click("#main-prompt")
        await pilot.press("a", "enter")
        await panel.refresh_tasks()
        assert app.prompts == ["a"]
        assert len(values) == 1
        release.set()
        await pilot.pause()
        changes = [body for _, body in agent.calls if body["operation"] == "resume"]
        assert len(changes) == 1
        assert changes[0]["responses"] == {"hook-pause": {"type": "hook_response", "request_id": "hook-1", "result": {"ok": True}}}
        await panel.refresh_tasks()
        assert len(values) == 1


@pytest.mark.parametrize("shutdown", [False, True])
async def test_hook_switch_or_shutdown_never_resumes_old_child(shutdown, fake_hook_adapter):
    started, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def fulfill(value):
        started.set()
        try:
            await release.wait()
        finally:
            stopped.set()
        return {"ok": True}

    agent = Agent([job("needs_input", interrupts=[{"id": "p", "value": {"type": "hook_invocation"}}])])
    app = Harness(agent, SimpleNamespace(fulfill_interrupt=fulfill))
    async with app.run_test() as pilot:
        panel = app.query_one(BackgroundPanel)
        await started.wait()
        await settle(pilot, panel)
        panel.timer.stop()
        if shutdown:
            await panel.remove()
        else:
            app._lc_thread_id = "new-thread"
            agent.jobs = []
            await panel.refresh_tasks()
        await asyncio.wait_for(stopped.wait(), 1)
        release.set()
        await pilot.pause()
        assert not any(body["operation"] == "resume" for _, body in agent.calls)


async def test_hook_failure_visible_and_not_reexecuted(fake_hook_adapter):
    calls = []

    async def fulfill(value):
        calls.append(value)
        raise RuntimeError("secret")

    agent = Agent([job("needs_input", interrupts=[{"id": "p", "value": {"type": "hook_invocation"}}])])
    app = Harness(agent, SimpleNamespace(fulfill_interrupt=fulfill))
    async with app.run_test() as pilot:
        panel = app.query_one(BackgroundPanel)
        await settle(pilot, panel)
        panel.timer.stop()
        await panel.refresh_tasks()
        await pilot.pause()
        assert len(calls) == 1
        summary = native_text(panel)
        assert "Hook response unconfirmed" in summary and "secret" not in summary


async def test_status_error_recovers_without_changed_snapshot():
    agent = Agent([job()])
    app = Harness(agent)
    async with app.run_test() as pilot:
        panel = app.query_one(BackgroundPanel)
        await settle(pilot, panel)
        panel.timer.stop()
        agent.error = RuntimeError("secret")
        await panel.refresh_tasks()
        assert "unavailable" in native_text(panel)
        agent.error = None
        await panel.refresh_tasks()
        assert "running" in native_text(panel).lower()


async def test_native_reset_invalidates_old_actions_before_new_thread_arrives():
    agent = Agent([job("needs_approval", interrupts=[approval()])])
    app = Harness(agent)
    async with app.run_test() as pilot:
        panel = app.query_one(BackgroundPanel)
        await settle(pilot, panel)
        panel.timer.stop()
        identity = panel.current()
        panel.presentation().reset()
        before = len(agent.calls)
        await panel.refresh_tasks()
        assert not panel.presentation().has_class("-visible") and not panel.details
        assert len(agent.calls) == before
        with pytest.raises(ValueError, match="Conversation changed"):
            await panel.submit(identity, operation="cancel", task_id="child-1")
        app._lc_thread_id = "new-conversation"
        agent.jobs = []
        await panel.refresh_tasks()
        assert panel.suspended_identity is None


async def test_connection_lookup_failure_clears_visible_task_actions(monkeypatch):
    agent = Agent([job("needs_approval", interrupts=[approval()])])
    app = Harness(agent)
    async with app.run_test() as pilot:
        panel = app.query_one(BackgroundPanel)
        await settle(pilot, panel)
        panel.timer.stop()
        original = agent._get_graph
        def disconnected():
            raise RuntimeError("disconnected")
        monkeypatch.setattr(agent, "_get_graph", disconnected)
        panel.schedule_refresh()
        assert not panel.details
        assert "connection unavailable" in native_text(panel).lower()
        monkeypatch.setattr(agent, "_get_graph", original)
        await panel.refresh_tasks()
        assert "child-1" in panel.details


async def test_approval_uses_native_file_preview_and_one_shared_panel():
    from lc_factory.upstream_cli import background_approval_widgets
    agent = Agent([job("needs_approval", interrupts=[approval()])])
    app = Harness(agent)
    async with app.run_test(size=(100, 32)) as pilot:
        panel = app.query_one(BackgroundPanel)
        await settle(pilot, panel)
        panel.timer.stop()
        assert not panel.display and len(app.query("#subagent-panel")) == 1
        panel.review_task("child-1")
        await pilot.pause()
        assert len(app.screen.query(background_approval_widgets().WriteFileApprovalWidget)) == 1
        assert "Waiting for approval" in str(app.screen.query(Static).first().render())


@pytest.mark.parametrize("payload", [
    {"action_requests": None}, {"action_requests": [None]},
    {"action_requests": [{"name": 1, "args": {}}]},
    {"action_requests": [{"name": "write_file", "args": {"file_path": None, "content": "text"}}]},
    {"action_requests": [{"name": "edit_file", "args": {"file_path": "file", "old_string": [], "new_string": "x"}}]},
])
async def test_unsupported_approval_details_remain_blocked_and_cancellable(payload):
    agent = Agent([job("needs_approval", interrupts=[{"id": "unsupported", "value": payload}])])
    app = Harness(agent)
    async with app.run_test() as pilot:
        controller = app.query_one(BackgroundPanel)
        await settle(pilot, controller)
        controller.timer.stop()
        controller.review_task("child-1")
        await pilot.pause()
        assert app.screen.query_one("#background-approve", Button).disabled
        assert app.screen.query_one("#background-reject", Button).disabled
        assert not app.screen.query_one("#background-cancel", Button).disabled
        await pilot.click("#background-cancel")
        await pilot.pause()
        assert any(body["operation"] == "cancel" for _, body in agent.calls)


def test_only_complete_supported_batches_can_be_approved():
    mixed = job("needs_approval", interrupts=[approval(), {"id": "other", "value": {"question": "?"}}])
    assert not allows(mixed, "approve")
    assert not allows(job("running", interrupts=[approval()]), "approve")
    assert not allows(job("needs_approval", interrupts=[approval(decisions=["reject"])]), "approve")
    duplicate = job("needs_approval", interrupts=[approval(), approval()])
    with pytest.raises(ValueError):
        approval_responses(duplicate, "approve")
    large = job("needs_approval", interrupts=[approval()])
    large["interrupts"][0]["value"]["action_requests"][0]["args"]["content"] = "x" * 30_000
    screen = TaskReview(large, None)
    assert not screen.can_decide("approve")
    assert len(screen.detail) < 24_100
    assert plain("[bold]literal[/bold]\x1b[31m") == "[bold]literal[/bold]\\x1b[31m"


async def test_context_restores_mount_and_nested_use_mounts_only_one_panel(monkeypatch):
    from lc_factory.upstream_cli import app_module

    class Host(App):
        def compose(self):
            with Vertical(id="bottom-app-container"):
                yield app_module.SubagentPanel(id="subagent-panel")

        async def on_mount(self):
            self.original_mounted = True

    original = Host.on_mount
    original_panel = app_module.SubagentPanel
    monkeypatch.setattr(app_module, "DeepAgentsApp", Host)
    with client_background_tasks(), client_background_tasks():
        app = Host()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.original_mounted
            assert len(app.query(BackgroundPanel)) == 1
    assert Host.on_mount is original
    assert app_module.SubagentPanel is original_panel


async def test_switch_hides_old_rows_even_while_status_request_is_stuck():
    agent = Agent([job()])
    app = Harness(agent)
    async with app.run_test() as pilot:
        panel = app.query_one(BackgroundPanel)
        await settle(pilot, panel)
        panel.timer.stop()
        old_task = next(iter(panel.details))
        agent.post_gate = asyncio.Event()
        panel.schedule_refresh()
        await pilot.pause(0.02)
        assert panel.polling
        app._agent = Agent([])
        app._lc_thread_id = "parent-two"
        panel.schedule_refresh()
        assert not panel.display and not panel.details
        panel.review_task(old_task)
        await settle(pilot, panel)
        panel.schedule_refresh()
        await settle(pilot, panel)
        assert panel.jobs == []
        assert len(app.screen_stack) == 1


async def test_literal_details_are_bounded_and_review_buttons_follow_exact_snapshot():
    agent = Agent([job("completed")])
    app = Harness(agent)
    async with app.run_test(size=(120, 40)) as pilot:
        panel = app.query_one(BackgroundPanel)
        await settle(pilot, panel)
        panel.timer.stop()
        old_task = next(iter(panel.details))
        agent.jobs = [job("needs_approval", task_id="new-child", interrupts=[approval()])]
        await panel.refresh_tasks()
        panel.review_task(old_task)
        await pilot.pause()
        assert len(app.screen_stack) == 1
        panel.review_task(next(iter(panel.details)))
        await pilot.pause()
        assert app.screen.job["task_id"] == "new-child"
        rendered_title = str(app.screen.query_one(Static).render())
        assert "[bold]child[/bold]" in rendered_title


@pytest.fixture
def fake_hook_adapter(monkeypatch):
    # The small transport tests above isolate panel behavior. The regressions
    # below use the real upstream shielded ledger and the production adapter.
    from lc_factory import upstream_cli

    async def fulfill(hooks, payload):
        return await hooks.fulfill_interrupt(payload)

    monkeypatch.setattr(upstream_cli, "fulfill_background_hook", fulfill)


def hook_payload(invocation_id, *, thread="child"):
    return {"type": "hook_invocation", "request": {
        "protocol_version": 1, "invocation_id": str(invocation_id), "snapshot_id": "snapshot",
        "run_id": "run", "deadline": "2099-01-01T00:00:00Z",
        "invocation": {"context": {"thread_id": thread, "cwd": "/tmp", "approval_mode": "manual"},
                       "event": {"event": "Stop", "continuation_count": 0, "last_assistant_message": ""}},
    }}


@pytest.mark.parametrize("change", ["cancel", "cancel_control", "disappear", "new_pause", "switch", "unmount", "runtime"])
async def test_real_shielded_hooks_cancel_exact_operation_and_preserve_completed_and_foreground(change):
    from uuid import uuid4
    from deepagents_code.hooks.client import HookFulfillmentLedger
    from deepagents_code.hooks.models.domain import StopDecision
    from lc_factory.upstream_cli import fulfill_background_hook

    first_id, blocked_id, foreground_id = uuid4(), uuid4(), uuid4()
    first = hook_payload(first_id, thread="first")
    blocked = hook_payload(blocked_id, thread="blocked")
    started, stopped = asyncio.Event(), asyncio.Event()
    foreground_started, foreground_release = asyncio.Event(), asyncio.Event()
    calls = []

    async def invoke(invocation):
        calls.append(invocation.context.thread_id)
        if invocation.context.thread_id == "blocked":
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                # Cleanup itself yields; callers must await it, not just cancel.
                await asyncio.sleep(0.01)
                stopped.set()
        return StopDecision(event="Stop", continue_loop=False)

    async def foreground_operation():
        foreground_started.set()
        await foreground_release.wait()
        return "unrelated foreground result"

    ledger = HookFulfillmentLedger()
    runtime = SimpleNamespace(snapshot_id="snapshot", fulfillments=ledger, invoke=invoke,
                              presenter=SimpleNamespace(present_decision=lambda decision: None))
    hooks = SimpleNamespace(_runtime=runtime)
    agent = Agent([job("needs_input", interrupts=[{"id": "first-pause", "value": first},
                                                 {"id": "blocked-pause", "value": blocked}])])
    foreground = asyncio.create_task(ledger.fulfill(("snapshot", foreground_id), foreground_operation))
    await foreground_started.wait()
    app = Harness(agent, hooks)
    replacement = None
    async with app.run_test() as pilot:
        panel = app.query_one(BackgroundPanel)
        await asyncio.wait_for(started.wait(), 2)
        await settle(pilot, panel)
        panel.timer.stop()
        assert ("snapshot", blocked_id) in ledger._in_flight
        assert ("snapshot", first_id) in ledger._completed
        if change == "cancel":
            agent.jobs[0]["status"] = "cancelled"
        elif change == "disappear":
            agent.jobs = []
        elif change == "new_pause":
            agent.jobs[0]["interrupts"] = [{"id": "question", "value": {"question": "Wait here"}}]
        elif change == "switch":
            app._lc_thread_id = "different-conversation"
            agent.jobs = []
        elif change == "runtime":
            # A replacement runtime has the same invocation key. Cancelling the
            # old captured runtime must leave this independent operation alone.
            replacement_ledger = HookFulfillmentLedger()
            replacement_started = asyncio.Event()

            async def replacement_operation():
                replacement_started.set()
                await foreground_release.wait()
                return "replacement result"

            replacement = asyncio.create_task(replacement_ledger.fulfill(
                ("snapshot", blocked_id), replacement_operation))
            await replacement_started.wait()
            hooks._runtime = SimpleNamespace(fulfillments=replacement_ledger)
        if change == "unmount":
            await panel.remove()
        elif change == "cancel_control":
            await panel.submit(panel.current(), operation="cancel", task_id="child-1")
        else:
            await panel.refresh_tasks()
        assert stopped.is_set()
        assert ("snapshot", blocked_id) not in ledger._in_flight
        assert not any(body["operation"] == "resume" for _, body in agent.calls)
        assert not foreground.done()
        if replacement:
            assert not replacement.done()
        # Fulfilled earlier items survive cleanup and cannot replay on delivery.
        await fulfill_background_hook(SimpleNamespace(_runtime=runtime), first)
        assert calls.count("first") == 1
    foreground_release.set()
    assert await foreground == "unrelated foreground result"
    if replacement:
        assert await replacement == "replacement result"


@pytest.mark.parametrize("count", [1, 2])
async def test_hook_pause_is_rechecked_before_next_hook_and_resume(count):
    from uuid import uuid4
    from deepagents_code.hooks.client import HookFulfillmentLedger
    from deepagents_code.hooks.models.domain import StopDecision

    calls = []
    holder = {}

    async def invoke(invocation):
        calls.append(invocation.context.thread_id)
        # Simulates an updated task snapshot arriving as this step completes.
        holder["app"].query_one(BackgroundPanel).jobs[0]["status"] = "cancelled"
        return StopDecision(event="Stop", continue_loop=False)

    runtime = SimpleNamespace(snapshot_id="snapshot", fulfillments=HookFulfillmentLedger(), invoke=invoke,
                              presenter=SimpleNamespace(present_decision=lambda decision: None))
    agent = Agent([job("needs_input", interrupts=[
        {"id": "first", "value": hook_payload(uuid4(), thread="first")},
        {"id": "second", "value": hook_payload(uuid4(), thread="second")},
    ][:count])])
    app = Harness(agent, SimpleNamespace(_runtime=runtime))
    holder["app"] = app
    async with app.run_test() as pilot:
        panel = app.query_one(BackgroundPanel)
        await settle(pilot, panel)
        panel.timer.stop()
        await pilot.pause()
        assert calls == ["first"]
        assert not any(body["operation"] == "resume" for _, body in agent.calls)


async def test_real_ledger_cleanup_survives_repeated_cancellation():
    from uuid import uuid4
    from deepagents_code.hooks.client import HookFulfillmentLedger
    from lc_factory.upstream_cli import fulfill_background_hook

    started, cleanup_started, release_cleanup, cleanup_done = (asyncio.Event() for _ in range(4))

    async def invoke(invocation):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()
            cleanup_done.set()

    runtime = SimpleNamespace(snapshot_id="snapshot", fulfillments=HookFulfillmentLedger(), invoke=invoke,
                              presenter=SimpleNamespace(present_decision=lambda decision: None))
    invocation_id = uuid4()
    task = asyncio.create_task(fulfill_background_hook(SimpleNamespace(_runtime=runtime), hook_payload(invocation_id)))
    await started.wait()
    task.cancel()
    await cleanup_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleanup_done.is_set()
    assert ("snapshot", invocation_id) not in runtime.fulfillments._in_flight
