"""Exclusive browser handoff; the existing TUI remains the backend owner."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import functools
import inspect

from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from lc_factory.web_launcher import WebLaunchError, WebProcess, bind, require_supported_hooks, transport


def has_native_goal_state(values):
    # Includes deferred proposals and one-shot rubrics, not just active goals.
    return values.get("goal_criteria_request") is not None or any(value for key, value in values.items() if key.startswith(("_goal_", "_pending_goal_"))
               or key in {"rubric", "_sticky_rubric", "goal_criteria_request"})


async def quiescent(agent, owner):
    client = agent._get_graph()._validate_client()
    for status in ("running", "pending"):
        if await client.runs.list(owner, status=status, limit=1):
            return False
    state = await agent.aget_state({"configurable": {"thread_id": owner}})
    if state.next or getattr(state, "interrupts", ()):
        return False
    if any(getattr(t, "interrupts", ()) for t in state.tasks):
        return False
    return True


def eligible(app):
    from lc_factory.background_wake import _BUSY
    if not getattr(app, "_agent_quiescent", asyncio.Event()).is_set() or any(
        getattr(app, key, False) for key in _BUSY if key != "_factory_background_input_depth"
    ):
        raise WebLaunchError("Wait for the current work and pending requests to finish, then use /web-frontend.")
    if not app._agent or not app._session_state:
        raise WebLaunchError("Wait for the agent to connect first.")
    if (getattr(app, "_server_kwargs", None) or {}).get("sandbox_type", "none") != "none":
        raise WebLaunchError("Browser handoff currently supports local workspaces. Keep this sandbox session in the TUI.")
    require_supported_hooks(app._hooks)


class BrowserHandoff(ModalScreen):
    DEFAULT_CSS = """
    BrowserHandoff { align: center middle; }
    BrowserHandoff > Vertical { width: 78; height: auto; max-height: 90%; padding: 1 2; border: round $accent; background: $surface; }
    BrowserHandoff Static { height: auto; margin-bottom: 1; }
    BrowserHandoff Horizontal { height: auto; }
    BrowserHandoff Button { margin-right: 1; }
    """
    BINDINGS = [("escape", "return_to_terminal", "Return to terminal"), ("ctrl+c", "quit_session", "Quit session")]

    def __init__(self, agent, owner, cwd):
        super().__init__()
        self.agent, self.owner, self.cwd = agent, owner, cwd
        self.web = None
        self.operation = asyncio.Lock()
        self.completed = asyncio.Event()
        self.transferred = False
        self.uncertain = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Continue in your browser", markup=False)
            yield Static("Starting the web frontend…", id="web-address", markup=False)
            yield Static("The terminal is waiting. Finish any active turn or approval in the browser, then return here. Keep this terminal open.", id="web-status", markup=False)
            with Horizontal():
                yield Button("Return to terminal", id="web-return", variant="primary")
                yield Button("Reconnect browser", id="web-reconnect")
                yield Button("Quit session", id="web-quit")

    def status(self, message):
        self.query_one("#web-status", Static).update(message)

    async def start_browser(self):
        if self.web is not None and self.web.process.returncode is None:
            await self.web.control("resume")
            return
        if self.transferred:
            self.uncertain = True
        await bind(self.agent, self.owner, self.cwd)
        self.web = await WebProcess.start(agent=self.agent, cwd=self.cwd, thread_id=self.owner, attached=True, runtime_context={
            "model": self.app._effective_model_spec(),
            "model_params": self.app._model_params_override or {},
            "profile_overrides": self.app._profile_override or {},
            "summarization_model": self.app._summarization_model_override,
            "auto_classifier_model": self.app._auto_classifier_context_value(),
        })
        self.query_one("#web-address", Static).update(self.web.url)
        self.transferred = True

    async def on_button_pressed(self, event: Button.Pressed):
        event.stop()
        if event.button.id == "web-quit":
            self.action_quit_session()
        elif event.button.id == "web-return":
            self.run_worker(self.return_control(), exclusive=False)
        else:
            self.run_worker(self.reconnect(), exclusive=False)

    def action_return_to_terminal(self):
        self.run_worker(self.return_control(), exclusive=False)

    def action_quit_session(self):
        self.app.exit()

    async def reconnect(self):
        if self.operation.locked():
            return
        async with self.operation:
            try:
                await self.start_browser()
                self.status("Use the address above. Finish the active turn and approvals there before returning to the terminal.")
            except Exception as exc:
                self.status(str(exc))

    async def return_control(self):
        if self.operation.locked():
            return
        async with self.operation:
            if self.transferred and (self.uncertain or self.web is None or self.web.process.returncode is not None):
                self.uncertain = True
                self.status("The frontend stopped before control could be handed back. Reconnect browser to inspect or finish its work, then Quit session and resume this conversation in the TUI. Terminal input remains paused.")
                return
            self.status("Checking the conversation before returning control…")
            try:
                if self.web is not None and self.web.process.returncode is None:
                    await self.web.control("pause")
                async with asyncio.timeout(20):
                    if not await quiescent(self.agent, self.owner):
                        if self.web is not None and self.web.process.returncode is None:
                            await self.web.control("resume")
                        self.status("The conversation still has an active or paused turn. Finish or cancel it in the browser first. If its server stopped, use Reconnect browser.")
                        return
                    # Fetch before clearing, so backend errors leave the TUI's
                    # existing transcript intact and input still suspended.
                    payload = await self.app._fetch_thread_history_data(self.owner)
                    response = await transport(self.agent).get("/store/items", params={
                        "namespace": "deepagents_code.approval_mode",
                        "key": __import__("hashlib").sha256(self.owner.encode()).hexdigest(),
                    })
                    response.raise_for_status()
                    mode = (response.json() or {}).get("value", {}).get("mode", "manual")
                    from lc_factory.upstream_cli import web_client_modules
                    _, _, _, approval = web_client_modules()
                    mode = approval.ApprovalMode(mode)
                if self.web is not None:
                    await self.web.stop()
                await self.app._clear_messages()
                await self.app._load_thread_history(preloaded_payload=payload, resolve_pending_goal=False)
                # Native history rendering logs some errors rather than raising.
                # Verify its material result before allowing another submission.
                if any(self.app._message_store.get_message(m.id) is None for m in payload.messages):
                    raise WebLaunchError("History could not be fully restored. Retry Return to terminal; input remains paused.")
                self.app._on_approval_mode_fallback(str(mode))
                self.completed.set()
            except Exception as exc:
                self.status(f"Control is still held here: {exc}. Retry Return to terminal or Reconnect browser.")


async def handoff(app):
    from lc_factory.background_ui import BackgroundPanel
    screen = None
    panels = list(app.query(BackgroundPanel))
    try:
        agent, owner, cwd = app._agent, app._lc_thread_id, app._cwd
        for panel in panels:
            panel.invalidate()
            await panel.drain_hooks()
        await bind(agent, owner, cwd)
        async with asyncio.timeout(20):
            if not await quiescent(agent, owner):
                raise WebLaunchError("Finish the paused turn or approval before handing this conversation to the browser.")
            snapshot = await agent.aget_state({"configurable": {"thread_id": owner}})
            if has_native_goal_state(snapshot.values):
                raise WebLaunchError("This conversation has native goal or rubric state. Continue it in the TUI.")
        if app._agent is not agent or app._lc_thread_id != owner:
            raise WebLaunchError("Conversation changed during handoff. Try again.")
        screen = BrowserHandoff(agent, owner, cwd)
        await app.push_screen(screen)
        try:
            async with screen.operation:
                await screen.start_browser()
        except Exception as exc:
            screen.status(str(exc) + " Retry Reconnect browser or Return to terminal.")
        await screen.completed.wait()
    except asyncio.CancelledError:
        if screen is not None and screen.transferred and not getattr(app, "_exiting", False):
            screen.status("The handoff was interrupted. Use Return to terminal to reconcile before continuing.")
            await screen.completed.wait()
        else:
            raise
    except Exception as exc:
        app.notify(str(exc), severity="error", timeout=10, markup=False)
    finally:
        if screen is not None and screen.web is not None:
            await screen.web.stop()
        if screen is not None and app.screen is screen and not getattr(app, "_exiting", False):
            await app.pop_screen()
        for panel in panels:
            panel.suspended_identity = None
            panel.schedule_refresh()
        app._factory_web_handoff = False


@contextmanager
def web_tui_command():
    from lc_factory.upstream_cli import app_module, web_command_registry
    registry, cls = web_command_registry(), app_module.DeepAgentsApp
    originals = []
    def replace(obj, name, value):
        originals.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)
    command = "/web-frontend"
    replace(registry, "COMMANDS", registry.COMMANDS + (registry.SlashCommand(
        name=command, description="Continue this conversation in the web frontend", bypass_tier=registry.BypassTier.QUEUED,
    ),))
    for name in ("QUEUE_BOUND", "ALL_CLASSIFIED"):
        replace(registry, name, getattr(registry, name) | {command})
    original_handle = cls._handle_command
    async def handle(self, text):
        if getattr(self, "_factory_web_handoff", False):
            self.notify("Return control from the browser handoff screen first.", markup=False)
            return
        if not text.strip() or text.strip().lower().split(maxsplit=1)[0] != command:
            return await original_handle(self, text)
        if text.strip().lower() != command:
            self.notify("Use /web-frontend without arguments.", markup=False)
            return
        try:
            eligible(self)
        except WebLaunchError as exc:
            self.notify(str(exc), severity="warning", markup=False)
            return
        self._factory_web_handoff = True
        if self._schedule_off_message_pump(handoff(self), context="web-frontend") is None:
            self._factory_web_handoff = False
    replace(cls, "_handle_command", handle)
    # Native keyboard shortcuts and force-bypass input can skip the modal queue.
    # Block them while the browser owns control; only the handoff screen acts.
    names = {name for name in dir(cls) if name.startswith("action_")} | {
        "_submit_input", "_send_to_agent", "_dispatch_queued_message", "_force_interrupt_active_work",
    }
    for name in names:
        original = getattr(cls, name)
        if not callable(original):
            continue
        def guard(fn):
            if inspect.iscoroutinefunction(fn):
                @functools.wraps(fn)
                async def wrapped(self, *args, **kwargs):
                    if not getattr(self, "_factory_web_handoff", False):
                        return await fn(self, *args, **kwargs)
                return wrapped
            @functools.wraps(fn)
            def wrapped(self, *args, **kwargs):
                if not getattr(self, "_factory_web_handoff", False):
                    return fn(self, *args, **kwargs)
            return wrapped
        replace(cls, name, guard(original))
    original_exit = cls.exit
    def exit_app(self, *args, **kwargs):
        # Revoke the child promptly before native exit tears down the backend.
        if isinstance(self.screen, BrowserHandoff) and self.screen.web is not None:
            process = self.screen.web.process
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
        return original_exit(self, *args, **kwargs)
    replace(cls, "exit", exit_app)
    try:
        yield
    finally:
        for obj, name, value in reversed(originals):
            setattr(obj, name, value)
