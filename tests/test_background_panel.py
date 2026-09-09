"""Exercise the real native panel and replay subclass without a server/model."""
from copy import deepcopy

import pytest
from textual.app import App
from textual.widgets import Input, Static

from lc_factory.background_panel import background_subagent_panel_class
from lc_factory.upstream_cli import subagent_panel_module as native


def job(task_id="background-1", status="running", name="Research task"):
    return {"task_id": task_id, "status": status, "name": name, "result": None}


def start(task_id="foreground-1", eval_id="eval-1"):
    return {"type": "subagent", "phase": "start", "id": task_id,
            "eval_id": eval_id, "subagent_type": "general", "description": "Read source"}


class Harness(App):
    BINDINGS = [("ctrl+t", "toggle_panel", "Subagents")]

    def __init__(self, base_class=native.SubagentPanel):
        super().__init__()
        self.panel_class = background_subagent_panel_class(base_class)
        self.prompts = []

    def get_theme_variable_defaults(self):
        from deepagents_code.theme import get_css_variable_defaults
        return get_css_variable_defaults()

    def compose(self):
        yield self.panel_class(id="panel")
        yield Input(id="main-input")

    def action_toggle_panel(self):
        self.query_one("#panel").toggle()

    def on_input_submitted(self, event):
        self.prompts.append(event.value)
        event.input.value = ""


def rendered(panel, widget_id):
    return panel._last_render[widget_id]


@pytest.mark.parametrize("width", [40, 80, 120])
async def test_mixed_native_phases_and_literal_background_rows(width):
    app = Harness()
    async with app.run_test(size=(width, 30)) as pilot:
        panel = app.query_one("#panel")
        assert isinstance(panel, native.SubagentPanel)
        panel.prepare_turn(model_label="foreground-model")
        panel.on_subagent_event(start())
        panel.set_background_jobs([
            job(name="[bold]literal[/bold]\n\x1b[31m\u202ehidden"),
            job("approval", "needs_approval"), job("input", "needs_input"),
            job("done", "completed"), job("failed", "failed"), job("cancelled", "cancelled"),
        ], lambda task_id: None)
        await pilot.pause()
        assert len(app.query(native.SubagentPanel)) == 1
        assert len(panel._phases) == 2
        assert panel._phases["eval-1"].index == 1
        assert "MODEL" in rendered(panel, "subagent-agents")
        assert "foreground-model" in rendered(panel, "subagent-agents")
        assert panel._turn_counts() == (3, 7, 1, 1)
        panel.focus()
        await pilot.press("down")
        await pilot.pause()
        phase = panel._displayed_phase()
        assert phase is panel._background_phase
        assert phase.counts() == (3, 6)
        assert phase.any_running() and not phase.all_terminal()
        rows = rendered(panel, "subagent-agents")
        assert "MODEL" not in rows and "TIME" not in rows and "foreground-model" not in rows
        assert "approval" in rows and "input" in rows and "cancelled" in rows
        assert "\x1b" not in rows and "\u202e" not in rows
        assert len(rows.splitlines()) == 7
        assert panel.query_one("#subagent-phases").virtual_size.height == 3
        assert panel.query_one("#subagent-agents").virtual_size.height == 7
        if width >= 80:
            assert "[bold]literal[/bold]" in rows
        assert "Enter/click review" in rendered(panel, "subagent-background-help")
        assert panel.size.width <= width
        assert panel.query_one("#subagent-body").size.height <= 12


async def test_jobs_survive_turn_prepare_and_finalize_but_reset_invalidates():
    async with Harness().run_test(size=(100, 30)) as pilot:
        panel = pilot.app.query_one("#panel")
        calls, resets = [], []
        panel.set_background_jobs([job(), job("waiting", "needs_approval")], calls.append)
        phase = panel._background_phase
        records = dict(phase.records)
        panel.set_background_notice("Response unconfirmed")
        panel.on_background_reset = lambda: resets.append(panel._background_phase)
        panel.prepare_turn(model_label="new-model")
        assert panel._background_phase is phase and phase.records == records
        assert panel._background_notice == "Response unconfirmed"
        panel.on_subagent_event(start())
        foreground = panel._phases["eval-1"].records["foreground-1"]
        panel.finalize_running()
        assert foreground.status == "cancelled"
        assert phase.records == records and phase.records["background-1"].status == "running"
        assert panel._turn_counts() == (1, 3, 0, 1)
        panel.prepare_turn()
        assert list(panel._phases.values()) == [phase]
        panel.reset(model_label="reset-model")
        assert resets == [None]
        assert not panel._phases and panel._background_phase is None
        assert not panel.has_class("-visible") and not panel._background_notice
        panel._review_background("background-1")
        assert calls == []
        panel.clear_background_jobs()
        assert resets == [None]  # clear must never recurse into the controller callback


async def test_updates_preserve_collapse_phase_task_and_unchanged_render(monkeypatch):
    async with Harness().run_test(size=(100, 30)) as pilot:
        panel = pilot.app.query_one("#panel")
        panel.on_subagent_event(start())
        jobs = [job("a", "needs_input"), job("b", "needs_approval")]
        panel.set_background_jobs(jobs, lambda task_id: None)
        panel.focus()
        await pilot.press("down", "right")
        selected = panel._selected_eval_id
        record = panel._background_phase.records["a"]
        agents = panel.query_one("#subagent-agents", Static)
        updates = []
        original_update = agents.update
        monkeypatch.setattr(agents, "update", lambda content: (updates.append(content), original_update(content))[-1])
        fresh_calls = []
        panel.set_background_jobs(deepcopy(jobs), fresh_calls.append)
        assert updates == []
        assert panel._background_phase.records["a"] is record
        assert panel._background_selected_id == "b"
        await pilot.press("enter")
        assert fresh_calls == ["b"]  # unchanged rows still take the new owner callback
        await pilot.press("ctrl+t")
        assert not panel.expanded
        panel.set_background_jobs([jobs[1], job("a", "completed")], fresh_calls.append)
        assert not panel.expanded and panel._selected_eval_id is selected
        assert panel._background_selected_id == "b"
        panel.prepare_turn()
        assert not panel.expanded and panel._selected_eval_id is selected
        await pilot.press("ctrl+t")
        assert panel.expanded and panel._background_selected_id == "b"


async def test_click_and_panel_keys_review_without_consuming_main_input():
    calls = []
    app = Harness()
    async with app.run_test(size=(80, 30)) as pilot:
        panel = app.query_one("#panel")
        panel.set_background_jobs([job("a", "needs_approval"), job("b", "completed")], calls.append)
        await pilot.pause()
        await pilot.click("#main-input")
        await pilot.press("right", "h", "i", "enter")
        assert app.prompts == ["hi"] and calls == []
        panel.focus()
        await pilot.press("right", "enter")
        assert calls == ["b"]
        await pilot.click("#subagent-agents", offset=(3, 1))
        assert calls == ["b", "a"]
        await pilot.click("#subagent-header-summary")
        assert not panel.expanded
        await pilot.press("enter")
        assert calls == ["b", "a"]


@pytest.mark.parametrize("width", [40, 100])
async def test_native_phase_navigation_moves_once_with_three_phases(width):
    async with Harness().run_test(size=(width, 30)) as pilot:
        panel = pilot.app.query_one("#panel")
        panel.on_subagent_event(start("first", "eval-1"))
        panel.on_subagent_event(start("second", "eval-2"))
        panel.set_background_jobs([job()], lambda task_id: None)
        panel.focus()
        await pilot.press("up")
        assert panel._displayed_phase() is panel._phases["eval-1"]
        await pilot.press("down")
        assert panel._displayed_phase() is panel._phases["eval-2"]
        await pilot.press("down")
        assert panel._displayed_phase() is panel._background_phase
        await pilot.click("#subagent-phases", offset=(3, 1))
        assert panel._displayed_phase() is panel._phases["eval-1"]


async def test_bounded_rows_scroll_selection_and_immediate_clear():
    calls = []
    async with Harness().run_test(size=(80, 25)) as pilot:
        panel = pilot.app.query_one("#panel")
        panel.set_background_jobs([job(str(i), "needs_input") for i in range(200)], calls.append)
        await pilot.pause()
        assert len(panel._background_phase.records) == 128
        assert "first 128 tasks" in rendered(panel, "subagent-background-help")
        panel.focus()
        await pilot.press(*(["right"] * 18), "enter")
        await pilot.pause()
        assert calls == ["18"]
        scroll = panel.query_one("#subagent-agents-scroll")
        assert scroll.scroll_y > 0
        assert scroll.scroll_y <= 19 < scroll.scroll_y + scroll.size.height
        before = scroll.scroll_y
        panel.set_background_jobs([job(str(i), "needs_input") for i in range(200)], calls.append)
        assert scroll.scroll_y == before
        panel.clear_background_jobs()
        assert not panel.has_class("-visible")
        assert not panel._phases and not rendered(panel, "subagent-agents")
        panel._review_background("18")
        assert calls == ["18"]


async def test_notice_can_show_without_jobs_and_is_literal_and_cleared():
    async with Harness().run_test(size=(40, 20)) as pilot:
        panel = pilot.app.query_one("#panel")
        panel.set_background_notice("[bold]Status unavailable\n\x1b[31m\u202e check connection")
        await pilot.pause()
        assert panel.has_class("-visible")
        notice = rendered(panel, "subagent-background-notice")
        assert notice.startswith("[bold]Status unavailable")
        assert "\x1b" not in notice and "\u202e" not in notice and "\n" not in notice
        panel.set_background_notice("")
        assert not panel.has_class("-visible")


async def test_enterprise_replay_and_same_id_foreground_background_isolation():
    from lc_factory_enterprise.replay import _ReplayAwareSubagentPanel
    async with Harness(_ReplayAwareSubagentPanel).run_test(size=(100, 30)) as pilot:
        panel = pilot.app.query_one("#panel")
        assert isinstance(panel, _ReplayAwareSubagentPanel)
        panel.set_background_jobs([job("shared", "needs_approval")], lambda task_id: None)
        background = panel._background_phase.records["shared"]
        event = start("shared")
        panel.on_subagent_event(event)
        record = panel._phases["eval-1"].records["shared"]
        record.started_monotonic -= 2
        panel.on_subagent_event(event)
        assert panel._phases["eval-1"].records["shared"] is record
        panel.on_subagent_event({**event, "phase": "complete", "duration_ms": 1})
        assert record.status == "done" and record.duration_ms >= 2000
        panel.on_subagent_event(start("pending"))
        panel.on_subagent_event(start("other", "eval-2"))
        panel.on_subagent_event({"phase": "phase_complete", "eval_id": "eval-1"})
        assert panel._phases["eval-1"].records["pending"].status == "cancelled"
        assert panel._phases["eval-2"].records["other"].status == "running"
        assert panel._background_phase.records["shared"] is background
        assert background.status == "needs_approval"
        assert [panel._phases[key].index for key in panel._phase_order[:-1]] == [1, 2]
        panel.finalize_running()
        assert panel._phases["eval-2"].records["other"].status == "cancelled"
        assert background.status == "needs_approval"


async def test_invalid_status_never_looks_finished_and_removed_tasks_cannot_review():
    calls = []
    async with Harness().run_test() as pilot:
        panel = pilot.app.query_one("#panel")
        panel.set_background_jobs([job("old", "mystery"), {"task_id": []}, None], calls.append)
        assert panel._turn_counts() == (0, 1, 0, 0)
        assert panel._background_phase.any_running()
        assert "unknown" in rendered(panel, "subagent-agents")
        panel.set_background_jobs([job("new", "completed")], calls.append)
        panel._review_background("old")
        assert calls == []
        assert panel._background_selected_id == "new"


async def test_timeout_is_terminal_failure_and_tasks_have_descriptions():
    async with Harness().run_test(size=(120, 30)) as pilot:
        panel = pilot.app.query_one("#panel")
        panel.set_background_jobs([{**job("timeout", "timed_out"), "description": "Prepare the report"}], lambda _: None)
        assert panel._turn_counts() == (1, 1, 1, 0)
        assert not panel._background_phase.any_running()
        assert "timed out" in rendered(panel, "subagent-agents")
        assert "Prepare the report" in rendered(panel, "subagent-agents")


async def test_waiting_attention_remains_visible_when_collapsed():
    async with Harness().run_test(size=(40, 24)) as pilot:
        panel = pilot.app.query_one("#panel")
        panel.set_background_jobs([job("waiting", "needs_approval")], lambda _: None)
        assert "Background !" in rendered(panel, "subagent-phases")
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert not panel.expanded
        notice = panel.query_one("#subagent-background-notice")
        assert notice.display and "waiting" in rendered(panel, "subagent-background-notice")
        panel.set_background_jobs([job("waiting", "completed")], lambda _: None)
        assert not notice.display
