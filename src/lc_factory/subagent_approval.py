"""Owner mode, child-local Auto state, and host-owned delegation authorization.

Only the retained native task tool establishes a child scope. Its ContextVar
crosses async dispatch and is re-established on checkpoint resume; no model
argument, child message, or checkpoint can manufacture an authorization scope.
The private checkpoint marker binds decisions to that exact dispatch snapshot.
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, replace
from functools import wraps
from hashlib import sha256
import json
from types import SimpleNamespace
from typing import Annotated, NotRequired

from lc_factory.auto_classifier import AutoModeHITLMiddleware
from lc_factory.interpreter_dispatch import _ReplayStableDispatchIds
from lc_factory.upstream import (
    AgentMiddleware, AIMessage, AutoModeState, HumanMessage, PrivateStateAttr,
    ToolMessage, USER_PROMPT_METADATA_KEY, APPROVAL_MODE_NAMESPACE,
    approval_mode_key, _live_mode, _trusted_prompt_rows, _active_user_directives,
)

_SCOPE = ContextVar("factory_child_approval", default=None)
_HUMAN_APPROVALS = ContextVar("factory_child_human_approvals", default=None)
_UNREAD = object()
_DIRECTIVES = ("_goal_objective", "_goal_status", "_goal_rubric", "_sticky_rubric", "rubric")
_MAX_AUTHORIZATION_BYTES = 131072


def _value(context, name):
    return context.get(name) if isinstance(context, Mapping) else getattr(context, name, None)


def _owner(context):
    owner = _value(context, "thread_id")
    key = _value(context, "approval_mode_key")
    return owner if isinstance(owner, str) and owner and key == approval_mode_key(owner) else None


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _action(call):
    return sha256(_json({key: call.get(key) for key in ("id", "name", "args")}).encode()).hexdigest()


@dataclass(frozen=True)
class _ModelSnapshot:
    model: object
    settings: dict
    classifier: object


@dataclass(frozen=True)
class _ChildScope:
    controller: object
    identity: str
    owner: str | None
    authorization: str
    model: _ModelSnapshot | None

    @property
    def evidence(self):
        # Decode a fresh copy; callers cannot mutate the retained snapshot.
        return json.loads(self.authorization)

    @property
    def authorized(self):
        evidence = self.evidence
        return bool(self.owner and self.model and (evidence["prompts"] or evidence["directives"]))


class DelegationApproval:
    """One graph generation's model snapshots and replay-stable dispatch IDs."""
    def __init__(self):
        self._models = OrderedDict()
        self._ids = _ReplayStableDispatchIds()

    def capture_model(self, request):
        owner = _owner(request.runtime.context)
        if owner is None:
            return
        self._models[owner] = _ModelSnapshot(
            request.model, deepcopy(request.model_settings),
            _value(request.runtime.context, "classifier_model"),
        )
        self._models.move_to_end(owner)
        while len(self._models) > 128:
            self._models.popitem(last=False)

    def scope(self, runtime, description, name):
        owner = _owner(runtime.context)
        rows, _ = _trusted_prompt_rows(runtime.state.get("messages", []))
        directives = ({key: runtime.state[key] for key in _DIRECTIVES
                       if isinstance(runtime.state.get(key), str) and runtime.state[key]}
                      if _active_user_directives(runtime.state) else {})
        evidence = _json({"prompts": rows[-20:], "directives": directives})
        # Dropping a trailing restriction would widen consent. Reject oversized
        # evidence as a whole and use human approval instead of truncating text.
        if len(evidence.encode()) > _MAX_AUTHORIZATION_BYTES:
            evidence = _json({"prompts": [], "directives": {}})
        stable = self._ids.next_id(runtime=runtime, parent_eval_id=None,
            description=description, subagent_type=name, label=None, response_schema=None)
        if stable is None:
            info = getattr(runtime, "execution_info", None)
            stable = _json([runtime.config.get("configurable", {}).get("thread_id"),
                            getattr(info, "checkpoint_ns", None), runtime.tool_call_id])
        identity = "child-approval-" + sha256(_json([owner, stable, evidence]).encode()).hexdigest()
        return _ChildScope(self, identity, owner, evidence, self._models.get(owner))

    def bind_task(self, task_tool):
        """Adapt the generation's real tool, including direct interpreter calls."""
        original = task_tool.coroutine

        @wraps(original)
        async def invoke(description, subagent_type, runtime):
            scope = self.scope(runtime, description, subagent_type)
            token = _SCOPE.set(scope)
            try:
                return await original(description=description, subagent_type=subagent_type, runtime=runtime)
            finally:
                _SCOPE.reset(token)

        task_tool.coroutine = invoke
        original_sync = task_tool.func

        @wraps(original_sync)
        def invoke_sync(description, subagent_type, runtime):
            # Auto's live Store and classifier are async, as in the main graph.
            raise RuntimeError("Use async invocation for approval-aware subagents.")

        task_tool.func = invoke_sync


class OwnerAutoModeHITLMiddleware(AutoModeHITLMiddleware):
    def __init__(self, *args, delegation, **kwargs):
        super().__init__(*args, **kwargs)
        self.delegation = delegation

    async def awrap_model_call(self, request, handler):
        self.delegation.capture_model(request)
        return await super().awrap_model_call(request, handler)


class _ModeStore:
    """Route only this child's mode reads to its owner. Counters stay local."""
    def __init__(self, store, scope, *, cache_mode=False):
        self._store, self._scope = store, scope
        self._cache_mode, self._mode_item = cache_mode, _UNREAD

    def __getattr__(self, name):
        return getattr(self._store, name)

    def _key(self, namespace, key):
        scope = self._scope
        if namespace == APPROVAL_MODE_NAMESPACE:
            if scope is None or scope.owner is None or key != approval_mode_key(scope.identity):
                return None
            return approval_mode_key(scope.owner)
        return key

    def _item(self, namespace, item):
        if namespace == APPROVAL_MODE_NAMESPACE and item is not None:
            value = getattr(item, "value", None)
            if isinstance(value, Mapping) and value.get("mode") == "auto" and not self._scope.authorized:
                return SimpleNamespace(value={"mode": "manual"})
        return item

    async def aget(self, namespace, key, **kwargs):
        target = self._key(namespace, key)
        if target is None or self._store is None:
            return None
        if namespace == APPROVAL_MODE_NAMESPACE and self._cache_mode:
            if self._mode_item is _UNREAD:
                try:
                    self._mode_item = await self._store.aget(namespace, target, **kwargs)
                except Exception:
                    # Missing control state has the same fail-closed resolution.
                    self._mode_item = None
            return self._item(namespace, self._mode_item)
        return self._item(namespace, await self._store.aget(namespace, target, **kwargs))

    def get(self, namespace, key, **kwargs):
        target = self._key(namespace, key)
        if target is None or self._store is None:
            return None
        return self._item(namespace, self._store.get(namespace, target, **kwargs))


class ChildApprovalState(AutoModeState):
    _factory_child_approval_scope: NotRequired[Annotated[str, PrivateStateAttr]]
    _factory_child_approved_actions: NotRequired[Annotated[dict, PrivateStateAttr]]
    _factory_child_initial_messages: NotRequired[Annotated[list[str], PrivateStateAttr]]


class ChildAutoModeHITLMiddleware(AutoModeHITLMiddleware):
    state_schema = ChildApprovalState

    def __init__(self, *args, delegation, **kwargs):
        # Parent ask_user receipts do not become child consent. Explicit child
        # action approvals still use the existing exact-action human review.
        kwargs["trusted_ask_user_tool"] = None
        super().__init__(*args, **kwargs)
        self.delegation = delegation
        for tool in self.tools:
            original = tool.func

            def wrap(original):
                @wraps(original)
                def invoke(*args, runtime, **kwargs):
                    return original(*args, runtime=self._runtime(runtime, state=runtime.state), **kwargs)
                return invoke

            tool.func = wrap(original)

    def scope(self):
        scope = _SCOPE.get()
        return scope if scope is not None and scope.controller is self.delegation else None

    def _state(self, state):
        scope = self.scope()
        evidence = scope.evidence if scope else {"prompts": [], "directives": {}}
        # Only original host-captured user metadata supplies classifier consent.
        # Fork history, summaries, assignments and steering remain context only.
        messages = [HumanMessage(content=row["literal_user_text"],
                    additional_kwargs={USER_PROMPT_METADATA_KEY: row}) for row in evidence["prompts"]]
        initial = set(state.get("_factory_child_initial_messages", []))
        # The classifier needs actions after the authorization boundary. Initial
        # fork history belongs to the parent; later child assignments/steering
        # are not user turns. This view never changes the child's model history.
        messages.extend(message for message in state.get("messages", [])
                        if not isinstance(message, HumanMessage) and message.id not in initial)
        result = {key: value for key, value in state.items() if key not in _DIRECTIVES}
        return {**result, **evidence["directives"], "messages": messages}

    def _runtime(self, runtime, *, state=None):
        scope = self.scope()
        identity = scope.identity if scope else "unavailable-child-approval"
        rows = scope.evidence["prompts"] if scope else []
        context = {"thread_id": identity, "approval_mode_key": approval_mode_key(identity),
                   "turn_id": rows[-1].get("turn_id") if rows else None,
                   "approval_mode": _value(runtime.context, "approval_mode"),
                   "classifier_model": scope.model.classifier if scope and scope.model else None}
        fields = {"context": context, "store": _ModeStore(runtime.store, scope)}
        if state is not None:
            fields["state"] = self._state(state)
        return replace(runtime, **fields)

    async def abefore_agent(self, state, runtime):
        scope = self.scope()
        identity = scope.identity if scope else "unavailable-child-approval"
        if state.get("_factory_child_approval_scope") != identity:
            return {"_factory_child_approval_scope": identity, "_auto_decision_plan": None,
                    "_factory_child_approved_actions": {},
                    "_factory_child_initial_messages": [m.id for m in state["messages"] if m.id]}
        return None

    async def awrap_model_call(self, request, handler):
        scope = self.scope()
        state = self._state(request.state)
        fields = {"runtime": self._runtime(request.runtime), "state": state, "messages": state["messages"]}
        if scope and scope.model:
            fields.update(model=scope.model.model, model_settings=deepcopy(scope.model.settings))
        async def actual(_policy_request):
            return await handler(request)
        return await super().awrap_model_call(replace(request, **fields), actual)

    def _process_decision(self, decision, tool_call, config):
        revised, message = super()._process_decision(decision, tool_call, config)
        receipts = _HUMAN_APPROVALS.get()
        if receipts is not None and revised is not None and decision.get("type") in {"approve", "edit"}:
            receipts.append(_action(revised))
        return revised, message

    async def aafter_model(self, state, runtime):
        # Native routing and our disposition must observe the same mode. The
        # admission guard below still reads live state afresh before execution.
        policy_runtime = replace(self._runtime(runtime),
                                 store=_ModeStore(runtime.store, self.scope(), cache_mode=True))
        human_actions = []
        token = _HUMAN_APPROVALS.set(human_actions)
        try:
            result = await super().aafter_model(self._state(state), policy_runtime)
        finally:
            _HUMAN_APPROVALS.reset(token)
        mode = (await _live_mode(policy_runtime))["mode"].value
        # Native YOLO proposals can bypass review even after switching to Auto.
        # Such actions still need fresh review under a stricter live mode.
        if (state.get("_auto_decision_plan") or {}).get("mode_at_proposal") == "yolo":
            mode = "yolo"
        messages = (result or {}).get("messages", state["messages"])
        last = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        scope = self.scope()
        approved = {"scope": scope.identity if scope else None, "mode": mode, "human_actions": human_actions,
                    "actions": [_action(call) for call in last.tool_calls] if last else []}
        return {**(result or {}), "_factory_child_approved_actions": approved}


class ChildApprovalAdmission(AgentMiddleware):
    """Recheck mode and exact actions after hooks, just before execution."""
    def __init__(self, approval):
        self.approval = approval

    async def awrap_tool_call(self, request, handler):
        approval = self.approval
        scope = approval.scope()
        record = request.state.get("_factory_child_approved_actions", {})
        mode = (await _live_mode(approval._runtime(request.runtime)))["mode"].value
        exact = (scope is not None and record.get("scope") == scope.identity
                 and _action(request.tool_call) in record.get("actions", []))
        unchanged = (record.get("mode") == mode or mode == "yolo"
                     or _action(request.tool_call) in record.get("human_actions", []))
        if not exact or not unchanged:
            return ToolMessage("Action skipped: approval mode or action changed. Propose the action again for review.",
                               tool_call_id=request.tool_call["id"], name=request.tool_call["name"], status="error")
        return await handler(request)
