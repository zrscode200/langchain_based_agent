"""Live background task status and user-only child approval controls."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from copy import deepcopy
import json
from urllib.parse import quote

from rich.text import Text
from textual import on
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Collapsible, Static

_MAX_DETAIL = 24_000
_ACTIVE = {"queued", "running", "needs_approval", "needs_input"}
_STATUS_LABELS = {"needs_approval": "Waiting for approval", "needs_input": "Waiting for input",
                  "queued": "Queued", "running": "Running", "completed": "Completed", "cancelled": "Cancelled",
                  "failed": "Failed", "timed_out": "Timed out"}


def plain(value, limit=600):
    """Render untrusted task text literally, with bounded terminal output."""
    value = str(value)
    value = "".join(c if (ord(c) >= 32 and not 127 <= ord(c) <= 159) or c in "\n\t" else f"\\x{ord(c):02x}" for c in value)
    return value if len(value) <= limit else value[:limit] + "\n[truncated]"


async def background_request(agent, owner, *, is_current=lambda: True, **operation):
    workspace = await agent._workspace_for_thread({"configurable": {"thread_id": owner}})
    # Workspace resolution may await a reconnect. Never send a decision to the
    # replacement connection/conversation after that await.
    if not is_current():
        raise ValueError("Conversation changed; reopen this task")
    return await agent._get_graph().client.http.post(
        f"/lc-factory/threads/{quote(owner, safe='')}/background",
        json={"workspace": workspace, "operation": "list", **operation},
    )


def approval_responses(job, decision):
    if job.get("status") != "needs_approval" or decision not in ("approve", "reject"):
        raise ValueError("This task is not waiting for a tool approval")
    responses = {}
    for item in job.get("interrupts", []):
        payload = item["value"]
        actions = payload.get("action_requests", []) if isinstance(payload, dict) else []
        if not actions or item["id"] in responses:
            raise ValueError("This task requires an unsupported response")
        responses[item["id"]] = {"decisions": [{"type": decision} for _ in actions]}
    if not responses:
        raise ValueError("Task is no longer waiting for approval")
    return responses


def allows(job, decision):
    try:
        from lc_factory.background import validate_response
        responses = approval_responses(job, decision)
        for item in job["interrupts"]:
            validate_response(item["value"], responses[item["id"]])
        return True
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def review_actions(job):
    """Validate display inputs before constructing native file preview widgets."""
    items = job.get("interrupts")
    if not isinstance(items, list) or not items:
        return None
    result = []
    for item in items:
        value = item.get("value") if isinstance(item, dict) else None
        actions = value.get("action_requests") if isinstance(value, dict) else None
        if not isinstance(actions, list) or not actions:
            return None
        for action in actions:
            if not isinstance(action, dict):
                return None
            name, args = action.get("name"), action.get("args")
            if not isinstance(name, str) or not name or not isinstance(args, dict):
                return None
            fields = {"write_file": ("file_path", "content"),
                      "edit_file": ("file_path", "old_string", "new_string")}.get(name, ())
            if any(not isinstance(args.get(key), str) for key in fields):
                return None
            result.append((name, args))
    return result


class TaskReview(ModalScreen):
    """Opened explicitly by the user; never consumes the parent's approval UI."""
    BINDINGS = [("escape", "back", "Back")]
    DEFAULT_CSS = """
    TaskReview { align: center middle; }
    TaskReview > Vertical { width: 90%; max-width: 100; height: 85%; border: round $primary; background: $surface; padding: 1 2; }
    TaskReview VerticalScroll { height: 1fr; }
    TaskReview .tool-approval-widget { height: auto; }
    TaskReview .background-action-title { color: $primary; text-style: bold; margin-top: 1; }
    TaskReview Horizontal { height: 3; }
    TaskReview Button { margin-right: 1; width: 1fr; min-width: 0; }
    TaskReview.-compact Horizontal { layout: grid; grid-size: 2; grid-rows: 3 3; height: 6; }
    """

    def __init__(self, job, submit):
        super().__init__()
        self.job, self.submit = deepcopy(job), submit
        self.pending = False
        self.attempted = False
        self.actions = review_actions(self.job)
        detail = json.dumps(self.job.get("interrupts") or {
            "result": self.job.get("result"), "outcome": self.job.get("outcome")
        }, indent=2, ensure_ascii=False)
        # Escaping controls can expand text; decisions require every displayed
        # action to fit after escaping as well.
        escaped = plain(detail, max(len(detail) * 6, _MAX_DETAIL))
        self.truncated = len(escaped) > _MAX_DETAIL
        self.detail = escaped[:_MAX_DETAIL] + "\n[truncated]" if self.truncated else escaped

    def can_decide(self, decision):
        return self.actions is not None and not self.truncated and allows(self.job, decision)

    def compose(self):
        with Vertical():
            yield Static(Text(plain(f"{self.job['name']} — {_STATUS_LABELS.get(self.job['status'], self.job['status'])}")))
            with VerticalScroll():
                yield Static(Text(plain(self.job.get("description", ""), 6000)))
                if self.job["status"] == "needs_approval" and not self.truncated and self.actions is not None:
                    from lc_factory.upstream_cli import background_approval_widgets
                    widgets = background_approval_widgets()
                    for name, args in self.actions:
                        yield Static(Text(plain(name.replace("_", " ").capitalize())), classes="background-action-title")
                        if name == "write_file":
                            yield widgets.WriteFileApprovalWidget({key: args[key] for key in ("file_path", "content")})
                        elif name == "edit_file":
                            yield widgets.EditFileApprovalWidget({key: args[key] for key in ("file_path", "old_string", "new_string")})
                        else:
                            yield Static(Text(plain(json.dumps(args, indent=2, ensure_ascii=False), _MAX_DETAIL)))
                        if name in ("write_file", "edit_file"):
                            with Collapsible(title="Full action arguments", collapsed=True):
                                yield Static(Text(plain(json.dumps(args, indent=2, ensure_ascii=False), _MAX_DETAIL)))
                elif self.job["status"] == "needs_approval":
                    yield Static(Text(self.detail))
                elif self.job["status"] in _ACTIVE:
                    yield Static(Text("This task is working." if self.job["status"] == "running" else
                                      "This task is waiting for input. You can cancel it below."))
                else:
                    yield Static(Text(plain(self.job.get("result") or "No result returned.", _MAX_DETAIL)))
            warning = ""
            if self.job["status"] in ("needs_approval", "needs_input") and not any(
                self.can_decide(d) for d in ("approve", "reject")
            ):
                warning = "Task remains blocked: this request cannot be fully reviewed here. You can cancel it."
            yield Static(Text(warning), id="background-review-error")
            with Horizontal():
                yield Button("Approve", id="background-approve", disabled=not self.can_decide("approve"))
                yield Button("Deny", id="background-reject", disabled=not self.can_decide("reject"))
                yield Button("Cancel task", id="background-cancel", disabled=self.job["status"] not in _ACTIVE)
                yield Button("Back", id="background-back")

    def action_back(self):
        if not self.pending:
            self.dismiss()

    def on_resize(self, event):
        self.set_class(event.size.width < 76, "-compact")

    @on(Button.Pressed)
    async def decide(self, event):
        event.stop()
        action = (event.button.id or "").removeprefix("background-")
        if action == "back":
            self.action_back()
            return
        if self.attempted or action not in ("approve", "reject", "cancel"):
            return
        if action == "cancel":
            if self.job["status"] not in _ACTIVE:
                return
            operation = {"operation": "cancel", "task_id": self.job["task_id"]}
        else:
            if not self.can_decide(action):
                return
            operation = {"operation": "resume", "task_id": self.job["task_id"],
                         "responses": approval_responses(self.job, action)}
        self.attempted = self.pending = True
        for button in self.query(Button):
            button.disabled = True
        try:
            await self.submit(**operation)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.query_one("#background-review-error", Static).update(Text(
                "Request was not confirmed. Close and reopen the task to refresh its state."))
            self.query_one("#background-back", Button).disabled = False
            # A timed-out request may have succeeded. Do not retry it against a
            # potentially different pause or disclose raw transport errors.
        else:
            self.dismiss()
        finally:
            self.pending = False


class BackgroundPanel(Vertical):
    """Hidden transport controller feeding the existing dynamic subagent panel."""
    DEFAULT_CSS = "BackgroundPanel { display: none; }"

    def __init__(self):
        super().__init__(id="factory-background-panel")
        from lc_factory.background_wake import BackgroundWake
        self.wake = BackgroundWake(self)
        self.identity = None
        self.snapshot = None
        self.jobs = []
        self.polling = False
        self.hook_seen = set()
        self.hook_errors = set()
        self.hook_tasks = {}
        self.hook_cleanup = set()
        self.details = {}
        self.closed = False
        self.display = False
        self.suspended_identity = None

    def compose(self):
        return iter(())

    def presentation(self):
        return self.app.query_one("#subagent-panel")

    def invalidate(self):
        """A conversation reset hides old rows before the new thread ID arrives."""
        self.suspended_identity = self.current()
        self.cancel_hooks()
        self.details.clear()

    def present_jobs(self):
        panel = self.presentation()
        panel.on_background_reset = self.invalidate
        self.details = {j["task_id"]: (self.identity, deepcopy(j)) for j in self.jobs}
        panel.set_background_jobs(self.jobs, self.show_task)

    def on_mount(self):
        self.timer = self.set_interval(1.0, self.schedule_refresh)
        self.schedule_refresh()

    async def on_unmount(self):
        self.closed = True
        self.timer.stop()
        self.workers.cancel_node(self)
        self.cancel_hooks()
        await self.drain_hooks()

    def current(self):
        agent = getattr(self.app, "_agent", None)
        owner = getattr(self.app, "_lc_thread_id", None)
        graph = agent._get_graph() if agent is not None and hasattr(agent, "_get_graph") else None
        return agent, owner, graph

    @staticmethod
    def same_identity(left, right):
        return (left is not None and right is not None and left[0] is right[0]
                and left[1] == right[1] and left[2] is right[2])

    def valid(self, identity):
        return (not self.closed and not self.same_identity(identity, self.suspended_identity)
                and self.same_identity(self.current(), identity))

    def cancel_hooks(self, *, obsolete_only=False, task_id=None):
        for key, (task, current) in tuple(self.hook_tasks.items()):
            if task_id is not None and key[0] != task_id:
                continue
            if obsolete_only and current():
                continue
            self.hook_tasks.pop(key)
            task.cancel()
            self.hook_cleanup.add(task)

    async def drain_hooks(self):
        pending = tuple(self.hook_cleanup)
        if pending:
            # Status refresh cancellation must not cancel cleanup a second time.
            await asyncio.shield(asyncio.gather(*pending, return_exceptions=True))
            self.hook_cleanup.difference_update(pending)

    def hook_current(self, identity, job, hooks, runtime):
        return (self.valid(identity) and getattr(self.app, "_hooks", None) is hooks
                and getattr(hooks, "_runtime", None) is runtime
                and any(j["task_id"] == job["task_id"] and j["status"] == "needs_input"
                        and j.get("interrupts") == job.get("interrupts") for j in self.jobs))

    def switch_identity(self, identity):
        self.cancel_hooks()
        self.identity, self.snapshot, self.jobs = identity, None, []
        self.details.clear()
        self.hook_seen.clear()
        self.hook_errors.clear()
        self.presentation().clear_background_jobs()
        if not self.same_identity(identity, self.suspended_identity):
            self.suspended_identity = None

    def schedule_refresh(self):
        if self.closed:
            return
        try:
            identity = self.current()
        except Exception:
            self.cancel_hooks()
            self.details.clear()
            self.presentation().clear_background_jobs()
            self.presentation().set_background_notice("Background connection unavailable")
            return
        if not self.same_identity(identity, self.identity):
            self.switch_identity(identity)
            self.workers.cancel_group(self, "background-status")
        if not self.polling:
            self.run_worker(self.refresh_tasks(), group="background-status", exclusive=True, exit_on_error=False)

    async def refresh_tasks(self):
        if self.polling or self.closed:
            return
        self.polling = True
        try:
            identity = self.current()
            agent, owner, graph = identity
            if not self.same_identity(identity, self.identity):
                self.switch_identity(identity)
            await self.drain_hooks()
            self.cancel_hooks(obsolete_only=True)
            await self.drain_hooks()
            if not self.valid(identity) or agent is None or owner is None or graph is None or not hasattr(agent, "_workspace_for_thread"):
                return
            async with asyncio.timeout(15):
                response = await background_request(agent, owner, is_current=lambda: self.valid(identity))
            if not self.valid(identity):
                return
            jobs = response["tasks"]
            snapshot = json.dumps(jobs, sort_keys=True)
            if snapshot != self.snapshot:
                prior = {job["task_id"]: job["status"] for job in self.jobs}
                self.snapshot, self.jobs = snapshot, deepcopy(jobs)
                self.cancel_hooks(obsolete_only=True)
                await self.drain_hooks()
                if not self.valid(identity):
                    return
                notifications = 0
                for job in jobs:
                    if notifications < 5 and job["status"] in ("needs_approval", "needs_input", "completed", "failed", "timed_out", "cancelled") and prior.get(job["task_id"]) != job["status"]:
                        notifications += 1
                        self.app.notify(plain(f"Background {job['name']}: {_STATUS_LABELS.get(job['status'], job['status'])}"), markup=False)
            self.present_jobs()
            paused_keys = {(j["task_id"], tuple(i["id"] for i in j.get("interrupts", [])))
                           for j in jobs if j["status"] == "needs_input"}
            self.hook_seen.intersection_update(paused_keys)
            self.hook_errors.intersection_update(paused_keys)
            self.update_summary()
            # Hooks use the same client-owned transport as foreground runs.
            # Slow hooks must not block status polling or seize the approval UI.
            for job in jobs:
                interrupts = job.get("interrupts", [])
                if job["status"] != "needs_input" or not interrupts or not all(
                    isinstance(i.get("value"), dict) and i["value"].get("type") == "hook_invocation" for i in interrupts
                ):
                    continue
                key = (job["task_id"], tuple(i["id"] for i in interrupts))
                hooks = getattr(self.app, "_hooks", None)
                if key in self.hook_seen or hooks is None:
                    continue
                self.hook_seen.add(key)
                captured_job = deepcopy(job)
                runtime = getattr(hooks, "_runtime", None)
                def current(job=captured_job, hooks=hooks, runtime=runtime):
                    return self.hook_current(identity, job, hooks, runtime)
                task = asyncio.create_task(self.fulfill_hooks(identity, captured_job, hooks, key, current))
                self.hook_tasks[key] = task, current
            await self.wake.consider(identity, response)
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self.closed and self.valid(self.identity):
                self.presentation().set_background_notice("Background status unavailable — reconnecting")
        finally:
            self.polling = False

    def update_summary(self):
        self.presentation().set_background_notice(
            "Hook response unconfirmed; review or cancel the waiting task." if self.hook_errors else "")

    async def fulfill_hooks(self, identity, job, hooks, key, current):
        from lc_factory.upstream_cli import fulfill_background_hook

        try:
            replies = {}
            for item in job["interrupts"]:
                if not current():
                    return
                replies[item["id"]] = await fulfill_background_hook(hooks, item["value"])
            if not current():
                return
            await self.submit(identity, is_current=current, operation="resume", task_id=job["task_id"], responses=replies)
        except asyncio.CancelledError:
            raise
        except Exception:
            if current():
                self.hook_errors.add(key)
                self.update_summary()

    async def submit(self, identity, *, is_current=lambda: True, **operation):
        def current():
            return self.valid(identity) and is_current()

        if not current():
            raise ValueError("Conversation changed; reopen this task")
        agent, owner, _ = identity
        async with asyncio.timeout(15):
            response = await background_request(agent, owner, is_current=current, **operation)
        if not current():
            raise ValueError("Conversation changed; reopen this task")
        if operation.get("operation") == "cancel":
            self.cancel_hooks(task_id=operation["task_id"])
            await self.drain_hooks()
        self.snapshot = None
        self.schedule_refresh()
        return response

    def review_task(self, task_id):
        detail = self.details.get(task_id)
        if detail is None:
            return
        identity, job = detail
        if not self.valid(identity):
            return

        async def submit(**operation):
            return await self.submit(identity, **operation)

        self.app.push_screen(TaskReview(job, submit))

    def show_task(self, task_id):
        """Open the selected child's live activity without redirecting chat."""
        from lc_factory.background_activity import TaskActivity
        detail = self.details.get(task_id)
        if detail is None:
            return
        identity, job = detail
        if not self.valid(identity):
            return

        def current():
            return self.valid(identity)

        async def inspect(*, transcript_page=-1):
            agent, owner, _ = identity
            async with asyncio.timeout(15):
                response = await background_request(agent, owner, is_current=current,
                                                    operation="inspect", task_id=task_id, transcript_page=transcript_page)
            if not current():
                raise ValueError("Conversation changed; reopen this task")
            return response["task"]

        async def submit(**operation):
            return await self.submit(identity, **operation)

        self.app.push_screen(TaskActivity(job, inspect, submit, current))


@contextmanager
def client_background_tasks():
    from lc_factory.upstream_cli import app_module
    from lc_factory.background_panel import background_subagent_panel_class
    from lc_factory.background_wake import client_background_wake
    cls = app_module.DeepAgentsApp
    original = cls.on_mount
    original_panel = app_module.SubagentPanel
    app_module.SubagentPanel = background_subagent_panel_class(original_panel)

    async def mount(self):
        await original(self)
        container = self.query_one("#bottom-app-container")
        if not self.query(BackgroundPanel):
            await container.mount(BackgroundPanel(), before=0)

    cls.on_mount = mount
    try:
        with client_background_wake(cls):
            yield
    finally:
        cls.on_mount = original
        app_module.SubagentPanel = original_panel
