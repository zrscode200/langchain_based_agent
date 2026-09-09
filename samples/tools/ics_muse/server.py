#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp==1.28.1",
#   "httpx>=0.27",
#   "pydantic>=2",
#   "pyyaml>=6.0",
#   "truststore>=0.9",
#   "typing-extensions>=4.12",
# ]
# ///
# NOTE: mcp is pinned to the version the host runtime itself pins. FastMCP was
# removed in later mcp releases, and matching the runtime's pin keeps connector
# behavior aligned with the client that consumes it.
"""ICS MUSE internal retrieval connector (MCP, stdio).

Program-scoped internal retrieval over the confirmed public MUSE search APIs.

MUSE is a search and discovery layer, NOT the authoritative program store. A
returned card identifies candidate source material; it establishes nothing about
applicability, currentness, completeness, or authority.

What this adapter owns (see references/muse-search-guide.md):
  - program-scope enforcement: the program filter is injected from the active
    workspace configuration and CANNOT be removed by the model;
  - exact request construction: the model supplies typed conditions, never raw
    datasource field names or a raw conditional envelope;
  - minimum result shape: `title` is always requested, because `fieldsToExtract`
    can otherwise silently drop it;
  - effective-mode reporting: a hybrid/KNN request downgraded to keyword because
    the source does not support it is reported as such, never presented as
    equivalent execution. MUSE does not echo the requested mode back, so the
    strategies it reports and any KNN hit count are surfaced rather than inferred;
  - failure normalization that preserves source behavior: 401 stays
    authentication failure, 400 stays invalid request, and neither becomes
    "zero results";
  - shared deadline and bounded call budget, so one tool call cannot monopolize
    the connector.

VERIFICATION STATUS: The wire schema is VERIFIED against MUSE's own OpenAPI spec
(GET /v3/api-docs) and exercised live on 2026-08-21, including negative controls.
Earlier revisions guessed three request field names wrong in a way that returned
HTTP 200 with the intent silently discarded — see the WIRE SCHEMA block. Re-run
`tools/muse_contract_check.py` after any change there; the guesses were only
detectable through a control that asserts an impossible condition returns zero.

Response parsing remains partly inferred: `cards`, `totalHits`, `queryId`,
`searchStrategy`, `totalHitsKnn` and `synonyms` are confirmed present, but per-card
field names are still probed defensively.

Environment:
  ICS_MUSE_BASE_URL       default https://qa.muse.merck.com
  ICS_MUSE_SESSION_FILE   default $XDG_CONFIG_HOME/ics-companion/secrets/muse-qa-session
  ICS_MUSE_PROJECT        MUSE project, default `muse`; datasources are
                          project-scoped, and a program's scope YAML may override
  ICS_PROGRAM             active program key, e.g. MK-6070
  ICS_PROGRAM_SCOPE_DIR   default <workspace>/config/program-scope
  ICS_MUSE_CALL_BUDGET    max upstream calls per server LIFETIME (default 500)
  ICS_MUSE_DEADLINE_S     wall-clock budget in seconds for ONE TOOL CALL (default 900), reset
                          at every entry point by `_begin_call`. It bounded the process until
                          that was fixed, which refused every call after 15 minutes uptime
  ICS_MUSE_WARM_DEADLINE_S  cap on the eager startup warm (default 120); separate from
                          DEADLINE_S because the warm runs before the stdio loop starts
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import sys
import time
import uuid
import zlib
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
import yaml
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

mcp = FastMCP("ics-muse")


# Every tool here is read-only retrieval or scope description. The annotation is
# load-bearing, not decoration: the host adds an MCP tool to its human-approval
# interrupt map unless the tool declares literal `readOnlyHint=true` with no
# destructive hint. Without it, every program search prompts for approval —
# unusable for retrieval-heavy scientific work, and it degrades the approval
# gate for the operations that genuinely need it.
#
# If an authoritative-effect operation is ever added to this connector it MUST
# NOT carry these annotations: effects belong in a separately credentialed
# surface and must stay fail-closed so the host gates them.
_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    openWorldHint=True,
)

BASE = os.environ.get("ICS_MUSE_BASE_URL", "https://qa.muse.merck.com").rstrip("/")
# MUSE scopes datasources per project, so a request without one searches a
# default set. `muse` is the project carrying the five program sources this
# workspace uses; a program in a different MUSE project overrides it in that
# program's scope YAML (`muse_project:`) or through the environment.
PROJECT = os.environ.get("ICS_MUSE_PROJECT", "muse").strip()
PROGRAM = os.environ.get("ICS_PROGRAM", "").strip()
# `X-Client-App` is declared required on 115/115 operations in MUSE's OpenAPI spec.
# On /v2/query it is a RULE CONDITION, not logging: `enterprise-search` enables a
# request-cancelling rule. Send our own identity, never another app's.
CLIENT_APP = "ics-program-companion"
# 500, not 60. The 60 was unsourced: it arrived in this repo's first commit and was
# copied forward unchanged, and the only justification ever written down was a short-lived
# authentication credential. That argues for DEADLINE_S, a *time* bound, and derives no count.
# At 60, the startup cache alone is 14 calls, leaving roughly four searches per session
# across five sources.
CALL_BUDGET = int(os.environ.get("ICS_MUSE_CALL_BUDGET", "500"))
DEADLINE_S = float(os.environ.get("ICS_MUSE_DEADLINE_S", "900"))

# --------------------------------------------------------------------------
# WIRE SCHEMA  (verified against the live OpenAPI spec, 2026-08-21)
# --------------------------------------------------------------------------
# Source of truth: GET {BASE}/v3/api-docs — MUSE publishes its full OpenAPI 3.1
# spec endpoint. Schemas used here:
# `BasicQueryRequest`, `ConditionalQueryRequest`, `ApiConditionQuery`,
# `ApiCondition`, `ApiTextWithSynonyms`.
#
# These names were previously GUESSED, and three of the guesses were wrong in a
# way that produced HTTP 200 with silently-ignored intent:
#   `datasources`  -> MUSE ignores it and searches a DEFAULT source set, so
#                     results came from unknown sources under a requested label.
#   `searchMode`   -> not a MUSE field at all; the real one is `queryType`.
#   conditionQuery -> `{must/should/mustNot}` has no `conditions` key, so MUSE
#                     applied NO conditions and returned the whole population.
# Verify with `tools/muse_contract_check.py`, which asserts an impossible
# condition drives totalHits to 0. Without that control these failures are
# invisible: every one of them returns 200 with plausible records.
EP_QUERY = "/resources/v2/query"
EP_CONDITIONAL = "/resources/v2/query/conditional"
EP_FACETS = "/resources/v1/facets"

# Request field names
F_QUERY = "query"
F_PROJECT = "project"
F_DATASOURCES = "dataSources"      # REQUIRED, camelCase; lowercase is ignored
F_MODE = "queryType"               # see MODE_* maps below
F_LIMIT = "limit"
F_OFFSET = "offset"
F_FIELDS = "fieldsToExtract"
F_FORM_PARAMS = "formParams"       # exact metadata post-filters (no relevance effect)
F_CONDITION = "conditionQuery"     # {"conditions": [ApiCondition, ...]}

# `queryType` enums, per endpoint. The model-facing vocabulary stays
# keyword/knn/hybrid; these are the wire values.
MODE_BASIC = {"keyword": "basic", "knn": "basic_knn", "hybrid": "basic_hybrid"}
MODE_CONDITIONAL = {
    "keyword": "conditional",
    "knn": "conditional_knn",
    "hybrid": "conditional_hybrid",
}

# Condition types, per `ApiCondition.type`.
COND_TYPES = {"must": "MUST", "should": "SHOULD", "must_not": "MUST_NOT"}

# Response field names
R_RESULTS = ("cards", "results", "hits", "documents")
R_TOTAL = ("totalHits", "total", "hitCount")
R_FACETS = ("facets", "aggregations")
# MUSE does not echo `queryType`. It reports the strategies it actually applied,
# and a separate KNN hit count when a semantic lane participated. Those are the
# only honest effective-mode signals; the previous `effectiveSearchMode` read was
# of a field that does not exist.
R_STRATEGY = ("searchStrategy",)
R_TOTAL_KNN = ("totalHitsKnn",)

TIMEOUT = httpx.Timeout(60.0, connect=10.0)


def _verify() -> Any:
    """TLS verification source.

    Internal hosts are typically re-signed by a corporate TLS-inspecting proxy
    whose root is in the OS trust store but not in certifi's bundle. Prefer the OS
    trust store; allow an explicit bundle override. Verification is never
    disabled — a TLS failure is reported as its own failure reason.
    """
    bundle = os.environ.get("ICS_CA_BUNDLE", "").strip()
    if bundle and os.path.isfile(bundle):
        return bundle
    try:
        import ssl

        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # noqa: BLE001 - fall back to certifi
        return True


_TLS_GUIDANCE = (
    "TLS certificate verification failed. This host is likely re-signed by a "
    "corporate TLS-inspecting proxy whose root is in the OS trust store but not "
    "in certifi's bundle. Install `truststore` in the connector environment, or "
    "set ICS_CA_BUNDLE to a PEM bundle containing the internal root. Do NOT "
    "disable verification. This is a CONNECTOR failure — it establishes nothing "
    "about the existence or absence of records."
)

_started = time.monotonic()
_calls_used = 0


def _begin_call() -> None:
    """Start a fresh wall-clock deadline. `DEADLINE_S` bounds ONE tool call, not the process.

    It used to bound the process, because `_started` was set at import and never reassigned.
    `_request`'s deadline check then began refusing every call once the server had been alive
    `DEADLINE_S` seconds — permanently, until restart, regardless of how little work was asked
    for. Found in use, not by a test: six consecutive `muse_population` calls returned
    `status: "not_searched"`, `reason: "deadline_exceeded"`, with no outbound HTTP request at
    all, and no amount of waiting recovered it.

    It read as an authentication problem because the default is 900 s.
    It is not, and the ORDERING is the diagnostic: the deadline check in `_request` runs BEFORE
    `_session_cookie()`, and an authentication failure has its own distinct
    `authentication_failure` reason. A caller who sees `deadline_exceeded` never got as far
    as loading the cookie.

    Every harness set `_started` by hand in its own fixture, which is exactly why no harness
    could see this. `contract_map_check.py` section 4 had already recorded the branch as
    unreached.

    Idempotent, and called at EVERY entry point: both MCP handlers before their lazy
    `warm_cache()` — that warm issues 14 calls when the eager startup one was abandoned, and
    `WARM_DEADLINE_S` wraps only the eager path — and both tool bodies, which is where offline
    harnesses drive it. `CALL_BUDGET` is deliberately NOT reset here: it bounds the process
    while this bounds one call, and that pairing is the point. Resetting both would leave
    nothing bounding a long-lived server at all.
    """
    global _started
    _started = time.monotonic()


def _workspace_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _session_cookie() -> tuple[str | None, str | None]:
    """Return (SESSION value, error_reason)."""
    default = (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        / "ics-companion" / "secrets" / "muse-qa-session"
    )
    path = Path(os.environ.get("ICS_MUSE_SESSION_FILE", str(default)))
    if not path.is_file():
        return None, (
            f"No MUSE SESSION cookie at {path}. Follow auth/README.md from the "
            "product-companion repository root."
        )
    try:
        session = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return None, f"Could not read MUSE SESSION cookie file: {exc}"
    if not session:
        return None, f"MUSE SESSION cookie file {path} is empty."
    return session, None


# --------------------------------------------------------------------------
# Program scope — injected, non-removable
# --------------------------------------------------------------------------

def _scope_dir() -> Path:
    env = os.environ.get("ICS_PROGRAM_SCOPE_DIR", "").strip()
    return Path(env) if env else _workspace_root() / "config" / "program-scope"


def _load_scope(program: str | None = None) -> tuple[dict[str, Any] | None, str | None]:
    prog = (program or PROGRAM).strip()
    if not prog:
        return None, (
            "No active program. Set ICS_PROGRAM or pass `program`. Retrieval "
            "cannot proceed without a program boundary."
        )
    path = _scope_dir() / f"{prog}.yaml"
    if not path.is_file():
        return None, (
            f"No program-scope mapping for '{prog}' at {path}. A source cannot be "
            "searched without a validated program-filter mapping."
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        return None, f"Could not parse program-scope mapping {path}: {exc}"
    return data, None


def _scopeable_sources(scope: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out = {}
    for name, cfg in (scope.get("sources") or {}).items():
        if isinstance(cfg, dict) and cfg.get("program_filter_field") and cfg.get("enabled", True):
            out[name] = cfg
    return out


def _project(scope: dict[str, Any]) -> str:
    """MUSE project for this program.

    Datasources are project-scoped, so a request without a project searches a
    default set. Program config wins over the environment default, because a
    program may live in a different MUSE project.
    """
    return str(scope.get("muse_project") or PROJECT or "").strip()


def _program_filters(source_cfg: dict[str, Any]) -> dict[str, Any]:
    """Exact metadata post-filters for one source.

    Post-filters are used deliberately: they do not contribute to relevance
    scoring, so program isolation never depends on a relevance-bearing clause.
    """
    field = source_cfg["program_filter_field"]
    values = source_cfg.get("program_filter_values") or []
    if isinstance(values, str):
        values = [values]
    return {field: values}


# --------------------------------------------------------------------------
# HTTP with budget and deadline
# --------------------------------------------------------------------------

class BudgetExhausted(Exception):
    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(kind)


async def _request(
    method: str,
    endpoint: str,
    *,
    payload: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call MUSE, returning a normalized envelope (never raises for HTTP).

    Generalized from POST-only because the startup cache is mostly GET: of its 14
    calls, 9 are GET (`/v2/configuration`, `/v3/ingest/.../{ds}` x5,
    `/v1/data-updates`, `/v1/user-access-check`, `/v1/taxonomy`) and 5 are POST
    (`/v1/facets`, one per source).

    The envelope and its failure taxonomy are unchanged and deliberately so. Both
    were measured correct on 2026-08-25: all three config endpoints return 401 for a
    invalid cookie AND for a missing Cookie header, so mapping 401 to
    `authentication_failure` rather than to zero results is right. (`/v1/version` and
    `/v1/health` return 200 in both cases, which is why neither may be used as an
    auth probe.)

    `X-Client-App` is now sent. It is `required: true` on 115/115 operations in MUSE's
    own OpenAPI spec and this connector never sent it. Measured inert on the three
    config endpoints — byte-identical with it, without it, and set to
    `enterprise-search` — but on `/v2/query` it is a *rule condition*, not logging, and
    `enterprise-search` there enables a request-cancelling rule. So send our own
    identity everywhere rather than relying on per-endpoint inertness.
    """
    global _calls_used
    if _calls_used >= CALL_BUDGET:
        raise BudgetExhausted("call_budget_exhausted")
    if time.monotonic() - _started > DEADLINE_S:
        raise BudgetExhausted("deadline_exceeded")

    session, err = _session_cookie()
    if err:
        return {"_ok": False, "_status": {
            "executed": False, "reason": "authentication_failure", "detail": err}}

    request_id = str(uuid.uuid4())
    _calls_used += 1
    url = f"{BASE}{endpoint}"
    headers = {
        "Cookie": f"SESSION={session}",
        "Accept": "application/json",
        "X-Request-ID": request_id,
        "X-Client-App": CLIENT_APP,
        "User-Agent": "ics-program-companion/1.0",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, verify=_verify()) as client:
            resp = await client.request(
                method.upper(), url,
                json=payload if payload is not None else None,
                params=params or None,
                headers=headers,
            )
    except httpx.TimeoutException:
        return {"_ok": False, "_status": {
            "executed": False, "reason": "timeout",
            "request_id": request_id, "endpoint": endpoint}}
    except httpx.ConnectError as exc:
        text = str(exc)
        tls = "CERTIFICATE_VERIFY_FAILED" in text or "SSL" in text
        return {"_ok": False, "_status": {
            "executed": False,
            "reason": "tls_verification_failure" if tls else "connector_failure",
            "detail": _TLS_GUIDANCE if tls else text[:300],
            "request_id": request_id, "endpoint": endpoint,
            "absence_meaning": (
                "The request never reached MUSE. This establishes NOTHING about "
                "whether records exist."
            )}}
    except httpx.HTTPError as exc:
        return {"_ok": False, "_status": {
            "executed": False, "reason": "connector_failure",
            "detail": str(exc)[:300], "request_id": request_id}}

    trace = {
        "endpoint": endpoint,
        "request_id": request_id,
        "muse_request_id": resp.headers.get("X-Request-ID"),
        "http_status": resp.status_code,
        "calls_used": _calls_used,
        "calls_remaining": max(0, CALL_BUDGET - _calls_used),
        # Remaining in THIS TOOL CALL. `_begin_call` resets `_started` at every entry point, so
        # this is a per-call figure; before the fix it was process uptime, and it counted down to
        # zero once and stayed there while every call was refused before reaching the network.
        "seconds_remaining": round(max(0.0, DEADLINE_S - (time.monotonic() - _started)), 1),
    }

    if resp.status_code == 401:
        return {"_ok": False, "_status": {
            **trace, "executed": False, "reason": "authentication_failure",
            "detail": (
                "HTTP 401. This is an AUTHENTICATION FAILURE, not zero results. "
                "Run `python3 auth/muse_session.py check`. If the SESSION is "
                "active, retry the request; if it is expired, run "
                "`python3 auth/muse_session.py capture`. See auth/README.md."
            )}}
    if resp.status_code == 403:
        return {"_ok": False, "_status": {
            **trace, "executed": False, "reason": "restricted_or_forbidden"}}
    if resp.status_code == 400:
        return {"_ok": False, "_status": {
            **trace, "executed": False, "reason": "invalid_query_or_filter",
            "detail": (
                "HTTP 400 — malformed request. This is client-side request "
                "construction, NOT demonstrated retrieval failure and NOT a valid "
                "empty result."
            ),
            "source_response": resp.text[:400]}}
    if resp.status_code == 429:
        return {"_ok": False, "_status": {
            **trace, "executed": False, "reason": "throttled_or_quota_exhausted"}}
    if resp.status_code >= 500:
        return {"_ok": False, "_status": {
            **trace, "executed": False, "reason": "source_failure",
            "source_response": resp.text[:400]}}
    if resp.status_code != 200:
        return {"_ok": False, "_status": {
            **trace, "executed": False, "reason": "unexpected_status",
            "source_response": resp.text[:400]}}

    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError):
        return {"_ok": False, "_status": {
            **trace, "executed": False, "reason": "connector_failure",
            "detail": "response was not JSON", "source_response": resp.text[:300]}}
    return {"_ok": True, "_status": {**trace, "executed": True}, "_body": body}


async def _post(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST to MUSE. Thin wrapper over `_request`.

    Kept as its own name because the five existing tools call it, and because
    delegating rather than duplicating keeps ONE `_calls_used` counter. Two counters
    against one budget would silently double the effective allowance.
    """
    return await _request("POST", endpoint, payload=payload)


async def _get(endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """GET from MUSE. Thin wrapper over `_request`."""
    return await _request("GET", endpoint, params=params)


def _first(body: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if isinstance(body, dict) and name in body:
            return body[name]
    return None


# --------------------------------------------------------------------------
# Startup cache  (group BUILD wave 1)
# --------------------------------------------------------------------------
# Fourteen calls, 9 GET + 5 POST, loaded once per process. Three of them answer
# questions that NOTHING else can answer, which is why this exists at all:
#
#   GET /v3/ingest/{p}/data-source/{ds}   the AUTHORITATIVE field list, `dynamic: strict`
#       so the mapping is complete rather than a sample. Measured: 19 of the 27
#       `searchable_fields` in the scope YAML do not exist, `indexed_text` exists on no
#       source, and a condition on a nonexistent field returns 0 at HTTP 200. Also the
#       only source of per-field `facetType`, `.raw` presence, `highlightable`, and
#       `knnSettings`.
#
#   POST /v1/facets, unfiltered, per source   the COMPUTED facet set. This is the
#       poison-rule predicate and it appears in NO readable configuration: a `formParams`
#       filter on a field the endpoint computes no facet for silently zeroes the whole
#       distribution. On meds the program filter field is exactly such a field.
#
#   GET /v1/user-access-check   SEARCHABILITY. Nothing else can say it. /v2/configuration
#       lists a DENIED source with `hidden: False`, /v3/ingest serves that source's full
#       schema at HTTP 200, and a denied source's search returns 0 with `datasourceErrors`
#       empty -- indistinguishable from absence. Keep ONLY the `access` map: the rest of
#       that response is the caller's department, location, postal code and ISID.
#       Do NOT substitute GET /v1/acl: it reports `accessible: false` for meds, a source
#       that returns 1,014 records, because it answers ACL-GROUP MEMBERSHIP in the
#       source's own ACL system, not searchability.
#
# Measured 2026-08-25. Full dive incl. the degradation path per call:
#   <hub>/projects/ddt-program-companion/work/design/model-facing-tools/
#     07-startup-cache-endpoints.md

EP_CONFIG = "/resources/v2/configuration"
EP_INGEST = "/resources/v3/ingest/{project}/data-source/{ds}"
EP_UPDATES = "/resources/v1/data-updates"
EP_ACCESS = "/resources/v1/user-access-check"
EP_TAXONOMY = "/resources/v1/taxonomy"

# Field ROLES, resolved per source against the real mapping. Roles are stable across
# sources while field names are not. A role a source cannot fill is reported UNFILLABLE,
# never substituted with the nearest-looking field -- `scited` has no body field and no
# status field, and conferring one would be conferring a state the source does not
# represent.
_ROLE_CANDIDATES: dict[str, tuple[str, ...]] = {
    "anywhere": ("_all",),
    "title": ("title",),
    "body": ("text",),
    "status": ("doc_status", "activity_status", "experiment_status"),
}

# An epoch-zero `data-updates` date means NEVER INDEXED, not "last updated in 1970".
# Formatting it would produce a plausible wrong answer.
#
# A prefix test, not a parse, and the limit is deliberate: an epoch rendered in a
# behind-UTC zone would read `1969-12-31T19:00:00` and slip past. Acceptable because every
# one of the 28 observed timestamps is naive with no offset, i.e. already rendered in one
# unstated zone; a `1969` date would be as visibly wrong as a `1970` one. Revisit if MUSE
# ever starts emitting offsets.
_EPOCH_PREFIX = "1970-01-01"

_CACHE: dict[str, Any] | None = None

# A wall-clock cap for the warm specifically, separate from DEADLINE_S. Without it the
# worst case is 14 sequential calls at the 60s per-call TIMEOUT -- ~840s, all of which
# pass the DEADLINE_S check because that is evaluated BEFORE each call. Eager warming
# happens before `mcp.run()`, so that is 840s of dead air during MCP init: the server
# fails by hanging rather than by raising, which is worse than either. Observed warm
# time is a few seconds.
WARM_DEADLINE_S = float(os.environ.get("ICS_MUSE_WARM_DEADLINE_S", "120"))

# Failures that a human can clear without restarting the process -- above all replacing an
# expired SESSION. A cache condemned by one of these must NOT be persisted, or the connector
# reports `searchable: False` for everything until the host restarts the subprocess,
# which is a plausible wrong answer with no timestamp to hint that it is stale.
_TRANSIENT_FAILURES = frozenset({
    "authentication_failure", "timeout", "throttled_or_quota_exhausted",
    "source_failure", "connector_failure", "tls_verification_failure",
})


def _persist(cache: dict[str, Any]) -> dict[str, Any]:
    """Store the cache unless its fatal failure is one a human can clear.

    The single exit point for `warm_cache`, so no early return can accidentally pin a
    expired-session cache for the life of the process.
    """
    global _CACHE
    retryable = any(d["fatal"] and d["reason"] in _TRANSIENT_FAILURES
                    for d in cache["degraded"])
    cache["retryable"] = retryable
    _CACHE = None if retryable else cache
    return cache


def _lpath(chain: list[str]) -> str:
    """Encode a UUID chain as MUSE's taxonomy path: `L<depth>|<uuid>|<uuid>...`.

    Depth is measured BELOW the branch root, so a top-level branch is `L0|<uuid>` and
    its child is `L1|<branch>|<child>`. Measured form (`04-05` §6A); the human-readable
    name returns 0 as a condition value, so the encoded path is the only usable one.
    """
    return f"L{len(chain) - 1}|" + "|".join(chain)


def _walk_taxonomy(
    children: dict[str, Any],
    chain: list[str],
    uuid_to_path: dict[str, tuple[str, ...]],
    label_to_lpath: dict[str, str],
    label_to_branch: dict[str, str],
    labels: list[str],
) -> None:
    """Flatten MMD into two lookups: uuid -> readable path, label -> encoded L-path.

    Shape per `B3-taxonomy.md`: `{"root": {"children": {...}}}`, where `children` is an
    OBJECT KEYED BY DISPLAY LABEL (not an array), and each node is
    `{"id": <uuid>, "name": <str>}` with optional `synonyms` and `children`. `name` is
    byte-identical to its key in all 2,818 nodes, so either may be read.

    `label_to_branch` records which top-level branch a label sits under. Field-to-branch
    must be matched by ROOT UUID, never by name: four of the ten branch names do not
    match the field they belong to.
    """
    if not isinstance(children, dict):
        return
    for label, node in children.items():
        if not isinstance(node, dict):
            continue
        uid = str(node.get("id") or "")
        if not uid:
            continue
        here = [*chain, uid]
        here_labels = [*labels, str(label)]
        uuid_to_path[uid] = tuple(here_labels)
        label_to_lpath[str(label)] = _lpath(here)
        label_to_branch[str(label)] = here[0]
        _walk_taxonomy(node.get("children") or {}, here, uuid_to_path,
                       label_to_lpath, label_to_branch, here_labels)


def _branch_uuid_of_facet(terms: Any) -> str | None:
    """Read the branch root UUID out of a taxonomy facet's own values.

    Every value of a taxonomy facet is an L-path whose first UUID is that branch's root,
    and each taxonomy field carries exactly ONE root -- measured 10 of 10. So the
    field-to-branch mapping is derivable from the unfiltered facets call we already make,
    with no extra request and no name matching.
    """
    if not isinstance(terms, list):
        return None
    roots = set()
    for entry in terms:
        term = entry.get("term") if isinstance(entry, dict) else None
        if isinstance(term, str) and term.startswith("L") and "|" in term:
            roots.add(term.split("|")[1])
    return next(iter(roots)) if len(roots) == 1 else None


async def warm_cache(program: str | None = None, *, force: bool = False) -> dict[str, Any]:
    """Load the startup cache. Idempotent per process unless `force`.

    Never raises for an upstream failure. Every failure lands in `degraded` with a stated
    reason, and `ok` is False only when the cache cannot support correct behaviour at all.
    A hard stop is a stated scientific limitation, not an empty result.
    """
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE

    scope, scope_err = _load_scope(program)
    cache: dict[str, Any] = {
        "loaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "program": (scope or {}).get("program") if scope else (program or PROGRAM),
        "project": _project(scope) if scope else PROJECT,
        "ok": True,
        "degraded": [],
        "calls": {},
        "sources": {},
        "facet_defs": {},
        "taxonomy": {"uuid_to_path": {}, "label_to_lpath": {}, "label_to_branch": {},
                     # Initialised, not created lazily: a shape that appears only when a
                     # branch happens to resolve makes every reader write `.get(...) or {}`.
                     "field_to_branch": {}, "ambiguous_labels": [],
                     "synonyms": {}, "loaded": False},
    }

    def fail(key: str, reason: str, *, fatal: bool) -> None:
        cache["degraded"].append({"call": key, "reason": reason, "fatal": fatal})
        if fatal:
            cache["ok"] = False

    if scope_err:
        fail("program_scope", scope_err, fatal=True)
        return _persist(cache)

    proj = cache["project"]
    if not proj:
        # A request with no project searches a DEFAULT source set, and `/v3/ingest`'s path
        # would become `/data-source//meds`. Both are the silent-wrong-scope failure this
        # connector exists to prevent, so refuse rather than send.
        fail("program_scope", "no MUSE project resolved; a request without one searches a "
                              "default source set", fatal=True)
        return _persist(cache)
    scopeable = _scopeable_sources(scope)
    # Recorded so "absent from `sources`" can be told apart from "never in scope" -- the
    # budget-exhaustion case would otherwise be indistinguishable from a scope decision.
    cache["in_scope"] = in_scope = list(scopeable)

    try:
        # 1. data-updates FIRST. 2,244 B, and it is the cheapest auth gate: it returns
        #    401 for an invalid SESSION and for a missing Cookie header alike. /version and
        #    /health return 200 in both cases and must never be used for this.
        env = await _get(EP_UPDATES)
        cache["calls"]["data_updates"] = env["_status"]
        freshness: dict[str, dict[str, Any]] = {}
        # Whether the call landed at all. Without this, a timeout gives every source the
        # "no entry in data-updates" default -- which asserts `absent: True` for sources
        # that DO have an entry, and `never_indexed: False` for a fact nobody read. The
        # per-source default below branches on it so an unread call says UNREAD, not
        # ABSENT. Two different things, and only one of them is a finding about the source.
        cache["updates_read"] = env["_ok"]
        if not env["_ok"]:
            reason = env["_status"].get("reason", "unknown")
            fail("data_updates", reason, fatal=reason == "authentication_failure")
            if reason == "authentication_failure":
                # STOP HERE. This is the entire reason data-updates is called first: it
                # is 2,244 B and it 401s honestly. Continuing would spend 13 more calls
                # that cannot succeed, on every startup with an expired SESSION. A 401 from
                # this endpoint while others might work is not a case that exists --
                # /v2/configuration and /v3/ingest 401 identically.
                return _persist(cache)
        else:
            # A flat list of 28 entries, each exactly {id, type, date}. It does NOT
            # match the 25 configured sources: three configured sources have no entry,
            # and six entries are not sources at all.
            # `_parse_updates` -- the SAME function `refresh_freshness` uses. This was an
            # inline copy, and the extracted function's docstring claimed two callers while having
            # one, so the two could drift with nothing noticing. A per-call re-read that parsed
            # differently from startup is a difference the model would read as index movement.
            freshness = _parse_updates(env["_body"])

        # 2. user-access-check. One call, every source. Keep ONLY `access`.
        # NOTE: `cache["calls"]` retains each call's `_status`, which on 400/5xx/non-200
        # carries up to 400 chars of the upstream body (`source_response`). `cache_status()`
        # deliberately omits `calls` for that reason. A later wave that surfaces `calls` in
        # a tool response would be emitting upstream error text to the model -- don't.
        env = await _get(EP_ACCESS, {"project": proj})
        cache["calls"]["user_access_check"] = env["_status"]
        access: dict[str, str] = {}
        if not env["_ok"]:
            # Fatal, and it returns immediately -- the same treatment `configuration`
            # gets. Without this map a DENIED source's zero is indistinguishable from
            # absence, which is the failure this whole surface exists to remove, so the
            # remaining 11 calls would be building a cache already condemned.
            fail("user_access_check", env["_status"].get("reason", "unknown"), fatal=True)
            return _persist(cache)
        got = env["_body"].get("access") if isinstance(env["_body"], dict) else None
        if isinstance(got, dict):
            access = {str(k): str(v) for k, v in got.items()}
        else:
            fail("user_access_check", "HTTP 200 but the response carried no `access` map",
                 fatal=True)
            return _persist(cache)

        # 3. /v2/configuration. Locator templates, readability, the 186 facet
        #    definitions. Fatal on failure: without it nothing can be cited and no read
        #    can be pre-filtered.
        env = await _get(EP_CONFIG, {"project": proj} if proj else None)
        cache["calls"]["configuration"] = env["_status"]
        if not env["_ok"]:
            fail("configuration", env["_status"].get("reason", "unknown"), fatal=True)
            return _persist(cache)
        cfg = env["_body"] if isinstance(env["_body"], dict) else {}
        # Byte-stable across calls and carries no etag or last-modified, so a content
        # hash is the ONLY drift signal available.
        cache["config_fingerprint"] = hashlib.sha256(
            json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:16]
        # The join key is the BARE field name. Sources declare `<field>.raw`; these
        # definitions key on `<field>`, so an as-declared join resolves 0 of 186. Used
        # for `valueLabels` and `primary` only -- `facetType` comes from /v3/ingest.
        for fdef in cfg.get("facets") or []:
            if isinstance(fdef, dict) and fdef.get("id"):
                cache["facet_defs"][str(fdef["id"])] = {
                    "label": fdef.get("label"),
                    "type": fdef.get("type"),
                    "primary": bool(fdef.get("primary")),
                    "value_labels": fdef.get("valueLabels"),
                    "value_order": fdef.get("valueOrder"),
                }
        # Project-wide KNN configuration. `knn_disabled_datasources` agrees exactly with
        # per-source `knnSettings` -- verified on all 8 implied-enabled sources -- and
        # gives it in one call instead of five.
        # PRESENT, not merely non-empty. `knn` is derived from this subtree, and without the
        # distinction an absent or renamed `projectKnnSettings` on a 200 made every source read
        # `knn: True` -- accepting `semantic=True` on three sources measured to have no vector
        # support. Every sibling capability field carries `None` for exactly this case.
        knn_settings_absent = not isinstance(
            (cfg.get("customization") or {}).get("projectKnnSettings"), dict)
        knn_cfg = (cfg.get("customization") or {}).get("projectKnnSettings") or {}
        knn_disabled = set(knn_cfg.get("knn_disabled_datasources") or [])
        cache["knn_project"] = {
            "embedding_model": knn_cfg.get("embedding_model"),
            "dimension": knn_cfg.get("dimension"),
            # The SOURCE field that gets vectorized -- NOT the index field the knn
            # clause demands, which appears in no mapping and is still unanswered.
            "vectorized_source_fields": knn_cfg.get("long_text_vectorized_fields"),
            "disabled_datasources": sorted(knn_disabled),
        }
        by_id = {str(d.get("id")): d for d in (cfg.get("dataSources") or [])
                 if isinstance(d, dict) and d.get("id")}

        # 4. /v3/ingest, one per in-scope source. A bogus name 404s only its own call,
        #    so one bad id cannot poison the others.
        for name in in_scope:
            cfg_entry = by_id.get(name) or {}
            src_cfg = scopeable[name]
            card = cfg_entry.get("cardModel") or {}
            src: dict[str, Any] = {
                "id": name,
                "title": cfg_entry.get("title"),
                "category": cfg_entry.get("category"),
                "listed_in_configuration": name in by_id,
                # `cardModel` is TOP-LEVEL here, with no `uiDetail` wrapper -- that path
                # belongs to a different endpoint. The template names the field that must
                # be requested, or `body.link` comes back uninterpolated.
                "locator_template": card.get("link"),
                "card_default_fields": card.get("fields"),
                # Absent or null must NEVER read as True: one unsupported document
                # returns HTTP 403 for the WHOLE read batch.
                "readable": cfg_entry.get("fullDocumentTextRetrievalEnabled") is True,
                "readable_declared": cfg_entry.get("fullDocumentTextRetrievalEnabled"),
                "max_export_results": cfg_entry.get("maxExportResults"),
                "declared_facets": cfg_entry.get("facets") or [],
                "advanced_search_model": cfg_entry.get("advancedSearchModel"),
                # Presence in the configuration is NOT searchability.
                "searchable": access.get(name) == "ALLOWED",
                "access": access.get(name),
                # An unread call and a source with no entry are DIFFERENT states, and
                # only the second is a fact about the source. `None` means unknown
                # throughout: never `False` for something nobody read.
                "freshness": freshness.get(name, {
                    "date": None, "kind": None,
                    "never_indexed": False if cache["updates_read"] else None,
                    "tz": None,
                    "absent": True if cache["updates_read"] else None,
                    "unread": not cache["updates_read"],
                }),
                # `None` is UNKNOWN, like every sibling capability field. `knn_disabled` comes
                # from `customization.projectKnnSettings`; if that subtree is absent or renamed on
                # a 200, every source used to read `knn: True` and `semantic=True` would be
                # accepted on sources measured to have no vector support.
                "knn": (None if knn_settings_absent else name not in knn_disabled),
                "program_filter": _program_filters(src_cfg),
                "boundary_strength": src_cfg.get("boundary_strength", "exact"),
                "fields": {},
                "roles": {},
                # ALL roles start unfillable and are removed as they resolve. The
                # opposite default -- an empty list -- reads as "nothing is unfillable"
                # on a source whose mapping never arrived, so a later wave doing
                # `if role in unfillable_roles: refuse` would not refuse.
                "unfillable_roles": sorted(_ROLE_CANDIDATES),
                "roles_resolved": False,
                "taxonomy_roles": {},
                # `None` is UNKNOWN. `[]` would assert "this source has no DATE facets",
                # and the DATE guard is the only defence against the one case the
                # facet-to-condition loop LIES about rather than failing.
                "computed_facets": None,
                # `None` is UNKNOWN: the filter field's value list is only obtainable where
                # that field is itself facetable, so absent here means "could not check the
                # stored literal", never "the literal is absent".
                "filter_field_terms": None,
                "date_facets": None,
                "facetable_program_filter": None,
                # Two different facts. `mapping_retrieved` False means nothing is known;
                # `mapping_complete` False with it True means the mapping arrived but
                # `dynamic` is not strict, so validation is best-effort. Collapsing them
                # would let an ingest-404 source pass EVERY field name as "best-effort".
                "mapping_retrieved": False,
                "mapping_complete": None,
                "field_to_branch": {},
            }
            env = await _get(EP_INGEST.format(project=proj, ds=name))
            cache["calls"][f"ingest:{name}"] = env["_status"]
            if not env["_ok"]:
                reason = env["_status"].get("reason", "unknown")
                # 403 here means a bad project, which is fatal for every source.
                fail(f"ingest:{name}", reason, fatal=reason == "restricted_or_forbidden")
                src["degraded"] = reason
                cache["sources"][name] = src
                continue
            body = env["_body"] if isinstance(env["_body"], dict) else {}
            doc = ((body.get("mappings") or {}).get("document")) or {}
            props = doc.get("properties") or {}
            # `dynamic: strict` is what makes this a COMPLETE field list rather than a
            # sample, which is what lets the clause compiler refuse an unknown field
            # instead of sending it and getting a silent zero.
            src["mapping_retrieved"] = True
            src["mapping_complete"] = doc.get("dynamic") == "strict"
            if not src["mapping_complete"]:
                fail(f"ingest:{name}",
                     f"mappings.document.dynamic is {doc.get('dynamic')!r}, not 'strict'"
                     " -- field validation is best-effort for this source", fatal=False)
            field_meta = {str(f.get("id")): f for f in (body.get("fields") or [])
                          if isinstance(f, dict) and f.get("id")}
            for fname, fspec in props.items():
                meta = field_meta.get(str(fname), {})
                src["fields"][str(fname)] = {
                    "type": (fspec or {}).get("type"),
                    # `match: "exact"` appends `.raw`; refuse when the field has none.
                    "has_raw": "raw" in ((fspec or {}).get("fields") or {}),
                    "facetable": bool(meta.get("facetField")),
                    # facetType for EVERY type, not just TAXONOMY -- so the DATE guard
                    # needs no join against /v2/configuration.
                    "facet_type": meta.get("facetType"),
                    "highlightable": bool(meta.get("highlightable")),
                    "restricted": bool(meta.get("restricted")),
                }
            # `_all` is resolved from the mapping like every other field. It IS declared
            # in `mappings.document.properties` on all five sources (measured
            # 2026-08-25), so an unconditional exemption would have been both unnecessary
            # and a hole in the rule this block exists to enforce -- and a condition on a
            # nonexistent field returns 0 at HTTP 200.
            src["unfillable_roles"] = []
            for role, candidates in _ROLE_CANDIDATES.items():
                hit = [c for c in candidates if c in src["fields"]]
                if hit:
                    src["roles"][role] = hit
                else:
                    src["unfillable_roles"].append(role)
            src["roles_resolved"] = True
            # Taxonomy roles are DERIVED, never hardcoded to ten, so the set
            # self-corrects if MUSE adds a branch or another source gains one.
            for fname, fmeta in src["fields"].items():
                if fmeta["facet_type"] == "TAXONOMY":
                    src["taxonomy_roles"][fname.removeprefix("mmd_tag_")] = fname
            # DATE-typed facets show a rendered year that returns 0 as a condition --
            # the one place the facet-to-condition loop lies instead of failing.
            src["date_facets"] = sorted(f for f, m in src["fields"].items()
                                        if m["facet_type"] == "DATE")
            cache["sources"][name] = src

        # 5. POST /v1/facets, unfiltered, per source. The COMPUTED set.
        for name, src in cache["sources"].items():
            # A source whose mapping never arrived has no known fields, so the poison-rule
            # predicate cannot be computed for it -- `fb in bare` against an empty field
            # set would return a confident answer about nothing. Skip it; its state stays
            # `None` = unknown.
            if not src["mapping_retrieved"]:
                continue
            payload: dict[str, Any] = {F_QUERY: "", F_DATASOURCES: [name]}
            if proj:
                payload[F_PROJECT] = proj
            # `counts` is a DEPTH control, and without it this endpoint returns six terms per
            # field with the `+` overflow bucket. Measured 2026-09-01: the program filter's own
            # literal falls outside that cut on all four faceted sources, so
            # `filter_field_terms` below recorded six terms that never contained it and every
            # zero-diagnosis read the filter as unverified. Two calls differing only in `counts`
            # returned 6 terms and 136/1001/87/926 respectively.
            #
            # Requested for the FILTER FIELD only. `counts` is not a field selector -- naming
            # one field does not narrow the response -- so `computed_facets` still arrives
            # complete and the poison-rule predicate is unaffected.
            filter_field_depth = next(iter(src["program_filter"]), "")
            if filter_field_depth:
                payload["counts"] = {filter_field_depth: _COUNTS_CEILING}
            env = await _post(EP_FACETS, payload)
            cache["calls"][f"facets:{name}"] = env["_status"]
            if not env["_ok"]:
                # Not fatal: records still work, but distributions must be refused here
                # because the poison-rule predicate is unknown.
                fail(f"facets:{name}", env["_status"].get("reason", "unknown"), fatal=False)
                continue
            facets = _first(env["_body"], R_FACETS)
            if not isinstance(facets, dict):
                # HTTP 200 carrying no facet map. `or {}` here would have set
                # `computed_facets: []` -- "this source exposes no facets" -- and a
                # confident `facetable_program_filter: False`, for a response we could
                # not read. Absence of a key is not a value.
                fail(f"facets:{name}",
                     "HTTP 200 but the response carried no facet map; the poison-rule "
                     "predicate is unknown for this source", fatal=False)
                continue
            computed = sorted(facets)
            src["computed_facets"] = computed
            bare = {c[:-4] if c.endswith(".raw") else c for c in computed}
            filter_field = next(iter(src["program_filter"]), "")
            fb = filter_field[:-4] if filter_field.endswith(".raw") else filter_field

            # PREFIX-DERIVED FILTER VALUES, for a source whose programme boundary is a folder
            # path rather than a stored literal. Measured 2026-09-03 on kneat: 70 distinct
            # folder terms begin with a programme alias, one per site and activity --
            # `MK-6070 (GAMV-AMV)`, `MK-6070 (BNX-AMV)`, `MK-6070 (500-DBY-PV)` and 67 more --
            # and a new site folder appears as a NEW TERM. A list stored in the scope YAML would
            # silently stop covering the programme the first time that happened, and the failure
            # would look like the programme having fewer documents rather than like a stale
            # config. So the list is re-derived here, per process, from the field's own values.
            #
            # `searchField` is prefix-only and is NOT subject to the poison rule -- measured, it
            # returns the requested field under the source's own program filter -- so this needs
            # no superset path.
            prefixes = (scopeable.get(name) or {}).get("program_filter_value_prefixes") or []
            if prefixes and filter_field:
                derived: set[str] = set()
                derived_ok = True
                for prefix in prefixes:
                    penv = await _post(EP_FACETS, {
                        F_DATASOURCES: [name], F_PROJECT: proj,
                        "searchField": filter_field,
                        F_FORM_PARAMS: {filter_field: [str(prefix)]},
                        "counts": {filter_field: _COUNTS_CEILING},
                    })
                    cache["calls"][f"filter_prefix:{name}:{prefix}"] = penv["_status"]
                    if not penv["_ok"]:
                        derived_ok = False
                        fail(f"filter_prefix:{name}",
                             f"the prefix search for {prefix!r} did not complete, so this "
                             "source's program filter could not be derived", fatal=False)
                        break
                    pfx_facets = _first(penv["_body"], R_FACETS)
                    if not isinstance(pfx_facets, dict) or filter_field not in pfx_facets:
                        derived_ok = False
                        fail(f"filter_prefix:{name}",
                             f"the prefix search for {prefix!r} returned HTTP 200 carrying no "
                             "value list for the filter field; the derivation is unknown, not "
                             "empty", fatal=False)
                        break
                    for entry in pfx_facets[filter_field]:
                        if isinstance(entry, dict) and entry.get("term"):
                            derived.add(str(entry["term"]))
                # An EMPTY filter must never reach the wire: `{field: []}` is a request with no
                # program scope, and this source's whole population is other programmes'
                # documents. `_plan_*`'s `no_program_filter` refusal catches it, so leaving the
                # list empty excludes the source with a stated reason. Never fall back to the
                # prefix as a literal -- a prefix matches nothing under `match: "exact"`.
                src["program_filter"] = {filter_field: sorted(derived) if derived_ok else []}
                src["filter_values_derived"] = {
                    "prefixes": [str(p) for p in prefixes],
                    "term_count": len(derived) if derived_ok else 0,
                    "complete": derived_ok,
                }
                if derived_ok and not derived:
                    fail(f"filter_prefix:{name}",
                         "no value in this source's filter field begins with any configured "
                         "programme prefix. Either the programme has no folder here or the "
                         "folder naming changed; the source is excluded rather than searched "
                         "unscoped", fatal=False)
            # THE POISON RULE. Filtering on a field the endpoint computes no facet for
            # silently zeroes the whole distribution -- 1 field returned of 35 on meds.
            # Known BEFORE any filtered facet request is sent.
            src["facetable_program_filter"] = fb in bare
            # The filter field's OWN value list, kept rather than discarded. This is the only
            # evidence available that the program filter's stored LITERAL is still live --
            # `01` section 1c names the literal as the fragile part, because a reindex that
            # renames it returns 0 records at HTTP 200 on every source, indistinguishable from
            # the program having no documents. The value list is already in this response; wave
            # 4's zero-diagnosis needs it to say "the filter is live" instead of claiming it.
            #
            # Only available where the filter field is itself facetable, so it is absent on
            # meds -- and there the diagnosis says so rather than asserting the check passed.
            if filter_field in facets and isinstance(facets[filter_field], list):
                src["filter_field_terms"] = {
                    str(t.get("term")): t.get("count")
                    for t in facets[filter_field] if isinstance(t, dict) and t.get("term")}
            # Field-to-branch by ROOT UUID, read out of the taxonomy facets' own values.
            # Never by name: four of ten branch names do not match their field.
            # Kept PER SOURCE, not globally: two sources both carrying `mmd_tag_issue`
            # would collide in a global map and one branch would silently win. Only meds
            # has taxonomy fields today, which is exactly why the collision would go
            # unnoticed if it ever stopped being true.
            for fname in src["taxonomy_roles"].values():
                root = _branch_uuid_of_facet(facets.get(f"{fname}.raw"))
                if root:
                    src["field_to_branch"][fname] = root
                    cache["taxonomy"]["field_to_branch"].setdefault(name, {})[fname] = root
                else:
                    # A taxonomy field whose branch cannot be established cannot be
                    # written to, so say so rather than dropping it silently.
                    fail(f"facets:{name}",
                         f"could not resolve a single branch root for {fname}; "
                         "`where` on that taxonomy role must be refused", fatal=False)

        # 6. taxonomy MMD. Degraded, not broken, when it fails: distributions show raw
        #    L-paths and a `where` on a taxonomy role is refused with the reason.
        env = await _get(EP_TAXONOMY, {"taxonomy-type": "MMD"})
        cache["calls"]["taxonomy"] = env["_status"]
        if not env["_ok"]:
            fail("taxonomy", env["_status"].get("reason", "unknown"), fatal=False)
        else:
            tree = env["_body"] if isinstance(env["_body"], dict) else {}
            root = tree.get("root") or {}
            u2p: dict[str, tuple[str, ...]] = {}
            l2p: dict[str, str] = {}
            l2b: dict[str, str] = {}
            _walk_taxonomy(root.get("children") or {}, [], u2p, l2p, l2b, [])
            syn: dict[str, list[str]] = {}

            def collect_syn(children: Any) -> None:
                if not isinstance(children, dict):
                    return
                for label, node in children.items():
                    if isinstance(node, dict):
                        if node.get("synonyms"):
                            syn[str(label)] = [str(s) for s in node["synonyms"]]
                        collect_syn(node.get("children"))

            collect_syn(root.get("children") or {})
            # DRIFT DETECTION. `label_to_lpath` is last-writer-wins, so a duplicate label
            # would silently encode to whichever branch was walked last -- a well-formed
            # L-path in the WRONG branch, which returns records from another branch or a
            # clean zero and never errors. Measured 2026-08-25: 2,818 nodes, 2,818
            # distinct labels, zero collisions. So this is a guard against drift, not a
            # live condition -- and it is the cheapest possible one, because the two
            # counts are already in hand.
            ambiguous = len(u2p) - len(l2p)
            if ambiguous > 0:
                fail("taxonomy",
                     f"{ambiguous} taxonomy labels are ambiguous (2,818 were distinct "
                     "when measured). Label-to-L-path encoding is unreliable; `where` on "
                     "a taxonomy role must be refused for an ambiguous label", fatal=False)
            cache["taxonomy"].update({
                "uuid_to_path": u2p, "label_to_lpath": l2p,
                "label_to_branch": l2b, "synonyms": syn, "loaded": True,
                "node_count": len(u2p), "distinct_labels": len(l2p),
                "ambiguous_label_count": ambiguous,
            })
            # `label -> (field, L-path)` -- the artifact wave 1 owes, per active-work.
            # Built here rather than left as a join a later wave has to invent: it needs
            # the tree (branch per label) AND the facets pass (branch per field), so this
            # is the only point where both are in hand. Per source, because the field
            # names are per source.
            #
            # Both guards this closes are measured silent zeros: an L-path in the WRONG
            # field returns 0, and an unknown label returns 0. Resolving through the
            # branch UUID makes the first unrepresentable.
            resolve: dict[str, dict[str, tuple[str, str]]] = {}
            for sname, s in cache["sources"].items():
                branch_to_field = {b: f for f, b in (s.get("field_to_branch") or {}).items()}
                per: dict[str, tuple[str, str]] = {}
                for label, lpath in l2p.items():
                    field = branch_to_field.get(l2b.get(label, ""))
                    if field:
                        per[label] = (field, lpath)
                if per:
                    resolve[sname] = per
            cache["taxonomy"]["label_to_field_lpath"] = resolve
    except BudgetExhausted as exc:
        fail("budget", exc.kind, fatal=True)

    return _persist(cache)


def cache_status() -> dict[str, Any]:
    """What the cache can and cannot support. Safe to call before it is warm."""
    if _CACHE is None:
        return {"warm": False, "detail": "startup cache has not been loaded"}
    # `calls` is deliberately NOT surfaced: each entry can carry up to 400 chars of an
    # upstream error body.
    return {
        "warm": True,
        "ok": _CACHE["ok"],
        # True when the failure is one a human can clear (replacing a SESSION cookie, above all), in
        # which case this cache was not persisted and the next warm retries.
        "retryable": _CACHE.get("retryable"),
        "loaded_at": _CACHE["loaded_at"],
        "in_scope": _CACHE.get("in_scope"),
        "degraded": _CACHE["degraded"],
        # False means freshness was never read at all, so a per-source absence of a date
        # is not a finding about that source.
        "index_freshness_read": _CACHE.get("updates_read"),
        "sources": {
            n: {
                "searchable": s["searchable"],
                "access": s["access"],
                "readable": s["readable"],
                "mapped_fields": len(s["fields"]),
                # Two facts, never collapsed: nothing known vs known-but-not-strict.
                "mapping_retrieved": s["mapping_retrieved"],
                "mapping_complete": s["mapping_complete"],
                "roles_resolved": s["roles_resolved"],
                # `None` throughout means UNKNOWN, never "none".
                "computed_facets": (None if s["computed_facets"] is None
                                    else len(s["computed_facets"])),
                "facetable_program_filter": s["facetable_program_filter"],
                "date_facets": s["date_facets"],
                "taxonomy_roles": sorted(s["taxonomy_roles"]),
                "unfillable_roles": s["unfillable_roles"],
                "knn": s["knn"],
                "index_date": s["freshness"].get("date"),
                "index_date_unread": s["freshness"].get("unread"),
                "index_entry_absent": s["freshness"].get("absent"),
            }
            for n, s in _CACHE["sources"].items()
        },
        "facet_definitions": len(_CACHE["facet_defs"]),
        "taxonomy_nodes": _CACHE["taxonomy"].get("node_count", 0),
        "taxonomy_ambiguous_labels": _CACHE["taxonomy"].get("ambiguous_label_count", 0),
        "config_fingerprint": _CACHE.get("config_fingerprint"),
        "calls_used": _calls_used,
    }


# --------------------------------------------------------------------------
# Handles  (group BUILD wave 2)
# --------------------------------------------------------------------------
# Two handle kinds, one mechanism. Both are OPAQUE to the model and both are
# SELF-DESCRIBING rather than server state.
#
# WHY SELF-DESCRIBING. The five tools this surface replaced kept handles in a
# process-memory dict and had to warn "Handle not found (server may have restarted)".
# An encoded handle survives a restart and a `/compact`, which matters concretely:
# `muse_read`'s cold path reads document references out of `ground/` and `artifacts/`
# recorded in an EARLIER SESSION, where no dict entry can possibly exist.
#
# WHY A POPULATION HANDLE AT ALL. Restating a population is where the measured failures
# come from. On meds, four restatements of the same intent return 135, 3,832, 1,014 and
# ~5.9 million records -- every one HTTP 200, every one plausible, and nothing in any
# response says which population it described. A handle carries the recipe so there is
# nothing to restate.
#
# WHY A DOCUMENT HANDLE. `card.id` is the document identifier and `body.document_id` is
# NOT -- passing the wrong one 404s. Wrapping it removes that trap by construction rather
# than by discipline, and keeps the model out of identifier construction entirely.
#
# FIELD NAMES ARE THE DESIGN'S, NOT SHORTER ONES. The payload keys below are verbatim
# from `00-surfaced-tools.md` §4: source, engine, program_filter, conditions, count,
# as_of, index_updated, facetable. An earlier revision of this section shortened them to
# s/e/f/c/n/fac/idx and justified it as "short because they are on the wire every call."
# Measured: that saves 46 characters, 19%. That is an ECONOMY argument, this group carries
# an explicit `ECONOMY -- DEFERRED` rule, and waves 3 to 5 read the design doc alongside
# this file. 46 characters is not a reason to diverge from an established design.
#
# WHAT A HANDLE MAY NOT CONTAIN: no token, no ISID, no caller data. The program filter
# literals are configuration, not secrets, and they are the recipe's core.
#
# `card.id` is OBFUSCATED, NOT PROTECTED. One line recovers it:
# `json.loads(zlib.decompress(base64.urlsafe_b64decode(body + pad)))`. That is sufficient
# for the design's actual intent -- keep the model out of identifier construction so the
# card.id/document_id trap cannot be reached -- and nothing later may rely on more.

_HANDLE_POPULATION = "p1"
_HANDLE_DOCUMENT = "d1"
_HANDLE_CHECK_LEN = 10

# A real population handle is ~285 characters. This bound exists because deflate reaches
# roughly 1032:1, so an unbounded decode turns a 1 MB tool argument into ~1 GB of RSS --
# measured: a 271,812-character handle inflates to 200 MiB in 232 ms and takes peak RSS
# from 267 to 685 MiB, with the refusal arriving from `json.loads` only AFTER zlib has
# materialised it. The connector would then fail by being OOM-killed, which is the same
# "fails by hanging rather than raising" shape the startup warm was capped to avoid.
_HANDLE_MAX_ENCODED = 8192
_HANDLE_MAX_DECODED = 65536

# `index_updated` must be the raw `freshness["date"]` value from the startup cache, which
# is `data-updates`' own naive fixed-width form. It must NOT be a locally formatted stamp:
# `as_of` in this same payload carries a trailing `Z` and would compare wrongly, and a
# date-only value like "2026-08-25" yields a confident WRONG staleness verdict.
_NAIVE_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")


def _handle_encode(kind: str, payload: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return `(handle, error)`. `<kind>.<b64url(deflate(json))>.<checksum>`.

    WHAT THE CHECKSUM ACTUALLY DOES, measured rather than assumed. An earlier revision
    claimed it stops "a truncated or mistyped handle that still base64-decodes" from
    executing a different recipe. That claim is FALSE for this codec: of 14,742
    single-character body substitutions, **zero** decoded to a different recipe -- 14,724
    were rejected by zlib's Adler-32 or the JSON parse, and 18 decoded to the identical
    recipe through trailing-bit aliasing. Of 233 body truncations, zero decoded at all.

    What it measurably does add, and why it stays:

    * `base64.urlsafe_b64decode` SILENTLY DISCARDS characters outside the alphabet, so a
      body with `*!\\n` injected mid-string decodes cleanly to the original recipe. The
      checksum covers `<kind>.<body>` as a STRING, before any decode, so it catches that.
      That ordering is the single most important property here.
    * it produces a distinct, actionable refusal -- "re-run the call that produced it" --
      instead of a generic decode failure.

    It is NOT integrity against an adversary, and the reason is not "no secret survives a
    restart" (this connector already reads a credential from a file). The reason is that there
    is no adversary: the model is a component that may truncate or mistype, not one that
    forges. A hand-crafted handle claiming `count: 999999` is accepted, and that is
    understood rather than defended against.
    """
    try:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    except (TypeError, ValueError) as exc:
        # The decode side is meticulous; without this the encode side had nothing. A
        # `datetime.date` in a scope-YAML value is a plausible way to reach it.
        return None, f"this recipe cannot be encoded ({exc})"
    body = base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode().rstrip("=")
    check = hashlib.sha256(f"{kind}.{body}".encode()).hexdigest()[:_HANDLE_CHECK_LEN]
    handle = f"{kind}.{body}.{check}"
    if len(handle) > _HANDLE_MAX_ENCODED:
        return None, ("this recipe is too large to encode as a handle; narrow the "
                      "population instead")
    return handle, None


def _handle_decode(kind: str, handle: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Return `(payload, error)`. Never raises, never partially decodes."""
    if not isinstance(handle, str) or not handle:
        return None, "a handle must be a non-empty string as returned by a prior call"
    if len(handle) > _HANDLE_MAX_ENCODED:
        # Refused BEFORE decompression, which is the point.
        return None, (f"this handle is {len(handle)} characters, far beyond any handle this "
                      "connector issues; refusing to decode it")
    parts = handle.strip().split(".")
    if len(parts) != 3:
        return None, ("this is not a handle this connector issued (expected three "
                      "dot-separated parts). Handles are opaque: pass one back exactly "
                      "as it was returned, and never construct or edit one")
    got_kind, body, check = parts
    if got_kind != kind:
        known = {_HANDLE_POPULATION: "a population handle",
                 _HANDLE_DOCUMENT: "a document handle"}
        if got_kind not in known:
            return None, (f"this handle carries an unrecognised format tag {got_kind!r}; it "
                          "was probably issued by an older version of this connector. "
                          "Re-run the call that produced it")
        return None, (f"wrong handle kind: expected {known[kind]} but got {known[got_kind]}")
    want = hashlib.sha256(f"{got_kind}.{body}".encode()).hexdigest()[:_HANDLE_CHECK_LEN]
    if check != want:
        return None, ("this handle is corrupted or was altered -- its checksum does not "
                      "match. Re-run the call that produced it rather than repairing it")
    try:
        pad = "=" * (-len(body) % 4)
        # Bounded, so a compression bomb is refused rather than materialised.
        dec = zlib.decompressobj()
        raw = dec.decompress(base64.urlsafe_b64decode(body + pad), _HANDLE_MAX_DECODED)
        if dec.unconsumed_tail:
            return None, ("this handle decompresses to far more than any recipe; refusing "
                          "to decode it")
        payload = json.loads(raw)
    except Exception:  # noqa: BLE001 - any decode failure is the same refusal
        return None, "this handle could not be decoded; re-run the call that produced it"
    if not isinstance(payload, dict):
        return None, "this handle does not contain a recipe"
    return payload, None


# The full key set a population handle is minted with. Requiring ALL of them means a
# caller can read `payload["count"]` without a KeyError, and it means `facetable: None`
# unambiguously says UNKNOWN rather than "the key is missing".
_POPULATION_KEYS = ("source", "engine", "program_filter", "conditions", "count",
                    "facetable", "as_of", "index_updated")


def encode_population_handle(
    *,
    source: str,
    engine: str,
    program_filter: dict[str, Any],
    conditions: list[dict[str, Any]] | None,
    count: int | None,
    facetable: bool | None,
    index_updated: str | None,
) -> tuple[str | None, str | None]:
    """Mint a population handle. Keys are verbatim from `00-surfaced-tools.md` §4."""
    if not isinstance(source, str) or not source:
        return None, "a population handle needs a datasource"
    if not isinstance(engine, str) or not engine:
        return None, "a population handle needs the engine that produced it"
    if index_updated is not None and not _NAIVE_TS.match(str(index_updated)):
        return None, (f"index_updated must be the raw data-updates value "
                      f"(YYYY-MM-DDTHH:MM:SS), got {index_updated!r}")
    return _handle_encode(_HANDLE_POPULATION, {
        "source": source,
        # Which engine ran. The two lanes report INCOMPARABLE quantities -- a term score on one,
        # a cosine in 0..1 on the other -- and nothing in either response
        # reveals which produced a score, so a handle that lost this would let two
        # incomparable result sets be ranked together.
        "engine": engine,
        "program_filter": program_filter,
        "conditions": conditions or [],
        "count": count,
        # The poison rule. False means a distribution cannot be produced for this
        # population. `None` means UNKNOWN -- the unfiltered facets call did not land --
        # which is a different statement and must not read as False.
        "facetable": facetable,
        "as_of": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        # The index freshness AT MINT TIME, raw from the cache. Compared against current
        # freshness on decode; that comparison is the whole of staleness detection.
        "index_updated": index_updated,
    })


def encode_document_handle(datasource: str, card_id: str) -> tuple[str | None, str | None]:
    """Mint a document handle. `card_id` is `card.id`, never `body.document_id`.

    Refuses at MINT time, because wave 4 mints these from a search response where
    `card.id` may be absent -- and a handle minted from nothing surfaces at the model much
    later as "missing its datasource or document id", indistinguishable from corruption
    and unactionable.
    """
    if not isinstance(datasource, str) or not datasource:
        return None, "a document handle needs a datasource"
    if not isinstance(card_id, str) or not card_id:
        return None, ("a document handle needs card.id; the response carried none, so this "
                      "record cannot be offered for reading")
    return _handle_encode(_HANDLE_DOCUMENT, {"datasource": datasource, "id": card_id})


def decode_population_handle(handle: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Decode and validate a population handle against the live cache.

    Returns `(payload, error)`. On success the payload carries `_validated`, which is the
    third channel this function needs: `(payload, None)` alone would mean both "checked
    against the live cache" and "could not check", and those are different claims.

    Two validations beyond the checksum, both of which would otherwise produce a
    confident wrong answer:

    * the source must still be in scope AND still searchable. A handle minted before a
      permission change would otherwise re-run against a source that now returns 0 with
      empty `datasourceErrors` -- indistinguishable from absence.
    * the program filter must still match the one configured for that source. A reindex
      that changed the stored literal, or a scope-YAML edit, would silently describe a
      different population under the same handle.
    """
    payload, err = _handle_decode(_HANDLE_POPULATION, handle)
    if err:
        return None, err
    if payload is None:  # not reachable; narrows the type without an `assert`, which -O drops
        return None, "this handle does not contain a recipe"
    missing = [k for k in _POPULATION_KEYS if k not in payload]
    if missing:
        return None, (f"this handle is missing part of its recipe ({', '.join(missing)}); "
                      "re-run the search")
    if not isinstance(payload["source"], str) or not payload["source"]:
        return None, "this handle names no datasource; re-run the search"
    if not isinstance(payload["engine"], str) or not payload["engine"]:
        return None, "this handle does not say which engine produced it; re-run the search"
    if not isinstance(payload["program_filter"], dict):
        return None, "this handle's program boundary is malformed; re-run the search"

    # Gated on the SOURCES BEING POPULATED, not on `cache["ok"]`. An earlier revision
    # gated on `ok`, which meant a cache condemned by an unrelated failure -- an ingest
    # 403, or budget exhaustion, neither of which is transient so both persist -- skipped
    # every check below. In exactly those paths `access` came from a 200 response, so it
    # was trustworthy precisely where it was being ignored, and a DENIED source decoded
    # with no error at all.
    sources = (_CACHE or {}).get("sources") or {}
    if not sources:
        payload["_validated"] = False
        payload["_validation_note"] = (
            "the startup cache is not loaded, so this handle's source scope, access and "
            "program boundary could NOT be re-checked. It decoded, but nothing confirms it "
            "still describes the same population")
        return payload, None
    src = sources.get(payload["source"])
    if src is None:
        return None, (f"the source this handle describes ({payload['source']}) is no longer "
                      "in scope for this program")
    if not src.get("searchable"):
        return None, (f"{payload['source']} is no longer accessible to you "
                      f"(access: {src.get('access')}). Re-running this handle would "
                      "return zero records, which is not the same as finding none")
    # Compared unconditionally. A truthiness gate here would let an empty configured
    # filter silently skip the check that enforces the program boundary.
    if payload["program_filter"] != (src.get("program_filter") or {}):
        return None, (f"the program boundary for {payload['source']} has changed since this "
                      "handle was issued, so it no longer describes the same population. "
                      "Re-run the search")
    payload["_validated"] = True
    return payload, None


def decode_document_handle(handle: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Decode a document handle. Deliberately does NOT check scope.

    `muse_read` has a cold path: a reference recorded in `ground/` in an earlier session,
    with no search behind it. Requiring the source to be in the current program scope
    would break exactly that path. Readability and access are checked by `muse_read`
    itself, per document, where the answer can be reported per document.
    """
    payload, err = _handle_decode(_HANDLE_DOCUMENT, handle)
    if err:
        return None, err
    if payload is None:
        return None, "this handle does not contain a recipe"
    if not isinstance(payload.get("datasource"), str) or not payload["datasource"]:
        return None, "this handle names no datasource"
    if not isinstance(payload.get("id"), str) or not payload["id"]:
        return None, "this handle carries no document identifier"
    return payload, None


def population_staleness(payload: dict[str, Any]) -> dict[str, Any]:
    """Has the index moved under this handle since it was minted?

    Returns a report, never a refusal -- and never raises, which is why every value is
    type-checked before it is compared. A stale population is still a real population, and
    whether staleness matters is a scientific judgement, not a mechanical one. What is not
    optional is SAYING SO: an unreported stale count is a plausible wrong answer.

    THREE directions, not two. An earlier revision compared `current > minted` and so
    reported a BACKWARDS-moving index as "has not moved" -- but a backwards date means a
    reindex from scratch, a rollback, or the `data-updates` entry changing identity, and
    the count is at least as suspect as in the forward case.

    And when freshness is unknown, WHICH unknown is reported. The startup cache separates
    never-indexed, never-read, and no-entry-for-this-source deliberately; flattening them
    into one "unknown" throws that away, and "never indexed" is a far stronger statement
    than "we did not read the freshness".
    """
    minted = payload.get("index_updated")
    source = payload.get("source")
    fresh: dict[str, Any] = {}
    if isinstance(source, str):
        src = ((_CACHE or {}).get("sources") or {}).get(source)
        if isinstance(src, dict):
            fresh = src.get("freshness") or {}
    current = fresh.get("date")

    def unknown(detail: str) -> dict[str, Any]:
        return {"stale": None, "moved": None, "minted_index_date": minted,
                "current_index_date": current, "detail": detail}

    if fresh.get("never_indexed") is True:
        return unknown(f"{source} has never been indexed, so there is no index date to "
                       "compare against -- a stronger statement than unknown freshness")
    if fresh.get("unread") is True:
        return unknown("index freshness was never read this session, so staleness could "
                       "not be established for any source")
    if fresh.get("absent") is True:
        return unknown(f"{source} has no entry in data-updates at all, so its index "
                       "freshness is unknown and cannot be compared")
    if not isinstance(minted, str) or not _NAIVE_TS.match(minted):
        return unknown("this handle carries no usable index date from mint time, so "
                       "staleness could not be established")
    if not isinstance(current, str) or not _NAIVE_TS.match(current):
        return unknown("the current index date is unavailable or malformed, so staleness "
                       "could not be established")
    # Lexical comparison is sound ONLY because both operands are now guaranteed to match
    # `YYYY-MM-DDTHH:MM:SS` -- naive, fixed-width, so lexical order is chronological order.
    # The real limit is not "MUSE might emit offsets": it is that `minted` comes from
    # whatever the caller passed, so both sides are validated above rather than assumed.
    if current == minted:
        return {"stale": False, "moved": "unchanged", "minted_index_date": minted,
                "current_index_date": current,
                "detail": "the index has not moved since this population was measured"}
    if current > minted:
        return {"stale": True, "moved": "forward", "minted_index_date": minted,
                "current_index_date": current,
                "detail": "the index has been refreshed since this population was "
                          "measured, so the count may no longer hold"}
    return {"stale": True, "moved": "backwards", "minted_index_date": minted,
            "current_index_date": current,
            "detail": "the current index date is EARLIER than when this population was "
                      "measured, which means a reindex from scratch, a rollback, or the "
                      "data-updates entry changing identity. The count is at least as "
                      "suspect as if the index had moved forward"}


# --------------------------------------------------------------------------
# Request construction  (group BUILD wave 3)
# --------------------------------------------------------------------------
# Turns `muse_population`'s FIFTEEN model-facing parameters into wire payloads, or into a
# refusal. Request side only: nothing here performs I/O. Wave 4 executes the plans and
# assembles responses.
#
# The whole point of this layer is stated in the design as one rule (README rule 1): every
# parameter whose wrong value produces a SILENT wrong answer is set here, not exposed. The
# model supplies intent; the connector supplies encoding. Seventeen measured silent-discard
# modes make that not a style preference -- on meds, 1014 is the unconditioned population,
# so ANY request that returns 1014 when a condition was requested did nothing at all.
#
# Design sources, and they are the authority over this code:
#   02-muse-search-conditional.md  section 12 (Clause, roles, what is not exposed)
#                                  section 13 (construction, 11 steps)
#   00-surfaced-tools.md           section 3 (routing) · section 8 (taxonomy as roles)
#   01-muse-search.md              section 1b (one request per source, forced)
#                                  section 3 (construction, fieldsToExtract floor)
#   06-muse-facets.md              section 2 (poison rule) · section 8 (facet request)

# The four categories a REFUSAL reports -- corrected in wave 8, in AGENTS.md and the guide,
# which had promised these four for a ZERO. A zero reports `ZERO_CAUSES` instead.
# The four categories a REFUSAL reports -- which of these the problem lies in. Not what a
# ZERO reports: an empty result names `ZERO_CAUSES` instead, and wave 8 corrected `AGENTS.md`
# and the guide, which had promised these four for a zero. Two lists, two situations.
#
# Also NOT the retrieval-contributor's list, which is that subagent's RETURN shape and now has
# FIFTEEN kinds -- wave 7 removed two that the connector refuses before sending. This comment
# said 17 in two duplicated copies, both stale by then.
DEFECT_CATEGORIES = ("source_access", "filter_integrity", "query_rewriting",
                     "field_validity")

# The causes of a silent, plausible zero. README rule 2 measured SIX API behaviours; this
# tuple has SEVEN -- `silent_widening` is ours, refused at construction rather than
# observed from MUSE, and it is the first entry the model actually sees. A refusal names which of
# these it has positively eliminated -- which is what `AGENTS.md` section 11 means by "the
# tool reports which absence kinds it ruled out". Nothing may be listed here that was not
# actually checked.
ZERO_CAUSES = (
    "denied_datasource",            # a DENIED source returns 0 with an EMPTY error list
    "broken_program_filter",        # a bare or mis-cased formParams field returns 0
    "nonexistent_condition_field",  # a field absent from the mapping returns 0
    "multi_word_phrase",            # condition text is adjacency-matched; reversal gives 0
    "question_mark_in_query",       # `?` in basic query text drives it to 0
    "asterisk_in_condition",        # `*` in condition text gives 0
    # A seventh, beyond README rule 2's six, because `02` section 7 rows 1-4 measure FOUR
    # separate ways to send a request that returns the whole population at HTTP 200 while
    # presenting as narrowed. Wave 3 refuses all four before sending, so a zero that reaches
    # the model has this eliminated BY CONSTRUCTION -- worth saying, and unsayable if the
    # vocabulary had no name for it.
    "silent_widening",
)

_NOT_EXECUTED = (
    "No search request was sent, so this is NOT an empty result and NOT evidence of absence. "
    "Nothing was searched. Fix the request and it will be."
)

# `limit` from `depth`. 10000 is MEASURED to return the entire 1,014-record population in
# one call, so `complete` is affordable and is what a defensible count requires.
#
# `sample` and `survey` are deliberately NOT given numbers. Their count mapping is
# `ECONOMY - DEFERRED` by an explicit group rule, and a plausible default here would be the
# defect both prior review gates found: a made-up value where the truth is unknown. An
# unreported cap would answer an exhaustive question from a fraction of the population.
_DEPTH_LIMITS: dict[str, int | None] = {"complete": 10000, "survey": None, "sample": None}

# `newest` and `oldest` are NOT inverses: measured at n=60, `date_desc` orders
# `modified_date` while `date_asc` orders `creation_date`, each leaving the other
# unordered. So they answer different questions -- "most recently revised" versus "created
# earliest" -- and the result must name which field ordered it.
_SORTS: dict[str, tuple[str, str | None]] = {
    "newest": ("date_desc", "modified_date"),
    "oldest": ("date_asc", "creation_date"),
    # `None` because relevance is the API's DEFAULT and no sort key is emitted for it.
    # An earlier version put the string "relevance" here, which is not a wire value at all
    # -- the measured one is `relevancy` -- and relied on a `!= "relevance"` test downstream
    # to keep the fake off the wire. A sentinel that looks like a real value is one edit away
    # from being sent.
    "relevance": (None, None),
}

# The extraction floor: DO NOT TRIM. Inherited from the deleted `MIN_FIELDS` comment, whose
# reasoning outlived it.
#
# `fieldsToExtract` genuinely filters the card `body` -- measured 39 body fields on meds
# against 2 for `["title","id"]`. Those stripped 2 removed exactly the represented-state
# fields a qualification decision needs (`doc_status`, `document_id`,
# `current_version_doc_id`, the dates), and the impoverished record was then misread as
# evidence the API returns "86% plumbing". **Requesting FEWER fields was never how a result
# gets cheaper.** Per-source `default_fields` come from `cardModel.fields` and are added to
# this floor, never substituted for it.
#
# `fieldsToExtract` names CARD-level extraction targets, and that namespace is NOT
# `mappings.document.properties`. Measured 2026-08-25: `id` is absent from the mapping on
# four of five sources and `text_size` on all five, yet both are required here -- omitting
# `text_size` makes `card.textSize` read `0` rather than absent, a plausible wrong value
# that silently destroys the read-cost signal `muse_read` depends on. So the mapping
# validator applies to CONDITION fields only; applying it here would drop the floor.
_EXTRACT_FLOOR = ("title", "id", "text_size")

# mrl_slides does not populate its own `cardModel.link` template, which renders `?link?`.
# Its three real locators sit at DIFFERENT granularities -- a slide, its deck, and the
# .pptx file are different referents -- so the fallback order is deliberate and the
# granularity travels with the value (wave 4 emits it).
_LOCATOR_FALLBACKS: dict[str, tuple[str, ...]] = {
    "mrl_slides": ("slide_source_url", "deck_url", "slide_url"),
}

_TEMPLATE_FIELD = re.compile(r"\{([A-Za-z0-9_]+)\}")

# A locator has to be RESOLVABLE. Wave 8: the candidate order starts with the template's own
# INPUT fields -- correct for `fieldsToExtract` (those are the values MUSE substitutes) and
# wrong as locator candidates, because an input need not be a locator. Measured on all five
# sources: med_comms' template interpolates `{title}` and `{muse_raw_id}`, so `title` was tried
# first, was a non-empty string, and won on 19 of 19 records -- a document's own title returned
# as the place to go and verify it, with a `caution` asserting it was a relative path.
#
# TWO guards, and the FIELD one is the real one. A first attempt used only the value-shape test
# below, which the wave-8 gate broke in three of four tries: `"/"` and `"10."` both match ordinary
# prose, so a med_comms title of `10.5 mg tablet labelling update` -- a dose-prefixed title, not
# an exotic input in this corpus -- passed the shape test and won, now with a `caution` falsely
# asserting it was a resolvable path. Testing the VALUE of a field already known to be the wrong
# field is a heuristic where an exact answer exists.
#
# So: a field that is never a locator on any source is never accepted as one, by name. `title` is
# a document's name and `_RETRIEVAL_IDS` are identifiers; neither is a place. Then the shape test
# stays as a second line for fields not on that list.
#
# NOT a reordering. Putting `link` first would also have fixed med_comms, but on meds `link` and
# `webview_pdf_url` carry the SAME interpolated value and `webview_pdf_url` names a better
# granularity ("the document PDF" against "the source record"), so reordering would trade a wrong
# locator on one source for a vaguer one on another.
_NEVER_A_LOCATOR = frozenset({"title"})

# Measured shapes of the correct locator on each source: meds `webview_pdf_url`, med_comms `link`,
# scited/signals `link` and mrl_slides `slide_source_url` are all absolute URLs or rooted paths.
_LOCATOR_SHAPES = ("http://", "https://", "/", "doi:", "10.")


def _refuse(cause: str, message: str, *, defect_in: tuple[str, ...] = (),
            ruled_out: tuple[str, ...] = (), remedy: str | None = None,
            **extra: Any) -> dict[str, Any]:
    """One refusal shape, so nothing downstream can mistake a refusal for a result.

    TWO distinct lists, and an earlier version of this function conflated them under one
    name whose docstring said the opposite of what every call site passed:

      `defect_in`  -- which of the four categories the PROBLEM is in. This is what the
                      caller has to fix.
      `ruled_out`  -- which of the seven `ZERO_CAUSES` this refusal has
                      positively ELIMINATED. Only causes actually checked may appear.

    Telling a model that `source_access` was "ruled out" on an unknown-source refusal makes
    it conclude the exact opposite of the truth, which is why these are now separate and
    why both are validated rather than trusted.
    """
    bad_defect = [c for c in defect_in if c not in DEFECT_CATEGORIES]
    bad_cause = [c for c in ruled_out if c not in ZERO_CAUSES]
    if bad_defect or bad_cause:
        # Fail loudly at the call site rather than shipping a typo'd category to the model.
        raise AssertionError(
            f"_refuse({cause!r}) names unknown categories: {bad_defect + bad_cause}")
    out: dict[str, Any] = {
        "refused": True,
        "cause": cause,
        "message": message,
        "not_executed": _NOT_EXECUTED,
        "defect_in": list(defect_in),
        "ruled_out_zero_causes": list(ruled_out),
        # Always true for a construction refusal, and stated rather than implied: the model
        # is explicitly instructed to read a zero's diagnosis before concluding anything.
        #
        # WAVE 8 reworded this. It used to read `"executed with no visible matches"` -- a term
        # lifted verbatim from `retrieval-contributor/AGENTS.md`'s fifteen-kind return taxonomy,
        # which the MAIN agent never sees. `AGENTS.md` section 6's eight absence meanings do not
        # contain it and the guide's distinctions table does not define it, so a main-agent caller
        # got a bare phrase from another agent's private vocabulary under a key that reads like a
        # verdict. Said plainly instead, and phrased as what it denies rather than as a category.
        "ruled_out_absence": ("this is not an empty result: the request was never sent, so "
                              "nothing was searched and nothing was found to be missing"),
    }
    if remedy:
        out["remedy"] = remedy
    out.update(extra)
    return out


def _sanitize_condition_value(value: str) -> tuple[str, bool]:
    """Strip `*` from condition text. Returns (cleaned, was_changed).

    Per-endpoint and INVERTED from the basic endpoint: `*` returns 0 on conditional and
    works on basic, while `?` is inert here and breaks basic. Sharing one sanitiser between
    them would silently damage one of the two.
    """
    cleaned = value.replace("*", "")
    return cleaned, cleaned != value


def _sanitize_query_text(text: str) -> tuple[str, bool]:
    """Strip `?` from basic query text. Inverted from the conditional sanitiser."""
    cleaned = text.replace("?", "")
    return cleaned, cleaned != text


def _resolve_role(src: dict[str, Any], name: str) -> tuple[list[str] | None, str | None]:
    """Resolve one `Clause.in` entry to real field names for THIS source.

    Three kinds of entry, in precedence order: a field role, a taxonomy role, or a literal
    field name. Returns (fields, error). A role the source cannot fill returns
    `(None, None)` -- droppable and reportable, not an error, because a source genuinely
    not representing a role is a finding about the source.

    The mapping-unknown test comes FIRST. Wave 1 pre-loads `unfillable_roles` with every
    role precisely so a source whose ingest call failed cannot silently pass field
    validation -- but that means "unfillable" and "we never found out" look identical here,
    and only the second is a statement about our own call rather than about the source.
    An earlier ordering tested `unfillable_roles` first, which made the honest branch
    unreachable for every role name and reported a schema fact we had not established.
    """
    roles = src.get("roles") or {}
    if name in roles:
        return list(roles[name]), None
    fields = src.get("fields") or {}
    if name in fields:
        return [name], None
    tax = src.get("taxonomy_roles") or {}
    if name in tax:
        return [tax[name]], None
    if name.endswith(".raw") and name[:-4] in fields:
        return [name[:-4]], None
    if not src.get("mapping_retrieved"):
        return None, (
            f"whether {src['id']} represents {name!r} is UNKNOWN: this source's field "
            "mapping was never retrieved this session, so no field name can be checked "
            "against it. That is a fact about our own startup call, not about the source. "
            "A condition on a nonexistent field returns 0 records at HTTP 200, so nothing "
            "is sent."
        )
    if name in (src.get("unfillable_roles") or []):
        return None, None
    known = sorted(roles) + sorted(tax)
    return None, (
        f"{name!r} is not a field role, a taxonomy role, or a field on {src['id']}. "
        f"A condition on a nonexistent field returns 0 records at HTTP 200 -- "
        f"indistinguishable from absence -- so it is refused instead. "
        f"Roles here: {', '.join(known) if known else '(none resolved)'}."
    )


def _taxonomy_value(cache: dict[str, Any], source: str, field: str,
                    label: str) -> tuple[str | None, str | None]:
    """Resolve a taxonomy LABEL to the L-path for `field` on `source`.

    Two measured silent zeros make this a refusal rather than a pass-through: an L-path in
    the WRONG field returns 0, and an unknown label returns 0. Resolution goes through the
    branch ROOT UUID, never by name -- four of ten branch names do not match their field.

    The model never sees an L-path, the same rule that keeps `card.id` out of its hands.
    """
    tax = cache.get("taxonomy") or {}
    if not tax.get("loaded"):
        return None, (
            "the taxonomy tree was not loaded, so a taxonomy label cannot be resolved to "
            "the value the index actually stores. The human-readable name as a condition "
            "returns 0 records at HTTP 200 (measured), so this is refused rather than "
            "sent. Other fields are unaffected."
        )
    per_source = (tax.get("label_to_field_lpath") or {}).get(source) or {}
    hit = per_source.get(label)
    if hit is not None:
        got_field, lpath = hit
        if got_field != field:
            return None, (
                f"{label!r} belongs to {got_field!r}, not {field!r}. An L-path in the "
                "wrong taxonomy field returns 0 records at HTTP 200 (measured), so it is "
                f"refused. Use {got_field.removeprefix('mmd_tag_')!r} instead."
            )
        return lpath, None
    # Not a lookup but a DISAMBIGUATION: measured, `stability` has 0 exact labels and 54
    # containing it, and `shelf life` is absent from the taxonomy entirely. Offering the
    # near matches is the difference between a usable refusal and a dead end.
    lowered = label.casefold()
    near = sorted(l for l in per_source if lowered in l.casefold())
    detail = (f" Labels containing it: {', '.join(near[:8])}"
              + (f", and {len(near) - 8} more." if len(near) > 8 else ".")) if near else (
        " No label on this source contains it either, so this concept may not be in the "
        "taxonomy at all -- which is a real finding, not a spelling problem.")
    return None, (
        f"{label!r} is not a taxonomy label on {source}. An unmapped taxonomy value "
        f"returns 0 records at HTTP 200 (measured), so it is refused.{detail}"
    )


def compile_clause(cache: dict[str, Any], source: str, clause: Any,
                   kind: str) -> tuple[list[dict[str, Any]], list[str], str | None]:
    """Compile one `Clause` for one source. Returns (conditions, notes, error).

    `Clause = {"in": [roles or fields], "any_of": [values], "match": "phrase"|"exact"}`.

    This shape exists to make four of the seventeen silent-discard modes UNREPRESENTABLE
    rather than merely discouraged: `any_of` is a list so `"A OR B"` (which returns 0) is
    not expressible; values cannot be blank; `in` and `any_of` are our keys so there is no
    wire key left to misspell; and field names resolve against the real mapping.
    """
    notes: list[str] = []
    src = (cache.get("sources") or {}).get(source) or {}
    if not isinstance(clause, dict):
        return [], notes, f"a clause must be an object, got {type(clause).__name__}"

    raw_in = clause.get("in")
    if isinstance(raw_in, str):
        raw_in = [raw_in]
    if not isinstance(raw_in, list) or not raw_in:
        return [], notes, "a clause needs a non-empty `in` naming at least one field role"

    raw_values = clause.get("any_of")
    if isinstance(raw_values, str):
        raw_values = [raw_values]
    if not isinstance(raw_values, list) or not raw_values:
        # Measured mode #1/#2: an empty or whitespace condition is DROPPED and the request
        # returns the entire unfiltered population at HTTP 200.
        return [], notes, (
            "a clause needs a non-empty `any_of`. An empty condition value is dropped by "
            "the API and the request returns the WHOLE population at HTTP 200, labelled "
            "as a narrowed result."
        )

    match = clause.get("match", "phrase")
    if match not in ("phrase", "exact"):
        return [], notes, f"`match` must be 'phrase' or 'exact', got {match!r}"

    # --- resolve fields -------------------------------------------------------------
    # A taxonomy role may NOT be mixed with any other field in one clause. `any_of` values
    # are shared across the whole field list, and a taxonomy value is an L-path: measured,
    # an L-path in a non-taxonomy field matches nothing, silently. So a mixed clause emits
    # one live half and one dead half with no signal -- exactly the silent zero the taxonomy
    # guard exists to prevent, one field over. Refused rather than partially honoured.
    tax_roles = src.get("taxonomy_roles") or {}
    # Names AND roles. Both guards below tested role keys only, so a clause mixing a literal
    # taxonomy field name with another field passed straight through them.
    _tax_fields = set(tax_roles.values())
    tax_named = [e for e in raw_in
                 if isinstance(e, str) and (e in tax_roles or e in _tax_fields)]
    # WAVE 8: the MULTI-TAXONOMY case is tested FIRST. It used to sit second, and the mixed-clause
    # branch above it fires on `len(raw_in) > 1` -- which is also true when every entry is a
    # taxonomy role. So `{"in": ["issue", "manufacturing_step"]}` produced "'issue' is a taxonomy
    # role and cannot share a clause with []", naming an empty list as the thing it conflicts
    # with, and the message written for that input was unreachable. The refusal happened; the
    # reason was degenerate, which is worse than a generic one because it reads like a bug in the
    # caller's request rather than a real constraint.
    if len(tax_named) > 1:
        return [], notes, (
            f"a clause may name only one taxonomy role, got {tax_named!r}. Each taxonomy "
            "field has its own value vocabulary, so one value list cannot serve two. Use one "
            "clause per role; separate clauses are AND-ed."
        )
    if tax_named and len(raw_in) > 1:
        return [], notes, (
            f"{tax_named[0]!r} is a taxonomy role and cannot share a clause with "
            f"{[e for e in raw_in if e not in tax_named]!r}. A taxonomy value is an encoded "
            "path, and that path matches nothing in an ordinary text field -- returning 0 "
            "records at HTTP 200 for that half of the clause, with no error. Use one clause "
            "per kind of field; separate clauses are AND-ed."
        )
    fields: list[str] = []
    dropped: list[str] = []
    taxonomy_field: str | None = None
    for entry in raw_in:
        if not isinstance(entry, str) or not entry.strip():
            return [], notes, "every `in` entry must be a non-empty string"
        resolved, err = _resolve_role(src, entry)
        if err:
            return [], notes, err
        if resolved is None:
            dropped.append(entry)
            notes.append(f"{source} does not represent {entry!r}, so that part of the "
                         "clause was dropped rather than substituted with another field")
            continue
        for f in resolved:
            if f not in fields:
                fields.append(f)
        # BOTH vocabularies. Only a ROLE used to set this, so a literal taxonomy FIELD name went
        # through `_resolve_role`'s `name in fields` branch, left `taxonomy_field` unset, and sent
        # the label as raw condition text -- 0 records at HTTP 200. Measured: `in: ["issue"]` with
        # an unknown label refuses and offers a disambiguation, while `in: ["mmd_tag_issue"]` with
        # the same label is silently sent and matches nothing. The multi-taxonomy and mixed-clause
        # guards test role keys too, so a literal name bypassed those as well.
        #
        # Reachable through ordinary use: `muse_population`'s `where` description says a literal
        # field name from this source's own mapping resolves.
        _tax = src.get("taxonomy_roles") or {}
        if entry in _tax:
            taxonomy_field = _tax[entry]
        elif entry in set(_tax.values()):
            taxonomy_field = entry

    if not fields:
        # Every field dropped. Measured mode #4: sending the clause-less request returns
        # the whole population, so this is a refusal and never a silent widening to `_all`.
        return [], notes, (
            f"{source} represents none of {raw_in!r}, so this clause has no field to "
            "match against. Sending it without the clause would return the whole "
            f"population at HTTP 200. Unfillable here: "
            f"{', '.join(src.get('unfillable_roles') or []) or '(none)'}."
        )

    # --- resolve values -------------------------------------------------------------
    values: list[str] = []
    labels: list[str] = []
    for v in raw_values:
        if not isinstance(v, str):
            return [], notes, f"every `any_of` value must be a string, got {type(v).__name__}"
        if not v.strip():
            return [], notes, (
                "a blank or whitespace-only value is dropped by the API and the request "
                "returns the WHOLE population at HTTP 200."
            )
        if taxonomy_field is not None:
            lpath, err = _taxonomy_value(cache, source, taxonomy_field, v)
            if err:
                return [], notes, err
            values.append(str(lpath))
            # The L-path is what the wire needs; the LABEL is what the result has to say.
            # `00-surfaced-tools.md` section 8 keeps L-paths away from the model entirely, so
            # if only the encoded value survived into the plan, wave 4 would have nothing to
            # render and `02` section 14's "report the compiled conditions in the tool's own
            # vocabulary" would be unsatisfiable.
            labels.append(v)
            continue
        cleaned, changed = _sanitize_condition_value(v)
        if not cleaned.strip():
            return [], notes, (
                f"{v!r} is only wildcard characters. `*` returns 0 records at HTTP 200 on "
                "this endpoint, so it is stripped -- leaving nothing to match."
            )
        if changed:
            notes.append(f"`*` was removed from {v!r}: it matches nothing on this endpoint "
                         "(measured 0 hits at HTTP 200) rather than acting as a wildcard")
        values.append(cleaned)
        if len(cleaned.split()) > 1:
            # Legal and often intended -- a congress name, a tumour type. Not refused, but
            # the result must say adjacency was required: measured, `shelf life` returns 23
            # and `life shelf` returns 0.
            notes.append(
                f"{cleaned!r} is matched as a PHRASE, so the words must appear adjacently "
                "and in this order; reversing them matches nothing. `AND`/`OR` inside a "
                "value are matched literally, not as operators -- use several `any_of` "
                "entries for alternatives."
            )

    # --- apply `match` --------------------------------------------------------------
    # For a taxonomy field `match` is NOT load-bearing: measured, the `.raw` subfield and
    # the bare field both return 11 for the same L-path. So neither is refused here; the
    # refusal is on the VALUE, above, because the human-readable name returns 0.
    wire_fields: list[str] = []
    no_raw: list[str] = []
    partial_exact = False
    for f in fields:
        meta = (src.get("fields") or {}).get(f) or {}
        if match == "exact":
            if not meta.get("has_raw"):
                # A ROLE can resolve to several fields and only some may support exact
                # matching -- `status` resolves to `doc_status` and `activity_status` on
                # med_comms and only one has a `.raw`. Measured: a field list may freely mix
                # analysed and `.raw` fields. So drop the incapable field and REPORT it,
                # rather than refusing a clause that can be honoured; refuse only when
                # nothing in the list can do exact matching at all.
                no_raw.append(f)
                continue
            # The one case the facet-to-condition loop LIES rather than fails: a DATE facet
            # renders a year, and that year as a condition value returns 0 silently.
            if meta.get("facet_type") == "DATE":
                return [], notes, (
                    f"{f!r} is a DATE-typed facet on {source}. Its facet terms are "
                    "rendered years and return 0 records at HTTP 200 when used as an "
                    "exact-match value -- the one case where a facet value silently lies "
                    "instead of failing. Use `since`/`until` for date scoping."
                )
            wire_fields.append(f"{f}.raw")
        else:
            wire_fields.append(f)
    if match == "exact" and no_raw:
        if not wire_fields:
            return [], notes, (
                f"exact matching is impossible on {source} for {', '.join(no_raw)}: none has "
                "a `.raw` subfield, and a `.raw` condition on a field without one returns 0 "
                "records at HTTP 200 rather than failing. Use match='phrase', or a field that "
                "supports exact matching."
            )
        # The note is written so a reader can ACT on it. It used to say "not available on
        # {no_raw} ... matched exactly in {wire_fields} only", and `_model_facing_notes` maps every
        # field of a multi-field role back to the same role -- so on med_comms both blanks rendered
        # `status` and the sentence named the role as both searched and not searched. That is the
        # degenerate shape wave 8 fixed in `_plan_facets` and left standing here.
        #
        # A partial exact match is also NOT a defensible count, and its zero is NOT an absence.
        # Measured on med_comms: `where status exact "Final"` returned 0 with `defensible: true`
        # and `is_absence: true`, while 19 of 19 records carried a `doc_status` and 15 began
        # "Final" -- the role spans two fields, only one has a `.raw`, and the exact condition
        # therefore ran against the half that does not hold the value. The literal field name
        # refuses outright; the role silently answered.
        notes.append(
            f"{match!r} matching covered only part of what this constraint named on {source}: "
            f"{len(wire_fields)} of {len(wire_fields) + len(no_raw)} fields behind it support "
            "exact matching, and the rest were dropped rather than sent, because an exact "
            "condition on a field that has no exact subfield returns 0 records silently. So this "
            "count is a count over PART of the constraint. A zero here is not an absence -- ask "
            "for a distribution, or use `match: \"phrase\"`, which covers every field behind it.")
        partial_exact = True

    # `text` is an `ApiTextWithSynonyms` OBJECT, not a string, and `synonyms` lives INSIDE
    # it. A bare string is HTTP 400: `Cannot construct instance of ApiTextWithSynonyms ...
    # no String-argument constructor ... from String value ('MK-6070')`.
    #
    # Measured 2026-08-25 by EXECUTING this connector's own constructed payload. An offline
    # shape check could never catch it: the shape such a check asserts is the shape we
    # chose, so it agrees with itself. The design says `text.text` and `text.synonyms`,
    # dotted, and the dots mean nesting -- an earlier draft of this function read them as
    # prose and emitted both keys flat, which every one of 196 offline assertions accepted
    # and MUSE rejected outright.
    #
    # `any_of[0]` -> `text.text`, the rest -> `text.synonyms`. Verified a true OR with a
    # paired control: impossible primary + real synonym returns the real term's count, and
    # impossible primary + impossible synonym returns 0. `synonyms` is the ONLY OR this
    # endpoint has -- `"A OR B"` inside the text is matched literally and returns 0.
    text_obj: dict[str, Any] = {"text": values[0]}
    if len(values) > 1:
        text_obj["synonyms"] = values[1:]
    condition: dict[str, Any] = {
        "fields": wire_fields,
        "text": text_obj,
        # `type` is ALWAYS present: omitting it is a 500 (a null dereference), even though
        # the spec marks it optional.
        "type": COND_TYPES["must_not" if kind == "must_not" else "must"],
    }
    if labels:
        # Not a wire key -- stripped before sending, and carried only so the result can name
        # what was asked in the vocabulary the caller used.
        condition["_labels"] = labels
    if partial_exact:
        # Internal, stripped before the wire like `_labels`. Carried on the condition rather than
        # through `compile_clause`'s return type, which has three call sites expecting a 3-tuple.
        condition["_partial_exact"] = True
    return [condition], notes, None


# A population handle records the recipe in MODEL vocabulary -- `00-surfaced-tools.md`
# section 4 shows `"conditions": [{"in": ["anywhere"], "any_of": ["MK-6070"]}]`. That is
# deliberate: the handle has to survive a restart and a `/compact`, and the model's own
# vocabulary is the stable thing. It is NOT the wire shape.
#
# So narrowing RECOMPILES the inherited clauses through exactly the same compiler the
# caller's clauses go through. An earlier version pasted them onto the wire verbatim, which
# produced conditions with no `type` and no `text` -- HTTP 500 -- and, had they reached
# OpenSearch, `in`/`any_of` are unknown keys that are silently dropped, widening the
# population to the whole program scope while the plan claimed a narrowing.
#
# CORRECTED, wave 8. This comment used to end: "Recompiling also means every guard in
# `compile_clause` applies to the inherited half." That stopped being true when wave 5 changed
# minting to record the COMPILED clause -- byte-exact replay is now the normal path, and it
# bypasses `compile_clause` entirely, which is the point of minting it that way. Wave 5 made
# that change for good reasons (reconstructing model vocabulary dropped the condition `type`,
# so a `where_not` exclusion recompiled as a `MUST`) and left this sentence behind.
#
# Only model-vocabulary clauses are recompiled. The compiled shape gets one guard, added in
# wave 8 -- field existence -- for the reason spelled out at the pass-through branch below.
def _compile_inherited(
    cache: dict[str, Any], source: str, inherited: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any] | None]:
    """Recompile a handle's recorded clauses. Returns (conditions, notes, refusal)."""
    if not inherited:
        return [], [], None
    raw = inherited.get("conditions")
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        return [], [], _refuse(
            "handle_recipe_malformed",
            "this population handle's recorded recipe is not a list of clauses, so the "
            "population it names cannot be reproduced. A handle exists to make a count "
            "re-derivable; one that cannot be replayed is refused rather than approximated.",
            defect_in=("filter_integrity",),
            remedy="Re-run the search that produced it.")
    out: list[dict[str, Any]] = []
    notes: list[str] = []
    for clause in raw:
        if not isinstance(clause, dict):
            return [], notes, _refuse(
                "handle_recipe_malformed",
                f"this handle's recipe contains a {type(clause).__name__} where a clause "
                "was expected, so the population it names cannot be reproduced.",
                defect_in=("filter_integrity",),
                remedy="Re-run the search that produced it.")
        # Already-compiled wire conditions are accepted as-is; anything else is model
        # vocabulary and gets compiled. Both shapes exist in handles minted by different
        # revisions, and refusing the older one would break a resume from `ground/`.
        if "type" in clause and "text" in clause:
            # WAVE 8. The pass-through has to re-check the FIELDS, because it runs none of
            # `compile_clause`'s guards and wave 5 made it the normal path -- minting records
            # the compiled clause precisely so replay is byte-exact.
            #
            # The failure without this, measured: record a handle for
            # `where=[{"in":["status"],"any_of":["Effective"],"match":"exact"}]` on meds; let a
            # reindex drop `doc_status`. A FRESH clause then refuses `clause_not_compilable` and
            # sends nothing. The same clause REPLAYED is sent verbatim, matches nothing, returns
            # 0 at HTTP 200 -- and `diagnose_zero` gates on `mapping_retrieved`, so it states
            # "every field named in this request exists in the source's authoritative mapping",
            # eliminating `nonexistent_condition_field` and shipping `is_absence: True`. A false
            # zero with its actual cause affirmatively ruled out, on the list `AGENTS.md`
            # section 11 tells the model to trust.
            #
            # Not hypothetical: meds moved its index twice in three days, 1,014 -> 799 -> 1,014.
            #
            # Only when the mapping was actually retrieved -- otherwise "we never found out"
            # would masquerade as "the field is gone", which is the inverse error.
            #
            # NO `_all` EXEMPTION. This carried one, justified by a comment saying `_all` is
            # "virtual and never in `mappings.document.properties`". That is false, and the
            # contradicting measurement was already in this file 1,100 lines above: `warm_cache`
            # records `_all` as declared in the mapping on all five sources, dated, and warns in
            # terms that "an unconditional exemption would have been both unnecessary and a hole in
            # the rule this block exists to enforce". Re-measured 2026-08-26: present on all five.
            # So the exemption was unnecessary -- `_all` passes the check on its own -- and it
            # would have let a replayed `_all` clause through unchecked on a source that had lost
            # it, while a fresh one was caught. A claim made without looking, with the measurement
            # in the same file.
            if src_fields := ((cache.get("sources") or {}).get(source) or {}):
                if src_fields.get("mapping_retrieved"):
                    known = src_fields.get("fields") or {}
                    gone = sorted({
                        str(f) for f in (clause.get("fields") or [])
                        if str(f).removesuffix(".raw") not in known})
                    if gone:
                        return [], notes, _refuse(
                            "handle_recipe_not_compilable",
                            f"this population handle names "
                            f"{', '.join(sorted({g.removesuffix('.raw') for g in gone}))} on "
                            f"{source}, and "
                            "that field is no longer in the source's authoritative mapping. "
                            "Replaying it would send a condition that matches nothing and "
                            "returns 0 records at HTTP 200 -- indistinguishable from the "
                            "population genuinely being empty, and the zero diagnosis would "
                            "report the field as present because the recorded recipe skips "
                            "field validation. So it is refused instead.",
                            defect_in=("field_validity", "filter_integrity"),
                            source=source,
                            remedy="Re-run the search that produced this handle; the source's "
                                   "schema has changed since it was minted.")
            out.append(clause)
            continue
        got, cnotes, err = compile_clause(
            cache, source, clause, "must_not"
            if clause.get("type") == COND_TYPES["must_not"] else "must")
        notes.extend(cnotes)
        if err:
            return [], notes, _refuse(
                "handle_recipe_not_compilable",
                f"this population's recorded recipe can no longer be compiled for "
                f"{source}: {err}",
                defect_in=("filter_integrity",),
                remedy="The source's schema or vocabulary changed since this population "
                       "was measured. Re-run the search rather than narrowing this handle.")
        out.extend(got)
    return out, notes, None


def compile_conditions(
    cache: dict[str, Any], source: str, *, about: str | None = None,
    where: list[Any] | None = None, where_not: list[Any] | None = None,
    inherited: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]] | None, list[str], dict[str, Any] | None]:
    """Compile a whole request's conditions for one source. Returns (conditions, notes, refusal).

    Refuses every route to a silently-unfiltered population. There are FOUR measured ways
    to reach it and all of them return HTTP 200 with the whole population: no clause at
    all, `conditions: []`, no `conditions` key, and `conditionQuery: {}`.
    """
    notes: list[str] = []
    inherited = list(inherited or [])
    conditions: list[dict[str, Any]] = list(inherited)

    # SILENT WIDENING versus a DELIBERATE whole-population request. The test is on the
    # SUPPLIED containers, not on what compiled -- an earlier version tested the compiled
    # result, so an inherited recipe filled the list and `where: []` slipped through
    # accepted, silently discarding the caller's (empty) constraint while the call presented
    # as a narrowing.
    if isinstance(where, list) and not where:
        return None, notes, _empty_container_refusal("where")
    if isinstance(where_not, list) and not where_not:
        return None, notes, _empty_container_refusal("where_not")

    # `about` is free text across everything. On this endpoint it compiles to a `MUST` on
    # the `anywhere` role, which reproduces a basic free-text query EXACTLY on all five
    # sources once synonyms are injected.
    if about is not None:
        if not isinstance(about, str) or not about.strip():
            return None, notes, _refuse(
                "blank_about",
                "`about` was given but is empty. An empty condition value is dropped by the "
                "API and the request returns the whole population at HTTP 200, so it is "
                "refused.",
                defect_in=("field_validity",),
                remedy="Give `about` real text, or omit it and use `where`.")
        got, cnotes, err = compile_clause(
            cache, source, {"in": ["anywhere"], "any_of": [about]}, "must")
        notes.extend(cnotes)
        if err:
            return None, notes, _refuse(
                "about_not_compilable", err, defect_in=("field_validity",))
        conditions.extend(got)

    for kind, clauses in (("must", where or []), ("must_not", where_not or [])):
        if not isinstance(clauses, list):
            return None, notes, _refuse(
                "bad_clause_list",
                f"`{'where' if kind == 'must' else 'where_not'}` must be a list of clauses.",
                defect_in=("field_validity",))
        for clause in clauses:
            got, cnotes, err = compile_clause(cache, source, clause, kind)
            notes.extend(cnotes)
            if err:
                return None, notes, _refuse(
                    "clause_not_compilable", err,
                    defect_in=("field_validity",),
                    source=source,
                    remedy="Correct the clause; nothing was searched.")
            conditions.extend(got)

    # A DELIBERATE whole-program-population request is legitimate -- it is
    # `00-surfaced-tools.md` section 7's canonical large-population call, and the program
    # filter still scopes it. What must never happen is reporting it as narrowed.
    #
    # An exclusion-only request is allowed for the same reason: the program filter supplies
    # the positive scoping and the emitted conditions state exactly what was excluded.
    if not [c for c in conditions if c.get("type") == COND_TYPES["must"]]:
        notes.append(
            "no positive constraint was given, so this is the source's whole program "
            "population under its program filter, not a narrowed one"
            + (" minus the stated exclusions" if conditions else ""))
    return conditions, notes, None


def _empty_container_refusal(name: str) -> dict[str, Any]:
    return _refuse(
        "empty_constraint_supplied",
        f"`{name}` was supplied but is empty, so nothing would be applied and the request "
        "would return this source's entire program population at HTTP 200 while appearing "
        "to be narrowed. That is the most dangerous outcome this endpoint offers, so it is "
        "not sent.",
        defect_in=("filter_integrity",),
        remedy=(f"Put a clause in `{name}`, or omit it entirely to ask for the whole "
                "program population deliberately -- that is a valid request and returns a "
                "count and a distribution."))


def route_engine(*, semantic: bool, question: str | None,
                 where: list[Any] | None, where_not: list[Any] | None,
                 inherited_engine: str | None = None) -> tuple[str | None, dict[str, Any] | None]:
    """Choose the engine. Returns (engine, refusal).

    Routing is INTERNAL by design. An earlier draft exposed it as
    `precision="discovery"|"measurement"` and that was rejected in terms: a defaultable
    parameter that silently produces a non-defensible count is the failure class the whole
    design removes.

    Basic offers exactly ONE thing conditional cannot do -- the semantic lane. Its 60-rule
    engine is measured inert on our population, its synonym expansion is exactly
    replicable via explicit `text.synonyms`, and its query rewriting recovers a `?` the
    connector sanitises anyway.
    """
    if question is not None and not semantic:
        # Checked BEFORE the inheritance shortcut. An earlier ordering returned early for an
        # inherited engine, so narrowing accepted a `question` with `semantic=False` that the
        # fresh path refuses -- the same input, two answers.
        return None, _refuse(
            "question_without_semantic",
            "`question` drives the semantic (vector) lane, which exists only on the basic "
            "engine. Without `semantic=True` it would be silently discarded, and quietly "
            "turning it on would change which engine produced your count -- the two are "
            "scored on incomparable scales, and only one of them yields a defensible count.",
            defect_in=("query_rewriting",),
            remedy="Pass `semantic=True` alongside `question`, or use `about` for keyword "
                   "retrieval.")
    if inherited_engine == "basic":
        # A basic-engine population cannot be narrowed, because the handle records no query
        # text -- so the recipe cannot be replayed and any "narrowed" count would describe a
        # population nobody can re-derive. Accepting it silently produced a FRESH population
        # presented as a narrowing of one it had no relationship to.
        return None, _refuse(
            "basic_population_not_narrowable",
            "this population was measured on the semantic engine, whose recipe is a query "
            "embedding rather than a set of conditions. The handle cannot replay it, so a "
            "narrowed count could not be re-derived from it and would describe a different "
            "population under the same handle.",
            defect_in=("filter_integrity",),
            remedy="Run a fresh keyword population with `where`, or re-run the semantic "
                   "search with a narrower `question`.")
    if inherited_engine is not None:
        # Narrowing an existing population may not switch engines: the two report incomparable
        # score quantities (a term score against a cosine in 0..1, for the SAME
        # documents), so the before and after counts would not be comparable.
        if semantic and inherited_engine != "basic":
            return None, _refuse(
                "engine_switch",
                f"This population was measured on the {inherited_engine} engine, and "
                "`semantic=True` would run the basic engine instead. Relevance scores are "
                "incomparable between the two and nothing in either response reveals which "
                "ran, so the narrowed count would not be comparable with the count you "
                "already hold.",
                defect_in=("query_rewriting",),
                remedy="Narrow on the same engine, or start a new population.")
        return inherited_engine, None

    if semantic:
        if where or where_not:
            # The semantic lane lives on `/v2/query`, which has no condition model at all.
            # Compiling clauses into `formParams` there is unmeasured, and a bare
            # non-`.raw` `formParams` field is measured to return 0 silently.
            return None, _refuse(
                "semantic_with_clauses",
                "The semantic lane runs on the basic engine, which has no condition model "
                "-- `where` and `where_not` cannot be applied there, and routing them "
                "through the filter surface instead is unmeasured and returns 0 silently "
                "when it is wrong.",
                defect_in=("filter_integrity",),
                remedy="Either drop `semantic` to use `where` on the conditional engine, "
                       "or run the semantic search first and narrow the population it "
                       "returns.")
        return "basic", None
    return "conditional", None


def plan_source_request(
    cache: dict[str, Any], source: str, *,
    about: str | None = None, where: list[Any] | None = None,
    where_not: list[Any] | None = None, since: str | None = None,
    until: str | None = None, semantic: bool = False, question: str | None = None,
    order: str = "relevance", depth: str = "complete", offset: int = 0,
    fields: list[str] | None = None, distribution: Any = True,
    value_prefix: dict[str, str] | None = None,
    inherited: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build every wire payload for ONE source, or a refusal. No I/O.

    One request per source is FORCED, not chosen: `formParams` keys AND together globally,
    so a single request cannot carry per-source filters. Measured -- one request naming two
    sources with only one source's filter returns HTTP 200 while the other source
    contributes 0 with no `datasourceErrors` and no warning at all.
    """
    src = (cache.get("sources") or {}).get(source)
    if src is None:
        in_scope = cache.get("in_scope") or []
        return _refuse(
            "unknown_source",
            f"{source!r} is not a source in this program's scope. A bogus datasource name "
            "returns HTTP 404 for the whole request, so it is not sent.",
            defect_in=("source_access",),
            remedy=f"In scope: {', '.join(in_scope) if in_scope else '(none resolved)'}.")

    # SEARCHABILITY, and nothing else can answer it. A DENIED source returns 0 records with
    # `datasourceErrors` EMPTY -- indistinguishable from absence. `/v2/configuration` lists
    # a denied source with `hidden: False` and `/v3/ingest` serves its full schema at 200,
    # so neither of those can stand in for this.
    if src.get("access") is None:
        # UNKNOWN, not denied. Failing closed is right; asserting the caller is denied when
        # `user-access-check` never answered is a plausible wrong answer -- and if that one
        # call fails, all five sources would report a denial and the model would conclude it
        # has access to nothing.
        return _refuse(
            "source_access_unknown",
            f"whether this caller can search {source} could not be determined this session: "
            "the access check did not complete, and it is the only thing that can answer it. "
            "A denied source returns 0 records with an EMPTY error list, indistinguishable "
            "from absence, so the search is not sent rather than risk reporting a denial as "
            "an empty result.",
            defect_in=("source_access",),
            source=source,
            remedy="This is a connector startup failure, not a finding about the program. "
                   "Report it as a limitation.")
    if src.get("searchable") is not True:
        return _refuse(
            "source_not_accessible",
            f"{source} is not searchable for this caller (access: {src['access']!r}). A "
            "denied source returns 0 records with an EMPTY error list, which is "
            "indistinguishable from the material not existing -- so this is reported as "
            "inaccessible rather than searched.",
            defect_in=("source_access",),
            # WAVE 8 removed `ruled_out=("denied_datasource",)` from here. `ruled_out` means
            # "positively ELIMINATED as a cause of a zero" -- this function's own docstring says
            # so and warns, in terms, that naming a category the problem IS makes the model
            # conclude the opposite of the truth. This refusal exists BECAUSE the source is
            # denied, so listing `denied_datasource` as ruled out was that exact inversion.
            #
            # It also read oppositely to `diagnose_zero`, which appends the same token only when
            # `access == "ALLOWED"`. Two places, one string, contradictory meanings.
            #
            # The earlier comment argued the caller's hypothetical zero "would have been
            # permission" -- true, and it is the `message` and `remedy` that say so. Nothing is
            # eliminated here, because nothing was searched.
            source=source,
            remedy="Request access, or exclude this source and note the exclusion.")

    program_filter = src.get("program_filter") or {}
    # README rule 2 cause #2: a bare or mis-cased `formParams` field returns 0 records at
    # HTTP 200. The field name comes verbatim from the scope YAML, which is recorded as
    # "mostly wrong" and which wave 7 regenerates -- so it gets checked here rather than
    # trusted. Without this, `program_filter_field: product_name` (no `.raw`) builds a
    # request that returns 0 with `count_defensible: True`.
    for field_name in program_filter:
        if not str(field_name).endswith(".raw"):
            return _refuse(
                "program_filter_not_exact",
                f"{source}'s configured program filter field is {field_name!r}, which is not "
                "an exact-match subfield. A `formParams` filter on a bare analysed field "
                "returns 0 records at HTTP 200 -- indistinguishable from this program having "
                "no documents in this source. The scope configuration is wrong, not the "
                "program.",
                defect_in=("filter_integrity",),
                source=source,
                remedy=f"The field should be {field_name}.raw. Regenerate the scope "
                       "configuration from the index mappings.")
        base = str(field_name)[:-4]
        if src.get("mapping_retrieved") and base not in (src.get("fields") or {}):
            return _refuse(
                "program_filter_field_absent",
                f"{source}'s configured program filter field {field_name!r} does not exist "
                "in this source's authoritative field mapping, so filtering on it would "
                "return 0 records at HTTP 200 and read as this program having no documents "
                "here.",
                defect_in=("filter_integrity",),
                source=source,
                remedy="Regenerate the scope configuration from the index mappings.")
    if not program_filter or not any(v for v in program_filter.values()):
        return _refuse(
            "no_program_filter",
            f"{source} has no validated program-filter mapping, so a search there cannot "
            "be scoped to this program. An unscoped request would return other programs' "
            "records under a program-scoped label.",
            defect_in=("filter_integrity",),
            source=source,
            remedy="Exclude this source and report the exclusion.")

    # RECOMPILED, not pasted. The handle records model vocabulary; the wire needs compiled
    # conditions. This is the whole of H1's fix and it must happen before routing, because a
    # recipe that no longer compiles is a refusal rather than a request.
    inherited_conditions, inh_notes, inh_refusal = _compile_inherited(cache, source, inherited)
    if inh_refusal:
        return inh_refusal
    engine, refusal = route_engine(
        semantic=semantic, question=question, where=where, where_not=where_not,
        inherited_engine=(inherited or {}).get("engine"))
    if refusal:
        return refusal

    if order not in _SORTS:
        return _refuse(
            "bad_order", f"`order` must be one of {', '.join(_SORTS)}, got {order!r}.",
            defect_in=("field_validity",))
    if depth not in _DEPTH_LIMITS:
        return _refuse(
            "bad_depth", f"`depth` must be one of {', '.join(_DEPTH_LIMITS)}, got {depth!r}.",
            defect_in=("field_validity",))
    limit = _DEPTH_LIMITS[depth]
    if limit is None:
        return _refuse(
            "depth_unmapped",
            f"depth={depth!r} has no record count assigned to it. Mapping "
            "'sample'/'survey' onto actual counts is a deferred economy decision, and "
            "guessing a number here would silently cap an exhaustive question -- so it is "
            "refused rather than defaulted.",
            defect_in=("field_validity",),
            remedy="Use depth='complete' (the default), which retrieves the whole "
                   "population in one call.")
    for label, value in (("since", since), ("until", until)):
        if value is None:
            continue
        # Validated HERE because wave 4 applies them as OUR post-filter over a complete
        # retrieval. An unparseable value would silently filter nothing or everything, and
        # the result would still report "both counts" as though the filter had meaning.
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
            return _refuse(
                "bad_date_bound",
                f"`{label}` must be an ISO date, `YYYY-MM-DD`, got {value!r}. This filter is "
                "applied by the connector rather than by the API -- no date expression works "
                "as a condition -- so an unparseable bound would silently filter nothing "
                "while the result claimed a date scope.",
                defect_in=("field_validity",))
    if since and until and since > until:
        return _refuse(
            "empty_date_window",
            f"`since` ({since}) is later than `until` ({until}), so the window is empty and "
            "every record would be filtered out by us -- reading as absence rather than as "
            "our own filter.",
            defect_in=("field_validity",))
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        return _refuse("bad_offset", f"`offset` must be a non-negative integer, got {offset!r}.",
                       defect_in=("field_validity",))
    if offset >= 10000:
        # Measured: 10000 and 10001 both return HTTP 400.
        return _refuse(
            "offset_too_large",
            f"offset={offset} is at or beyond the API's paging ceiling, which returns "
            "HTTP 400. depth='complete' retrieves the whole population without paging.",
            defect_in=("field_validity",))

    notes: list[str] = []
    conditions: list[dict[str, Any]] | None = None
    query_text: str | None = None

    if engine == "basic":
        # MEASURED: `basic_knn` returns `totalHits: 0` with the real count in `totalHitsKnn`,
        # and KNN silently downgrades to keyword on scited, mrl_slides and signals -- only
        # meds and med_comms support it. `02` section 5's prohibition is on the CONDITIONAL
        # `_knn` variants, which execute silently as keyword; on the basic endpoint the knn
        # variant is the one thing this engine uniquely offers and is the entire reason
        # routing exists.
        #
        # An earlier version sent plain `basic` with no `knnSearchQuery`, so `semantic=True`
        # ran a literal keyword search of the model's natural-language question while the
        # plan claimed a semantic lane. `route_engine` refuses `question` without `semantic`
        # on the grounds that it "drives the vector lane" -- so not engaging that lane made
        # the refusal incoherent as well as the result wrong.
        if src.get("knn") is None:
            # UNKNOWN is not a negative capability. This branch was added by the fix that gave
            # `knn` a `None` state, and then `is not True` swept `None` in with `False` --
            # asserting a definite absence of support and a definite mechanism ("silently
            # downgrades") for a source about which nothing was established. Third instance in
            # this group of a fix breaking the convention it was written to honour.
            return _refuse(
                "semantic_support_unknown",
                f"whether {source} supports semantic retrieval could not be established this "
                "session: the project's vector configuration did not arrive in a readable shape, "
                "so this connector does not know which sources have vector support. A KNN request "
                "to a source without it downgrades to a keyword search silently, so nothing is "
                "sent rather than guessing.",
                defect_in=("source_access",), source=source,
                remedy="Run this question without `semantic` to get a term-matched population.")
        if src.get("knn") is not True:
            return _refuse(
                "semantic_unavailable_on_source",
                f"{source} does not support semantic retrieval, and a KNN request there "
                "silently downgrades to a keyword search -- returning term matches under a "
                "similarity label, which is a different question answered without saying so.",
                defect_in=("query_rewriting",),
                source=source,
                remedy=f"Drop `semantic` to search {source} by keyword, or restrict "
                       "`sources` to the semantic-capable ones.")
        raw = question if question is not None else about
        if not isinstance(raw, str) or not raw.strip():
            return _refuse(
                "semantic_without_text",
                "The semantic lane needs text to embed. Pass `question` (or `about`).",
                defect_in=("field_validity",))
        query_text, changed = _sanitize_query_text(raw)
        if changed:
            # Inverted from the conditional sanitiser: `?` in basic query text is measured
            # to drive the result to 0, recoverable only through the server's own hint.
            notes.append("`?` was removed from the query text: on this engine it drives "
                         "the result to 0 rather than acting as a wildcard")
        if not query_text.strip():
            return _refuse(
                "semantic_text_all_punctuation",
                f"{raw!r} is only characters this engine cannot use.",
                defect_in=("field_validity",))
    else:
        conditions, cnotes, refusal = compile_conditions(
            cache, source, about=about, where=where, where_not=where_not,
            inherited=inherited_conditions)
        notes.extend(cnotes)
        if refusal:
            return refusal
        notes.extend(inh_notes)

    # --- fieldsToExtract ------------------------------------------------------------
    # Floor + the locator source field(s) + validated extras. NOT validated against
    # `mappings.document.properties`: that is a different namespace (see _EXTRACT_FLOOR).
    extract: list[str] = list(_EXTRACT_FLOOR)
    mapping = src.get("fields") or {}
    # PER-SOURCE `default_fields`, which `02` section 13 step 7 and `01` section 3 step 5
    # both require in the floor. Omitting them recreates the `MIN_FIELDS` failure rule 0
    # forbids: sending `fieldsToExtract` TRIMS the card body to exactly what is named, and
    # the fields lost are the represented-state ones a qualification decision needs --
    # status, version, document ids, dates. Every record would arrive qualifiable only by
    # its title, and README's "the claim, and what said it" slice would be unfillable.
    # `cardModel.fields` entries are OBJECTS, not strings: `{id, label, showInSummary,
    # startsNewSection}`. Measured 2026-08-25 -- and once the `id` is read, EVERY entry
    # resolves in the authoritative mapping (21/21 meds, 38/38 med_comms, 10/10 scited,
    # 14/14 mrl_slides, 13/13 signals), which closes the endpoint dive's open question about
    # what this list is. It is the card display configuration, and it is the per-source
    # `default_fields` the construction steps call for.
    #
    # An earlier version filtered on `isinstance(f, str)` and therefore admitted NONE of
    # them, leaving the floor at four fields while appearing to have applied the fix.
    #
    # `text` is excluded: it appears in mrl_slides' list and is rejected by
    # `fieldsToExtract` with HTTP 400, so including it would fail every request to that
    # source rather than merely omitting a field.
    default_fields = []
    for entry in src.get("card_default_fields") or []:
        name = entry.get("id") if isinstance(entry, dict) else entry
        if isinstance(name, str) and name and name != "text":
            default_fields.append(name)
    added_defaults = [f for f in default_fields if f in mapping and f not in extract]
    extract.extend(added_defaults)
    if default_fields and not added_defaults:
        # LOUD, because the silent version of this is what shipped once: the list was
        # non-empty (21 entries on meds live) and the mapping filter admitted none of them,
        # so the floor stayed at four fields and the fix looked applied while doing nothing.
        #
        # `cardModel.fields` is recorded as UNEXAMINED in the endpoint dive -- "plausibly the
        # card display fields" -- so treating it as the design's per-source `default_fields`
        # is an inference, not a measurement. Until that is closed, a request whose
        # qualification fields could not be resolved says so rather than returning records
        # qualifiable only by their title.
        notes.append(
            f"WARNING: none of {source}'s {len(default_fields)} configured card fields "
            "resolved against its authoritative mapping, so this request carries only the "
            "floor. Records will arrive WITHOUT represented state -- no status, no version, "
            "no dates -- and cannot be qualified beyond their title and locator. Where the "
            "per-source default fields come from is an open measurement.")
        default_fields_unresolved = True
    else:
        default_fields_unresolved = False
    for f in _TEMPLATE_FIELD.findall(src.get("locator_template") or ""):
        if f not in extract:
            extract.append(f)
    for f in _LOCATOR_FALLBACKS.get(source, ()):
        if f in mapping and f not in extract:
            extract.append(f)
    dropped_fields: list[str] = []
    malformed_fields: list[str] = []
    for f in fields or []:
        if not isinstance(f, str) or not f.strip():
            # Reported, not skipped in silence: an unnamed drop is the same defect as an
            # unnamed unknown field, one type-error earlier.
            malformed_fields.append(repr(f))
            continue
        if f == "text":
            # Measured HTTP 400. The API enforces the search/read boundary here, so this is
            # a refusal rather than a silent drop -- the caller wanted document content and
            # should be told which tool provides it.
            return _refuse(
                "text_not_extractable",
                "`text` cannot be requested as a record field -- the API rejects it with "
                "HTTP 400. Document content is a separate act: search identifies and "
                "qualifies candidates, and `muse_read` returns what a document says.",
                defect_in=("field_validity",),
                remedy="Drop `text` here and call `muse_read` on the records you choose.")
        if f in mapping:
            if f not in extract:
                extract.append(f)
        else:
            dropped_fields.append(f)
    if malformed_fields:
        notes.append(
            f"ignored in `fields`, not usable field names: {', '.join(malformed_fields)}")
    if dropped_fields:
        if src.get("mapping_retrieved"):
            notes.append(
                f"dropped from `fields`, absent from {source}'s mapping: "
                f"{', '.join(sorted(dropped_fields))}. Reported rather than sent, because an "
                "unknown field name is not an error the API reports back.")
        else:
            # A claim about our own startup call, not about the source's schema.
            notes.append(
                f"not requested from {source}: {', '.join(sorted(dropped_fields))}. Whether "
                "this source has these fields is UNKNOWN -- its field mapping was never "
                "retrieved this session, so they could not be validated.")

    sort_value, sort_field = _SORTS[order]
    if order == "relevance" and engine == "conditional":
        # WAVE 8: all three claims this note carried were measured FALSE, and one was invalidated
        # by wave 8 itself.
        #
        #   "spans about 2% (2.5113-2.5653)"       measured 2026-08-26: meds 3.5533-7.3447, a 51.6%
        #                                          spread; med_comms 0.7808-5.0179, 84.4%. The
        #                                          design-phase range was one population, one day.
        #   "so it barely discriminates"           the REASON the note gave for not ranking on
        #                                          relevance -- wrong by a factor of 25.
        #   "the semantic engine runs ~80x higher" the 80x compared two `relevance` values across
        #                                          engines. Wave 8 changed the similarity lane's
        #                                          score to the chunk COSINE, so the model now sees
        #                                          0.56-0.80 against a term score of 3.55-7.34:
        #                                          term is ~9x HIGHER, not 80x lower.
        #
        # What survives needs no number and cannot go stale.
        notes.append("relevance here is a term score, local to this source and this query. Its "
                     "spread varies with the population, so it is not a fixed scale; and it may "
                     "never be compared with a similarity score, which is a cosine in 0..1 and a "
                     "different quantity. Every result names the engine that produced it")

    search: dict[str, Any] = {
        F_DATASOURCES: [source],
        F_PROJECT: cache.get("project"),
        # NEVER a `_knn`/`_hybrid` variant on the conditional endpoint: they are measured
        # to execute silently as keyword, so the request would claim a semantic lane it did
        # not use.
        # `basic_knn` on the semantic path, plain `conditional` otherwise. NEVER a
        # conditional `_knn`/`_hybrid` variant: those execute silently as keyword.
        F_MODE: MODE_BASIC["knn"] if engine == "basic" else MODE_CONDITIONAL["keyword"],
        F_LIMIT: limit,
        F_OFFSET: offset,
        F_FIELDS: extract,
        # Exactly this source's program filter and nothing else. A multi-source payload
        # never carries one.
        F_FORM_PARAMS: dict(program_filter),
    }
    # `is not None`, not a string comparison. Relevance is the API's default and emits no
    # sort key at all; with the sentinel now `None` rather than a fake wire value, comparing
    # against the string would have emitted `sort: None`.
    if sort_value is not None:
        search["sort"] = sort_value
    if engine == "basic":
        # `question` routes to `knnSearchQuery` -- that is the text the vector lane embeds.
        # `query` carries the same text so the term lane is comparable, and the response
        # reports `totalHitsKnn` separately from `totalHits`.
        search[F_QUERY] = query_text
        search["knnSearchQuery"] = query_text
    else:
        # A COPY. `plan["conditions"]` and the wire list were previously the same object, so
        # anything wave 4 did to one silently mutated the request record the count has to be
        # re-derivable from.
        search[F_CONDITION] = {"conditions": [
            {k: v for k, v in c.items() if not k.startswith("_")} for c in conditions]}

    plan: dict[str, Any] = {
        "refused": False,
        "source": source,
        "engine": engine,
        # Only the conditional engine yields a count that is a pure function of the
        # request. On basic, rules, synonym tables and query rewriting all touch it.
        # NOT defensible when an exact constraint covered only part of what it named. Measured on
        # med_comms: `where status exact "Final"` ran against `activity_status` alone because
        # `doc_status` has no `.raw`, returned 0, and claimed `defensible: True` with
        # `is_absence: True` -- while 15 of 19 records' `doc_status` began "Final". The engine is
        # the right default for this field; a partial constraint overrides it.
        "count_defensible": (engine == "conditional"
                             and not any(c.get("_partial_exact")
                                         for c in (conditions or []))),
        "partial_exact_constraint": any(c.get("_partial_exact")
                                        for c in (conditions or [])),
        "endpoint": EP_QUERY if engine == "basic" else EP_CONDITIONAL,
        "search": search,
        "program_filter": dict(program_filter),
        "conditions": conditions or [],
        # Whether any POSITIVE constraint scopes this population beyond the program filter.
        # False means the result describes the source's whole program population, which is a
        # legitimate request (`00-surfaced-tools.md` section 7) but must never be reported as
        # narrowed -- that confusion is the restatement failure this whole design removes.
        #
        # Counted over ALL conditions including a handle's recompiled recipe. An earlier
        # version counted only `type == MUST` on a list that held the handle's clauses in
        # model vocabulary (no `type` at all), so narrowing a 19-record population reported
        # `narrowed: False` and stated in prose that it was the unnarrowed whole population.
        "narrowed": bool([c for c in (conditions or [])
                          if c.get("type") == COND_TYPES["must"]]),
        # Whether THIS call added a constraint on top of an inherited recipe. Distinct from
        # `narrowed`: re-running a handle unchanged is narrowed-but-not-narrowed-further, and
        # wave 4 has to be able to tell those apart.
        "narrowed_further": bool(inherited) and bool(
            about is not None or where or where_not),
        # Wave 2's three signals, carried rather than dropped. `_validated` False with a note
        # means the scope checks could not run, which is NOT the same as passing them; and a
        # handle minted before the 2026-08-25 meds reindex records a count of 1,014 where the
        # live population is 799, so staleness has to travel with the recipe.
        "inherited": ({
            "count_at_mint": (inherited or {}).get("count"),
            "as_of": (inherited or {}).get("as_of"),
            "validated": (inherited or {}).get("_validated"),
            "validation_note": (inherited or {}).get("_validation_note"),
            "staleness": population_staleness(inherited),
        } if inherited else None),
        # Fact about our own startup call, not about the source. Wave 4 must label a result
        # built against an unretrieved mapping as best-effort field validation.
        # True when the qualification fields could not be resolved, so wave 4 must not
        # present these records as qualifiable.
        "default_fields_unresolved": default_fields_unresolved,
        # `broad` on signals: the filter admits other programs' records, so exact program
        # relevance still has to be established per record.
        "ordered_by": sort_field,
        "notes": notes,
        # OURS, and reported as ours. No date expression works as a condition: every range
        # syntax is a 500 and a plain date returns 0. So this is a post-filter wave 4
        # applies over a complete retrieval, and it must report BOTH counts.
        "date_filter": ({"since": since, "until": until, "applied_by": "connector",
                         "reason": "no date expression is usable as a condition on this "
                                   "API; range syntax returns HTTP 500 and a plain date "
                                   "returns 0 records",
                         "report": "both the count MUSE matched and the count that "
                                   "survived this filter"}
                        if (since or until) else None),
        "synonym_expansion": (
            "none applied; alternatives are stated explicitly through `any_of` so the "
            "count stays a pure function of the request. The API reports a `synonyms` map "
            "on this endpoint describing an expansion it did not perform -- it is not to "
            "be believed." if engine == "conditional" else
            "server-side expansion applies on this engine and is not itemised in the "
            "response"),
    }

    facet_plan, facet_refusal = _plan_facets(
        cache, source, conditions=conditions, distribution=distribution,
        value_prefix=value_prefix, engine=engine, query_text=query_text)
    if facet_refusal:
        # WAVE 8. A `value_prefix` a SOURCE cannot serve degrades the value lookup, not the
        # search. Measured before this: `value_prefix={"status": "Eff"}` across all five sources
        # returned `sources_searched: [meds, med_comms, signals]` with `scited` and `mrl_slides`
        # in `excluded_before_searching` -- neither represents a `status` role, so a facet-side
        # capability gap removed two sources' RECORDS AND COUNTS from the answer. The model asked
        # to see a field's values and silently lost two fifths of its coverage.
        #
        # Same principle the distribution already follows: budget exhaustion during the facet
        # call degrades the distribution and leaves the search intact.
        #
        # THREE causes degrade, not four. `value_prefix_unknown_field` is what `_resolve_role`
        # returns for a name that is neither a role, a taxonomy role, nor a field on this source --
        # i.e. a TYPO, which is a defect in the request and not a property of the source. Measured:
        # `value_prefix={"stattus": "Eff"}` degraded while `where=[{"in": ["stattus"], ...}]`
        # refused, so the same typo got two treatments; and the degrade carried
        # `_resolve_role`'s own sentence "so it is refused instead" directly beside "this source
        # was still searched". Two contradictory statements about one request.
        #
        # `value_prefix_unfillable`, `value_prefix_unknown_field_shape` and
        # `value_prefix_not_facetable` ARE per-source capability gaps: the name is meaningful, this
        # source cannot serve it. Those degrade, so a facet-side gap cannot cost the records.
        #
        # `bad_value_prefix` and `bad_distribution` are malformed arguments, and also refuse:
        # silently searching past a caller error is how a wrong request returns a right-looking
        # answer.
        # The `_vp_*` tags exist for the degrade branch below to read. On every other cause the
        # refusal goes straight to the model carrying them -- and this module's convention is that
        # `_`-prefixed keys are internal, enforced with `not k.startswith("_")` where conditions go
        # on the wire. `PLAN_INTERNAL_KEYS` cannot catch these because they are not in that set.
        cause = str(facet_refusal.get("cause") or "")
        _vp_meta = {k: facet_refusal.pop(k) for k in list(facet_refusal)
                    if str(k).startswith("_vp_")}
        if cause in ("value_prefix_unfillable", "value_prefix_unknown_field_shape",
                     "value_prefix_not_facetable"):
            plan.update(facet_plan)
            # The refusal's own `message` ends "so it is refused instead", which is now false on
            # this path -- the request was NOT refused, only the value lookup was skipped. Trimmed
            # rather than passed through, and `remedy` omitted when there is none rather than
            # emitted as null.
            detail = str(facet_refusal.get("message") or "")
            for tail in ("so it is refused instead. ", "so it is refused instead.",
                         "so nothing is sent rather than guessing.",
                         "It is excluded here rather than sent, "):
                detail = detail.replace(tail, "")
            entry: dict[str, Any] = {
                "cause": cause,
                # The field the CAUSE is about, not every field asked for. `_plan_facets` returns
                # on the first failing entry, so listing all of them told the model that a field
                # which had succeeded was unavailable, and that one never attempted was too --
                # with a `detail` belonging to a third.
                "field": _vp_meta.get("_vp_field"),
                "fields_not_attempted": _vp_meta.get("_vp_not_attempted") or [],
                "detail": detail.strip(),
                "note": ("this source was still searched; only the value lookup could not be "
                         "performed here. Its records and count below are complete"),
            }
            if facet_refusal.get("remedy"):
                entry["remedy"] = facet_refusal["remedy"]
            plan["value_prefix_unavailable"] = entry
            return plan
        return facet_refusal
    plan.update(facet_plan)
    return plan


def _plan_facets(
    cache: dict[str, Any], source: str, *, conditions: list[dict[str, Any]] | None,
    distribution: Any, value_prefix: dict[str, str] | None, engine: str,
    query_text: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Facet payloads for one source: the distribution, and any `value_prefix` requests.

    `engine` and `query_text` are load-bearing, not decoration. On the basic engine there
    are no `conditions`, so an earlier version built the facet request as if the population
    were unconstrained -- the records were the query hits while the distribution described
    the WHOLE program population, inside one call, labelled `filtered` as though the two
    matched. That is the canonical 135 / 3,832 / 1,014 failure this design exists to remove.
    """
    src = (cache.get("sources") or {}).get(source) or {}
    # `facet_scope` names FOUR distinct states, because `None` previously meant both "not
    # requested" and "requested but the poison predicate is unknown", and wave 4 could not
    # tell them apart from this field alone.
    out: dict[str, Any] = {"facets": None, "facet_scope": "not_requested", "poison": None,
                           "value_prefix_requests": []}
    program_filter = src.get("program_filter") or {}
    filter_field = next(iter(program_filter), "")
    facetable = src.get("facetable_program_filter")

    # --- distribution ---------------------------------------------------------------
    # Identity test, not truthiness: `0`, `None` and `""` are not `False` and previously all
    # produced a full distribution.
    if distribution is not False:
        if not isinstance(distribution, (bool, list)):
            return out, _refuse(
                "bad_distribution",
                f"`distribution` must be True, False, or a list of field names, got "
                f"{type(distribution).__name__}.",
                defect_in=("field_validity",))
        shown, dnotes, unknown = _resolve_distribution_request(src, distribution)
        # Wave 4's presentation filter, recorded rather than sent: the API has no field
        # selector, so which fields are SHOWN is our choice and the result must say so.
        out["distribution_fields_requested"] = shown
        out["distribution_unknown_fields"] = unknown
        out["facet_notes"] = dnotes
        if facetable is None:
            # UNKNOWN, not False. Without the unfiltered facet call the poison predicate
            # cannot be computed, and answering it confidently either way would be a
            # plausible value where the truth was never read.
            out["poison"] = {
                "facetable_program_filter": None,
                "detail": (f"whether {source}'s program filter field is facetable is "
                           "unknown -- the startup facet call for this source did not "
                           "complete, and that call is the only place the answer exists. "
                           "A distribution is not requested, because a poisoned request "
                           "returns HTTP 200 with one field, which reads as sparse data "
                           "rather than as a broken request."),
            }
            out["facet_scope"] = "unknown"
        elif facetable is False:
            # The poison rule. A `formParams` filter on a field the endpoint computes no
            # facet for silently zeroes the WHOLE facet computation -- one such field
            # poisons the request and a valid field alongside it does not rescue it.
            out["poison"] = {
                "facetable_program_filter": False,
                # The field NAME stays internal: `01` section 1e classifies the per-source
                # filter field and literal as mechanics to hide, and naming it in this text
                # leaked it into the model-facing distribution.
                "_filter_field": filter_field,
                "detail": (f"the field {source} is scoped by is not one this endpoint "
                           "computes a facet for, so filtering on it returns a single field "
                           "instead of a distribution -- at HTTP 200, reading as sparse data "
                           "rather than as a broken request. The exact population therefore "
                           "cannot be faceted here."),
            }
            # The superset path, offered explicitly and labelled. `query` scoping does not
            # poison. It is a DIFFERENT population -- on meds 3,832 documents that mention
            # the program against 1,014 whose product IS the program -- so it is never
            # substituted silently, and it is unavailable at all when conditions are
            # present because `query` is dropped whenever `conditionQuery` is.
            if conditions:
                out["facet_scope"] = "unavailable"
                out["poison"]["superset_available"] = False
                out["poison"]["superset_detail"] = (
                    "the query-scoped superset is also unavailable for this request: "
                    "`query` is silently dropped whenever conditions are present "
                    "(measured on both the search and facet endpoints), so a conditioned "
                    "population on this source can be exactly scoped or faceted, never "
                    "both.")
            else:
                literals = [v[0] for v in program_filter.values()
                            if isinstance(v, list) and v and isinstance(v[0], str)]
                literal = literals[0] if literals else None
                if literal is not None:
                    # This lands in `query`, where `?` drives the result to 0 -- the basic
                    # endpoint's sanitiser, not the conditional one.
                    literal, changed = _sanitize_query_text(literal)
                    if changed:
                        out["superset_literal_sanitised"] = True
                    if not literal.strip():
                        literal = None
                if literal:
                    out["facet_scope"] = "superset"
                    out["poison"]["superset_available"] = True
                    out["poison"]["superset_detail"] = (
                        "this distribution describes a DIFFERENT and larger population than "
                        "the records: it is scoped by the program literal as free text "
                        "rather than by the program filter field, so it includes documents "
                        "that merely mention the program alongside those the program owns. "
                        "Measured on meds, the live figures reported beside this note. Report BOTH counts and never "
                        "substitute one for the other.")
                    out["facets"] = {
                        F_DATASOURCES: [source], F_PROJECT: cache.get("project"),
                        F_QUERY: literal,
                        "counts": _counts_for(src),
                    }
                else:
                    out["facet_scope"] = "unavailable"
                    out["poison"]["superset_available"] = False
        else:
            out["poison"] = {"facetable_program_filter": True, "_filter_field": filter_field,
                             "detail": f"the field {source} is scoped by is a facet field "
                                       "here, so filtering on it does not suppress the "
                                       "distribution."}
            payload: dict[str, Any] = {
                F_DATASOURCES: [source], F_PROJECT: cache.get("project"),
                F_FORM_PARAMS: dict(program_filter),
                "counts": _counts_for(src),
            }
            if engine == "basic":
                # Scope the distribution to the SAME query the records came from. `query` is
                # only dropped when `conditionQuery` is present, and on this path there is
                # none, so it applies.
                out["facet_scope"] = "query_scoped"
                payload[F_QUERY] = query_text
                out["facet_population_note"] = (
                    "this distribution is scoped by the same query text as the records, but "
                    "the semantic engine's record set is ranked by embedding similarity "
                    "while the facet count is a term-scoped count -- so the distribution "
                    "describes the term-matching population, not the similarity-ranked one")
            else:
                out["facet_scope"] = "narrowed" if conditions else "filtered"
                if conditions:
                    payload[F_CONDITION] = {"conditions": [
                        {k: v for k, v in c.items() if not k.startswith("_")}
                        for c in conditions]}
            out["facets"] = payload

    # --- value_prefix ---------------------------------------------------------------
    # `searchField` selects exactly one facet and prefix-searches its values, so N prefixes
    # are N requests. The prefix goes in `formParams` under the SAME field name -- naming
    # `searchField` without it is HTTP 400.
    # WAVE 8. The TYPE of `value_prefix` itself, before iterating it. The per-entry check below
    # guarded the keys and values; `(value_prefix or {}).items()` on a bare string raised
    # `AttributeError: 'str' object has no attribute 'items'` straight out of the planner, so a
    # caller error escaped as an MCP protocol error instead of a named refusal stating nothing
    # was executed. `distribution` has had this guard since wave 3 (`bad_distribution`); this
    # parameter did not, and nothing exercised it because nothing executed the requests at all.
    if value_prefix is not None and not isinstance(value_prefix, dict):
        return out, _refuse(
            "bad_value_prefix",
            f"`value_prefix` maps one field name to one prefix string, and this request passed "
            f"a {type(value_prefix).__name__} instead. Nothing was sent: iterating it would "
            f"raise rather than refuse, and a crash carries no statement about what did or did "
            f"not execute.",
            defect_in=("field_validity",),
            remedy='One entry per field, e.g. {"status": "Eff"}.')
    tax_roles_here = src.get("taxonomy_roles") or {}
    vp_items = list((value_prefix or {}).items())
    for _vp_i, (raw_field, prefix) in enumerate(vp_items):
        # Which field this refusal is about, and which were never reached. Every `_refuse` below
        # gets both, because the caller cannot tell otherwise: the loop returns on the first
        # failure, and the degrade path used to list every field the request named.
        _vp_tag = {"_vp_field": raw_field if isinstance(raw_field, str) else None,
                   "_vp_not_attempted": [str(k) for k, _ in vp_items[_vp_i + 1:]]}
        if not isinstance(raw_field, str) or not isinstance(prefix, str):
            return out, _refuse(
                "bad_value_prefix",
                "`value_prefix` maps a field name to a prefix string.",
                defect_in=("field_validity",), **_vp_tag)
        resolved, err = _resolve_role(src, raw_field)
        if err:
            return out, _refuse("value_prefix_unknown_field", err,
                                defect_in=("field_validity",), source=source, **_vp_tag)
        if not resolved:
            return out, _refuse(
                "value_prefix_unfillable",
                f"{source} does not represent {raw_field!r}, so its values cannot be "
                "searched there.",
                defect_in=("field_validity",), source=source, **_vp_tag)
        # Check EVERY field the role resolves to, not just the first. `status` resolves to
        # both `doc_status` and `activity_status` on med_comms, and `searchField` on the
        # wrong one is HTTP 500 -- the design's own named example of a failure reachable
        # through ordinary use. Taking `resolved[0]` picked it.
        usable = [f for f in resolved
                  if ((src.get("fields") or {}).get(f) or {}).get("has_raw") is True]
        unknown_raw = [f for f in resolved
                       if ((src.get("fields") or {}).get(f) or {}).get("has_raw") is None]
        if not usable and unknown_raw:
            return out, _refuse(
                "value_prefix_unknown_field_shape",
                f"whether {', '.join(unknown_raw)} on {source} can have its values searched "
                "is UNKNOWN -- this source's field mapping was never retrieved this session. "
                "`searchField` on a non-facet field returns HTTP 400 or 500, so nothing is "
                "sent rather than guessing.",
                defect_in=("field_validity",), source=source, **_vp_tag)
        field = usable[0] if usable else resolved[0]
        if usable and len(resolved) > 1:
            skipped = [f for f in resolved if f != field]
            if skipped:
                # WAVE 8. `_role_of` maps every field of a multi-field role back to the SAME
                # role, so this read "'status' covers more than one field ... Not searched:
                # status" -- naming the role as both searched and not searched, and giving the
                # model nothing to act on, since `in` and `value_prefix` both take the role. The
                # same degenerate shape as the "cannot share a clause with []" message wave 8
                # fixed in `compile_clause`. Say why the others could not be searched instead of
                # emitting wire names as though they were addressable.
                why = ("the rest are not facet fields on this source, so their values cannot be "
                       "searched here"
                       if any(not (((src.get("fields") or {}).get(f) or {}).get("has_raw"))
                              for f in skipped)
                       else "this endpoint returns exactly one field's values per request")
                out.setdefault("facet_notes", []).append(
                    f"{raw_field!r} covers {len(resolved)} fields on {source}, and the values "
                    f"reported come from one of them: {why}. A value from this list is exact for "
                    f"the field it came from, and constraining on {raw_field!r} matches across "
                    "all of them.")
        meta = (src.get("fields") or {}).get(field) or {}
        # Measured across 28 cells on all five sources: `searchField` returns 200 only for
        # a field in the source's facet set, 400 for a computed-but-undeclared field, and
        # 500 otherwise. Both failures are LOUD, so this converts a crash into a statement.
        # `has_raw`, `facetField` and the declared list are the SAME set on every source
        # (55/51/6/11/21), so any of them serves as the predicate.
        if not meta.get("has_raw"):
            candidates = sorted(f for f, m in (src.get("fields") or {}).items()
                                if m.get("has_raw"))
            return out, _refuse(
                "value_prefix_not_facetable",
                f"{field!r} is not a facet field on {source}, so searching its values "
                "returns HTTP 400 or 500 rather than a result. This is reachable through "
                "ordinary use -- `doc_status` is a facet field on meds and not on "
                "med_comms -- so it is checked per source rather than assumed.",
                defect_in=("field_validity",), source=source,
                remedy=(f"Facet fields here: {', '.join(candidates[:12])}"
                        + (f", and {len(candidates) - 12} more." if len(candidates) > 12
                           else ".")), **_vp_tag)
        raw_name = f"{field}.raw"
        req: dict[str, Any] = {
            F_DATASOURCES: [source], F_PROJECT: cache.get("project"),
            "searchField": raw_name,
            F_FORM_PARAMS: {**program_filter, raw_name: [prefix]},
            "counts": {raw_name: _COUNTS_CEILING},
        }
        out["value_prefix_requests"].append({
            "field": field,
            # TAXONOMY, decided HERE. `_execute_plan` tried to re-derive this and tested the wire
            # field name against `taxonomy_roles`' ROLE keys -- `"mmd_tag_issue" in {"issue":
            # "mmd_tag_issue"}` -- which is always False, so the refusal it guarded never executed
            # and 7 raw L-paths shipped with `complete: True` certifying them. The planner already
            # holds both halves: `raw_field` is what the caller named, `field` is what it resolved
            # to. Deciding it once, where both are in scope, removes the drift.
            "taxonomy": raw_field in tax_roles_here or field in tax_roles_here.values(),
            "prefix": prefix,
            # `searchField` is NOT subject to the poison rule -- measured, it returns the
            # requested field under meds' own program filter -- so this uses the exact
            # population and needs no superset path.
            "poisoned": False,
            "payload": req,
            "match": "prefix only; a mid-string substring matches nothing",
            # A single term's count is a count of records carrying that value, and the
            # values of a MULTI-VALUED field do not partition the population -- measured on
            # `author.raw`, where the counts sum well above the population. So a term count
            # is not a share of the population unless the field is single-valued, and
            # nothing in the response says which it is.
            #
            # An earlier version of this caveat claimed something stronger and FALSE: that
            # a term count cannot be trusted against the population at all, citing
            # `doc_status.raw` = 'Effective' returning 799 where a condition returned 1,014.
            # That was not an API anomaly -- the meds index shrank ~20% in the 2026-08-25
            # reindex and the 1,014 was a stale recorded figure. Live, both are 799 and they
            # agree exactly. Withdrawn rather than left as a plausible-sounding warning.
            "counts_caveat": (
                "a term count is the number of records carrying that value. Values of a "
                "multi-valued field do not partition the population and can sum above it, "
                "and the response does not say whether this field is multi-valued."),
        })

    return out, None


# High enough that the `+` overflow bucket does not appear for any field measured so far
# (`counts: 1000` sufficed everywhere tried, and 3000 on all ten taxonomy fields). The
# ABSENCE of that bucket is the one signal on this endpoint that can be trusted
# positively -- it certifies the value list is complete -- so depth is not a model-facing
# choice and truncation is reported rather than hidden. Whether `counts` has a ceiling on a
# field with tens of thousands of values is unmeasured.
_COUNTS_CEILING = 3000


def _counts_for(src: dict[str, Any]) -> dict[str, int]:
    """`counts` for EVERY computed field, high enough to avoid the `+` bucket.

    `counts` is a DEPTH control, not a field selector. Measured: naming one field and naming
    three both return the same 35 fields on meds, and the only field selector that exists is
    `searchField`. So a subset here does not narrow the response -- it leaves every unnamed
    field at DEFAULT depth, which forfeits the `+`-bucket completeness certificate for the
    whole response.

    An earlier version derived this from the model's `distribution` list, which both misused
    the parameter and silently dropped entries. `distribution` is wave 4's presentation
    filter; it is recorded in the plan, not pushed onto the wire.
    """
    computed = src.get("computed_facets")
    if not computed:
        # `None` is UNKNOWN and `[]` is "none computed". Neither supports naming fields, and
        # an empty `counts` map is preferable to inventing names -- the endpoint then uses
        # default depth and the response reports truncation per field.
        return {}
    return {n: _COUNTS_CEILING for n in computed}


def _resolve_distribution_request(
    src: dict[str, Any], requested: Any,
) -> tuple[list[str] | None, list[str], list[str]]:
    """Resolve a `distribution` list to computed facet names. Returns (fields, notes, unknown).

    Roles are resolved the same way `Clause.in` resolves them, because `00` section 8 tells
    the model to ask for `issue` -- a taxonomy ROLE -- and the computed facet is
    `mmd_tag_issue.raw`. An earlier version appended `.raw` to whatever it was given, so the
    documented call produced `issue.raw`, a field that does not exist.
    """
    if not isinstance(requested, list):
        return None, [], []
    resolved: list[str] = []
    unknown: list[str] = []
    notes: list[str] = []
    computed = set(src.get("computed_facets") or [])
    for entry in requested:
        if not isinstance(entry, str) or not entry.strip():
            unknown.append(str(entry))
            continue
        fields, err = _resolve_role(src, entry)
        candidates = fields or ([entry[:-4]] if entry.endswith(".raw") else [entry])
        hit = next((f"{f}.raw" for f in candidates if f"{f}.raw" in computed), None)
        if hit:
            resolved.append(hit)
        else:
            unknown.append(entry)
    if unknown:
        notes.append(
            f"requested distribution fields not among {src['id']}'s computed facets, so no "
            f"values can be shown for them: {', '.join(sorted(unknown))}. Reported rather "
            "than dropped silently, because the response will simply not contain them.")
    return resolved, notes, unknown


# Keys a PLAN carries for the connector's own use. They hold the program filter field and
# literal, the raw wire payloads, and the endpoint -- all of which `01` section 1e classifies
# as mechanics to hide, because surfacing them invites the model to reason about encoding
# instead of about evidence. Named here rather than left as a comment so the contract is
# testable.
#
# ENFORCED BY CONSTRUCTION, not by a filter. `_execute_plan` builds a fresh result dict and
# reads named fields off the plan; the plan is never spread into the result. Wave 7 deleted
# the `_strip_internal` filter that was supposed to enforce this: it had ZERO call sites from
# the day it was written, so the promise in its docstring was kept by the assembler's shape
# and by nothing else. The assertion that matters is therefore over a real result -- see
# `response_assembly_check.py` section 16, which iterates this set against `repr(result)`.
PLAN_INTERNAL_KEYS = frozenset({
    "search", "facets", "endpoint", "program_filter", "value_prefix_requests",
    "conditions",
})


def plan_population(
    cache: dict[str, Any], *,
    sources: list[str] | None = None, about: str | None = None,
    where: list[Any] | None = None, where_not: list[Any] | None = None,
    since: str | None = None, until: str | None = None,
    population: str | None = None,
    semantic: bool = False, question: str | None = None,
    order: str = "relevance", depth: str = "complete", offset: int = 0,
    fields: list[str] | None = None, distribution: Any = True,
    value_prefix: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Fan out `muse_population`'s fifteen parameters into per-source plans, or refuse.

    EXACTLY the fifteen parameters in `00-surfaced-tools.md` section 2. There is no
    sixteenth: `read_top` was superseded because it required this tool to invoke
    `muse_read` internally, contradicting the rule that neither tool calls the other.

    A partial result is the NORMAL case, not an exception: one source can be denied or
    have a broken filter literal while the others succeed. So a per-source refusal is
    recorded against that source and the others still plan.
    """
    if not isinstance(cache, dict) or not cache.get("sources"):
        return _refuse(
            "no_program_scope",
            "The program scope and source metadata could not be loaded, so no request can "
            "be built: field names cannot be validated, the program filter is unknown, and "
            "source access is unknown. Every one of those produces 0 records at HTTP 200 "
            "when it is wrong.",
            defect_in=("source_access", "filter_integrity", "field_validity"),
            remedy="This is a connector startup failure, not a question about the "
                   "program. Report it as a limitation.")

    in_scope = list(cache.get("in_scope") or cache.get("sources") or {})
    inherited: dict[str, Any] | None = None
    if population is not None:
        payload, err = decode_population_handle(population)
        if err or payload is None:
            return _refuse(
                "bad_population_handle", err or "the population handle could not be read",
                defect_in=("filter_integrity",),
                remedy="Re-run the search that produced it; a handle carries the exact "
                       "recipe, so a damaged one is refused rather than half-applied.")
        inherited = payload
        # The handle names its own source. Honouring a different `sources` argument would
        # silently describe a different population under a handle minted for another.
        if sources and list(sources) != [payload["source"]]:
            return _refuse(
                "handle_source_conflict",
                f"this population was measured on {payload['source']!r}, but "
                f"`sources={sources!r}` was also given. Narrowing applies to the "
                "population the handle names; searching a different source is a new "
                "population, not a narrowing of this one.",
                defect_in=("filter_integrity",),
                remedy=f"Drop `sources`, or omit `population` to search {sources!r} fresh.")
        targets = [payload["source"]]
    elif sources is None:
        targets = in_scope
    else:
        if not isinstance(sources, list) or not sources:
            return _refuse("bad_sources", "`sources` must be a non-empty list of source ids, "
                                          "or omitted to search everything in scope.",
                           defect_in=("field_validity",))
        # De-duplicated, order preserved: two identical entries would otherwise produce two
        # identical plans and wave 4 would report the source twice.
        targets = list(dict.fromkeys(str(x) for x in sources))

    if not targets:
        return _refuse(
            "no_sources_in_scope",
            "No source is in scope for this program, so there is nothing to search.",
            defect_in=("source_access",))

    plans: list[dict[str, Any]] = []
    for source in targets:
        plans.append(plan_source_request(
            cache, source, about=about, where=where, where_not=where_not, since=since,
            until=until, semantic=semantic, question=question, order=order, depth=depth,
            offset=offset, fields=fields, distribution=distribution,
            value_prefix=value_prefix,
            inherited=inherited if inherited and inherited["source"] == source else None))

    usable = [p for p in plans if not p.get("refused")]
    if not usable:
        per_source = [{"source": t, **p} for t, p in zip(targets, plans)]
        # Surface the SPECIFIC reason rather than a generic wrapper. Collapsing N refusals
        # into "no source could be searched" would throw away the diagnosis, which is the
        # exact failure this layer exists to prevent -- and for a single-source request it
        # would replace a precise, actionable refusal with an unusable one.
        causes = {p.get("cause") for p in plans}
        if len(causes) == 1:
            shared = dict(plans[0])
            if len(targets) > 1:
                shared["message"] = (
                    f"None of {', '.join(targets)} could be searched, for the same reason. "
                    + shared["message"])
            shared["per_source"] = per_source
            return shared
        return _refuse(
            "no_source_could_be_planned",
            "No requested source could be searched, and the sources failed for DIFFERENT "
            "reasons. Each one is listed separately -- they are different findings about "
            "different sources and collapsing them into one would lose that.",
            defect_in=("source_access", "filter_integrity", "field_validity"),
            per_source=per_source)

    return {
        "refused": False,
        # N sources are N requests, and that is forced rather than chosen: `formParams`
        # keys AND together globally, so one request cannot carry per-source filters.
        "requests": usable,
        "excluded": [{"source": t, "cause": p.get("cause"), "message": p.get("message")}
                     for t, p in zip(targets, plans) if p.get("refused")],
        "engine": usable[0]["engine"],
        "narrowing": population is not None,
        # Never merged: counts are per source, relevance is per source and per query with no
        # fixed range -- the design-phase "~4x gap between meds and med_comms" measured 1.6-1.8x
        # on three queries in 2026-08-26, so the magnitude is not a stable fact and the
        # incomparability does not need one -- and record shapes share only 7 of 58 keys.
        "grouping": "per source; counts, relevance and record shapes are never merged",
    }


# --------------------------------------------------------------------------
# Response assembly  (group BUILD wave 4)
# --------------------------------------------------------------------------
# Executes wave 3's plans and turns the responses into the model-facing result. This is the
# first wave whose output the model actually sees, so the discipline shifts: wave 3 was about
# not sending a wrong request, this is about not making a claim the response does not support.
#
# The response envelope is NINE keys, identical on both endpoints (measured on the same
# population): cards · datasourceErrors · hints · query · queryId · searchStrategy ·
# synonyms · totalHits · totalHitsKnn. Two of them actively mislead:
#
#   `synonyms`   describes an expansion the CONDITIONAL endpoint did not perform, byte for
#                byte identical to what basic returns and applies. A reader who trusts it
#                believes six aliases were searched when one term was. Proof: basic with
#                `disableSynonyms: true` reproduces the conditional number exactly.
#   `datasourceErrors` is `[]` even for a DENIED datasource returning 0 records, so it can
#                never attribute a zero. Attribution comes from the access map.
#
# Design sources, and they outrank this code:
#   01-muse-search.md              section 4 (response handling, the two grouping axes)
#                                  section 5 (five zero-result steps) · section 6 (in-band)
#   02-muse-search-conditional.md  section 8b (the returned record, locators, no server
#                                  signal) · section 10 (envelope, the synonyms defect)
#                                  section 14 · section 15 (SEVEN zero-result steps)
#                                  section 16 (in-band)
#   06-muse-facets.md              section 9 (emission) · section 10 (three causes of an
#                                  empty facet) · section 11 (in-band)
#   00-surfaced-tools.md           section 7 (size, delivery) · section 8 (ten distribution
#                                  keys out, the undecodable-value rule)

# `relevance` and `chunkScore` are different SCALES and different EVIDENTIAL KINDS: 2373
# unnormalised term score against 0.837 cosine similarity. A term match is checkable -- the
# word is in the document. A similarity match means the embedding of this chunk is near the
# embedding of the query, a judgement made by a model at dimension 384. Ordering them
# together is arithmetically meaningless and epistemically worse.
LANE_TERM = "term"
LANE_SIMILARITY = "similarity"

# Kept verbatim so the model is never handed a number without its kind.
_SCORE_KINDS = {
    LANE_TERM: ("relevance", "an unnormalised term score, comparable only within this "
                             "source, this query and this engine"),
    LANE_SIMILARITY: ("chunkScore", "a cosine similarity in 0..1 between the query "
                                    "embedding and this chunk, not comparable with a term "
                                    "score"),
}

# Retrieval identifiers, excluded from every model-facing record. `00-surfaced-tools.md`
# section 5: "The model never sees `card.id` or `body.document_id`, which removes that trap by
# construction rather than by discipline" -- the trap being that the two are different keys and
# passing the wrong one to the read endpoint 404s. A document handle is the seam instead.
#
# Measured 2026-08-25: guarding only `card.id` was not enough. `scited` carries `id` and
# `med_comms` carries `muse_raw_id` inside the card BODY, so both reached the model through
# `source_fields` and the trap was back, one field over.
#
# `current_version_doc_id` is deliberately NOT here: the design names it among the
# represented-state fields a qualification decision needs, and it is a version pointer rather
# than this document's retrieval key.
_RETRIEVAL_IDS = frozenset({"id", "document_id", "muse_raw_id", "documentId", "card_id"})

# `<em class="hlt1">` and friends. Stripped rather than passed through: the markup is a UI
# concern and a model reading it as content would quote the tags.
_HLT_MARKUP = re.compile(r"</?em[^>]*>")

# A locator that still contains its own template markers was never interpolated. mrl_slides
# renders `?link?`, and passing that through would MANUFACTURE a citation -- a reference that
# looks resolvable and is not.
_UNINTERPOLATED = re.compile(r"\?[A-Za-z0-9_]+\?|\{[A-Za-z0-9_]+\}")

# Per-source locator granularity. These are NOT interchangeable for citation: a slide, the
# deck containing it, and the .pptx file are different referents, and `01` section 1d
# measured all three populated on mrl_slides. The granularity travels with the value.
_LOCATOR_GRANULARITY = {
    "slide_source_url": "the specific slide",
    "deck_url": "the deck containing the slide, not the slide itself",
    "slide_url": "the raw .pptx file, not a viewable page",
    "webview_pdf_url": "the document PDF",
    "link": "the source record",
    "citation_link": "the citation, not the document",
}

# The strategies that mean the server RELAXED the query. They say nothing about whether the
# relaxation found anything, and must be read together with the hit count -- measured,
# `__ZZZ_NOPE__` returned 2 hits under exactly these while a longer impossible token returned
# 0 under the same two. So this is not a stronger absence claim, and not a weaker one either.
_RELAXED = ("FULLTEXT_ZERO_RESULTS_PREFIX", "FULLTEXT_ZERO_RESULTS_FUZZINESS")
_MUTILATED = ("FULLTEXT_WRONG_SYNTAX_REMOVED",)

_UNNAMED_TAXONOMY = "<unnamed taxonomy value>"

# Both of these were declared down in the old tools' section and used up here. Wave 7 moved
# them; the old tools are gone. `ast.parse` accepted the file without them, because an
# unresolved module-level name is a RUNTIME NameError -- three harnesses caught it, a syntax
# check could not.
_NARROW_AT = 200   # above this, paginating is the wrong instinct -- narrow instead

# `AGENTS.md` section 11 and `01` section 8: tool use creates no work, artifact, or program
# ground. Stated on every successful result rather than left to be remembered.
_TRANSIENCE = (
    "Raw retrieval is transient dynamic context. It creates no work, artifact, or "
    "program ground, and establishes no applicability, materiality, sufficiency, "
    "independence, currentness, review, or authority."
)


def _strip_markup(value: Any) -> Any:
    if isinstance(value, str):
        return _HLT_MARKUP.sub("", value)
    if isinstance(value, list):
        return [_strip_markup(v) for v in value]
    return value


def _highlights(card: dict[str, Any]) -> dict[str, Any]:
    """Every highlight key, markup stripped.

    ITERATED, never hardcoded. The key is `text` on four sources and `scited` returns no
    highlight map at all (observed twice) -- so hardcoding `"text"` discards nothing on four
    sources and everything on one. A single fragment was measured at 1,009 characters;
    capping its size is `ECONOMY - DEFERRED` and is not done here.
    """
    raw = card.get("highlightFragments") or card.get("highlights") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): _strip_markup(v) for k, v in raw.items() if v}


def _locator(card: dict[str, Any], src: dict[str, Any]) -> dict[str, Any]:
    """Resolve a citable locator, or refuse -- with its granularity named.

    Refusing is the point. `01` section 1d measured mrl_slides rendering `?link?` because the
    source does not populate its own template field, and emitting that would manufacture a
    citation.
    """
    body = card.get("body") if isinstance(card.get("body"), dict) else card
    template_fields = _TEMPLATE_FIELD.findall(src.get("locator_template") or "")
    order = list(dict.fromkeys(
        template_fields + list(_LOCATOR_FALLBACKS.get(src["id"], ())) + ["link"]))
    rejected: list[str] = []
    not_a_locator: list[str] = []
    for field in order:
        # BY NAME, before the value is even looked at. A title is not a place on any source, and
        # neither is a retrieval identifier -- no value either could hold makes it one.
        if field in _NEVER_A_LOCATOR or field in _RETRIEVAL_IDS:
            if ((body or {}).get(field) or card.get(field)):
                not_a_locator.append(field)
            continue
        value = (body or {}).get(field) or card.get(field)
        if not isinstance(value, str) or not value.strip():
            continue
        if _UNINTERPOLATED.search(value):
            # The template was never filled in. Named, not silently skipped: which field
            # failed is a real finding about the source's configuration.
            rejected.append(field)
            continue
        if not value.strip().startswith(_LOCATOR_SHAPES):
            # A real value that is not a place. Kept distinct from the uninterpolated case:
            # that one is a source misconfiguration, this one is us having asked the wrong
            # field for a locator, and only the second is our defect.
            not_a_locator.append(field)
            continue
        out: dict[str, Any] = {
            "value": value,
            "field": field,
            "granularity": _LOCATOR_GRANULARITY.get(field, "unspecified"),
        }
        if src["id"] == "med_comms":
            # Measured: a relative path into the attachments provider, not a browser URL.
            #
            # WAVE 8 adds the second clause. This path carries `documentId=<int>` in its query
            # string -- a source-system identifier inside a VALUE, where `_RETRIEVAL_IDS` cannot
            # see it, because that filter works on keys. Redacting it would break the locator,
            # since the provider requires it. So the value stays whole and is labelled: it is a
            # place, not a set of identifiers to take apart. Reading an id out of it and handing
            # it back is the `card.id`-vs-`document_id` trap that document handles remove, and
            # the one that 404s is the one this URL contains.
            out["caution"] = ("a relative path into the attachments provider, not a "
                              "browser-resolvable URL. Use it whole; the identifiers inside it "
                              "are the provider's, not ones to pass to a read")
        # COUNTS, not field names. These lists carried wire field names to the model, and on
        # med_comms one of them was `muse_raw_id` -- the retrieval identifier wave 4 removed from
        # `source_fields` via `_RETRIEVAL_IDS`, which filters KEYS and cannot see a name that has
        # become a VALUE. `01` section 1e puts the locator template and its fallback chain on the
        # Hide list, so which fields were tried is mechanics; that a fallback happened is not.
        if rejected:
            out["fell_back"] = {
                "count": len(rejected),
                "why": ("this source did not interpolate its own locator template, so the value "
                        "carried the template markers and would have manufactured a citation"),
            }
        if not_a_locator:
            out["skipped_not_a_locator"] = {
                "count": len(not_a_locator),
                "why": ("fields ahead of this one in the source's own locator chain held values "
                        "that are not places -- a title, an identifier -- so they were passed "
                        "over rather than cited"),
            }
        return out
    return {
        "value": None,
        "field": None,
        "granularity": None,
        "refused": (
            f"{src['id']} did not populate a resolvable locator"
            # COUNTS here too. Wave 8 replaced the field-name lists on the SUCCESS path because
            # `muse_raw_id` reached the model as a value inside a string; this sibling return kept
            # interpolating the same lists, and med_comms populates `not_a_locator` on every card.
            + (f"; {len(rejected)} candidate field(s) came back with the template markers still "
               "in them, which would manufacture a citation if passed through" if rejected else "")
            + (f"; {len(not_a_locator)} held a value that is not a place to go"
               if not_a_locator else "")),
    }


def _chunk(card: dict[str, Any]) -> dict[str, Any] | None:
    """A similarity match's matching passage. MANDATORY when the lane is similarity.

    A KNN hit without the passage that matched is an unverifiable claim: the only way to
    check a similarity match is to read what matched. `chunkData` carries `chunkText`,
    `chunkScore` and `chunkIndex`; the predecessor dropped it as plumbing.
    """
    raw = card.get("chunkData")
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if not isinstance(raw, dict):
        return None
    return {
        "text": raw.get("chunkText"),
        "score": raw.get("chunkScore"),
        "index": raw.get("chunkIndex"),
    }


def assemble_record(
    card: Any, plan: dict[str, Any], src: dict[str, Any], *, lane: str,
) -> dict[str, Any] | None:
    """One model-facing record. `card.id` never reaches the model."""
    if not isinstance(card, dict):
        return None
    body = card.get("body") if isinstance(card.get("body"), dict) else {}
    # `card.id` ONLY. The `or card.get("documentId")` fallback here contradicted
    # `encode_document_handle`'s own docstring ("`card_id` is `card.id`, never
    # `body.document_id`") and `plan_read`'s measured finding that a returned `document_id` is a
    # source-system number that 404s if sent back. A handle minted from it would surface in
    # `run_read` as `identifier_not_found` -- attributing to the model an identifier the
    # connector chose. Wave 8 removed it: measured, no search card carries `documentId` at top
    # level, and `_EXTRACT_FLOOR` requests `id`, so it was unreachable as well as wrong. A card
    # with no `id` now mints no handle and says so, which it already did.
    card_id = card.get("id")
    # `encode_document_handle` owns the model-facing sentence for a missing id ("a document handle
    # needs card.id; the response carried none..."). The ternary short-circuited it and shipped the
    # two-word placeholder `"no id"` instead, while wave 8's comment claimed this path "says so".
    handle, herr = encode_document_handle(src["id"], str(card_id) if card_id else "")
    score_key, score_meaning = _SCORE_KINDS[lane]
    chunk = _chunk(card)
    # WAVE 8. `card["chunkScore"]` does not exist -- measured on a live KNN card, whose
    # top-level keys are `@class, chunkData, dataSource, highlightFragments, id, matchingTerms,
    # missingTerms, queryId, relevance, restricted, searchMethod, textSize, title`. Neither
    # `chunkScore` nor `score` is among them, so every similarity record shipped
    # `score: {"value": null, "kind": "chunkScore", "meaning": "a cosine similarity in 0..1"}`
    # -- a described quantity with no value, on the one lane where three instruction tiers tell
    # the model to reason about magnitude.
    #
    # The cosine is in TWO places and they agree: `chunkData[0].chunkScore` and, measured,
    # `card["relevance"]` -- 0.669722318649292 in both. That is a quirk worth stating: on a KNN
    # card `relevance` holds a cosine in 0..1, and on a term card the same field holds a term
    # score around 2,373. One field name, two incomparable quantities, which is exactly why
    # the engine is named on every result and why the two are never ranked together.
    #
    # The chunk's own score is preferred over `relevance` because it is the score OF the passage
    # being returned; `relevance` is the card's. They coincide today on a single-chunk hit.
    if lane == LANE_SIMILARITY:
        raw_score = (chunk or {}).get("score")
        if raw_score is None:
            raw_score = card.get("relevance")
    else:
        raw_score = card.get("relevance")
    if raw_score is None:
        raw_score = card.get("score")
    extent = card.get("textSize")
    # WAVE 8. The per-source program filter FIELD was on every record, measured live:
    # `product_name` on meds and `primary_mkv_number1` on med_comms. `01` section 1e puts the
    # filter field and its literal on the Hide list -- hiding them removes error, not
    # information -- and `PROGRAM.md` promises the model they are the connector's business.
    # Every record carrying the field the population was already scoped by invites the model to
    # reason about the scoping mechanism as though it were evidence.
    filter_fields = {str(k).removesuffix(".raw") for k in (src.get("program_filter") or {})}
    out: dict[str, Any] = {
        "source": src["id"],
        "title": card.get("title") or body.get("title"),
        # The represented state a qualification decision needs. Present because wave 3 puts
        # the per-source card fields in the floor; `default_fields_unresolved` on the plan
        # says when they could not be resolved.
        # An UNINTERPOLATED template value is dropped here as well as from `locator`.
        # Guarding only the locator left `?link?` reaching the model through `source_fields`,
        # where it looks like an ordinary value and would manufacture a citation exactly as
        # if it had come from the locator field.
        "source_fields": {k: v for k, v in (body or {}).items()
                          if k not in ("title", "text") and k not in _RETRIEVAL_IDS
                          and k not in filter_fields
                          and v not in (None, "", [])
                          and not (isinstance(v, str) and _UNINTERPOLATED.search(v))},
        "matched_by": lane,
        "why_it_matched": _highlights(card),
        "score": {"value": raw_score, "kind": score_key, "meaning": score_meaning},
        "locator": _locator(card, src),
        # Verbatim. `card.textSize` reads 0 rather than absent when `text_size` is missing
        # from the request, so a 0 here is only trustworthy because wave 3 puts it in the
        # floor -- and the plan says so.
        "extent_chars": extent,
        "readable": src.get("readable"),
        # The declared value, because `readable` collapses absent/null to False and "we do not
        # know" is not "cannot be read". One unsupported document returns HTTP 403 for a whole
        # read batch, so the distinction changes what the model should attempt.
        "readable_declared": src.get("readable_declared"),
        "document": handle,
    }
    if lane == LANE_SIMILARITY:
        chunk = _chunk(card)
        out["passage"] = chunk
        if chunk is None or not chunk.get("text"):
            # Stated, not silently omitted: without the passage this record cannot be
            # checked at all, and the model needs to know that before relying on it.
            out["passage_missing"] = (
                "this is a similarity match and the matching passage did not come back, so "
                "there is nothing to check the match against")
    if herr:
        out["document_error"] = herr
    if src.get("boundary_strength") == "broad":
        out["scope_caution"] = (
            f"{src['id']} is scoped by a broad code that admits other programs' records, so "
            "exact program relevance still has to be established for this record")
    restricted = card.get("restricted")
    if restricted:
        out["access_restricted"] = True
    return out


def _population_established(plan: dict[str, Any], src: dict[str, Any],
                            matched: int | None = None) -> tuple[bool | None, str]:
    """Is this source's stored program-filter value still live? True / False / None = UNKNOWN.

    WAVE 10 (R1b), and `decisions.md` D15 decides where it goes: NOT into `count_defensible`.
    That flag means one thing -- the count is a pure function of the request, which is engine
    purity -- and whether the population was established is an independent fact. Folding them
    together would be the one-name-two-quantities shape this API already punishes, where
    `card["relevance"]` holds a cosine on one card and a term score on another.

    The three states are real and must not collapse:

      True   a configured literal appears in this source's own value list with a non-zero count,
             so the filter matched something when the connector started.
      False  the field IS facetable and its term list came back, and no configured literal is in
             it. That is affirmative evidence the filter is broken, not an absence of evidence.
      None   cannot be checked. Either the filter field is not among this source's computed facets
             -- meds, where `product_name` has no `.raw` subfield -- or the startup facet call did
             not complete. `None` is UNKNOWN everywhere in this module and never defaults to False.
    """
    pf = plan.get("program_filter") or {}
    if not pf:
        return None, "no program filter is applied to this source"
    field = next(iter(pf), "")
    wanted = {str(v) for vals in pf.values()
              for v in (vals if isinstance(vals, list) else [vals])}

    # A DERIVED filter cannot be checked against the value list it was derived FROM. `wanted`
    # here IS a subset of this source's own terms, so the comparison below would return True by
    # construction -- a circular check that certifies the filter no matter what the index did.
    # The non-circular evidence is the derivation itself: a prefix search that returned terms
    # proves values matching the programme exist in that field right now, which is the same
    # thing a stored literal's presence proves for the other sources.
    derived = src.get("filter_values_derived")
    if isinstance(derived, dict):
        if not derived.get("complete"):
            return None, (
                "this source's program filter is derived by prefix search at startup and that "
                "search did not complete, so whether the programme's values still exist in the "
                "filter field could not be checked")
        n = derived.get("term_count") or 0
        if n > 0:
            return True, ""
        return False, (
            "no value in this source's filter field begins with any configured programme "
            "prefix, and the prefix search completed. That is affirmative evidence the "
            "programme has no folder here under its current naming, not an absence of evidence")

    terms = src.get("filter_field_terms") or {}
    if field not in (src.get("computed_facets") or []) or not terms:
        return None, (
            "whether this source's stored program-filter value still matches could not be "
            "checked: its filter field is not one this source computes a value list for, so "
            "there is nothing to compare against. The population may be correct; nothing here "
            "establishes that it is")
    if any(t in wanted and (terms.get(t) or 0) > 0 for t in terms):
        return True, ""
    # WAVE 10 FOLLOW-UP (S3). Two ways the value list can fail to contain the literal without the
    # filter being broken, and `False` -- which asserts it IS broken -- must not be returned for
    # either.
    #
    # 1. RECORDS CAME BACK. A filter matching nothing returns zero, so a non-zero count is
    #    positive proof it matched. `run_population`'s own page-past-the-end branch already says
    #    exactly this: "a non-zero count came back for this request, so none of the silent-zero
    #    causes applies". Returning False beside 19 records was two contradictory rulings on one
    #    fact inside one result.
    # 2. THE VALUE LIST IS TRUNCATED. The startup facet call sends no `counts`, so it runs at
    #    default depth, and this module's own note on `_COUNTS_CEILING` says that is where the
    #    response reports truncation per field. A `+` bucket means the list is a top-N and the
    #    literal may simply be below the cut.
    if isinstance(matched, int) and matched > 0:
        return None, (
            f"whether this source's stored program-filter value is the intended one was not "
            f"established from its value list -- but {matched} records came back through that "
            "filter, so it matched something. A filter matching nothing returns zero")
    if "+" in terms:
        return None, (
            "this source's value list came back truncated -- it carries an overflow bucket -- so "
            "the stored program-filter value may be present below the cut. Not established, and "
            "not evidence against it either")
    return False, (
        "this source's stored program-filter value does NOT appear in its own value list, which "
        "came back complete, and no records matched. A filter matching nothing returns zero "
        "records at HTTP 200, so this population is probably not the one intended -- treat the "
        "count as unestablished and escalate the stored value")


def reconcile_count(body: dict[str, Any], plan: dict[str, Any],
                    lane: str | None = None) -> dict[str, Any]:
    """One count, mode-aware, with what it is a count OF.

    `basic_knn` returns `totalHits: 0` with the real figure in `totalHitsKnn`, so reporting
    `totalHits` as "the count" would report zero records for a search that found 334.

    WAVE 10 (R3). `lane` is the EFFECTIVE lane and must be passed whenever the caller knows it.
    Deriving it from `plan["engine"]` alone is what produced a count of 0 beside returned
    records: on a silent KNN downgrade the caller detected the lane change AFTER calling this,
    and never recomputed -- so the count stayed a vector total of zero while `counts_what` went
    on describing embeddings the records were not. Defaulting to the requested lane keeps every
    other caller unchanged.
    """
    keyword = _first(body, R_TOTAL)
    semantic = _first(body, R_TOTAL_KNN)
    lane = lane or (LANE_SIMILARITY if plan["engine"] == "basic" else LANE_TERM)
    primary = semantic if lane == LANE_SIMILARITY else keyword
    out: dict[str, Any] = {
        "records_matched": primary,
        "counts_what": ("chunks whose embedding is near the query embedding, in documents "
                        "under this source's program filter"
                        if lane == LANE_SIMILARITY else
                        "documents matching the stated conditions under this source's "
                        "program filter"),
        "defensible": plan["count_defensible"],
        "engine": plan["engine"],
        "narrowed": plan["narrowed"],
        # Distinct from `narrowed`: re-running a handle unchanged and narrowing it further are
        # different acts, and wave 3 set this flag specifically so wave 4 could tell them
        # apart. It was being computed and then dropped.
        "narrowed_further": plan.get("narrowed_further"),
    }
    if plan.get("narrowed_further") is False and plan.get("inherited"):
        out["same_population"] = (
            "this re-runs a population you already held without narrowing it further, so the "
            "count should match the one the handle recorded unless the index moved")
    if not plan["count_defensible"]:
        # TWO reasons now, and they are different facts. Asserting the semantic one for a partial
        # exact constraint would name a cause that did not apply.
        out["not_defensible_because"] = (
            "an exact constraint covered only part of what it named -- some of the fields behind "
            "it have no exact subfield and were dropped -- so this counts records matching PART "
            "of the request. See the note on this result"
            if plan.get("partial_exact_constraint") else
            "the semantic engine applies server-side ranking rules, synonym expansion and "
            "query rewriting, so this count is not a pure function of the request")
    if lane == LANE_SIMILARITY and keyword is not None:
        if keyword == 0:
            # `basic_knn` returns `totalHits: 0` REGARDLESS, so emitting the bare number reads
            # as "no document matched by term", which is not what was measured -- it is what
            # this engine always reports. Explain it rather than surfacing the artifact.
            out["term_matches_note"] = (
                "this engine reports no term-match count on a semantic request -- the zero is "
                "how it always answers, not a finding that nothing matched by term. Run the "
                "same question without `semantic` to get a term count")
        else:
            out["term_matches_also"] = keyword
    if not plan["narrowed"]:
        out["scope"] = ("this source's whole program population, not a narrowed subset"
                        + (" minus the stated exclusions" if plan["conditions"] else ""))
    return out


def diagnose_zero(
    body: Any, plan: dict[str, Any], src: dict[str, Any], *, date_filtered: Any = None,
) -> dict[str, Any]:
    """Why is this zero? Runs the ordered chain and reports what it RULED OUT.

    An empty result must never be returned bare. `AGENTS.md` section 11 promises the model
    that "the tool reports which absence kinds it ruled out -- source access, filter
    integrity, query rewriting, field validity", and this is the function that makes that
    promise true. `ZERO_CAUSES` holds SEVEN -- README rule 2's six measured API behaviours
    plus `silent_widening`, which this connector refuses at construction;
    each step below eliminates one and says so.

    The conditional endpoint has NO server-side signal to read: ten provocations -- including
    every input that changes it on basic -- all returned `FULLTEXT_ENHANCERS` with empty
    `hints`. So on that engine every step here is our own pre-check, which is exactly why
    wave 3 refuses before sending. The basic engine does emit signals, and two of them change
    what a zero means.
    """
    envelope = body if isinstance(body, dict) else {}
    strategies = [str(x) for x in (_first(envelope, R_STRATEGY) or [])]
    ruled_out: list[str] = []
    findings: list[str] = []

    # 1. Could the request have silently widened? Refused at construction, so no.
    ruled_out.append("silent_widening")
    findings.append(
        "the request could not have been silently unfiltered: every route to that was "
        "refused before sending, so this zero is about the query as stated")

    # 2. Is the source DENIED? A denied source returns 0 with an EMPTY error list, which is
    #    indistinguishable from absence without the access map.
    if src.get("access") == "ALLOWED":
        ruled_out.append("denied_datasource")
        findings.append(f"{src['id']} is searchable for this caller, so this is not a "
                        "permission boundary presenting as absence")
    elif src.get("access") is None:
        findings.append(f"whether {src['id']} is searchable could not be determined, so a "
                        "permission boundary is NOT ruled out")
    else:
        return {
            "is_absence": False,
            "cause": "inaccessible",
            "detail": f"{src['id']} is not searchable for this caller, so this zero is a "
                      "permission boundary and says nothing about whether the material "
                      "exists",
            "ruled_out": ruled_out,
        }

    # 3. Did every clause field exist in the mapping? Asserted pre-send.
    if src.get("mapping_retrieved"):
        ruled_out.append("nonexistent_condition_field")
        findings.append("every field named in this request exists in the source's "
                        "authoritative mapping, so this is not a condition on a field that "
                        "silently matches nothing")
    else:
        findings.append(f"{src['id']}'s field mapping was never retrieved this session, so "
                        "field validity is NOT ruled out")

    # 4. Is the program filter live? A reindex changing the stored literal would look exactly
    #    like absence. `01` section 1c calls this the fragile part, and it is.
    # What was actually checked pre-send is the filter field's SHAPE: that it ends in `.raw`
    # and that its base exists in the mapping. That is what `ZERO_CAUSES`'
    # `broken_program_filter` names -- a bare or mis-cased field.
    #
    # It is NOT a check on the stored LITERAL, which `01` section 1c names as the fragile
    # part: a reindex renaming `MK-6070` would return 0 on every source, and the earlier text
    # here ruled that out in prose while nothing had established it.
    #
    # The literal CAN be established when the source's own filter field is facetable: the
    # startup unfiltered facet pass holds its term list, so a term equal to the literal with a
    # non-zero count proves it live as of warm time. Where that is unavailable -- meds, the
    # poisoned source -- say so instead of claiming it.
    if plan.get("program_filter"):
        # WAVE 10. Computed by `_population_established` rather than inline, because the count now
        # reports the same fact and two hand-rolled copies of one predicate is the producer/consumer
        # drift that left the facet label map dead for six waves.
        # `matched=0` explicitly: this function only runs on a zero, and that zero is exactly
        # the evidence that makes a missing literal meaningful rather than inconclusive.
        literal_live, _why = _population_established(plan, src, matched=0)
        if literal_live:
            ruled_out.append("broken_program_filter")
            findings.append(
                "the program filter is live: its stored value was present with a non-zero "
                "count in this source's own value list when the connector started, so this "
                "is not a filter matching nothing after a reindex renamed the literal")
        else:
            findings.append(
                "the program filter field's SHAPE was checked before sending -- an exact-match "
                "subfield present in the mapping -- but whether its stored VALUE still matches "
                "this program could not be established for this source, so a filter broken by "
                "a reindex is NOT ruled out")

    # 5. Was any clause an exact match? Offer the field's real values -- or say the source
    #    cannot enumerate them, which on meds is the honest answer.
    exact_fields = [f for c in (plan.get("conditions") or [])
                    for f in (c.get("fields") or []) if str(f).endswith(".raw")]
    if exact_fields:
        if (plan.get("poison") or {}).get("facetable_program_filter") is False:
            findings.append(
                f"an exact-match value was required, and {src['id']} cannot enumerate its "
                "values for this population -- so the real values cannot be offered here. "
                "That is a limitation of this source, not evidence the value is absent")
        else:
            findings.append(
                "an exact-match value was required. Ask for a distribution of this "
                f"population to see {src['id']}'s real values for "
                f"{', '.join(sorted({_role_of(src, f) for f in exact_fields}))} -- an exact "
                "value taken from the distribution matches exactly")

    # 6. Was any value multi-word? It was phrase-matched, and reversal collapses it to zero.
    # EVERY value, not just the primary. `any_of[0]` goes in `text.text` and `any_of[1:]` in
    # `text.synonyms`, and every one of them is phrase-matched. Inspecting only the primary
    # meant `any_of=["stability", "accelerated stability study"]` returning 0 had
    # `multi_word_phrase` affirmatively RULED OUT while the multi-word value sat in
    # `synonyms` -- eliminating the most likely cause of that zero, on a list `AGENTS.md`
    # section 11 tells the model to trust.
    multiword = []
    for c in (plan.get("conditions") or []):
        text_obj = c.get("text")
        if not isinstance(text_obj, dict):
            continue
        for value in [text_obj.get("text"), *(text_obj.get("synonyms") or [])]:
            if isinstance(value, str) and len(value.split()) > 1:
                multiword.append(value)
    if multiword:
        findings.append(
            f"matched as a phrase, so the words had to appear adjacently and in order: "
            f"{multiword!r}. The individual words may match where the phrase does not")
    else:
        ruled_out.append("multi_word_phrase")

    # 7. Did OUR date filter cause it? If MUSE matched records and since/until removed them
    #    all, that is our filter and not absence -- and both numbers have to be reported.
    if plan.get("date_filter") and date_filtered is not None:
        matched = date_filtered.get("matched")
        retrieved = date_filtered.get("retrieved")
        surviving = date_filtered.get("surviving")
        if matched and not surviving:
            # The filter only ever sees RETRIEVED records. Claiming it removed all of `matched`
            # when only `retrieved` were in hand was flatly false under paging or a retrieval
            # cap -- "removed all 1014" when it removed all of 514.
            partial = isinstance(retrieved, int) and isinstance(matched, int) and retrieved < matched
            return {
                "is_absence": False,
                "cause": "our_date_filter",
                "detail": (
                    f"MUSE matched {matched} records; {retrieved} were retrieved and the "
                    f"connector's own since/until filter removed all of those. Records beyond "
                    f"the retrieved set were never date-checked, so this says nothing about "
                    f"them" if partial else
                    f"MUSE matched {matched} records and the connector's own since/until "
                    f"filter removed all of them. This is our filter, not absence"),
                "ruled_out": ruled_out,
            }

    # --- basic-engine signals, which only exist on that engine -----------------------
    relaxed = [x for x in strategies if x in _RELAXED]
    if relaxed:
        # NOT a stronger absence claim, and not a weaker one. Measured: a token appearing
        # nowhere returned 2 hits under exactly these strategies, and a longer impossible
        # token returned 0 under the same two. It says only that the query was relaxed.
        findings.append(
            f"the server relaxed the query ({', '.join(relaxed)}) and still matched nothing. "
            "Relaxation succeeding or failing is term-dependent, so this neither strengthens "
            "nor weakens the absence -- it only means the query that ran was not the query "
            "sent")
    if any(x in strategies for x in _MUTILATED):
        return {
            "is_absence": False,
            "cause": "query_rewritten",
            "detail": ("the server removed syntax it considered invalid, so the query that "
                       "ran was not the query sent. This zero is about a mutilated query"),
            "ruled_out": ruled_out,
        }
    if plan["engine"] == "conditional":
        ruled_out.append("question_mark_in_query")
        ruled_out.append("asterisk_in_condition")
        findings.append(
            "no server-side relaxation or rewriting occurred: this engine reports none and "
            "applies none, and punctuation that matches nothing was stripped before sending. "
            "So this zero is about the query exactly as stated, which is a stronger absence "
            "claim than the other engine can make")

    # A PARTIAL exact constraint cannot yield an absence. Measured on med_comms: `where status
    # exact "Final"` returned 0 and shipped `is_absence: True` with the load-bearing statement,
    # while 19 of 19 records carried a `doc_status` and 15 began "Final" -- the role spans two
    # fields, only one has an exact subfield, and the condition ran against the half that does not
    # hold the value. `absence.checks` then reassured against the actual cause: "every field named
    # in this request exists in the source's authoritative mapping."
    if plan.get("partial_exact_constraint"):
        return {
            "is_absence": False,
            "cause": "partial_exact_constraint",
            "detail": (
                "this zero is NOT an absence. An exact constraint in this request covered only "
                "part of what it named: some of the fields behind it have no exact subfield and "
                "were dropped before sending, so the condition ran against a subset that need not "
                "hold the value. Records matching it may well exist under the dropped fields."),
            "remedy": ("re-run with `match: \"phrase\"`, which covers every field behind the "
                       "constraint, or name the field you mean directly and read the refusal if "
                       "it cannot be matched exactly"),
            "ruled_out": ruled_out,
            "checks": findings,
            "index_date": (src.get("freshness") or {}).get("date"),
        }

    # WAVE 10 (R1). NOT unconditional. Everything above this point is a chain of early returns for
    # causes that were POSITIVELY IDENTIFIED, and this was the fall-through -- so a zero whose
    # cause could not be established still claimed absence, with the load-bearing sentence, while
    # `checks` two entries up said in plain words that the program filter was "NOT ruled out".
    # Correct prose beside the wrong decisive boolean, which is the shape the independent review
    # named as the reason four rounds of review had not converged.
    #
    # Gated on the PROGRAM FILTER specifically, not on all seven `ZERO_CAUSES`. Every cause has a
    # rule-out path, but none has an is-this-applicable predicate -- `multi_word_phrase` on a
    # single-word query is not unresolved, it is irrelevant -- and inventing one here would be a
    # larger change than the finding. The filter is the one that matters: it scopes the population,
    # so if its stored value no longer matches, EVERY source returns zero at HTTP 200 and looks
    # exactly like absence. The unresolved set is reported either way.
    #
    # Consequence, and it is a capability loss accepted deliberately (`decisions.md` D15): on meds
    # the literal can never be established from the startup facet pass, because `product_name` has
    # no `.raw` subfield and is not among the computed facets. So meds can no longer claim absence
    # at all until a prefix probe is built. "I cannot establish that this is an absence" is the
    # honest answer, and preferring it to a confident wrong one is what this group is for.
    unresolved = [c for c in ZERO_CAUSES if c not in ruled_out]
    if "broken_program_filter" not in ruled_out:
        return {
            "is_absence": False,
            "cause": "program_filter_unverified",
            "detail": (
                "this zero is NOT established as an absence. Whether this source's stored program "
                "filter value still matches could not be verified, and a filter that matches "
                "nothing after a reindex returns zero records at HTTP 200 -- indistinguishable "
                "from the program having no documents here."),
            "remedy": ("treat this as unknown coverage rather than absence. A source whose filter "
                       "field is itself listed in its own value list can verify this; where it is "
                       "not, ask the MUSE team to make that field facetable"),
            "ruled_out": ruled_out,
            "unresolved": unresolved,
            "checks": findings,
            "index_date": (src.get("freshness") or {}).get("date"),
        }

    return {
        # The load-bearing sentence, and the one AGENTS.md section 6 exists to protect. Reached
        # only when the program filter has been positively established as live.
        "is_absence": True,
        "statement": (
            "No accessible indexed record matched the executed request under the represented "
            "sources, fields, filters, access and time. This is NOT proof that the "
            "information or evidence does not exist."),
        "ruled_out": ruled_out,
        "unresolved": unresolved,
        "checks": findings,
        "index_date": (src.get("freshness") or {}).get("date"),
        "index_date_unknown": (src.get("freshness") or {}).get("unread")
        or (src.get("freshness") or {}).get("absent"),
    }


def _decode_taxonomy_term(term: str, cache: dict[str, Any]) -> tuple[str, bool]:
    """`L1|<branch>|<node>` -> `Analytical Method > Microbial testing`. Returns (label, decoded).

    The 590 KB tree buys exactly this: turn UUIDs into names and names back into UUIDs.
    Without it you have a filter you cannot aim and a distribution you cannot read.

    **65 of the 1,300 in-use values do not exist in the tree** -- 5% undecodable. Those keep
    their L-path and are labelled, never dropped: the taxonomy carries no version, no `etag`
    and `cache-control: no-store`, so the undecodable count is the ONLY drift signal there is.
    """
    if not isinstance(term, str) or not term.startswith("L") or "|" not in term:
        return term, True
    uuid_to_path = ((cache.get("taxonomy") or {}).get("uuid_to_path") or {})
    leaf = term.rsplit("|", 1)[-1]
    path = uuid_to_path.get(leaf)
    if not path:
        return f"{_UNNAMED_TAXONOMY} ({term})", False
    return " > ".join(path), True


def assemble_distribution(
    body: Any, plan: dict[str, Any], cache: dict[str, Any], *, population: Any,
) -> dict[str, Any]:
    """Per-field value distribution, with what each count is a count of.

    Four things every field must state, because each is a way the number is narrower or wider
    than it looks:

      complete or truncated  -- the `+` overflow bucket. Its ABSENCE certifies you have every
                               value, which is the one signal on this endpoint that can be
                               trusted positively. Six terms plus a `+` is not "the values".
      partitions or not      -- if the counts sum ABOVE the population the field is
                               multi-valued and its values do not partition the population.
                               Measured on `author.raw`: 65 against a population of 5.
      usable as an exact condition -- NO for a DATE facet. Its terms are rendered years and
                               return 0 records at HTTP 200 as a condition: the one case
                               where the facet-to-condition loop LIES rather than fails.
      what the label is      -- `valueLabels` when configured, the raw term otherwise, never
                               an invented label.
    """
    src = (cache.get("sources") or {}).get(plan["source"]) or {}
    raw = (_first(body, R_FACETS) if isinstance(body, dict) else None) or {}
    out: dict[str, Any] = {
        "scope": plan.get("facet_scope"),
        "fields": {},
        "population_described": population,
    }
    if plan.get("facet_population_note"):
        out["caution"] = plan["facet_population_note"]
    facetable = (plan.get("poison") or {}).get("facetable_program_filter")
    if facetable is False:
        out["poison"] = plan["poison"].get("detail")
        if plan.get("facet_scope") == "superset":
            out["superset_caution"] = plan["poison"].get("superset_detail")
        elif plan.get("facet_scope") == "unavailable":
            # `02` section 15 step 5 and section 16: on meds this recovery is unavailable, so
            # SAY the source cannot enumerate its values rather than implying absence.
            out["unavailable"] = (
                f"{plan['source']} cannot enumerate its values for this population. "
                + str(plan["poison"].get("superset_detail") or "")).strip()
    elif facetable is None and plan.get("poison"):
        # WAVE 8. `None` is UNKNOWN, and both consumers of `plan["poison"]` gated on `is False`,
        # so the explanation wave 3 wrote specifically for this state was built and discarded.
        # The model then got `scope: "unknown"` -- a bare token -- plus `empty_because: "no facet
        # field came back for this population"`, which is a claim about the POPULATION when the
        # truth is that no facet request was ever sent, because the connector's own startup call
        # for this source did not complete.
        #
        # It persists for the process: `warm_cache` memoises, so one transient 500 at startup
        # degrades every distribution for that source with that wording until a restart.
        out["predicate_unknown"] = plan["poison"].get("detail")
    if not isinstance(raw, dict) or not raw:
        # An empty facet has FOUR quite different causes and they must not be conflated.
        # The population count comes FIRST, because 0 terms is CORRECT when 0 records match --
        # which is the mistake that once got recorded as a broken mechanism.
        out["empty_because"] = (
            "the population is empty, so there is nothing to distribute" if population == 0
            else out.get("unavailable")
            or ("whether a distribution can be produced for this source is UNKNOWN, so none was "
                "requested. This says nothing about the population: the connector's startup call "
                "that establishes it did not complete" if facetable is None and plan.get("poison")
                else "no facet field came back for this population"))
        return out

    taxonomy_roles = {v: k for k, v in (src.get("taxonomy_roles") or {}).items()}
    declared_labels = {}
    # WAVE 10 (R7). Two independent breaks, either of which alone emptied this map permanently:
    # `warm_cache` keys these definitions BY the facet id and does not store an `id` field, so
    # `definition.get("id")` was always None; and it renames `valueLabels` to `value_labels` on
    # the way in, so even reaching the object would have missed the key. Nine live source/field
    # combinations carry configured labels and every one rendered as a raw code.
    #
    # No assertion distinguished the broken map from a working one in EITHER direction --
    # `mutation_gate.py`'s inverse mutant `R7-labels-repaired` is this exact patch, and it
    # survived the whole suite. The fixture gap behind that: `facets_body` returns
    # `restricted.raw` at startup, so the definition was cached, but `rich_facets` never returned
    # it at search time, so the lookup was never exercised.
    for _fid, definition in ((cache.get("facet_defs") or {}).items()
                             if isinstance(cache.get("facet_defs"), dict) else []):
        if isinstance(definition, dict):
            declared_labels[str(_fid)] = definition.get("value_labels") or {}

    # WAVE 8. The per-source program filter FIELD and its LITERAL are both on `01`'s Hide list,
    # and stripping them from records only was half a fix: measured live, the field was in the
    # DISTRIBUTION on all four faceted sources -- `primary_mkv_number1`, `compound_id`, `compound`,
    # `project_m_code` -- with the program literal itself present as a term inside each. meds was
    # clean only because `product_name` is not a computed facet, which is luck, not design.
    #
    # A distribution over the field the population was already scoped by carries no information
    # either: every record in the population has that value, so the term list is one entry
    # equalling the count. Hiding it removes error, not information.
    filter_bare = {str(k).removesuffix(".raw")
                   for k in ((plan.get("program_filter") or {}))}
    undecodable = 0
    for field, terms in raw.items():
        if not isinstance(terms, list):
            continue
        bare = str(field)[:-4] if str(field).endswith(".raw") else str(field)
        if bare in filter_bare:
            continue
        meta = (src.get("fields") or {}).get(bare) or {}
        is_date = meta.get("facet_type") == "DATE"
        is_taxonomy = bare in taxonomy_roles
        labels = declared_labels.get(bare) or {}
        values: list[dict[str, Any]] = []
        overflow = False
        total = 0
        for entry in terms:
            if not isinstance(entry, dict):
                continue
            term = entry.get("term")
            count = entry.get("count")
            if term in ("+", "...") or (isinstance(term, str) and term.strip() == "+"):
                overflow = True
                continue
            shown = str(term)
            decoded = True
            if is_taxonomy:
                shown, decoded = _decode_taxonomy_term(str(term), cache)
                if not decoded:
                    undecodable += 1
            elif labels.get(str(term)):
                shown = str(labels[str(term)])
            if isinstance(count, int):
                total += count
            values.append({"value": shown, "count": count})
        entry_out: dict[str, Any] = {
            "values": values,
            # The one positively trustworthy signal here.
            "complete": not overflow,
            "usable_as_exact_condition": not is_date,
        }
        if overflow:
            entry_out["truncated"] = (
                "more values exist than were returned, so this is not the full list. "
                "Search the values by prefix instead of enumerating them.")
        if is_date:
            entry_out["not_conditionable"] = (
                "these are rendered years, and using one as an exact-match value returns 0 "
                "records at HTTP 200 rather than failing")
        # THREE states, because `True` was being asserted in two situations that do not
        # support it -- both measured by the review gate:
        #
        #   with a `+` bucket present, the sum is of a TRUNCATED list. A field summing to
        #   4,200 out of a possible 65,000 was declared to partition the population. The one
        #   positively trustworthy signal on this endpoint is the ABSENCE of that bucket, and
        #   the claim was being made in its presence.
        #
        #   when the sum is BELOW the population, some records carry no value for the field.
        #   799 records with `doc_status` summing to 640 is not a partition, and a model
        #   computing a share has no way to see the denominator is 640.
        #
        # `None` means UNKNOWN here, as everywhere else.
        if not isinstance(population, int) or not population:
            entry_out["partitions_population"] = None
            entry_out["partition_unknown"] = (
                "the population these counts describe is not established, so whether they "
                "partition it cannot be stated")
        elif overflow:
            entry_out["partitions_population"] = None
            entry_out["partition_unknown"] = (
                "this value list is truncated, so the counts shown sum to less than the "
                "field's real total and cannot be compared with the population")
        elif total > population:
            entry_out["partitions_population"] = False
            entry_out["multi_valued"] = (
                f"the counts sum to {total} against a population of {population}, so records "
                "carry several values for this field and these counts do not partition it")
        elif total == population:
            entry_out["partitions_population"] = True
        else:
            entry_out["partitions_population"] = False
            entry_out["incomplete_coverage"] = (
                f"the counts sum to {total} against a population of {population}, so "
                f"{population - total} records carry no value for this field. A share "
                f"computed from these counts has {total} as its denominator, not {population}")
        if is_taxonomy:
            entry_out["axis"] = "assigned taxonomy tag, not a text match"
        out["fields"][taxonomy_roles.get(bare, bare)] = entry_out

    if undecodable:
        # The only available drift signal on a tree with no version.
        out["undecodable_taxonomy_values"] = undecodable
        out["undecodable_note"] = (
            f"{undecodable} taxonomy values are not in the tree this connector loaded, so "
            "they keep their encoded path. The taxonomy carries no version and forbids "
            "caching, so this count is the only signal that it has changed.")
    # The filter is APPLIED, not merely described. An earlier version emitted the note while
    # iterating every computed field, so the sentence claiming the response had been filtered
    # to the requested fields fired exactly when it had not been.
    shown = plan.get("distribution_fields_requested")
    if shown:
        wanted_roles = set()
        for name in shown:
            bare_name = name[:-4] if name.endswith(".raw") else name
            wanted_roles.add(taxonomy_roles.get(bare_name, bare_name))
        withheld = sorted(k for k in out["fields"] if k not in wanted_roles)
        out["fields"] = {k: v for k, v in out["fields"].items() if k in wanted_roles}
        out["fields_shown_is_our_filter"] = (
            "this endpoint has no field selector, so the response carried every computed "
            "field and the connector filtered it to the ones asked for. Filtering is ours, "
            "not the API's")
        if withheld:
            out["fields_withheld_by_our_filter"] = withheld
    unknown = plan.get("distribution_unknown_fields")
    if unknown:
        out["requested_but_not_computed"] = unknown
    # Wave 3 produced these and wave 4 was discarding them, so the "requested field is not
    # among this source's computed facets" explanation never reached anyone.
    if plan.get("facet_notes"):
        out["notes"] = list(plan["facet_notes"])
    # `06` section 9: relabelled, never emitted as the result's own count. It answers a
    # different question -- how many OTHER sources in the project would match -- and reading
    # it as this result's number is a category error the field name invites.
    stats = (body or {}).get("statistics") if isinstance(body, dict) else None
    if isinstance(stats, dict) and stats.get("matches") is not None:
        out["other_sources_in_project_that_would_match"] = stats["matches"]
        # WAVE 8. The second sentence. This lists ~25 datasource ids while every instruction
        # tier names FIVE program-scopeable sources, and nothing said the other twenty cannot be
        # asked for -- so the result handed the model a list of ids that look like valid
        # `sources` values and are not. None has a verified program filter field, which is the
        # whole reason they are out of scope.
        out["other_sources_note"] = (
            "this counts sources elsewhere in the project whose content would match, not "
            "records in this result. They are NOT available to search: none has a verified "
            "program filter, so a request naming one is refused. Treat these as evidence that "
            "material exists outside the program-scoped set, not as somewhere to look next")
    declared_not_computed = [
        f for f in (src.get("declared_facets") or [])
        if f not in (src.get("computed_facets") or []) and f not in raw]
    if declared_not_computed:
        # WAVE 8. Through `_role_of`, because `declared_facets` holds WIRE names. Measured: 21
        # `.raw` names reached the model on meds and 7 on med_comms -- `applicable_sites.raw`,
        # `muse_file_sensitivity.raw`, `folder_level10.raw` and so on. The harm is stated by this
        # module a few hundred lines down: passing `doc_status.raw` into `where` resolves to the
        # BARE field with `match` defaulting to `phrase`, so the promise that an exact value
        # taken from the distribution matches exactly silently becomes an analysed phrase match.
        # `_resolve_role` performs exactly that strip. So this list handed the model 21 names
        # that quietly downgrade `match: "exact"`, in the same result that teaches the
        # facet-to-condition loop.
        #
        # `.raw` STRIPPED, not mapped to a role. Routing through `_role_of` removed the `.raw`
        # harm and introduced a worse one: two wire names can share a role, so on med_comms both
        # `doc_status` and `activity_status` resolve to `status`. With one of them declared-absent
        # and the other computed, this block said the role `status` carries no value in the
        # population while `fields` in the same object showed `activity_status` with 19 -- a false
        # absence claim, plus two vocabularies for one set of fields inside one distribution.
        #
        # Stripping matches how `fields` is keyed a hundred lines above (`taxonomy_roles.get(bare,
        # bare)`), so the two halves now speak one vocabulary, and the wire-name hazard the fix
        # targeted -- a `.raw` name passed back into `where` silently degrading `exact` to
        # `phrase` -- is gone either way.
        out["declared_but_absent"] = {
            "fields": sorted({str(f).removesuffix(".raw") for f in declared_not_computed}),
            "meaning": ("these fields exist on this source and nothing in this population "
                        "carries a value for them. That is a finding, not an unavailability"),
        }
    return out


def _next_moves(result: dict[str, Any], plan: dict[str, Any],
                src: dict[str, Any]) -> list[str]:
    """Result-conditional moves. Fills only the slots already allocated to in-band.

    `model-facing-tools/README.md` assigns nine judgments: five are taught in prior guidance
    and FOUR belong in-band -- which candidates to read, is this zero an absence, is this
    result set neutral, is the ordering meaningful. `agent-instruction-changes.md` section F
    fixes exactly those four and says writing them into guidance would be the wrong tier.
    So this function fills those slots and adds no new ones: net guidance shrinks.

    Deliberately NOT a workflow. Every line is conditional on what actually came back, names
    a move rather than prescribing a sequence, and is omitted when the result does not make
    that move informative. A hint on every call trains the model to follow suggestions
    instead of reasoning.
    """
    moves: list[str] = []
    count = (result.get("count") or {}).get("records_matched")
    dist = result.get("distribution") or {}

    if isinstance(count, int) and count > _NARROW_AT and plan["engine"] == "conditional":
        facetable = sorted(f for f, m in (src.get("fields") or {}).items()
                           if m.get("has_raw"))
        line = (f"{count} records is a population, not an answer. Narrowing here means an "
                "exact value rather than a broader query")
        shown = [k for k, v in (dist.get("fields") or {}).items() if v.get("values")]
        if shown:
            # The selectivity mechanism: read a value off the distribution you already hold
            # and pass it back. Measured on meds: 799 -> 106 -> 5 in two steps.
            line += (f", and the distribution above already gives you real values to narrow "
                     f"on: {', '.join(shown[:6])}")
        elif dist.get("unavailable"):
            line += (f", but {src['id']} cannot enumerate its values for this population, so "
                     "narrowing here is by free text or by date rather than by an exact value")
        elif facetable:
            line += f". Facetable fields here: {', '.join(facetable[:6])}"
        moves.append(line)

    if dist.get("scope") == "superset":
        moves.append(
            "the distribution and the records describe DIFFERENT populations here. Any share "
            "or proportion you compute from the distribution is about the larger set")

    if plan.get("default_fields_unresolved"):
        moves.append(
            "these records carry only a title, a locator and an extent -- the fields that "
            "would let you qualify their status, version or date did not resolve, so "
            "qualification needs the document itself")

    # WAVE 10 FOLLOW-UP (S1). The EFFECTIVE lane, read from `match_kind`, not the requested engine.
    # On a silent KNN downgrade this said "the score is a distance between embeddings" in the same
    # object as `score.meaning: "an unnormalised term score"`. `match_kind` is already the effective
    # lane, so no signature change is needed -- and a signature change here would break a harness
    # double, which is what adding `lane` to `reconcile_count` cost.
    if result.get("match_kind") == LANE_SIMILARITY:
        moves.append(
            "these are similarity matches: the passage shown is what matched, and the score "
            "is a distance between embeddings rather than evidence the term appears. Read the "
            "passage before treating any of them as being about your question")

    cannot_read = [r for r in (result.get("records") or [])
                   if r.get("readable_declared") is False]
    unknown_read = [r for r in (result.get("records") or [])
                    if r.get("readable_declared") is None]
    if cannot_read:
        moves.append(
            f"{len(cannot_read)} of these records are in a source that declares no full-text "
            "retrieval, so a read of them fails -- and one such document fails the whole read "
            "batch, not just itself")
    if unknown_read:
        # UNKNOWN, not impossible. An earlier version collapsed both into "a read will fail".
        moves.append(
            f"for {len(unknown_read)} of these records the source does not declare whether "
            "full text can be retrieved, so a read may fail; it is not established either way")
    return moves


async def _execute_plan(plan: dict[str, Any], cache: dict[str, Any]) -> dict[str, Any]:
    """Run ONE source's plan and assemble its result. Never raises for an upstream failure."""
    src = (cache.get("sources") or {}).get(plan["source"]) or {"id": plan["source"]}
    env = await _post(plan["endpoint"], plan["search"])
    if not env["_ok"]:
        # A source failing is a per-source finding, not a failed search. `01` section 1e:
        # partial failure is the NORMAL case, because one call is N independent operations.
        return {
            "source": plan["source"],
            "status": "failed",
            "reason": env["_status"].get("reason"),
            "detail": env["_status"].get("detail"),
            "is_absence": False,
            "note": ("this source did not answer, which is not the same as it having no "
                     "matching records"),
        }
    body = env["_body"] if isinstance(env["_body"], dict) else {}
    lane = LANE_SIMILARITY if plan["engine"] == "basic" else LANE_TERM
    cards = _first(body, R_RESULTS) or []

    # EFFECTIVE mode, not requested. KNN silently downgrades to keyword on three of the five
    # sources, and the detector is `totalHitsKnn == 0` with cards present. Deriving the lane
    # from the request alone produced a count of 0 beside 20 records, every one labelled a
    # cosine similarity, with no absence block and nothing explaining the contradiction.
    #
    # WAVE 10 (R3). This block moved ABOVE `reconcile_count`. It used to run after it and the
    # count was never recomputed, so a downgrade shipped `records_matched: 0` beside returned
    # records with `counts_what` still describing embeddings -- the count and the records
    # answering different questions, in one object, which is the failure class this group exists
    # to remove. The count now derives from the lane that actually ran.
    downgraded = False
    if lane == LANE_SIMILARITY and cards and not _first(body, R_TOTAL_KNN):
        lane = LANE_TERM
        downgraded = True

    count = reconcile_count(body, plan, lane=lane)
    # WAVE 10 (R1b). ALWAYS present, alongside `defensible` and never folded into it -- D15. A
    # caller reading `defensible: True` alone still over-trusts, so the reason is stated whenever
    # this is not True. Attached here rather than inside `reconcile_count` because that function
    # takes only `body` and `plan`, and giving it a third parameter would break the two harness
    # doubles again -- which wave 10 already did once with `lane`.
    # `matched` is bound BEFORE the establishment call, which needs it -- a non-zero count is
    # positive proof the filter matched. Ordered this way deliberately: `_execute_plan` runs
    # once per source inside a loop, so reading `matched` before assigning it would not
    # raise, it would silently use the PREVIOUS source's count.
    matched = count.get("records_matched")
    established, why = _population_established(plan, src, matched=matched)
    count["population_established"] = established
    if established is not True:
        count["population_established_note"] = why

    records = [r for r in (assemble_record(c, plan, src, lane=lane) for c in cards) if r]

    # OUR date filter, applied over a complete retrieval and reported as ours with BOTH
    # numbers -- no date expression is usable as a condition on this API.
    date_report = None
    if plan.get("date_filter"):
        before = len(records)
        since, until = plan["date_filter"]["since"], plan["date_filter"]["until"]

        def in_window(rec: dict[str, Any]) -> bool:
            fields = rec.get("source_fields") or {}
            stamp = fields.get("modified_date") or fields.get("creation_date")
            if not isinstance(stamp, str) or len(stamp) < 10:
                return True          # unknown date -> kept, and counted as unknown
            day = stamp[:10]
            return (not since or day >= since) and (not until or day <= until)

        kept = [r for r in records if in_window(r)]
        undated = sum(1 for r in records
                      if not isinstance((r.get("source_fields") or {}).get("modified_date")
                                        or (r.get("source_fields") or {}).get("creation_date"),
                                        str))
        date_report = {
            "applied_by": "connector",
            "since": since, "until": until,
            "matched_by_muse": matched,
            "retrieved": before,
            # Pre-filter, for the `count_discrepancy` guard below — it must compare MUSE's count
            # against what MUSE sent, never against what our own filter left behind.
            "distinct_retrieved": len({r.get("document") for r in records
                                       if r.get("document")}),
            # `before >= matched` asserted True when MORE records came back than matched, which
            # is not coverage -- it is a discrepancy. Measured by the wave-8 gate: MUSE
            # intermittently returns a duplicate card, 1015 cards against `totalHits: 1014`, in 5
            # of ~16 samples. I could not reproduce it in 16 samples of my own; both measurements
            # stand, which is itself the reason to guard rather than to conclude. Three
            # instruction tiers tell the reader to check the count against the records, and a
            # reader who did got 1015 for 1014 beside `covers_whole_population: True`.
            "covers_whole_population": (
                None if not isinstance(matched, int)
                else True if before == matched
                else None if before > matched
                else False),
            "surviving_our_filter": len(kept),
            "records_with_no_usable_date": undated,
            "why_ours": plan["date_filter"]["reason"],
        }
        if isinstance(matched, int) and before < matched:
            date_report["partial"] = (
                f"this filter was applied to the {before} records retrieved, not to all "
                f"{matched} MUSE matched. The rest were never date-checked")
        if undated:
            date_report["undated_kept"] = (
                "records with no usable date were KEPT rather than dropped, because dropping "
                "them would silently narrow the population by our own filter")
        records = kept

    result: dict[str, Any] = {
        "source": plan["source"],
        "status": "ok",
        "engine": plan["engine"],
        "count": count,
        "records": records,
        "records_returned": len(records),
        "match_kind": lane,
        "requested_match_kind": (LANE_SIMILARITY if plan["engine"] == "basic" else LANE_TERM),
        "index_date": (src.get("freshness") or {}).get("date"),
        "index_date_unknown": bool((src.get("freshness") or {}).get("unread")
                                   or (src.get("freshness") or {}).get("absent")),
        # WAVE 10 FOLLOW-UP (S2). `mapping_retrieved is None` means UNKNOWN and is tested FIRST, so
        # a blanked mapping can never read as `authoritative` even if a sibling fact survived. Belt
        # as well as braces: `refresh_freshness` now blanks `mapping_complete` too, and either fix
        # alone would close this -- but the ordering is the one that holds if a third fact is ever
        # derived from the same call and missed.
        "field_validation": ("unavailable" if src.get("mapping_retrieved") is None
                            else "authoritative" if src.get("mapping_complete")
                            else "best-effort" if src.get("mapping_retrieved")
                            else "unavailable"),
        # WHICH FIELD ordered the result, not just that it was ordered. `newest` and `oldest`
        # are not inverses -- measured at n=60, `date_desc` orders `modified_date` while
        # `date_asc` orders `creation_date`, each leaving the other unordered -- so "the newest
        # five" means a different set depending on which ran, and nothing else in the response
        # reveals it. `None` under relevance order, where no sort key is emitted at all.
        #
        # Wave 3 set this on the PLAN and wave 4 never carried it to the result, so the
        # requirement stated at the `_SORTS` definition went unmet for three waves. Wave 7's
        # gate found it because wave 7 wrote a parameter description promising it.
        "ordered_by": plan.get("ordered_by"),
        # Never passed through unqualified: on the conditional engine this map describes an
        # expansion that did not happen.
        "synonym_expansion": plan["synonym_expansion"],
        # WAVE 10 (R5). `fields` stays the ROLE, which is the vocabulary the caller used and the
        # only one stable across sources. But the role is not always one field: `status` spans
        # `doc_status`, `activity_status` and `experiment_status`, and `_role_of` maps every one of
        # them back to `status` -- so a request that constrained `activity_status` alone reported
        # `status`, and the executed field was unrecoverable.
        #
        # Two harms, both measured. A reader restating `where status` gets a BROADER population
        # than the one they were shown. And on `signals`, 16 experiment records came back described
        # only as `status`, which confers a document-status meaning the source does not represent.
        #
        # So `executed_fields` names what actually ran, and `role_covers_also` names the siblings
        # that did NOT -- present only when the role spans more than ran, because an empty key on
        # every single-field condition is noise. Bare names throughout: a `.raw` name handed back
        # is one the caller cannot pass to `where`.
        "conditions_as_asked": [
            _condition_as_asked(c, src) for c in (plan.get("conditions") or [])],
    }
    # WAVE 10 (R6b). The text this population was matched against, echoed back. A semantic result
    # arrived describing 19 matches with no record of the question that produced them, so it could
    # not be restated, compared, or cited -- and no handle is offered on this engine, which makes
    # the echo the only thing that makes the result self-describing.
    #
    # Read from `knnSearchQuery`, which is the text actually embedded, so it is right whether the
    # caller supplied `question` or `semantic=True` with `about`. That field is internal and is
    # stripped from the result; this is a deliberate, single, named copy of it.
    #
    # WAVE 10 FOLLOW-UP (S1). The text is still echoed on a downgrade -- it WAS the text sent, and
    # a result that cannot say what produced it is the complaint R6b exists for. But the NOTE is
    # branched on the effective lane. It read "matched against by meaning" beside `match_kind:
    # "term"` and a term count, which is the same contradiction R3 had just been fixed to remove,
    # one key over, in the same wave.
    if plan["engine"] == "basic" and plan["search"].get("knnSearchQuery"):
        result["question_asked"] = plan["search"]["knnSearchQuery"]
        result["question_asked_note"] = (
            "the text this population was matched against by meaning. Ranking on this engine is "
            "not a pure function of it, so two runs of the same text need not return the same "
            "count"
            if lane == LANE_SIMILARITY else
            "the text that was sent to be matched by meaning. This source returned no vector "
            "matches, so what came back are TERM matches on that same text -- it describes what "
            "was asked, not how these records were found")
    if downgraded:
        result["semantic_downgraded"] = (
            "semantic retrieval was requested and this source returned no vector matches while "
            "still returning records, so what came back are TERM matches under a similarity "
            "request. They answer a different question and are labelled accordingly")
    if date_report:
        result["date_filter"] = date_report
    if plan.get("notes"):
        result["notes"] = _model_facing_notes(plan["notes"], src)

    # The distribution needs its own call, and it describes whichever population the plan's
    # facet scope names -- which is not always the records' population.
    # Budget exhaustion here must DEGRADE the distribution, not unwind past a completed
    # search. `_request` raises before its HTTP call, and an escape from the facet or superset
    # call reached `run_population`'s handler, which reported the source as "not searched" --
    # discarding its records, count and absence diagnosis and telling the model the source was
    # never looked at. The docstring's "never raises for an upstream failure" was false here.
    if plan.get("facets"):
      try:
        fenv = await _post(EP_FACETS, plan["facets"])
        fbody = fenv["_body"] if fenv["_ok"] and isinstance(fenv["_body"], dict) else {}
        dist_population = matched
        if plan["engine"] == "basic":
            # `matched` here is `totalHitsKnn` -- CHUNKS near an embedding, not documents. The
            # facet call is term-scoped, so labelling its fields with the similarity number
            # judged ~96 documents' coverage against 334 chunks, and suppressed a genuinely
            # multi-valued field's warning by comparing it to a number the facets do not
            # describe. Wave 3 already says the two populations differ; this used the wrong
            # one anyway. Not established rather than guessed.
            dist_population = None
        if plan.get("facet_scope") == "superset":
            # A DIFFERENT population. Fetch its own count rather than labelling the records'
            # count as though it described these facets.
            senv = await _post(EP_QUERY, {
                F_DATASOURCES: [plan["source"]], F_PROJECT: cache.get("project"),
                F_MODE: MODE_BASIC["keyword"], F_LIMIT: 1, F_FIELDS: ["title", "id"],
                F_QUERY: plan["facets"].get(F_QUERY)})
            dist_population = (_first(senv["_body"] or {}, R_TOTAL)
                               if senv["_ok"] else None)
        result["distribution"] = assemble_distribution(
            fbody, plan, cache, population=dist_population)
        if plan.get("facet_scope") == "superset":
            result["distribution"]["records_population"] = matched
            result["distribution"]["both_counts"] = (
                f"{dist_population} records are described by this distribution; "
                f"{matched} are in the program population the records come from")
            # WAVE 8. `superset_literal_sanitised` was set by the planner and read by nothing, so
            # the sentence above described a population fetched with DIFFERENT query text than the
            # configured program literal, and nothing said so. Only reachable for a literal
            # containing `?` -- which drives a basic-endpoint query to 0 and so must be stripped
            # -- but the flag exists because the case was considered, and an unreported gap
            # between "the program" and "what we actually asked for" is the whole failure class
            # this surface exists to remove.
            if plan.get("superset_literal_sanitised"):
                result["distribution"]["superset_scope_caveat"] = (
                    "the text used to scope this superset is NOT this program's configured "
                    "literal: punctuation that drives this engine's query to zero was stripped "
                    "first. So the superset is a near neighbour of the program population rather "
                    "than a strict one, and the two counts above are not exactly comparable")
        if not fenv["_ok"]:
            result["distribution"]["failed"] = fenv["_status"].get("reason")
      except BudgetExhausted as exc:
        result["distribution"] = {
            "scope": plan.get("facet_scope"), "fields": {},
            "failed": exc.kind,
            "note": ("the call budget or deadline was reached before the distribution could be "
                     "fetched. The records and count above are complete; only the "
                     "distribution is missing"),
        }
    elif plan.get("facet_scope") not in (None, "not_requested"):
        result["distribution"] = assemble_distribution({}, plan, cache, population=matched)

    # WAVE 8. EXECUTE the value-prefix requests. Wave 3 built them, wave 4 classified
    # `value_prefix_requests` in `PLAN_INTERNAL_KEYS` as a key to STRIP, and no wave was ever
    # assigned their execution -- so `value_prefix` was planned, field-validated, poison-checked
    # and then discarded. Measured before this: a normal result with no values, no note and no
    # refusal, and on a five-source call `scited` and `mrl_slides` dropped out of the SEARCH
    # entirely, because a facet-side refusal propagates as the source's whole plan. The
    # parameter cost coverage and returned nothing.
    #
    # `response_assembly_check.py` passed `value_prefix` for the express purpose of asserting
    # the key was absent from the result, so the harness affirmatively locked the omission in.
    # That is why six gates missed it and the group gate found it twice.
    #
    # Shape is measured, `06-muse-facets.md` section 1: the value goes in `formParams` under the
    # same field name (`searchField` alone is HTTP 400 `Value for autocomplete search field ...
    # is not specified`), the response collapses to exactly ONE facet field, matching is
    # PREFIX-ONLY (`"Belt"` -> 2 terms, mid-string `"eltran"` -> 0), and `searchPrefix` is
    # decoration with no effect at any value. Not subject to the poison rule.
    if plan.get("value_prefix_requests"):
      try:
        found: list[dict[str, Any]] = []
        for vp in plan["value_prefix_requests"]:
            role = _role_of(src, vp["field"])
            entry: dict[str, Any] = {
                # The field ASKED FOR as well as its role. `_role_of` relabels an explicitly named
                # field, so `value_prefix={"activity_status": …}` and `{"status": …}` produced
                # byte-identical blocks on med_comms -- where `status` covers two fields -- and the
                # model could not tell which of them an empty answer was about.
                "field": role,
                "field_consulted": vp["field"] if vp["field"] != role else None,
                "prefix": vp["prefix"],
                "match": vp["match"],
            }
            if entry["field_consulted"] is None:
                del entry["field_consulted"]
            # A TAXONOMY field stores encoded L-paths, and prefix matching runs against THOSE, not
            # against labels. Measured live: `value_prefix={"issue": "L1"}` returned 7 values, all
            # 7 raw L-paths at 76 characters -- which `00-surfaced-tools.md` section 8 keeps away
            # from the model entirely -- while `{"issue": "Out"}`, a real label prefix, returned 0
            # with `complete: True` certifying the empty list and a message blaming the prefix.
            # So the only vocabulary the model has can never match, and that failure was reported
            # as a measured absence.
            #
            # REFUSED rather than decoded. Decoding the results would still leave the REQUEST
            # matching on a vocabulary the model cannot see -- a prefix of an L-path is a prefix of
            # a UUID -- so a label prefix would keep returning nothing. Both working routes already
            # exist: the distribution lists decoded labels, and `where` on a taxonomy role resolves
            # a label properly.
            if vp.get("taxonomy"):
                entry["unavailable"] = (
                    f"{role!r} is a taxonomy field, and its values are stored as encoded paths -- "
                    "so a prefix search here matches those paths rather than the labels you would "
                    f"recognise, and a label prefix matches nothing. Read the labels off this "
                    f"population's distribution, then constrain with `where` on {role!r}, which "
                    "resolves a label properly.")
                found.append(entry)
                continue
            venv = await _post(EP_FACETS, vp["payload"])
            if not venv["_ok"]:
                entry["failed"] = venv["_status"].get("reason")
                found.append(entry)
                continue
            vbody = venv["_body"] if isinstance(venv["_body"], dict) else {}
            fmap = _first(vbody, R_FACETS) or {}
            # Exactly one field comes back; take it by iteration rather than by name, because
            # the key is the `.raw` wire name and that must not be what we key on OR emit.
            terms = next(iter(fmap.values()), []) if isinstance(fmap, dict) else []
            # TYPE-CHECKED, like `assemble_distribution` guards its equivalent. A non-list here
            # yielded `values: []` and then a confident `empty_because` about the prefix -- a claim
            # about the data from a response that was never read.
            if not isinstance(terms, list):
                entry["unreadable"] = (
                    "this source returned a value list in a shape this connector does not "
                    "recognise, so no values are reported. That is not an absence of values")
                found.append(entry)
                continue
            values = [{"value": t.get("term"), "count": t.get("count")}
                      for t in terms if isinstance(t, dict) and t.get("term") not in (None, "+")]
            entry["values"] = values
            entry["counts_caveat"] = vp["counts_caveat"]
            # The `+` bucket's ABSENCE is the one positively trustworthy signal here: it
            # certifies the list is complete rather than merely short.
            entry["complete"] = not any(
                isinstance(t, dict) and t.get("term") == "+" for t in terms)
            if not values and not entry["complete"]:
                entry["empty_because"] = (
                    "no value is reported, and the response was also truncated -- so whether any "
                    "value matches this prefix is UNKNOWN rather than established as none")
            elif not values:
                # 0 terms at HTTP 200. Indistinguishable from "no such field" unless said:
                # measured, `activity_status` on med_comms holds 2 values in this population
                # and neither starts "Fin", so the empty answer is about the PREFIX.
                entry["empty_because"] = (
                    f"no value of {role!r} in this population starts with {vp['prefix']!r}. "
                    "Matching is prefix-only, so a substring from the middle of a value never "
                    "matches. This says nothing about whether the field is populated")
            found.append(entry)
        # No `if found:` -- the enclosing `if plan.get("value_prefix_requests")` guarantees at
        # least one entry, so the guard could not be False and read as though it could.
        result["value_prefix"] = found
      except BudgetExhausted as exc:
        # KEEP what was already retrieved. `found` used to be discarded wholesale, so with three
        # fields requested and the budget reached on the third, the first two fields' values were
        # thrown away -- and the note said "the field's values", singular and unnamed, for a
        # partial result. Which fields are missing is now stated by name.
        asked = [_role_of(src, vp["field"]) for vp in plan["value_prefix_requests"]]
        done = [e.get("field") for e in found]
        result["value_prefix"] = found + [{
            "failed": exc.kind,
            "not_searched": [f for f in asked if f not in done],
            "note": ("the call budget or deadline was reached before these fields' values could "
                     "be searched. Any values above are complete for the fields they name; the "
                     "records and count are complete"),
        }]

    # OUTSIDE the requests block, deliberately: on the degrade path there are no requests to
    # execute, which is exactly when the model most needs to be told the lookup did not happen.
    # Nested inside, this said nothing in the one case it exists for.
    if plan.get("value_prefix_unavailable"):
        result["value_prefix_unavailable"] = plan["value_prefix_unavailable"]

    if not records and (matched in (0, None)):
        result["absence"] = diagnose_zero(
            body, plan, src, date_filtered=(
                {"matched": matched, "retrieved": date_report.get("retrieved"),
                 "surviving": len(records)} if date_report else None))
    elif date_report and not records:
        result["absence"] = diagnose_zero(
            body, plan, src,
            date_filtered={"matched": matched, "retrieved": date_report.get("retrieved"),
                           "surviving": 0})
    elif not records and isinstance(matched, int) and matched > 0:
        # NO records but a real count -- reachable by paging past the end. This is the one
        # shape where the model most needs to be told it is not absence, and it was the one
        # shape that said nothing at all.
        result["absence"] = {
            "is_absence": False,
            "cause": "paging_past_the_population",
            "detail": (f"{matched} records matched and none were returned. That is a paging "
                       f"boundary, not absence: offset {plan['search'].get(F_OFFSET, 0)} is at "
                       "or beyond the end of this population"),
            # The tool docstring says an empty result ALWAYS names which causes of a false zero
            # were ruled out. This was the one branch that carried none -- because it is built
            # here rather than in `diagnose_zero`. Every cause IS ruled out: a real count came
            # back, so the request executed, matched, and was scoped correctly.
            "ruled_out": list(ZERO_CAUSES),
            "ruled_out_basis": ("a non-zero count came back for this request, so none of the "
                                "silent-zero causes applies -- the population exists and this "
                                "page is past its end"),
        }
    # Silent truncation is the other half of criterion 16. `depth="complete"` sends the API's
    # 10000 ceiling, which is not the population's size -- above it, records are fewer than
    # matched with nothing saying retrieval stopped.
    # RECORDS AGAINST THE COUNT, below the ceiling. Measured by the wave-8 gate: MUSE
    # intermittently returned 1015 cards for `totalHits: 1014` on meds, a duplicated card, in 5 of
    # ~16 samples (not reproduced in 16 of mine -- so it is intermittent, which is the reason to
    # state it rather than to reason about it). `retrieval_incomplete` below is correctly gated on
    # `matched > limit` and cannot see this. `run_read` already dedupes client-side because
    # "without ours a duplicate reads as a missing document"; the population path did not.
    #
    # WAVE 10 (R4). Compares the count against the records MUSE RETURNED, not against the records
    # left after our own date filter ran. `records` is reassigned to the surviving set above, so
    # this guard was comparing a post-filter number to a pre-filter one and reporting that neither
    # was established -- when both were. Measured: med_comms, 19 matched, 19 retrieved, our filter
    # removed all 19, and the result carried "Neither number is established as the population
    # size" beside a `date_filter` block stating all three figures exactly.
    #
    # `date_report["retrieved"]` is what MUSE returned; without a date filter that is `records`.
    limit = plan["search"].get(F_LIMIT)
    returned = (date_report or {}).get("retrieved", len(records))
    if (isinstance(matched, int) and isinstance(limit, int) and matched <= limit
            and isinstance(returned, int) and returned != matched):
        # Also pre-filter: a real MUSE-side discrepancy can coexist with a date filter (20
        # returned for 19 matched, then our filter drops 5), and counting distinct documents over
        # the survivors would understate what MUSE actually sent.
        distinct = (date_report or {}).get(
            "distinct_retrieved",
            len({r.get("document") for r in records if r.get("document")}))
        result["count_discrepancy"] = {
            "matched": matched,
            "records_returned": returned,
            "distinct_documents": distinct,
            "detail": (f"this source reported {matched} matching records and returned "
                       f"{returned}, which is fewer or more than it counted, below any "
                       f"retrieval ceiling. {distinct} of the returned records are distinct "
                       "documents. Neither number is established as the population size, so "
                       "treat the count as approximate here rather than reconciling them"),
        }
    if isinstance(matched, int) and isinstance(limit, int) and matched > limit:
        result["retrieval_incomplete"] = (
            f"{matched} records matched and at most {limit} could be retrieved in one call, "
            f"which is the API's ceiling and not a choice of ours. {len(records)} are here; "
            "the rest were not retrieved and are not described by anything above")

    result["next_moves"] = _next_moves(result, plan, src)
    return result


def _role_of(src: dict[str, Any], wire_field: str) -> str:
    """Wire field name -> the vocabulary the caller used. `doc_status.raw` -> `status`.

    Wire names must not reach the model, and not only for tidiness: a `.raw` name handed back
    is one the model cannot use. Passing `doc_status.raw` into `where` resolves to the BARE
    field and `match` defaults to `phrase`, so the promise that "an exact value taken from the
    distribution matches exactly" silently becomes an analysed phrase match.
    """
    bare = wire_field[:-4] if wire_field.endswith(".raw") else wire_field
    for role, fields in (src.get("roles") or {}).items():
        if bare in (fields or []):
            return role
    for role, field in (src.get("taxonomy_roles") or {}).items():
        if field == bare:
            return role
    return bare


def _condition_as_asked(c: dict[str, Any], src: dict[str, Any]) -> dict[str, Any]:
    """One condition echoed in the caller's vocabulary, without erasing which field ran.

    See the note at the `conditions_as_asked` construction. `_role_of` is deliberately left
    alone -- a harness doubles it as `lambda src, f: f`, and more importantly the role IS the
    right thing for `fields` to carry. What was missing is everything alongside it.
    """
    wire = [str(f) for f in (c.get("fields") or [])]
    bare = [f[:-4] if f.endswith(".raw") else f for f in wire]
    out: dict[str, Any] = {
        "fields": [_role_of(src, f) for f in wire],
        "executed_fields": bare,
        "exact": any(f.endswith(".raw") for f in wire),
        "value": (c.get("_labels") or [None])[0] or (c.get("text") or {}).get("text"),
        "alternatives": (c.get("_labels") or [])[1:]
        or (c.get("text") or {}).get("synonyms") or [],
        "type": c.get("type"),
    }
    # Siblings behind the same role that this condition did NOT constrain. Read from the source's
    # own role map, so it is right for whatever that source actually declares.
    ran = set(bare)
    also: list[str] = []
    for role in {_role_of(src, f) for f in wire}:
        for sibling in ((src.get("roles") or {}).get(role) or []):
            if sibling not in ran and sibling not in also:
                also.append(sibling)
    if also:
        out["role_covers_also"] = sorted(also)
        out["scope_note"] = (
            f"this constrained {', '.join(bare)}. The role also covers "
            f"{', '.join(sorted(also))}, which this request did not constrain -- so restating it "
            "by role would describe a larger population than the one reported here")
    return out


def _model_facing_notes(notes: list[str] | None, src: dict[str, Any]) -> list[str]:
    """Rewrite wire field names in note text to the roles the caller would recognise.

    `plan["notes"]` was previously passed through wholesale -- the one place the result was
    not assembled field by field -- and wave 3's notes name wire fields, `.raw` suffixes
    included.
    """
    out: list[str] = []
    for note in notes or []:
        text = str(note)
        for field in sorted((src.get("fields") or {}), key=len, reverse=True):
            for wire in (f"{field}.raw", field):
                if wire in text:
                    text = text.replace(wire, _role_of(src, wire))
        out.append(text)
    return out


# --------------------------------------------------------------------------
# Document reading  (group BUILD wave 5)
# --------------------------------------------------------------------------
# `POST /v1/document/retrieve-full-documents`. Reading what a document actually SAYS, once the
# companion has decided that answering requires its words rather than its metadata.
#
# This endpoint's specialty failure is **HTTP 200 with fewer documents than asked for**. There
# are three ways a document goes missing and only one of them is loud:
#
#   a bogus id                    silently filtered -- 200, fewer returned
#   a DENIED source               silently filtered -- 200, fewer returned
#   a source without full text    **403, the ENTIRE batch fails**
#
# So the two things that carry this wave are both done AROUND the call rather than in it:
# pre-filter so the 403 never fires, and diff requested against returned so a silent omission
# cannot pass. `03` section 11 states which failure is worse -- "miss the first and a good batch
# dies loudly; miss the second and a bad batch passes quietly."
#
# Design source, closed and complete: 03-muse-fetch-document.md, sections 2-9.

EP_READ = "/resources/v1/document/retrieve-full-documents"

# Table cells arrive as SEPARATE tab-prefixed lines in document order with no row or column
# grouping. Measured on a meds specification/stability document: endotoxin limits, a process
# designation, lot numbers and stability durations all arriving as a flat sequence with nothing
# tying a limit to its attribute or a duration to its lot.
#
# This is a CORRECTNESS problem, not a formatting one, and a subtler one than a fabricated
# number: the values are real and the associations are lost. `AGENTS.md` section 15 forbids
# inventing thresholds and acceptance criteria; supplying real ones with wrong associations is
# the same failure, harder to notice.
# TWO consecutive tab-prefixed lines is enough, cells may be EMPTY, and a line may carry several
# tab-separated columns. Measured against the earlier pattern, which required three consecutive
# lines each holding at least one non-tab character: a two-cell limits table, a run containing one
# empty cell, and a table with columns on a single line ALL went undetected -- and each miss meant
# no tabular flag, no association caution, no locator substitution, and the passage labelled
# prose. A detector that misses the shapes that matter is the same as no detector.
_TAB_RUN = re.compile(r"(?:^|\n)\t[^\n]{0,200}(?:\n\t[^\n]{0,200}){1,}")

# A number that looks like a limit, criterion or duration. Used only to decide whether the
# association-uncertain flag applies -- never to extract a value.
_NUMERIC_CRITERION = re.compile(
    r"[<>=\u2264\u2265]\s*\d"
    r"|\bNMT\s*\d|\bNLT\s*\d|\bpH\s*\d"
    r"|\d+\s*(?:EU/mL|mg/mL|mg|ppm|%|M\b|months?\b|weeks?\b|days?\b|hours?\b|years?\b)",
    re.IGNORECASE)

# Non-Latin ranges seen inside meds documents: Cyrillic (~1,200 characters across 30 documents,
# Russian content) and Vietnamese diacritics. A passage returned without its language noted can
# be unreadable to the reader, and language is not a returned field on every source.
# Any non-Latin script, not only the two observed. The earlier pattern covered Cyrillic and one
# Vietnamese block, so a document in Chinese, Japanese, Arabic, Hebrew or Greek returned
# `non_latin_content: False` -- which a model reads as "this is Latin text".
_NON_LATIN = re.compile(r"[^\u0000-\u024F\u2000-\u206F\u20A0-\u20CF\u2100-\u214F]")

# `sensitivity` is a DATA CLASSIFICATION and exists only on meds. `acl_group` is an ACCESS LIST
# and exists on all five. They answer different questions -- what may be derived or disclosed
# versus who may see it -- and neither may be presented as the other. So the disclosure gate has
# no classification available for four of five sources, and says so rather than defaulting one.
_CLASSIFICATION_FIELD = "sensitivity"
_ACCESS_LIST_FIELD = "acl_group"

# Slices `03` section 4 requires, with the fields that can fill each per source. A slice a source
# cannot fill is reported as unfillable -- NEVER filled with the nearest-looking field, because
# reporting `experiment_review_status` as a document's status confers a status the source does not
# represent (`AGENTS.md` section 13). scited is published literature: "current version" is not a
# property a journal article has. mrl_slides is a slide repository. signals is an ELN.
_READ_SLICES: dict[str, tuple[str, ...]] = {
    "version_identity": ("current_version_doc_id", "i_chronicle_id", "r_object_id", "version"),
    "represented_status": ("doc_status", "retention_class", "activity_status",
                           "experiment_status", "experiment_review_status"),
    "time": ("creation_date", "modified_date", "original_creation_date", "issued_date",
             "approval_date", "year", "timestamp_indexed", "timestamp_processed"),
    "locator": ("webview_pdf_url", "doc_source_url", "webview_orig_url", "attachment",
                "link", "doc_link", "uri", "deck_url", "slide_source_url", "slide_url",
                "attachments"),
}

# `experiment_status` and `experiment_review_status` are the lifecycle of an EXPERIMENT, not of a
# document. They may fill the slice only with that said, so they are named separately.
_EXPERIMENT_STATUS = frozenset({"experiment_status", "experiment_review_status"})

# `03` section 4's matrix: which slices each source CAN fill at all. This separates two claims
# that are not interchangeable --
#
#   "this SOURCE does not represent a document version"  a category fact. scited is published
#       literature; "current version" is not a property a journal article has.
#   "this DOCUMENT carries no version identity"          a fact about one record. A field is
#       absent when unpopulated, and two meds documents returned 54 and 58 keys, so a key count
#       is a property of a document and NOT of a source.
#
# An earlier version made the SOURCE claim from a single document, so one meds record with an
# unpopulated `doc_status` produced "meds does not represent a document represented status" --
# false, and the kind of claim a model may generalise and stop asking for.
_SOURCE_CAN_FILL: dict[str, frozenset[str]] = {
    "meds": frozenset({"version_identity", "represented_status", "time", "locator"}),
    "med_comms": frozenset({"version_identity", "represented_status", "time", "locator"}),
    "scited": frozenset({"time", "locator"}),
    "mrl_slides": frozenset({"time", "locator"}),
    "signals": frozenset({"represented_status", "time", "locator"}),
}


def _normalise_text(value: Any) -> Any:
    """Replace the non-breaking space before anything reads the text.

    241 occurrences across 30 documents. Splitting, trimming or matching on `" "` silently
    misses content, so this is a normalisation rather than a flag.
    """
    if isinstance(value, str):
        return value.replace("\xa0", " ")
    return value


def text_fidelity(text: Any) -> dict[str, Any]:
    """The three flags `03` section 7 requires, all of them, on every read."""
    out: dict[str, Any] = {"tabular_material": False, "association_uncertain": False,
                           "non_latin_content": False}
    if not isinstance(text, str) or not text:
        return out
    tab_spans = [mo.group(0) for mo in _TAB_RUN.finditer(text)]
    if tab_spans:
        out["tabular_material"] = True
        out["tabular_note"] = (
            "this document contains tabular material, and extraction emits table cells as "
            "separate lines in document order with NO row or column grouping. A value here can "
            "belong to a different attribute or lot than the one it appears beside")
        # Searched over the TABULAR REGIONS only. Searching the whole document meant a prose
        # sentence like "yield improved by 12%" beside a table of lot numbers produced
        # `association_uncertain: True` plus a statement that the numbers are inside the tabular
        # material -- which was false.
        if any(_NUMERIC_CRITERION.search(span) for span in tab_spans):
            out["association_uncertain"] = True
            out["association_note"] = (
                "numeric limits, criteria or durations appear inside that tabular material. "
                "They are REAL numbers whose associations were lost, so none of them may be "
                "reported as an established value for a named attribute. When the number is "
                "what matters, use the locator and read the controlled document at source")
    if _NON_LATIN.search(text):
        out["non_latin_content"] = True
        out["language_note"] = (
            "this text contains non-Latin content -- Russian and Vietnamese both appear inside "
            "this corpus -- and language is not a field every source returns. A passage quoted "
            "without that noted can be unreadable to its reader")
    return out


def _read_slices(doc: dict[str, Any], source: str,
                 src_meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fill `03` section 4's slices, and say plainly which cannot be filled -- and WHY."""
    src_meta = src_meta or {"id": source}
    out: dict[str, Any] = {}
    can_fill = _SOURCE_CAN_FILL.get(source)
    for slice_name, candidates in _READ_SLICES.items():
        present = {f: doc[f] for f in candidates if doc.get(f) not in (None, "", [])}
        readable_name = slice_name.replace("_", " ")
        if not present:
            if can_fill is not None and slice_name not in can_fill:
                out[slice_name] = {
                    "available": False, "why": "source_does_not_represent_it",
                    "detail": (f"{source} does not represent a document {readable_name}. That is "
                               "a category difference, not a gap -- this source is not that kind "
                               "of system"),
                }
            else:
                out[slice_name] = {
                    "available": False, "why": "not_populated_on_this_document",
                    "detail": (f"this document carries no {readable_name}. {source} does "
                               "represent one, so this is absent for this record rather than "
                               "unavailable from this source"),
                }
            continue
        entry: dict[str, Any] = {
            "available": True,
            # ONE ENTRY PER FIELD, with its role alongside. Keying by role collapsed
            # `doc_status` and `activity_status` into one `status` entry on med_comms and one
            # silently overwrote the other -- so a document whose represented status was "Final
            # (E-Signature captured)" reported an activity lifecycle of "Completed" instead,
            # `available: True` and uncautioned. That is the substitution `03` section 4 forbids
            # and that `signals` gets an explicit caution for. Which fact the source represents is
            # the entire purpose of this slice, so a multi-field role may not collapse.
            #
            # The role is still carried, because a wire name passed back to `muse_population` does
            # not resolve -- that tool takes roles.
            "fields": dict(present),
            "roles": {f: _role_of(src_meta, f) for f in present},
        }
        if slice_name == "represented_status":
            experiment = sorted(set(present) & _EXPERIMENT_STATUS)
            if experiment:
                # ANY co-occurrence. Gating on `len(experiment) == len(present)` suppressed the
                # caution whenever a second status field appeared, and the model then read
                # "Reviewed" as a document status.
                entry["caution"] = (
                    f"{', '.join(experiment)} is the lifecycle of an EXPERIMENT, not of a "
                    "document. It is not a document status and must not be read as one")
        out[slice_name] = entry
    return out


def _restriction(doc: dict[str, Any], source: str) -> dict[str, Any]:
    """Which KIND of restriction this record carries. Never one presented as the other."""
    classification = doc.get(_CLASSIFICATION_FIELD)
    access_list = doc.get(_ACCESS_LIST_FIELD)
    out: dict[str, Any] = {}
    if classification:
        out["classification"] = classification
        out["classification_travels"] = (
            f"{classification!r} is a data classification and it travels with any meaning "
            "derived from this document, including across any external boundary")
    else:
        out["classification"] = None
        # TWO different absences, and only the second leaves the classification UNKNOWN.
        # `sensitivity` exists only on meds, so a scited record CANNOT carry one while a meds
        # record with it unpopulated MIGHT be Proprietary. Identical messages would have
        # collapsed exactly the disclosure-relevant distinction.
        if source != "meds":
            out["classification_absent"] = (
                f"{source} does not carry a data classification at all -- that field exists on "
                "one source only. This is not a default of 'unclassified'; it is a fact this "
                "source cannot state")
        else:
            out["classification_absent"] = (
                "this document's data classification is not populated. The field exists here, so "
                "this is UNKNOWN rather than absent -- it may be restricted")
    if access_list:
        out["access_list_present"] = True
        out["access_list_note"] = (
            "this record carries an access list, which constrains WHO may see it -- a different "
            "question from what may be derived or disclosed from it")
    else:
        # Not asserted. An earlier version stated "this source has an access list instead"
        # unconditionally while reporting none.
        # `None`, not `False`. A boolean False asserts "this record has no access list"; the
        # fact is that the field did not arrive. UNKNOWN is this project's `None` everywhere else.
        out["access_list_present"] = None
    return out


def plan_read(
    cache: dict[str, Any], documents: Any, purpose: Any, passages: Any = None,
) -> dict[str, Any]:
    """Decode, pre-filter and dedupe. Returns a plan or a refusal. No I/O."""
    if not isinstance(cache, dict) or not cache.get("sources"):
        return _refuse(
            "no_program_scope",
            "The program scope and source metadata could not be loaded, so no document can be "
            "read: whether a source supports full-text retrieval is unknown, and one "
            "unsupported document returns HTTP 403 for the whole batch.",
            defect_in=("source_access",),
            remedy="This is a connector startup failure. Report it as a limitation.")
    if not isinstance(purpose, str) or not purpose.strip():
        # Required, and not decoration: it is what selects passages on a large document, and what
        # qualifies the read if it becomes part of a durable claim. A read without a stated
        # purpose cannot be qualified later.
        return _refuse(
            "no_purpose",
            "`purpose` is required: what this read is meant to establish is what selects "
            "passages from a large document, and what makes the read qualifiable if it ends up "
            "supporting a claim. A read with no stated purpose cannot be qualified afterwards.",
            defect_in=("field_validity",),
            remedy="State what the read is meant to establish.")
    if isinstance(documents, str):
        documents = [documents]
    if not isinstance(documents, list) or not documents:
        return _refuse(
            "no_documents",
            "`documents` must be a non-empty list of document handles from a population result "
            "or recorded in the workspace.",
            defect_in=("field_validity",))

    decoded: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in documents:
        payload, err = decode_document_handle(raw)
        if err or payload is None:
            # A raw `card.id` is refused, not attempted. The model never holds an identifier:
            # `card.id` and `body.document_id` are different keys and the wrong one 404s, which
            # is the trap handles remove by construction rather than by discipline.
            refused.append({
                "given": str(raw)[:40],
                "cause": "not_a_document_handle",
                "detail": (f"{err}. Document identifiers are not the model's to construct -- "
                           "pass the handle a population result gave you, or one recorded in "
                           "the workspace."),
            })
            continue
        source = str(payload.get("datasource"))
        card_id = str(payload.get("id"))
        key = (source, card_id)
        if key in seen:
            # Deduped BEFORE the refusal checks. Afterwards, each repeat of an UNREADABLE handle
            # appended its own exclusion, so one reason was listed twice, `duplicates_removed`
            # computed to 0 and the coverage denominator counted a document twice.
            continue
        seen.add(key)
        src = (cache.get("sources") or {}).get(source) or {}
        # THE STEP THAT CANNOT BE SKIPPED. One document from a source without full-text
        # retrieval returns HTTP 403 for the WHOLE batch, taking down every document that would
        # have succeeded. Reported before sending, so the model learns why instead of losing
        # everything.
        # `readable` is wave 1's purpose-built collapse -- `... is True`, with the comment
        # "Absent or null must NEVER read as True". Testing `readable_declared` directly meant a
        # config returning `"false"`, `0` or `"true"` passed both guards and 403'd the batch.
        if src.get("readable") is not True and src.get("readable_declared") is not None:
            refused.append({
                "given": source, "cause": "source_has_no_full_text",
                "detail": (f"{source} declares no full-text retrieval. It is excluded here "
                           "rather than sent, because one such document returns HTTP 403 for "
                           "the entire batch -- including the documents that would have "
                           "succeeded."),
            })
            continue
        # WAVE 8. OUT OF SCOPE is not the same as a startup failure, and this branch reported
        # every out-of-scope source as one. `cache["sources"]` holds only the CURRENT program's
        # in-scope sources, while `decode_document_handle` deliberately runs no scope check so a
        # reference recorded in `ground/` in an earlier session can still be read. Those two facts
        # meet here: a handle naming any other source found `src == {}` and was told "the startup
        # configuration for it did not load", which did not happen. Fail-closed was right; the
        # reason was invented, and the reason is what the model reports.
        if source not in (cache.get("sources") or {}):
            refused.append({
                "given": source, "cause": "source_not_in_program_scope",
                "detail": (f"{source} is not one of this program's configured sources, so nothing "
                           "is known about it here -- not its access, not whether it supports "
                           "full-text retrieval. A handle for it may have been recorded under a "
                           "different program or before the scope changed. It is excluded rather "
                           "than risked, because an unsupported document fails the whole batch."),
            })
            continue
        if src.get("readable") is not True:
            refused.append({
                "given": source, "cause": "full_text_support_unknown",
                "detail": (f"whether {source} supports full-text retrieval is UNKNOWN -- the "
                           "startup configuration for it did not load. It is excluded rather "
                           "than risked, because an unsupported document fails the whole "
                           "batch."),
            })
            continue
        if src.get("access") is not None and src.get("searchable") is not True:
            refused.append({
                "given": source, "cause": "source_denied",
                "detail": (f"{source} is not accessible to this caller. The read endpoint drops "
                           "such documents silently, so this is stated before sending rather "
                           "than left as an unexplained gap in the response."),
            })
            continue
        decoded.append({"source": source, "id": card_id, "handle": str(raw)})

    if not decoded:
        return _refuse(
            "no_readable_document",
            "None of the requested documents could be read. Each one's reason is listed "
            "separately -- they are different findings and are not collapsed into one.",
            defect_in=("source_access", "field_validity"),
            excluded=refused)
    return {
        "refused": False,
        "endpoint": EP_READ,
        "purpose": purpose.strip(),
        "passages": passages if isinstance(passages, str) and passages.strip() else None,
        "passages_ignored": (None if passages is None or (isinstance(passages, str)
                                                          and passages.strip())
                             else f"`passages` was {type(passages).__name__}, not a string, so "
                                  "the whole document is returned instead of located passages"),
        "requested": decoded,
        "excluded": refused,
        # ONE call. No splitting by count or size: no ceiling was reached at 50 documents /
        # 593,153 bytes / 0.3 s, measured on representative AND largest documents. Any batching
        # we introduced would be ours, not the API's.
        # `{"document_entries": [{datasource, document_id}]}` -- read from the live OpenAPI
        # schema (`DocumentsListRequest` / `DocumentEntry`), not guessed. An earlier version sent
        # `{"documentIds": [...], "dataSources": [...]}`, which this endpoint rejects, and the
        # offline suite could not have caught it: a stub asserts the shape we chose.
        #
        # NOTE the field name is `document_id` here while the value is the CARD id -- the same
        # `card.id` a search returns. `03` section 1 measured that each returned object carries
        # `id` equal to the requested one, and `document_id` in a RETURNED body is a different,
        # source-system number that 404s if sent back. The two names collide across request and
        # response, which is precisely the trap document handles remove.
        "payload": {"document_entries": [{"datasource": d["source"], "document_id": d["id"]}
                                         for d in decoded]},
        "duplicates_removed": len(documents) - len(decoded) - len(refused),
    }


def assemble_document(doc: Any, cache: dict[str, Any], *, purpose: str,
                      passages: str | None) -> dict[str, Any] | None:
    """One read document. `card.id` and `document_id` never reach the model."""
    if not isinstance(doc, dict):
        return None
    source = str(doc.get("datasource") or "")
    src = (cache.get("sources") or {}).get(source) or {"id": source}
    body = {k: _normalise_text(v) for k, v in doc.items()}
    text = body.get("text")
    declared = body.get("text_size")
    handle, herr = encode_document_handle(source, str(doc.get("id")))
    out: dict[str, Any] = {
        "source": source,
        "title": body.get("title"),
        "read_for": purpose,
        "slices": _read_slices(body, source, src),
        "restriction": _restriction(body, source),
        "document": handle,
    }
    # NAMED when the document states it. Reporting only "non-Latin content is present" while
    # discarding a `document_language` field the record carried declines a question we can answer.
    if body.get("document_language"):
        out["language"] = body["document_language"]
    # `03` section 9 assertion 6: no truncation was found, and `len(text)` == `text_size` exactly
    # at 485,760 characters. Asserted per document rather than assumed, because a silent
    # truncation here would be indistinguishable from a shorter document.
    if isinstance(text, str):
        out["extent_chars"] = len(text)
        if isinstance(declared, str) and declared.strip().isdigit():
            declared = int(declared)
        if isinstance(declared, bool):
            declared = None
        if isinstance(declared, int) and declared and len(text) != declared:
            out["extent_mismatch"] = (
                f"the source declares {declared} characters and {len(text)} arrived. The text "
                "may be truncated, so it is not a complete reading of this document")
    if not isinstance(text, str) or not text:
        # A fourth absence kind, in a tool whose product is naming absence. This document was
        # returned and counted as read while carrying no content, and nothing said so.
        out["content_absent"] = (
            "this document was returned but carries no text. That is not the same as an empty "
            "document: the endpoint gives no signal for content it could not extract")
    fidelity = text_fidelity(text)
    out["text_fidelity"] = fidelity
    if fidelity.get("association_uncertain"):
        # `03` section 7: when the number is what matters, the locator wins. Pointing at the
        # controlled document is the CORRECT answer for a specification limit, not a
        # best-effort extraction -- consistent with the system not owning recorded facts.
        locator = (out["slices"].get("locator") or {}).get("fields") or {}
        out["use_the_locator_instead"] = {
            "why": ("a specification limit or acceptance criterion read out of this text has "
                    "lost the association that makes it meaningful. The controlled document is "
                    "the answer for that, not an extraction from it"),
            "locators": locator,
        }
    if passages:
        # `03` section 3: the classification travels with EVERY passage. A passage lifted out of
        # this result and handed to an external tool otherwise arrives with nothing attached.
        out["passages"] = _select_passages(
            text, passages, classification=out["restriction"].get("classification"))
        # WAVE 8. NO MATCH IS A CLAIM, and it needs saying. Measured: `passages="acceptance
        # criterion"` against a 7,971-character meds document returned `passages: []` with no
        # `text` key -- a document with no content and no statement, which is a zero presented
        # as absence.
        #
        # `_select_passages` already does exactly this for the unusable-query case, with a
        # comment naming the hazard in those words. The no-match case -- the common one -- was
        # the branch that did not get it.
        #
        # The extent is stated because it is the difference between "this document does not
        # discuss it" and "this document has no text to search": both return an empty list.
        if not out["passages"]:
            out["no_passage_matched"] = {
                "searched_for": passages,
                "searched_chars": len(str(text or "")),
                "meaning": (
                    "the terms do not occur in this document's extracted text. That is not the "
                    "same as the document not addressing the subject: extraction loses table "
                    "structure, the wording may differ from yours, and matching here is literal. "
                    "Read the whole document, or use the locator, before concluding absence"),
            }
    else:
        out["text"] = text
    # Index currency and document currency are DIFFERENT FACTS. One document was modified
    # 2025-03-24 and indexed 2026-06-05 -- 14 months apart -- and only the second is about the
    # science.
    indexed, modified = body.get("timestamp_indexed"), body.get("modified_date")
    if isinstance(indexed, str) and isinstance(modified, str) and indexed[:10] > modified[:10]:
        out["currency"] = {
            "document_modified": modified, "index_saw_it": indexed,
            "note": ("these are different facts. The document has not changed since the first "
                     "date; the index only reflected it at the second. Index staleness is not "
                     "document staleness"),
        }
    if herr:
        out["document_error"] = herr
    if src.get("boundary_strength") == "broad":
        out["scope_caution"] = (
            f"{source} is scoped by a broad code that admits other programs' records, so this "
            "document's relevance to this program still has to be established")
    return out


def _select_passages(text: Any, query: str, *,
                     classification: Any = None) -> list[dict[str, Any]]:
    """Matching passages with their POSITION and which kind of material they came from.

    A located passage is citable, which is why position travels with it. And passages are
    reliable for PROSE -- rationale, justification, narrative -- and unreliable for tables, so
    each one says which it came from rather than leaving the reader to assume.
    """
    if not isinstance(text, str) or not text:
        return []
    terms = [t for t in re.split(r"\W+", query.lower()) if len(t) > 2]
    if not terms:
        # Reported, not silently returned as an empty list beside an absent `text`: the model
        # would otherwise get a document with no content and no statement that its query was
        # unusable. `passages="pH"` and `passages="EU/mL"` both reduce to nothing.
        return [{"unusable_query": (
            f"{query!r} yields no searchable term longer than two characters, so no passage "
            "could be located. Ask for the whole document, or use a longer term")}]

    # Window around each MATCH, not the first 2,000 characters of the containing block. An
    # earlier version split on blank lines, and real extracted text frequently has none -- a
    # tab-run table has none by construction. The whole document then became one paragraph and
    # the returned passage was characters 0-2000, labelled with the term it was searched for and
    # `position_chars: 0`, WITHOUT containing that term. A located, cited passage that does not
    # contain what it is cited for is worse than no passage.
    window = 900
    low = text.lower()
    spans: list[tuple[int, int, list[str]]] = []
    for term in terms:
        start = 0
        while True:
            at = low.find(term, start)
            if at < 0:
                break
            spans.append((max(0, at - window // 2), min(len(text), at + window // 2), [term]))
            start = at + len(term)
            if len(spans) > 40:
                break
    if not spans:
        return []
    spans.sort()
    merged: list[list[Any]] = []
    for lo, hi, hit in spans:
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
            merged[-1][2] = sorted(set(merged[-1][2]) | set(hit))
        else:
            merged.append([lo, hi, list(hit)])

    out: list[dict[str, Any]] = []
    for lo, hi, hit in merged[:20]:
        excerpt = text[lo:hi]
        tabular = bool(_TAB_RUN.search(excerpt)) or excerpt.startswith("\t")
        entry: dict[str, Any] = {
            "text": excerpt,
            # The offset in the ORIGINAL text, which is what makes a passage citable.
            "position_chars": lo,
            "matched": hit,
            "material": "tabular" if tabular else "prose",
            "excerpt_of_chars": len(text),
        }
        if lo > 0 or hi < len(text):
            entry["is_an_excerpt"] = (
                f"characters {lo}-{hi} of {len(text)}. The document continues either side of "
                "this, so it is not the whole of what the document says on the subject")
        if tabular:
            entry["association_uncertain"] = (
                "this passage came from tabular material whose row and column associations were "
                "not preserved, so any value in it may belong to a different attribute")
        if classification:
            entry["classification"] = classification
            entry["classification_travels"] = (
                f"{classification!r} travels with this passage wherever it goes, including "
                "across any external boundary")
        out.append(entry)
    return out


async def run_read(cache: dict[str, Any], documents: Any, purpose: Any,
                   passages: Any = None) -> dict[str, Any]:
    """Plan, read, and DIFF. The diff is the product."""
    # A tool call gets its own deadline — see `_begin_call`.
    _begin_call()
    # The second of `_ensure_index_facts`' two call sites. This path reads `readable`, which is
    # configuration-derived and NOT blanked by a reindex — but a re-read is what clears
    # `index_moved`, and leaving it set on this path would mean a session that only ever read
    # documents carried invalidated facts forward into the next population call.
    try:
        cache, _ = await _ensure_index_facts(cache)
    except BudgetExhausted:
        pass
    plan = plan_read(cache, documents, purpose, passages)
    if plan.get("refused"):
        return plan

    requested = plan["requested"]
    try:
        env = await _post(plan["endpoint"], plan["payload"])
    except BudgetExhausted as exc:
        # Every other execution path catches this. Letting it escape turned a stated coverage
        # limit into an MCP protocol error -- and offline tests cannot reach it, because
        # `_calls_used` increments only inside the real `_request`.
        return {
            "refused": False, "purpose": plan["purpose"],
            "requested_count": len(requested), "read_count": 0, "read": [],
            "not_read": [{"source": d["source"], "document": d["handle"],
                          "cause": exc.kind,
                          "detail": ("the call budget or deadline was reached before this read "
                                     "was sent, so its content is unknown rather than absent")}
                         for d in requested],
            "excluded_before_sending": plan["excluded"],
            "status": "not_read", "is_absence": False,
            "status_of_this_result": _TRANSIENCE,
        }
    if not env["_ok"]:
        reason = env["_status"].get("reason")
        executed = reason not in ("authentication_failure", "timeout", "connector_failure",
                                 "tls_verification_failure")
        return {
            "refused": False,
            # The SAME shape as a success, so nothing downstream branches on which it got --
            # including `purpose`, which an earlier version dropped here, and
            # `status_of_this_result`.
            "purpose": plan["purpose"],
            "requested_count": len(requested),
            "read_count": 0,
            "read": [],
            "status_of_this_result": _TRANSIENCE,
            # Built explicitly, NOT splatted from the request: `**d` carried the raw `card.id`
            # back to the model for every requested document on every non-200.
            "not_read": [{"source": d["source"], "document": d["handle"], "cause": reason,
                          "detail": env["_status"].get("detail")} for d in requested],
            "excluded_before_sending": plan["excluded"],
            "status": "failed",
            "is_absence": False,
            # A 404 means the request RAN and nothing was retrievable -- measured: one bogus id
            # alone returns 404. Calling that "did not execute" was wrong.
            "note": (("the read executed and the API returned nothing retrievable for any "
                      "requested document. That is not the same as those documents having no "
                      "content: an unknown identifier and a document you may not read are "
                      "indistinguishable here"
                      if reason == "unexpected_status" and executed else
                      "the read did not execute, which is not the same as these documents "
                      "having no content")
                     + (". A 403 means one requested source does not support full-text "
                        "retrieval and the whole batch was rejected"
                        if reason == "restricted_or_forbidden" else "")),
        }
    raw = env["_body"]
    returned = raw if isinstance(raw, list) else (raw.get("documents") if isinstance(raw, dict)
                                                  else []) or []

    # MATCH ON RETURNED `id`, NEVER ON POSITION. A `[meds, scited]` batch came back
    # `['scited','meds']`, so zipping by index would attribute one document's text to another --
    # a provenance failure nothing downstream would catch.
    # Keyed on the PAIR, because `card.id` is not established as globally unique and the request
    # schema itself requires `{datasource, document_id}` together -- the strongest available
    # signal that the id alone is not a key. `plan_read` already dedupes on the pair, so a batch
    # may legitimately hold the same id from two sources; keying on the id alone then attributed
    # one document's text to the other AND let a silent omission pass, in one call.
    by_id: dict[tuple[str, str], dict[str, Any]] = {}
    unkeyed = 0
    for d in returned:
        if not isinstance(d, dict) or not d.get("id"):
            # Neither matched nor reportable as unexpected. Counted, because in a tool whose
            # product is naming absence, a returned object vanishing is its own finding.
            unkeyed += 1
            continue
        by_id[(str(d.get("datasource") or ""), str(d["id"]))] = d
    read: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for want in requested:
        got = by_id.get((want["source"], want["id"]))
        if got is None:
            src = (cache.get("sources") or {}).get(want["source"]) or {}
            # Attribution in the order the connector can establish it. The endpoint gives NO
            # signal for either of the last two, which is `03` section 5's stated gap: it cannot
            # tell "you may not read this" from "that is not a document".
            # NOTE: a DENIED source is excluded in `plan_read` before sending, so that branch is
            # unreachable here by construction. What remains is genuinely inferential, and saying
            # "established" for it would claim more than this endpoint can support -- it cannot
            # tell "you may not read this" from "that is not a document".
            if src.get("access") is None:
                cause, how = "source_access_unknown", "the access check did not complete"
            else:
                cause, how = "identifier_not_found", "inferred by elimination"
            missing.append({
                "source": want["source"],
                # The model's own handle, so the absent document is identifiable without
                # disclosing any new identifier. Without this the model cannot tell WHICH of two
                # requested documents is missing, so it can neither retry nor attribute.
                "document": want["handle"],
                "cause": cause, "attribution": how,
                "detail": ("this document was requested and did not come back. The endpoint "
                           "drops unreadable documents silently and returns HTTP 200, so its "
                           "absence carries no signal of its own"
                           + ("" if cause != "identifier_not_found" else
                              ". This endpoint cannot distinguish 'you may not read this' from "
                              "'that is not a document' -- both are silent omissions")),
            })
            continue
        assembled = assemble_document(got, cache, purpose=plan["purpose"],
                                      passages=plan["passages"])
        if assembled:
            read.append(assembled)

    # Reported as a COUNT and the datasources involved, never as raw ids -- the model does not
    # hold identifiers, and handing one back gives it a string `plan_read` would refuse.
    extra = sorted(set(by_id) - {(d["source"], d["id"]) for d in requested})
    out: dict[str, Any] = {
        "refused": False,
        "purpose": plan["purpose"],
        # THE DIFF. Everything else here is metadata around this one act.
        "requested_count": len(requested),
        "read_count": len(read),
        "read": read,
        "not_read": missing,
        "excluded_before_sending": plan["excluded"],
        "status_of_this_result": _TRANSIENCE,
    }
    if plan.get("duplicates_removed"):
        out["duplicates_removed"] = plan["duplicates_removed"]
        out["duplicates_note"] = (
            "duplicate requests were removed before sending. The server also deduplicates, so "
            "without ours a repeated document would have shown up below as missing")
    if missing or plan["excluded"]:
        out["coverage_caution"] = (
            f"{len(read)} of {len(requested) + len(plan['excluded'])} requested documents were "
            "read. The others are listed with their attribution; their absence is not evidence "
            "about their content")
    if extra:
        out["unexpected_documents"] = {
            "count": len(extra), "sources": sorted({src for src, _ in extra}),
            "note": ("the endpoint returned documents that were not requested. They are not "
                     "included above, because nothing establishes what they are"),
        }
    if unkeyed:
        out["unidentifiable_documents"] = {
            "count": unkeyed,
            "note": ("the endpoint returned objects carrying no identifier, so they could not be "
                     "matched to anything requested and are not included above"),
        }
    classified = [d for d in read if (d.get("restriction") or {}).get("classification")]
    if classified:
        out["disclosure"] = (
            f"{len(classified)} of these documents carry a data classification, and it travels "
            "with any meaning derived from them -- including across any external boundary")
    tabular = [d for d in read if (d.get("text_fidelity") or {}).get("association_uncertain")]
    if tabular:
        out["fidelity_caution"] = (
            f"{len(tabular)} of these documents contain numeric limits or criteria inside "
            "tabular material whose associations were lost. For any such value, use the "
            "locator and read the controlled document at source")
    return out


@mcp.tool(annotations=_READ_ONLY)
async def muse_read(
    documents: Annotated[list[str], Field(
        description="Document handles from a `muse_population` result, or handles recorded in "
                    "the workspace earlier. Opaque -- not identifiers you construct.")],
    purpose: Annotated[str, Field(
        description="Required. What this read is meant to establish. Selects passages from a "
                    "large document, and is what makes the read qualifiable later.")],
    passages: Annotated[str | None, Field(
        description="Return only the parts of each document discussing this, with their "
                    "position, instead of the whole text.")] = None,
) -> dict[str, Any]:
    """Read what documents actually say, once metadata is not enough to answer.

    `documents` are handles from a `muse_population` result, or handles recorded in the
    workspace from an earlier session. Identifiers are not yours to construct.

    `purpose` is required: it states what the read is meant to establish, selects passages from
    a large document, and is what makes the read qualifiable if it ends up supporting a claim.

    `passages` returns the parts of each document that discuss something, with their position,
    instead of the whole text. "Where does this document discuss X" is a different and often
    more precise question than "give me this document".

    The result always diffs what was requested against what came back: this endpoint drops
    documents it cannot return and still answers HTTP 200. Extracted text is not structurally
    faithful -- table associations are lost -- so for a specification limit the locator is the
    answer rather than an extraction.
    """
    # BEFORE the warm, not after. If the eager startup warm was abandoned this issues 14 calls
    # inside the tool call, and `WARM_DEADLINE_S` wraps only the eager path — so without this the
    # lazy warm runs under a deadline that may already have expired. `run_read` resets again;
    # `_begin_call` is idempotent.
    _begin_call()
    cache = await warm_cache()
    return await run_read(cache, documents, purpose, passages)


def _parse_updates(body: Any) -> dict[str, dict[str, Any]]:
    """`data-updates` -> per-id freshness. Extracted in wave 8 so it has two callers."""
    out: dict[str, dict[str, Any]] = {}
    for entry in body if isinstance(body, list) else []:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        raw_date = str(entry.get("date") or "")
        out[str(entry["id"])] = {
            "absent": False, "unread": False,
            "date": None if raw_date.startswith(_EPOCH_PREFIX) else raw_date or None,
            "kind": str(entry.get("type") or "") or None,
            "never_indexed": raw_date.startswith(_EPOCH_PREFIX),
            "tz": "naive",
        }
    return out


async def refresh_freshness(cache: dict[str, Any]) -> dict[str, Any]:
    """Re-read index freshness before a tool call, and report whether it moved.

    WAVE 8. Freshness was read ONCE, in the startup cache, and every result reported that
    snapshot -- while `references/muse-search-guide.md` told the model it is "read fresh on every
    request". The consequence was worse than the wording: `population_staleness` compares a
    handle's mint-time index date against `src["freshness"]["date"]`, which is the same
    never-refreshed value, so within one process `current == minted` always held and the function
    emitted "the index has not moved since this population was measured" unconditionally. It had
    not re-read the index; it re-read its own snapshot, and `retrieval-contributor` is told to
    rely on that sentence.

    Not hypothetical. meds moved 1,014 -> 799 -> 1,014 in three days, so a long-lived process
    can straddle a reindex and affirm the opposite.

    Affordable: measured 80-90 ms and ~1.9 KB, one GET, twice in a row. One call per tool call.

    Failure LEAVES the snapshot and says so, rather than blanking freshness -- an unread re-read
    is a fact about this call, not about the source.
    """
    env = await _get(EP_UPDATES)
    if not env["_ok"]:
        return {"reread": False, "reason": env["_status"].get("reason", "unknown"),
                "note": ("index freshness could not be re-read for this call, so the dates below "
                         "are from connector startup and may be older than the index")}
    fresh = _parse_updates(env["_body"])
    moved: list[str] = []
    first_read: list[str] = []
    unread_still: list[str] = []
    vanished: list[str] = []
    for name, src in (cache.get("sources") or {}).items():
        prior = src.get("freshness") or {}
        now = fresh.get(name)
        if not now:
            # No entry in this response. Measured: 28 entries against 25 configured sources, and
            # three configured sources have no entry at all. Keep whatever was known -- including
            # `unread: True` -- and remember that this source is still unread, because
            # `updates_read` must not claim otherwise.
            if prior.get("unread"):
                unread_still.append(name)
            elif prior.get("date") and not prior.get("absent"):
                # WAVE 10 (R2b). A source that HAD a real date and has now dropped out of the
                # response. The old branch only recorded sources that were ALREADY unread, so this
                # case fell through `continue` and the stale date stood with no signal at all --
                # a date presented as current for a source the index no longer reports.
                #
                # Gated on `prior["date"]` deliberately: three configured sources have no entry
                # ever, `signals` among them, and re-flagging those on every call would say
                # something changed when nothing did. What is asserted here is a TRANSITION.
                src["freshness"] = {**prior, "absent": True, "vanished_since_startup": True}
                vanished.append(name)
            continue
        # `None` IS UNKNOWN, and comparing it as a value fabricated movement. If startup's
        # `data-updates` call failed non-fatally -- a 500, a timeout -- every source carries
        # `{"date": None, "unread": True}` and the cache is still memoised, so the FIRST successful
        # re-read found `None != <a real date>` on all five and reported the index as having moved
        # for every one of them. Nothing had moved; nothing had been read. That is this module's own
        # rule broken inside the function written to fix a different instance of it.
        # NEVER INDEXED is excluded. `_parse_updates` sets `date: None` AND `never_indexed: True`
        # for the epoch sentinel, so such a source satisfied `date is None` on every call and was
        # reported as a "first reading" forever -- under a note saying the dates now reported are a
        # first reading, when the date is still None and nothing was ever indexed. That is the
        # conflation waves 1 and 8 both forbid, in the function written to feed `population_staleness`.
        if prior.get("never_indexed") and (now or {}).get("never_indexed"):
            pass
        elif prior.get("unread") or prior.get("date") is None:
            first_read.append(name)
        elif prior.get("date") != now.get("date"):
            moved.append(name)
            # WAVE 10 (R2a). The index moved, so every fact DERIVED FROM THAT INDEX is now a
            # statement about a state that no longer exists. Blanked to `None`, which is UNKNOWN
            # everywhere in this module and never means False. Before this, `moved_since_startup`
            # was reported and invalidated nothing: `diagnose_zero` went on ruling
            # `nonexistent_condition_field` out from a mapping read against the old index and
            # shipped `is_absence: True` on the strength of it.
            #
            # The line is INDEX-DERIVED vs CONFIGURATION-DERIVED, and that line is the whole
            # judgment here. `knn` and `readable` come from the configuration endpoint --
            # `projectKnnSettings` and `fullDocumentTextRetrievalEnabled` -- so a reindex says
            # nothing about them and blanking them would refuse semantic retrieval and document
            # reads for no reason. Configuration drift has its own detector, `config_fingerprint`.
            #
            # CORRECTED after the integrated review, which found the first version of this list
            # incomplete and this comment wrong for claiming otherwise. `mapping_complete` comes
            # from the same ingest call as `mapping_retrieved`, and `field_validation` reads it
            # FIRST -- so blanking one and not the other shipped
            # `field_validation: "authoritative"` beside prose saying the mapping was never
            # retrieved this session. `facetable_program_filter` comes from the same facets call as
            # `computed_facets` and gates the poison rule, so a reindex that made a filter field
            # non-facetable would have silently zeroed a distribution.
            for fact in ("mapping_retrieved", "mapping_complete", "computed_facets",
                         "filter_field_terms", "facetable_program_filter"):
                src[fact] = None
            src["index_moved"] = True
        src["freshness"] = now
    # NOT unconditional. Set only when nothing in scope is still unread, otherwise
    # `population_staleness` reports "freshness was never read this session" for a source whose
    # entry is genuinely ABSENT -- which has its own stronger branch -- while `cache_status()`
    # simultaneously reports `index_freshness_read: True` and `index_date_unread: True`.
    # The flag is about OUR CALL, not about per-source coverage. Gating it on `unread_still` meant
    # it could never recover: three of 25 configured sources have no `data-updates` entry, so after
    # one failed startup they stayed unread on every successful re-read and `updates_read` was
    # False for the life of the process -- while per-source dates were populated and
    # `index_date_unread` was False. Directly against this flag's documented meaning.
    #
    # A source with no entry is ABSENT, which is a finding about the source and has its own key.
    cache["updates_read"] = True
    for _n in unread_still:
        _s = (cache.get("sources") or {}).get(_n) or {}
        _s["freshness"] = {**(_s.get("freshness") or {}), "unread": False, "absent": True}
    out: dict[str, Any] = {"reread": True}
    if moved:
        out["moved_since_startup"] = moved
        out["note"] = ("the index moved for these sources after this connector started, so a "
                       "count taken earlier in this session may no longer hold")
    if first_read:
        out["first_read_this_call"] = first_read
        out["first_read_note"] = (
            "freshness for these sources was not known before this call, so the dates now "
            "reported are a first reading and not evidence of a change")
    if unread_still:
        out["still_unread"] = unread_still
        # RESTORED to this block after the integrated review. Wave 10 mis-indented it into
        # the `vanished` block below, so `still_unread` shipped with no note at all -- and
        # the note shipped beside `vanished_note` instead, saying currency is "unknown
        # rather than old", which is the OPPOSITE of the vanished finding. A misattributed
        # sentence, not merely a missing one.
        out["still_unread_note"] = (
            "these sources have no entry in the index-freshness response at all, so their "
            "currency is unknown rather than old")
    if vanished:
        # A finding about the source, not about our call, and stronger than a stale date: the index
        # no longer reports this source at all, so its recorded date describes a state nothing
        # confirms.
        out["vanished_since_startup"] = vanished
        out["vanished_note"] = (
            f"{', '.join(vanished)} had an index date at connector startup and no longer appears "
            "in the index-freshness response. The date previously recorded is not current and "
            "nothing establishes what is")
    return out


async def _ensure_index_facts(cache: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Re-establish the index-derived facts a reindex invalidated. Returns (cache, report).

    WAVE 10 (R2a), and the shape is the owner's choice: blank on detection, re-read on FIRST USE.
    So a process that straddles a reindex pays nothing until a call actually needs facts that the
    move invalidated, rather than paying at the moment of detection.

    It re-reads through `warm_cache(force=True)` rather than re-implementing the two per-source
    calls. That is eager across sources where a per-source re-read would be surgical -- 15 calls
    against 3 -- and it is the right trade here: the ingest and facet loops derive `mapping_complete`,
    the field set, the taxonomy branch map, the poison-rule predicate and `filter_field_terms`
    through logic tangled with `warm_cache`'s own failure bookkeeping. A second copy of that
    derivation is precisely the producer/consumer drift that left the configured-label map dead for
    six waves. One reindex per long-lived process, **15** calls out of a 500 budget -- the
    rebuild issues its own `/data-updates` on top of the one `refresh_freshness` already made
    -- and no duplicated derivation. This said 14 until the integrated review counted it.

    `force=True` REBUILDS the cache object, so the caller must rebind to what this returns. The old
    dict keeps the pre-move facts and nothing updates it.
    """
    moved = sorted(n for n, s in (cache.get("sources") or {}).items() if s.get("index_moved"))
    if not moved:
        return cache, None
    rebuilt = await warm_cache(cache.get("program"), force=True)
    return rebuilt, {
        "reason": "index_moved",
        "sources": moved,
        "note": (f"{', '.join(moved)} reindexed since this connector started, so the field "
                 "mapping, computed facet list and program-filter evidence for them described an "
                 "index that no longer exists. They were re-read before this request was planned"),
        # `degraded` entries are dicts -- `{"call", "reason", "fatal"}` -- so this reports the calls
        # that failed, not the dicts. Sorting the dicts raised TypeError, caught by the fault case
        # on its first run: I assumed a list of strings without looking.
        "re_read_failed": sorted({str(d.get("call")) for d in (rebuilt.get("degraded") or [])
                                  if isinstance(d, dict)}) or None,
    }


async def run_population(cache: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Plan, execute and assemble. The tool body, separated so it is testable without MCP."""
    # A tool call gets its own deadline. See `_begin_call`: this used to be process uptime, so a
    # server 15 minutes old refused every call before sending anything.
    _begin_call()
    # BEFORE planning, so staleness compares against the live index rather than our own
    # snapshot. Inside the budget, and a failure degrades to the snapshot with that stated.
    try:
        freshness_read = await refresh_freshness(cache)
    except BudgetExhausted as exc:
        freshness_read = {"reread": False, "reason": exc.kind,
                          "note": ("the call budget or deadline was reached before index "
                                   "freshness could be re-read; the dates below are from "
                                   "connector startup")}
    # BEFORE planning. Planning validates fields against the mapping and decides the facet scope
    # from the computed-facet list, so it must not run on facts a reindex just invalidated. A
    # failure here degrades rather than unwinds: the blanked facts stay UNKNOWN and every guard
    # that reads them already treats UNKNOWN as "not established".
    try:
        cache, refetched = await _ensure_index_facts(cache)
    except BudgetExhausted as exc:
        cache, refetched = cache, {"reason": exc.kind, "note": (
            "the index moved and the budget was reached before its field mapping and facet list "
            "could be re-read, so they stay UNKNOWN rather than stale")}
    planned = plan_population(cache, **kwargs)
    if planned.get("refused"):
        return planned

    results = []
    for plan in planned["requests"]:
        try:
            results.append(await _execute_plan(plan, cache))
        except BudgetExhausted as exc:
            # Truncation of COVERAGE, and it must be reported as such rather than leaving the
            # remaining sources looking like they had nothing.
            results.append({
                "source": plan["source"], "status": "not_searched",
                "reason": exc.kind, "is_absence": False,
                "note": ("the call budget or deadline was reached before this source was "
                         "searched, so its coverage is unknown rather than empty"),
            })

    ok = [r for r in results if r["status"] == "ok"]
    out: dict[str, Any] = {
        "program": cache.get("program"),
        "engine": planned["engine"],
        # NEVER a single total. `01` section 1b: each source has its own totalHits and a sum
        # is only meaningful if stated as a sum.
        "per_source": results,
        "sources_searched": [r["source"] for r in ok],
        "sources_not_searched": [
            {"source": r["source"], "reason": r.get("reason") or r.get("cause"),
             "detail": r.get("detail") or r.get("message")}
            for r in results if r["status"] != "ok"],
        "excluded_before_searching": planned.get("excluded") or [],
        "grouping": planned["grouping"],
        "narrowing": planned["narrowing"],
        "population": None,
        # `08` of the transience rule: raw retrieval creates nothing durable.
        "status_of_this_result": _TRANSIENCE,
    }
    # WAVE 8. Whether the per-source index dates below were read for THIS call or inherited from
    # connector startup. Emitted only when it is a finding -- a re-read that found no movement is
    # the ordinary case and says nothing worth a key.
    # EVERY finding, not just failure and movement. `first_read_this_call` and `still_unread` were
    # built and then dropped here -- and `still_unread`'s sentence ("no entry in the
    # index-freshness response at all, so their currency is unknown rather than old") is the one
    # distinction in this function the model cannot derive from anywhere else in the result.
    # `vanished_since_startup` ADDED after the integrated review, which found R2b's whole
    # finding computed, reported by `refresh_freshness`, and then dropped here -- the same
    # defect the note above says was fixed for the other two keys. A source that had a date
    # and no longer appears is the strongest freshness finding this function can make.
    if (not freshness_read.get("reread") or freshness_read.get("moved_since_startup")
            or freshness_read.get("first_read_this_call")
            or freshness_read.get("still_unread")
            or freshness_read.get("vanished_since_startup")):
        out["index_freshness"] = freshness_read
    if refetched:
        # Stated, not silent. A reindex mid-session means the counts in this result and any count a
        # handle recorded before it describe different indexes, and nothing else in the result
        # would say so.
        out["index_facts_refetched"] = refetched
    if out["sources_not_searched"] or out["excluded_before_searching"]:
        out["coverage_caution"] = (
            f"{len(ok)} of {len(results) + len(out['excluded_before_searching'])} sources "
            "answered. The others are listed with their reasons; this result does not "
            "describe them, and their silence is not absence")

    # A handle PER SOURCE, because a population is per source. Minted from what actually ran,
    # so re-sending it reproduces the same records rather than a restatement of them.
    for r in ok:
        plan = next(p for p in planned["requests"] if p["source"] == r["source"])

        # WAVE 10 (R6). NOT minted on the semantic engine. A handle's stated job is "re-send and
        # get the same records", and this loop minted one for every answering source with no
        # engine check -- while `route_engine` refuses any inherited basic-engine handle outright,
        # because the recipe is a query embedding rather than a set of conditions. So the model was
        # handed a token that could only ever be refused.
        #
        # Not minting is the fix rather than encoding the recipe, and the reason is the count: on
        # this engine it is `totalHitsKnn`, a count of CHUNKS near an embedding, and
        # `count_defensible` is False because server-side ranking, synonym expansion and query
        # rewriting all touch it. A handle exists so two counts can be compared. Nothing here is
        # comparable, so there is nothing for a handle to preserve.
        #
        # The self-description half of the finding is answered separately, by echoing the text
        # that was matched -- see `question_asked` on the per-source result.
        # WAVE 10 FOLLOW-UP (S1). The DECISION still keys on the engine -- a basic-engine request
        # cannot be replayed whatever lane answered it, because the recipe is a query embedding and
        # `route_engine` refuses any inherited basic handle. But the REASON keys on the effective
        # lane: on a downgrade this claimed "its count is of chunks near that embedding" in the same
        # object as a term count and `counts_what` describing documents.
        if plan["engine"] == "basic":
            r["population_not_offered"] = (
                "no population handle for this result. This is the semantic engine, whose recipe "
                "is a query embedding rather than a set of conditions, so a handle could not "
                "replay it"
                + (" -- and its count is of chunks near that embedding, which is not comparable "
                   "with a later count anyway."
                   if r.get("match_kind") == LANE_SIMILARITY else
                   ". This source returned no vector matches, so the records are TERM matches, but "
                   "the request still cannot be replayed from a handle.")
                + " The text this matched against is under `question_asked`; to hold a population "
                "you can narrow and re-measure, run the same request with `where` instead")
            continue
        handle, herr = encode_population_handle(
            source=r["source"], engine=plan["engine"],
            program_filter=plan["program_filter"],
            # Mint the ALREADY-COMPILED wire clause, so `_compile_inherited` takes its
            # pass-through fast path and replay is byte-exact.
            #
            # An earlier version reconstructed model vocabulary and dropped `type` and
            # `match`, which broke replay three ways -- each measured by the review gate:
            #   * `MUST_NOT` inverted. A reconstructed clause has neither `type` nor `text`,
            #     so it recompiled as MUST: re-narrowing a `where_not` population searched
            #     EXACTLY the set the caller had excluded, while staleness reported the index
            #     unmoved so the old count read as still valid.
            #   * `match: "exact"` degraded to `phrase`, because the reconstruction carried no
            #     `match` and `_resolve_role` strips `.raw` -- so an exact population of 400
            #     re-narrowed against an analysed phrase match that also admits "Final Draft".
            #   * a taxonomy clause ALWAYS refused: `text.text` is an L-path and the
            #     recompile looked it up in the label map, missed, and reported "the source's
            #     schema or vocabulary changed" when nothing had.
            #
            # `00-surfaced-tools.md` section 4 illustrates the recipe in model vocabulary. Its
            # stated JOB is "re-send and get the same records", and the compiled form is the
            # only shape that guarantees it. The label a caller used is preserved separately
            # in `conditions_as_asked` for display.
            conditions=[dict(c) for c in (plan.get("conditions") or [])],
            count=r["count"].get("records_matched"),
            facetable=(plan.get("poison") or {}).get("facetable_program_filter") is True,
            index_updated=r.get("index_date"))
        r["population"] = handle
        if herr:
            r["population_error"] = herr
        staleness = plan.get("inherited", {}) or {}
        if staleness.get("staleness"):
            r["inherited_population"] = staleness
    return out


@mcp.tool(annotations=_READ_ONLY)
async def muse_population(
    sources: Annotated[list[str] | None, Field(
        description="Datasource ids to search. Omit for every source in program scope. One "
                    "request is issued per source and the results are never merged.")] = None,
    about: Annotated[str | None, Field(
        description="Free text. Matched as a term across everything the source indexes -- or, "
                    "with semantic=True and no `question`, used as the text to match "
                    "meaning against instead.")] = None,
    where: Annotated[list[dict[str, Any]] | None, Field(
        description='Constraints, all of which must hold. Each is {"in": [field roles], '
                    '"any_of": [values], "match": "phrase"|"exact"}. `in` takes a role -- '
                    "anywhere, title, body, status, or a taxonomy role such as issue or "
                    "manufacturing_step. A literal field name from this source's own mapping "
                    "also resolves, but a role is stable across sources where a field name is "
                    "not. A taxonomy role needs a clause of its own. `any_of` is OR-ed. "
                    "`match: exact` needs the exact stored value -- from a distribution, or "
                    "from `value_prefix`.")] = None,
    where_not: Annotated[list[dict[str, Any]] | None, Field(
        description="Same clause shape; none of these may match.")] = None,
    since: Annotated[str | None, Field(
        description="ISO date lower bound. Applied by this connector, not the index, and "
                    "both counts are reported.")] = None,
    until: Annotated[str | None, Field(description="ISO date upper bound.")] = None,
    population: Annotated[str | None, Field(
        description="A population handle from an earlier result. Narrows exactly that "
                    "population instead of restating it as a fresh query.")] = None,
    semantic: Annotated[bool, Field(
        description="Retrieve by similarity rather than by term. Needs `question`. Its count "
                    "is reported as not defensible, and most sources cannot do it -- which ones "
                    "is read from the index configuration at startup, not fixed here. Those are "
                    "refused, never downgraded silently.")] = False,
    question: Annotated[str | None, Field(
        description="Natural-language question driving the similarity lane.")] = None,
    order: Annotated[Literal["relevance", "newest", "oldest"], Field(
        description="`newest` and `oldest` are not inverses -- they order different date "
                    "fields, and the result names which.")] = "relevance",
    depth: Annotated[Literal["sample", "survey", "complete"], Field(
        description="`complete` returns the whole population in one call and is what a "
                    "defensible count needs. `sample` and `survey` are REFUSED today: "
                    "mapping them onto record counts is deferred, and guessing would cap an "
                    "exhaustive question silently.")] = "complete",
    offset: Annotated[int, Field(
        description="Paging offset. Narrow a large population rather than paging "
                    "through it.")] = 0,
    fields: Annotated[list[str] | None, Field(
        description="Extra record fields beyond each source's own defaults. Names are "
                    "validated against the real mapping; unknown ones are dropped and "
                    "reported. Asking for the document body here is refused with the "
                    "reason -- reading is `muse_read`.")] = None,
    distribution: Annotated[Any, Field(
        description="True for the value distribution across the population -- this is how you "
                    "learn what to narrow on. A list picks fields. False skips it, saving a "
                    "call per source (two where the distribution has to be taken over a "
                    "superset).")] = True,
    value_prefix: Annotated[dict[str, str] | None, Field(
        description='Prefix-search a field\'s values, e.g. {"status": "Eff"}, to find the '
                    "exact stored value before constraining on it. Several fields are "
                    "allowed, one request each.")] = None,
) -> dict[str, Any]:
    """Define or narrow a program-scoped population, and see its records, count and shape.

    A large count is a population, not an answer. Narrow it with `where` using a value taken
    from the distribution, rather than paginating through it.

    `where` and `where_not` take clauses: `{"in": [field roles], "any_of": [values],
    "match": "phrase"|"exact"}`. `any_of` is OR-ed. `in` takes a role -- `anywhere`, `title`,
    `body`, `status` -- or, on sources that have them, a taxonomy role such as `issue`,
    `manufacturing_step` or `analytical_method`. Roles a source cannot represent are reported,
    never substituted.

    `population` narrows a population you already hold, using the handle from a prior call.
    `semantic=True` with a `question` retrieves by similarity instead of by term; the result
    says which engine ran, because their scores are not comparable.

    Every count says what it is a count of and whether it is defensible. An empty result
    always says which causes of a false zero were ruled out.
    """
    # BEFORE the warm — same reason as `muse_read`. See `_begin_call`.
    _begin_call()
    cache = await warm_cache()
    return await run_population(
        cache, sources=sources, about=about, where=where, where_not=where_not,
        since=since, until=until, population=population, semantic=semantic,
        question=question, order=order, depth=depth, offset=offset, fields=fields,
        distribution=distribution, value_prefix=value_prefix)


def _warm_at_startup() -> None:
    """Load the cache EAGERLY, before the stdio loop starts.

    Eager rather than on-first-call, deliberately. Lazy loading cannot distinguish "the
    cache is building" from "this search is slow", and it defers an authentication
    failure until the middle of scientific work. Eager surfaces it once, up front, where
    it can be reported as a limitation instead of as an empty result.

    Failures are printed to stderr -- which `.mcp.json` redirects to
    `logs/ics-muse.stderr.log` -- and never raised. An expired SESSION at startup must not
    prevent the server from answering, because a tool that reports
    "retrieval is unavailable because the SESSION is invalid" is useful and a server that
    does not start is not. `cache_status()` carries the reason.

    SESSION acquisition is deliberately NOT here. Reauthentication is a human browser
    login today; `auth/muse_session.py` owns capture and keepalive.
    """
    async def _bounded() -> dict[str, Any]:
        # Capped, because this runs BEFORE `mcp.run()`. Uncapped, the worst case is 14
        # sequential calls at the 60s per-call TIMEOUT -- and DEADLINE_S does not save us,
        # because it is checked before each call and 840s is under 900. The server would
        # then fail by hanging through MCP init rather than by raising, which is the worse
        # failure. On timeout `_CACHE` is left unset so the next call retries.
        return await asyncio.wait_for(warm_cache(), timeout=WARM_DEADLINE_S)

    try:
        status = asyncio.run(_bounded())
    except TimeoutError:
        print(f"[ics-muse] startup cache exceeded {WARM_DEADLINE_S}s and was abandoned; "
              "retrieval will report itself unavailable until it succeeds",
              file=sys.stderr, flush=True)
        return
    except Exception as exc:  # noqa: BLE001 - startup must never take the server down
        print(f"[ics-muse] startup cache raised {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)
        return
    n_ok = sum(1 for s in status["sources"].values() if s["searchable"])
    print(f"[ics-muse] startup cache: ok={status['ok']} "
          f"sources={len(status['sources'])} searchable={n_ok} "
          f"calls={_calls_used} degraded={len(status['degraded'])}",
          file=sys.stderr, flush=True)
    for d in status["degraded"]:
        print(f"[ics-muse]   {'FATAL' if d['fatal'] else 'degraded'} "
              f"{d['call']}: {d['reason']}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    _warm_at_startup()
    mcp.run()
