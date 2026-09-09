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
from textual.widgets import Button, Static

_MAX_DETAIL = 24_000
_MAX_ROWS = 100
_ACTIVE = {"running", "needs_approval", "needs_input"}


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


class TaskReview(ModalScreen):
    """Opened explicitly by the user; never consumes the parent's approval UI."""
    BINDINGS = [("escape", "back", "Back")]
    DEFAULT_CSS = """
    TaskReview { align: center middle; }
    TaskReview > Vertical { width: 85%; height: 85%; border: solid $primary; background: $surface; padding: 1 2; }
    TaskReview VerticalScroll { height: 1fr; }
    TaskReview Horizontal { height: 3; }
    TaskReview Button { margin-right: 1; }
    """

    def __init__(self, job, submit):
        super().__init__()
        self.job, self.submit = deepcopy(job), submit
        self.pending = False
        self.attempted = False
        detail = json.dumps(self.job.get("interrupts") or {
            "result": self.job.get("result"), "outcome": self.job.get("outcome")
        }, indent=2, ensure_ascii=False)
        # Escaping controls can expand text; decisions require every displayed
        # action to fit after escaping as well.
        escaped = plain(detail, max(len(detail) * 6, _MAX_DETAIL))
        self.truncated = len(escaped) > _MAX_DETAIL
        self.detail = escaped[:_MAX_DETAIL] + "\n[truncated]" if self.truncated else escaped

    def can_decide(self, decision):
        return not self.truncated and allows(self.job, decision)

    def compose(self):
        with Vertical():
            yield Static(Text(plain(f"{self.job['name']} — {self.job['status']}")))
            with VerticalScroll():
                yield Static(Text(plain(self.job.get("description", ""), 6000)))
                yield Static(Text(self.detail))
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
    """A bounded task list updated independently of the main run's stream."""
    DEFAULT_CSS = """
    BackgroundPanel { height: auto; max-height: 12; border-top: solid $primary; }
    BackgroundPanel > Static { height: auto; max-height: 3; }
    BackgroundPanel VerticalScroll { height: auto; max-height: 10; }
    BackgroundPanel Horizontal { height: 3; }
    BackgroundPanel Horizontal Static { width: 1fr; padding: 1 0; }
    BackgroundPanel Button { min-width: 12; }
    """

    def __init__(self):
        super().__init__(id="factory-background-panel")
        self.identity = None
        self.snapshot = None
        self.jobs = []
        self.polling = False
        self.hook_seen = set()
        self.hook_errors = set()
        self.hook_tasks = {}
        self.hook_cleanup = set()
        self.details = {}
        self.revision = 0
        self.closed = False
        self.display = False

    def compose(self):
        yield Static("Background tasks", id="factory-background-summary")
        yield VerticalScroll(id="factory-background-rows")

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
        return not self.closed and self.same_identity(self.current(), identity)

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
        self.display = False

    def schedule_refresh(self):
        if self.closed:
            return
        try:
            identity = self.current()
        except Exception:
            self.display = False
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
            if agent is None or owner is None or graph is None or not hasattr(agent, "_workspace_for_thread"):
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
                self.display = bool(jobs)
                self.revision += 1
                self.details.clear()
                rows = self.query_one("#factory-background-rows", VerticalScroll)
                await rows.remove_children()
                if not self.valid(identity):
                    return
                visible_jobs = sorted(jobs, key=lambda j: (
                    0 if j["status"] in ("needs_approval", "needs_input") else
                    1 if j["status"] == "running" else 2
                ))[:_MAX_ROWS]
                notifications = 0
                for i, job in enumerate(visible_jobs):
                    button_id = f"background-detail-{self.revision}-{i}"
                    self.details[button_id] = (identity, deepcopy(job))
                    await rows.mount(Horizontal(
                        Static(Text(plain(f"{job['name']}: {job['status']}"))),
                        Button("Review" if job["status"] == "needs_approval" else "Details", id=button_id),
                    ))
                    if not self.valid(identity):
                        self.display = False
                        return
                    if notifications < 5 and job["status"] in ("needs_approval", "needs_input", "completed", "failed", "timed_out", "cancelled") and prior.get(job["task_id"]) != job["status"]:
                        notifications += 1
                        self.app.notify(plain(f"Background {job['name']}: {job['status']}"), markup=False)
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
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self.closed and self.display:
                self.query_one("#factory-background-summary", Static).update(Text("Background status unavailable — reconnecting"))
        finally:
            self.polling = False

    def update_summary(self):
        active = sum(j["status"] == "running" for j in self.jobs)
        waiting = sum(j["status"] in ("needs_approval", "needs_input") for j in self.jobs)
        summary = f"Background tasks: {active} running · {waiting} waiting · {len(self.jobs)} total"
        if len(self.jobs) > _MAX_ROWS:
            summary += f" (showing first {_MAX_ROWS})"
        if self.hook_errors:
            summary += "\nHook response unconfirmed; affected tasks remain blocked. Review or cancel them."
        self.query_one("#factory-background-summary", Static).update(Text(summary))

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

    @on(Button.Pressed)
    def review(self, event):
        detail = self.details.get(event.button.id)
        if detail is None:
            return
        event.stop()
        identity, job = detail
        if not self.valid(identity):
            return

        async def submit(**operation):
            return await self.submit(identity, **operation)

        self.app.push_screen(TaskReview(job, submit))


@contextmanager
def client_background_tasks():
    from lc_factory.upstream_cli import app_module
    cls = app_module.DeepAgentsApp
    original = cls.on_mount

    async def mount(self):
        await original(self)
        container = self.query_one("#bottom-app-container")
        if not self.query(BackgroundPanel):
            await container.mount(BackgroundPanel(), before=0)

    cls.on_mount = mount
    try:
        yield
    finally:
        cls.on_mount = original
