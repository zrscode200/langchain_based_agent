"""Private Europe PMC adapter for bounded MED/PMC literature mapping."""

from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import unquote

from metadata import europe_metadata
from contracts import normalize_doi
from disclosure import AttemptBudgetExceeded
from envelopes import coverage, currentness, failure, stages, utc_now, warning
from http_client import ProviderHttpClient, status_failure
from normalization import candidate
from validation import bounded_text, json_within_limits, unique_identities


__all__ = ["map_records"]

_PROVIDER = "europe_pmc"
_COLLECTIONS = {"MED", "PMC"}
_PMID_RE = re.compile(r"[0-9]{1,12}")
_PMCID_RE = re.compile(r"PMC[0-9]+", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_TAG_RE = re.compile(r"<[^>]{0,500}>")
_MAX_PROVIDER_INTEGER = (1 << 63) - 1
_MISSING = object()


def _effective_request(
    endpoint_id: str,
    fields: list[tuple[str, Any]],
) -> dict[str, Any]:
    return {
        "adapter": _PROVIDER,
        "endpoint_id": endpoint_id,
        "method": "GET",
        "fields": [{"name": name, "value": value} for name, value in fields],
    }


def _retrieval_currentness(
    retrieved_at: str,
    provider_version: str | None = None,
) -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    if provider_version:
        observations.append(
            {
                "role": "provider_version",
                "value": bounded_text(provider_version, 100),
                "source": _PROVIDER,
                "state": "represented",
            }
        )
    observations.append(
        {
            "role": "retrieved_at",
            "value": retrieved_at,
            "source": _PROVIDER,
            "state": "represented",
        }
    )
    return currentness(observations, drift="unknown")


def _clean_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    without_tags = _TAG_RE.sub(" ", html.unescape(value))
    return bounded_text(without_tags, limit)


def _parse_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= _MAX_PROVIDER_INTEGER else None
    if (
        isinstance(value, str)
        and 1 <= len(value) <= 19
        and all("0" <= character <= "9" for character in value)
    ):
        parsed = int(value)
        return parsed if parsed <= _MAX_PROVIDER_INTEGER else None
    return None


def _valid_cursor(value: Any) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4_096
        or _CONTROL_RE.search(value)
    ):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


class _JsonParserLimitError(ValueError):
    """A provider JSON body exceeded the runtime parser's safe depth."""


def _safe_json(result: Any) -> Any | None:
    try:
        return result.json()
    except RecursionError as exc:
        raise _JsonParserLimitError from exc
    except (UnicodeDecodeError, ValueError, TypeError):
        return None


def _provider_error_detail(payload: dict[str, Any]) -> str | None:
    error_keys = (
        "errCode",
        "errMsg",
        "error",
        "errors",
        "errorMessage",
        "errorMessages",
    )
    represented = [
        key
        for key in error_keys
        if key in payload and payload[key] not in (None, "", [], {})
    ]
    if not represented:
        status = payload.get("status")
        if isinstance(status, str) and status.casefold() in {"error", "failed"}:
            represented.append("status")
    if not represented:
        return None
    return "Europe PMC returned an HTTP-200 error body (" + ", ".join(represented) + ")"


def _validation_failure(
    output: dict[str, Any],
    code: str,
    scope: str,
    detail: str,
) -> dict[str, Any]:
    output["stages"] = stages(
        validation="failed",
        disclosure="not_requested",
        result="failed",
    )
    output["failures"].append(
        failure(code, "validation", scope, detail, retryable=False)
    )
    return output


def _transport_failure(
    output: dict[str, Any],
    result: Any,
    *,
    scope: str,
) -> bool:
    problem = status_failure(result)
    if problem is None:
        output["stages"]["transport"] = "succeeded"
        return False
    code, detail, retryable = problem
    output["stages"]["transport"] = "failed"
    output["stages"]["result"] = "failed"
    output["failures"].append(
        failure(code, "transport", scope, detail, retryable=retryable)
    )
    return True


def _provider_failure(
    output: dict[str, Any],
    code: str,
    detail: str,
    *,
    stage: str = "provider_body",
    scope: str = "europe_pmc.response",
    absence: bool = False,
) -> dict[str, Any]:
    output["stages"][stage] = "failed"
    output["stages"]["result"] = "failed"
    output["failures"].append(
        failure(
            code,
            stage,
            scope,
            detail,
            retryable=False,
            absence=absence,
        )
    )
    return output


def _source_filter(source_collections: list[str]) -> str:
    clauses = [f"SRC:{value}" for value in source_collections]
    return clauses[0] if len(clauses) == 1 else "(" + " OR ".join(clauses) + ")"


def _provider_query(
    compiled_query: str,
    source_collections: list[str],
) -> str:
    if not source_collections:
        return compiled_query
    return f"({compiled_query}) AND {_source_filter(source_collections)}"


def _provider_sort(order: str) -> str:
    return "" if order == "relevance" else "FIRST_PDATE_D desc"


def _map_base(
    *,
    provider_query: str,
    page_size: int,
    order: str,
    cursor: str,
    searched_scope: str,
    source_collections: list[str],
    retrieved_at: str,
) -> dict[str, Any]:
    return {
        "provider": _PROVIDER,
        "source_collections": list(source_collections),
        "effective_request": _effective_request(
            "search",
            [
                ("query", provider_query),
                ("format", "json"),
                ("resultType", "core"),
                ("pageSize", page_size),
                ("cursorMark", cursor),
                ("sort", _provider_sort(order)),
                ("synonym", False),
            ],
        ),
        "stages": stages(
            validation="succeeded",
            disclosure="not_requested",
            request_build="succeeded",
        ),
        "query_fidelity": "unknown",
        "provider_order": _provider_sort(order) or "relevance",
        "searched_scope": searched_scope,
        "candidates": [],
        "failures": [],
        "warnings": [],
        "currentness": _retrieval_currentness(retrieved_at),
        "count_observation": {
            "hit_count": None,
            "returned_result_count": 0,
            "represented_candidate_count": 0,
        },
        "cursor_observation": {
            "requested_cursor": cursor,
            "next_cursor": None,
            "state": "unknown",
        },
        "continuation_observation": {
            "state": "unknown",
            "next_cursor": None,
        },
        "coverage": coverage(
            "the exact Europe PMC query, source collections, sort, and cursor page",
            0,
            limitations=["cursor pages are not a stable historical snapshot"],
        ),
        "call_count": 0,
    }


def _represented_aliases(item: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    identities: list[dict[str, Any]] = []
    omitted = 0

    pmid = item.get("pmid")
    if isinstance(pmid, str) and _PMID_RE.fullmatch(pmid):
        identities.append({"kind": "pmid", "value": pmid})
    elif pmid not in (None, ""):
        omitted += 1

    pmcid = item.get("pmcid")
    if (
        isinstance(pmcid, str)
        and 4 <= len(pmcid) <= 20
        and _PMCID_RE.fullmatch(pmcid)
    ):
        identities.append({"kind": "pmcid", "value": pmcid.upper()})
    elif pmcid not in (None, ""):
        omitted += 1

    doi = item.get("doi")
    if isinstance(doi, str) and doi.strip():
        try:
            identities.append({"kind": "doi", "value": normalize_doi(doi)})
        except ValueError:
            omitted += 1
    elif doi not in (None, ""):
        omitted += 1

    return (unique_identities(identities) if identities else []), omitted


def _authors(item: dict[str, Any]) -> tuple[list[str], int | None]:
    author_list = item.get("authorList")
    if isinstance(author_list, dict) and isinstance(author_list.get("author"), list):
        names: list[str] = []
        for author in author_list["author"]:
            if not isinstance(author, dict):
                continue
            name = _clean_text(author.get("fullName"), 300)
            if not name:
                name = bounded_text(
                    " ".join(
                        value
                        for value in (
                            _clean_text(author.get("firstName"), 150),
                            _clean_text(author.get("lastName"), 150),
                        )
                        if value
                    ),
                    300,
                )
            if name:
                names.append(name)
        return names, len(names)

    combined = _clean_text(item.get("authorString"), 500)
    return ([combined] if combined else []), None


def _publication_types(item: dict[str, Any]) -> list[str]:
    values: list[str] = []
    raw_list = item.get("pubTypeList")
    if isinstance(raw_list, dict):
        raw_list = raw_list.get("pubType")
    if isinstance(raw_list, list):
        values.extend(
            clean
            for raw in raw_list[:100]
            if (clean := _clean_text(raw, 200))
        )
    elif isinstance(raw_list, str):
        clean = _clean_text(raw_list, 200)
        if clean:
            values.append(clean)

    raw = item.get("pubType")
    if isinstance(raw, list):
        values.extend(
            clean
            for value in raw[:100]
            if (clean := _clean_text(value, 200))
        )
    elif isinstance(raw, str):
        clean = _clean_text(raw, 200)
        if clean:
            values.append(clean)
    return list(dict.fromkeys(values))


def _record_class(source: str) -> tuple[str, str]:
    return "publication", "publication"


def _map_candidate(
    item: dict[str, Any],
    *,
    searched_scope: str,
    order: str,
    page_position: int,
    start_position: int,
    retrieved_at: str,
    include_abstract: bool = False,
    exact_record: bool = False,
    metadata: list[str] | None = None,
) -> tuple[dict[str, Any] | None, int]:
    source = item.get("source")
    source_id = item.get("id")
    if (
        not isinstance(source, str)
        or source not in _COLLECTIONS
        or not isinstance(source_id, str)
        or not source_id.strip()
        or len(source_id) > 255
    ):
        return None, 0
    if source == "MED" and not _PMID_RE.fullmatch(source_id):
        return None, 0
    if source == "PMC" and not _PMCID_RE.fullmatch(source_id):
        return None, 0

    identity = {
        "kind": "europe_pmc",
        "collection": source,
        "value": source_id,
    }
    aliases, alias_omissions = _represented_aliases(item)
    contributors, contributor_count = _authors(item)
    bounded_abstract = _clean_text(item.get("abstractText"), 20_001)
    abstract_truncated = len(bounded_abstract) > 20_000
    abstract = bounded_abstract[:20_000]
    journal_info = item.get("journalInfo")
    journal = journal_info.get("journal") if isinstance(journal_info, dict) else None
    container = (
        _clean_text(journal.get("title"), 500)
        if isinstance(journal, dict)
        else ""
    )
    date = ""
    date_role = None
    for name, role in (
        ("firstPublicationDate", "first_publication"),
        ("electronicPublicationDate", "electronic_publication"),
        ("firstIndexDate", "first_index_date"),
        ("pubYear", "publication_year"),
    ):
        date = _clean_text(item.get(name), 80)
        if date:
            date_role = role
            break
    publication_types = _publication_types(item)
    record_class, record_role = _record_class(source)

    access_signals: list[dict[str, Any]] = []
    for field, kind in (
        ("isOpenAccess", "open_access"),
        ("inPMC", "pmc_full_text"),
        ("hasPDF", "provider_pdf"),
    ):
        represented = item.get(field)
        if represented is not None and not isinstance(represented, str):
            raise ValueError(f"Europe PMC {field} was not a string")
        if represented in {"Y", "N"}:
            access_signals.append(
                {
                    "kind": kind,
                    "state": "represented",
                    "value": represented == "Y",
                    "source": _PROVIDER,
                }
            )

    integrity_signals: list[dict[str, Any]] = []
    if item.get("isRetracted") == "Y":
        integrity_signals.append(
            {
                "kind": "retracted",
                "state": "represented",
                "source": _PROVIDER,
                "detail": "Europe PMC represented isRetracted=Y",
            }
        )

    observations = [
        {
            "role": "retrieved_at",
            "value": retrieved_at,
            "source": _PROVIDER,
            "state": "represented",
        }
    ]
    first_index_date = _clean_text(item.get("firstIndexDate"), 80)
    if first_index_date:
        observations.insert(
            0,
            {
                "role": "first_index_date",
                "value": first_index_date,
                "source": _PROVIDER,
                "state": "represented",
            },
        )

    card = candidate(
        identity=identity,
        represented_identifiers=aliases,
        provider=_PROVIDER,
        record_class=record_class,
        record_role=record_role,
        title=_clean_text(item.get("title"), 1_000) or None,
        contributors=contributors,
        contributor_count=contributor_count,
        container=container or None,
        date=date or None,
        date_role=date_role,
        searched_scope=searched_scope,
        matched_scope=searched_scope,
        provider_order=order,
        absolute_position=start_position + page_position - 1,
        context_kind="abstract_excerpt" if abstract else "none",
        context_text=abstract or None,
        context_source_role="abstract" if abstract else None,
        integrity_signals=integrity_signals,
        access_signals=access_signals,
        currentness_state=currentness(observations, drift="unknown"),
    )
    card.update(
        {
            "source_collection": source,
            "source_id": source_id,
            "publication_roles": publication_types,
            "abstract_available": bool(abstract),
            "peer_review_state": {"state": "unknown", "value": None},
            "provider_page_position": page_position,
        }
    )
    if exact_record:
        card["contributors"] = contributors[:100]
        card["abstract_state"] = (
            "represented"
            if include_abstract and abstract
            else "not_represented"
            if include_abstract
            else "not_requested"
        )
    if include_abstract:
        card["abstract"] = abstract or None
        card["abstract_truncated"] = abstract_truncated
    if metadata:
        card["metadata"] = europe_metadata(item, metadata)
    return card, alias_omissions


async def map_records(
    http: ProviderHttpClient,
    *,
    compiled_query: str,
    page_size: int,
    order: str,
    cursor: str = "*",
    searched_scope: str,
    source_collections: list[str],
    start_position: int = 0,
    include_abstract: bool = False,
    exact_record: bool = False,
    expand_synonyms: bool = False,
    metadata: list[str] | None = None,
) -> dict:
    """Map one bounded Europe PMC core-result cursor page."""

    retrieved_at = utc_now()
    clean_query = compiled_query.strip() if isinstance(compiled_query, str) else ""
    clean_cursor = cursor if isinstance(cursor, str) else ""
    collections = list(source_collections) if isinstance(source_collections, list) else []
    provider_query = (
        _provider_query(clean_query, collections)
        if clean_query and order in {"relevance", "newest"}
        else clean_query
    )
    output = _map_base(
        provider_query=provider_query,
        page_size=page_size,
        order=order,
        cursor=clean_cursor,
        searched_scope=searched_scope,
        source_collections=collections,
        retrieved_at=retrieved_at,
    )

    if not clean_query:
        return _validation_failure(
            output, "invalid_request", "compiled_query", "query must not be empty"
        )
    if _CONTROL_RE.search(clean_query):
        return _validation_failure(
            output,
            "invalid_request",
            "compiled_query",
            "compiled query contains a control character",
        )
    if len(clean_query.encode("utf-8")) > 8_000:
        return _validation_failure(
            output,
            "invalid_request",
            "compiled_query",
            "compiled query exceeds 8,000 UTF-8 bytes",
        )
    if (
        not isinstance(page_size, int)
        or isinstance(page_size, bool)
        or not 1 <= page_size <= 25
    ):
        return _validation_failure(
            output, "invalid_request", "page_size", "page_size must be 1..25"
        )
    if order not in {"relevance", "newest"}:
        return _validation_failure(
            output,
            "invalid_request",
            "order",
            "Europe PMC order must be 'relevance' or 'newest'",
        )
    if not _valid_cursor(clean_cursor):
        return _validation_failure(
            output, "invalid_request", "cursor", "cursor is invalid"
        )
    if (
        not isinstance(searched_scope, str)
        or not searched_scope.strip()
        or len(searched_scope) > 200
    ):
        return _validation_failure(
            output,
            "invalid_request",
            "searched_scope",
            "searched_scope must be a bounded nonempty label",
        )
    if (
        any(not isinstance(value, str) or value not in _COLLECTIONS for value in collections)
        or len(collections) != len(set(collections))
        or not collections
    ):
        return _validation_failure(
            output,
            "invalid_request",
            "source_collections",
            "source_collections must contain MED and/or PMC without duplicates",
        )
    if (
        not isinstance(start_position, int)
        or isinstance(start_position, bool)
        or start_position < 0
    ):
        return _validation_failure(
            output,
            "invalid_request",
            "start_position",
            "start_position must be a nonnegative integer",
        )
    if not isinstance(include_abstract, bool):
        return _validation_failure(
            output,
            "invalid_request",
            "include_abstract",
            "include_abstract must be true or false",
        )
    if not isinstance(exact_record, bool):
        return _validation_failure(
            output,
            "invalid_request",
            "exact_record",
            "exact_record must be true or false",
        )
    if len(provider_query.encode("utf-8")) > 8_000:
        return _validation_failure(
            output,
            "invalid_request",
            "compiled_query",
            "effective Europe PMC query exceeds 8,000 UTF-8 bytes",
        )

    params = {
        "query": provider_query,
        "format": "json",
        "resultType": "core",
        "pageSize": page_size,
        "cursorMark": clean_cursor,
        "sort": _provider_sort(order),
        "synonym": str(expand_synonyms).lower(),
    }
    try:
        result = await http.get(
            _PROVIDER,
            "search",
            params=params,
            max_bytes=2 * 1024 * 1024,
        )
    except AttemptBudgetExceeded:
        raise
    except Exception:
        output["stages"]["transport"] = "failed"
        output["stages"]["result"] = "failed"
        output["failures"].append(
            failure(
                "connection_failure",
                "transport",
                "europe_pmc.search",
                "provider transport raised an exception",
            )
        )
        return output
    output["call_count"] = 1
    if _transport_failure(output, result, scope="europe_pmc.search"):
        return output

    try:
        payload = _safe_json(result)
    except _JsonParserLimitError:
        return _provider_failure(
            output,
            "parser_limit_exceeded",
            "Europe PMC search JSON exceeded the parser depth limit",
        )
    if payload is not None and not json_within_limits(payload):
        return _provider_failure(
            output,
            "parser_limit_exceeded",
            "Europe PMC search JSON exceeded structural parser limits",
        )
    if not isinstance(payload, dict):
        return _provider_failure(
            output,
            "provider_schema_drift",
            "Europe PMC search did not return a JSON object",
        )
    if detail := _provider_error_detail(payload):
        return _provider_failure(output, "provider_error_body", detail)
    provider_version = payload.get("version")
    if not isinstance(provider_version, str) or not provider_version.strip():
        return _provider_failure(
            output,
            "provider_schema_drift",
            "Europe PMC search omitted its provider version",
        )

    hit_count = _parse_nonnegative_int(payload.get("hitCount"))
    result_list = payload.get("resultList")
    rows = result_list.get("result") if isinstance(result_list, dict) else None
    if hit_count is None or not isinstance(rows, list):
        return _provider_failure(
            output,
            "provider_schema_drift",
            "Europe PMC search count or result list has an invalid shape",
        )
    if hit_count < len(rows) or len(rows) > page_size:
        return _provider_failure(
            output,
            "provider_schema_drift",
            "Europe PMC search count and returned page are inconsistent",
        )

    request_echo = payload.get("request")
    if not isinstance(request_echo, dict):
        return _provider_failure(
            output,
            "provider_schema_drift",
            "Europe PMC search omitted its request echo",
        )
    echoed_query = request_echo.get("queryString")
    if (
        not isinstance(echoed_query, str)
        or " ".join(echoed_query.split()) != " ".join(provider_query.split())
    ):
        return _provider_failure(
            output,
            "query_fidelity_unknown",
            "Europe PMC did not echo the exact requested query",
            stage="identity_or_query_fidelity",
        )
    echoed_cursor = request_echo.get("cursorMark")
    if not isinstance(echoed_cursor, str) or unquote(echoed_cursor) != clean_cursor:
        return _provider_failure(
            output,
            "query_fidelity_unknown",
            "Europe PMC did not echo the requested cursor",
            stage="identity_or_query_fidelity",
        )
    echoed_size = request_echo.get("pageSize")
    if echoed_size is None or _parse_nonnegative_int(echoed_size) != page_size:
        return _provider_failure(
            output,
            "query_fidelity_unknown",
            "Europe PMC did not echo the requested page size",
            stage="identity_or_query_fidelity",
        )
    if request_echo.get("sort") != _provider_sort(order):
        return _provider_failure(
            output,
            "query_fidelity_unknown",
            "Europe PMC did not echo the requested sort",
            stage="identity_or_query_fidelity",
        )
    if request_echo.get("synonym") is not expand_synonyms:
        return _provider_failure(
            output,
            "query_fidelity_unknown",
            "Europe PMC did not confirm the requested synonym expansion setting",
            stage="identity_or_query_fidelity",
        )
    result_type_echo = request_echo.get("resultType", _MISSING)
    if result_type_echo is not _MISSING and (
        not isinstance(result_type_echo, str)
        or result_type_echo.casefold() != "core"
    ):
        return _provider_failure(
            output,
            "query_fidelity_unknown",
            "Europe PMC echoed a projection other than the requested core projection",
            stage="identity_or_query_fidelity",
        )
    output["query_observation"] = {
        "provider_query_echo": bounded_text(echoed_query, 8_000),
        "sort": _provider_sort(order),
        "synonym": expand_synonyms,
        "projection_requested": "core",
        "projection_echo": (
            None if result_type_echo is _MISSING else result_type_echo
        ),
        "projection_echo_state": (
            "omitted" if result_type_echo is _MISSING else "represented"
        ),
    }

    next_cursor = payload.get("nextCursorMark")
    if next_cursor is not None and not _valid_cursor(next_cursor):
        return _provider_failure(
            output,
            "provider_schema_drift",
            "Europe PMC returned an invalid next cursor",
        )

    output["stages"]["provider_body"] = "succeeded"
    output["stages"]["identity_or_query_fidelity"] = "succeeded"
    output["query_fidelity"] = "exact"
    output["count_observation"].update(
        {"hit_count": hit_count, "returned_result_count": len(rows)}
    )
    output["cursor_observation"]["next_cursor"] = next_cursor
    represented_end = start_position + len(rows)
    if rows and represented_end > hit_count:
        return _provider_failure(
            output,
            "snapshot_drift",
            "Europe PMC hit count is smaller than the represented page boundary",
            stage="currentness",
        )
    if clean_cursor == "*" and hit_count > 0 and not rows:
        return _provider_failure(
            output,
            "provider_schema_drift",
            "Europe PMC returned an empty first page for a nonzero hit count",
        )

    cursor_advanced = bool(next_cursor and next_cursor != clean_cursor and rows)
    has_more = represented_end < hit_count
    pagination_lost = has_more and not cursor_advanced
    if has_more and cursor_advanced:
        output["cursor_observation"]["state"] = "advanced"
        output["continuation_observation"].update(
            {"state": "available", "next_cursor": next_cursor}
        )
    else:
        output["cursor_observation"]["state"] = "terminal"
        output["continuation_observation"]["state"] = "exhausted"
    if pagination_lost:
        output["cursor_observation"]["state"] = "unavailable"
        output["continuation_observation"]["state"] = "unavailable"
        output["failures"].append(
            failure(
                "pagination_boundary",
                "provider_body",
                "europe_pmc.nextCursorMark",
                "Europe PMC reported remaining hits without a usable next cursor",
            )
        )
        output["warnings"].append(
            warning(
                "partial_coverage",
                "europe_pmc.nextCursorMark",
                "The represented page cannot be continued",
            )
        )

    cards: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    malformed_rows = 0
    malformed_aliases = 0
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            malformed_rows += 1
            continue
        try:
            card, alias_omissions = _map_candidate(
                row,
                searched_scope=searched_scope.strip(),
                order=_provider_sort(order) or "relevance",
                page_position=index,
                start_position=start_position,
                retrieved_at=retrieved_at,
                include_abstract=include_abstract,
                exact_record=exact_record,
                metadata=metadata,
            )
        except ValueError:
            malformed_rows += 1
            continue
        malformed_aliases += alias_omissions
        if card is None:
            malformed_rows += 1
            continue
        key = (card["source_collection"], card["source_id"])
        if key in seen:
            malformed_rows += 1
            continue
        seen.add(key)
        if collections and key[0] not in collections:
            return _provider_failure(
                output,
                "query_fidelity_unknown",
                "Europe PMC returned a source outside the requested collections",
                stage="identity_or_query_fidelity",
            )
        cards.append(card)

    if malformed_rows:
        output["failures"].append(
            failure(
                "provider_schema_drift",
                "projection_or_selection",
                "europe_pmc.results",
                (
                    f"{malformed_rows} result rows contained malformed fields "
                    "or lacked an exact source identity"
                ),
            )
        )
    if malformed_aliases:
        output["warnings"].append(
            warning(
                "partial_coverage",
                "europe_pmc.identifiers",
                f"{malformed_aliases} malformed represented aliases were omitted",
            )
        )

    output["candidates"] = cards
    output["count_observation"]["represented_candidate_count"] = len(cards)
    output["coverage"] = coverage(
        "the exact Europe PMC query, source collections, sort, and cursor page",
        len(cards),
        exhaustive=False,
        provider_count=hit_count,
        boundary="one bounded cursor page",
        limitations=["cursor pages are not a stable historical snapshot"],
    )
    output["stages"]["projection_or_selection"] = (
        "inconsistent" if malformed_rows else "succeeded"
    )
    if malformed_rows and rows and not cards:
        output["stages"]["result"] = "inconsistent"
    elif clean_cursor == "*" and hit_count == 0:
        output["stages"]["result"] = "valid_zero"
    elif pagination_lost and not cards:
        output["stages"]["result"] = "failed"
    else:
        output["stages"]["result"] = "succeeded"
    output["stages"]["currentness"] = "succeeded"
    output["currentness"] = _retrieval_currentness(
        retrieved_at,
        provider_version,
    )
    output["warnings"].append(
        warning(
            "provider_rank_local_only",
            "europe_pmc.candidates",
            "Europe PMC order is local to this exact provider request",
        )
    )
    if not include_abstract:
        output["warnings"].append(
            warning(
                "candidate_text_is_triage_only",
                "europe_pmc.candidates",
                "Abstract excerpts are bounded triage text, not qualified content",
            )
        )
    return output
