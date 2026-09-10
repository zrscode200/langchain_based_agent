"""Local, paged child conversation capture for the host detail window.

Only message content, exposed reasoning and tool calls are projected. Callback
metadata, credentials, opaque reasoning and hook transport are never serialized.
The temporary spool is removed on eviction/close and never sent to a model.
"""
from __future__ import annotations

import hashlib
import json
import tempfile

PAGE_CHARS = 24_000
MAX_BYTES = 64 * 1024 * 1024


def content_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content) if content is not None else ""
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            kind = block.get("type", "content")
            if kind in ("thinking", "reasoning", "reasoning_content"):
                value = block.get("thinking", block.get("reasoning", block.get("text")))
                if isinstance(value, str):
                    parts.append("Reasoning (provider-exposed)\n" + value)
                for summary in block.get("summary", []) if isinstance(block.get("summary"), list) else []:
                    if isinstance(summary, dict) and isinstance(summary.get("text"), str):
                        parts.append("Reasoning summary (provider-exposed)\n" + summary["text"])
            elif isinstance(block.get("text"), str):
                parts.append(block["text"])
            else:
                parts.append(f"[{kind}: non-text content]")
    return "\n\n".join(parts)


def message_text(message):
    kind = getattr(message, "type", "message")
    role = {"human": "User / assignment", "ai": "Assistant", "tool": "Tool result", "system": "System"}.get(kind, kind)
    if kind == "tool":
        role += f" · {message.name or 'tool'} · {message.tool_call_id} · {message.status}"
    parts = [role, content_text(message.content)]
    # DeepSeek and some OpenAI-compatible adapters expose this separately.
    reasoning = message.additional_kwargs.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        parts.insert(1, "Reasoning (provider-exposed)\n" + reasoning)
    for call in getattr(message, "tool_calls", []):
        parts.append(f"Tool call · {call['name']} · {call['id']}\n" +
                     json.dumps(call["args"], ensure_ascii=False, indent=2, default=str))
    return "\n\n".join(part for part in parts if part)


def projected_message(message):
    """Keep displayable message fields, never arbitrary transport metadata."""
    content, reasoning = [], []
    if isinstance(message.content, str):
        content.append(message.content)
    elif isinstance(message.content, list):
        for block in message.content:
            if isinstance(block, str):
                content.append(block)
            elif isinstance(block, dict):
                kind = block.get("type", "content")
                if kind in ("thinking", "reasoning", "reasoning_content"):
                    value = block.get("thinking", block.get("reasoning", block.get("text")))
                    if isinstance(value, str):
                        reasoning.append(value)
                    for summary in block.get("summary", []) if isinstance(block.get("summary"), list) else []:
                        if isinstance(summary, dict) and isinstance(summary.get("text"), str):
                            reasoning.append(summary["text"])
                elif kind in ("text", "output_text") and isinstance(block.get("text"), str):
                    content.append(block["text"])
                else:
                    content.append("[Non-text content]")
    value = {"type": getattr(message, "type", "message"), "content": "\n\n".join(content)}
    exposed = message.additional_kwargs.get("reasoning_content")
    if isinstance(exposed, str) and exposed and exposed not in reasoning:
        reasoning.append(exposed)
    if reasoning:
        value["additional_kwargs"] = {"reasoning_content": "\n\n".join(reasoning)}
    calls = getattr(message, "tool_calls", [])
    if calls:
        value["tool_calls"] = [{"id": call.get("id"), "name": call.get("name"), "args": call.get("args")} for call in calls]
    if value["type"] == "tool":
        value.update(name=message.name, tool_call_id=message.tool_call_id, status=message.status)
        artifact = getattr(message, "artifact", None)
        if isinstance(artifact, dict) and isinstance(artifact.get("exit_code"), int):
            value["artifact"] = {"exit_code": artifact["exit_code"]}
    return value


def preview_message(value):
    """Bound a polling response; complete projected content is separately paged."""
    truncated = False
    def clip(text, limit):
        nonlocal truncated
        if not isinstance(text, str):
            return text
        if len(text) > limit:
            truncated = True
            return text[:limit] + "\n[Preview shortened — open full message]"
        return text
    value["content"] = clip(value["content"], 6000)
    if value.get("additional_kwargs"):
        value["additional_kwargs"]["reasoning_content"] = clip(value["additional_kwargs"]["reasoning_content"], 3000)
    calls = value.get("tool_calls", [])
    if len(calls) > 12:
        truncated = True
        value["tool_calls"] = calls[:12]
    for call in value.get("tool_calls", []):
        args = json.dumps(call["args"], ensure_ascii=False, default=str)
        if len(args) > 1500:
            call["args"] = clip(args, 1500)
    return value, truncated


class ChildTranscript:
    """Append-only message observations, with complete text split across pages.

    Repeated checkpoint observations are deduplicated; a revised message is an
    explicit later observation. Large output spills into a private temporary
    file instead of retaining many copies in status polling responses.
    """

    def __init__(self, *, max_bytes=MAX_BYTES):
        self.max_bytes = max_bytes
        self.file = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
        self.records_file = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
        self.records_size = 0
        self.records = {}  # identity -> (offset, byte length, revision, ordinal)
        self.revision = 0
        self.pages = []  # (byte offset, byte length, character count)
        self.size = 0
        self.seen = {}
        self.limited = self.closed = False
        self.storage_error = False

    def capture(self, messages):
        if self.closed or self.limited or self.storage_error:
            return
        for message in messages:
            text = message_text(message)
            projected = projected_message(message)
            encoded = json.dumps(projected, ensure_ascii=False, separators=(",", ":"), default=str).encode()
            digest = hashlib.sha256(encoded).hexdigest()
            # Tool returns are first observed before add_messages assigns their
            # checkpoint ID. The originating call is stable across both views.
            key = ("tool", message.tool_call_id) if getattr(message, "type", None) == "tool" else (
                "message", getattr(message, "id", None) or digest)
            if self.seen.get(key) == digest:
                continue
            prefix = "\n\n────────\n\n" if self.size else ""
            if key in self.seen:
                prefix += "Updated message\n\n"
            try:
                if self.size + self.records_size + len(encoded) + len((prefix + text).encode()) > self.max_bytes:
                    self.limited = True
                    return
                if not self.append(prefix + text):
                    return
                self.records_file.seek(self.records_size)
                self.records_file.write(encoded)
                identity = hashlib.sha256(json.dumps(key).encode()).hexdigest()
                ordinal = self.records[identity][3] if identity in self.records else len(self.records)
                self.revision += 1
                self.records[identity] = (self.records_size, len(encoded), self.revision, ordinal)
                self.records_size += len(encoded)
            except OSError:
                # Optional observation must not fail the child task when the
                # local spool cannot grow. Keep the already captured prefix.
                self.storage_error = True
                return
            self.seen[key] = digest

    def append(self, text):
        if self.closed or self.limited or self.storage_error:
            return False
        if self.size + self.records_size + len(text.encode()) > self.max_bytes:
            self.limited = True
            return False
        while text:
            if not self.pages or self.pages[-1][2] == PAGE_CHARS:
                self.pages.append((self.size, 0, 0))
            offset, length, count = self.pages[-1]
            part, text = text[:PAGE_CHARS - count], text[PAGE_CHARS - count:]
            data = part.encode()
            self.file.seek(self.size)
            self.file.write(data)
            self.size += len(data)
            self.pages[-1] = offset, length + len(data), count + len(part)
        return True

    def page(self, index=-1):
        if isinstance(index, bool) or not isinstance(index, int) or index < -1:
            raise ValueError("Invalid transcript page")
        count = len(self.pages)
        index = max(0, count - 1) if index == -1 else index
        if index >= max(1, count) or self.closed:
            raise ValueError("Transcript page unavailable")
        text = ""
        if count:
            offset, length, _ = self.pages[index]
            self.file.seek(offset)
            text = self.file.read(length).decode()
        notice = "Only reasoning exposed by the provider is available."
        if self.limited:
            notice = (f"Conversation capture reached its {self.max_bytes:,} byte limit; "
                      "later messages were not retained.")
        if self.storage_error:
            notice = "Local conversation storage failed; later messages were not retained."
        return {"text": text, "page": index, "pages": count, "limited": self.limited, "notice": notice}

    def _record(self, identity):
        if self.closed or identity not in self.records:
            raise ValueError("Message unavailable")
        offset, length, _, _ = self.records[identity]
        self.records_file.seek(offset)
        return json.loads(self.records_file.read(length))

    def structured(self, *, before=None, after=None):
        """Cursor reads of newest messages, older history, or changed identities."""
        for cursor in (before, after):
            if cursor is not None and (isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0):
                raise ValueError("Invalid conversation cursor")
        if self.closed or (before is not None and after is not None):
            raise ValueError("Conversation unavailable")
        if (after is not None and after > self.revision) or (before is not None and before > len(self.records)):
            raise ValueError("Conversation cursor unavailable")
        entries = list(self.records.items())
        if after is not None:
            entries = sorted((item for item in entries if item[1][2] > after), key=lambda item: item[1][2])[:12]
        else:
            entries = [item for item in entries if before is None or item[1][3] < before][-12:]
        messages = []
        for identity, (_, _, revision, ordinal) in entries:
            value, truncated = preview_message(self._record(identity))
            value["id"] = identity
            value["_transcript"] = {"revision": revision, "order": ordinal, "truncated": truncated}
            messages.append(value)
        return {"version": 1, "messages": messages,
                "cursor": entries[-1][1][2] if after is not None and entries else self.revision,
                "before": min((item[1][3] for item in entries), default=0),
                "notice": self.page()["notice"], "limited": self.limited}

    def record_page(self, identity, *, offset=0, revision=None):
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("Invalid message offset")
        value = self._record(identity)
        current = self.records[identity][2]
        if revision != current or isinstance(revision, bool):
            raise ValueError("Message changed; reopen the current message")
        text = json.dumps(value, ensure_ascii=False, indent=2)
        if offset > len(text):
            raise ValueError("Message offset unavailable")
        return {"text": text[offset:offset + PAGE_CHARS], "offset": offset, "next": min(offset + PAGE_CHARS, len(text)), "total": len(text), "revision": current}

    def close(self):
        self.closed = True
        self.file.close()
        self.records_file.close()
        self.records.clear()
        self.seen.clear()
