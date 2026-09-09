"""Validation and normal-query compilation for PubMed and Europe PMC."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_NORMAL_FORBIDDEN = re.compile(r"[\[\]{}*?~^:]")
_TERM_OPERATOR = re.compile(r"(?:&&|\|\||[!\\])")
_MAX_TOKENS = 128
_MAX_LEAVES = 64
_MAX_NESTING = 16


class QuerySyntaxError(ValueError):
    """The bounded Boolean grammar cannot represent the supplied query."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def identity_key(identity: dict[str, Any]) -> str:
    if not isinstance(identity, dict):
        raise ValueError("identity must be an object")
    return fingerprint(identity)


def json_within_limits(
    value: Any,
    *,
    max_depth: int = 64,
    max_nodes: int = 100_000,
) -> bool:
    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes:
            return False
        if isinstance(current, dict):
            if current and depth >= max_depth:
                return False
            if any(not isinstance(key, str) for key in current):
                return False
            stack.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            if current and depth >= max_depth:
                return False
            stack.extend((child, depth + 1) for child in current)
        elif isinstance(current, float) and not math.isfinite(current):
            return False
        elif current is not None and not isinstance(
            current,
            (str, int, float, bool),
        ):
            return False
    return True


def _tokenize(query: str) -> list[tuple[str, str]]:
    clean = query.strip()
    if not clean:
        raise QuerySyntaxError("query must not be empty")
    if len(clean.encode("utf-8")) > 2_000:
        raise QuerySyntaxError("query exceeds 2,000 UTF-8 bytes")
    if _CONTROL_RE.search(clean):
        raise QuerySyntaxError("query contains a control character")
    if _NORMAL_FORBIDDEN.search(clean):
        raise QuerySyntaxError(
            "query rejects provider fields, wildcards, fuzzy syntax, and "
            "proximity syntax"
        )

    tokens: list[tuple[str, str]] = []
    index = 0
    nesting = 0
    while index < len(clean):
        char = clean[index]
        if char.isspace():
            index += 1
            continue
        if char == "(":
            nesting += 1
            if nesting > _MAX_NESTING:
                raise QuerySyntaxError(
                    f"query nesting exceeds {_MAX_NESTING} levels"
                )
            tokens.append(("LPAREN", char))
            index += 1
            continue
        if char == ")":
            nesting -= 1
            tokens.append(("RPAREN", char))
            index += 1
            continue
        if char == '"':
            end = index + 1
            value: list[str] = []
            while end < len(clean) and clean[end] != '"':
                if clean[end] == "\\":
                    raise QuerySyntaxError("quoted phrases do not accept escapes")
                value.append(clean[end])
                end += 1
            if end >= len(clean):
                raise QuerySyntaxError("query contains an unbalanced quote")
            phrase = "".join(value).strip()
            if not phrase:
                raise QuerySyntaxError("quoted phrases must not be empty")
            tokens.append(("PHRASE", phrase))
            index = end + 1
            continue
        end = index
        while (
            end < len(clean)
            and not clean[end].isspace()
            and clean[end] not in "()\""
        ):
            end += 1
        word = clean[index:end]
        if _TERM_OPERATOR.search(word):
            raise QuerySyntaxError(
                "unquoted terms may not contain provider Boolean operators "
                "or backslashes"
            )
        upper = word.upper()
        tokens.append((upper, upper) if upper in {"AND", "OR", "NOT"} and word == upper else ("TERM", word))
        index = end
    if len(tokens) > _MAX_TOKENS:
        raise QuerySyntaxError(f"query exceeds {_MAX_TOKENS} tokens")
    if sum(kind in {"TERM", "PHRASE"} for kind, _ in tokens) > _MAX_LEAVES:
        raise QuerySyntaxError(f"query exceeds {_MAX_LEAVES} terms or phrases")
    return tokens


class _BooleanParser:
    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.tokens = tokens
        self.position = 0

    def parse(self) -> tuple[Any, ...]:
        if not self.tokens:
            raise QuerySyntaxError("query must contain a term")
        node = self._or_expression()
        if self.position != len(self.tokens):
            raise QuerySyntaxError(
                f"unexpected token {self.tokens[self.position][1]!r}"
            )
        return node

    def _peek(self, kind: str) -> bool:
        return (
            self.position < len(self.tokens)
            and self.tokens[self.position][0] == kind
        )

    def _consume(self, kind: str) -> tuple[str, str]:
        if not self._peek(kind):
            found = (
                self.tokens[self.position][1]
                if self.position < len(self.tokens)
                else "end of query"
            )
            raise QuerySyntaxError(f"expected {kind}, found {found!r}")
        token = self.tokens[self.position]
        self.position += 1
        return token

    def _or_expression(self) -> tuple[Any, ...]:
        node = self._and_expression()
        while self._peek("OR"):
            self._consume("OR")
            node = ("OR", node, self._and_expression())
        return node

    def _and_expression(self) -> tuple[Any, ...]:
        node = self._not_expression()
        while self._peek("AND") or any(
            self._peek(kind) for kind in ("NOT", "LPAREN", "TERM", "PHRASE")
        ):
            if self._peek("AND"):
                self._consume("AND")
            node = ("AND", node, self._not_expression())
        return node

    def _not_expression(self) -> tuple[Any, ...]:
        if self._peek("NOT"):
            self._consume("NOT")
            return ("NOT", self._not_expression())
        return self._atom()

    def _atom(self) -> tuple[Any, ...]:
        if self._peek("LPAREN"):
            self._consume("LPAREN")
            node = self._or_expression()
            self._consume("RPAREN")
            return node
        if self._peek("TERM"):
            return ("TERM", self._consume("TERM")[1])
        if self._peek("PHRASE"):
            return ("PHRASE", self._consume("PHRASE")[1])
        found = (
            self.tokens[self.position][1]
            if self.position < len(self.tokens)
            else "end of query"
        )
        raise QuerySyntaxError(f"expected a term or phrase, found {found!r}")


def parse_normal_query(query: str) -> tuple[Any, ...]:
    return _BooleanParser(_tokenize(query)).parse()


def compile_boolean(
    ast: tuple[Any, ...],
    *,
    provider: str,
    target: str,
) -> str:
    fields = {
        "pubmed": {
            "title_abstract": "[Title/Abstract]",
            "title": "[Title]",
            "all_fields": "[All Fields]",
        },
        "europe_pmc": {
            "title_abstract": "TITLE_ABS",
            "title": "TITLE",
            "all_fields": "",
        },
    }
    if provider not in fields:
        raise QuerySyntaxError(f"unknown literature source {provider!r}")
    if target not in fields[provider]:
        raise QuerySyntaxError(f"{provider} does not support target {target!r}")
    field = fields[provider][target]

    def render(node: tuple[Any, ...]) -> str:
        kind = node[0]
        if kind in {"TERM", "PHRASE"}:
            value = node[1] if kind == "TERM" else f'"{node[1]}"'
            if provider == "pubmed":
                if target == "all_fields" and kind == "TERM":
                    value = f'"{value}"'
                return f"{value}{field}" if field else value
            if kind == "TERM":
                value = re.sub(r"([+\-/])", r"\\\1", value)
            return f"{field}:{value}" if field else value
        if kind == "NOT":
            return f"(NOT {render(node[1])})"
        return f"({render(node[1])} {kind} {render(node[2])})"

    return render(ast)


def bounded_text(value: Any, limit: int = 400) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()[:limit]


def unique_identities(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for value in values:
        key = identity_key(value)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def validate_provider_query(query: str, *, source: str) -> str:
    """Validate transport/outer-scope safety; syntax meaning belongs to the provider.

    Balanced grouping prevents escaping the Europe PMC collection restriction.
    Do not translate native fields into the controlled grammar.
    """
    if not isinstance(query, str) or not query.strip():
        raise QuerySyntaxError("provider query must be a nonempty string")
    clean = query.strip()
    if len(clean.encode("utf-8")) > 2_000 or _CONTROL_RE.search(clean):
        raise QuerySyntaxError("provider query exceeds limits or contains control characters")
    stack = []
    quoted = False
    for char in clean:
        if char == "\\":
            raise QuerySyntaxError("escaped provider syntax is not supported")
        if char == '"':
            quoted = not quoted
        elif not quoted:
            if char in "([{":
                stack.append(char)
                if len(stack) > 16:
                    raise QuerySyntaxError("provider query nesting exceeds 16")
            elif char in ")]}":
                if not stack or stack.pop() != {")": "(", "]": "[", "}": "{"}[char]:
                    raise QuerySyntaxError("provider query has unbalanced grouping")
    if quoted or stack:
        raise QuerySyntaxError("provider query has unbalanced quotes or grouping")
    if source == "europe_pmc" and re.search(r"\bSORT_[A-Z_]+\s*:", clean, re.I):
        raise QuerySyntaxError("use the order argument rather than inline sort directives")
    return clean
