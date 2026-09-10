"""Background jobs projected into the installed native dynamic subagent panel.

This is a client-only adapter over the pinned upstream widget. Background records
have their own status/count/rendering rules: the API supplies neither a model nor
elapsed time, and a paused child is still unfinished. The supplied base class may
already provide enterprise replay handling, which remains in its method chain.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
from typing import Callable

from textual.content import Content
from textual.css.query import NoMatches, TooManyMatches
from textual.widgets import Static

from lc_factory.upstream_cli import subagent_panel_module as native

_MAX_JOBS = 128
_TERMINAL = {"completed", "failed", "timed_out", "cancelled"}
_WAITING = {"needs_approval", "needs_input"}
# A non-string key cannot collide with a foreground eval id from the stream.
_BACKGROUND = object()
_STATUS_LABELS = {
    "queued": "queued", "running": "running", "needs_approval": "approval", "needs_input": "input",
    "completed": "done", "failed": "failed", "timed_out": "timed out", "cancelled": "cancelled",
}


@dataclass(frozen=True)
class _BackgroundRecord:
    id: str
    label: str
    status: str


class _BackgroundPhase(native._Phase):
    def counts(self):
        return sum(r.status in _TERMINAL for r in self.records.values()), len(self.records)

    def any_running(self):
        # Native header completion uses this as its unfinished predicate.
        return any(r.status not in _TERMINAL for r in self.records.values())

    def any_error(self):
        return any(r.status in {"failed", "timed_out"} for r in self.records.values())


def background_subagent_panel_class(base_class):
    """Extend the CURRENT app SubagentPanel, preserving its existing overrides."""
    if getattr(base_class, "_factory_background_panel", False):
        return base_class
    class BackgroundSubagentPanel(base_class):
        _factory_background_panel = True
        DEFAULT_CSS = """
        BackgroundSubagentPanel #subagent-phases,
        BackgroundSubagentPanel #subagent-agents {
            text-wrap: nowrap; text-overflow: ellipsis;
        }
        BackgroundSubagentPanel #subagent-background-help {
            height: auto; color: $text-muted; display: none;
        }
        BackgroundSubagentPanel #subagent-background-notice {
            height: auto; max-height: 2; color: $warning; display: none;
        }
        BackgroundSubagentPanel.-background-narrow {
            padding: 1 1;
        }
        BackgroundSubagentPanel.-background-narrow.-collapsed {
            padding: 0 1;
        }
        BackgroundSubagentPanel.-background-narrow #subagent-phases-scroll {
            width: 16; padding-right: 1; margin-right: 1;
        }
        BackgroundSubagentPanel.-background-narrow #subagent-header-hint {
            margin-left: 1;
        }
        """

        def __init__(self, **kwargs):
            self._background_phase = None
            self._background_selected_id = None
            self._background_review = None
            self._background_notice = ""
            self._background_truncated = False
            self.on_background_reset: Callable[[], None] | None = None
            super().__init__(**kwargs)

        def compose(self):
            yield from super().compose()
            yield Static("", id="subagent-background-help", markup=False)
            yield Static("", id="subagent-background-notice", markup=False)

        def set_background_jobs(self, jobs, on_review):
            """Synchronously update bounded literal rows without changing navigation."""
            self._background_review = on_review
            records = {}
            candidates = list(islice(jobs, _MAX_JOBS + 1))
            truncated = len(candidates) > _MAX_JOBS
            for job in candidates[:_MAX_JOBS]:
                if not isinstance(job, dict):
                    continue
                task_id = job.get("task_id")
                if not isinstance(task_id, str) or not task_id:
                    continue
                status = job.get("status")
                status = status if isinstance(status, str) and status in _STATUS_LABELS else "unknown"
                label = native._sanitize(native.SubagentPanel._row_label({
                    "subagent_type": str(job.get("name") or "Background task"),
                    "description": str(job.get("description") or ""),
                }).rstrip(": "), max_chars=200)
                records[task_id] = _BackgroundRecord(task_id, label, status)
            phase = self._background_phase
            if phase is not None and phase.records == records and list(records) == phase.order and truncated == self._background_truncated:
                return
            if not records:
                self._remove_background_phase()
            else:
                if phase is None:
                    phase = self._background_phase = _BackgroundPhase(eval_id=_BACKGROUND, index=0)
                # Reuse unchanged records; there are no per-task widgets to remount.
                phase.records = {key: phase.records.get(key) if phase.records.get(key) == row else row
                                 for key, row in records.items()}
                phase.order = list(records)
                if self._background_selected_id not in records:
                    self._background_selected_id = phase.order[0]
                self._attach_background_phase()
            self._background_truncated = truncated
            self._sync_background_display()

        def set_background_notice(self, message: str):
            """Show a compact polling/hook notice inside this same panel."""
            notice = native._sanitize(message, max_chars=240)
            if notice != self._background_notice:
                self._background_notice = notice
                self._sync_background_display()

        def clear_background_jobs(self):
            """Immediately discard all projected jobs, callback, and notices."""
            self._remove_background_phase()
            self._background_review = None
            self._background_notice = ""
            self._background_truncated = False
            self._sync_background_display()

        def _remove_background_phase(self):
            self._phases.pop(_BACKGROUND, None)
            if _BACKGROUND in self._phase_order:
                self._phase_order.remove(_BACKGROUND)
            if self._selected_eval_id is _BACKGROUND:
                self._selected_eval_id = None
            self._background_phase = None
            self._background_selected_id = None

        def _attach_background_phase(self):
            if self._background_phase is not None:
                self._phases[_BACKGROUND] = self._background_phase
                if _BACKGROUND not in self._phase_order:
                    self._phase_order.append(_BACKGROUND)

        def _sync_background_display(self):
            if self._phases or self._background_notice:
                self._show()
                self._apply_body_height()
            else:
                self.remove_class("-visible")
            if any(r.status == "running" for p in self._phases.values() for r in p.records.values()):
                self._ensure_timer()
            else:
                self._stop_timer()
            self._refresh()

        def prepare_turn(self, *, model_label=None):
            selected_background = self._selected_eval_id is _BACKGROUND
            super().prepare_turn(model_label=model_label)
            self._attach_background_phase()
            if selected_background and self._background_phase is not None:
                self._selected_eval_id = _BACKGROUND
            self._sync_background_display()

        def reset(self, *, model_label=None, **kwargs):
            self.clear_background_jobs()
            super().reset(model_label=model_label, **kwargs)
            if self.on_background_reset is not None:
                self.on_background_reset()

        def finalize_running(self):
            # Let the installed base finalize foreground records normally. Its
            # generic running->cancelled loop must never touch a background job.
            phases, order, selected = self._phases, self._phase_order, self._selected_eval_id
            self._phases = {key: phase for key, phase in phases.items() if key is not _BACKGROUND}
            self._phase_order = [key for key in order if key is not _BACKGROUND]
            if selected is _BACKGROUND:
                self._selected_eval_id = None
            try:
                super().finalize_running()
            finally:
                self._phases, self._phase_order, self._selected_eval_id = phases, order, selected
                self._sync_background_display()

        def _find_record(self, sub_id):
            # Dispatch and background task ids are separate namespaces.
            phases = self._phases
            self._phases = {key: phase for key, phase in phases.items() if key is not _BACKGROUND}
            try:
                return super()._find_record(sub_id)
            finally:
                self._phases = phases

        def _ensure_phase(self, eval_key):
            phase = super()._ensure_phase(eval_key)
            if _BACKGROUND in self._phase_order:
                self._phase_order.remove(_BACKGROUND)
                self._phase_order.append(_BACKGROUND)
            phase.index = self._phase_order.index(eval_key) + 1
            return phase

        def _displayed_phase(self):
            return super()._displayed_phase() or self._background_phase

        def _turn_counts(self):
            done = total = failed = cancelled = 0
            for phase in self._phases.values():
                finished, count = phase.counts()
                done += finished
                total += count
                failed += sum(r.status in {"error", "failed", "timed_out"} for r in phase.records.values())
                cancelled += sum(r.status == "cancelled" for r in phase.records.values())
            return done, total, failed, cancelled

        def _header_meta_parts(self, done, total, failed, cancelled, colors):
            parts = super()._header_meta_parts(done, total, failed, cancelled, colors)
            if self._background_phase is not None:
                waiting = sum(r.status in _WAITING for r in self._background_phase.records.values())
                if waiting:
                    parts.append(Content.styled(f"  ·  {waiting} waiting", colors.warning))
            return parts

        def _phase_row(self, phase, *, selected, colors):
            if phase is not self._background_phase:
                return super()._phase_row(phase, selected=selected, colors=colors)
            caret = native.get_glyphs().cursor if selected else " "
            done, total = phase.counts()
            text = f"{caret} Background"
            waiting = any(r.status in _WAITING for r in phase.records.values())
            if waiting:
                text += " !"
            if not self.size.width or self.size.width >= 70:
                text += f" {done}/{total}"
            return Content.styled(text, colors.warning if waiting else colors.primary if selected else colors.muted)

        def _refresh_header(self):
            super()._refresh_header()
            if self.size.width and self.size.width < 70:
                self._update_cached("subagent-header-hint", Content.styled("Ctrl+T", native.get_theme_colors(self).muted))

        def _refresh(self):
            self.set_class(bool(self.size.width and self.size.width < 70), "-background-narrow")
            super()._refresh()
            try:
                help_widget = self.query_one("#subagent-background-help", Static)
                notice_widget = self.query_one("#subagent-background-notice", Static)
            except (NoMatches, TooManyMatches):
                return
            background_visible = self._background_phase is not None and self._displayed_phase() is self._background_phase
            help_widget.display = background_visible and self.expanded
            help_text = "↑↓ phase · ←→ task · Enter/click conversation"
            if self._background_truncated:
                help_text += f" · first {_MAX_JOBS} tasks"
            self._update_cached("subagent-background-help", Content(help_text))
            notice = self._background_notice
            if not notice and not self.expanded and self._background_phase is not None:
                waiting = sum(r.status in _WAITING for r in self._background_phase.records.values())
                if waiting:
                    notice = f"{waiting} background task{'s' if waiting != 1 else ''} waiting for approval or input"
            notice_widget.display = bool(notice)
            self._update_cached("subagent-background-notice", Content(notice))

        def _refresh_agents(self):
            phase = self._displayed_phase()
            if phase is not self._background_phase or phase is None:
                return super()._refresh_agents()
            colors, glyphs = native.get_theme_colors(self), native.get_glyphs()
            try:
                width = self.query_one("#subagent-agents", Static).size.width
            except (NoMatches, TooManyMatches):
                width = 0
            width = max(1, (width or 48) - 2)
            rows = [Content.styled("   STATUS     TASK", colors.muted).truncate(width)]
            for task_id in phase.order:
                record = phase.records[task_id]
                status = record.status
                if status == "completed":
                    icon, tint = glyphs.checkmark, colors.success
                elif status in {"failed", "timed_out"}:
                    icon, tint = glyphs.error, colors.error
                elif status == "cancelled":
                    icon, tint = glyphs.circle_empty, colors.muted
                elif status == "running":
                    icon, tint = self._spinner.current_frame(), colors.warning
                elif status == "queued":
                    icon, tint = glyphs.circle_empty, colors.muted
                else:
                    icon, tint = "?", colors.warning
                selected = task_id == self._background_selected_id
                caret = glyphs.cursor if selected else " "
                prefix = Content.styled(f"{caret}{icon} {_STATUS_LABELS.get(status, 'unknown'):<10} ", tint)
                label = Content.styled(record.label, colors.primary if selected else colors.foreground)
                rows.append(Content.assemble(prefix, label).truncate(width, ellipsis=True))
            self._update_cached("subagent-agents", Content("\n").join(rows))

        def _review_background(self, task_id):
            phase = self._background_phase
            if phase is not None and task_id in phase.records and self._background_review is not None:
                self._background_selected_id = task_id
                self._refresh_agents()
                self._background_review(task_id)

        def on_click(self, event):
            # Textual otherwise dispatches the base handler again after ours.
            # Forward explicitly once so the installed base keeps its behavior.
            event.prevent_default()
            if self.expanded and self._background_phase is not None and self._displayed_phase() is self._background_phase:
                try:
                    agents = self.query_one("#subagent-agents", Static)
                    offset = event.get_content_offset(agents)
                except (NoMatches, TooManyMatches):
                    offset = None
                if offset is not None and 0 <= offset.y - 1 < len(self._background_phase.order):
                    event.stop()
                    self._review_background(self._background_phase.order[offset.y - 1])
                    return
            super().on_click(event)

        def on_key(self, event):
            event.prevent_default()
            phase = self._background_phase
            if self.has_focus and self.expanded and phase is not None and self._displayed_phase() is phase:
                if event.key == "enter":
                    event.stop()
                    event.prevent_default()
                    self._review_background(self._background_selected_id)
                    return
                if event.key in {"left", "right"}:
                    delta = 1 if event.key == "right" else -1
                    index = phase.order.index(self._background_selected_id)
                    index = max(0, min(len(phase.order) - 1, index + delta))
                    self._background_selected_id = phase.order[index]
                    self._refresh_agents()
                    scroll = self.query_one("#subagent-agents-scroll")
                    # Keep the selected logical row visible in the native scroll pane.
                    row = index + 1
                    if row < scroll.scroll_y:
                        scroll.scroll_to(y=row, animate=False)
                    elif row >= scroll.scroll_y + scroll.size.height:
                        scroll.scroll_to(y=row - scroll.size.height + 1, animate=False)
                    event.stop()
                    event.prevent_default()
                    return
            super().on_key(event)

    return BackgroundSubagentPanel
