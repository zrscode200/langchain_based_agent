"""Enterprise display for OG stable dispatch IDs and eval completion."""
from __future__ import annotations
import time
from typing import Any
from lc_factory.interpreter_dispatch import SUBAGENT_PHASE_COMPLETE
from .upstream_cli import SubagentPanel

class _ReplayAwareSubagentPanel(SubagentPanel):
    """Keep one logical subagent row and clock across LangGraph replays."""

    def on_subagent_event(self, event: dict[str, Any]) -> None:
        """Handle parent-eval completion before upstream's required-id check."""
        if event.get("phase") == SUBAGENT_PHASE_COMPLETE:
            self._finish_phase(event)
            return
        super().on_subagent_event(event)

    def _handle_start(
        self, sub_id: str, eval_key: str, event: dict[str, Any]
    ) -> None:
        """Treat a repeated stable id as the same logical dispatch."""
        phase = self._phases.get(eval_key)
        if phase is None or sub_id not in phase.records:
            super()._handle_start(sub_id, eval_key, event)
            return

        self._active_eval_id = eval_key
        self._show()
        self._apply_body_height()
        if phase.records[sub_id].status == "running":
            self._ensure_timer()

    def _handle_finish(
        self, sub_id: str, eval_key: str, outcome: str, event: dict[str, Any]
    ) -> None:
        """Keep total wall time when a replay reports only its latest attempt."""
        record = self._find_record(sub_id)
        finished_monotonic = time.monotonic()
        super()._handle_finish(sub_id, eval_key, outcome, event)
        if record is None or record.status == "running":
            return
        wall_duration_ms = int(
            max(0.0, finished_monotonic - record.started_monotonic) * 1000
        )
        record.duration_ms = max(record.duration_ms or 0, wall_duration_ms)

    def _finish_phase(self, event: dict[str, Any]) -> None:
        """Cancel rows left running after their parent eval returned."""
        eval_id = event.get("eval_id")
        if not isinstance(eval_id, str):
            return
        phase = self._phases.get(eval_id)
        if phase is None:
            return

        finished_monotonic = time.monotonic()
        changed = False
        for record in phase.records.values():
            if record.status != "running":
                continue
            record.duration_ms = int(
                max(0.0, finished_monotonic - record.started_monotonic) * 1000
            )
            record.status = "cancelled"
            changed = True
        if not changed:
            return
        if not self._any_running():
            self._stop_timer()
        self._refresh()
