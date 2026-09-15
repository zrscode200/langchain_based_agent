"""Observed skill reads, carried with the messages that caused them.

This is display metadata, not an execution/permission signal. A successful
read proves delivery of instructions, never that the model followed them.
"""
from __future__ import annotations

from dataclasses import replace
import re

from lc_factory.upstream import AgentMiddleware, AIMessage, Command, ToolMessage

SKILL_ACTIVITY = "lc_skill_activity"
_STATES = {"loading", "loaded", "partial", "empty", "failed", "unavailable"}
_FIELDS = {"name": 200, "path": 4096, "description": 2000, "source": 200}


def skill_activities(kwargs):
    """Allowlist small display records; never forward arbitrary metadata."""
    raw = kwargs.get(SKILL_ACTIVITY) if isinstance(kwargs, dict) else None
    if not isinstance(raw, dict):
        return {}
    result = {}
    for key, row in list(raw.items())[:128]:
        if not isinstance(key, str) or not key or len(key) > 300 or not isinstance(row, dict):
            continue
        if row.get("origin") != "agent" or not isinstance(row.get("status"), str) or row["status"] not in _STATES:
            continue
        if not isinstance(row.get("name"), str) or not row["name"].strip():
            continue
        if not isinstance(row.get("path"), str) or not row["path"].startswith("/"):
            continue
        value = {k: re.sub(r"[\x00-\x1f\x7f]", " ", row.get(k, ""))[:limit]
                 for k, limit in _FIELDS.items() if isinstance(row.get(k, ""), str)}
        value.update(origin="agent", status=row["status"])
        for field in ("offset", "limit"):
            if type(row.get(field)) is int:
                value[field] = row[field]
        result[key] = value
    return result


def skill_for_call(call, catalog):
    """Match the effective catalog's exact absolute path; do no extra I/O."""
    if call.get("name") != "read_file" or not isinstance(call.get("id"), str) or not call["id"]:
        return None
    args = call.get("args")
    path = args.get("file_path") if isinstance(args, dict) else None
    if not isinstance(path, str) or not path.startswith("/"):
        return None
    for row in catalog if isinstance(catalog, list) else []:
        candidate = row.get("path") if isinstance(row, dict) else None
        if not isinstance(candidate, str) or not candidate.startswith("/"):
            continue
        # Do not collapse '..': a symlink in the requested path could make
        # that lexical alias resolve to a different file at execution time.
        if path != candidate:
            continue
        value = {k: row.get(k, "") for k in _FIELDS}
        value.update(origin="agent", status="loading", offset=args.get("offset", 0),
                     limit=args.get("limit", 100))
        return skill_activities({SKILL_ACTIVITY: {call["id"]: value}}).get(call["id"])
    return None


def skill_label(row):
    labels = {"loading": "Loading skill", "loaded": "Loaded skill",
              "partial": "Loaded skill excerpt", "empty": "No skill instructions loaded",
              "failed": "Couldn't load skill", "unavailable": "Skill read result unavailable"}
    return f"{labels[row['status']]}: {row['name']}"


def _read_status(message, row):
    if message.status == "error":
        return "failed"
    content = message.content
    if message.status != "success" or not isinstance(content, str):
        return "unavailable"
    # SDK 0.7.14 puts the actual window in a header, followed by verbatim
    # source. Only the tool's leading notices may precede that header: a
    # header-looking line inside the source must never replace its range.
    header = re.match(
        r"(?:\[Requested offset -\d+ is before the start of the file; read from line 1 instead\.\]\n"
        r"|\[Output was truncated due to size limits\.[^\n]*\]\n)*"
        r"@@ lines (\d+)-(\d+)(?: of (\d+))?"
        r"(?: \| next offset (\d+))?"
        r"( \| truncated due to size| \| truncated mid-line \| \d+ of \d+ chars)? @@\n",
        content,
    )
    if header:
        body = content[header.end():]
        if not body.strip():
            return "empty"
        start, end, total, next_offset, truncated = header.groups()
        if int(start) < 1 or int(end) < int(start) or (total and int(end) > int(total)):
            return "unavailable"
        if int(start) > 1 or next_offset is not None or truncated or total is None or int(end) < int(total):
            return "partial"
        return "loaded"
    # Retain legacy numbered output support. Warnings, empty files and
    # zero-line reads must not become successful instruction loads.
    lines = re.findall(r"(?m)^\s*(\d+)(?:\.\d+)?  (.*)$", content)
    if not lines:
        return "empty" if content.startswith(("System reminder: File exists but has empty contents",
                                               "System reminder: no lines were read")) else "unavailable"
    if not any(text.strip() for _, text in lines):
        return "empty"
    # Use the tool's actual read window, not unvalidated request parameters:
    # Pydantic accepts numeric strings, and an exact limit can cover a full file.
    remaining = re.search(
        r"\n\n\[Read \d+ lines? \(lines \d+-\d+(?: of \d+ total)?\)\. "
        r"(?:More lines remain|\d+ lines? remaining) from offset \d+\.\]", content)
    if (int(lines[0][0]) > 1 or remaining
            or "\n\n[Output was truncated due to size limits." in content
            ):
        return "partial"
    return "loaded"


class SkillActivityMiddleware(AgentMiddleware):
    """Stateless observation shared by main agents and children."""

    def after_model(self, state, runtime):
        messages = state.get("messages", [])
        last = messages[-1] if messages else None
        if not isinstance(last, AIMessage) or not last.id:
            return None
        activities = {call["id"]: row for call in last.tool_calls
                      if (row := skill_for_call(call, state.get("skills_metadata", [])))}
        kwargs = {k: v for k, v in last.additional_kwargs.items() if k != SKILL_ACTIVITY}
        if activities:
            kwargs[SKILL_ACTIVITY] = activities
        if kwargs == last.additional_kwargs:
            return None
        return {"messages": [last.model_copy(update={"additional_kwargs": kwargs})]}

    async def aafter_model(self, state, runtime):
        return self.after_model(state, runtime)

    def _annotate(self, result, call, row):
        if isinstance(result, ToolMessage) and result.tool_call_id == call["id"]:
            kwargs = {**result.additional_kwargs,
                      SKILL_ACTIVITY: {call["id"]: {**row, "status": _read_status(result, row)}}}
            return result.model_copy(update={"additional_kwargs": kwargs})
        if isinstance(result, Command) and isinstance(result.update, dict):
            messages = result.update.get("messages")
            if isinstance(messages, list):
                return replace(result, update={**result.update,
                    "messages": [self._annotate(m, call, row) for m in messages]})
        if isinstance(result, list):
            return [self._annotate(m, call, row) for m in result]
        return result

    def wrap_tool_call(self, request, handler):
        row = skill_for_call(request.tool_call, request.state.get("skills_metadata", []))
        result = handler(request)
        return self._annotate(result, request.tool_call, row) if row else result

    async def awrap_tool_call(self, request, handler):
        row = skill_for_call(request.tool_call, request.state.get("skills_metadata", []))
        result = await handler(request)
        return self._annotate(result, request.tool_call, row) if row else result
