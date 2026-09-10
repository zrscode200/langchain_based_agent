"""Guarded idle continuation using the pinned TUI's ordinary agent lifecycle."""
from __future__ import annotations

import asyncio
import contextvars
from contextlib import aclosing, contextmanager
from dataclasses import dataclass

from textual.screen import ModalScreen
from lc_factory.upstream import Command, convert_to_messages

_WAKE = contextvars.ContextVar("lc_factory_background_wake", default=None)
WAKE_CHECKPOINT = "lc_factory_background_wake_checkpoint"


@dataclass
class WakeTurn:
    graph: object
    owner: str
    checkpoint: str
    turn_id: str | None = None
    admitted: bool = False

_BUSY = (
    "_agent_running", "_agent_reconciling", "_goal_state_mutating", "_shell_running",
    "_pending_messages", "_pending_shell_messages", "_reloading", "_connecting", "_resuming",
    "_restart_in_flight", "_startup_sequence_running", "_server_startup_error", "_thread_switching",
    "_model_switching", "_agent_switching", "_model_install_switching", "_approval_mode_blocked",
    "_pending_approval_widget", "_pending_ask_user_widget", "_pending_goal_review_widget",
    "_offload_worker", "_exiting", "_factory_background_input_depth",
)


def idle_for_background(app):
    """Fail closed for unknown clients; running=False alone is not quiescence."""
    quiescent = getattr(app, "_agent_quiescent", None)
    if quiescent is None or not quiescent.is_set() or any(getattr(app, key, False) for key in _BUSY):
        return False
    if not all(getattr(app, key, None) for key in ("_agent", "_ui_adapter", "_session_state")):
        return False
    if getattr(app, "_lc_thread_id", None) in getattr(app, "_factory_background_stopped_owners", set()):
        return False
    if isinstance(app.screen, ModalScreen) or app._modal_command_running() or app._is_user_typing():
        return False
    prompt = getattr(app, "_chat_input", None)
    return not (prompt is not None and prompt.value)


async def remote_idle(identity):
    """Observe persisted interrupts and other clients' runs; never recover them."""
    agent, owner, graph = identity
    snapshot = await agent.aget_state({"configurable": {"thread_id": owner}})
    if snapshot is None or snapshot.next or getattr(snapshot, "interrupts", ()):
        return False
    if any(getattr(task, "interrupts", ()) or getattr(task, "error", None) for task in snapshot.tasks):
        return False
    client = graph._validate_client()
    for status in ("running", "pending"):
        runs = await client.runs.list(owner, status=status, limit=1)
        if not isinstance(runs, list) or runs:
            return False
    checkpoint = snapshot.config.get("configurable", {}).get("checkpoint_id")
    if not isinstance(checkpoint, str) or not checkpoint:
        return None
    from lc_factory.upstream_cli import latest_trusted_turn_id
    messages = convert_to_messages(getattr(snapshot, "values", {}).get("messages", []))
    return WakeTurn(graph, owner, checkpoint, turn_id=latest_trusted_turn_id(messages))


class BackgroundWake:
    def __init__(self, panel):
        self.panel = panel
        self.attempted = set()

    async def consider(self, identity, response):
        pending = response.get("pending_results", [])
        if not isinstance(pending, list) or not all(isinstance(key, str) for key in pending):
            return
        pending = set(pending)
        self.attempted.intersection_update(pending)
        app = self.panel.app
        if not pending - self.attempted or not self.panel.valid(identity) or not idle_for_background(app):
            return
        try:
            async with asyncio.timeout(10):
                admission = await remote_idle(identity)
        except asyncio.CancelledError:
            raise
        except Exception:
            return  # A later status refresh may retry this read-only preflight.
        if not admission or not self.panel.valid(identity) or not idle_for_background(app):
            return
        # One attempt per pending batch. A failed/cancelled stream leaves its
        # outcomes pending for the next user turn, without an automatic loop.
        self.attempted.update(pending)
        app._set_agent_running(True)
        app._active_turn_visible_output_started = False
        app._agent_turn_started = False
        app._active_user_message = None
        try:
            if app._chat_input is not None:
                app._chat_input.set_cursor_active(active=False)
            # No await between final admission, busy reservation and worker
            # ownership. Native cleanup handles approvals, stop and user queue.
            async def run():
                token = _WAKE.set(admission)
                try:
                    await app._run_agent_task("", graph_input={"messages": []})
                finally:
                    _WAKE.reset(token)

            app._agent_worker = app.run_worker(
                run,
                name="_run_agent_task", exclusive=False,
            )
        except BaseException:
            await app._release_unstarted_turn()
            raise


@contextmanager
def client_background_wake(cls):
    """Protect native submission gaps and observe the exact main-cancel path."""
    originals = {name: getattr(cls, name) for name in (
        "_submit_input", "_dispatch_queued_message", "_send_to_agent", "_cancel_worker", "_force_interrupt_active_work")}
    from lc_factory.upstream_cli import RemoteGraph
    original_stream = RemoteGraph.astream

    async def stream(self, input, config=None, **kwargs):
        wake = _WAKE.get()
        if wake is not None:
            configurable = dict((config or {}).get("configurable", {}))
            if self is not wake.graph or configurable.get("thread_id") != wake.owner:
                raise ValueError("Background continuation conversation changed")
            if isinstance(input, Command) or wake.admitted:
                # Native user/hook responses resume the checkpoint created by
                # this turn, not the completed checkpoint that admitted it.
                configurable.pop(WAKE_CHECKPOINT, None)
            else:
                configurable[WAKE_CHECKPOINT] = wake.checkpoint
            wake.admitted = True
            config = {**(config or {}), "configurable": configurable}
            kwargs["multitask_strategy"] = "reject"
            if wake.turn_id is not None:
                # The native graph_input path omits a new user turn marker.
                # Reuse the existing trusted turn for genuine ask_user receipts;
                # this creates neither a new prompt nor new authorization.
                kwargs["context"] = {**(kwargs.get("context") or {}), "turn_id": wake.turn_id}
        async with aclosing(original_stream(self, input, config, **kwargs)) as events:
            async for event in events:
                yield event

    async def submit(self, *args, **kwargs):
        self._factory_background_input_depth = getattr(self, "_factory_background_input_depth", 0) + 1
        try:
            return await originals["_submit_input"](self, *args, **kwargs)
        finally:
            self._factory_background_input_depth -= 1

    async def dispatch(self, message):
        self._factory_background_input_depth = getattr(self, "_factory_background_input_depth", 0) + 1
        if message.mode == "normal":
            getattr(self, "_factory_background_stopped_owners", set()).discard(self._lc_thread_id)
        try:
            return await originals["_dispatch_queued_message"](self, message)
        finally:
            self._factory_background_input_depth -= 1

    async def send(self, *args, **kwargs):
        # Native cleanup can spawn the next queued user/goal worker before the
        # preceding _run_agent_task returns. That successor is an ordinary turn.
        token = _WAKE.set(None)
        try:
            return await originals["_send_to_agent"](self, *args, **kwargs)
        finally:
            _WAKE.reset(token)

    def cancel(self, worker, **kwargs):
        if worker is not None and worker is self._agent_worker:
            stop(self)
        return originals["_cancel_worker"](self, worker, **kwargs)

    def force_interrupt(self, *args, **kwargs):
        stop(self)
        return originals["_force_interrupt_active_work"](self, *args, **kwargs)

    def stop(self):
        if not hasattr(self, "_factory_background_stopped_owners"):
            self._factory_background_stopped_owners = set()
        self._factory_background_stopped_owners.add(self._lc_thread_id)

    cls._submit_input, cls._dispatch_queued_message = submit, dispatch
    cls._send_to_agent = send
    cls._cancel_worker, cls._force_interrupt_active_work = cancel, force_interrupt
    RemoteGraph.astream = stream
    try:
        yield
    finally:
        for name, value in originals.items():
            setattr(cls, name, value)
        RemoteGraph.astream = original_stream
