"""Native child steering exercises checkpoint, hook and execution boundaries."""
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from deepagents.middleware.subagents import _build_task_tool
from deepagents_code.hooks.server_middleware import ServerHooksMiddleware
from langchain.agents.middleware import AgentMiddleware, HumanInTheLoopMiddleware
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import interrupt
from pydantic import Field

from lc_factory.background import BackgroundTasks, Job, _CURRENT_JOB
from lc_factory.background_child import BackgroundChildMiddleware


@pytest.mark.parametrize("mode", ["isolated", "fork"])
@pytest.mark.parametrize("shape", ["message", "dict", "command_list"])
async def test_command_error_is_failed_activity_without_exposing_content(mode, shape):
    from langchain_core.messages import ToolMessage
    from langgraph.types import Command
    effects = []

    @tool
    def update_state(runtime: ToolRuntime) -> Any:
        """Return a supported state update with a failed tool result."""
        effects.append("called")
        message = ToolMessage("PRIVATE_COMMAND_TOOL_OUTPUT", tool_call_id=runtime.tool_call_id, status="error")
        value = {"type": "tool", "content": message.content, "tool_call_id": message.tool_call_id,
                 "status": "error"} if shape == "dict" else message
        result = Command(update={"messages": [value]})
        # The pinned SDK requires ToolMessage objects in list-level terminators;
        # direct Commands also accept the message-dictionary representation.
        return [result] if shape == "command_list" else result

    model = Model(responses=[call("update_state", "error-1"), AIMessage("Handled the failure")])
    async with child(mode, model, tools=[update_state]) as (tasks, key, job):
        await settled(job)
        assert job.status == "completed" and effects == ["called"]
        assert any(isinstance(m, ToolMessage) and m.tool_call_id == "error-1" and m.status == "error"
                   for m in model.seen[-1])
        observed = tasks.inspect("owner", key)
        assert [a["status"] for a in observed["activity"] if a.get("tool_call_id") == "error-1"] == ["started", "failed"]
        assert "PRIVATE_COMMAND_TOOL_OUTPUT" not in json.dumps(observed)


class Model(FakeMessagesListChatModel):
    seen: list[list] = Field(default_factory=list)
    bound_names: list[list[str]] = Field(default_factory=list)
    entered: Any = None
    release: Any = None
    hold_at: int = -1

    def bind_tools(self, tools, **kwargs):
        self.bound_names.append([getattr(t, "name", t.get("name", "") if isinstance(t, dict) else "") for t in tools])
        return self

    async def _agenerate(self, messages, **kwargs):
        self.seen.append(list(messages))
        if len(self.seen) == self.hold_at:
            self.entered.set()
            await self.release.wait()
        return await super()._agenerate(messages, **kwargs)


def call(name, identifier, args=None):
    return AIMessage(content="", tool_calls=[dict(name=name, args=args or {}, id=identifier, type="tool_call")])


@asynccontextmanager
async def child(mode, model, *, tools=(), middleware=(), context=None, tasks=None, steerable=True):
    tasks = tasks or BackgroundTasks()
    task_tool = _build_task_tool([dict(name="child", description="Bounded child", mode=mode,
        model=model, tools=list(tools), middleware=[*middleware, BackgroundChildMiddleware()])])
    task_tool.metadata = {"lc_factory_subagent_names": ("child",),
                          "lc_factory_steerable_subagents": ("child",) if steerable else ()}
    runtime = ToolRuntime(state={"messages": [HumanMessage("Parent context")]}, context=context,
        config={"configurable": {"thread_id": "owner"}}, stream_writer=lambda _: None,
        tool_call_id="launch", store=None)
    result = tasks._submit(task_tool, {"name": "task", "type": "tool_call", "id": "launch",
        "args": {"description": "Find evidence within the assignment", "subagent_type": "child"}}, runtime)
    assert result["ok"], result
    key = result["task_id"]
    try:
        yield tasks, key, tasks.jobs[key]
    finally:
        await tasks.close()


async def settled(job):
    for _ in range(12):
        worker = job.worker
        await asyncio.wait_for(asyncio.shield(worker), 5)
        await asyncio.sleep(0)  # done callback may claim a stale approval
        if worker is job.worker:
            return
    raise AssertionError("worker did not settle")


def responses(job, decision="approve"):
    return {item["id"]: {"decisions": [{"type": decision}
        for _ in item["value"]["action_requests"]]} for item in job.interrupts}


@pytest.mark.parametrize("mode", ["isolated", "fork"])
@pytest.mark.parametrize("obsolete", ["tool", "final"])
async def test_steering_during_model_discards_obsolete_proposal(mode, obsolete):
    effects = []
    entered, release = asyncio.Event(), asyncio.Event()

    @tool
    def marker() -> str:
        """Record an admitted effect."""
        effects.append("marker")
        return "PRIVATE TOOL OUTPUT"

    model = Model(responses=[call("marker", "old") if obsolete == "tool" else AIMessage("obsolete final"),
                            call("report_background_task", "report", {"finding": "Explicit evidence"}),
                            AIMessage("revised final")],
                  entered=entered, release=release, hold_at=1)
    async with child(mode, model, tools=[marker]) as (tasks, key, job):
        await asyncio.wait_for(entered.wait(), 5)
        record = tasks.steer("owner", key, "Preserve the existing evidence; skip the proposed action")
        assert record["status"] == "queued"
        assert tasks.inspect("owner", key)["steering"][0]["status"] == "queued"
        release.set()
        await settled(job)
        assert job.status == "completed" and job.result == "revised final"
        assert effects == []
        snapshot = tasks.inspect("owner", key)
        assert snapshot["steering"][0]["status"] == "delivered"
        assert snapshot["activity"][-2]["text"] == "Explicit evidence"
        assert "obsolete final" not in str(snapshot["activity"])
        assert "PRIVATE TOOL OUTPUT" not in str(snapshot)
        ids = [m.id for m in model.seen[1]]
        assert ids.count(record["message_id"]) == 1
        if obsolete == "tool":
            assert snapshot["activity"][0] == dict(sequence=1, kind="tool", tool_name="marker",
                                                  tool_call_id="old", status="skipped")


@pytest.mark.parametrize("mode", ["isolated", "fork"])
async def test_steering_during_tool_preserves_admitted_effect_and_explicit_ack(mode):
    effects = []
    entered, release = asyncio.Event(), asyncio.Event()

    @tool
    async def marker() -> str:
        """Wait in an admitted tool then record its effect once."""
        entered.set()
        await release.wait()
        effects.append("completed")
        return "PRIVATE RAW OUTPUT"

    model = Model(responses=[call("marker", "admitted"), AIMessage("placeholder"), AIMessage("done")])
    async with child(mode, model, tools=[marker]) as (tasks, key, job):
        await asyncio.wait_for(entered.wait(), 5)
        record = tasks.steer("owner", key, "Report what the admitted action established")
        model.responses[1] = call("report_background_task", "report", {
            "finding": "Recorded evidence", "acknowledged_message_ids": [record["message_id"]]})
        release.set()
        await settled(job)
        assert effects == ["completed"] and job.status == "completed"
        snapshot = tasks.inspect("owner", key)
        assert snapshot["steering"][0]["status"] == "acknowledged"
        assert [a["status"] for a in snapshot["activity"] if a.get("tool_call_id") == "admitted"] == ["started", "completed"]
        assert "PRIVATE RAW OUTPUT" not in str(snapshot)


@pytest.mark.parametrize("mode", ["isolated", "fork"])
async def test_stale_multi_action_approval_rejected_without_replay(mode):
    effects = []

    @tool
    def marker() -> str:
        """Record completed evidence."""
        effects.append("marker")
        return "evidence"

    @tool
    def protected() -> str:
        """Record an approval gated effect."""
        effects.append("protected")
        return "changed"

    batch = AIMessage(content="", tool_calls=[dict(name="protected", args={}, id=name, type="tool_call") for name in ("old1", "old2")])
    gate = HumanInTheLoopMiddleware(interrupt_on={"protected": {"allowed_decisions": ["approve", "reject"]}})
    model = Model(responses=[call("marker", "prior"), batch, AIMessage("updated result")])
    async with child(mode, model, tools=[marker, protected], middleware=[gate]) as (tasks, key, job):
        await settled(job)
        old = responses(job)
        assert job.status == "needs_approval" and effects == ["marker"]
        tasks.steer("owner", key, "Keep the evidence and do no protected actions")
        with pytest.raises(ValueError):
            tasks.resume("owner", key, old)
        await settled(job)
        assert job.status == "completed" and effects == ["marker"]
        assert job.steering[0]["status"] == "delivered"


def hook_response(payload):
    request = payload["request"]
    event = request["invocation"]["event"]["event"]
    decision = {"event": event}
    if event == "PreToolUse":
        decision["permission"] = {"behavior": "none"}
    return {"protocol_version": 1, "invocation_id": request["invocation_id"],
            "snapshot_id": request["snapshot_id"], "decision": decision}


@pytest.mark.parametrize("mode", ["isolated", "fork"])
@pytest.mark.parametrize("event", ["PreToolUse", "PostToolUse"])
async def test_real_hook_pause_preserved_until_real_reply(mode, event):
    effects = []

    @tool
    def marker() -> str:
        """Record a harmless effect."""
        effects.append("marker")
        return "PRIVATE_INTERMEDIATE_OUTPUT_PROBE"

    @tool
    def protected() -> str:
        """Record a protected effect."""
        effects.append("protected")
        return "changed"

    mw = [HumanInTheLoopMiddleware(interrupt_on={"protected": {"allowed_decisions": ["approve", "reject"]}}),
          ServerHooksMiddleware(cwd=Path("/private/tmp"), emit_stop=False)]
    context = {"hooks_snapshot_id": "real-test-snapshot", "hooks_server_events": [event], "thread_id": "owner"}
    model = Model(responses=[call("marker", "prior"), call("protected", "obsolete"), AIMessage("updated")]
                  if event == "PreToolUse" else [call("marker", "prior"), AIMessage("updated")])
    async with child(mode, model, tools=[marker, protected], middleware=mw, context=context) as (tasks, key, job):
        await settled(job)
        if event == "PreToolUse":
            first = job.interrupts[0]
            tasks.resume("owner", key, {first["id"]: hook_response(first["value"])})
            await settled(job)
        assert effects == ["marker"] and job.status == "needs_input"
        frozen = tasks.list("owner")[0]["interrupts"]
        inspected = tasks.inspect("owner", key)
        assert inspected["interrupts"][0]["value"]["event"] == event
        assert "request" not in inspected["interrupts"][0]["value"]
        assert "PRIVATE_INTERMEDIATE_OUTPUT_PROBE" not in json.dumps(inspected)
        runtime = ToolRuntime(state={}, context=None, config={"configurable": {"thread_id": "owner"}},
            stream_writer=lambda _: None, tool_call_id="inspect", store=None)
        for tool_name, args in (("list_background_tasks", {}), ("inspect_background_task", {"task_id": key})):
            observer = next(t for t in tasks.tools if t.name == tool_name)
            observed = await observer.ainvoke({**args, "runtime": runtime})
            assert "PRIVATE_INTERMEDIATE_OUTPUT_PROBE" not in json.dumps(observed)
            assert '"invocation":' not in json.dumps(observed)
        if event == "PostToolUse":
            assert "PRIVATE_INTERMEDIATE_OUTPUT_PROBE" in json.dumps(frozen)
        tasks.steer("owner", key, "Do no further protected actions; report the finding")
        await asyncio.sleep(0)
        assert tasks.list("owner")[0]["interrupts"] == frozen
        assert job.steering[0]["status"] == "queued"
        pause = frozen[0]
        reply = hook_response(pause["value"])
        with pytest.raises(ValueError):
            tasks.resume("owner", key, {pause["id"]: {**reply, "snapshot_id": "wrong"}})
        tasks.resume("owner", key, {pause["id"]: reply})
        await settled(job)
        assert job.status == "completed" and effects == ["marker"]
        assert job.steering[0]["status"] == "delivered"


@pytest.mark.parametrize("mode", ["isolated", "fork"])
async def test_final_tool_guard_after_real_hook_allow(mode):
    effects = []

    @tool
    def protected() -> str:
        """Record a protected effect."""
        effects.append("protected")
        return "changed"

    hooks = ServerHooksMiddleware(cwd=Path("/private/tmp"), emit_stop=False)
    context = {"hooks_snapshot_id": "test-snapshot", "hooks_server_events": ["PreToolUse"], "thread_id": "owner"}
    model = Model(responses=[call("protected", "old"), AIMessage("new result")])
    async with child(mode, model, tools=[protected], middleware=[hooks], context=context) as (tasks, key, job):
        await settled(job)
        pause = job.interrupts[0]
        tasks.steer("owner", key, "Skip that action")
        reply = hook_response(pause["value"])
        reply["decision"]["permission"]["behavior"] = "allow"
        tasks.resume("owner", key, {pause["id"]: reply})
        await settled(job)
        assert job.status == "completed" and effects == []
        assert job.activity[0]["status"] == "skipped"


async def test_unknown_input_remains_paused_with_queued_steering():
    @tool
    def unknown() -> str:
        """Raise an unsupported input request."""
        return interrupt({"type": "custom-input", "question": "unhandled"})

    model = Model(responses=[call("unknown", "unknown")])
    async with child("isolated", model, tools=[unknown]) as (tasks, key, job):
        await settled(job)
        frozen = tasks.list("owner")[0]["interrupts"]
        assert tasks.inspect("owner", key)["interrupts"][0]["value"] == {"type": "unsupported_input"}
        tasks.steer("owner", key, "New guidance")
        await asyncio.sleep(0)
        assert job.status == "needs_input" and job.interrupts == frozen
        assert job.activity[-1]["status"] == "paused"
        assert job.steering[0]["status"] == "queued"
        with pytest.raises(ValueError):
            tasks.resume("owner", key, {frozen[0]["id"]: {"arbitrary": True}})
        await tasks.cancel("owner", task_id=key)
        assert job.steering[0]["delivery_outcome"] == "undelivered-terminal"


async def test_owner_capacity_bounds_copying_and_terminal_close():
    tasks = BackgroundTasks()
    job = Job("owner", "child", steerable=True)
    tasks.jobs["child"] = job
    with pytest.raises(ValueError, match="Unknown"):
        tasks.inspect("other", "child")
    with pytest.raises(ValueError, match="Unknown"):
        tasks.steer("other", "child", "change")
    for text in ("", " ", "x" * 2001, None):
        with pytest.raises(ValueError, match="2000"):
            tasks.steer("owner", "child", text)
    for i in range(32):
        item = tasks.steer("owner", "child", "x" * 2000)
        assert item["revision"] == i + 1
    with pytest.raises(ValueError, match="capacity"):
        tasks.steer("owner", "child", "overflow")
    for i in range(160):
        job.record("tool", tool_name="marker", tool_call_id=str(i), status="completed")
    snapshot = tasks.inspect("owner", "child")
    assert len(snapshot["activity"]) == 128 and snapshot["activity"][0]["sequence"] == 33
    snapshot["steering"][0]["status"] = "tampered"
    snapshot["activity"].clear()
    assert job.steering[0]["status"] == "queued" and len(job.activity) == 128
    assert "steering" not in tasks.list("owner")[0] and "activity" not in tasks.list("owner")[0]
    job.status = "completed"
    job.close_inbox()
    assert all(i["delivery_outcome"] == "undelivered-terminal" for i in job.steering)
    with pytest.raises(ValueError, match="no longer"):
        tasks.steer("owner", "child", "late")
    await tasks.close()


async def test_report_ack_requires_delivery_and_findings_are_bounded():
    mw = BackgroundChildMiddleware()
    report = mw.tools[0]
    job = Job("owner", "child", steerable=True, steering=[dict(message_id="id", revision=1, message="guidance", status="queued")])
    assert "error" in await report.ainvoke({"finding": "not a child"})
    token = _CURRENT_JOB.set(job)
    try:
        assert "error" in await report.ainvoke({"finding": "hidden", "acknowledged_message_ids": ["id"]})
        assert not job.activity
        job.steering[0]["status"] = "delivered"
        assert (await report.ainvoke({"finding": "x" * 3000, "acknowledged_message_ids": ["id"]}))["ok"]
        assert len(job.activity[0]["text"]) == 2048 and job.steering[0]["status"] == "acknowledged"
    finally:
        _CURRENT_JOB.reset(token)


async def test_unsupported_child_advertises_no_steering():
    entered, release = asyncio.Event(), asyncio.Event()
    model = Model(responses=[AIMessage("done")], entered=entered, release=release, hold_at=1)
    async with child("isolated", model, steerable=False) as (tasks, key, job):
        await asyncio.wait_for(entered.wait(), 5)
        assert not tasks.inspect("owner", key)["steerable"]
        with pytest.raises(ValueError, match="does not support"):
            tasks.steer("owner", key, "steer")
        release.set()
        await settled(job)
        assert job.status == "completed"


@pytest.mark.parametrize("close", [False, True])
async def test_cancel_before_worker_starts_closes_accepted_inbox(close):
    model = Model(responses=[AIMessage("must not run")])
    async with child("isolated", model) as (tasks, key, job):
        tasks.steer("owner", key, "Guidance before worker starts")
        if close:
            await tasks.close()
        else:
            await tasks.cancel("owner", task_id=key)
        assert job.status == "cancelled" and job.graph is None
        assert job.steering[0]["delivery_outcome"] == "undelivered-terminal"
        assert model.seen == []
        with pytest.raises(ValueError, match="no longer"):
            tasks.steer("owner", key, "too late")


async def test_completion_tail_closes_queued_message_without_restart():
    entered, release = asyncio.Event(), asyncio.Event()

    class CompletionTail(AgentMiddleware):
        async def aafter_agent(self, state, runtime):
            entered.set()
            await release.wait()

    model = Model(responses=[AIMessage("completed finding")])
    async with child("isolated", model, middleware=[CompletionTail()]) as (tasks, key, job):
        await asyncio.wait_for(entered.wait(), 5)
        tasks.steer("owner", key, "Arrived after the final child boundary")
        release.set()
        await settled(job)
        assert job.status == "completed" and len(model.seen) == 1
        assert job.steering[0]["status"] == "queued"
        assert job.steering[0]["delivery_outcome"] == "undelivered-terminal"


async def test_stale_approval_waits_for_worker_publication_and_available_quota():
    @tool
    def protected() -> str:
        """Never execute the obsolete action."""
        raise AssertionError("obsolete execution")

    gate = HumanInTheLoopMiddleware(interrupt_on={"protected": {"allowed_decisions": ["approve", "reject"]}})
    tasks = BackgroundTasks(max_running=1)
    model = Model(responses=[call("protected", "old"), AIMessage("fresh result")])
    async with child("isolated", model, tools=[protected], middleware=[gate], tasks=tasks) as (_, key, job):
        # Hold pause publication in _run.finally: visible status is paused but
        # its worker still owns capacity and must not be resumed concurrently.
        await tasks.changed.acquire()
        try:
            for _ in range(100):
                if job.status == "needs_approval":
                    break
                await asyncio.sleep(.001)
            assert job.status == "needs_approval" and not job.worker.done()
            worker = job.worker
            tasks.steer("owner", key, "Reject pending actions")
            assert job.worker is worker
            with pytest.raises(ValueError, match="publishing"):
                tasks.resume("owner", key, responses(job))
            # A running owner-independent job now occupies the only quota slot.
            blocker_release = asyncio.Event()
            blocker = asyncio.create_task(blocker_release.wait())
            blocker.add_done_callback(lambda _: tasks._drain_stale_approvals())
            tasks.jobs["other"] = Job("other-owner", "other", worker=blocker)
        finally:
            tasks.changed.release()
        await asyncio.wait_for(asyncio.shield(worker), 5)
        await asyncio.sleep(0)
        assert job.worker is worker and job.status == "needs_approval"
        with pytest.raises(ValueError, match="superseded"):
            tasks.resume("owner", key, responses(job))
        blocker_release.set()
        await blocker
        await asyncio.sleep(0)
        await settled(job)
        assert job.status == "completed" and job.steering[0]["status"] == "delivered"


async def test_closed_owner_does_not_auto_resume_stale_approval():
    @tool
    def protected() -> str:
        """Never run after close."""
        raise AssertionError("closed action")

    gate = HumanInTheLoopMiddleware(interrupt_on={"protected": {"allowed_decisions": ["approve", "reject"]}})
    model = Model(responses=[call("protected", "old"), AIMessage("should never ask again")])
    async with child("isolated", model, tools=[protected], middleware=[gate]) as (tasks, key, job):
        await settled(job)
        tasks.max_running = 0
        tasks.steer("owner", key, "queued")
        await tasks.close()
        tasks.max_running = 4
        tasks._drain_stale_approvals()
        assert job.status == "cancelled" and len(model.seen) == 1
        assert job.steering[0]["delivery_outcome"] == "undelivered-terminal"


async def test_cancellation_cleanup_cannot_accept_steering_or_reports():
    entered, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    reports = []

    @tool
    async def slow() -> str:
        """Pause inside cancellation cleanup."""
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            reports.append(await BackgroundChildMiddleware().tools[0].ainvoke({"finding": "late report"}))
            cleanup.set()
            await release.wait()
            raise

    model = Model(responses=[call("slow", "slow")])
    async with child("isolated", model, tools=[slow]) as (tasks, key, job):
        await asyncio.wait_for(entered.wait(), 5)
        cancellation = asyncio.create_task(tasks.cancel("owner", task_id=key))
        await asyncio.wait_for(cleanup.wait(), 5)
        try:
            with pytest.raises(ValueError, match="no longer"):
                tasks.steer("owner", key, "late guidance")
            assert "error" in reports[0]
            assert not any(a.get("text") == "late report" for a in job.activity)
        finally:
            release.set()
        await cancellation
        assert job.status == "cancelled"


@pytest.mark.parametrize("malformed", [
    {"action_requests": [None], "review_configs": []},
    {"action_requests": [{"name": "protected"}], "review_configs": [None]},
    {"action_requests": [{"name": "protected"}], "review_configs": [{"action_name": "protected", "allowed_decisions": None}]},
])
async def test_unsupported_approval_shapes_stay_paused(malformed):
    tasks = BackgroundTasks()
    job = Job("owner", "child", status="needs_approval", steerable=True,
              interrupts=[{"id": "pause", "value": malformed}])
    tasks.jobs["child"] = job
    tasks.steer("owner", "child", "queued guidance")
    assert job.status == "needs_approval" and job.worker is None
    assert job.steering[0]["status"] == "queued"
    await tasks.close()


async def test_public_pause_ids_cannot_authorize_reused_underlying_interrupt():
    from langgraph.types import Interrupt

    payload = {"action_requests": [{"name": "marker", "args": {}}],
               "review_configs": [{"action_name": "marker", "allowed_decisions": ["approve", "reject"]}]}

    class ReusingGraph:
        calls = []

        async def ainvoke(self, value, config):
            self.calls.append(value)
            return {"__interrupt__": [Interrupt(value=payload, id="same-graph-id")]}

    tasks = BackgroundTasks()
    job = Job("owner", "child", graph=ReusingGraph())
    tasks.jobs["child"] = job
    try:
        tasks._launch("child", job, {})
        await settled(job)
        old = responses(job)
        first_id = job.interrupts[0]["id"]
        tasks.resume("owner", "child", old)
        await settled(job)
        assert job.interrupts[0]["id"] != first_id
        assert job.graph.calls[1].resume == {"same-graph-id": {"decisions": [{"type": "approve"}]}}
        with pytest.raises(ValueError, match="does not match"):
            tasks.resume("owner", "child", old)
    finally:
        await tasks.close()


@pytest.mark.parametrize("mode", ["isolated", "fork"])
async def test_runtime_manual_inspection_report_and_steering_survive_reload(tmp_path, mode):
    from deepagents_code._fake_models import _ToolBindingFakeModel
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command
    from lc_factory.runtime import FactoryRuntime, RuntimeOptions

    target = tmp_path / "must-not-exist"
    child_model = Model(responses=[
        call("report_background_task", "finding", {"finding": "Existing evidence"}),
        call("write_file", "obsolete", {"file_path": str(target), "content": "no"}), AIMessage("updated result")])
    parent = _ToolBindingFakeModel(messages=iter([
        call("start_background_task", "launch", {"description": "Find evidence", "subagent_type": "child"}),
        AIMessage("parent ready"), AIMessage("after reload"),
    ]))
    async with await FactoryRuntime.create(agent_kwargs=dict(
        assistant_id="steering-runtime", cwd=tmp_path, model=parent,
        subagents=[dict(name="child", description="Child", mode=mode, model=child_model)],
        enable_memory=False, enable_skills=False, enable_ask_user=False,
        interactive=True, auto_approve=False, checkpointer=InMemorySaver()),
        options=RuntimeOptions(background=True, reload=True), workspace_id="work") as runtime:
        config = {"configurable": {"thread_id": "owner"}}
        output = await runtime.ainvoke({"messages": [HumanMessage("start")]}, config)
        assert output["__interrupt__"] and not runtime.background.jobs
        await runtime.ainvoke(Command(resume={"decisions": [{"type": "approve"}]}), config)
        tasks = runtime.background
        key, job = next(iter(tasks.jobs.items()))
        await settled(job)
        assert job.steerable and job.status == "needs_approval"
        assert any(a.get("text") == "Existing evidence" for a in job.activity)
        assert job.interrupts[0]["value"]["action_requests"][0]["name"] == "write_file"
        previous = runtime.current
        runtime.request_reload()
        await runtime.ainvoke({"messages": [HumanMessage("continue")]}, config)
        assert runtime.current is not previous and runtime.background is tasks
        tasks.steer("owner", key, "Keep existing evidence; skip the write")
        await settled(job)
        assert job.status == "completed" and job.steering[0]["status"] == "delivered"
        assert not target.exists()


@pytest.mark.parametrize("mode", ["isolated", "fork"])
@pytest.mark.parametrize("fresh_structured", [False, True])
async def test_steering_clears_obsolete_tool_strategy_result(tmp_path, mode, fresh_structured):
    from langgraph.checkpoint.memory import InMemorySaver
    from lc_factory.runtime import FactoryRuntime, RuntimeOptions

    entered, release = asyncio.Event(), asyncio.Event()
    schema = {"title": "Finding", "type": "object", "properties": {"summary": {"type": "string"}},
              "required": ["summary"], "additionalProperties": False}
    child_model = Model(responses=[call("Finding", "old", {"summary": "obsolete"}),
        call("Finding", "new", {"summary": "current"}) if fresh_structured else AIMessage("no valid structure")],
        entered=entered, release=release, hold_at=1)
    parent = Model(responses=[call("start_background_task", "launch", {"description": "Find evidence", "subagent_type": "child"}),
                             AIMessage("parent ready")])
    async with await FactoryRuntime.create(agent_kwargs=dict(
        assistant_id="steering-structured", cwd=tmp_path, model=parent,
        subagents=[dict(name="child", description="Child", mode=mode, model=child_model, response_format=schema)],
        enable_memory=False, enable_skills=False, enable_ask_user=False,
        interactive=False, auto_approve=True, checkpointer=InMemorySaver()),
        options=RuntimeOptions(background=True), workspace_id="work") as runtime:
        await runtime.ainvoke({"messages": [HumanMessage("start")]}, {"configurable": {"thread_id": "owner"}})
        await asyncio.wait_for(entered.wait(), 5)
        tasks = runtime.background
        key, job = next(iter(tasks.jobs.items()))
        tasks.steer("owner", key, "Update the summary to current evidence")
        release.set()
        await settled(job)
        if fresh_structured:
            assert job.status == "completed" and job.outcome == {"ok": True, "value": {"summary": "current"}}
        else:
            assert job.status == "failed" and not job.outcome["ok"]
        assert "report_background_task" not in {name for names in parent.bound_names for name in names}
        assert {"inspect_background_task", "steer_background_task"} <= set(parent.bound_names[0])
        assert "report_background_task" in child_model.bound_names[0]
        from langchain_core.messages import ToolMessage
        assert len([m for m in child_model.seen[1] if isinstance(m, ToolMessage) and m.tool_call_id == "old"]) == 1


@pytest.mark.parametrize("mode", ["isolated", "fork"])
async def test_foreground_child_has_no_background_report_tool(mode):
    model = Model(responses=[AIMessage("foreground result")])
    task_tool = _build_task_tool([dict(name="child", description="Foreground", mode=mode,
        model=model, tools=[], middleware=[BackgroundChildMiddleware()])])
    runtime = ToolRuntime(state={"messages": [HumanMessage("Parent context")]}, context=None,
        config={"configurable": {"thread_id": "owner"}}, stream_writer=lambda _: None,
        tool_call_id="foreground", store=None)
    await task_tool.ainvoke({"name": "task", "type": "tool_call", "id": "foreground",
        "args": {"description": "Foreground work", "subagent_type": "child", "runtime": runtime}}, runtime.config)
    assert all("report_background_task" not in names for names in model.bound_names)


async def test_timeout_marks_queued_guidance_undelivered():
    entered, release = asyncio.Event(), asyncio.Event()
    model = Model(responses=[AIMessage("too late")], entered=entered, release=release, hold_at=1)
    async with child("isolated", model, tasks=BackgroundTasks(timeout=.1)) as (tasks, key, job):
        await asyncio.wait_for(entered.wait(), 5)
        tasks.steer("owner", key, "queued during model")
        await settled(job)
        assert job.status == "timed_out" and job.graph is None
        assert job.steering[0]["delivery_outcome"] == "undelivered-terminal"
        assert job.remaining == 0


async def test_opaque_compiled_child_keeps_ordinary_background_lifecycle():
    from langchain_core.runnables import RunnableLambda

    entered, release = asyncio.Event(), asyncio.Event()

    async def opaque(state):
        entered.set()
        await release.wait()
        return {"messages": [AIMessage("opaque evidence")]}

    task_tool = _build_task_tool([dict(name="opaque", description="Opaque child", runnable=RunnableLambda(opaque))])
    runtime = ToolRuntime(state={"messages": [HumanMessage("Parent")]}, context=None,
        config={"configurable": {"thread_id": "owner"}}, stream_writer=lambda _: None,
        tool_call_id="opaque", store=None)
    tasks = BackgroundTasks()
    try:
        handle = tasks._submit(task_tool, {"name": "task", "type": "tool_call", "id": "opaque",
            "args": {"description": "Find evidence", "subagent_type": "opaque"}}, runtime)
        key = handle["task_id"]
        await asyncio.wait_for(entered.wait(), 5)
        assert tasks.inspect("owner", key)["steerable"] is False
        with pytest.raises(ValueError, match="does not support"):
            tasks.steer("owner", key, "unsupported")
        release.set()
        await settled(tasks.jobs[key])
        assert tasks.inspect("owner", key)["outcome"] == {"ok": True, "value": "opaque evidence"}
    finally:
        await tasks.close()
