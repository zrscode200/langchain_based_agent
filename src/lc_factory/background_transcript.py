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


class ChildTranscript:
    """Append-only message observations, with complete text split across pages.

    Repeated checkpoint observations are deduplicated; a revised message is an
    explicit later observation. Large output spills into a private temporary
    file instead of retaining many copies in status polling responses.
    """

    def __init__(self, *, max_bytes=MAX_BYTES):
        self.max_bytes = max_bytes
        self.file = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
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
            digest = hashlib.sha256(text.encode()).hexdigest()
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
                if not self.append(prefix + text):
                    return
            except OSError:
                # Optional observation must not fail the child task when the
                # local spool cannot grow. Keep the already captured prefix.
                self.storage_error = True
                return
            self.seen[key] = digest

    def append(self, text):
        if self.closed or self.limited or self.storage_error:
            return False
        if self.size + len(text.encode()) > self.max_bytes:
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

    def close(self):
        self.closed = True
        self.file.close()
        self.seen.clear()
