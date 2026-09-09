"""Mechanical response-envelope construction."""

from __future__ import annotations

import re
import time
from typing import Any


STAGE_NAMES = (
    "validation",
    "disclosure",
    "request_build",
    "transport",
    "provider_body",
    "identity_or_query_fidelity",
    "projection_or_selection",
    "result",
    "currentness",
)
STAGE_STATES = {
    "not_reached",
    "not_requested",
    "succeeded",
    "valid_zero",
    "unsupported",
    "blocked",
    "failed",
    "unknown",
    "inconsistent",
}
FAILURE_CODES = {
    "invalid_request",
    "invalid_identifier",
    "unsupported_combination",
    "batch_limit_exceeded",
    "resource_plan_exceeded",
    "disclosure_blocked",
    "license_blocked",
    "license_unknown",
    "adapter_disabled",
    "configuration_unavailable",
    "invalid_handle",
    "unknown_handle_version",
    "expired_continuation",
    "stale_content_handle",
    "handle_key_unavailable",
    "dns_failure",
    "tls_failure",
    "timeout",
    "connection_failure",
    "redirect_blocked",
    "response_too_large",
    "unexpected_content_encoding",
    "throttled",
    "provider_invalid_request",
    "provider_forbidden",
    "provider_not_found",
    "provider_server_error",
    "provider_error_body",
    "provider_schema_drift",
    "query_repaired",
    "query_fidelity_unknown",
    "identity_mismatch",
    "ambiguous_identity",
    "unsupported_agency",
    "projection_incomplete",
    "selection_not_represented",
    "unsupported_representation",
    "parser_limit_exceeded",
    "checksum_mismatch",
    "source_visibility_limit",
    "pagination_boundary",
    "snapshot_drift",
    "currentness_unknown",
    "relationship_coverage_limited",
}
WARNING_CODES = {
    "provider_query_normalized",
    "query_repaired",
    "query_semantics_not_equivalent",
    "provider_rank_local_only",
    "candidate_text_is_triage_only",
    "preprint_not_peer_reviewed",
    "registry_plan_not_observed_result",
    "posted_results_not_peer_reviewed",
    "access_not_permission",
    "currentness_not_history",
    "partial_coverage",
    "source_visibility_limit",
    "external_content_untrusted",
}


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def stages(**updates: str) -> dict[str, str]:
    value = {name: "not_reached" for name in STAGE_NAMES}
    for name, state in updates.items():
        if name not in value:
            raise ValueError(f"unknown outcome stage {name!r}")
        if state not in STAGE_STATES:
            raise ValueError(f"unknown outcome stage state {state!r}")
        value[name] = state
    return value


def failure(
    code: str,
    stage: str,
    scope: str,
    detail: str,
    *,
    retryable: bool = False,
    absence: bool = False,
) -> dict[str, Any]:
    if code not in FAILURE_CODES:
        raise ValueError(f"unknown failure code {code!r}")
    if stage not in STAGE_NAMES:
        raise ValueError(f"unknown failure stage {stage!r}")
    clean = re.sub(r"\s+", " ", str(detail)).strip()[:800]
    return {
        "code": code,
        "stage": stage,
        "scope": scope,
        "retryable": retryable,
        "absence": absence,
        "detail": clean,
    }


def warning(code: str, scope: str, detail: str) -> dict[str, str]:
    if code not in WARNING_CODES:
        raise ValueError(f"unknown warning code {code!r}")
    return {
        "code": code,
        "scope": scope,
        "detail": re.sub(r"\s+", " ", str(detail)).strip()[:800],
    }


def currentness(
    observations: list[dict[str, Any]] | None = None,
    drift: str = "unknown",
) -> dict[str, Any]:
    if drift not in {"none", "detected", "unknown"}:
        raise ValueError(f"unknown currentness drift state {drift!r}")
    return {"observations": observations or [], "drift": drift}


def validate_adapter_metadata(result: dict[str, Any]) -> None:
    if not isinstance(result, dict):
        raise ValueError("adapter outcome must be an object")
    result_stages = result.get("stages")
    if not isinstance(result_stages, dict) or set(result_stages) != set(STAGE_NAMES):
        raise ValueError("adapter outcome stages are incomplete or contain unknown keys")
    if any(value not in STAGE_STATES for value in result_stages.values()):
        raise ValueError("adapter outcome contains an unknown stage state")
    failures = result.get("failures")
    warnings = result.get("warnings")
    if not isinstance(failures, list) or not isinstance(warnings, list):
        raise ValueError("adapter outcome must contain failure and warning arrays")
    if not isinstance(result.get("currentness"), dict):
        raise ValueError("adapter outcome must contain currentness metadata")
    for item in failures:
        if not isinstance(item, dict) or set(item) != {
            "code",
            "stage",
            "scope",
            "retryable",
            "absence",
            "detail",
        }:
            raise ValueError("adapter failure has an invalid shape")
        if item["code"] not in FAILURE_CODES or item["stage"] not in STAGE_NAMES:
            raise ValueError("adapter failure uses an unknown code or stage")
    for item in warnings:
        if not isinstance(item, dict) or set(item) != {"code", "scope", "detail"}:
            raise ValueError("adapter warning has an invalid shape")
        if item["code"] not in WARNING_CODES:
            raise ValueError("adapter warning uses an unknown code")


def coverage(
    scope: str,
    returned: int,
    *,
    exhaustive: bool = False,
    provider_count: int | None = None,
    boundary: str | None = None,
    limitations: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "scope": scope,
        "exhaustive": exhaustive,
        "returned": returned,
        "provider_count": provider_count,
        "boundary": boundary,
        "limitations": limitations or [],
    }
