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


def is_child(state):
    return _IN_WORKER.get() or bool(state.get("_deepagents_forked_context"))


def thread_id(config):
    owner = config.get("configurable", {}).get("thread_id")
    if not isinstance(owner, str) or not owner:
        raise ValueError("A nonempty thread_id is required")
    return owner


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
            return self.list(thread_id(runtime.config))

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

        self.tools = [list_background_tasks, cancel_background_task, start_background_task]

    def list(self, owner):
        return [dict(task_id=key, name=j.name, description=j.description, status=j.status,
                     result=j.result, outcome=deepcopy(j.outcome), interrupts=deepcopy(j.interrupts))
                for key, j in self.jobs.items() if j.owner == owner]

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
            "cancel_background_task when needed; do not repeatedly poll. Results are "
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
                  description=str(call["args"].get("description", ""))[:4000], remaining=self.timeout)
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
        job.worker = asyncio.create_task(self._run(job, {}),
                                        name=key, context=contextvars.Context())
        return {"ok": True, "task_id": key, "status": "running"}

    def resume(self, owner, task_id, responses):
        """Accept an exact, user-originated interrupt response once.

        This is a host API, deliberately not exposed as a model tool. A model
        may inspect task status but cannot approve its own protected actions.
        """
        job = self.jobs.get(task_id)
        if job is None or job.owner != owner:
            raise ValueError("Unknown task for this conversation")
        if self.closed or job.status not in ("needs_approval", "needs_input") or not job.interrupts:
            raise ValueError("Task is not waiting for a response")
        if job.worker is not None and not job.worker.done():
            raise ValueError("Task is still publishing its pause; refresh before responding")
        if sum(j.worker is not None and not j.worker.done() for j in self.jobs.values()) >= self.max_running:
            raise ValueError("Background task capacity unavailable; retry when a running task finishes")
        if not isinstance(responses, dict) or set(responses) != {i["id"] for i in job.interrupts}:
            raise ValueError("Response does not match the current task interrupts")
        for item in job.interrupts:
            validate_response(item["value"], responses[item["id"]])
        # No await between validation and claim: concurrent/duplicate decisions
        # cannot schedule two executions or authorize a later interrupt.
        job.interrupts = []
        job.status = "running"
        job.worker = asyncio.create_task(self._run(job, Command(resume=deepcopy(responses))),
                                        name=task_id, context=contextvars.Context())

    async def _run(self, job, value):
        _IN_WORKER.set(True)
        started = monotonic()
        try:
            async with asyncio.timeout(job.remaining):
                with use_environment(self.environ):
                    output = await job.graph.ainvoke(value, job.config)
            interrupts = output.get("__interrupt__", ())
            if interrupts:
                job.interrupts = [{"id": i.id, "value": deepcopy(i.value)} for i in interrupts]
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
                job.graph = None
                job.config = {}
            async with self.changed:
                self.changed.notify_all()

    async def cancel(self, owner=None, *, task_id=None):
        workers = []
        cancelled_jobs = []
        for key, job in self.jobs.items():
            if (owner is None or job.owner == owner) and (task_id is None or key == task_id):
                if job.status in ("needs_approval", "needs_input"):
                    job.status, job.result = "cancelled", "Background task cancelled."
                    job.interrupts, job.graph, job.config = [], None, {}
                    job.outcome = {"ok": False, "error": {"type": "cancelled", "message": job.result}}
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
    if set(response) != {"decisions"} or len(decisions) != len(actions) or not actions:
        raise ValueError("Approval decision count must match requested actions")
    for action, decision in zip(actions, decisions, strict=True):
        allowed = next((r.get("allowed_decisions", []) for r in reviews if r.get("action_name") == action.get("name")), [])
        if not isinstance(decision, dict) or decision.get("type") not in ("approve", "reject") or decision["type"] not in allowed:
            raise ValueError("Decision is not allowed for this action")
        if set(decision) - {"type", "message"} or ("message" in decision and not isinstance(decision["message"], str)):
            raise ValueError("Malformed approval decision")
