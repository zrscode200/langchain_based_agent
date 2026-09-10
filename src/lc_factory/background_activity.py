"""Read-only child activity, entered through the native dynamic subagent panel."""
from __future__ import annotations

import asyncio
import re
from copy import deepcopy

from rich.text import Text
from textual import on
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from lc_factory.background_ui import TaskReview, _ACTIVE, _STATUS_LABELS, plain
from lc_factory.background_transcript import PAGE_CHARS


def activity_text(job):
    """Project only observable records; never display an arbitrary transcript."""
    output = Text()

    def section(title, body):
        if output:
            output.append("\n\n")
        output.append(title + "\n", style="bold")
        output.append(body)

    section("Assignment", plain(job.get("description", ""), 4000) or "No assignment recorded.")
    if not job.get("steerable", False):
        section("Activity availability", "This child does not support live steering or detailed tool activity.")
    pending = job.get("interrupts")
    if job.get("status") == "needs_approval":
        names = []
        for item in pending if isinstance(pending, list) else []:
            payload = item.get("value") if isinstance(item, dict) else None
            actions = payload.get("action_requests") if isinstance(payload, dict) else None
            for action in actions if isinstance(actions, list) else []:
                if isinstance(action, dict) and isinstance(action.get("name"), str):
                    names.append(plain(action["name"], 100))
        section("Waiting for your approval", ", ".join(names[:20]) or "Open Review to inspect the pending request.")
    elif job.get("status") == "needs_input":
        section("Waiting for input", "A hook or another input request is pending. The task remains paused.")

    records = job.get("activity")
    lines = []
    for record in records[-128:] if isinstance(records, list) else []:
        if not isinstance(record, dict):
            continue
        kind = record.get("kind")
        if kind == "tool":
            # Findings and acknowledgment states are already shown directly.
            if record.get("tool_name") == "report_background_task":
                continue
            name = plain(record.get("tool_name", "Tool"), 100)
            state = plain(record.get("status", ""), 100).replace("_", " ")
            lines.append(f"{name} · {state}")
        elif kind in ("report", "finding", "status", "steering"):
            text = plain(record.get("text", ""), 2048)
            if text:
                lines.append(text)
    section("Recent activity", "\n\n".join(lines) or "No activity reported yet.")

    steering = job.get("steering")
    lines = []
    for entry in steering[-32:] if isinstance(steering, list) else []:
        if not isinstance(entry, dict):
            continue
        state = plain(entry.get("status", "queued"), 80).capitalize()
        outcome = entry.get("delivery_outcome")
        if outcome == "undelivered-terminal":
            state = "Not delivered · " + _STATUS_LABELS.get(job.get("status"), "Task ended")
        elif outcome:
            state += " · " + plain(outcome, 160).replace("_", " ")
        lines.append(f"{state}\n{plain(entry.get('message', ''), 2000)}")
    if lines:
        section("Main-agent steering", "\n\n".join(lines))
    if job.get("result") is not None:
        section("Result", plain(job["result"], 64_000))
    return output


def conversation_text(conversation):
    output = Text(plain(conversation.get("text", ""), PAGE_CHARS * 4))
    if not output:
        output.append("Waiting for the child's first conversation messages…")
    for title in ("User / assignment", "Assistant", "Tool result", "Tool call", "System",
                  "Reasoning (provider-exposed)", "Reasoning summary (provider-exposed)"):
        output.highlight_regex(r"(?m)^" + re.escape(title) + r".*$", "bold cyan")
    return output


class TaskActivity(ModalScreen):
    """One live read-only detail window; approval review uses a frozen snapshot."""
    BINDINGS = [("escape", "back", "Back to main agent")]
    DEFAULT_CSS = """
    TaskActivity { align: center middle; }
    TaskActivity > Vertical { width: 94%; max-width: 110; height: 90%; border: round $primary; background: $surface; padding: 1 2; }
    TaskActivity #activity-title { text-style: bold; color: $text; height: auto; }
    TaskActivity #activity-status { color: $text-muted; height: auto; margin-bottom: 1; }
    TaskActivity VerticalScroll { height: 1fr; }
    TaskActivity #activity-body { height: auto; }
    TaskActivity #activity-notice { height: auto; color: $text-muted; margin-top: 1; }
    TaskActivity Horizontal { height: 3; }
    TaskActivity Button { width: 1fr; min-width: 0; margin-right: 1; }
    TaskActivity.-compact Horizontal { layout: grid; grid-size: 2; grid-rows: 3 3; height: 6; }
    """

    def __init__(self, job, inspect, submit, is_current):
        super().__init__()
        self.job = deepcopy(job)
        self.task_id = job["task_id"]
        self.inspect, self.submit, self.connection_current = inspect, submit, is_current
        self.polling = self.closed = self.fresh = self.pending = False
        self.cancel_attempted = False
        self.rendered = None
        self.view_activity = False
        self.page = -1  # Follow the latest page until the user navigates back.

    def compose(self):
        with Vertical():
            yield Static(Text(plain(self.job.get("name", "Subagent"), 300)), id="activity-title")
            yield Static(id="activity-status")
            with VerticalScroll(id="activity-scroll"):
                yield Static(id="activity-body")
            yield Static(Text("Loading activity…"), id="activity-notice")
            with Horizontal(id="conversation-navigation"):
                yield Button("Activity", id="conversation-toggle")
                yield Button("Previous", id="conversation-previous")
                yield Button("Next", id="conversation-next")
                yield Button("Latest", id="conversation-latest")
            with Horizontal():
                yield Button("Review", id="activity-review", disabled=True)
                yield Button("Cancel task", id="activity-cancel", disabled=True)
                yield Button("Back", id="activity-back")

    def current(self):
        try:
            return not self.closed and self.connection_current()
        except Exception:
            return False

    def render_job(self):
        conversation = self.job.get("conversation")
        available = isinstance(conversation, dict)
        self.query_one("#conversation-navigation").display = available
        show_conversation = available and not self.view_activity
        body = conversation_text(conversation) if show_conversation else activity_text(self.job)
        if body != self.rendered:
            self.query_one("#activity-body", Static).update(body)
            self.rendered = body
        label = _STATUS_LABELS.get(self.job.get("status"), "Unknown status")
        if show_conversation:
            label += f" · Conversation · Page {conversation['page'] + 1}/{max(1, conversation['pages'])}"
            if self.page == -1:
                label += " · Following latest"
        self.query_one("#conversation-toggle", Button).label = "Conversation" if self.view_activity else "Activity"
        self.query_one("#conversation-toggle", Button).disabled = not self.current()
        self.query_one("#conversation-previous", Button).disabled = not (show_conversation and conversation["page"] > 0)
        self.query_one("#conversation-next", Button).disabled = not (show_conversation and conversation["page"] + 1 < conversation["pages"])
        self.query_one("#conversation-latest", Button).disabled = not show_conversation or self.page == -1
        self.query_one("#activity-status", Static).update(Text(label))
        self.update_controls()

    def update_controls(self):
        ready = self.fresh and self.current() and not self.pending
        self.query_one("#activity-review", Button).disabled = not (ready and self.job.get("status") == "needs_approval")
        self.query_one("#activity-cancel", Button).disabled = not (ready and not self.cancel_attempted and self.job.get("status") in _ACTIVE)

    def on_mount(self):
        self.render_job()
        self.timer = self.set_interval(1.0, self.schedule_refresh)
        self.schedule_refresh()

    def on_resize(self, event):
        self.set_class(event.size.width < 60, "-compact")

    def on_unmount(self):
        self.closed = True
        self.timer.stop()
        self.workers.cancel_node(self)

    def schedule_refresh(self):
        if self.closed or self.polling or self.pending or self.app.screen is not self:
            return
        self.run_worker(self.refresh_task(), group="child-activity", exclusive=True, exit_on_error=False)

    async def refresh_task(self):
        if self.closed or self.polling:
            return
        self.polling = True
        try:
            if not self.current():
                self.invalidate()
                return
            value = await self.inspect() if self.page == -1 else await self.inspect(transcript_page=self.page)
            if not self.current():
                self.invalidate()
                return
            if not isinstance(value, dict) or value.get("task_id") != self.task_id:
                raise ValueError("Unknown child")
            self.job = deepcopy(value)
            self.fresh = True
            self.render_job()
            conversation = self.job.get("conversation")
            notice = conversation.get("notice", "") if isinstance(conversation, dict) and not self.view_activity else "Read-only activity"
            self.query_one("#activity-notice", Static).update(Text(plain(notice, 500) + " · Esc returns to the main agent"))
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self.closed:
                if not self.current():
                    self.invalidate()
                else:
                    self.fresh = False
                    self.update_controls()
                    self.query_one("#activity-notice", Static).update(Text(
                        "Activity unavailable. Shown details may be outdated; controls are disabled."))
        finally:
            self.polling = False

    def invalidate(self):
        self.fresh = False
        self.query_one("#activity-body", Static).update(Text("Conversation changed. Reopen a task from the subagent panel."))
        self.query_one("#activity-title", Static).update(Text("Subagent activity"))
        self.query_one("#activity-status", Static).update(Text("Unavailable"))
        self.query_one("#activity-notice", Static).update(Text("Esc returns to the main agent"))
        for button in self.query("#conversation-navigation Button"):
            button.disabled = True
        self.update_controls()
        self.timer.stop()

    def action_back(self):
        # Inspection is read-only and can be abandoned even during an await.
        self.dismiss()

    @on(Button.Pressed)
    async def handle_button(self, event):
        event.stop()
        if event.button.id == "activity-back":
            self.action_back()
            return
        if event.button.id == "conversation-toggle":
            if not self.current():
                self.invalidate()
                return
            self.view_activity = not self.view_activity
            self.render_job()
            return
        if event.button.id in ("conversation-previous", "conversation-next", "conversation-latest"):
            conversation = self.job.get("conversation", {})
            if not self.fresh or not self.current() or self.polling or event.button.disabled:
                return
            self.page = (-1 if event.button.id == "conversation-latest" else
                         conversation["page"] + (-1 if event.button.id == "conversation-previous" else 1))
            await self.refresh_task()
            self.query_one("#activity-scroll").scroll_home(animate=False)
            return
        if not self.fresh or not self.current() or self.pending:
            self.update_controls()
            return
        if event.button.id == "activity-review" and self.job.get("status") == "needs_approval":
            # Further detail polling cannot replace the actions being approved.
            self.app.push_screen(TaskReview(deepcopy(self.job), self.submit))
        elif event.button.id == "activity-cancel" and not self.cancel_attempted and self.job.get("status") in _ACTIVE:
            self.cancel_attempted = self.pending = True
            self.update_controls()
            try:
                await self.submit(operation="cancel", task_id=self.task_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                if not self.closed:
                    self.query_one("#activity-notice", Static).update(Text(
                        "Cancellation was not confirmed. Reopen the task to check its state."))
            finally:
                self.pending = False
                if not self.closed:
                    self.fresh = False
                    self.update_controls()
                    self.schedule_refresh()
