"""Native-panel entry, read-only live details, and frozen permission review."""
import asyncio
from copy import deepcopy

import pytest
from textual.widgets import Button, Input, Static

from lc_factory.background_activity import TaskActivity, activity_text
from lc_factory.background_ui import BackgroundPanel, TaskReview
from test_background_ui import Agent, Harness, approval, job, settle


class ActivityAgent(Agent):
    async def post(self, path, json):
        result = await super().post(path, json)
        if json["operation"] == "inspect":
            selected = next((j for j in self.jobs if j["task_id"] == json["task_id"]), None)
            if selected is None:
                raise ValueError("Unknown task")
            return {"task": deepcopy(selected), "enabled": True}
        return result


def active_job(status="running", **kwargs):
    return {**job(status, **kwargs), "steerable": True, "steering": [], "activity": [
        {"sequence": 1, "kind": "tool", "tool_name": "read_file", "tool_call_id": "read-1", "status": "completed"},
        {"sequence": 2, "kind": "report", "text": "Found the relevant section."},
    ]}


async def open_activity(app, pilot):
    controller = app.query_one(BackgroundPanel)
    await settle(pilot, controller)
    controller.timer.stop()
    controller.show_task("child-1")
    await pilot.pause()
    screen = app.screen
    assert isinstance(screen, TaskActivity)
    screen.timer.stop()
    await settle(pilot, screen)
    return controller, screen


async def test_native_row_opens_activity_then_returns_input_to_main_agent():
    agent = ActivityAgent([active_job()])
    app = Harness(agent)
    async with app.run_test(size=(100, 35)) as pilot:
        controller = app.query_one(BackgroundPanel)
        await settle(pilot, controller)
        controller.timer.stop()
        native = controller.presentation()
        # The callback actually installed on the existing native panel opens details.
        assert native._background_review.__self__ is controller
        native._background_review("child-1")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, TaskActivity)
        screen.timer.stop()
        await settle(pilot, screen)
        body = str(screen.query_one("#activity-body", Static).render())
        assert "Do the work" in body and "read_file" in body and "Found the relevant section" in body
        assert len(screen.query(Input)) == 0
        assert screen.query_one("#activity-review", Button).disabled
        assert not screen.query_one("#activity-cancel", Button).disabled
        assert any(call[1]["operation"] == "inspect" for call in agent.calls)
        await pilot.press("escape")
        await pilot.click("#main-prompt")
        await pilot.press("h", "i", "enter")
        assert app.prompts == ["hi"]
        assert all(call[1]["operation"] in ("list", "inspect") for call in agent.calls)


async def test_live_detail_updates_and_permission_review_keeps_exact_old_snapshot():
    agent = ActivityAgent([active_job()])
    app = Harness(agent)
    async with app.run_test(size=(100, 36)) as pilot:
        _, screen = await open_activity(app, pilot)
        agent.jobs[0] = active_job("needs_approval", interrupts=[approval("old-pause")])
        agent.jobs[0]["steering"] = [{"message_id": "message-1", "revision": 1, "message": "Check the appendix first.", "status": "delivered"}]
        await screen.refresh_task()
        assert not screen.query_one("#activity-review", Button).disabled
        body = str(screen.query_one("#activity-body", Static).render())
        assert "Waiting for your approval" in body and "Delivered" in body
        await pilot.click("#activity-review")
        await pilot.pause()
        review = app.screen
        assert isinstance(review, TaskReview)
        agent.jobs[0]["interrupts"] = [approval("new-pause")]
        before = len(agent.calls)
        screen.schedule_refresh()
        await pilot.pause()
        assert len(agent.calls) == before  # Hidden detail screen does not poll.
        await screen.refresh_task()  # Even an already-running refresh cannot rewrite review.
        assert review.job["interrupts"][0]["id"] == "old-pause"
        await pilot.click("#background-reject")
        await pilot.pause()
        response = [body for _, body in agent.calls if body["operation"] == "resume"][-1]
        assert set(response["responses"]) == {"old-pause"}
        assert app.screen is screen
        agent.jobs[0]["status"] = "completed"
        agent.jobs[0]["result"] = "Final result."
        agent.jobs[0]["interrupts"] = []
        await screen.refresh_task()
        assert screen.query_one("#activity-review", Button).disabled
        assert screen.query_one("#activity-cancel", Button).disabled
        assert "Final result." in str(screen.query_one("#activity-body", Static).render())


@pytest.mark.parametrize("change", ["thread", "connection", "reset"])
async def test_identity_change_during_inspection_clears_details_and_blocks_controls(change):
    agent = ActivityAgent([active_job("needs_approval", interrupts=[approval()])])
    app = Harness(agent)
    async with app.run_test(size=(100, 36)) as pilot:
        controller, screen = await open_activity(app, pilot)
        agent.post_gate = asyncio.Event()
        pending = asyncio.create_task(screen.refresh_task())
        await asyncio.sleep(0)
        if change == "thread":
            app._lc_thread_id = "other"
        elif change == "connection":
            app._agent = ActivityAgent([])
        else:
            controller.presentation().reset()
        agent.post_gate.set()
        await pending
        assert "Conversation changed" in str(screen.query_one("#activity-body", Static).render())
        assert screen.query_one("#activity-review", Button).disabled
        assert screen.query_one("#activity-cancel", Button).disabled
        await screen.handle_button(Button.Pressed(screen.query_one("#activity-cancel", Button)))
        assert all(body["operation"] in ("list", "inspect") for _, body in agent.calls)


async def test_missing_task_or_transport_failure_disables_actions_then_recovers():
    agent = ActivityAgent([active_job()])
    app = Harness(agent)
    async with app.run_test() as pilot:
        _, screen = await open_activity(app, pilot)
        original = agent.jobs
        agent.jobs = []
        await screen.refresh_task()
        assert not screen.fresh
        assert screen.query_one("#activity-cancel", Button).disabled
        agent.jobs = original
        agent.error = RuntimeError("private transport detail")
        await screen.refresh_task()
        notice = str(screen.query_one("#activity-notice", Static).render())
        assert "unavailable" in notice and "private" not in notice
        agent.error = None
        await screen.refresh_task()
        assert screen.fresh and not screen.query_one("#activity-cancel", Button).disabled


async def test_cancel_targets_selected_child_once_and_close_stops_polling():
    agent = ActivityAgent([active_job(), active_job(task_id="second")])
    app = Harness(agent)
    async with app.run_test(size=(40, 32)) as pilot:
        _, screen = await open_activity(app, pilot)
        assert screen.has_class("-compact")
        button = screen.query_one("#activity-cancel", Button)
        await screen.handle_button(Button.Pressed(button))
        await pilot.pause()
        await screen.handle_button(Button.Pressed(button))
        assert [body["task_id"] for _, body in agent.calls if body["operation"] == "cancel"] == ["child-1"]
        screen.action_back()
        await pilot.pause()
        before = len(agent.calls)
        screen.schedule_refresh()
        await pilot.pause()
        assert screen.closed and len(agent.calls) == before


async def test_inspect_is_single_flight_and_scroll_is_preserved():
    value = active_job()
    value["activity"] = [{"kind": "report", "text": f"Finding {n}"} for n in range(80)]
    agent = ActivityAgent([value])
    app = Harness(agent)
    async with app.run_test(size=(80, 32)) as pilot:
        _, screen = await open_activity(app, pilot)
        scroll = screen.query_one("#activity-scroll")
        scroll.scroll_to(y=20, animate=False)
        await pilot.pause()
        agent.post_gate = asyncio.Event()
        pending = asyncio.create_task(screen.refresh_task())
        await asyncio.sleep(0)
        before = len(agent.calls)
        await screen.refresh_task()
        assert len(agent.calls) == before
        agent.jobs[0]["activity"].append({"kind": "report", "text": "Newest finding"})
        agent.post_gate.set()
        await pending
        await pilot.pause()
        assert scroll.scroll_y == 20


def test_activity_projection_is_bounded_literal_and_excludes_private_fields():
    value = active_job()
    value["activity"] = [{"kind": "reasoning", "text": "private chain"},
                         {"kind": "tool", "tool_name": "read_file", "status": "completed", "output": "raw secret result"},
                         {"kind": "report", "text": "[bold]literal[/bold]\x1b" + "x" * 3000}]
    value["messages"] = [{"content": "private transcript"}]
    value["steering"] = [{"message": "skip it", "status": "queued", "delivery_outcome": "task_completed_before_delivery"}]
    text = activity_text(value).plain
    assert "private" not in text and "raw secret" not in text
    assert "[bold]literal[/bold]\\x1b" in text and "[truncated]" in text
    assert "task completed before delivery" in text
    assert len(text) < 3000
    value["status"] = "cancelled"
    value["steering"][0]["delivery_outcome"] = "undelivered-terminal"
    assert "Not delivered · Cancelled" in activity_text(value).plain
