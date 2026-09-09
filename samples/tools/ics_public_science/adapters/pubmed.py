"""Private PubMed adapters for bounded discovery and exact qualification."""

from __future__ import annotations

import os
import re
import unicodedata
from typing import Any
from xml.etree import ElementTree as ET

from contracts import normalize_doi
from disclosure import AttemptBudgetExceeded
from envelopes import coverage, currentness, failure, stages, utc_now, warning
from http_client import ProviderHttpClient, status_failure
from normalization import (
    candidate,
    projection_manifest,
    qualified_record,
    relationship_edge,
    source_fact,
    source_family,
    source_item,
)
from validation import (
    QuerySyntaxError,
    bounded_text,
    json_within_limits,
    parse_normal_query,
    unique_identities,
)


__all__ = ["map_publications", "qualify_publication"]

_PROVIDER = "pubmed"
_PMID_RE = re.compile(r"[0-9]{1,12}")
_PMCID_RE = re.compile(r"PMC[0-9]+", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_DOCTYPE_RE = re.compile(rb"<!DOCTYPE\b.*?>", re.IGNORECASE | re.DOTALL)
_ALLOWED_DOCTYPE_RE = re.compile(
    rb"""<!DOCTYPE\s+(?P<root>PubmedArticleSet|PubmedBookArticleSet)\s+
        PUBLIC\s+["'][^"'<>]{1,300}["']\s+
        ["']https://dtd\.nlm\.nih\.gov/ncbi/pubmed/out/
        pubmed_[0-9]{6}\.dtd["']\s*>""",
    re.IGNORECASE | re.VERBOSE,
)
_MAX_VISIBLE_POSITION = 9_998
_MAX_XML_ELEMENTS = 50_000
_MAX_XML_DEPTH = 128
_MAX_ABSTRACT_TEXT = 20_000
_MAX_PROVIDER_INTEGER = (1 << 63) - 1
_SUPPORTED_INCLUDES = {
    "abstracts",
    "indexing",
    "integrity",
    "relationships",
    "funding",
}


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


def _retrieval_currentness(retrieved_at: str) -> dict[str, Any]:
    return currentness(
        [
            {
                "role": "retrieved_at",
                "value": retrieved_at,
                "source": _PROVIDER,
                "state": "represented",
            }
        ],
        drift="unknown",
    )


def _ncbi_params(fields: dict[str, Any]) -> dict[str, Any]:
    """Add configured NCBI identification without reflecting secrets."""

    tool = os.environ.get("ICS_NCBI_TOOL", "").strip()
    if not tool:
        tool = "ics-program-companion"
    params: dict[str, Any] = {"tool": tool, **fields}

    email = os.environ.get("ICS_NCBI_EMAIL", "").strip()
    if not email:
        email = os.environ.get("ICS_EXTERNAL_CONTACT", "").strip()
    if email:
        params["email"] = email

    api_key = os.environ.get("ICS_NCBI_API_KEY", "").strip()
    if api_key:
        params["api_key"] = api_key
    return params


def _map_base(
    *,
    compiled_query: str,
    page_size: int,
    order: str,
    offset: int,
    effective_page_size: int,
    retrieved_at: str,
) -> dict[str, Any]:
    return {
        "provider": _PROVIDER,
        "effective_request": _effective_request(
            "esearch.fcgi",
            [
                ("db", "pubmed"),
                ("term", compiled_query),
                ("retmode", "json"),
                ("retstart", offset),
                ("retmax", effective_page_size),
                ("sort", order),
            ],
        ),
        "stages": stages(
            validation="succeeded",
            disclosure="not_requested",
            request_build="succeeded",
        ),
        "query_fidelity": "unknown",
        "provider_order": order,
        "candidates": [],
        "failures": [],
        "warnings": [],
        "currentness": _retrieval_currentness(retrieved_at),
        "count_observation": {
            "provider_count": None,
            "returned_id_count": 0,
            "represented_candidate_count": 0,
            "offset": offset,
            "requested_page_size": page_size,
            "effective_page_size": effective_page_size,
            "position_basis": "zero_based_retstart",
            "maximum_visible_position": _MAX_VISIBLE_POSITION,
        },
        "continuation_observation": {
            "state": "unknown",
            "next_offset": None,
            "maximum_visible_position": _MAX_VISIBLE_POSITION,
        },
        "coverage": coverage(
            "the exact PubMed query, sort, offset, and visible source window",
            0,
            boundary=f"zero-based positions 0..{_MAX_VISIBLE_POSITION}",
        ),
        "call_count": 0,
    }


def _qualification_base(
    *,
    pmid: str,
    projection: str,
    include: list[str],
    retrieved_at: str,
) -> dict[str, Any]:
    return {
        "provider": _PROVIDER,
        "effective_request": _effective_request(
            "efetch.fcgi",
            [
                ("db", "pubmed"),
                ("id", pmid),
                ("retmode", "xml"),
                ("rettype", "abstract"),
            ],
        ),
        "stages": stages(
            validation="succeeded",
            disclosure="not_requested",
            request_build="succeeded",
        ),
        "record": None,
        "failures": [],
        "warnings": [],
        "currentness": _retrieval_currentness(retrieved_at),
        "projection_observation": {
            "profile": projection,
            "requested_families": list(include),
        },
        "call_count": 0,
    }


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
    scope: str = "pubmed.response",
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


def _has_signal(value: Any) -> bool:
    stack = [value]
    while stack:
        current = stack.pop()
        if current is None or current is False:
            continue
        if isinstance(current, str):
            if current.strip():
                return True
            continue
        if isinstance(current, dict):
            stack.extend(current.values())
            continue
        if isinstance(current, (list, tuple, set)):
            stack.extend(current)
            continue
        return True
    return False


def _signal_keys(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    return sorted(
        bounded_text(key, 100)
        for key, child in value.items()
        if _has_signal(child)
    )


def _normalized_field_tag(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value.strip().casefold())
    return {
        "title/abstract": "title_abstract",
        "tiab": "title_abstract",
        "ti": "title",
        "title": "title",
        "all fields": "all_fields",
        "all": "all_fields",
    }.get(normalized, "")


def _strip_and_validate_field_tags(
    value: str,
) -> tuple[str, str, int] | None:
    rendered: list[str] = []
    fields: list[str] = []
    position = 0
    while position < len(value):
        character = value[position]
        if character.isspace():
            rendered.append(" ")
            position += 1
            continue
        if character in "()":
            rendered.append(character)
            position += 1
            continue

        if character == '"':
            end = value.find('"', position + 1)
            if end < 0:
                return None
            token = value[position : end + 1]
            position = end + 1
        else:
            end = position
            while (
                end < len(value)
                and not value[end].isspace()
                and value[end] not in "()[]"
            ):
                end += 1
            if end == position:
                return None
            token = value[position:end]
            position = end
            if token in {"AND", "OR", "NOT"}:
                rendered.append(token)
                continue

        while position < len(value) and value[position].isspace():
            position += 1
        if position >= len(value) or value[position] != "[":
            return None
        field_end = value.find("]", position + 1)
        if field_end < 0 or "[" in value[position + 1 : field_end]:
            return None
        field = _normalized_field_tag(
            value[position + 1 : field_end]
        )
        if not field:
            return None
        fields.append(field)
        rendered.append(token)
        rendered.append(" ")
        position = field_end + 1

    if not fields or len(set(fields)) != 1:
        return None
    return "".join(rendered), fields[0], len(fields)


def _normalized_leaf(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = "".join(
        " "
        if character.isspace()
        or unicodedata.category(character) == "Pd"
        else character
        for character in normalized
    )
    return re.sub(r"\s+", " ", normalized).strip()


def _normalized_boolean_ast(node: tuple[Any, ...]) -> tuple[Any, ...]:
    kind = node[0]
    if kind in {"TERM", "PHRASE"}:
        leaf = _normalized_leaf(node[1])
        if not leaf:
            raise ValueError("query leaf normalized to an empty value")
        return ("LEAF", leaf)
    if kind == "NOT":
        return ("NOT", _normalized_boolean_ast(node[1]))
    if kind in {"AND", "OR"}:
        return (
            kind,
            _normalized_boolean_ast(node[1]),
            _normalized_boolean_ast(node[2]),
        )
    raise ValueError("query contained an unknown Boolean node")


def _boolean_leaf_count(node: tuple[Any, ...]) -> int:
    kind = node[0]
    if kind in {"TERM", "PHRASE"}:
        return 1
    if kind == "NOT":
        return _boolean_leaf_count(node[1])
    if kind in {"AND", "OR"}:
        return _boolean_leaf_count(node[1]) + _boolean_leaf_count(node[2])
    raise ValueError("query contained an unknown Boolean node")


def _fielded_query_signature(
    value: str,
) -> tuple[str, tuple[Any, ...]] | None:
    represented = _strip_and_validate_field_tags(value)
    if represented is None:
        return None
    query, field, field_count = represented
    try:
        ast = parse_normal_query(query)
        if _boolean_leaf_count(ast) != field_count:
            return None
        return field, _normalized_boolean_ast(ast)
    except (QuerySyntaxError, TypeError, ValueError):
        return None


def _translation_preserves_intent(
    submitted: str,
    translated: str,
) -> bool:
    submitted_signature = _fielded_query_signature(submitted)
    return (
        submitted_signature is not None
        and _fielded_query_signature(translated) == submitted_signature
    )


class _JsonParserLimitError(ValueError):
    """A provider JSON body exceeded the runtime parser's safe depth."""


def _safe_json(result: Any) -> Any | None:
    try:
        return result.json()
    except RecursionError as exc:
        raise _JsonParserLimitError from exc
    except (UnicodeDecodeError, ValueError, TypeError):
        return None


def _verified_no_items(
    *,
    count: int,
    ids: list[str],
    offset: int,
    warning_list: Any,
    error_list: Any,
) -> bool:
    if count != 0 or ids or offset != 0 or not isinstance(warning_list, dict):
        return False
    if error_list is not None and not isinstance(error_list, dict):
        return False
    messages = warning_list.get("outputmessages")
    if (
        not isinstance(messages, list)
        or len(messages) != 1
        or not isinstance(messages[0], str)
        or messages[0].strip().casefold() != "no items found."
    ):
        return False
    if set(_signal_keys(warning_list)) != {"outputmessages"}:
        return False
    return set(_signal_keys(error_list)) <= {"phrasesnotfound"}


def _is_xml_content_type(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    media_type = value.split(";", 1)[0].strip().casefold()
    return media_type in {"application/xml", "text/xml"} or media_type.endswith(
        "+xml"
    )


def _pmid_identity(value: str) -> dict[str, str]:
    return {"kind": "pmid", "value": value}


def _summary_aliases(item: dict[str, Any], pmid: str) -> list[dict[str, Any]]:
    identities: list[dict[str, Any]] = []
    article_ids = item.get("articleids")
    if not isinstance(article_ids, list):
        return identities
    for raw in article_ids:
        if not isinstance(raw, dict):
            continue
        id_type = str(raw.get("idtype") or "").strip().casefold()
        value = raw.get("value")
        if not isinstance(value, str):
            continue
        value = value.strip()
        try:
            if id_type in {"pubmed", "pmid"} and value == pmid:
                identities.append(_pmid_identity(value))
            elif id_type == "doi":
                identities.append({"kind": "doi", "value": normalize_doi(value)})
            elif (
                id_type in {"pmc", "pmcid"}
                and 4 <= len(value) <= 20
                and _PMCID_RE.fullmatch(value)
            ):
                identities.append({"kind": "pmcid", "value": value.upper()})
        except ValueError:
            continue
    return unique_identities(identities) if identities else []


def _summary_candidate(
    item: dict[str, Any],
    pmid: str,
    *,
    order: str,
    position: int,
    retrieved_at: str,
) -> dict[str, Any]:
    authors_raw = item.get("authors")
    authors: list[str] = []
    if isinstance(authors_raw, list):
        for author in authors_raw:
            if isinstance(author, dict) and isinstance(author.get("name"), str):
                name = bounded_text(author["name"], 300)
                if name:
                    authors.append(name)

    pubtype_raw = item.get("pubtype")
    if isinstance(pubtype_raw, list):
        publication_types = [
            bounded_text(value, 200)
            for value in pubtype_raw[:100]
            if isinstance(value, str) and bounded_text(value, 200)
        ]
    elif isinstance(pubtype_raw, str) and bounded_text(pubtype_raw, 200):
        publication_types = [bounded_text(pubtype_raw, 200)]
    else:
        publication_types = []

    lowered_types = [value.casefold() for value in publication_types]
    integrity_signals: list[dict[str, Any]] = []
    for kind, needle in (
        ("retracted_publication", "retracted publication"),
        ("retraction_notice", "retraction of publication"),
        ("correction_notice", "published erratum"),
        ("expression_of_concern", "expression of concern"),
    ):
        if any(needle in value for value in lowered_types):
            integrity_signals.append(
                {
                    "kind": kind,
                    "state": "represented",
                    "source": _PROVIDER,
                    "detail": "represented in PubMed publication types",
                }
            )

    aliases = _summary_aliases(item, pmid)
    access_signals: list[dict[str, Any]] = []
    if any(alias.get("kind") == "pmcid" for alias in aliases):
        access_signals.append(
            {
                "kind": "pmc_record",
                "state": "represented",
                "value": True,
                "source": _PROVIDER,
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
    sort_date = item.get("sortpubdate")
    if isinstance(sort_date, str) and sort_date.strip():
        observations.insert(
            0,
            {
                "role": "summary_sort_publication_date",
                "value": bounded_text(sort_date, 80),
                "source": _PROVIDER,
                "state": "represented",
            },
        )

    card = candidate(
        identity=_pmid_identity(pmid),
        represented_identifiers=aliases,
        provider=_PROVIDER,
        record_class="publication",
        record_role=_publication_role(publication_types, "publication"),
        title=item.get("title") if isinstance(item.get("title"), str) else None,
        contributors=authors,
        contributor_count=len(authors),
        container=(
            item.get("fulljournalname")
            if isinstance(item.get("fulljournalname"), str)
            else item.get("source")
            if isinstance(item.get("source"), str)
            else None
        ),
        date=item.get("pubdate") if isinstance(item.get("pubdate"), str) else None,
        date_role="publication",
        searched_scope="compiled PubMed query",
        matched_scope=None,
        provider_order=order,
        absolute_position=position,
        integrity_signals=integrity_signals,
        access_signals=access_signals,
        currentness_state=currentness(observations, drift="unknown"),
    )
    attributes = item.get("attributes")
    has_abstract: bool | None = None
    if isinstance(attributes, list):
        has_abstract = any(
            isinstance(value, str) and value.casefold() == "has abstract"
            for value in attributes
        )
    card.update(
        {
            "source_collection": "pubmed",
            "source_id": pmid,
            "publication_roles": publication_types,
            "abstract_available": has_abstract,
            "provider_position": position,
        }
    )
    return card


async def map_publications(
    http: ProviderHttpClient,
    *,
    compiled_query: str,
    page_size: int,
    order: str,
    offset: int = 0,
    query_mode: str = "controlled",
) -> dict:
    """Map one bounded PubMed page through ESearch and ESummary."""

    retrieved_at = utc_now()
    clean_query = compiled_query.strip() if isinstance(compiled_query, str) else ""
    valid_page_size = (
        isinstance(page_size, int)
        and not isinstance(page_size, bool)
        and 1 <= page_size <= 25
    )
    valid_offset = (
        isinstance(offset, int) and not isinstance(offset, bool) and offset >= 0
    )
    effective_page_size = (
        min(page_size, _MAX_VISIBLE_POSITION - offset + 1)
        if valid_page_size and valid_offset and offset <= _MAX_VISIBLE_POSITION
        else page_size if isinstance(page_size, int) else 0
    )
    output = _map_base(
        compiled_query=clean_query,
        page_size=page_size,
        order=order,
        offset=offset,
        effective_page_size=effective_page_size,
        retrieved_at=retrieved_at,
    )

    if query_mode not in {"controlled", "provider"}:
        return _validation_failure(output, "invalid_request", "query_mode", "unsupported query mode")
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
    if not valid_page_size:
        return _validation_failure(
            output, "invalid_request", "page_size", "page_size must be 1..25"
        )
    if order not in {"relevance", "pub_date"}:
        return _validation_failure(
            output,
            "invalid_request",
            "order",
            "PubMed order must be 'relevance' or 'pub_date'",
        )
    if not valid_offset:
        return _validation_failure(
            output, "invalid_request", "offset", "offset must be nonnegative"
        )
    if offset > _MAX_VISIBLE_POSITION:
        output["warnings"].append(
            warning(
                "source_visibility_limit",
                "offset",
                "PubMed exposes no ordinary search position beyond 9,998",
            )
        )
        return _validation_failure(
            output,
            "source_visibility_limit",
            "offset",
            "PubMed exposes no ordinary search position beyond 9,998",
        )

    search_fields = {
        "db": "pubmed",
        "term": clean_query,
        "retmode": "json",
        "retstart": offset,
        "retmax": effective_page_size,
        "sort": order,
    }
    try:
        search_result = await http.get(
            _PROVIDER,
            "esearch.fcgi",
            params=_ncbi_params(search_fields),
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
                "pubmed.esearch",
                "provider transport raised an exception",
            )
        )
        return output
    output["call_count"] = 1
    if _transport_failure(output, search_result, scope="pubmed.esearch"):
        return output

    try:
        payload = _safe_json(search_result)
    except _JsonParserLimitError:
        return _provider_failure(
            output,
            "parser_limit_exceeded",
            "PubMed ESearch JSON exceeded the parser depth limit",
        )
    if payload is not None and not json_within_limits(payload):
        return _provider_failure(
            output,
            "parser_limit_exceeded",
            "PubMed ESearch JSON exceeded structural parser limits",
        )
    if not isinstance(payload, dict):
        return _provider_failure(
            output,
            "provider_schema_drift",
            "PubMed ESearch did not return a JSON object",
        )
    result = payload.get("esearchresult")
    if not isinstance(result, dict):
        return _provider_failure(
            output,
            "provider_schema_drift",
            "PubMed ESearch omitted esearchresult",
        )

    count = _parse_nonnegative_int(result.get("count"))
    id_list = result.get("idlist")
    if count is None or not isinstance(id_list, list):
        return _provider_failure(
            output,
            "provider_schema_drift",
            "PubMed ESearch count or idlist has an invalid shape",
        )
    ids: list[str] = []
    for value in id_list:
        if not isinstance(value, str) or not _PMID_RE.fullmatch(value):
            return _provider_failure(
                output,
                "provider_schema_drift",
                "PubMed ESearch returned a non-numeric PMID",
            )
        ids.append(value)
    if len(ids) != len(set(ids)):
        return _provider_failure(
            output,
            "provider_schema_drift",
            "PubMed ESearch returned duplicate PMIDs in one page",
        )
    if len(ids) > effective_page_size or count < len(ids):
        return _provider_failure(
            output,
            "provider_schema_drift",
            "PubMed ESearch count and returned page are inconsistent",
        )
    if ids and count < offset + len(ids):
        return _provider_failure(
            output,
            "snapshot_drift",
            "PubMed ESearch count is smaller than the represented page boundary",
            stage="currentness",
        )
    echoed_offset = result.get("retstart")
    parsed_offset = _parse_nonnegative_int(echoed_offset)
    if parsed_offset is None or parsed_offset != offset:
        return _provider_failure(
            output,
            "query_fidelity_unknown",
            "PubMed ESearch did not echo the requested offset",
            stage="identity_or_query_fidelity",
        )
    echoed_maximum = result.get("retmax")
    parsed_maximum = _parse_nonnegative_int(echoed_maximum)
    if parsed_maximum is None or parsed_maximum != len(ids):
        return _provider_failure(
            output,
            "query_fidelity_unknown",
            "PubMed ESearch did not report the number of records returned",
            stage="identity_or_query_fidelity",
        )

    output["stages"]["provider_body"] = "succeeded"
    output["count_observation"].update(
        {
            "provider_count": count,
            "returned_id_count": len(ids),
        }
    )

    next_offset = offset + len(ids)
    visible_total = min(count, _MAX_VISIBLE_POSITION + 1)
    if ids and next_offset < visible_total:
        output["continuation_observation"].update(
            {"state": "available", "next_offset": next_offset}
        )
    elif count > _MAX_VISIBLE_POSITION + 1 and next_offset >= visible_total:
        output["continuation_observation"]["state"] = "source_visibility_limit"
    else:
        output["continuation_observation"]["state"] = "exhausted"

    if count > _MAX_VISIBLE_POSITION + 1:
        output["warnings"].append(
            warning(
                "source_visibility_limit",
                "pubmed.count",
                "PubMed reports matches beyond its ordinary 9,999-result window",
            )
        )

    warning_list = result.get("warninglist")
    error_list = result.get("errorlist")
    translated = result.get("querytranslation")
    output["query_observation"] = {
        "translated_query": (
            bounded_text(translated, 8_000)
            if isinstance(translated, str)
            else None
        ),
        "warning_categories": _signal_keys(warning_list),
        "error_categories": _signal_keys(error_list),
    }
    verified_no_items = _verified_no_items(
        count=count,
        ids=ids,
        offset=offset,
        warning_list=warning_list,
        error_list=error_list,
    )
    has_notices = _has_signal(warning_list) or _has_signal(error_list)
    warned = has_notices and not verified_no_items
    translation_missing = not isinstance(translated, str) or not translated.strip()
    translation_changed = (
        isinstance(translated, str)
        and translated.strip() != clean_query.strip()
    )
    scope_preserved = (
        translation_changed
        and isinstance(translated, str)
        and _translation_preserves_intent(clean_query, translated)
    )
    output["query_observation"]["translation_scope_preserved"] = scope_preserved
    if (
        warned
        or (translation_changed and has_notices)
        or (translation_changed and not scope_preserved and query_mode == "controlled")
    ):
        output["query_fidelity"] = "repaired"
        output["stages"]["identity_or_query_fidelity"] = "failed"
        output["stages"]["result"] = "failed"
        output["failures"].append(
            failure(
                "query_repaired",
                "identity_or_query_fidelity",
                "compiled_query",
                "PubMed warned about or changed the submitted query",
            )
        )
        output["warnings"].append(
            warning(
                "query_repaired",
                "compiled_query",
                "No candidates are represented because PubMed changed query fidelity",
            )
        )
        return output
    if translation_missing:
        output["query_fidelity"] = "unknown"
        output["stages"]["identity_or_query_fidelity"] = "unknown"
        output["stages"]["result"] = "failed"
        output["failures"].append(
            failure(
                "query_fidelity_unknown",
                "identity_or_query_fidelity",
                "compiled_query",
                "PubMed omitted querytranslation",
            )
        )
        return output
    output["query_fidelity"] = (
        "provider_interpreted" if query_mode == "provider" else
        "scope_preserved" if translation_changed else "exact"
    )
    output["query_observation"]["mode"] = query_mode
    if query_mode == "provider":
        output["query_observation"]["translation_scope_preserved"] = None
    output["stages"]["identity_or_query_fidelity"] = "succeeded"
    if translation_changed:
        output["warnings"].append(
            warning(
                "provider_query_normalized",
                "compiled_query",
                (
                    "PubMed interpreted the explicitly native query; inspect its translation"
                    if query_mode == "provider" else
                    "PubMed normalized query text while retaining every requested field boundary"
                ),
            )
        )

    limitations = ["PubMed source order is local to this query and retrieval"]
    if count > _MAX_VISIBLE_POSITION + 1:
        limitations.append("ordinary retrieval stops after position 9,998")
    output["coverage"] = coverage(
        "the exact PubMed query, sort, offset, and visible source window",
        0,
        exhaustive=False,
        provider_count=count,
        boundary=f"zero-based positions 0..{_MAX_VISIBLE_POSITION}",
        limitations=limitations,
    )

    if not ids:
        if offset > 0:
            return _provider_failure(
                output,
                "snapshot_drift",
                "PubMed returned no records for a continuation offset; "
                "the visible population may have changed",
                stage="currentness",
                scope="pubmed.count",
            )
        if offset < count:
            return _provider_failure(
                output,
                "provider_schema_drift",
                "PubMed returned an empty page before the observed page boundary",
            )
        output["stages"]["projection_or_selection"] = "succeeded"
        output["stages"]["result"] = "valid_zero" if count == 0 else "succeeded"
        output["stages"]["currentness"] = "succeeded"
        output["warnings"].append(
            warning(
                "provider_rank_local_only",
                "pubmed",
                "PubMed positions are local to this exact provider request",
            )
        )
        return output

    summary_fields = {
        "db": "pubmed",
        "id": ",".join(ids),
        "retmode": "json",
    }
    output["summary_request"] = _effective_request(
        "esummary.fcgi",
        [("db", "pubmed"), ("id", ids), ("retmode", "json")],
    )
    try:
        summary_result = await http.get(
            _PROVIDER,
            "esummary.fcgi",
            params=_ncbi_params(summary_fields),
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
                "pubmed.esummary",
                "provider transport raised an exception",
            )
        )
        return output
    output["call_count"] = 2
    if _transport_failure(output, summary_result, scope="pubmed.esummary"):
        return output

    try:
        summary_payload = _safe_json(summary_result)
    except _JsonParserLimitError:
        return _provider_failure(
            output,
            "parser_limit_exceeded",
            "PubMed ESummary JSON exceeded the parser depth limit",
        )
    if summary_payload is not None and not json_within_limits(summary_payload):
        return _provider_failure(
            output,
            "parser_limit_exceeded",
            "PubMed ESummary JSON exceeded structural parser limits",
        )
    if not isinstance(summary_payload, dict):
        return _provider_failure(
            output,
            "provider_schema_drift",
            "PubMed ESummary did not return a JSON object",
        )
    summary = summary_payload.get("result")
    if not isinstance(summary, dict) or _has_signal(summary.get("error")):
        return _provider_failure(
            output,
            "provider_error_body",
            "PubMed ESummary returned an error or omitted its result object",
        )

    uids = summary.get("uids")
    if not isinstance(uids, list) or any(
        not isinstance(value, str) or not _PMID_RE.fullmatch(value)
        for value in uids
    ):
        return _provider_failure(
            output,
            "provider_schema_drift",
            "PubMed ESummary omitted its exact UID list",
        )
    expected_uid_order = [value for value in ids if value in set(uids)]
    if (
        len(uids) != len(set(uids))
        or not set(uids).issubset(set(ids))
        or uids != expected_uid_order
    ):
        return _provider_failure(
            output,
            "identity_mismatch",
            "PubMed ESummary introduced, duplicated, or reordered identities",
            stage="identity_or_query_fidelity",
        )
    represented_summary_ids = {
        key
        for key, value in summary.items()
        if isinstance(key, str)
        and _PMID_RE.fullmatch(key)
        and isinstance(value, dict)
    }
    if not represented_summary_ids.issubset(set(ids)):
        return _provider_failure(
            output,
            "identity_mismatch",
            "PubMed ESummary introduced an unexpected record identity",
            stage="identity_or_query_fidelity",
        )

    cards: list[dict[str, Any]] = []
    partial_summaries = 0
    for index, pmid in enumerate(ids):
        item = summary.get(pmid)
        if not isinstance(item, dict):
            item = {}
            partial_summaries += 1
            output["failures"].append(
                failure(
                    "projection_incomplete",
                    "projection_or_selection",
                    f"pubmed.esummary.{pmid}",
                    "PubMed ESummary omitted metadata for an ESearch PMID",
                )
            )
        uid = item.get("uid")
        if uid is not None and uid != pmid:
            return _provider_failure(
                output,
                "identity_mismatch",
                "PubMed ESummary returned a mismatched record identity",
                stage="identity_or_query_fidelity",
            )
        per_record_error = _has_signal(item.get("error"))
        if per_record_error:
            partial_summaries += 1
            output["failures"].append(
                failure(
                    "projection_incomplete",
                    "projection_or_selection",
                    f"pubmed.esummary.{pmid}",
                    "PubMed ESummary returned an error for this PMID",
                )
            )
        card = _summary_candidate(
            item,
            pmid,
            order=order,
            position=offset + index,
            retrieved_at=retrieved_at,
        )
        card["metadata_state"] = (
            "partial" if not item or per_record_error else "represented"
        )
        cards.append(card)

    output["candidates"] = cards
    output["count_observation"]["represented_candidate_count"] = len(cards)
    output["coverage"]["returned"] = len(cards)
    output["stages"]["provider_body"] = "succeeded"
    output["stages"]["identity_or_query_fidelity"] = "succeeded"
    output["stages"]["projection_or_selection"] = (
        "inconsistent" if partial_summaries else "succeeded"
    )
    output["stages"]["result"] = "succeeded"
    output["stages"]["currentness"] = "succeeded"
    output["warnings"].extend(
        [
            warning(
                "provider_rank_local_only",
                "pubmed.candidates",
                "PubMed positions are local to this exact provider request",
            ),
            warning(
                "candidate_text_is_triage_only",
                "pubmed.candidates",
                "Candidate metadata is bounded triage material, not article content",
            ),
        ]
    )
    return output


def _local_name(element: ET.Element) -> str:
    tag = element.tag
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _children(element: ET.Element | None, name: str) -> list[ET.Element]:
    if element is None:
        return []
    return [child for child in list(element) if _local_name(child) == name]


def _child(element: ET.Element | None, name: str) -> ET.Element | None:
    values = _children(element, name)
    return values[0] if values else None


def _path(element: ET.Element | None, *names: str) -> ET.Element | None:
    current = element
    for name in names:
        current = _child(current, name)
        if current is None:
            return None
    return current


def _descendants(element: ET.Element | None, name: str) -> list[ET.Element]:
    if element is None:
        return []
    return [value for value in element.iter() if _local_name(value) == name]


def _element_text(element: ET.Element | None, limit: int = 4_000) -> str:
    if element is None:
        return ""
    return bounded_text("".join(element.itertext()), limit)


def _xml_is_safe(body: bytes) -> bool:
    if b"\x00" in body:
        return False
    probe = body.upper()
    if b"<!ENTITY" in probe:
        return False
    marker_count = probe.count(b"<!DOCTYPE")
    if marker_count == 0:
        return True
    declarations = _DOCTYPE_RE.findall(body)
    return (
        marker_count == 1
        and len(declarations) == 1
        and _ALLOWED_DOCTYPE_RE.fullmatch(declarations[0]) is not None
    )


def _declared_doctype_root(body: bytes) -> str | None:
    declarations = _DOCTYPE_RE.findall(body)
    if len(declarations) != 1:
        return None
    match = _ALLOWED_DOCTYPE_RE.fullmatch(declarations[0])
    if match is None:
        return None
    return match.group("root").decode("ascii")


def _xml_within_limits(root: ET.Element) -> bool:
    count = 0
    stack: list[tuple[ET.Element, int]] = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        count += 1
        if count > _MAX_XML_ELEMENTS or depth > _MAX_XML_DEPTH:
            return False
        stack.extend((child, depth + 1) for child in list(element))
    return True


def _xml_uses_unqualified_tags(root: ET.Element) -> bool:
    return all(
        isinstance(element.tag, str)
        and element.tag == _local_name(element)
        for element in root.iter()
    )


def _branch_pmids(branch: ET.Element) -> list[str]:
    name = _local_name(branch)
    if name == "PubmedArticle":
        element = _path(branch, "MedlineCitation", "PMID")
        value = _element_text(element, 20)
        return [value] if _PMID_RE.fullmatch(value) else []
    if name == "PubmedBookArticle":
        element = _path(branch, "BookDocument", "PMID")
        value = _element_text(element, 20)
        return [value] if _PMID_RE.fullmatch(value) else []
    if name == "DeleteCitation":
        values = [_element_text(value, 20) for value in _children(branch, "PMID")]
        return [value for value in values if _PMID_RE.fullmatch(value)]
    return []


def _date_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    medline = _element_text(_child(element, "MedlineDate"), 80)
    if medline:
        return medline
    parts = []
    for name in ("Year", "Month", "Day", "Season"):
        value = _element_text(_child(element, name), 30)
        if value:
            parts.append(value)
    return "-".join(parts)


def _author_name(author: ET.Element) -> str:
    collective = _element_text(_child(author, "CollectiveName"), 300)
    if collective:
        return collective
    given = _element_text(_child(author, "ForeName"), 150)
    if not given:
        given = _element_text(_child(author, "Initials"), 40)
    family = _element_text(_child(author, "LastName"), 150)
    suffix = _element_text(_child(author, "Suffix"), 40)
    return bounded_text(" ".join(value for value in (given, family, suffix) if value), 300)


def _contributors(container: ET.Element | None) -> tuple[list[str], int]:
    author_list = _child(container, "AuthorList")
    if author_list is None:
        author_list = _path(container, "Book", "AuthorList")
    names = [
        name
        for author in _children(author_list, "Author")
        if (name := _author_name(author))
    ]
    return names[:100], len(names)


class _TextBudget:
    def __init__(self, limit: int) -> None:
        self.remaining = limit
        self.truncated = False

    def take(self, value: str) -> tuple[str, bool]:
        clean = bounded_text(value, max(len(value), 1))
        if len(clean) <= self.remaining:
            self.remaining -= len(clean)
            return clean, False
        represented = clean[: self.remaining]
        self.remaining = 0
        self.truncated = True
        return represented, True


def _abstract_family(
    citation: ET.Element | None,
    content: ET.Element | None,
    *,
    requested: bool,
) -> tuple[dict[str, Any], bool]:
    if not requested:
        return source_family("abstracts", state="not_requested"), False

    abstract_nodes: list[tuple[str, ET.Element]] = []
    primary = _child(content, "Abstract")
    if primary is not None:
        abstract_nodes.append(("primary", primary))
    for alternate in _children(citation, "OtherAbstract"):
        abstract_nodes.append(("other", alternate))

    if not abstract_nodes:
        return source_family("abstracts", state="not_represented"), False

    article_languages = [
        value
        for node in _children(content, "Language")
        if (value := _element_text(node, 40))
    ]
    budget = _TextBudget(_MAX_ABSTRACT_TEXT)
    items: list[dict[str, Any]] = []
    for abstract_index, (kind, abstract) in enumerate(abstract_nodes, start=1):
        sections = _children(abstract, "AbstractText")
        if not sections and _element_text(abstract, _MAX_ABSTRACT_TEXT + 1):
            sections = [abstract]
        section_items: list[dict[str, Any]] = []
        abstract_truncated = abstract.attrib.get("TruncatedYN") == "Y"
        for section_index, section in enumerate(sections, start=1):
            raw_text = bounded_text(
                "".join(section.itertext()), _MAX_ABSTRACT_TEXT + 1
            )
            text, truncated = budget.take(raw_text)
            provider_truncated = section.attrib.get("TruncatedYN") == "Y"
            section_truncated = truncated or provider_truncated
            abstract_truncated = abstract_truncated or section_truncated
            label = bounded_text(section.attrib.get("Label"), 200)
            category = bounded_text(section.attrib.get("NlmCategory"), 100)
            language = bounded_text(section.attrib.get("Language"), 40)
            section_items.append(
                source_item(
                    f"{kind}-{abstract_index}-section-{section_index}",
                    "abstract_section",
                    [
                        source_fact(
                            "text", "abstract_text", text, "represented", _PROVIDER
                        ),
                        source_fact(
                            "label",
                            "section_label",
                            label or None,
                            "represented" if label else "not_represented",
                            _PROVIDER,
                        ),
                        source_fact(
                            "nlm_category",
                            "section_category",
                            category or None,
                            "represented" if category else "not_represented",
                            _PROVIDER,
                        ),
                        source_fact(
                            "language",
                            "section_language",
                            language or None,
                            "represented" if language else "not_represented",
                            _PROVIDER,
                        ),
                        source_fact(
                            "truncated",
                            "fidelity",
                            section_truncated,
                            "represented",
                            _PROVIDER,
                        ),
                    ],
                )
            )

        source_type = bounded_text(abstract.attrib.get("Type"), 100)
        language = bounded_text(abstract.attrib.get("Language"), 40)
        languages = [language] if language else article_languages
        copyright_text = _element_text(
            _child(abstract, "CopyrightInformation"), 2_000
        )
        items.append(
            source_item(
                f"{kind}-{abstract_index}",
                "primary_abstract" if kind == "primary" else "other_abstract",
                [
                    source_fact(
                        "abstract_role", "role", kind, "represented", _PROVIDER
                    ),
                    source_fact(
                        "source_type",
                        "provider_type",
                        source_type or None,
                        "represented" if source_type else "not_represented",
                        _PROVIDER,
                    ),
                    source_fact(
                        "languages",
                        "language",
                        languages,
                        "represented" if languages else "not_represented",
                        _PROVIDER,
                    ),
                    source_fact(
                        "copyright",
                        "copyright",
                        copyright_text or None,
                        "represented" if copyright_text else "not_represented",
                        _PROVIDER,
                    ),
                    source_fact(
                        "truncated",
                        "fidelity",
                        abstract_truncated,
                        "represented",
                        _PROVIDER,
                    ),
                ],
                section_items,
            )
        )
    return (
        source_family(
            "abstracts",
            state="represented",
            items=items,
            represented_count=len(items),
        ),
        budget.truncated,
    )


def _indexing_family(
    citation: ET.Element | None,
    *,
    requested: bool,
) -> dict[str, Any]:
    if not requested:
        return source_family("indexing", state="not_requested")

    values: list[dict[str, Any]] = []
    for index, heading in enumerate(
        _descendants(_child(citation, "MeshHeadingList"), "MeshHeading"),
        start=1,
    ):
        descriptor = _child(heading, "DescriptorName")
        descriptor_name = _element_text(descriptor, 500)
        qualifiers: list[dict[str, Any]] = []
        for qualifier_index, qualifier in enumerate(
            _children(heading, "QualifierName"), start=1
        ):
            qualifier_name = _element_text(qualifier, 300)
            qualifiers.append(
                source_item(
                    f"mesh-{index}-qualifier-{qualifier_index}",
                    "mesh_qualifier",
                    [
                        source_fact(
                            "name",
                            "qualifier",
                            qualifier_name,
                            "represented",
                            _PROVIDER,
                        ),
                        source_fact(
                            "ui",
                            "mesh_ui",
                            bounded_text(qualifier.attrib.get("UI"), 40) or None,
                            (
                                "represented"
                                if qualifier.attrib.get("UI")
                                else "not_represented"
                            ),
                            _PROVIDER,
                        ),
                        source_fact(
                            "major_topic",
                            "indexing_flag",
                            qualifier.attrib.get("MajorTopicYN") == "Y",
                            "represented",
                            _PROVIDER,
                        ),
                    ],
                )
            )
        values.append(
            source_item(
                f"mesh-{index}",
                "mesh_heading",
                [
                    source_fact(
                        "descriptor",
                        "mesh_descriptor",
                        descriptor_name,
                        "represented",
                        _PROVIDER,
                    ),
                    source_fact(
                        "ui",
                        "mesh_ui",
                        (
                            bounded_text(descriptor.attrib.get("UI"), 40)
                            if descriptor is not None
                            else None
                        ),
                        (
                            "represented"
                            if descriptor is not None and descriptor.attrib.get("UI")
                            else "not_represented"
                        ),
                        _PROVIDER,
                    ),
                    source_fact(
                        "major_topic",
                        "indexing_flag",
                        (
                            descriptor is not None
                            and descriptor.attrib.get("MajorTopicYN") == "Y"
                        ),
                        "represented",
                        _PROVIDER,
                    ),
                ],
                qualifiers,
            )
        )

    chemical_list = _child(citation, "ChemicalList")
    for index, chemical in enumerate(_children(chemical_list, "Chemical"), start=1):
        substance = _child(chemical, "NameOfSubstance")
        values.append(
            source_item(
                f"chemical-{index}",
                "chemical",
                [
                    source_fact(
                        "name",
                        "substance",
                        _element_text(substance, 500),
                        "represented",
                        _PROVIDER,
                    ),
                    source_fact(
                        "ui",
                        "mesh_ui",
                        (
                            bounded_text(substance.attrib.get("UI"), 40)
                            if substance is not None
                            else None
                        ),
                        (
                            "represented"
                            if substance is not None and substance.attrib.get("UI")
                            else "not_represented"
                        ),
                        _PROVIDER,
                    ),
                    source_fact(
                        "registry_number",
                        "registry_number",
                        _element_text(_child(chemical, "RegistryNumber"), 100)
                        or None,
                        (
                            "represented"
                            if _element_text(
                                _child(chemical, "RegistryNumber"), 100
                            )
                            else "not_represented"
                        ),
                        _PROVIDER,
                    ),
                ],
            )
        )

    for list_index, keyword_list in enumerate(
        _children(citation, "KeywordList"), start=1
    ):
        owner = bounded_text(keyword_list.attrib.get("Owner"), 40)
        for keyword_index, keyword in enumerate(
            _children(keyword_list, "Keyword"), start=1
        ):
            values.append(
                source_item(
                    f"keyword-{list_index}-{keyword_index}",
                    "keyword",
                    [
                        source_fact(
                            "value",
                            "keyword",
                            _element_text(keyword, 500),
                            "represented",
                            _PROVIDER,
                        ),
                        source_fact(
                            "owner",
                            "indexing_owner",
                            owner or None,
                            "represented" if owner else "not_represented",
                            _PROVIDER,
                        ),
                        source_fact(
                            "major_topic",
                            "indexing_flag",
                            keyword.attrib.get("MajorTopicYN") == "Y",
                            "represented",
                            _PROVIDER,
                        ),
                    ],
                )
            )

    represented = values[:500]
    return source_family(
        "indexing",
        state="represented" if values else "not_represented",
        items=represented,
        represented_count=len(represented),
        omitted_count=max(0, len(values) - len(represented)),
    )


def _publication_types(content: ET.Element | None) -> list[tuple[str, str | None]]:
    publication_type_list = _child(content, "PublicationTypeList")
    values = _children(publication_type_list, "PublicationType")
    if not values:
        values = _children(content, "PublicationType")
    result: list[tuple[str, str | None]] = []
    for value in values:
        name = _element_text(value, 300)
        if name:
            result.append((name, bounded_text(value.attrib.get("UI"), 40) or None))
    return result


def _publication_role(types: list[str], default: str) -> str:
    lowered = [value.casefold() for value in types]
    if any("retraction of publication" == value for value in lowered):
        return "retraction_notice"
    if any("retracted publication" == value for value in lowered):
        return "retracted_publication"
    if any("published erratum" == value for value in lowered):
        return "correction_notice"
    if any("expression of concern" == value for value in lowered):
        return "expression_of_concern"
    if any("corrected and republished article" == value for value in lowered):
        return "corrected_and_republished"
    return default


def _publication_role_family(
    values: list[tuple[str, str | None]],
) -> dict[str, Any]:
    items = [
        source_item(
            f"publication-type-{index}",
            "publication_type",
            [
                source_fact("name", "publication_type", name, "represented", _PROVIDER),
                source_fact(
                    "ui",
                    "mesh_ui",
                    ui,
                    "represented" if ui else "not_represented",
                    _PROVIDER,
                ),
            ],
        )
        for index, (name, ui) in enumerate(values[:100], start=1)
    ]
    return source_family(
        "publication_roles",
        state="represented" if values else "not_represented",
        items=items,
        represented_count=len(items),
        omitted_count=max(0, len(values) - len(items)),
    )


def _funding_family(
    citation: ET.Element | None,
    *,
    requested: bool,
) -> dict[str, Any]:
    if not requested:
        return source_family("funding", state="not_requested")
    grants = _descendants(_child(citation, "GrantList"), "Grant")
    items: list[dict[str, Any]] = []
    for index, grant in enumerate(grants[:200], start=1):
        facts: list[dict[str, Any]] = []
        for tag, role in (
            ("GrantID", "grant_id"),
            ("Acronym", "acronym"),
            ("Agency", "agency"),
            ("Country", "country"),
        ):
            value = _element_text(_child(grant, tag), 500)
            facts.append(
                source_fact(
                    tag.casefold(),
                    role,
                    value or None,
                    "represented" if value else "not_represented",
                    _PROVIDER,
                )
            )
        items.append(source_item(f"grant-{index}", "grant", facts))
    return source_family(
        "funding",
        state="represented" if grants else "not_represented",
        items=items,
        represented_count=len(items),
        omitted_count=max(0, len(grants) - len(items)),
    )


def _article_aliases(
    branch: ET.Element,
    pmid: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    identities: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    if _local_name(branch) == "PubmedArticle":
        article_id_list = _path(branch, "PubmedData", "ArticleIdList")
        lists = [article_id_list] if article_id_list is not None else []
    else:
        lists = [
            value
            for value in (
                _path(branch, "PubmedBookData", "ArticleIdList"),
                _path(branch, "BookDocument", "ArticleIdList"),
            )
            if value is not None
        ]
    for article_id_list in lists:
        for article_id in _children(article_id_list, "ArticleId"):
            id_type = article_id.attrib.get("IdType", "").strip().casefold()
            value = _element_text(article_id, 300)
            if not value:
                continue
            try:
                if id_type in {"pubmed", "pmid"}:
                    if value != pmid:
                        warnings.append(
                            warning(
                                "partial_coverage",
                                "pubmed.article_ids",
                                "A represented PMID alias did not match the record PMID",
                            )
                        )
                elif id_type == "doi":
                    identities.append(
                        {"kind": "doi", "value": normalize_doi(value)}
                    )
                elif (
                    id_type in {"pmc", "pmcid"}
                    and 4 <= len(value) <= 20
                    and _PMCID_RE.fullmatch(value)
                ):
                    identities.append({"kind": "pmcid", "value": value.upper()})
            except ValueError:
                warnings.append(
                    warning(
                        "partial_coverage",
                        "pubmed.article_ids",
                        "A malformed represented alias was not normalized",
                    )
                )
    unique = unique_identities(identities) if identities else []
    aliases = [
        {"identity": identity, "role": "article_id", "source": _PROVIDER}
        for identity in unique
    ]
    return aliases, warnings


def _date_observations(
    citation: ET.Element | None,
    content: ET.Element | None,
    data: ET.Element | None,
) -> list[dict[str, Any]]:
    values: list[tuple[str, str]] = []

    journal_date = _path(content, "Journal", "JournalIssue", "PubDate")
    if journal_date is None:
        journal_date = _path(content, "Book", "PubDate")
    if date := _date_text(journal_date):
        values.append(("publication", date))

    for article_date in _children(content, "ArticleDate"):
        date = _date_text(article_date)
        if date:
            role = bounded_text(article_date.attrib.get("DateType"), 50)
            values.append((role.casefold() or "article_date", date))
    if date := _date_text(_child(content, "ContributionDate")):
        values.append(("contribution", date))

    for tag, role in (
        ("DateCompleted", "medline_completed"),
        ("DateRevised", "medline_revised"),
    ):
        if date := _date_text(_child(citation, tag)):
            values.append((role, date))

    history = _child(data, "History")
    for provider_date in _children(history, "PubMedPubDate"):
        date = _date_text(provider_date)
        if date:
            status = bounded_text(provider_date.attrib.get("PubStatus"), 50)
            values.append((f"pubmed_{status.casefold()}" if status else "pubmed", date))

    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    for role, value in values:
        key = (role, value)
        if key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "role": role,
                "value": value,
                "state": "represented",
                "source": _PROVIDER,
            }
        )
    return result


_RELATION_MAP: dict[str, tuple[str, str]] = {
    "Cites": ("cites", "outbound"),
    "CommentOn": ("comments_on", "outbound"),
    "CommentIn": ("commented_on_by", "inbound"),
    "ErratumFor": ("corrects", "outbound"),
    "ErratumIn": ("corrected_by", "inbound"),
    "ExpressionOfConcernFor": ("expresses_concern_about", "outbound"),
    "ExpressionOfConcernIn": ("has_expression_of_concern", "inbound"),
    "RepublishedFrom": ("updates", "outbound"),
    "RepublishedIn": ("updated_by", "inbound"),
    "RetractionOf": ("retracts", "outbound"),
    "RetractionIn": ("retracted_by", "inbound"),
    "UpdateOf": ("updates", "outbound"),
    "UpdateIn": ("updated_by", "inbound"),
    "ReprintOf": ("version_of", "outbound"),
    "ReprintIn": ("has_version", "inbound"),
    "SummaryFor": ("summarizes", "outbound"),
    "OriginalReportIn": ("summarized_by", "inbound"),
}


def _relationships(
    citation: ET.Element | None,
    *,
    pmid: str,
    retrieved_at: str,
    requested: bool,
) -> tuple[list[dict[str, Any]], bool]:
    if not requested:
        return [], False
    seed = _pmid_identity(pmid)
    relation_list = _child(citation, "CommentsCorrectionsList")
    represented = _children(relation_list, "CommentsCorrections")
    edges: list[dict[str, Any]] = []
    for index, item in enumerate(represented[:200], start=1):
        ref_type = bounded_text(item.attrib.get("RefType"), 100)
        relation, direction = _RELATION_MAP.get(
            ref_type,
            (f"pubmed:{ref_type or 'unknown'}", "outbound"),
        )
        target_text = _element_text(_child(item, "PMID"), 20)
        target = (
            _pmid_identity(target_text)
            if _PMID_RE.fullmatch(target_text)
            else None
        )
        ref_source = _element_text(_child(item, "RefSource"), 1_500)
        note = _element_text(_child(item, "Note"), 1_500)
        unresolved = None
        if target is None and (ref_source or note or target_text):
            unresolved = {
                "kind": (
                    "provider_identifier" if target_text else "citation_string"
                ),
                "value": target_text or ref_source or note,
                "source_collection": "pubmed",
                "reason_unresolved": (
                    "PubMed did not represent a valid numeric target PMID"
                ),
            }
        if target is None and unresolved is None:
            continue

        subject = target if direction == "inbound" and target is not None else seed
        edge = relationship_edge(
            seed=seed,
            relation=relation,
            provider=_PROVIDER,
            provider_vocabulary=f"CommentsCorrections:{ref_type or 'unknown'}",
            subject=subject,
            target=target,
            unresolved_lead=unresolved,
            asserted_by=_PROVIDER,
            retrieved_at=retrieved_at,
            provider_position=index,
            coverage_state=coverage(
                "direct CommentsCorrections represented on this PubMed record",
                1,
                provider_count=len(represented),
                limitations=["direct record relationships are not a paged citation graph"],
            ),
        )
        if direction == "inbound" and target is not None:
            edge["assertions"][0]["object"] = seed
        edges.append(edge)
    return edges, len(represented) > len(edges)


def _integrity_state(
    publication_types: list[str],
    relationships: list[dict[str, Any]],
    *,
    deleted: bool = False,
) -> dict[str, Any]:
    labels: list[str] = []
    if deleted:
        labels.append("deleted_citation")
    lowered = [value.casefold() for value in publication_types]
    for label, needle in (
        ("retracted", "retracted publication"),
        ("retraction_notice", "retraction of publication"),
        ("correction_notice", "published erratum"),
        ("expression_of_concern", "expression of concern"),
    ):
        if needle in lowered and label not in labels:
            labels.append(label)
    for edge in relationships:
        relation = edge.get("relation")
        if relation in {
            "retracted_by",
            "corrected_by",
            "has_expression_of_concern",
        } and relation not in labels:
            labels.append(str(relation))

    facts = [
        source_fact(
            "publication_types",
            "integrity_source",
            publication_types,
            "represented" if publication_types else "not_represented",
            _PROVIDER,
        ),
        source_fact(
            "direct_relationship_count",
            "integrity_source",
            len(relationships),
            "represented",
            _PROVIDER,
        ),
    ]
    return {
        "state": "represented" if labels else "unknown",
        "labels": labels,
        "facts": facts,
    }


def _record_currentness(
    dates: list[dict[str, Any]],
    retrieved_at: str,
) -> dict[str, Any]:
    observations = [
        {
            "role": value["role"],
            "value": value["value"],
            "source": _PROVIDER,
            "state": value["state"],
        }
        for value in dates
        if value["role"]
        in {
            "medline_revised",
            "pubmed_entrez",
            "pubmed_pubmed",
            "pubmed_medline",
        }
    ]
    observations.append(
        {
            "role": "retrieved_at",
            "value": retrieved_at,
            "source": _PROVIDER,
            "state": "represented",
        }
    )
    return currentness(observations, drift="unknown")


def _deleted_record(
    pmid: str,
    projection: str,
    include: list[str],
    retrieved_at: str,
) -> dict[str, Any]:
    requested = _pmid_identity(pmid)
    abstract_state = (
        "not_represented"
        if projection == "standard" or "abstracts" in include
        else "not_requested"
    )
    indexing_state = (
        "not_represented"
        if projection == "standard" or "indexing" in include
        else "not_requested"
    )
    funding_state = "not_represented" if "funding" in include else "not_requested"
    extension = {
        "kind": "pubmed",
        "abstracts": source_family("abstracts", state=abstract_state),
        "indexing": source_family("indexing", state=indexing_state),
        "publication_roles": source_family(
            "publication_roles",
            state="represented",
            items=[
                source_item(
                    "deleted-citation",
                    "record_state",
                    [
                        source_fact(
                            "state",
                            "record_state",
                            "deleted_citation",
                            "represented",
                            _PROVIDER,
                        )
                    ],
                )
            ],
        ),
        "funding": source_family("funding", state=funding_state),
    }
    record = qualified_record(
        requested_identity=requested,
        canonical_identity=requested,
        provider=_PROVIDER,
        record_class="deleted_citation",
        record_role="deleted",
        title=None,
        contributors=[],
        contributor_count=0,
        container_title=None,
        projection=projection,
        extension=extension,
        currentness_state=_retrieval_currentness(retrieved_at),
        integrity_state=_integrity_state([], [], deleted=True),
        represented_families=["integrity", "publication_roles"],
        relationships=[],
        locator_id=pmid,
    )
    unsupported = [value for value in include if value not in _SUPPORTED_INCLUDES]
    record["projection"] = projection_manifest(
        projection,
        ["integrity", "publication_roles"],
        unsupported=unsupported,
    )
    return record


def _qualified_article_record(
    branch: ET.Element,
    *,
    pmid: str,
    projection: str,
    include: list[str],
    retrieved_at: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    branch_name = _local_name(branch)
    if branch_name == "PubmedArticle":
        citation = _child(branch, "MedlineCitation")
        content = _child(citation, "Article")
        data = _child(branch, "PubmedData")
        record_class = "publication"
        default_role = "publication"
        title_element = _child(content, "ArticleTitle")
        container_title = _element_text(_path(content, "Journal", "Title"), 500)
        publisher = None
    else:
        citation = _child(branch, "BookDocument")
        content = citation
        data = _child(branch, "PubmedBookData")
        record_class = "book_article"
        default_role = "book_article"
        title_element = _child(content, "ArticleTitle")
        book = _child(content, "Book")
        if title_element is None:
            title_element = _child(book, "BookTitle")
        container_title = _element_text(_child(book, "BookTitle"), 500)
        publisher = _element_text(_path(book, "Publisher", "PublisherName"), 500)

    if citation is None or content is None:
        raise ValueError("PubMed record branch omitted its bibliographic structure")

    title = _element_text(title_element, 2_000)
    contributors, contributor_count = _contributors(content)
    type_pairs = _publication_types(content)
    type_names = [name for name, _ in type_pairs]
    role = _publication_role(type_names, default_role)
    dates = _date_observations(citation, content, data)
    abstract_requested = projection == "standard" or "abstracts" in include
    indexing_requested = projection == "standard" or "indexing" in include
    relationships_requested = "relationships" in include
    funding_requested = "funding" in include

    abstracts, abstract_truncated = _abstract_family(
        citation, content, requested=abstract_requested
    )
    indexing = _indexing_family(citation, requested=indexing_requested)
    funding = _funding_family(content, requested=funding_requested)
    relationships, relationship_omission = _relationships(
        citation,
        pmid=pmid,
        retrieved_at=retrieved_at,
        requested=relationships_requested,
    )
    aliases, alias_warnings = _article_aliases(branch, pmid)
    extension = {
        "kind": "pubmed",
        "abstracts": abstracts,
        "indexing": indexing,
        "publication_roles": _publication_role_family(type_pairs),
        "funding": funding,
    }

    represented_families = [
        "metadata",
        "publication_roles",
        "integrity",
    ]
    for name, was_requested in (
        ("abstracts", abstract_requested),
        ("indexing", indexing_requested),
        ("relationships", relationships_requested),
        ("funding", funding_requested),
    ):
        if was_requested:
            represented_families.append(name)

    unsupported = [value for value in include if value not in _SUPPORTED_INCLUDES]
    omitted = []
    local_failures: list[dict[str, Any]] = []
    local_warnings = list(alias_warnings)
    if abstract_truncated:
        omitted.append("abstracts")
        local_failures.append(
            failure(
                "projection_incomplete",
                "projection_or_selection",
                "record.extension.abstracts",
                "Abstract text exceeded the 20,000-character record ceiling",
            )
        )
        local_warnings.append(
            warning(
                "partial_coverage",
                "record.extension.abstracts",
                "Abstract sections retain labels and explicit truncation state",
            )
        )
    if relationship_omission:
        omitted.append("relationships")
        local_failures.append(
            failure(
                "projection_incomplete",
                "projection_or_selection",
                "record.direct_relationships",
                "Direct PubMed relationships exceeded the adapter bound",
            )
        )
    for name in unsupported:
        local_failures.append(
            failure(
                "unsupported_combination",
                "projection_or_selection",
                f"include.{name}",
                f"PubMed does not support qualification family {name!r}",
            )
        )

    record_currentness = _record_currentness(dates, retrieved_at)
    record = qualified_record(
        requested_identity=_pmid_identity(pmid),
        canonical_identity=_pmid_identity(pmid),
        provider=_PROVIDER,
        record_class=record_class,
        record_role=role,
        title=title,
        contributors=contributors,
        contributor_count=contributor_count,
        container_title=container_title,
        container_kind="journal" if branch_name == "PubmedArticle" else "book",
        publisher=publisher,
        projection=projection,
        extension=extension,
        aliases=aliases,
        dates=dates,
        currentness_state=record_currentness,
        integrity_state=_integrity_state(type_names, relationships),
        represented_families=represented_families,
        relationships=relationships,
        warnings=local_warnings,
        locator_id=pmid,
    )
    record["projection"] = projection_manifest(
        projection,
        represented_families,
        unsupported=unsupported,
        omitted=omitted,
    )
    if contributor_count > len(contributors):
        record["contributors"]["truncated"] = True
    return record, local_failures, local_warnings


async def qualify_publication(
    http: ProviderHttpClient,
    *,
    pmid: str,
    projection: str,
    include: list[str],
) -> dict:
    """Qualify one exact PMID from one bounded, safely parsed EFetch record."""

    retrieved_at = utc_now()
    clean_pmid = pmid.strip() if isinstance(pmid, str) else ""
    clean_include = list(include) if isinstance(include, list) else []
    output = _qualification_base(
        pmid=clean_pmid,
        projection=projection,
        include=clean_include,
        retrieved_at=retrieved_at,
    )

    if not _PMID_RE.fullmatch(clean_pmid):
        return _validation_failure(
            output,
            "invalid_identifier",
            "pmid",
            "PMID must contain 1..12 digits",
        )
    if projection not in {"summary", "standard", "source_detail"}:
        return _validation_failure(
            output,
            "invalid_request",
            "projection",
            "projection is not supported",
        )
    if (
        not isinstance(include, list)
        or any(not isinstance(value, str) or not value for value in include)
        or len(include) != len(set(include))
    ):
        return _validation_failure(
            output,
            "invalid_request",
            "include",
            "include must contain unique, nonempty section names",
        )
    if projection == "source_detail" and not include:
        return _validation_failure(
            output,
            "invalid_request",
            "include",
            "source_detail requires at least one included family",
        )

    fields = {
        "db": "pubmed",
        "id": clean_pmid,
        "retmode": "xml",
        "rettype": "abstract",
    }
    try:
        result = await http.get(
            _PROVIDER,
            "efetch.fcgi",
            params=_ncbi_params(fields),
            accept="application/xml, text/xml",
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
                "pubmed.efetch",
                "provider transport raised an exception",
            )
        )
        return output
    output["call_count"] = 1
    if _transport_failure(output, result, scope="pubmed.efetch"):
        return output

    body = result.body
    if not isinstance(body, bytes) or not body:
        return _provider_failure(
            output,
            "provider_schema_drift",
            "PubMed EFetch returned an empty XML body",
        )
    if not _is_xml_content_type(result.content_type):
        code = (
            "provider_error_body"
            if isinstance(result.content_type, str)
            and "html" in result.content_type.casefold()
            else "provider_schema_drift"
        )
        return _provider_failure(
            output,
            code,
            "PubMed EFetch returned a non-XML response body",
        )
    if not _xml_is_safe(body):
        return _provider_failure(
            output,
            "parser_limit_exceeded",
            "PubMed XML contains a forbidden DTD or entity declaration",
        )
    try:
        root = ET.fromstring(body)
    except RecursionError:
        return _provider_failure(
            output,
            "parser_limit_exceeded",
            "PubMed XML exceeded the parser depth limit",
        )
    except (ET.ParseError, LookupError, ValueError):
        return _provider_failure(
            output,
            "provider_schema_drift",
            "PubMed EFetch returned malformed XML",
        )
    if not _xml_uses_unqualified_tags(root):
        return _provider_failure(
            output,
            "provider_schema_drift",
            "PubMed EFetch returned XML with an unexpected namespace",
        )
    if not _xml_within_limits(root):
        return _provider_failure(
            output,
            "parser_limit_exceeded",
            "PubMed XML exceeded structural parser limits",
        )

    for error_node in (
        node
        for node in root.iter()
        if _local_name(node).casefold() == "error"
    ):
        if _element_text(error_node, 800):
            return _provider_failure(
                output,
                "provider_error_body",
                "PubMed EFetch returned an XML error body",
            )

    branch_names = {"PubmedArticle", "PubmedBookArticle", "DeleteCitation"}
    root_name = _local_name(root)
    declared_root = _declared_doctype_root(result.body)
    if (
        not isinstance(root.tag, str)
        or root.tag != root_name
        or (declared_root is not None and declared_root != root_name)
    ):
        return _provider_failure(
            output,
            "provider_schema_drift",
            "PubMed EFetch returned an inconsistent XML document root",
        )
    if root_name not in {
        "PubmedArticleSet",
        "PubmedBookArticleSet",
        *branch_names,
    }:
        return _provider_failure(
            output,
            "provider_schema_drift",
            "PubMed EFetch returned an unexpected XML document root",
        )
    branches = [
        node for node in root.iter() if _local_name(node) in branch_names
    ]
    branch_id_lists = [_branch_pmids(branch) for branch in branches]
    represented_ids = [value for values in branch_id_lists for value in values]
    if not branches:
        output["stages"]["provider_body"] = "succeeded"
        return _provider_failure(
            output,
            "provider_not_found",
            "PubMed did not return a record for the requested PMID",
            stage="identity_or_query_fidelity",
            scope="pmid",
            absence=True,
        )
    if not represented_ids or any(not values for values in branch_id_lists):
        output["stages"]["provider_body"] = "succeeded"
        return _provider_failure(
            output,
            "identity_mismatch",
            "A returned PubMed record branch omitted a valid PMID",
            stage="identity_or_query_fidelity",
            scope="pmid",
        )
    if set(represented_ids) != {clean_pmid}:
        output["stages"]["provider_body"] = "succeeded"
        return _provider_failure(
            output,
            "identity_mismatch",
            "PubMed returned an identity other than the requested PMID",
            stage="identity_or_query_fidelity",
            scope="pmid",
        )
    matching = [
        branch for branch in branches if clean_pmid in _branch_pmids(branch)
    ]
    if len(matching) != 1:
        output["stages"]["provider_body"] = "succeeded"
        return _provider_failure(
            output,
            "ambiguous_identity",
            "PubMed returned more than one branch for the requested PMID",
            stage="identity_or_query_fidelity",
            scope="pmid",
        )

    output["stages"]["provider_body"] = "succeeded"
    output["stages"]["identity_or_query_fidelity"] = "succeeded"
    branch = matching[0]
    if _local_name(branch) == "DeleteCitation":
        record = _deleted_record(
            clean_pmid, projection, clean_include, retrieved_at
        )
        unsupported = [
            value for value in clean_include if value not in _SUPPORTED_INCLUDES
        ]
        for name in unsupported:
            output["failures"].append(
                failure(
                    "unsupported_combination",
                    "projection_or_selection",
                    f"include.{name}",
                    f"PubMed does not support qualification family {name!r}",
                )
            )
        output["record"] = record
    else:
        try:
            record, local_failures, local_warnings = _qualified_article_record(
                branch,
                pmid=clean_pmid,
                projection=projection,
                include=clean_include,
                retrieved_at=retrieved_at,
            )
        except ValueError:
            return _provider_failure(
                output,
                "provider_schema_drift",
                "PubMed record omitted required bibliographic structure",
            )
        output["record"] = record
        output["failures"].extend(local_failures)
        output["warnings"].extend(local_warnings)

    output["stages"]["projection_or_selection"] = (
        "inconsistent"
        if any(
            item.get("code") == "projection_incomplete"
            for item in output["failures"]
        )
        else "succeeded"
    )
    output["stages"]["result"] = "succeeded"
    output["stages"]["currentness"] = "succeeded"
    output["currentness"] = output["record"]["currentness"]
    output["warnings"].append(
        warning(
            "currentness_not_history",
            "pubmed.record",
            "The record reflects PubMed state observed at retrieval time",
        )
    )
    return output
