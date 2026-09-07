"""Opt-in settled delegation and replay-aware interpreter lifecycle.

Adapted from the enterprise distribution at d57a0ea. Outcomes, policy-aware
binding, schema checks, eval identity and interruption propagation are owned here.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from functools import wraps
from hashlib import sha256
from threading import Lock
from typing import Any
from weakref import WeakKeyDictionary
from weakref import ref

from lc_factory.upstream import (
    AgentMiddleware, GraphInterrupt, StructuredTool, ToolRuntime, ToolStrategy,
    SUBAGENT_RESPONSE_FORMAT_CONFIG_KEY, import_subagent_dispatch, Command,
)
from lc_factory.structured_results import result_validator

logger = logging.getLogger(__name__)
SETTLED_DISPATCH_TOOL_NAME = "task_settled"
SUBAGENT_PHASE_COMPLETE = "phase_complete"
MAX_CONCURRENT_DISPATCHES = 32
MAX_RESULT_CHARS = 64000
_SCOPE_KEY = "__factory_eval_scope"


def _failure(error_type, message):
    return {"ok": False, "error": {"type": error_type, "message": str(message)[:2048]}}


def _find_task_tool(tools):
    for tool in tools or ():
        if getattr(tool, "name", None) == "task" and {"description", "subagent_type"} <= set(getattr(tool, "args", {})):
            return tool
    return None


def _is_child(state):
    return bool(state.get("_deepagents_forked_context") or state.get("_deepagents_subagent_name"))


def _emit(writer, event):
    if (isinstance(event, dict) and event.get("type") == "subagent"
            and event.get("phase") == "error" and isinstance(event.get("error"), str)):
        event = {**event, "error": event["error"][:2048]}
    try:
        if callable(writer):
            writer(event)
    except Exception:
        logger.debug("Unable to emit delegation lifecycle event")


@dataclass
class _EvalScope:
    eval_id: str
    interrupts: list = field(default_factory=list)


def _eval_scope(runtime):
    return runtime.config.get("configurable", {}).get(_SCOPE_KEY)


class _ReplayStableDispatchIds:
    """Allocate one panel row id per logical dispatch across node replay.

    LangGraph resumes an interrupt by entering the node again from its beginning.
    The PTC bridge and upstream dispatcher both mint fresh random ids on that path,
    so neither id identifies the logical call. ``ExecutionInfo`` supplies the two
    missing dimensions:

    - checkpoint/task fields identify the node execution and stay stable on resume;
    - first-attempt time plus attempt number identify one entry into that node.

    Calls with different payloads use their payload fingerprint. Repeated identical
    payloads get an occurrence number within that node entry, so legitimate duplicate
    siblings remain separate while the same sequence reuses those numbers on replay.
    """

    _MAX_TRACKED_EXECUTIONS = 256

    def __init__(self) -> None:
        self._counts: OrderedDict[
            tuple[str, ...], tuple[tuple[float, int], dict[str, int]]
        ] = OrderedDict()
        self._lock = Lock()

    def next_id(
        self,
        *,
        runtime: Any,  # noqa: ANN401
        parent_eval_id: str | None,
        description: str,
        subagent_type: str,
        label: str | None,
        response_schema: dict[str, Any] | None,
    ) -> str | None:
        """Return the replay-stable id for this dispatch, if runtime data permits."""
        execution_info = getattr(runtime, "execution_info", None)
        task_id = getattr(execution_info, "task_id", None)
        first_attempt_time = getattr(
            execution_info, "node_first_attempt_time", None
        )
        node_attempt = getattr(execution_info, "node_attempt", None)
        if (
            not isinstance(task_id, str)
            or not task_id
            or not isinstance(first_attempt_time, (int, float))
            or isinstance(first_attempt_time, bool)
            or not isinstance(node_attempt, int)
            or isinstance(node_attempt, bool)
            or node_attempt < 1
        ):
            # Without both a stable node key and a replay-local entry key, guessing
            # could merge two real siblings. Preserve upstream's random ids instead.
            return None

        logical_key = (
            str(getattr(execution_info, "thread_id", None) or ""),
            str(getattr(execution_info, "checkpoint_ns", None) or ""),
            str(getattr(execution_info, "checkpoint_id", None) or ""),
            task_id,
            parent_eval_id or "",
        )
        attempt_marker = (float(first_attempt_time), node_attempt)
        signature = sha256(
            json.dumps(
                {
                    "description": description,
                    "subagent_type": subagent_type,
                    "label": label,
                    "response_schema": response_schema,
                },
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()

        with self._lock:
            current = self._counts.get(logical_key)
            if current is None or current[0] != attempt_marker:
                signature_counts: dict[str, int] = {}
                self._counts[logical_key] = (attempt_marker, signature_counts)
            else:
                signature_counts = current[1]
            self._counts.move_to_end(logical_key)
            occurrence = signature_counts.get(signature, 0)
            signature_counts[signature] = occurrence + 1
            while len(self._counts) > self._MAX_TRACKED_EXECUTIONS:
                self._counts.popitem(last=False)

        digest_payload = json.dumps(
            [*logical_key, signature, occurrence],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode()
        return f"ptc_task_{sha256(digest_payload).hexdigest()[:24]}"


class _CheckedTaskTool:
    """Retain dispatcher identity while recording pauses and explicit tool errors."""
    def __init__(self, tool, scope=None):
        self.tool, self.scope = tool, scope

    def __getattr__(self, name):
        return getattr(self.tool, name)

    async def arun(self, *args, **kwargs):
        try:
            result = await self.tool.arun(*args, **kwargs)
        except GraphInterrupt as exc:
            if self.scope is not None:
                self.scope.interrupts.append(exc)
            raise
        update = result.update if isinstance(result, Command) else None
        if isinstance(update, dict):
            messages = update.get("messages", [])
            terminal = messages[-1] if messages else None
        else:
            terminal = result
        if getattr(terminal, "status", None) == "error":
            raise ValueError(str(terminal.content)[:2048])
        return result


def build_settled_dispatch_tool(subagent_names, *, max_concurrent=MAX_CONCURRENT_DISPATCHES):
    """Build a generation-bound dispatcher; scopes limits to each active loop."""
    names = frozenset(subagent_names)
    if not 1 <= max_concurrent <= MAX_CONCURRENT_DISPATCHES:
        raise ValueError("Invalid settled dispatch concurrency limit")
    limiters, lock = WeakKeyDictionary(), Lock()
    ids = _ReplayStableDispatchIds()

    def limiter():
        loop = asyncio.get_running_loop()
        with lock:
            reference = limiters.get(loop)
            semaphore = reference() if reference is not None else None
            if semaphore is None:
                semaphore = asyncio.Semaphore(max_concurrent)
                limiters[loop] = ref(semaphore)
            return semaphore

    async def task_settled(description: str, subagentType: str, runtime: ToolRuntime[Any, Any],
                           label: str | None = None, responseSchema: dict[str, Any] | None = None) -> dict:
        if _is_child(runtime.state):
            return _failure("DelegationError", "A subagent cannot recursively delegate at this SDK pin.")
        if subagentType not in names:
            return _failure("LookupError", f"Unknown local subagent: {subagentType}")
        task_tool = _find_task_tool(runtime.tools)
        if task_tool is None:
            return _failure("LookupError", "No permitted task tool is available.")
        dispatch, validate_budget, ensure_title = import_subagent_dispatch()
        scope = _eval_scope(runtime)
        parent_id = scope.eval_id if scope is not None else runtime.tool_call_id
        dispatch_runtime = replace(runtime, tool_call_id=parent_id)
        validator = None
        try:
            if responseSchema is not None:
                validate_budget(responseSchema)
                schema = ensure_title(responseSchema)
                validator = result_validator(schema)
                config = {**runtime.config, "configurable": {
                    **runtime.config.get("configurable", {}),
                    SUBAGENT_RESPONSE_FORMAT_CONFIG_KEY: ToolStrategy(schema),
                }}
                dispatch_runtime = replace(dispatch_runtime, config=config)
        except Exception as exc:
            return _failure(type(exc).__name__, "Invalid response schema: " + str(exc))

        stable_id = ids.next_id(runtime=runtime, parent_eval_id=parent_id,
            description=description, subagent_type=subagentType, label=label, response_schema=responseSchema)
        outer_id, completion = None, None

        def write(event):
            nonlocal outer_id, completion
            if isinstance(event, dict) and event.get("type") == "subagent":
                if outer_id is None and event.get("phase") == "start":
                    outer_id = event.get("id")
                if outer_id is not None and event.get("id") == outer_id:
                    event = {**event, "id": stable_id or outer_id}
                    if event.get("phase") == "complete":
                        completion = event
                        return
            _emit(runtime.stream_writer, event)

        dispatch_runtime = replace(dispatch_runtime, stream_writer=write)
        try:
            async with limiter():
                value = await dispatch(_CheckedTaskTool(task_tool, scope),
                    description=description, subagent_type=subagentType, response_schema=None,
                    runtime=dispatch_runtime, label=label)
            if validator is not None:
                if isinstance(value, str):
                    value = json.loads(value)
                validator.validate(value)
            if len(json.dumps(value, ensure_ascii=False)) > MAX_RESULT_CHARS:
                raise ValueError("Delegation result exceeds the 64000-character limit; save an artifact instead.")
        except GraphInterrupt:
            raise
        except Exception as exc:
            if completion is not None:
                _emit(runtime.stream_writer, {**completion, "phase": "error", "error": str(exc)[:2048]})
            return _failure(type(exc).__name__, str(exc))
        if completion is not None:
            _emit(runtime.stream_writer, completion)
        return {"ok": True, "value": value}

    @wraps(task_settled)
    def task_settled_sync(*args, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(task_settled(*args, **kwargs))
        raise RuntimeError("Use async invocation for settled dispatch inside an active event loop.")

    return StructuredTool.from_function(name=SETTLED_DISPATCH_TOOL_NAME,
        func=task_settled_sync, coroutine=task_settled,
        description=("Run one local subagent to completion. Arguments use subagentType and optional responseSchema. "
                     "Returns {ok:true,value} or {ok:false,error:{type,message}}. Approvals pause execution. "
                     "This is foreground work even when native task runs in the background. "
                     "In JavaScript use tools.taskSettled and collect its outcomes without try/catch."))


class SettledDispatchMiddleware(AgentMiddleware):
    def __init__(self, subagent_names):
        self.tools = [build_settled_dispatch_tool(subagent_names)]


def _with_eval_lifecycle(runtime, ids):
    scope = _EvalScope(runtime.tool_call_id)
    source_ids = {}

    def write(event):
        if isinstance(event, dict) and event.get("type") == "subagent" and event.get("eval_id") == scope.eval_id:
            source_id = event.get("id")
            if event.get("phase") == "start" and source_id:
                stable = ids.next_id(runtime=runtime, parent_eval_id=scope.eval_id,
                    description=event.get("description", ""), subagent_type=event.get("subagent_type", ""),
                    label=event.get("label"), response_schema=None)
                if stable:
                    source_ids[source_id] = stable
            if source_id in source_ids:
                event = {**event, "id": source_ids[source_id]}
        _emit(runtime.stream_writer, event)

    config = {**runtime.config, "configurable": {**runtime.config.get("configurable", {}), _SCOPE_KEY: scope}}
    tools = [_CheckedTaskTool(t, scope) if t is _find_task_tool(runtime.tools) else t for t in runtime.tools]
    return replace(runtime, config=config, tools=tools, stream_writer=write), scope


def instrument_interpreter_lifecycle(middleware):
    """Preserve the upstream eval/slot implementation, wrapping only its boundary."""
    ids = _ReplayStableDispatchIds()

    def finish(runtime, scope):
        if scope.interrupts:
            raise scope.interrupts[0]
        _emit(runtime.stream_writer, {"type": "subagent", "phase": SUBAGENT_PHASE_COMPLETE,
                                      "eval_id": runtime.tool_call_id})

    def wrap_sync(original):
        @wraps(original)
        def wrapped(runtime, *args, **kwargs):
            nested, scope = _with_eval_lifecycle(runtime, ids)
            result = original(nested, *args, **kwargs)
            finish(runtime, scope)
            return result
        return wrapped

    def wrap_async(original):
        @wraps(original)
        async def wrapped(runtime, *args, **kwargs):
            nested, scope = _with_eval_lifecycle(runtime, ids)
            result = await original(nested, *args, **kwargs)
            finish(runtime, scope)
            return result
        return wrapped

    for tool in middleware.tools:
        if tool.func is not None:
            tool.func = wrap_sync(tool.func)
        if tool.coroutine is not None:
            tool.coroutine = wrap_async(tool.coroutine)
    return middleware
