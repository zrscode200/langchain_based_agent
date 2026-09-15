"""Decorate native tool rows without changing tools, approvals or prompts."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import inspect

from lc_factory.skill_activity import skill_activities, skill_label
from lc_factory.upstream import AIMessage, ToolMessage

_ATTR = "_lc_skill_activity"


def _render_skill(widget):
    row = getattr(widget, _ATTR, None)
    if not row:
        return
    label = skill_label(row)
    if row["status"] == "loading" and widget.is_failed:
        label = "Couldn't load skill: " + row["name"]
    elif row["status"] == "loading" and not widget.is_pending:
        label = "Skill read result unavailable: " + row["name"]
    header = getattr(widget, "_header_widget", None)
    if header is not None:
        header.update(label + " · Agent selected")
    widget.tooltip = "\n".join(filter(None, [row.get("description"), row.get("source"), row["path"]]))


def _release_skills(group):
    """Use native accessory release so approval-owned hiding still wins."""
    released = False
    for widget in list(group._tools):
        if not getattr(widget, _ATTR, None):
            continue
        group._tools.remove(widget)
        released = True
        group._release_collapsible(widget)
        if widget.is_attached and not widget.has_own_hide_reason:
            widget.display = True
        group._present_text = group._past_text = group._present_key = None
    if released and not group._tools and group.is_attached:
        group._release_all_collapsible()
        group._stop_timer()
        group.remove()


def _chunk_messages(chunk):
    if not isinstance(chunk, tuple) or len(chunk) != 3:
        return []
    namespace, mode, data = chunk
    if namespace:  # Children have their own conversations and identity domain.
        return []
    if mode == "messages" and isinstance(data, (list, tuple)) and data:
        return [data[0]]
    if mode == "updates" and isinstance(data, dict):
        return [m for node in data.values() if isinstance(node, dict)
                for m in (node["messages"] if isinstance(node.get("messages"), list) else [])]
    return []


class SkillActivityAgent:
    """Observe exactly the stream the native executor consumes."""
    def __init__(self, agent, adapter, group_type):
        self._agent, self._adapter, self._group_type = agent, adapter, group_type

    def __getattr__(self, name):
        return getattr(self._agent, name)

    async def astream(self, *args, **kwargs):
        observations, widgets = {}, {}
        stream = self._agent.astream(*args, **kwargs)

        def apply():
            current = self._adapter._current_tool_messages
            for key, row in observations.items():
                widget = current.get(key) or widgets.get(key)
                if widget is None or widget.tool_name != "read_file":
                    continue
                widgets[key] = widget
                setattr(widget, _ATTR, row)
                _render_skill(widget)
                self._adapter._sync_tool_widget(widget)
                if widget.is_attached:
                    for group in widget.app.query(self._group_type):
                        _release_skills(group)
                        group._render_line()

        try:
            async for chunk in stream:
                for message in _chunk_messages(chunk):
                    metadata = message.get("additional_kwargs") if isinstance(message, dict) else getattr(message, "additional_kwargs", {})
                    for key, row in skill_activities(metadata).items():
                        # A replayed proposal must not overwrite a result.
                        if row["status"] != "loading" or observations.get(key, {}).get("status", "loading") == "loading":
                            observations[key] = row
                apply()  # Capture the widget before native result handling pops it.
                yield chunk
                apply()  # Tool widgets may have been created while processing it.
        finally:
            close = getattr(stream, "aclose", None)
            if close:
                await close()
            apply()


@contextmanager
def client_skill_activity():
    """Scoped native adapter patches, shared by lc-code and ddt-agent."""
    from lc_factory.upstream_cli import skill_activity_ui_modules
    app, adapter, widgets, store = skill_activity_ui_modules()
    App, Data, Type = app.DeepAgentsApp, store.MessageData, store.MessageType
    Tool, Group = widgets.ToolCallMessage, widgets.ToolGroupSummary
    execute = adapter.execute_task_textual
    convert = App._convert_messages_to_data
    from_widget, to_widget = Data.from_widget, Data.to_widget
    mount, sync = Tool.on_mount, App._sync_tool_message_state
    visibility, render = Group._apply_visibility, Group._render_line

    async def execute_with_skills(*args, **kwargs):
        bound = inspect.signature(execute).bind(*args, **kwargs)
        bound.arguments["agent"] = SkillActivityAgent(bound.arguments["agent"], bound.arguments["adapter"], Group)
        return await execute(*bound.args, **bound.kwargs)

    def convert_with_skills(messages, **kwargs):
        # Render-only copies retain exact call identity through the pinned
        # converter, which preserves each call's args object on MessageData.
        copied, pending, observations = [], {}, {}
        for message in messages:
            rows = skill_activities(getattr(message, "additional_kwargs", {}))
            if isinstance(message, AIMessage):
                calls = [{**c, "args": deepcopy(c["args"])} for c in message.tool_calls]
                for c in calls:
                    identity = id(c["args"])
                    pending[c["id"]] = identity
                    if row := rows.get(c["id"]):
                        observations[identity] = row
                message = message.model_copy(update={"tool_calls": calls})
            elif isinstance(message, ToolMessage):
                identity = pending.pop(message.tool_call_id, None)
                row = rows.get(message.tool_call_id) or observations.get(identity)
                if identity is not None and row:
                    if message.status == "error":
                        row = {**row, "status": "failed"}
                    elif row["status"] == "loading":
                        row = {**row, "status": "unavailable"}
                    observations[identity] = row
            copied.append(message)
        output = convert(copied, **kwargs)

        def decorate(rows):
            result = []
            for data in rows:
                if data.type == Type.TOOL_GROUP:
                    children = decorate(data.tool_group_messages)
                    if any(getattr(child, _ATTR, None) for child in children):
                        # Split only affected groups; ordinary history retains
                        # native compact grouping and lazy hydration.
                        pending = []
                        for child in children:
                            if getattr(child, _ATTR, None):
                                if pending:
                                    result.append(Data(type=Type.TOOL_GROUP, content="", tool_group_messages=pending))
                                    pending = []
                                result.append(child)
                            else:
                                pending.append(child)
                        if pending:
                            result.append(Data(type=Type.TOOL_GROUP, content="", tool_group_messages=pending))
                        continue
                row = observations.get(id(data.tool_args))
                if data.type == Type.TOOL and data.tool_name == "read_file" and row:
                    setattr(data, _ATTR, row)
                result.append(data)
            return result
        return decorate(output)

    def from_with_skills(cls, widget):
        data = from_widget(widget)
        if row := getattr(widget, _ATTR, None):
            setattr(data, _ATTR, row)
        return data

    def to_with_skills(self, **kwargs):
        widget = to_widget(self, **kwargs)
        if row := getattr(self, _ATTR, None):
            setattr(widget, _ATTR, row)
        return widget

    def mount_with_skills(self):
        mount(self)
        _render_skill(self)

    def sync_with_skills(self, widget):
        sync(self, widget)
        if row := getattr(widget, _ATTR, None):
            data = self._message_store.get_message(widget.id) if widget.id else None
            if data is not None:
                setattr(data, _ATTR, row)
            _render_skill(widget)

    def visible_with_skills(self):
        _release_skills(self)
        visibility(self)

    def render_with_skills(self, **kwargs):
        _release_skills(self)
        render(self, **kwargs)

    replacements = [
        (adapter, "execute_task_textual", execute_with_skills),
        (App, "_convert_messages_to_data", staticmethod(convert_with_skills)),
        (Data, "from_widget", classmethod(from_with_skills)), (Data, "to_widget", to_with_skills),
        (Tool, "on_mount", mount_with_skills), (App, "_sync_tool_message_state", sync_with_skills),
        (Group, "_apply_visibility", visible_with_skills), (Group, "_render_line", render_with_skills),
    ]
    originals = [(obj, name, inspect.getattr_static(obj, name)) for obj, name, _ in replacements]
    try:
        for obj, name, value in replacements:
            setattr(obj, name, value)
        yield
    finally:
        for obj, name, value in reversed(originals):
            setattr(obj, name, value)
