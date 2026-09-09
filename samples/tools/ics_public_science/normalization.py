"""Shared normalized record builders for private provider adapters."""

from __future__ import annotations

from typing import Any

from envelopes import currentness
from validation import bounded_text, unique_identities


def source_fact(
    name: str,
    role: str,
    value: Any,
    state: str,
    source: str,
) -> dict[str, Any]:
    return {
        "name": name,
        "role": role,
        "value": value,
        "state": state,
        "source": source,
    }


def source_item(
    item_id: str,
    item_type: str,
    facts: list[dict[str, Any]],
    children: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "item_type": item_type,
        "facts": facts,
        "children": children or [],
    }


def source_family(
    name: str,
    *,
    state: str = "not_requested",
    items: list[dict[str, Any]] | None = None,
    represented_count: int | None = None,
    omitted_count: int = 0,
) -> dict[str, Any]:
    values = items or []
    return {
        "name": name,
        "state": state,
        "items": values,
        "represented_count": (
            len(values) if represented_count is None else represented_count
        ),
        "omitted_count": omitted_count,
    }


def candidate(
    *,
    identity: dict[str, Any],
    provider: str,
    record_class: str,
    title: str | None,
    contributors: list[str] | None,
    contributor_count: int | None,
    container: str | None,
    date: str | None,
    searched_scope: str,
    provider_order: str,
    absolute_position: int | None,
    represented_identifiers: list[dict[str, Any]] | None = None,
    record_role: str | None = None,
    date_role: str | None = None,
    context_kind: str = "none",
    context_text: str | None = None,
    context_source_role: str | None = None,
    matched_scope: str | None = None,
    integrity_signals: list[dict[str, Any]] | None = None,
    access_signals: list[dict[str, Any]] | None = None,
    currentness_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context = bounded_text(context_text, 600) if context_text else None
    return {
        "identity": identity,
        "represented_identifiers": unique_identities(
            [identity, *(represented_identifiers or [])]
        ),
        "record_class": record_class,
        "record_role": record_role,
        "title": bounded_text(title, 1000) or None,
        "contributors": [
            bounded_text(value, 300) for value in (contributors or [])[:4]
        ],
        "contributor_count": contributor_count,
        "container": bounded_text(container, 500) or None,
        "date": bounded_text(date, 80) or None,
        "date_role": date_role,
        "selection_context": {
            "kind": context_kind,
            "text": context,
            "represented_chars": len(context) if context is not None else None,
            "truncated": bool(context_text and len(context_text) > 600),
            "source_role": context_source_role,
        },
        "searched_scope": searched_scope,
        "matched_scope": matched_scope,
        "provider_order": provider_order,
        "absolute_position": absolute_position,
        "integrity_signals": integrity_signals or [],
        "access_signals": access_signals or [],
        "currentness": currentness_state or currentness(),
    }


def projection_manifest(
    profile: str,
    represented: list[str],
    *,
    unsupported: list[str] | None = None,
    omitted: list[str] | None = None,
) -> dict[str, Any]:
    unsupported_set = set(unsupported or [])
    omitted_set = set(omitted or [])
    names = list(dict.fromkeys([*represented, *unsupported_set, *omitted_set]))
    families = []
    for name in names:
        if name in unsupported_set:
            state = "unsupported"
            failure_code = "unsupported_combination"
        elif name in omitted_set:
            state = "omitted_by_limit"
            failure_code = "projection_incomplete"
        else:
            state = "represented"
            failure_code = None
        families.append(
            {
                "name": name,
                "state": state,
                "represented_count": 1 if state == "represented" else 0,
                "omitted_count": 1 if state == "omitted_by_limit" else 0,
                "failure_code": failure_code,
            }
        )
    return {"profile": profile, "families": families}


def qualified_record(
    *,
    requested_identity: dict[str, Any],
    canonical_identity: dict[str, Any],
    provider: str,
    record_class: str,
    title: str | None,
    contributors: list[str] | None,
    contributor_count: int | None,
    container_title: str | None,
    projection: str,
    extension: dict[str, Any],
    aliases: list[dict[str, Any]] | None = None,
    redirects: list[dict[str, Any]] | None = None,
    record_role: str | None = None,
    version: dict[str, Any] | None = None,
    container_kind: str | None = None,
    publisher: str | None = None,
    dates: list[dict[str, Any]] | None = None,
    currentness_state: dict[str, Any] | None = None,
    integrity_state: dict[str, Any] | None = None,
    descriptors: dict[str, Any] | None = None,
    represented_families: list[str] | None = None,
    access: dict[str, Any] | None = None,
    content_manifest: list[dict[str, Any]] | None = None,
    relationships: list[dict[str, Any]] | None = None,
    lineage: dict[str, Any] | None = None,
    warnings: list[dict[str, Any]] | None = None,
    contradictions: list[dict[str, Any]] | None = None,
    locator_id: str = "",
) -> dict[str, Any]:
    return {
        "requested_identity": requested_identity,
        "canonical_identity": canonical_identity,
        "aliases": aliases or [],
        "redirects": redirects or [],
        "record_class": record_class,
        "record_role": record_role,
        "version": version
        or {
            "exact_version": None,
            "version_role": None,
            "source_current": None,
            "selection_basis": None,
        },
        "title": bounded_text(title, 2000) or None,
        "contributors": {
            "names": [
                bounded_text(value, 300) for value in (contributors or [])[:100]
            ],
            "represented_count": contributor_count,
            "truncated": bool(
                contributor_count is not None and contributor_count > 100
            ),
        },
        "container": {
            "title": bounded_text(container_title, 500) or None,
            "kind": container_kind,
            "publisher": bounded_text(publisher, 500) or None,
        },
        "dates": dates or [],
        "currentness": currentness_state or currentness(),
        "integrity": integrity_state
        or {"state": "unknown", "labels": [], "facts": []},
        "study_or_evidence_descriptors": descriptors
        or source_family("descriptors", state="not_represented"),
        "projection": projection_manifest(
            projection, represented_families or []
        ),
        "access": access
        or {
            "state": "unknown",
            "facts": [],
            "content_eligibility": "not_evaluated",
        },
        "content_manifest": content_manifest or [],
        "direct_relationships": relationships or [],
        "lineage": lineage or {"state": "unknown", "facts": []},
        "warnings": warnings or [],
        "contradictions": contradictions or [],
        "source_locator": {
            "provider": provider,
            "locator_id": locator_id,
            "canonical_identity": canonical_identity,
        },
        "extension": extension,
    }


def relationship_edge(
    *,
    seed: dict[str, Any],
    relation: str,
    provider: str,
    provider_vocabulary: str,
    subject: dict[str, Any],
    target: dict[str, Any] | None,
    retrieved_at: str,
    unresolved_lead: dict[str, Any] | None = None,
    asserted_by: str | None = None,
    dates: list[dict[str, Any]] | None = None,
    provider_position: int | None = None,
    coverage_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    assertion = {
        "provider": provider,
        "provider_vocabulary": provider_vocabulary,
        "subject": subject,
        "object": target,
        "unresolved_object": unresolved_lead,
        "asserted_by": asserted_by,
        "dates": dates or [],
        "retrieved_at": retrieved_at,
        "provider_position": provider_position,
    }
    return {
        "seed": seed,
        "relation": relation,
        "target": target,
        "unresolved_lead": unresolved_lead,
        "assertions": [assertion],
        "verification": "unresolved" if target is None else "single_source",
        "conflicts": [],
        "coverage": coverage_state
        or {
            "scope": "one represented provider assertion",
            "exhaustive": False,
            "returned": 1,
            "provider_count": None,
            "boundary": None,
            "limitations": [],
        },
    }
