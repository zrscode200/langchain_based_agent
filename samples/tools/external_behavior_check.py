#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx>=0.27",
#   "mcp==1.28.1",
#   "pydantic>=2",
# ]
# ///
"""Offline orchestration checks for literature retrieval and backward compatibility."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
CONNECTOR = HERE / "ics_public_science"
sys.path.insert(0, str(CONNECTOR))

import server  # noqa: E402
from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402
from envelopes import currentness, failure, stages  # noqa: E402
from http_client import HttpResult  # noqa: E402
from normalization import candidate  # noqa: E402
from registry import RuntimeRegistry  # noqa: E402


CHECKS = 0
REAL_PUBMED_MAP = server.pubmed.map_publications
REAL_PUBMED_GET = server.pubmed.qualify_publication
REAL_EUROPE_PMC_MAP = server.europe_pmc.map_records


def check(condition: bool, message: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(message)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class DummyHttp:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def get(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        before_attempt = kwargs.pop("before_attempt", None)
        if before_attempt is not None:
            before_attempt()
        self.calls.append((args, kwargs))
        return {"fixture": True}


class FixtureHttp:
    def __init__(
        self,
        result: HttpResult | list[HttpResult],
    ) -> None:
        self.results = result if isinstance(result, list) else [result]
        self.calls = 0

    async def get(self, *_: Any, **kwargs: Any) -> HttpResult:
        before_attempt = kwargs.pop("before_attempt", None)
        if before_attempt is not None:
            before_attempt()
        self.calls += 1
        if not self.results:
            raise AssertionError("FixtureHttp exhausted its queued responses")
        return self.results.pop(0)


def represented_stages(result: str = "succeeded") -> dict[str, str]:
    return stages(
        validation="succeeded",
        disclosure="not_requested",
        request_build="succeeded",
        transport="succeeded",
        provider_body="succeeded",
        identity_or_query_fidelity="succeeded",
        projection_or_selection="succeeded",
        result=result,
        currentness="succeeded",
    )


def pubmed_card(position: int) -> dict[str, Any]:
    return candidate(
        identity={"kind": "pmid", "value": str(12000 + position)},
        represented_identifiers=[
            {"kind": "doi", "value": "10.1000/example"}
        ],
        provider="pubmed",
        record_class="journal_article",
        title="A public mechanism study",
        contributors=["A Researcher"],
        contributor_count=1,
        container="Journal",
        date="2026",
        searched_scope="title_abstract",
        provider_order="relevance",
        absolute_position=position,
    )


def epmc_card(
    position: int,
    *,
    collection: str = "MED",
    source_id: str = "12345",
    abstract: str | None = None,
    alias_kind: str | None = None,
    alias_value: str | None = None,
) -> dict[str, Any]:
    value = candidate(
        identity={
            "kind": "europe_pmc",
            "collection": collection,
            "value": source_id,
        },
        represented_identifiers=[
            {
                "kind": alias_kind
                or ("pmid" if collection == "MED" else "pmcid"),
                "value": alias_value or source_id,
            }
        ],
        provider="europe_pmc",
        record_class="journal_article",
        title="A public mechanism study",
        contributors=["A Researcher"],
        contributor_count=1,
        container="Journal",
        date="2026-01-01",
        date_role="first_publication",
        searched_scope="title_abstract",
        provider_order="relevance",
        absolute_position=position,
        context_kind="abstract_excerpt" if abstract is not None else "none",
        context_text=abstract,
        context_source_role="abstract" if abstract is not None else None,
        integrity_signals=[
            {
                "kind": "retracted",
                "state": "represented",
                "source": "europe_pmc",
            }
        ],
        access_signals=[
            {
                "kind": "open_access",
                "state": "represented",
                "value": True,
                "source": "europe_pmc",
            }
        ],
    )
    value.update(
        {
            "source_collection": collection,
            "source_id": source_id,
            "abstract_available": abstract is not None,
        }
    )
    if abstract is not None:
        value["abstract"] = abstract
    return value


def install_adapter_stubs(calls: dict[str, list[dict[str, Any]]]) -> None:
    async def map_publications(http: Any, **args: Any) -> dict[str, Any]:
        calls.setdefault("pubmed_search", []).append(args)
        await http.get("pubmed", "esearch.fcgi")
        await http.get("pubmed", "esummary.fcgi")
        offset = args["offset"]
        return {
            "provider": "pubmed",
            "stages": represented_stages(),
            "candidates": [pubmed_card(offset)],
            "count_observation": {
                "provider_count": 3,
                "returned_id_count": 1,
            },
            "continuation_observation": {
                "state": "available" if offset < 2 else "exhausted",
                "next_offset": offset + 1 if offset < 2 else None,
            },
            "coverage": {"scope": "fixture", "returned": 1},
            "failures": [],
            "warnings": [],
            "currentness": currentness(),
        }

    async def qualify_publication(http: Any, **args: Any) -> dict[str, Any]:
        calls.setdefault("pubmed_get", []).append(args)
        await http.get("pubmed", "efetch.fcgi")
        identity = {"kind": "pmid", "value": args["pmid"]}
        return {
            "provider": "pubmed",
            "stages": represented_stages(),
            "record": {
                "canonical_identity": identity,
                "aliases": [
                    {
                        "identity": {
                            "kind": "doi",
                            "value": "10.1000/example",
                        },
                        "role": "article_id",
                        "source": "pubmed",
                    }
                ],
                "record_class": "book_article",
                "title": "Exact PubMed record",
                "contributors": {
                    "names": ["A Researcher"],
                    "represented_count": 1,
                },
                "container": {
                    "kind": "book",
                    "title": "A Test Book",
                    "publisher": "Test Publisher",
                },
                "dates": [{"role": "published", "value": "2026-01-01"}],
                "extension": {
                    "abstracts": {
                        "state": (
                            "represented"
                            if args["projection"] == "standard"
                            else "not_requested"
                        ),
                        "items": (
                            [
                                {
                                    "facts": [
                                        {
                                            "name": "truncated",
                                            "value": False,
                                        }
                                    ],
                                    "children": [
                                        {
                                            "facts": [
                                                {
                                                    "name": "label",
                                                    "value": "Results",
                                                },
                                                {
                                                    "name": "text",
                                                    "value": "Exact abstract.",
                                                },
                                                {
                                                    "name": "truncated",
                                                    "value": False,
                                                },
                                            ]
                                        }
                                    ],
                                }
                            ]
                            if args["projection"] == "standard"
                            else []
                        ),
                    }
                },
                "integrity": {
                    "state": "represented",
                    "labels": ["correction_notice"],
                    "facts": [
                        {
                            "name": "publication_types",
                            "role": "integrity_source",
                            "value": ["Published Erratum"],
                            "state": "represented",
                            "source": "pubmed",
                        },
                        {
                            "name": "direct_relationship_count",
                            "role": "integrity_source",
                            "value": 1,
                            "state": "represented",
                            "source": "pubmed",
                        },
                    ],
                },
                "access": {
                    "state": "unknown",
                    "facts": [],
                    "content_eligibility": "not_evaluated",
                },
                "direct_relationships": [
                    {
                        "relation": "corrected_by",
                        "qualification": {
                            "state": "unknown",
                            "transition": None,
                        },
                    }
                ],
                "content_manifest": [{"role": "jats"}],
                "transitions": [{"kind": "content"}],
            },
            "failures": [],
            "warnings": [],
            "currentness": currentness(),
        }

    async def map_records(http: Any, **args: Any) -> dict[str, Any]:
        calls.setdefault("europe_pmc", []).append(args)
        await http.get("europe_pmc", "search")
        is_exact = args["searched_scope"] == "exact_identifier"
        exact_value = (
            args["compiled_query"].split(":", 1)[1].strip('"')
            if is_exact
            else None
        )
        alias_kind = None
        alias_value = None
        if is_exact and args["compiled_query"].startswith("PMCID:"):
            collection = "PMC"
            source_id = exact_value
        else:
            collection = args["source_collections"][0] if is_exact else "MED"
            source_id = exact_value or "12345"
        item = epmc_card(
            args["start_position"],
            collection=collection,
            source_id=source_id,
            abstract=(
                "Complete bounded abstract."
                if is_exact and args["include_abstract"]
                else ("Bounded search excerpt." if not is_exact else None)
            ),
            alias_kind=alias_kind,
            alias_value=alias_value,
        )
        if is_exact:
            item["contributors"] = ["A", "B", "C", "D", "E"]
            item["contributor_count"] = 5
            item["abstract_state"] = (
                "represented"
                if args["include_abstract"]
                else "not_requested"
            )
            if args["compiled_query"].startswith("PMCID:"):
                item["represented_identifiers"] = [item["identity"]]
        return {
            "provider": "europe_pmc",
            "stages": represented_stages(),
            "candidates": [item],
            "count_observation": {
                "hit_count": 1 if is_exact else 2,
                "returned_result_count": 1,
            },
            "continuation_observation": {
                "state": "exhausted" if is_exact else "available",
                "next_cursor": None if is_exact else "cursor-two",
            },
            "coverage": {"scope": "fixture", "returned": 1},
            "failures": [],
            "warnings": [],
            "currentness": currentness(),
        }

    server.pubmed.map_publications = map_publications
    server.pubmed.qualify_publication = qualify_publication
    server.europe_pmc.map_records = map_records


def check_tool_surface() -> None:
    tools = server.mcp._tool_manager.list_tools()
    check(
        {tool.name for tool in tools} == {"literature_search", "literature_get", "literature_links", "literature_read"},
        "the MCP surface must contain search, get, links and read",
    )
    check(
        all(
            tool.parameters.get("additionalProperties") is False
            for tool in tools
        ),
        "real MCP tool schemas must forbid unknown arguments",
    )
    raw_description = next(
        tool.description for tool in tools if tool.name == "literature_search"
    )
    description = " ".join(raw_description.split())
    check(
        "PubMed search cards do not contain abstract excerpts" in description
        and "Europe PMC search cards may contain a bounded abstract excerpt"
        in description,
        "literature_search description obscures provider-specific excerpts",
    )


def check_real_mcp_argument_validation(
    calls: dict[str, list[dict[str, Any]]],
) -> None:
    before = sum(len(value) for value in calls.values())
    cases = (
        (
            "literature_search",
            {"source": "pubmed", "query": "public", "limit": True},
        ),
        (
            "literature_search",
            {"source": "pubmed", "query": "public", "limti": 1},
        ),
        (
            "literature_get",
            {
                "source": "pubmed",
                "identifier": "12345",
                "include_abstract": 1,
            },
        ),
        (
            "literature_get",
            {
                "source": "pubmed",
                "identifier": "12345",
                "include_abtract": False,
            },
        ),
    )
    for tool_name, arguments in cases:
        tool = server.mcp._tool_manager.get_tool(tool_name)
        try:
            run(tool.run(arguments))
        except ToolError:
            check(True, f"{tool_name} rejected invalid MCP arguments")
        else:
            check(False, f"{tool_name} accepted invalid MCP arguments")
    check(
        sum(len(value) for value in calls.values()) == before,
        "invalid real-MCP arguments reached an adapter",
    )


def check_pubmed_search_and_continuation(
    calls: dict[str, list[dict[str, Any]]],
    http: DummyHttp,
) -> None:
    first = run(
        server.literature_search(
            "pubmed",
            '"target mediated clearance" AND variability',
            limit=1,
        )
    )
    check(first["state"] == "success", "PubMed search should succeed")
    check(first["source"] == "pubmed", "PubMed source changed")
    check(len(first["records"]) == 1, "PubMed result card missing")
    check(
        first["records"][0]["record_url"].endswith("/12000/"),
        "PubMed record URL missing",
    )
    check(
        first["records"][0]["abstract_excerpt"] is None,
        "PubMed ESummary search card unexpectedly promised an excerpt",
    )
    check(
        "coverage" in first
        and "currentness" in first
        and "continuation" in first
        and "retrieval_scope" not in first,
        "search-success envelope fields changed",
    )
    check(
        any(
            item["code"] == "external_content_untrusted"
            for item in first["warnings"]
        ),
        "search result omitted the untrusted-content warning",
    )
    check(
        "qualification" not in first["records"][0],
        "removed transition state leaked into a search card",
    )
    check(
        first["continuation"]["available"],
        "PubMed continuation should be available",
    )
    check(
        first["disclosure"]["outbound_calls"] == 2,
        "PubMed search must count both provider calls",
    )
    check(
        calls["pubmed_search"][0]["compiled_query"]
        == '("target mediated clearance"[Title/Abstract] AND variability[Title/Abstract])',
        "PubMed query compiler changed",
    )

    second = run(
        server.literature_search(
            "pubmed",
            '"target mediated clearance" AND variability',
            limit=1,
            continuation=first["continuation"]["token"],
        )
    )
    check(
        calls["pubmed_search"][1]["offset"] == 1,
        "PubMed continuation did not restore the next offset",
    )
    check(
        second["records"][0]["position"] == 1,
        "PubMed continuation position changed",
    )

    before = len(calls["pubmed_search"])
    rejected = run(
        server.literature_search(
            "pubmed",
            "a different query",
            limit=1,
            continuation=first["continuation"]["token"],
        )
    )
    check(
        rejected["failures"][0]["code"] == "invalid_request",
        "mismatched continuation must be rejected",
    )
    check(
        len(calls["pubmed_search"]) == before,
        "rejected continuation must not reach the adapter",
    )
    for changed_request in (
        {
            "source": "pubmed",
            "query": '"target mediated clearance" AND variability',
            "target": "title",
            "limit": 1,
        },
        {
            "source": "pubmed",
            "query": '"target mediated clearance" AND variability',
            "order": "newest",
            "limit": 1,
        },
        {
            "source": "pubmed",
            "query": '"target mediated clearance" AND variability',
            "limit": 2,
        },
        {
            "source": "europe_pmc",
            "query": '"target mediated clearance" AND variability',
            "limit": 1,
        },
    ):
        rejected = run(
            server.literature_search(
                **changed_request,
                continuation=first["continuation"]["token"],
            )
        )
        check(
            rejected["failures"][0]["code"] == "invalid_request",
            "continuation binding did not reject a changed request field",
        )
    check(
        sum(len(value) for value in calls.values()) == before,
        "changed continuation binding reached an adapter",
    )

    original_key = server._CONTINUATION_KEY
    server._CONTINUATION_KEY = b"x" * 32
    try:
        rejected = run(
            server.literature_search(
                "pubmed",
                '"target mediated clearance" AND variability',
                limit=1,
                continuation=first["continuation"]["token"],
            )
        )
    finally:
        server._CONTINUATION_KEY = original_key
    check(
        rejected["failures"][0]["code"] == "invalid_request",
        "a process-key change did not invalidate the continuation",
    )
    check(
        len(calls["pubmed_search"]) == before,
        "restart-invalidated continuation reached the adapter",
    )

    prefix, payload, signature = first["continuation"]["token"].split(".")
    changed = "A" if payload[0] != "A" else "B"
    tampered = f"{prefix}.{changed}{payload[1:]}.{signature}"
    rejected = run(
        server.literature_search(
            "pubmed",
            '"target mediated clearance" AND variability',
            limit=1,
            continuation=tampered,
        )
    )
    check(
        rejected["failures"][0]["code"] == "invalid_request",
        "tampered continuation must be rejected",
    )
    check(
        len(calls["pubmed_search"]) == before,
        "tampered continuation must not reach the adapter",
    )
    check(len(http.calls) == 4, "unexpected PubMed provider call count")


def check_europe_pmc_search_and_get(
    calls: dict[str, list[dict[str, Any]]],
) -> None:
    first = run(
        server.literature_search(
            "europe_pmc",
            "exposure AND response",
            target="title",
            limit=1,
        )
    )
    call = calls["europe_pmc"][0]
    check(
        call["source_collections"] == ["MED", "PMC"],
        "Europe PMC search must exclude preprint and patent collections",
    )
    check(
        call["compiled_query"]
        == "(TITLE:exposure AND TITLE:response)",
        "Europe PMC query compiler changed",
    )
    check(
        first["source_scope"].startswith("Europe PMC MED and PMC collections")
        and "publication status must be examined per record" in first["source_scope"],
        "Europe PMC scope is not explicit",
    )
    check(
        first["records"][0]["record_url"].endswith("/MED/12345"),
        "Europe PMC search card URL missing",
    )
    check(
        first["records"][0]["abstract_excerpt"]
        == "Bounded search excerpt.",
        "Europe PMC bounded search excerpt missing",
    )
    check(
        first["continuation"]["available"],
        "Europe PMC continuation should be available",
    )

    run(
        server.literature_search(
            "europe_pmc",
            "exposure AND response",
            target="title",
            limit=1,
            continuation=first["continuation"]["token"],
        )
    )
    check(
        calls["europe_pmc"][1]["cursor"] == "cursor-two",
        "Europe PMC continuation did not restore its cursor",
    )
    check(
        calls["europe_pmc"][1]["start_position"] == 1,
        "Europe PMC continuation did not restore its absolute position",
    )

    exact = run(
        server.literature_get(
            "europe_pmc",
            "MED:12345",
        )
    )
    exact_call = calls["europe_pmc"][2]
    check(
        exact_call["source_collections"] == ["MED"],
        "Europe PMC get crossed collection boundaries",
    )
    check(
        exact_call["compiled_query"] == "EXT_ID:12345",
        "Europe PMC exact query changed",
    )
    check(
        exact_call["include_abstract"] is True,
        "Europe PMC get did not request its represented abstract",
    )
    check(
        exact_call["exact_record"] is True,
        "Europe PMC get did not request complete exact-record metadata",
    )
    check(
        exact["record"]["abstract"]["text"] == "Complete bounded abstract.",
        "Europe PMC exact abstract missing",
    )
    check(
        exact["record"]["record_url"].endswith("/MED/12345"),
        "Europe PMC record URL missing",
    )
    check(
        set(exact["record"]["container"]) == {"kind", "title", "publisher"}
        and set(exact["record"]["dates"][0])
        == {"role", "value", "source", "state"},
        "Europe PMC exact metadata does not use the common public shape",
    )
    check(
        set(exact["record"]["integrity"]) == {"state", "signals"}
        and set(exact["record"]["access"]) == {"state", "signals"},
        "Europe PMC integrity/access shape changed",
    )
    check(
        "currentness" in exact
        and "retrieval_scope" in exact
        and "coverage" not in exact
        and "continuation" not in exact,
        "get-success envelope fields changed",
    )

    pmc = run(server.literature_get("europe_pmc", "pmc7654321", False))
    check(
        calls["europe_pmc"][3]["source_collections"] == ["MED", "PMC"],
        "bare PMCID did not search both publication collections",
    )
    check(
        calls["europe_pmc"][3]["compiled_query"]
        == 'PMCID:"PMC7654321"',
        "bare PMCID did not use the exact PMCID field",
    )
    check(
        pmc["record"]["abstract"]["state"] == "not_requested"
        and pmc["record"]["abstract"]["text"] is None,
        "include_abstract=false should omit the abstract",
    )
    check(
        len(pmc["record"]["authors"]) == 5,
        "include_abstract=false truncated exact-record author metadata",
    )
    check(
        pmc["record"]["identity"]
        == {
            "kind": "europe_pmc",
            "collection": "PMC",
            "value": "PMC7654321",
        },
        "bare PMCID did not match the canonical PMC identity",
    )
    explicit_pmc = run(
        server.literature_get("europe_pmc", "PMC:PMC7654321", False)
    )
    explicit_pmc_call = calls["europe_pmc"][4]
    check(
        explicit_pmc_call["source_collections"] == ["PMC"]
        and explicit_pmc_call["compiled_query"] == "EXT_ID:PMC7654321"
        and explicit_pmc["record"]["identity"]
        == {
            "kind": "europe_pmc",
            "collection": "PMC",
            "value": "PMC7654321",
        },
        "explicit PMC identity did not use an unquoted collection-scoped EXT_ID",
    )


def check_pubmed_get(calls: dict[str, list[dict[str, Any]]]) -> None:
    result = run(server.literature_get("pubmed", "12345"))
    check(result["state"] == "success", "PubMed get should succeed")
    check(
        calls["pubmed_get"][0]["projection"] == "standard",
        "PubMed get should request standard metadata and abstract",
    )
    check(
        result["record"]["abstract"]["text"] == "Results: Exact abstract.",
        "PubMed abstract was not normalized",
    )
    check(
        result["record"]["identifiers"]
        == [
            {"kind": "pmid", "value": "12345"},
            {"kind": "doi", "value": "10.1000/example"},
        ],
        "PubMed exact identifiers are not flat identity objects",
    )
    check(
        result["record"]["container"]
        == {
            "kind": "book",
            "title": "A Test Book",
            "publisher": "Test Publisher",
        },
        "PubMed book container metadata was not preserved",
    )
    check(
        set(result["record"]["dates"][0])
        == {"role", "value", "source", "state"},
        "PubMed dates do not use the common public shape",
    )
    check(
        set(result["record"]["integrity"]) == {"state", "signals"}
        and set(result["record"]["access"]) == {"state", "signals"},
        "PubMed integrity/access shape differs from Europe PMC",
    )
    check(
        not any(
            item.get("kind") == "direct_relationship_count"
            for item in result["record"]["integrity"]["signals"]
        ),
        "legacy relationship counts leaked through integrity metadata",
    )
    check(
        any(
            item["code"] == "external_content_untrusted"
            for item in result["warnings"]
        ),
        "exact result omitted the untrusted-content warning",
    )
    check(
        "content_manifest" not in result["record"],
        "full-text transition metadata must not leak into the slim tool",
    )
    check(
        "transitions" not in result["record"],
        "old transition contracts must not leak into the slim tool",
    )
    check(
        "relationships" not in result["record"],
        "legacy relationship/transition state leaked into literature_get",
    )
    check(
        "direct_relationships" not in result["record"],
        "legacy direct-relationship state leaked into literature_get",
    )
    check(
        result["record"]["record_url"].endswith("/12345/"),
        "PubMed exact URL missing",
    )

    no_abstract = run(server.literature_get("pubmed", "12345", False))
    check(
        calls["pubmed_get"][1]["projection"] == "summary",
        "PubMed summary projection changed",
    )
    check(
        no_abstract["retrieval_scope"].endswith(
            "no article full text is retrieved"
        ),
        "retrieval boundary is not explicit",
    )


def check_rejections(calls: dict[str, list[dict[str, Any]]]) -> None:
    before = sum(len(value) for value in calls.values())
    blocked = run(
        server.literature_search(
            "pubmed",
            (
                "literature on experiment EXP22000794 and our "
                "internal batch number 4471727-A"
            ),
        )
    )
    check(
        blocked["state"] == "blocked",
        "disclosure match should block the operation",
    )
    check(
        blocked["failures"][0]["code"] == "disclosure_blocked",
        "disclosure failure code changed",
    )
    check(
        blocked["failures"][0]["absence"] is False,
        "disclosure rejection became established absence",
    )
    check(
        "coverage" not in blocked
        and "currentness" not in blocked
        and "retrieval_scope" not in blocked,
        "error envelope became success-shaped",
    )
    check(
        blocked["disclosure"]["outbound_calls"] == 0,
        "blocked disclosure must not reach a provider",
    )
    check(
        blocked["disclosure"]["blocked_rule_ids"]
        == ["batch-lot-id", "internal-experiment-id"]
        and "EXP22000794" not in str(blocked["disclosure"])
        and "4471727-A" not in str(blocked["disclosure"]),
        "blocked disclosure omitted safe categories or leaked matched values",
    )
    check(
        "public scientific concepts" in blocked["failures"][0]["detail"]
        and "not obfuscate or encode" in blocked["failures"][0]["detail"],
        "blocked disclosure omitted safe reformulation guidance",
    )

    previous = os.environ.get("ICS_DISCLOSURE_DENY_FILE")
    with tempfile.TemporaryDirectory() as directory:
        policy = Path(directory) / "block-valid-identifier.txt"
        policy.write_text("^12345$", encoding="utf-8")
        os.environ["ICS_DISCLOSURE_DENY_FILE"] = str(policy)
        try:
            blocked_get = run(server.literature_get("pubmed", "12345"))
        finally:
            if previous is None:
                os.environ.pop("ICS_DISCLOSURE_DENY_FILE", None)
            else:
                os.environ["ICS_DISCLOSURE_DENY_FILE"] = previous
    check(
        blocked_get["state"] == "blocked"
        and blocked_get["disclosure"]["outbound_calls"] == 0
        and blocked_get["disclosure"]["blocked_rule_ids"]
        == ["operator-rule-1"],
        "operator-blocked exact get lost its safe category or made a call",
    )
    check(
        "literature_search" in blocked_get["failures"][0]["detail"]
        and "public scientific concepts" in blocked_get["failures"][0]["detail"]
        and "not obfuscate or encode" in blocked_get["failures"][0]["detail"],
        "blocked exact get omitted concept-based search guidance",
    )

    invalid_query = run(
        server.literature_search("pubmed", "title:provider-syntax")
    )
    check(
        invalid_query["failures"][0]["stage"] == "validation"
        and invalid_query["disclosure"]["blocked_rule_ids"] == [],
        "provider syntax must be rejected before disclosure",
    )
    invalid_id = run(server.literature_get("pubmed", "PMC123"))
    check(
        invalid_id["failures"][0]["code"] == "invalid_identifier",
        "PubMed must reject a PMCID",
    )
    preprint_id = run(server.literature_get("europe_pmc", "PPR:123"))
    check(
        preprint_id["failures"][0]["code"] == "invalid_identifier",
        "Europe PMC must reject preprint collection identifiers",
    )
    after = sum(len(value) for value in calls.values())
    check(before == after, "rejected operations reached an adapter")

    previous = os.environ.get("ICS_DISCLOSURE_DENY_FILE")
    with tempfile.TemporaryDirectory() as directory:
        policy = Path(directory) / "invalid-policy.txt"
        policy.write_bytes(b"\xff\xfe")
        os.environ["ICS_DISCLOSURE_DENY_FILE"] = str(policy)
        try:
            unavailable = run(
                server.literature_search("pubmed", "public mechanism")
            )
        finally:
            if previous is None:
                os.environ.pop("ICS_DISCLOSURE_DENY_FILE", None)
            else:
                os.environ["ICS_DISCLOSURE_DENY_FILE"] = previous
    check(
        unavailable["state"] == "failed"
        and unavailable["failures"][0]["code"]
        == "configuration_unavailable",
        "non-UTF-8 disclosure policy bypassed structured failure",
    )
    check(
        unavailable["disclosure"]["outbound_calls"] == 0,
        "malformed disclosure policy reached a provider",
    )
    check(
        sum(len(value) for value in calls.values()) == after,
        "malformed disclosure policy reached an adapter",
    )

    for unsafe_pattern, safe_query in (
        ("(" * 2_000 + "x" + ")" * 2_000, "public mechanism"),
        ("(a*)(a*)(a*)(a*)(a*)Z", "public mechanism"),
        ("a*Z", "a" * 1_999),
    ):
        with tempfile.TemporaryDirectory() as directory:
            policy = Path(directory) / "unsafe-policy.txt"
            policy.write_text(unsafe_pattern, encoding="utf-8")
            os.environ["ICS_DISCLOSURE_DENY_FILE"] = str(policy)
            try:
                unavailable = run(
                    server.literature_search("pubmed", safe_query)
                )
            finally:
                if previous is None:
                    os.environ.pop("ICS_DISCLOSURE_DENY_FILE", None)
                else:
                    os.environ["ICS_DISCLOSURE_DENY_FILE"] = previous
        check(
            unavailable["state"] == "failed"
            and unavailable["failures"][0]["code"]
            == "configuration_unavailable",
            "unsafe disclosure regex bypassed structured failure",
        )
        check(
            unavailable["disclosure"]["outbound_calls"] == 0
            and sum(len(value) for value in calls.values()) == after,
            "unsafe disclosure regex reached a provider",
        )


def check_provider_failure_passthrough(
    calls: dict[str, list[dict[str, Any]]],
) -> None:
    original = server.pubmed.map_publications

    async def failed_map(http: Any, **args: Any) -> dict[str, Any]:
        calls.setdefault("failed", []).append(args)
        await http.get("pubmed", "esearch.fcgi")
        return {
            "provider": "pubmed",
            "stages": represented_stages("failed"),
            "candidates": [],
            "count_observation": None,
            "continuation_observation": {
                "state": "available",
                "next_offset": 10,
            },
            "coverage": None,
            "failures": [
                failure(
                    "provider_error_body",
                    "provider_body",
                    "pubmed.esearch",
                    "fixture provider error",
                )
            ],
            "warnings": [],
            "currentness": currentness(),
        }

    server.pubmed.map_publications = failed_map
    result = run(server.literature_search("pubmed", "public term"))
    check(result["state"] == "failed", "provider failure became success-shaped")
    check(
        result["failures"][0]["code"] == "provider_error_body",
        "provider failure was not preserved",
    )
    check(
        result["failures"][0]["absence"] is False
        and "coverage" in result
        and "currentness" in result
        and "continuation" in result,
        "validated search-provider failure lost its outcome fields",
    )
    check(
        result["continuation"]["available"] is False,
        "a failed adapter result minted a continuation",
    )
    server.pubmed.map_publications = original


def check_timeout_nonabsence() -> None:
    original = server.pubmed.map_publications

    async def timed_out_map(http: Any, **_: Any) -> dict[str, Any]:
        raise TimeoutError

    server.pubmed.map_publications = timed_out_map
    try:
        result = run(server.literature_search("pubmed", "public term"))
    finally:
        server.pubmed.map_publications = original
    check(
        result["state"] == "failed"
        and result["failures"][0]["code"] == "timeout"
        and result["failures"][0]["absence"] is False
        and "coverage" not in result
        and "currentness" not in result,
        "search timeout became absence or gained provider-outcome fields",
    )

    def provider_timeout() -> HttpResult:
        return HttpResult(
            status=0,
            body=b"",
            content_type="",
            headers={},
            network_bytes=0,
            failure_code="timeout",
            failure_detail="provider request timed out",
        )

    original_runtime = server._RUNTIME
    server._RUNTIME = RuntimeRegistry(http=FixtureHttp(provider_timeout()))
    server.pubmed.map_publications = REAL_PUBMED_MAP
    try:
        provider_search = run(
            server.literature_search("pubmed", "public term")
        )
    finally:
        server.pubmed.map_publications = original
        server._RUNTIME = original_runtime
    check(
        provider_search["state"] == "failed"
        and provider_search["failures"][0]["code"] == "timeout"
        and provider_search["failures"][0]["absence"] is False
        and {
            "records",
            "count",
            "coverage",
            "continuation",
            "currentness",
            "source_scope",
        }.issubset(provider_search),
        "provider search timeout lost its validated search-outcome fields",
    )

    original_runtime = server._RUNTIME
    original_get = server.pubmed.qualify_publication
    server._RUNTIME = RuntimeRegistry(http=FixtureHttp(provider_timeout()))
    server.pubmed.qualify_publication = REAL_PUBMED_GET
    try:
        provider_get = run(server.literature_get("pubmed", "12345"))
    finally:
        server.pubmed.qualify_publication = original_get
        server._RUNTIME = original_runtime
    check(
        provider_get["state"] == "failed"
        and provider_get["failures"][0]["code"] == "timeout"
        and provider_get["failures"][0]["absence"] is False
        and {"record", "currentness", "retrieval_scope"}.issubset(
            provider_get
        )
        and "coverage" not in provider_get
        and "continuation" not in provider_get,
        "provider exact-get timeout lost its validated get-outcome fields",
    )


def check_adapter_outcome_validation() -> None:
    original_map = server.pubmed.map_publications
    original_get = server.pubmed.qualify_publication
    original_epmc = server.europe_pmc.map_records

    async def malformed_map(http: Any, **_: Any) -> dict[str, Any]:
        await http.get("pubmed", "esearch.fcgi")
        malformed_stages = represented_stages()
        malformed_stages["transport"] = "unknown_state"
        return {
            "provider": "pubmed",
            "stages": malformed_stages,
            "candidates": [pubmed_card(0)],
            "count_observation": {
                "provider_count": 1,
                "returned_id_count": 1,
            },
            "continuation_observation": {
                "state": "exhausted",
                "next_offset": None,
            },
            "coverage": {"scope": "fixture", "returned": 1},
            "failures": [],
            "warnings": [],
            "currentness": currentness(),
        }

    server.pubmed.map_publications = malformed_map
    try:
        result = run(server.literature_search("pubmed", "public term"))
    finally:
        server.pubmed.map_publications = original_map
    check(
        result["state"] == "failed"
        and result["failures"][0]["code"] == "provider_schema_drift",
        "malformed adapter stage metadata escaped server validation",
    )

    async def malformed_candidate_map(
        http: Any,
        **_: Any,
    ) -> dict[str, Any]:
        await http.get("pubmed", "esearch.fcgi")
        return {
            "provider": "pubmed",
            "stages": represented_stages(),
            "candidates": [None],
            "count_observation": {
                "provider_count": 1,
                "returned_id_count": 1,
            },
            "continuation_observation": {
                "state": "exhausted",
                "next_offset": None,
            },
            "coverage": {"scope": "fixture", "returned": 1},
            "failures": [],
            "warnings": [],
            "currentness": currentness(),
        }

    server.pubmed.map_publications = malformed_candidate_map
    try:
        result = run(server.literature_search("pubmed", "public term"))
    finally:
        server.pubmed.map_publications = original_map
    check(
        result["state"] == "failed"
        and result["failures"][0]["code"] == "provider_schema_drift",
        "a non-object adapter candidate became a successful empty page",
    )

    async def wrong_provider_map(
        http: Any,
        **_: Any,
    ) -> dict[str, Any]:
        await http.get("pubmed", "esearch.fcgi")
        return {
            "provider": "europe_pmc",
            "stages": represented_stages(),
            "candidates": [pubmed_card(0)],
            "count_observation": {
                "provider_count": 1,
                "returned_id_count": 1,
            },
            "continuation_observation": {
                "state": "exhausted",
                "next_offset": None,
            },
            "coverage": {"scope": "fixture", "returned": 1},
            "failures": [],
            "warnings": [],
            "currentness": currentness(),
        }

    server.pubmed.map_publications = wrong_provider_map
    try:
        result = run(server.literature_search("pubmed", "public term"))
    finally:
        server.pubmed.map_publications = original_map
    check(
        result["state"] == "failed"
        and result["failures"][0]["code"] == "provider_schema_drift",
        "a search outcome from the wrong adapter provider was accepted",
    )

    async def invalid_pubmed_identity_map(
        http: Any,
        **_: Any,
    ) -> dict[str, Any]:
        await http.get("pubmed", "esearch.fcgi")
        item = pubmed_card(0)
        item["identity"] = {"kind": "pmid", "value": "PMC123"}
        return {
            "provider": "pubmed",
            "stages": represented_stages(),
            "candidates": [item],
            "count_observation": {
                "provider_count": 1,
                "returned_id_count": 1,
            },
            "continuation_observation": {
                "state": "exhausted",
                "next_offset": None,
            },
            "coverage": {"scope": "fixture", "returned": 1},
            "failures": [],
            "warnings": [],
            "currentness": currentness(),
        }

    server.pubmed.map_publications = invalid_pubmed_identity_map
    try:
        result = run(server.literature_search("pubmed", "public term"))
    finally:
        server.pubmed.map_publications = original_map
    check(
        result["state"] == "failed"
        and result["failures"][0]["code"] == "provider_schema_drift",
        "an invalid PubMed candidate identity reached public projection",
    )

    async def invalid_epmc_identity_map(
        http: Any,
        **_: Any,
    ) -> dict[str, Any]:
        await http.get("europe_pmc", "search")
        item = epmc_card(
            0,
            collection="PMC",
            source_id="PMC123",
        )
        item["identity"] = {
            "kind": "europe_pmc",
            "collection": "PMC",
            "value": "123",
        }
        return {
            "provider": "europe_pmc",
            "stages": represented_stages(),
            "candidates": [item],
            "count_observation": {
                "hit_count": 1,
                "returned_result_count": 1,
            },
            "continuation_observation": {
                "state": "exhausted",
                "next_cursor": None,
            },
            "coverage": {"scope": "fixture", "returned": 1},
            "failures": [],
            "warnings": [],
            "currentness": currentness(),
        }

    server.europe_pmc.map_records = invalid_epmc_identity_map
    try:
        result = run(
            server.literature_search(
                "europe_pmc",
                "public term",
            )
        )
    finally:
        server.europe_pmc.map_records = original_epmc
    check(
        result["state"] == "failed"
        and result["failures"][0]["code"] == "provider_schema_drift",
        "an invalid Europe PMC candidate identity reached public projection",
    )

    async def malformed_get(http: Any, **_: Any) -> dict[str, Any]:
        await http.get("pubmed", "efetch.fcgi")
        return {
            "provider": "pubmed",
            "stages": represented_stages(),
            "record": {},
            "failures": [],
            "warnings": [],
            "currentness": currentness(),
        }

    server.pubmed.qualify_publication = malformed_get
    try:
        result = run(server.literature_get("pubmed", "12345"))
    finally:
        server.pubmed.qualify_publication = original_get
    check(
        result["state"] == "failed"
        and result["failures"][0]["code"] == "provider_schema_drift",
        "an empty successful exact record escaped identity validation",
    )

    for provider, identity, message in (
        (
            "europe_pmc",
            {"kind": "pmid", "value": "12345"},
            "a PubMed exact outcome from the wrong provider was accepted",
        ),
        (
            "pubmed",
            {"kind": "pmid", "value": "99999"},
            "a PubMed exact outcome with the wrong canonical PMID was accepted",
        ),
    ):
        async def invalid_get(
            http: Any,
            *,
            _provider: str = provider,
            _identity: dict[str, str] = identity,
            **_: Any,
        ) -> dict[str, Any]:
            await http.get("pubmed", "efetch.fcgi")
            return {
                "provider": _provider,
                "stages": represented_stages(),
                "record": {"canonical_identity": _identity},
                "failures": [],
                "warnings": [],
                "currentness": currentness(),
            }

        server.pubmed.qualify_publication = invalid_get
        try:
            result = run(server.literature_get("pubmed", "12345"))
        finally:
            server.pubmed.qualify_publication = original_get
        check(
            result["state"] == "failed"
            and result["failures"][0]["code"] == "provider_schema_drift",
            message,
        )


def check_pagination_safety() -> None:
    original = server.europe_pmc.map_records

    async def oversized_cursor(http: Any, **args: Any) -> dict[str, Any]:
        await http.get("europe_pmc", "search")
        return {
            "provider": "europe_pmc",
            "stages": represented_stages(),
            "candidates": [epmc_card(args["start_position"])],
            "count_observation": {
                "hit_count": 10,
                "returned_result_count": 1,
            },
            "continuation_observation": {
                "state": "available",
                "next_cursor": "é" * 4_096,
            },
            "coverage": {"scope": "fixture", "returned": 1},
            "failures": [],
            "warnings": [],
            "currentness": currentness(),
        }

    server.europe_pmc.map_records = oversized_cursor
    try:
        result = run(
            server.literature_search(
                "europe_pmc",
                "public term",
                limit=1,
            )
        )
    finally:
        server.europe_pmc.map_records = original
    check(
        result["state"] == "partial_success"
        and result["continuation"]["available"] is False,
        "an oversized encoded cursor produced a success-shaped continuation",
    )
    check(
        any(item["code"] == "pagination_boundary" for item in result["failures"]),
        "an oversized encoded cursor omitted its pagination failure",
    )


def check_opaque_cursor_round_trip() -> None:
    original = server.europe_pmc.map_records
    seen: list[str] = []
    opaque_cursor = " cursor-two "

    async def opaque_cursor_page(http: Any, **args: Any) -> dict[str, Any]:
        await http.get("europe_pmc", "search")
        seen.append(args["cursor"])
        position = args["start_position"]
        return {
            "provider": "europe_pmc",
            "stages": represented_stages(),
            "candidates": [epmc_card(position)],
            "count_observation": {
                "hit_count": 2,
                "returned_result_count": 1,
            },
            "continuation_observation": {
                "state": "available" if position == 0 else "exhausted",
                "next_cursor": opaque_cursor if position == 0 else None,
            },
            "coverage": {"scope": "fixture", "returned": 1},
            "failures": [],
            "warnings": [],
            "currentness": currentness(),
        }

    server.europe_pmc.map_records = opaque_cursor_page
    try:
        first = run(
            server.literature_search(
                "europe_pmc",
                "public cursor",
                limit=1,
            )
        )
        second = run(
            server.literature_search(
                "europe_pmc",
                "public cursor",
                limit=1,
                continuation=first["continuation"]["token"],
            )
        )
    finally:
        server.europe_pmc.map_records = original
    check(
        first["continuation"]["available"] is True
        and second["state"] == "success"
        and seen == ["*", opaque_cursor],
        "an opaque Europe PMC cursor did not round-trip through continuation",
    )


def check_identity_mismatch_semantics() -> None:
    original = server.europe_pmc.map_records

    async def mismatched_record(http: Any, **_: Any) -> dict[str, Any]:
        await http.get("europe_pmc", "search")
        item = epmc_card(0, collection="MED", source_id="12345")
        item["represented_identifiers"] = [item["identity"]]
        return {
            "provider": "europe_pmc",
            "stages": represented_stages(),
            "candidates": [item],
            "count_observation": {
                "hit_count": 1,
                "returned_result_count": 1,
            },
            "continuation_observation": {"state": "exhausted"},
            "coverage": {"scope": "fixture", "returned": 1},
            "failures": [],
            "warnings": [],
            "currentness": currentness(),
        }

    server.europe_pmc.map_records = mismatched_record
    try:
        result = run(
            server.literature_get("europe_pmc", "PMC7654321")
        )
    finally:
        server.europe_pmc.map_records = original
    check(
        result["failures"][0]["code"] == "identity_mismatch"
        and result["failures"][0]["absence"] is False,
        "nonzero Europe PMC identity mismatches were reported as absence",
    )

    for continuation_state in ("available", "unavailable"):
        async def incomplete_exact(
            http: Any,
            **_: Any,
        ) -> dict[str, Any]:
            await http.get("europe_pmc", "search")
            item = epmc_card(
                0,
                collection="PMC",
                source_id="PMC7654321",
            )
            return {
                "provider": "europe_pmc",
                "stages": represented_stages(),
                "candidates": [item],
                "count_observation": {
                    "hit_count": 2,
                    "returned_result_count": 1,
                },
                "continuation_observation": {
                    "state": continuation_state,
                    "next_cursor": (
                        "cursor-two"
                        if continuation_state == "available"
                        else None
                    ),
                },
                "coverage": {"scope": "fixture", "returned": 1},
                "failures": (
                    []
                    if continuation_state == "available"
                    else [
                        failure(
                            "pagination_boundary",
                            "provider_body",
                            "europe_pmc.nextCursorMark",
                            "fixture pagination loss",
                        )
                    ]
                ),
                "warnings": [],
                "currentness": currentness(),
            }

        server.europe_pmc.map_records = incomplete_exact
        try:
            result = run(
                server.literature_get(
                    "europe_pmc",
                    "PMC7654321",
                )
            )
        finally:
            server.europe_pmc.map_records = original
        check(
            result["state"] == "inconsistent"
            and result["record"] is None
            and any(
                item["scope"] == "europe_pmc.exact_population"
                for item in result["failures"]
            ),
            (
                "Europe PMC exact retrieval accepted an incomplete "
                f"{continuation_state} population"
            ),
        )


def check_public_abstract_budget() -> None:
    children = []
    for _ in range(100):
        children.append(
            {
                "facts": [
                    {"name": "label", "value": "L" * 200},
                    {"name": "text", "value": "T" * 200},
                    {"name": "truncated", "value": False},
                ]
            }
        )
    original = server.pubmed.qualify_publication

    async def oversized_abstract(http: Any, **args: Any) -> dict[str, Any]:
        await http.get("pubmed", "efetch.fcgi")
        return {
            "provider": "pubmed",
            "stages": represented_stages(),
            "record": {
                "canonical_identity": {
                    "kind": "pmid",
                    "value": args["pmid"],
                },
                "aliases": [],
                "record_class": "publication",
                "title": "Bounded abstract",
                "contributors": {
                    "names": [],
                    "represented_count": 0,
                },
                "container": {
                    "kind": "journal",
                    "title": "Journal",
                    "publisher": None,
                },
                "dates": [],
                "extension": {
                    "abstracts": {
                        "state": "represented",
                        "items": [
                            {
                                "facts": [
                                    {"name": "truncated", "value": False}
                                ],
                                "children": children,
                            }
                        ],
                    }
                },
                "integrity": {"state": "unknown", "labels": [], "facts": []},
                "access": {"state": "unknown", "facts": []},
            },
            "failures": [],
            "warnings": [],
            "currentness": currentness(),
        }

    server.pubmed.qualify_publication = oversized_abstract
    try:
        result = run(server.literature_get("pubmed", "12345"))
    finally:
        server.pubmed.qualify_publication = original
    abstract = result["record"]["abstract"]
    check(
        len(abstract["text"]) <= 20_000
        and len(abstract["sections"]) <= 100
        and abstract["truncated"] is True,
        "the final public abstract exceeded its end-to-end budget",
    )

    exact_boundary = server._pubmed_abstract(
        {
            "extension": {
                "abstracts": {
                    "state": "represented",
                    "items": [
                        {
                            "facts": [
                                {"name": "truncated", "value": False}
                            ],
                            "children": [
                                {
                                    "facts": [
                                        {"name": "text", "value": "A" * 20_000},
                                        {"name": "truncated", "value": False},
                                    ]
                                }
                            ],
                        }
                    ],
                }
            }
        }
    )
    check(
        len(exact_boundary["text"]) == 20_000
        and exact_boundary["truncated"] is False
        and exact_boundary["sections"][0]["truncated"] is False,
        "an exactly full public abstract was falsely marked truncated",
    )

    label_limited = server._pubmed_abstract(
        {
            "extension": {
                "abstracts": {
                    "state": "represented",
                    "items": [
                        {
                            "facts": [
                                {"name": "truncated", "value": False}
                            ],
                            "children": [
                                {
                                    "facts": [
                                        {"name": "text", "value": "A" * 19_850},
                                        {"name": "truncated", "value": False},
                                    ]
                                },
                                {
                                    "facts": [
                                        {"name": "label", "value": "L" * 200},
                                        {"name": "text", "value": "tail"},
                                        {"name": "truncated", "value": False},
                                    ]
                                },
                            ],
                        }
                    ],
                }
            }
        }
    )
    check(
        label_limited["sections"][1]["label"] is None
        and label_limited["sections"][1]["truncated"] is True
        and label_limited["truncated"] is True,
        "an omitted abstract label was not represented as truncation",
    )

    tiny_sections = [
        {
            "facts": [
                {"name": "text", "value": "x"},
                {"name": "truncated", "value": False},
            ]
        }
        for _ in range(101)
    ]
    count_limited = server._pubmed_abstract(
        {
            "extension": {
                "abstracts": {
                    "state": "represented",
                    "items": [
                        {
                            "facts": [
                                {"name": "truncated", "value": False}
                            ],
                            "children": tiny_sections,
                        }
                    ],
                }
            }
        }
    )
    check(
        len(count_limited["sections"]) == 100
        and count_limited["truncated"] is True
        and len(count_limited["text"]) < 20_000,
        "the abstract section-count cap was not exercised independently",
    )


def check_public_parser_limit() -> None:
    nested_warning = b'{"child":' * 850 + b'"signal"' + b"}" * 850
    body = (
        b'{"esearchresult":{"count":"0","retstart":"0","retmax":"0",'
        b'"idlist":[],"querytranslation":"public mechanism",'
        b'"warninglist":'
        + nested_warning
        + b"}}"
    )
    fixture_http = FixtureHttp(
        HttpResult(
            status=200,
            body=body,
            content_type="application/json",
            headers={},
            network_bytes=len(body),
        )
    )
    original_runtime = server._RUNTIME
    original_map = server.pubmed.map_publications
    server._RUNTIME = RuntimeRegistry(http=fixture_http)
    server.pubmed.map_publications = REAL_PUBMED_MAP
    try:
        result = run(
            server.literature_search(
                "pubmed",
                "public mechanism",
                limit=1,
            )
        )
    finally:
        server.pubmed.map_publications = original_map
        server._RUNTIME = original_runtime
    check(
        result["state"] == "failed"
        and result["failures"][0]["code"] == "parser_limit_exceeded",
        "parseable deep PubMed JSON escaped the public tool boundary",
    )
    check(
        fixture_http.calls == 1
        and result["disclosure"]["outbound_calls"] == 1,
        "the public parser-limit failure misreported provider attempts",
    )


def check_public_provider_boundaries() -> None:
    comments = "".join(
        (
            '<CommentsCorrections RefType="CommentOn">'
            f"<PMID>{1_000 + index}</PMID>"
            "</CommentsCorrections>"
        )
        for index in range(201)
    )
    pubmed_body = f"""<?xml version="1.0"?>
<!DOCTYPE PubmedArticleSet PUBLIC "-//NLM//DTD PubMedArticle, 1st January 2025//EN" "https://dtd.nlm.nih.gov/ncbi/pubmed/out/pubmed_250101.dtd">
<PubmedArticleSet><PubmedArticle>
<MedlineCitation><PMID>12345</PMID><Article>
<Journal><Title>J Test</Title></Journal>
<ArticleTitle>Relationship-free public record</ArticleTitle>
<PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList>
</Article><CommentsCorrectionsList>{comments}</CommentsCorrectionsList></MedlineCitation>
<PubmedData><ArticleIdList><ArticleId IdType="pubmed">12345</ArticleId></ArticleIdList></PubmedData>
</PubmedArticle></PubmedArticleSet>""".encode("utf-8")
    original_runtime = server._RUNTIME
    original_get = server.pubmed.qualify_publication
    fixture_http = FixtureHttp(
        HttpResult(
            status=200,
            body=pubmed_body,
            content_type="application/xml",
            headers={},
            network_bytes=len(pubmed_body),
        )
    )
    server._RUNTIME = RuntimeRegistry(http=fixture_http)
    server.pubmed.qualify_publication = REAL_PUBMED_GET
    try:
        result = run(server.literature_get("pubmed", "12345"))
    finally:
        server.pubmed.qualify_publication = original_get
        server._RUNTIME = original_runtime
    check(
        result["state"] == "success"
        and result["failures"] == []
        and "relationships" not in result["record"]
        and "direct_relationships" not in result["record"],
        "removed PubMed relationships still affected the public exact record",
    )

    query = '"tarlatamab" "cytokine release syndrome"'
    compiled_pubmed_query = server.compile_boolean(
        server.parse_normal_query(query),
        provider="pubmed",
        target="title_abstract",
    )
    translated_query = compiled_pubmed_query[1:-1]
    esearch_body = json.dumps(
        {
            "esearchresult": {
                "count": "1",
                "retstart": "0",
                "retmax": "1",
                "idlist": ["111"],
                "querytranslation": translated_query,
            }
        }
    ).encode("utf-8")
    esummary_body = json.dumps(
        {
            "result": {
                "uids": ["111"],
                "111": {
                    "uid": "111",
                    "title": "Tarlatamab and cytokine release syndrome",
                    "fulljournalname": "J Test",
                    "pubdate": "2026",
                    "sortpubdate": "2026/01/01 00:00",
                    "authors": [{"name": "A Researcher"}],
                    "pubtype": ["Journal Article"],
                    "articleids": [
                        {"idtype": "pubmed", "value": "111"}
                    ],
                    "attributes": ["Has Abstract"],
                },
            }
        }
    ).encode("utf-8")
    fixture_http = FixtureHttp(
        [
            HttpResult(
                status=200,
                body=esearch_body,
                content_type="application/json",
                headers={},
                network_bytes=len(esearch_body),
            ),
            HttpResult(
                status=200,
                body=esummary_body,
                content_type="application/json",
                headers={},
                network_bytes=len(esummary_body),
            ),
        ]
    )
    original_runtime = server._RUNTIME
    original_map = server.pubmed.map_publications
    server._RUNTIME = RuntimeRegistry(http=fixture_http)
    server.pubmed.map_publications = REAL_PUBMED_MAP
    try:
        result = run(
            server.literature_search(
                "pubmed",
                query,
                limit=1,
            )
        )
    finally:
        server.pubmed.map_publications = original_map
        server._RUNTIME = original_runtime
    check(
        result["state"] == "success"
        and len(result["records"]) == 1
        and any(
            item["code"] == "provider_query_normalized"
            for item in result["warnings"]
        ),
        "scope-preserving PubMed normalization failed at the public tool",
    )

    compiled = server.compile_boolean(
        server.parse_normal_query("public"),
        provider="europe_pmc",
        target="title_abstract",
    )
    provider_query = f"({compiled}) AND (SRC:MED OR SRC:PMC)"
    exact_query = "EXT_ID:12345"
    exact_provider_query = f"({exact_query}) AND SRC:MED"
    query_fidelity_payload = {
        "version": "6.9",
        "hitCount": 1,
        "request": {
            "queryString": exact_provider_query,
            "cursorMark": "*",
            "pageSize": 2,
            "sort": "",
            "synonym": False,
        },
        "resultList": {
            "result": [
                {
                    "source": "PMC",
                    "id": "PMC7654321",
                    "pmcid": "PMC7654321",
                    "title": "Out-of-scope collection",
                }
            ]
        },
    }
    body = json.dumps(query_fidelity_payload).encode("utf-8")
    fixture_http = FixtureHttp(
        HttpResult(
            status=200,
            body=body,
            content_type="application/json",
            headers={},
            network_bytes=len(body),
        )
    )
    original_runtime = server._RUNTIME
    original_map = server.europe_pmc.map_records
    server._RUNTIME = RuntimeRegistry(http=fixture_http)
    server.europe_pmc.map_records = REAL_EUROPE_PMC_MAP
    try:
        result = run(
            server.literature_get(
                "europe_pmc",
                "MED:12345",
            )
        )
    finally:
        server.europe_pmc.map_records = original_map
        server._RUNTIME = original_runtime
    check(
        result["state"] == "failed"
        and result["failures"][0]["code"] == "query_fidelity_unknown"
        and result["failures"][0]["absence"] is False
        and "record" in result
        and "currentness" in result,
        "query-fidelity failure became absence or lost get-outcome fields",
    )

    for malformed_value in ([], {}):
        payload = {
            "version": "6.9",
            "hitCount": 1,
            "request": {
                "queryString": provider_query,
                "cursorMark": "*",
                "pageSize": 1,
                "sort": "",
                "synonym": False,
                "resultType": "core",
            },
            "resultList": {
                "result": [
                    {
                        "source": "MED",
                        "id": "12345",
                        "title": "Malformed access metadata",
                        "isOpenAccess": malformed_value,
                    }
                ]
            },
        }
        body = json.dumps(payload).encode("utf-8")
        fixture_http = FixtureHttp(
            HttpResult(
                status=200,
                body=body,
                content_type="application/json",
                headers={},
                network_bytes=len(body),
            )
        )
        original_runtime = server._RUNTIME
        original_map = server.europe_pmc.map_records
        server._RUNTIME = RuntimeRegistry(http=fixture_http)
        server.europe_pmc.map_records = REAL_EUROPE_PMC_MAP
        try:
            result = run(
                server.literature_search(
                    "europe_pmc",
                    "public",
                    limit=1,
                )
            )
        finally:
            server.europe_pmc.map_records = original_map
            server._RUNTIME = original_runtime
        check(
            result["state"] == "inconsistent"
            and result["failures"][0]["code"] == "provider_schema_drift",
            "malformed Europe PMC access metadata escaped the public tool",
        )

    exact_payload = {
        "version": "6.9",
        "hitCount": 1,
        "request": {
            "queryString": exact_provider_query,
            "cursorMark": "*",
            "pageSize": 2,
            "sort": "",
            "synonym": False,
        },
        "resultList": {
            "result": [
                {
                    "source": "MED",
                    "id": "12345",
                    "pmid": "12345",
                    "title": "Exact Europe PMC record",
                    "authorList": {
                        "author": [{"fullName": "A Researcher"}]
                    },
                    "journalInfo": {"journal": {"title": "J Test"}},
                    "pubYear": "2026",
                    "isOpenAccess": "N",
                }
            ]
        },
    }
    exact_body = json.dumps(exact_payload).encode("utf-8")
    fixture_http = FixtureHttp(
        HttpResult(
            status=200,
            body=exact_body,
            content_type="application/json",
            headers={},
            network_bytes=len(exact_body),
        )
    )
    original_runtime = server._RUNTIME
    original_map = server.europe_pmc.map_records
    server._RUNTIME = RuntimeRegistry(http=fixture_http)
    server.europe_pmc.map_records = REAL_EUROPE_PMC_MAP
    try:
        result = run(
            server.literature_get(
                "europe_pmc",
                "MED:12345",
            )
        )
    finally:
        server.europe_pmc.map_records = original_map
        server._RUNTIME = original_runtime
    check(
        result["state"] == "success"
        and result["record"]["identity"]
        == {
            "kind": "europe_pmc",
            "collection": "MED",
            "value": "12345",
        },
        "Europe PMC exact retrieval required an absent projection echo",
    )

    body = b"<PubmedArticleSet />"
    fixture_http = FixtureHttp(
        HttpResult(
            status=200,
            body=body,
            content_type="application/xml",
            headers={},
            network_bytes=len(body),
        )
    )
    original_runtime = server._RUNTIME
    original_get = server.pubmed.qualify_publication
    server._RUNTIME = RuntimeRegistry(http=fixture_http)
    server.pubmed.qualify_publication = REAL_PUBMED_GET
    try:
        result = run(server.literature_get("pubmed", "12345"))
    finally:
        server.pubmed.qualify_publication = original_get
        server._RUNTIME = original_runtime
    check(
        result["state"] == "failed"
        and result["failures"][0]["code"] == "provider_not_found"
        and result["failures"][0]["absence"] is True,
        "exact provider miss did not preserve established absence",
    )
    check(
        result["disclosure"]["outbound_calls"] == 1
        and "coverage" not in result
        and "record" in result
        and "currentness" in result
        and "retrieval_scope" in result,
        "validated exact-provider miss lost its get-outcome fields",
    )


def main() -> None:
    calls: dict[str, list[dict[str, Any]]] = {}
    http = DummyHttp()
    server._RUNTIME = RuntimeRegistry(http=http)
    install_adapter_stubs(calls)

    check_tool_surface()
    check_real_mcp_argument_validation(calls)
    check_pubmed_search_and_continuation(calls, http)
    check_europe_pmc_search_and_get(calls)
    check_pubmed_get(calls)
    check_rejections(calls)
    check_provider_failure_passthrough(calls)
    check_timeout_nonabsence()
    check_adapter_outcome_validation()
    check_pagination_safety()
    check_opaque_cursor_round_trip()
    check_identity_mismatch_semantics()
    check_public_abstract_budget()
    check_public_parser_limit()
    check_public_provider_boundaries()
    # Parsed by contract_map_check.py, which looks for a line containing
    # "assertions passed" and reads passed/total off it. This harness FAILS FAST -- a
    # failing assertion raises out of main() and this line never prints at all -- so
    # reached and passed are the same number by construction. The total is pinned in
    # contract_map_check.py's EXTERNAL_HARNESSES, deliberately in ONE place: pinning a
    # number twice is only as good as the arithmetic beside it, and that has been wrong
    # here twice.
    print(f"{CHECKS}/{CHECKS} assertions passed  (behavior)")


if __name__ == "__main__":
    main()
