"""Child-only observation and steering at native model/tool boundaries."""
from __future__ import annotations

import asyncio
from typing import Annotated, NotRequired

from lc_factory.background import _CURRENT_JOB
from lc_factory.upstream import (
    AgentMiddleware, AgentState, AIMessage, Command, GraphInterrupt, HumanMessage, PrivateStateAttr,
    SystemMessage, ToolMessage, convert_to_messages, hook_config, tool,
)


def tool_result_failed(result, call_id):
    """Read only the matching call's status from native validated tool results."""
    if isinstance(result, list):
        return any(tool_result_failed(item, call_id) for item in result)
    if isinstance(result, Command):
        messages = result.update.get("messages", []) if isinstance(result.update, dict) else (
            result.update if isinstance(result.update, list) else [])
        return any(isinstance(message, ToolMessage) and message.tool_call_id == call_id
                   and message.status == "error" for message in convert_to_messages(messages))
    return isinstance(result, ToolMessage) and result.tool_call_id == call_id and result.status == "error"


class BackgroundChildState(AgentState):
    _background_steering_revision: NotRequired[Annotated[int, PrivateStateAttr]]


class BackgroundChildMiddleware(AgentMiddleware):
    """Last child middleware, distinct from the inherited main middleware.

    Job identity is process-local and survives native wrapper resume through a
    ContextVar. Only the revision and stable message IDs enter child checkpoints.
    Foreground children have no job and keep their ordinary lifecycle.
    """
    state_schema = BackgroundChildState

    def __init__(self):
        @tool
        async def report_background_task(finding: str = "", acknowledged_message_ids: list[str] | None = None) -> dict:
            """Report an explicit finding to your parent and optionally acknowledge delivered steering IDs.

            Report concise observable findings, never private reasoning or raw tool output.
            Acknowledge only steering messages you have received and understood.
            """
            job = _CURRENT_JOB.get()
            if job is None or not job.inbox_open:
                return {"error": "Reporting is available to an active background child only."}
            ids = set(acknowledged_message_ids or [])
            delivered = {item["message_id"] for item in job.steering
                         if item["status"] in ("delivered", "acknowledged")}
            if ids - delivered:
                return {"error": "Only delivered steering messages can be acknowledged."}
            if finding.strip():
                job.record("finding", text=finding[:2048])
            for item in job.steering:
                if item["message_id"] in ids:
                    item["status"] = "acknowledged"
            return {"ok": True, "acknowledged_message_ids": sorted(ids)}

        self.tools = [report_background_task]

    async def abefore_model(self, state, runtime):
        job = _CURRENT_JOB.get()
        if job is not None:
            job.transcript.capture(state.get("messages", []))
        if job is None or state.get("_background_steering_revision", 0) >= job.revision:
            return None
        items = [item for item in job.steering
                 if item["revision"] > state.get("_background_steering_revision", 0)]
        return {"_background_steering_revision": job.revision,
                "messages": [HumanMessage(
                    content=(f"Parent steering {item['message_id']} (revision {item['revision']}):\n"
                             f"{item['message']}\n"
                             "Apply this within your existing assignment and permissions. Earlier pending "
                             "action proposals were superseded. Acknowledge receipt explicitly with "
                             "report_background_task when understood."), id=item["message_id"])
                    for item in items]}

    async def awrap_model_call(self, request, handler):
        job = _CURRENT_JOB.get()
        if job is None:
            return await handler(request.override(tools=[t for t in request.tools
                if getattr(t, "name", "") != "report_background_task"]))
        # This runs inside the factory's hooks/retry wrappers at actual model
        # admission, not when the inbox is merely checkpointed before_model.
        ids = {message.id for message in request.messages}
        for item in job.steering:
            if item["status"] == "queued" and item["message_id"] in ids:
                item["status"] = "delivered"
        job.model_revision = request.state.get("_background_steering_revision", 0)
        blocks = request.system_message.content_blocks if request.system_message else []
        system = SystemMessage(content_blocks=[*blocks, {"type": "text", "text":
            "You are performing a short-lived background assignment. Use report_background_task "
            "for concise explicit findings the parent may inspect, and to acknowledge delivered "
            "steering IDs. Do not report private reasoning or dump tool output. Finish your "
            "assignment normally; there is no idle inbox or later restart."}])
        job.transcript.capture([system, *request.messages])
        response = await handler(request.override(system_message=system))
        job.transcript.capture(response.result)
        return response

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state, runtime):
        job = _CURRENT_JOB.get()
        if job is not None:
            job.transcript.capture(state.get("messages", []))
        if job is None or state.get("_background_steering_revision", 0) >= job.revision:
            return None
        messages = state["messages"]
        last_index = next((i for i in reversed(range(len(messages))) if isinstance(messages[i], AIMessage)), None)
        last = messages[last_index] if last_index is not None else None
        calls = last.tool_calls if last is not None else []
        answered = {m.tool_call_id for m in messages[last_index + 1:] if isinstance(m, ToolMessage)} if last_index is not None else set()
        for call in calls:
            job.record("tool", tool_name=call["name"], tool_call_id=call["id"], status="skipped")
        # Reverse after_model ordering runs this before approval policy. Close
        # every obsolete call in the transcript, then obtain a fresh proposal.
        update = {"messages": [self._superseded(call) for call in calls if call["id"] not in answered],
                  "jump_to": "model"}
        # ToolStrategy can already have materialized the obsolete answer in
        # this same model node. Do not let a subsequent tool-free reply reuse it.
        if "structured_response" in state:
            update["structured_response"] = None
        return update

    @staticmethod
    def _superseded(call):
        return ToolMessage("Action skipped: superseded by newer parent steering.",
                           tool_call_id=call["id"], name=call["name"], status="error")

    async def awrap_tool_call(self, request, handler):
        job = _CURRENT_JOB.get()
        if job is None:
            return await handler(request)
        call = request.tool_call
        fields = {"tool_name": call["name"], "tool_call_id": call["id"]}
        # Hooks and HITL can suspend earlier wrappers after model completion.
        # Recheck here, immediately before execution; never cancel or replay a
        # tool that was admitted before the new message arrived.
        if request.state.get("_background_steering_revision", 0) < job.revision:
            job.record("tool", **fields, status="skipped")
            return self._superseded(call)
        job.record("tool", **fields, status="started")
        try:
            result = await handler(request)
        except GraphInterrupt:
            job.record("tool", **fields, status="paused")
            raise
        except asyncio.CancelledError:
            job.record("tool", **fields, status="cancelled")
            raise
        except BaseException:
            job.record("tool", **fields, status="failed")
            raise
        job.record("tool", **fields, status="failed" if tool_result_failed(result, call["id"]) else "completed")
        messages = result.update.get("messages", []) if isinstance(result, Command) and isinstance(result.update, dict) else (
            [result] if isinstance(result, ToolMessage) else [])
        job.transcript.capture(convert_to_messages(messages))
        return result
