"""Exercise native live rows, grouping and virtualization with no server."""
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from textual.app import App
from textual.containers import Vertical

from lc_factory.skill_activity import SKILL_ACTIVITY
from lc_factory.skill_activity_ui import SkillActivityAgent, client_skill_activity
from lc_factory.upstream_cli import skill_activity_ui_modules

ROW = {"name": "review [bold]", "path": "/skills/review/SKILL.md",
       "description": "Review changes", "source": "", "origin": "agent", "status": "loaded"}
CALL = {"id": "read", "name": "read_file", "args": {"file_path": ROW["path"]}}


def meta(status="loaded"):
    return {SKILL_ACTIVITY: {"read": {**ROW, "status": status}}}


def history():
    return [HumanMessage("Review"), AIMessage("", tool_calls=[CALL, {**CALL, "id": "ordinary"}], additional_kwargs=meta("loading")),
            ToolMessage("1  Ordinary", tool_call_id="ordinary"),
            ToolMessage("1  Instructions", tool_call_id="read", additional_kwargs=meta())]


def test_history_matches_exact_ids_and_survives_widget_round_trip():
    app, _, widgets, store = skill_activity_ui_modules()
    original = app.DeepAgentsApp._convert_messages_to_data
    source = history()
    with client_skill_activity():
        rows = app.DeepAgentsApp._convert_messages_to_data(source)
        skill = next(r for r in rows if getattr(r, "_lc_skill_activity", None))
        assert skill.type == store.MessageType.TOOL
        assert skill._lc_skill_activity["status"] == "loaded"
        groups = [r for r in rows if r.type == store.MessageType.TOOL_GROUP]
        assert len(groups) == 1 and len(groups[0].tool_group_messages) == 1
        assert not getattr(groups[0].tool_group_messages[0], "_lc_skill_activity", None)
        widget = store.MessageData.from_widget(skill.to_widget()).to_widget()
        assert widget._lc_skill_activity == ROW
        assert widget.tool_name == "read_file"
        assert widget._args == CALL["args"]
    assert app.DeepAgentsApp._convert_messages_to_data is original
    assert all("_lc_skill_activity" not in vars(m) for m in source)


@pytest.mark.parametrize("width", [40, 80, 120])
async def test_real_widget_header_literal_and_visible_outside_generic_group(width):
    _, _, widgets, store = skill_activity_ui_modules()
    class Harness(App):
        def get_theme_variable_defaults(self):
            from deepagents_code.theme import get_css_variable_defaults
            return get_css_variable_defaults()

        def compose(self):
            yield Vertical(id="messages")
    with client_skill_activity():
        async with Harness().run_test(size=(width, 30)) as pilot:
            host = pilot.app.query_one("#messages")
            skill = widgets.ToolCallMessage("read_file", CALL["args"])
            skill._lc_skill_activity = ROW
            ordinary = widgets.ToolCallMessage("read_file", {"file_path": "/readme"})
            await host.mount(skill, ordinary)
            skill.set_success("1  Instructions")
            ordinary.set_success("1  Ordinary")
            group = widgets.ToolGroupSummary([skill, ordinary], [skill, ordinary])
            await host.mount(group, before=skill)
            await pilot.pause()
            assert "Loaded skill: review [bold]" in str(skill._header_widget.content)
            assert skill.display and not skill.has_class("-grouped")
            assert not ordinary.display
            assert group._tools == [ordinary]
            # Native live mount starts with an empty group, then adds its first
            # tool. The adapter must not remove that newborn summary.
            newborn = widgets.ToolGroupSummary(live=True)
            await host.mount(newborn)
            await pilot.pause()
            assert newborn.is_attached
            later = widgets.ToolCallMessage("read_file", {"file_path": "/another"})
            await host.mount(later)
            newborn.add_member(later)
            await pilot.pause()
            assert newborn.is_attached and not later.display
            assert later.has_class("-grouped")
            hydrated = store.MessageData.from_widget(skill).to_widget()
            await host.mount(hydrated)
            await pilot.pause()
            assert "Loaded skill: review [bold]" in str(hydrated._header_widget.content)


async def test_live_updates_one_row_ignore_child_collisions_and_replayed_proposals():
    _, _, widgets, _ = skill_activity_ui_modules()
    row = widgets.ToolCallMessage("read_file", CALL["args"])
    current, synced = {}, []
    adapter = SimpleNamespace(_current_tool_messages=current, _sync_tool_widget=lambda w: synced.append(w))
    class Agent:
        sentinel = 42
        async def astream(self):
            yield ((), "messages", (AIMessage("", tool_calls=[CALL]), {}))
            yield ((), "updates", {"SkillActivityMiddleware.after_model": {"messages": [AIMessage("", additional_kwargs=meta("loading"))]}})
            yield (("child:id",), "updates", {"tools": {"messages": [ToolMessage("bad", tool_call_id="read", additional_kwargs=meta("failed"))]}})
            yield ((), "messages", (ToolMessage("1  Instructions", tool_call_id="read", additional_kwargs=meta()), {}))
            yield ((), "updates", {"replay": {"messages": [AIMessage("", additional_kwargs=meta("loading"))]}})
    with client_skill_activity():
        proxy = SkillActivityAgent(Agent(), adapter, widgets.ToolGroupSummary)
        assert proxy.sentinel == 42
        async for namespace, mode, data in proxy.astream():
            if not namespace and mode == "messages" and isinstance(data[0], AIMessage):
                current["read"] = row
            if not namespace and mode == "messages" and isinstance(data[0], ToolMessage):
                current.pop("read")
        assert row._lc_skill_activity["status"] == "loaded"
        assert synced and all(w is row for w in synced)


def test_native_history_denial_and_reused_call_id_do_not_claim_another_skill():
    app, _, _, store = skill_activity_ui_modules()
    first = AIMessage("", tool_calls=[CALL], additional_kwargs=meta("loading"))
    second_meta = {SKILL_ACTIVITY: {"read": {**ROW, "name": "second", "status": "loaded"}}}
    source = [first, ToolMessage("Denied", tool_call_id="read", status="error"),
              HumanMessage("Another turn"), AIMessage("", tool_calls=[CALL]),
              ToolMessage("1  Instructions", tool_call_id="read", additional_kwargs=second_meta)]
    with client_skill_activity():
        rows = app.DeepAgentsApp._convert_messages_to_data(source)
        skills = [r for r in rows if getattr(r, "_lc_skill_activity", None)]
        assert [r._lc_skill_activity["name"] for r in skills] == [ROW["name"], "second"]
        assert [r._lc_skill_activity["status"] for r in skills] == ["failed", "loaded"]
