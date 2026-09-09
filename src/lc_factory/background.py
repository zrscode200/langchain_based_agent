"""Bounded, in-memory background delegation adapted from Talon's task workers.

Copyright (c) LangChain, Inc. MIT License. See TALON_ADAPTATIONS.md.
Only explicit start_background_task calls detach; native task stays foreground.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
from copy import deepcopy
from dataclasses import dataclass, field, replace
from time import monotonic
from uuid import uuid4
from typing import Any, TypedDict

from lc_factory.upstream import (
    AgentMiddleware, Command, GraphInterrupt, SystemMessage,
    ToolRuntime, tool, Runtime, use_environment,
    InMemorySaver, StateGraph, START, END,
)

_IN_WORKER = contextvars.ContextVar("lc_factory_background_worker", default=False)
_CURRENT_JOB = contextvars.ContextVar("lc_factory_background_job", default=None)
_LIVE = {"running", "needs_approval", "needs_input"}


def is_child(state):
    return _IN_WORKER.get() or bool(state.get("_deepagents_forked_context"))


def thread_id(config):
    owner = config.get("configurable", {}).get("thread_id")
    if not isinstance(owner, str) or not owner:
        raise ValueError("A nonempty thread_id is required")
    return owner


def inspection_interrupts(interrupts):
    """Project observable blockers without copying private hook event payloads.

    Genuine hook requests stay on the host-only list/resume transport. Their
    prompts, transcript paths and intermediate tool results are not inspection
    data. Approval actions retain the exact fields used by the user's review.
    """
    result = []
    for item in interrupts:
        payload = item["value"]
        value = {"type": "unsupported_input"}
        if isinstance(payload, dict):
            if payload.get("type") == "hook_invocation":
                value = {"type": "hook_invocation"}
                request = payload.get("request")
                invocation = request.get("invocation") if isinstance(request, dict) else None
                event = invocation.get("event") if isinstance(invocation, dict) else None
                if isinstance(event, dict):
                    name = event.get("event")
                    if name in ("PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop", "UserPromptSubmit", "PermissionRequest"):
                        value["event"] = name
                    call = event.get("call")
                    if isinstance(call, dict) and isinstance(call.get("name"), str):
                        value["tool_name"] = call["name"][:200]
            elif "action_requests" in payload:
                value = {key: deepcopy(payload.get(key)) for key in ("action_requests", "review_configs")}
        result.append({"id": item["id"], "value": value})
    return result


@dataclass
class Job:
    owner: str
    name: str
    worker: asyncio.Task | None = None
    status: str = "running"
    result: str | None = None
    acknowledged: bool = False
    structured: bool = False
    outcome: dict | None = None
    description: str = ""
    interrupts: list[dict] = field(default_factory=list)
    graph: Any = field(default=None, repr=False)
    config: dict = field(default_factory=dict, repr=False)
    remaining: float = 3600
    steerable: bool = False
    inbox_open: bool = True
    steering: list[dict] = field(default_factory=list)
    activity: list[dict] = field(default_factory=list)
    revision: int = 0
    model_revision: int = 0
    activity_sequence: int = 0
    interrupt_ids: dict[str, str] = field(default_factory=dict, repr=False)

    def record(self, kind, **fields):
        self.activity_sequence += 1
        self.activity.append({"sequence": self.activity_sequence, "kind": kind, **fields})
        del self.activity[:-128]

    def close_inbox(self):
        self.inbox_open = False
        for item in self.steering:
            if item["status"] == "queued":
                item["delivery_outcome"] = "undelivered-terminal"


class WorkerState(TypedDict):
    result: Any


class BackgroundTasks(AgentMiddleware):
    """Keep local task calls alive after their parent turn returns.

    Remote async tools retain the SDK's existing lifecycle. Local jobs are
    expendable and are cancelled when this owner closes. Instances are shared
    across graph generations; each worker retains its original compiled tool.
    """
    def __init__(self, *, environ=None, max_running=4, max_jobs=128, timeout=3600):
        self.environ = environ
        self.max_running, self.max_jobs, self.timeout = max_running, max_jobs, timeout
        self.jobs: dict[str, Job] = {}
        self.changed = asyncio.Condition()
        self.closed = False

        @tool
        async def list_background_tasks(runtime: ToolRuntime) -> list[dict]:
            """List this conversation's background tasks, statuses and bounded results."""
            if is_child(runtime.state):
                return []
            return self.list(thread_id(runtime.config), inspection=True)

        @tool
        async def cancel_background_task(task_id: str, runtime: ToolRuntime) -> str:
            """Cancel a background task owned by this conversation."""
            if is_child(runtime.state):
                return "Background control is available to the main agent only."
            owner = thread_id(runtime.config)
            job = self.jobs.get(task_id)
            if job is None or job.owner != owner:
                return "Unknown task for this conversation."
            await self.cancel(owner, task_id=task_id)
            return job.status

        @tool
        async def start_background_task(description: str, subagent_type: str, runtime: ToolRuntime[Any, Any]) -> dict:
            """Start a local subagent in the background; return its running task ID or a failure."""
            task_tool = next((t for t in runtime.tools if getattr(t, "name", None) == "task"), None)
            return self._submit(task_tool, {"name": "task", "id": runtime.tool_call_id,
                "type": "tool_call", "args": {"description": description, "subagent_type": subagent_type}}, runtime)

        @tool
        async def inspect_background_task(task_id: str, runtime: ToolRuntime) -> dict:
            """Inspect a task's bounded activity, explicit findings and steering delivery states."""
            if is_child(runtime.state):
                return {"error": "Background control is available to the main agent only."}
            try:
                return self.inspect(thread_id(runtime.config), task_id)
            except ValueError as exc:
                return {"error": str(exc)}

        @tool
        async def steer_background_task(task_id: str, message: str, runtime: ToolRuntime) -> dict:
            """Queue assignment guidance for a running child. Queued is not delivered or acknowledged.

            Guidance supersedes earlier pending action proposals; it never approves protected actions.
            Completed tasks cannot be restarted or sent more messages.
            """
            if is_child(runtime.state):
                return {"error": "Background control is available to the main agent only."}
            try:
                return self.steer(thread_id(runtime.config), task_id, message)
            except ValueError as exc:
                return {"error": str(exc)}

        self.tools = [list_background_tasks, cancel_background_task, start_background_task,
                      inspect_background_task, steer_background_task]

    @staticmethod
    def _snapshot(key, job, *, inspection=False):
        return dict(task_id=key, name=job.name, description=job.description, status=job.status,
                    result=job.result, outcome=deepcopy(job.outcome),
                    interrupts=inspection_interrupts(job.interrupts) if inspection else deepcopy(job.interrupts),
                    steerable=job.steerable)

    def list(self, owner, *, inspection=False):
        """List host transport snapshots; model callers use the inspection projection."""
        return [self._snapshot(key, j, inspection=inspection)
                for key, j in self.jobs.items() if j.owner == owner]

    def _owned(self, owner, task_id):
        job = self.jobs.get(task_id)
        if job is None or job.owner != owner:
            raise ValueError("Unknown task for this conversation")
        return job

    def inspect(self, owner, task_id):
        job = self._owned(owner, task_id)
        return {**self._snapshot(task_id, job, inspection=True), "steering": deepcopy(job.steering),
                "activity": deepcopy(job.activity)}

    def steer(self, owner, task_id, message):
        job = self._owned(owner, task_id)
        if self.closed or not job.inbox_open or job.status not in _LIVE:
            raise ValueError("Task is no longer accepting steering")
        if not job.steerable:
            raise ValueError("This compiled child does not support steering")
        if not isinstance(message, str) or not message.strip() or len(message) > 2000:
            raise ValueError("Steering must contain 1 to 2000 characters")
        if len(job.steering) >= 32:
            raise ValueError("Task steering capacity unavailable")
        job.revision += 1
        item = {"message_id": f"steering-{uuid4().hex}", "revision": job.revision,
                "message": message, "status": "queued"}
        job.steering.append(item)
        # The worker may still be publishing its pause. Its done callback is
        # the sole scheduler until it has actually exited and released quota.
        self._drain_stale_approvals()
        return deepcopy(item)

    def pending(self, owner):
        return {key: j.result for key, j in self.jobs.items()
                if j.owner == owner and j.result is not None and not j.acknowledged}

    def acknowledge(self, owner, ids):
        for key in ids:
            job = self.jobs.get(key)
            if job is not None and job.owner == owner:
                job.acknowledged = True

    async def wait(self, owner):
        """Wait for results or user interaction; closing wakes all waiters."""
        async with self.changed:
            await self.changed.wait_for(lambda: bool(self.pending(owner)) or self.closed or any(
                job.owner == owner and job.interrupts for job in self.jobs.values()))
            return self.pending(owner)

    async def awrap_model_call(self, request, handler):
        if is_child(request.state):
            names = {t.name for t in self.tools}
            return await handler(request.override(tools=[t for t in request.tools
                                                         if getattr(t, "name", "") not in names]))
        # ModelRequest has a Runtime, not a RunnableConfig. Results are injected
        # by the runtime's before-agent middleware using the graph config.
        blocks = request.system_message.content_blocks if request.system_message else []
        system = SystemMessage(content_blocks=[*blocks, {"type": "text", "text":
            "Use start_background_task to start local background work and immediately receive an ID. "
            "A needs_approval or needs_input task is paused, not finished. Direct the user to its "
            "background task review controls; do not restart it or approve it yourself. "
            "The task and task_settled tools and JavaScript task() wait for foreground completion. "
            "Continue the conversation while it runs. Use list_background_tasks or "
            "inspect_background_task to inspect bounded activity and explicit findings, and "
            "steer_background_task to queue further assignment guidance. Queued guidance is not "
            "delivered until the child model receives it, and acknowledgment requires its explicit "
            "report. Pending earlier actions are superseded, never approved by steering. "
            "Use cancel_background_task when needed; do not repeatedly poll. Results are "
            "delivered on the next conversation turn. Remote async tools keep their "
            "ordinary start/check/cancel behavior."}])
        return await handler(request.override(system_message=system))

    def _submit(self, task_tool, call, parent_runtime):
        def failure(message):
            return {"ok": False, "error": {"type": "SubmissionError", "message": message}}
        if is_child(parent_runtime.state):
            return failure("Background submission is available to the main agent only.")
        owner = thread_id(parent_runtime.config)
        self.jobs = {k: j for k, j in self.jobs.items()
                     if not (j.acknowledged and j.worker is not None and j.worker.done())}
        if self.closed or len(self.jobs) >= self.max_jobs or sum(
                j.worker is not None and not j.worker.done() for j in self.jobs.values()
        ) >= self.max_running:
            return failure("Background task capacity unavailable.")
        if task_tool is None:
            return failure("No permitted compiled task tool is available.")
        metadata = getattr(task_tool, "metadata", None) or {}
        name = str(call["args"].get("subagent_type", ""))
        names = metadata.get("lc_factory_subagent_names")
        if names is not None and name not in names:
            return failure("Unknown local subagent.")
        key = f"background-{uuid4().hex}"
        job = Job(owner, name, structured=name in metadata.get("lc_factory_structured_subagents", ()),
                  description=str(call["args"].get("description", ""))[:4000], remaining=self.timeout,
                  steerable=name in metadata.get("lc_factory_steerable_subagents", ()))
        # Snapshot before detaching; no mutable parent state, callbacks, stream
        # writer, checkpoint namespace or Pregel runner survives this boundary.
        state = deepcopy(parent_runtime.state)
        context = deepcopy(parent_runtime.context)
        config = {"configurable": {"thread_id": key,
                  "__pregel_runtime": Runtime(context=context, store=parent_runtime.store)},
                  "recursion_limit": parent_runtime.config.get("recursion_limit", 500)}
        runtime = replace(parent_runtime, config=config, state=state,
                          context=context, stream_writer=lambda _: None)

        async def run(state: WorkerState, config):
            # The wrapper's node config holds the child checkpoint namespace
            # and resume scratchpad. Reusing the original detached config here
            # would repeat completed child steps instead of resuming them.
            child_runtime = replace(runtime, config=config)
            result = await task_tool.ainvoke(
                {**call, "args": {**call["args"], "runtime": child_runtime}}, config)
            return {"result": result}

        builder = StateGraph(WorkerState)
        builder.add_node("run", run)
        builder.add_edge(START, "run")
        builder.add_edge("run", END)
        job.graph = builder.compile(checkpointer=InMemorySaver())
        job.config = config
        self.jobs[key] = job
        self._launch(key, job, {})
        return {"ok": True, "task_id": key, "status": "running"}

    def _launch(self, key, job, value):
        job.worker = asyncio.create_task(self._run(job, value), name=key, context=contextvars.Context())
        job.worker.add_done_callback(lambda _: self._drain_stale_approvals())

    def _drain_stale_approvals(self):
        if self.closed:
            return
        for key, job in self.jobs.items():
            if sum(j.worker is not None and not j.worker.done() for j in self.jobs.values()) >= self.max_running:
                return
            if (not job.inbox_open or job.status != "needs_approval" or job.model_revision >= job.revision
                    or (job.worker is not None and not job.worker.done())):
                continue
            # Unsupported inputs and genuine Hooks-v2 pauses are untouched.
            # Only the exact supported ordinary approval batch is rejected.
            try:
                responses = {item["id"]: {"decisions": [
                    {"type": "reject", "message": "Superseded by newer parent steering."}
                    for _ in item["value"]["action_requests"]]} for item in job.interrupts}
                for item in job.interrupts:
                    validate_response(item["value"], responses[item["id"]])
            except (AttributeError, KeyError, TypeError, ValueError):
                continue
            if responses:
                self._resume_job(key, job, responses)

    def _resume_job(self, task_id, job, responses):
        actual = {job.interrupt_ids.get(key, key): deepcopy(value) for key, value in responses.items()}
        for item in job.interrupts:
            payload = item["value"]
            if isinstance(payload, dict) and "action_requests" in payload:
                for action, decision in zip(payload["action_requests"], responses[item["id"]]["decisions"], strict=True):
                    if decision["type"] == "reject":
                        job.record("tool", tool_name=action["name"], status="skipped")
        job.interrupts, job.interrupt_ids = [], {}
        job.status = "running"
        self._launch(task_id, job, Command(resume=actual))

    def resume(self, owner, task_id, responses):
        """Accept an exact, user-originated interrupt response once.

        This is a host API, deliberately not exposed as a model tool. A model
        may inspect task status but cannot approve its own protected actions.
        """
        job = self._owned(owner, task_id)
        if self.closed or not job.inbox_open or job.status not in ("needs_approval", "needs_input") or not job.interrupts:
            raise ValueError("Task is not waiting for a response")
        if job.worker is not None and not job.worker.done():
            raise ValueError("Task is still publishing its pause; refresh before responding")
        if sum(j.worker is not None and not j.worker.done() for j in self.jobs.values()) >= self.max_running:
            raise ValueError("Background task capacity unavailable; retry when a running task finishes")
        if not isinstance(responses, dict) or set(responses) != {i["id"] for i in job.interrupts}:
            raise ValueError("Response does not match the current task interrupts")
        for item in job.interrupts:
            validate_response(item["value"], responses[item["id"]])
            if (isinstance(item["value"], dict) and "action_requests" in item["value"]
                    and job.model_revision < job.revision):
                raise ValueError("Approval was superseded by newer steering")
        # No await between validation and claim: concurrent/duplicate decisions
        # cannot schedule two executions or authorize a later interrupt.
        self._resume_job(task_id, job, responses)

    async def _run(self, job, value):
        _IN_WORKER.set(True)
        _CURRENT_JOB.set(job)
        started = monotonic()
        try:
            async with asyncio.timeout(job.remaining):
                with use_environment(self.environ):
                    output = await job.graph.ainvoke(value, job.config)
            if not job.inbox_open:
                # A tool may finish cleanup after swallowing cancellation. Its
                # late result cannot reopen the cancelled task or its pause.
                raise asyncio.CancelledError
            interrupts = output.get("__interrupt__", ())
            if interrupts:
                # Public IDs are single-pause capabilities. Underlying graph
                # interrupt IDs can repeat across resumed checkpoint generations.
                job.interrupt_ids = {f"pause-{uuid4().hex}": i.id for i in interrupts}
                job.interrupts = [{"id": key, "value": deepcopy(i.value)}
                                  for key, i in zip(job.interrupt_ids, interrupts, strict=True)]
                job.status = ("needs_approval" if all(isinstance(i.value, dict) and
                    "action_requests" in i.value for i in interrupts) else "needs_input")
                return
            result = output.get("result")
            if isinstance(result, Command):
                update = result.update if isinstance(result.update, dict) else {}
                if update.get("__interrupt__"):
                    raise ValueError("Child did not publish a resumable checkpoint")
                else:
                    messages = update.get("messages", [])
                    if not messages:
                        raise ValueError("No result returned")
                    if getattr(messages[-1], "status", None) == "error":
                        raise ValueError("Subagent returned a tool error")
                    job.result = str(messages[-1].content)
            else:
                if getattr(result, "status", None) == "error" or result is None:
                    raise ValueError("Subagent returned an error or no result")
                job.result = str(getattr(result, "content", result))
            if job.status == "running":
                if len(job.result) > 64000:
                    raise ValueError("Subagent result exceeds the output limit")
                value = json.loads(job.result) if job.structured else job.result
                job.outcome = {"ok": True, "value": value}
                job.status = "completed"
            job.result = job.result[:64000]
        except GraphInterrupt:
            job.status, job.result = "failed", "Subagent interrupted without a resumable checkpoint."
        except asyncio.CancelledError:
            job.status, job.result = "cancelled", "Background task cancelled."
        except TimeoutError:
            job.status, job.result = "timed_out", "Background task exceeded its time limit."
        except Exception:
            job.status, job.result = "failed", "Background task failed before returning a result."
        finally:
            job.remaining = max(0, job.remaining - (monotonic() - started))
            if job.outcome is None and job.status not in ("needs_approval", "needs_input"):
                job.outcome = {"ok": False, "error": {"type": job.status, "message": job.result}}
            if job.status not in ("needs_approval", "needs_input"):
                job.close_inbox()
                job.graph = None
                job.config = {}
                job.interrupts, job.interrupt_ids = [], {}
            async with self.changed:
                self.changed.notify_all()

    async def cancel(self, owner=None, *, task_id=None):
        workers = []
        cancelled_jobs = []
        for key, job in self.jobs.items():
            if (owner is None or job.owner == owner) and (task_id is None or key == task_id):
                # Revoke inbox acceptance before cancellation cleanup can yield.
                job.close_inbox()
                if job.status in ("needs_approval", "needs_input"):
                    job.status, job.result = "cancelled", "Background task cancelled."
                    job.interrupts, job.graph, job.config = [], None, {}
                    job.outcome = {"ok": False, "error": {"type": "cancelled", "message": job.result}}
                    job.interrupt_ids = {}
                    job.close_inbox()
                if job.worker is not None and not job.worker.done():
                    job.worker.cancel()
                    workers.append(job.worker)
                    cancelled_jobs.append(job)
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
            # A worker cancelled before its first coroutine step never reaches
            # _run's cancellation handler.
            for job in cancelled_jobs:
                if job.status == "running":
                    job.status, job.result = "cancelled", "Background task cancelled."
                    job.outcome = {"ok": False, "error": {"type": job.status, "message": job.result}}
                    job.graph, job.config = None, {}
                    job.close_inbox()
            async with self.changed:
                self.changed.notify_all()

        async with self.changed:
            self.changed.notify_all()

    async def discard(self, owner):
        await self.cancel(owner)
        self.jobs = {key: job for key, job in self.jobs.items() if job.owner != owner}

    async def close(self):
        self.closed = True
        await self.cancel()
        async with self.changed:
            self.changed.notify_all()


def validate_response(payload, response):
    """Validate the complete approval batch or matching hook transport reply."""
    if not isinstance(payload, dict) or not isinstance(response, dict):
        raise ValueError("Unsupported task response")
    if payload.get("type") == "hook_invocation":
        from lc_factory.upstream import validate_background_hook_response
        validate_background_hook_response(payload, response)
        return
    actions, reviews = payload.get("action_requests"), payload.get("review_configs")
    decisions = response.get("decisions")
    if not isinstance(actions, list) or not isinstance(reviews, list) or not isinstance(decisions, list):
        raise ValueError("Task requires a supported approval response")
    if (any(not isinstance(action, dict) or not isinstance(action.get("name"), str) for action in actions)
            or any(not isinstance(review, dict) or not isinstance(review.get("action_name"), str)
                   or not isinstance(review.get("allowed_decisions"), list) for review in reviews)):
        raise ValueError("Task requires a supported approval response")
    if set(response) != {"decisions"} or len(decisions) != len(actions) or not actions:
        raise ValueError("Approval decision count must match requested actions")
    for action, decision in zip(actions, decisions, strict=True):
        allowed = next((r.get("allowed_decisions", []) for r in reviews if r.get("action_name") == action.get("name")), [])
        if not isinstance(decision, dict) or decision.get("type") not in ("approve", "reject") or decision["type"] not in allowed:
            raise ValueError("Decision is not allowed for this action")
        if set(decision) - {"type", "message"} or ("message" in decision and not isinstance(decision["message"], str)):
            raise ValueError("Malformed approval decision")
