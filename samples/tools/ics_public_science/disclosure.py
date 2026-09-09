"""Fail-closed disclosure preflight for every provider-bound field."""

from __future__ import annotations

import os
import re
from re import _constants, _parser
from pathlib import Path
from typing import Any


_POLICY_VERSION = "external-disclosure.v1"
_DEFAULT_RULES = (
    ("internal-experiment-id", r"\bEXP\d{6,}\b"),
    ("notebook-id", r"\bNB-\d{4,}-\d{3,}\b"),
    ("project-accounting-id", r"\bM\d{7,}\b"),
    (
        "confidentiality-marker",
        r"\b(?:confidential|proprietary|internal[ -]only|restricted)\b",
    ),
    (
        "batch-lot-id",
        r"\b(?:batch|lot)\s*(?:no\.?|number|#)\s*[:#]?\s*\w+",
    ),
    ("document-id", r"\bDOC-\d{4,}\b"),
    ("sop-id", r"\bSOP-?\d{3,}\b"),
)


class DisclosureConfigurationError(RuntimeError):
    """An explicitly configured disclosure policy could not be loaded."""


def _load_lines(environment_name: str) -> list[str]:
    path_value = os.environ.get(environment_name, "").strip()
    if not path_value:
        return []
    path = Path(path_value).expanduser()
    try:
        if not path.is_file():
            raise DisclosureConfigurationError(
                f"{environment_name} does not identify a readable file"
            )
        if path.stat().st_size > 128 * 1024:
            raise DisclosureConfigurationError(
                f"{environment_name} exceeds the 128 KiB policy limit"
            )
        return [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    except (OSError, UnicodeError) as exc:
        raise DisclosureConfigurationError(
            f"{environment_name} could not be read"
        ) from exc


class DisclosureBlocked(ValueError):
    def __init__(self, blocked_fields: list[str], rule_ids: list[str]) -> None:
        self.blocked_fields = blocked_fields
        self.rule_ids = rule_ids
        super().__init__("provider-bound fields are blocked by disclosure policy")


class AttemptBudgetExceeded(RuntimeError):
    """The operation exhausted its total outbound-attempt allowance."""


def _contains_grouping_or_alternation(pattern: str) -> bool:
    escaped = False
    in_class = False
    for character in pattern:
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character == "[":
            in_class = True
            continue
        if character == "]" and in_class:
            in_class = False
            continue
        if in_class:
            continue
        if character in {"(", ")", "|"}:
            return True
    return False


def _validate_regex_pattern(pattern: str) -> None:
    if len(pattern.encode("utf-8")) > 1_024:
        raise ValueError("regex exceeds the 1,024-byte policy limit")
    if _contains_grouping_or_alternation(pattern):
        raise ValueError("regex grouping and alternation are not allowed")

    repeat_ops = {
        _constants.MAX_REPEAT,
        _constants.MIN_REPEAT,
        _constants.POSSESSIVE_REPEAT,
    }
    simple_repeat_atoms = {
        _constants.LITERAL,
        _constants.NOT_LITERAL,
        _constants.IN,
        _constants.CATEGORY,
    }
    allowed_leaf_ops = {
        _constants.LITERAL,
        _constants.NOT_LITERAL,
        _constants.IN,
        _constants.CATEGORY,
        _constants.AT,
        _constants.ANY,
    }

    repeat_count = 0

    def walk(parsed: Any) -> None:
        nonlocal repeat_count
        for operation, argument in parsed.data:
            if operation in repeat_ops:
                repeat_count += 1
                if repeat_count > 1:
                    raise ValueError(
                        "regex may contain at most one quantified atom"
                    )
                _, _, repeated = argument
                if (
                    len(repeated.data) != 1
                    or repeated.data[0][0] not in simple_repeat_atoms
                ):
                    raise ValueError(
                        "regex repetitions may only wrap one simple atom"
                    )
                continue
            if operation in allowed_leaf_ops:
                continue
            raise ValueError(
                "regex grouping, alternation, and backtracking constructs "
                "are not allowed"
            )

    walk(_parser.parse(pattern, re.IGNORECASE))
    if repeat_count and not (
        pattern.startswith("^") or pattern.startswith(r"\A")
    ):
        raise ValueError("regexes with quantifiers must be anchored at the start")


def _compile_rule(pattern: str) -> re.Pattern[str]:
    _validate_regex_pattern(pattern)
    return re.compile(pattern, re.IGNORECASE)


class DisclosureTracker:
    """Tracks approved fields and actual outbound calls without retaining values."""

    def __init__(self, *, attempt_ceiling: int | None = None) -> None:
        operator_patterns = list(
            dict.fromkeys(_load_lines("ICS_DISCLOSURE_DENY_FILE"))
        )
        if len(operator_patterns) > 256:
            raise DisclosureConfigurationError(
                "ICS_DISCLOSURE_DENY_FILE exceeds 256 unique rules"
            )
        extra = [
            (f"operator-rule-{index}", pattern)
            for index, pattern in enumerate(
                operator_patterns,
                start=1,
            )
        ]
        try:
            self._rules = [
                (rule_id, re.compile(pattern, re.IGNORECASE))
                for rule_id, pattern in _DEFAULT_RULES
            ] + [
                (rule_id, _compile_rule(pattern))
                for rule_id, pattern in extra
            ]
        except Exception as exc:
            raise DisclosureConfigurationError(
                "the configured disclosure policy contains an unsafe regex"
            ) from exc
        self._allowed = {
            term.casefold()
            for term in _load_lines("ICS_DISCLOSURE_ALLOW_TERMS")
        }
        self._approved_fields: list[str] = []
        self._blocked_fields: list[str] = []
        self._blocked_rule_ids: list[str] = []
        self._preflight_attempted = False
        self._preflight_permitted = False
        self._outbound_calls = 0
        self._attempt_ceiling = attempt_ceiling

    def preflight(self, fields: dict[str, Any]) -> None:
        if self._preflight_attempted:
            raise RuntimeError("disclosure preflight may run only once per tracker")
        self._preflight_attempted = True
        blocked: set[str] = set()
        rules: set[str] = set()
        for path, raw in fields.items():
            values = raw if isinstance(raw, list) else [raw]
            for value in values:
                if value is None:
                    continue
                text = str(value)
                for rule_id, pattern in self._rules:
                    try:
                        for match in pattern.finditer(text):
                            if (
                                match.group(0).strip().casefold()
                                not in self._allowed
                            ):
                                blocked.add(path)
                                rules.add(rule_id)
                    except Exception as exc:
                        raise DisclosureConfigurationError(
                            "the configured disclosure policy could not be evaluated"
                        ) from exc
        if blocked:
            self._blocked_fields = sorted(blocked)
            self._blocked_rule_ids = sorted(rules)
            raise DisclosureBlocked(
                self._blocked_fields,
                self._blocked_rule_ids,
            )
        self._approved_fields = sorted(fields)
        self._preflight_permitted = True

    def assert_cleared(self, sent_fields: list[str]) -> None:
        if not self._preflight_permitted:
            raise RuntimeError("an outbound call requires a permitted preflight")
        unknown = set(sent_fields) - set(self._approved_fields)
        if unknown:
            raise RuntimeError(
                "an outbound call attempted fields not covered by preflight"
            )

    def record_call(self, sent_fields: list[str]) -> None:
        self.assert_cleared(sent_fields)
        if (
            self._attempt_ceiling is not None
            and self._outbound_calls >= self._attempt_ceiling
        ):
            raise AttemptBudgetExceeded(
                "the operation exhausted its outbound-attempt ceiling"
            )
        self._outbound_calls += 1

    def result(self, *, reached: bool = True) -> dict[str, Any]:
        if self._blocked_fields:
            decision = "blocked"
            sent_fields: list[str] = []
        elif reached and self._preflight_permitted:
            decision = "permitted"
            sent_fields = self._approved_fields
        else:
            decision = "not_reached"
            sent_fields = []
        return {
            "decision": decision,
            "outbound_calls": self._outbound_calls,
            "sent_fields": sent_fields,
            "blocked_fields": self._blocked_fields,
            "blocked_rule_ids": self._blocked_rule_ids,
            "policy_version": _POLICY_VERSION if reached else None,
        }


def not_reached_disclosure() -> dict[str, Any]:
    return {
        "decision": "not_reached",
        "outbound_calls": 0,
        "sent_fields": [],
        "blocked_fields": [],
        "blocked_rule_ids": [],
        "policy_version": None,
    }
