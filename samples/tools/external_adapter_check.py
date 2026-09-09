#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx>=0.27",
#   "pydantic>=2",
# ]
# ///
"""Offline contract checks for the private PubMed and Europe PMC adapters.

    ./tools/external_adapter_check.py

NO NETWORK. Every provider response is a hand-built fixture handed to the adapter
through a fake ``http`` object shaped exactly like ``ProviderHttpClient.get`` --
same keyword arguments, same ``HttpResult`` return value (imported straight from
``http_client`` so ``status_failure`` and ``.json()`` behave identically to a real
call). Nothing here starts a transport, reads a config file, or touches the
network.

Follows the ``check()`` style of ``external_contract_check.py`` and
``external_behavior_check.py``: every assertion tests an observable field in the
adapter's returned envelope (a stage, a failure code, a candidate value, an
echoed request field) -- never merely that a call returned without raising.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent / "ics_public_science"
sys.path.insert(0, str(ROOT))

from adapters import europe_pmc, pubmed  # noqa: E402
from disclosure import AttemptBudgetExceeded  # noqa: E402
from http_client import HttpResult  # noqa: E402


CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(message)


# ---------------------------------------------------------------------------
# Fake transport
# ---------------------------------------------------------------------------


class FakeHttp:
    """Replays queued ``HttpResult``s (or raises queued exceptions) in order."""

    def __init__(self, *responses: HttpResult | BaseException) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def get(
        self,
        provider: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        accept: str = "application/json",
        max_bytes: int = 2 * 1024 * 1024,
        before_attempt: Any = None,
    ) -> HttpResult:
        self.calls.append(
            {
                "provider": provider,
                "path": path,
                "params": dict(params) if params is not None else None,
                "accept": accept,
                "max_bytes": max_bytes,
            }
        )
        if before_attempt is not None:
            before_attempt()
        if not self.responses:
            raise AssertionError("FakeHttp exhausted its queued responses")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def json_result(payload: Any, *, status: int = 200) -> HttpResult:
    body = json.dumps(payload).encode("utf-8")
    return HttpResult(
        status=status,
        body=body,
        content_type="application/json",
        headers={},
        network_bytes=len(body),
    )


def malformed_json_result(*, status: int = 200) -> HttpResult:
    body = b"{not actually json"
    return HttpResult(
        status=status,
        body=body,
        content_type="application/json",
        headers={},
        network_bytes=len(body),
    )


def xml_result(
    body: bytes,
    *,
    status: int = 200,
    content_type: str = "application/xml",
) -> HttpResult:
    return HttpResult(
        status=status,
        body=body,
        content_type=content_type,
        headers={},
        network_bytes=len(body),
    )


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# PubMed fixtures
# ---------------------------------------------------------------------------

def esearch_payload(
    *,
    count: int,
    idlist: list[str],
    offset: int,
    retmax: int,
    translation: str | None,
    warninglist: Any = None,
    errorlist: Any = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "count": str(count),
        "retstart": str(offset),
        "retmax": str(retmax),
        "idlist": idlist,
        "querytranslation": translation,
    }
    if warninglist is not None:
        result["warninglist"] = warninglist
    if errorlist is not None:
        result["errorlist"] = errorlist
    return {"esearchresult": result}


def esummary_item(
    pmid: str,
    *,
    title: str = "Example Title",
    journal: str = "J Test",
    pubdate: str = "2024",
    sortpubdate: str = "2024/01/01 00:00",
    authors: list[str] | None = None,
    pubtype: list[str] | None = None,
    article_ids: list[dict[str, str]] | None = None,
    has_abstract: bool = True,
    uid: str | None = ...,  # type: ignore[assignment]
    error: str | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "uid": pmid if uid is ... else uid,
        "title": title,
        "fulljournalname": journal,
        "pubdate": pubdate,
        "sortpubdate": sortpubdate,
        "authors": [{"name": name} for name in (authors or ["Doe J"])],
        "pubtype": pubtype or ["Journal Article"],
        "articleids": article_ids
        or [
            {"idtype": "pubmed", "value": pmid},
            {"idtype": "doi", "value": "10.1000/xyz"},
            {"idtype": "pmc", "value": "PMC1234567"},
        ],
        "attributes": ["Has Abstract"] if has_abstract else [],
    }
    if error:
        item["error"] = error
    return item


def esummary_payload(items: dict[str, dict[str, Any]], uids: list[str]) -> dict[str, Any]:
    return {"result": {"uids": uids, **items}}


def pubmed_article_xml(
    pmid: str,
    *,
    title: str = "Example Title",
    journal_title: str = "J Test",
    author_last: str = "Doe",
    author_fore: str = "Jane",
    abstract_text: str | None = "A primary abstract.",
    other_abstracts: list[tuple[str, str]] | None = None,
    pub_types: list[str] | None = None,
    comments: list[tuple[str, str]] | None = None,
) -> bytes:
    abstract_xml = (
        f"<Abstract><AbstractText>{abstract_text}</AbstractText></Abstract>"
        if abstract_text
        else ""
    )
    other_xml = "".join(
        f'<OtherAbstract Type="{kind}"><AbstractText>{text}</AbstractText></OtherAbstract>'
        for kind, text in (other_abstracts or [])
    )
    pub_type_xml = "".join(
        f"<PublicationType>{value}</PublicationType>"
        for value in (pub_types or ["Journal Article"])
    )
    comments_xml = "".join(
        f'<CommentsCorrections RefType="{ref_type}"><PMID>{target}</PMID></CommentsCorrections>'
        for ref_type, target in (comments or [])
    )
    return f"""<PubmedArticleSet><PubmedArticle>
<MedlineCitation><PMID>{pmid}</PMID>
<Article>
<Journal><Title>{journal_title}</Title>
<JournalIssue><PubDate><Year>2024</Year></PubDate></JournalIssue></Journal>
<ArticleTitle>{title}</ArticleTitle>
<AuthorList><Author><LastName>{author_last}</LastName><ForeName>{author_fore}</ForeName></Author></AuthorList>
<PublicationTypeList>{pub_type_xml}</PublicationTypeList>
{abstract_xml}
</Article>
{other_xml}
<CommentsCorrectionsList>{comments_xml}</CommentsCorrectionsList>
</MedlineCitation>
<PubmedData><ArticleIdList><ArticleId IdType="pubmed">{pmid}</ArticleId></ArticleIdList></PubmedData>
</PubmedArticle></PubmedArticleSet>""".encode("utf-8")


def pubmed_book_article_xml(
    pmid: str,
    *,
    title: str = "Chapter Title",
    book_title: str = "A Test Book",
    publisher: str = "Test Publisher",
    author_last: str = "Smith",
    author_fore: str = "Ann",
) -> bytes:
    return f"""<PubmedArticleSet><PubmedBookArticle>
<BookDocument><PMID>{pmid}</PMID>
<ArticleTitle>{title}</ArticleTitle>
<Book><BookTitle>{book_title}</BookTitle>
<Publisher><PublisherName>{publisher}</PublisherName></Publisher>
<AuthorList><Author><LastName>{author_last}</LastName><ForeName>{author_fore}</ForeName></Author></AuthorList>
</Book>
</BookDocument>
<PubmedBookData><ArticleIdList><ArticleId IdType="pubmed">{pmid}</ArticleId></ArticleIdList></PubmedBookData>
</PubmedBookArticle></PubmedArticleSet>""".encode("utf-8")


def delete_citation_xml(pmid: str) -> bytes:
    return (
        f"<PubmedArticleSet><DeleteCitation><PMID>{pmid}</PMID>"
        "</DeleteCitation></PubmedArticleSet>"
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# PubMed map_publications
# ---------------------------------------------------------------------------


def check_pubmed_map() -> None:
    query = "target mediated clearance"

    # 1. Valid candidate page + ESummary normalization ----------------------
    http = FakeHttp(
        json_result(
            esearch_payload(
                count=2,
                idlist=["111", "222"],
                offset=0,
                retmax=2,
                translation=query,
            )
        ),
        json_result(
            esummary_payload(
                {
                    "111": esummary_item("111", authors=["Doe J", "Roe A"]),
                    "222": esummary_item(
                        "222",
                        title="Second Title",
                        pubtype=["Journal Article", "Retracted Publication"],
                    ),
                },
                uids=["111", "222"],
            )
        ),
    )
    out = run(
        pubmed.map_publications(
            http, compiled_query=query, page_size=10, order="relevance", offset=0
        )
    )
    check(out["stages"]["result"] == "succeeded", "valid page did not succeed")
    check(len(out["candidates"]) == 2, "expected exactly two candidates")
    first, second = out["candidates"]
    check(first["identity"] == {"kind": "pmid", "value": "111"}, "identity not normalized")
    check(first["absolute_position"] == 0, "first candidate absolute_position wrong")
    check(second["absolute_position"] == 1, "second candidate absolute_position wrong")
    check(
        {"kind": "doi", "value": "10.1000/xyz"} in first["represented_identifiers"],
        "DOI alias was not normalized onto the candidate",
    )
    check(
        {"kind": "pmcid", "value": "PMC1234567"} in first["represented_identifiers"],
        "PMCID alias was not normalized onto the candidate",
    )
    check(first["contributors"] == ["Doe J", "Roe A"], "contributor names changed")
    check(first["container"] == "J Test", "container/journal title changed")
    check(
        "retracted_publication" in [s["kind"] for s in second["integrity_signals"]],
        "retracted publication type was not represented as an integrity signal",
    )
    check(len(http.calls) == 2, "expected exactly ESearch then ESummary")
    check(http.calls[0]["path"] == "esearch.fcgi", "first call was not esearch.fcgi")
    check(http.calls[1]["path"] == "esummary.fcgi", "second call was not esummary.fcgi")

    # 2. PubMed canonicalization may change text but not field scope ----------
    normalized_queries = (
        (
            'T-cell[Title/Abstract]',
            '"T cell"[Title/Abstract]',
        ),
        (
            (
                '("tarlatamab"[Title/Abstract] AND '
                '"cytokine release syndrome"[Title/Abstract])'
            ),
            (
                '"tarlatamab"[Title/Abstract] AND '
                '"cytokine release syndrome"[Title/Abstract]'
            ),
        ),
    )
    for submitted, translated in normalized_queries:
        http = FakeHttp(
            json_result(
                esearch_payload(
                    count=1,
                    idlist=["111"],
                    offset=0,
                    retmax=1,
                    translation=translated,
                )
            ),
            json_result(
                esummary_payload(
                    {"111": esummary_item("111")},
                    uids=["111"],
                )
            ),
        )
        out = run(
            pubmed.map_publications(
                http,
                compiled_query=submitted,
                page_size=10,
                order="relevance",
                offset=0,
            )
        )
        check(
            out["stages"]["result"] == "succeeded"
            and out["query_fidelity"] == "scope_preserved"
            and len(out["candidates"]) == 1,
            "scope-preserving PubMed normalization made a query unusable",
        )
        check(
            any(
                item["code"] == "provider_query_normalized"
                for item in out["warnings"]
            ),
            "scope-preserving PubMed normalization was not disclosed",
        )

    broadened_query = "aspirin[Title/Abstract]"
    http = FakeHttp(
        json_result(
            esearch_payload(
                count=1,
                idlist=["111"],
                offset=0,
                retmax=1,
                translation=(
                    '("aspirin"[MeSH Terms] OR "aspirin"[All Fields])'
                ),
            )
        )
    )
    out = run(
        pubmed.map_publications(
            http,
            compiled_query=broadened_query,
            page_size=10,
            order="relevance",
            offset=0,
        )
    )
    check(
        out["stages"]["result"] == "failed"
        and out["failures"][0]["code"] == "query_repaired",
        "PubMed scope broadening was mistaken for benign normalization",
    )

    submitted = "(alpha[Title/Abstract] AND beta[Title/Abstract])"
    for translated in (
        "(alpha[Title/Abstract] OR beta[Title/Abstract])",
        "(alpha[Title/Abstract] AND alpha[Title/Abstract])",
        (
            "(alpha[Title/Abstract] AND beta[Title/Abstract] "
            "OR gamma)"
        ),
        (
            '("alpha[Title/Abstract]"[Title/Abstract] '
            "AND beta[Title/Abstract])"
        ),
    ):
        http = FakeHttp(
            json_result(
                esearch_payload(
                    count=1,
                    idlist=["111"],
                    offset=0,
                    retmax=1,
                    translation=translated,
                )
            )
        )
        out = run(
            pubmed.map_publications(
                http,
                compiled_query=submitted,
                page_size=10,
                order="relevance",
                offset=0,
            )
        )
        check(
            out["stages"]["result"] == "failed"
            and out["failures"][0]["code"] == "query_repaired"
            and len(http.calls) == 1,
            "semantic PubMed query repair passed field-aware validation",
        )

    for submitted, translated in (
        ("HER2+[Title/Abstract]", "HER2[Title/Abstract]"),
        ("C++[Title/Abstract]", "C[Title/Abstract]"),
    ):
        http = FakeHttp(
            json_result(
                esearch_payload(
                    count=1,
                    idlist=["111"],
                    offset=0,
                    retmax=1,
                    translation=translated,
                )
            )
        )
        out = run(
            pubmed.map_publications(
                http,
                compiled_query=submitted,
                page_size=10,
                order="relevance",
                offset=0,
            )
        )
        check(
            out["stages"]["result"] == "failed"
            and out["failures"][0]["code"] == "query_repaired"
            and len(http.calls) == 1,
            "meaningful PubMed query punctuation was normalized away",
        )

    http = FakeHttp(
        json_result(
            esearch_payload(
                count=1,
                idlist=["111"],
                offset=0,
                retmax=1,
                translation="HER2[Ti!tle/Abstract]",
            )
        )
    )
    out = run(
        pubmed.map_publications(
            http,
            compiled_query="HER2[Title/Abstract]",
            page_size=10,
            order="relevance",
            offset=0,
        )
    )
    check(
        out["stages"]["result"] == "failed"
        and out["failures"][0]["code"] == "query_repaired"
        and len(http.calls) == 1,
        "a malformed PubMed field tag was normalized into a valid alias",
    )

    for submitted, translated in (
        (
            "alphabeta[Title/Abstract]",
            "alpha[Title/Abstract]beta[Title/Abstract]",
        ),
        (
            "(alpha[Title/Abstract] AND beta[Title/Abstract])",
            "alpha[Title/Abstract]OR beta[Title/Abstract]",
        ),
    ):
        http = FakeHttp(
            json_result(
                esearch_payload(
                    count=1,
                    idlist=["111"],
                    offset=0,
                    retmax=1,
                    translation=translated,
                )
            )
        )
        out = run(
            pubmed.map_publications(
                http,
                compiled_query=submitted,
                page_size=10,
                order="relevance",
                offset=0,
            )
        )
        check(
            out["stages"]["result"] == "failed"
            and out["failures"][0]["code"] == "query_repaired"
            and len(http.calls) == 1,
            "adjacent PubMed tags or operators collapsed query boundaries",
        )

    http = FakeHttp(
        json_result(
            esearch_payload(
                count=0,
                idlist=[],
                offset=0,
                retmax=0,
                translation='"T-cell"[Title/Abstract]',
                warninglist={"outputmessages": ["No items found."]},
                errorlist={"phrasesnotfound": ["T-cell"]},
            )
        )
    )
    out = run(
        pubmed.map_publications(
            http,
            compiled_query="T-cell[Title/Abstract]",
            page_size=10,
            order="relevance",
            offset=0,
        )
    )
    check(
        out["stages"]["result"] == "failed"
        and out["failures"][0]["code"] == "query_repaired",
        "a changed PubMed translation with no-items notices became valid_zero",
    )

    # 2. Short/final pages report the number actually returned ----------------
    http = FakeHttp(
        json_result(
            esearch_payload(
                count=1,
                idlist=["111"],
                offset=0,
                retmax=10,
                translation=query,
            )
        )
    )
    out = run(
        pubmed.map_publications(
            http, compiled_query=query, page_size=10, order="relevance", offset=0
        )
    )
    check(
        out["failures"][0]["code"] == "query_fidelity_unknown",
        "a retmax ceiling must not be accepted as the returned row count",
    )

    # 2. Warnings/repair reject normal candidates ----------------------------
    http = FakeHttp(
        json_result(
            esearch_payload(
                count=1,
                idlist=["111"],
                offset=0,
                retmax=1,
                translation=query,
                warninglist={"phrasesignored": ["ignored term"]},
            )
        )
    )
    out = run(
        pubmed.map_publications(
            http, compiled_query=query, page_size=10, order="relevance", offset=0
        )
    )
    check(out["stages"]["result"] == "failed", "a PubMed warning must fail the page")
    check(out["query_fidelity"] == "repaired", "query_fidelity was not marked repaired")
    check(out["candidates"] == [], "no candidates may be represented once repaired")
    check(
        out["failures"][0]["code"] == "query_repaired",
        "expected the query_repaired failure code",
    )
    check(len(http.calls) == 1, "ESummary must not be called once PubMed repaired the query")

    http = FakeHttp(
        json_result(
            esearch_payload(
                count=0,
                idlist=[],
                offset=0,
                retmax=0,
                translation=query,
                warninglist={
                    "phrasesignored": [],
                    "quotedphrasesnotfound": [],
                    "outputmessages": ["No items found."],
                },
                errorlist={
                    "phrasesnotfound": ["target mediated clearance"],
                    "fieldsnotfound": [],
                },
            )
        )
    )
    out = run(
        pubmed.map_publications(
            http, compiled_query=query, page_size=10, order="relevance", offset=0
        )
    )
    check(
        out["stages"]["result"] == "valid_zero",
        "the verified PubMed no-items response must remain a valid zero",
    )
    check(
        out["query_fidelity"] == "exact",
        "the verified no-items response changed exact query fidelity",
    )

    # 3. Malformed JSON / HTTP-200 error --------------------------------------
    http = FakeHttp(malformed_json_result())
    out = run(
        pubmed.map_publications(
            http, compiled_query=query, page_size=10, order="relevance", offset=0
        )
    )
    check(
        out["failures"][0]["code"] == "provider_schema_drift",
        "malformed HTTP-200 ESearch body must be reported as schema drift",
    )
    check(out["stages"]["provider_body"] == "failed", "provider_body stage must be failed")
    check(out["stages"]["result"] == "failed", "result stage must be failed")

    for invalid_count in ("²", "9" * 5_000):
        payload = esearch_payload(
            count=0,
            idlist=[],
            offset=0,
            retmax=0,
            translation=query,
        )
        payload["esearchresult"]["count"] = invalid_count
        out = run(
            pubmed.map_publications(
                FakeHttp(json_result(payload)),
                compiled_query=query,
                page_size=10,
                order="relevance",
                offset=0,
            )
        )
        check(
            out["failures"][0]["code"] == "provider_schema_drift",
            "an unsafe PubMed integer escaped structured provider failure",
        )

    deep_json = HttpResult(
        status=200,
        body=b"[" * 20_000 + b"0" + b"]" * 20_000,
        content_type="application/json",
        headers={},
        network_bytes=40_001,
    )
    out = run(
        pubmed.map_publications(
            FakeHttp(deep_json),
            compiled_query=query,
            page_size=10,
            order="relevance",
            offset=0,
        )
    )
    check(
        out["failures"][0]["code"] == "parser_limit_exceeded",
        "deep PubMed JSON must become a structured parser-limit failure",
    )

    nested_warning = b'{"child":' * 850 + b'"signal"' + b"}" * 850
    parseable_deep_body = (
        b'{"esearchresult":{"count":"0","retstart":"0","retmax":"0",'
        b'"idlist":[],"querytranslation":"target mediated clearance",'
        b'"warninglist":'
        + nested_warning
        + b"}}"
    )
    out = run(
        pubmed.map_publications(
            FakeHttp(
                HttpResult(
                    status=200,
                    body=parseable_deep_body,
                    content_type="application/json",
                    headers={},
                    network_bytes=len(parseable_deep_body),
                )
            ),
            compiled_query=query,
            page_size=10,
            order="relevance",
            offset=0,
        )
    )
    check(
        out["failures"][0]["code"] == "parser_limit_exceeded",
        "parseable deep PubMed JSON escaped structural limits",
    )

    # 4. ESummary missing/per-record error semantics --------------------------
    http = FakeHttp(
        json_result(
            esearch_payload(
                count=2,
                idlist=["111", "222"],
                offset=0,
                retmax=2,
                translation=query,
            )
        ),
        json_result(
            esummary_payload({"111": esummary_item("111")}, uids=["111", "222"])
        ),
    )
    out = run(
        pubmed.map_publications(
            http, compiled_query=query, page_size=10, order="relevance", offset=0
        )
    )
    check(
        out["stages"]["result"] == "succeeded",
        "a missing per-PMID ESummary entry is a partial projection, not a failed page",
    )
    check(
        out["stages"]["projection_or_selection"] == "inconsistent",
        "projection_or_selection must record the partial ESummary coverage",
    )
    check(
        any(f["code"] == "projection_incomplete" for f in out["failures"]),
        "expected a projection_incomplete failure for the omitted PMID",
    )
    check(len(out["candidates"]) == 2, "both requested PMIDs must still be represented")
    check(
        out["candidates"][1]["metadata_state"] == "partial",
        "the candidate missing ESummary metadata must be marked partial",
    )
    check(
        out["candidates"][0]["metadata_state"] == "represented",
        "the candidate with full ESummary metadata must be marked represented",
    )

    http = FakeHttp(
        json_result(
            esearch_payload(
                count=1,
                idlist=["111"],
                offset=0,
                retmax=1,
                translation=query,
            )
        ),
        json_result(
            esummary_payload(
                {"111": esummary_item("111", error="cannot get document summary")},
                uids=["111"],
            )
        ),
    )
    out = run(
        pubmed.map_publications(
            http, compiled_query=query, page_size=10, order="relevance", offset=0
        )
    )
    check(
        out["candidates"][0]["metadata_state"] == "partial",
        "a per-record ESummary error must mark the candidate partial",
    )
    check(
        any(f["code"] == "projection_incomplete" for f in out["failures"]),
        "a per-record ESummary error must be reported as projection_incomplete",
    )

    http = FakeHttp(
        json_result(
            esearch_payload(
                count=1,
                idlist=["111"],
                offset=0,
                retmax=1,
                translation=query,
            )
        ),
        json_result(
            esummary_payload({"111": esummary_item("111", uid="999")}, uids=["111"])
        ),
    )
    out = run(
        pubmed.map_publications(
            http, compiled_query=query, page_size=10, order="relevance", offset=0
        )
    )
    check(
        out["failures"][0]["code"] == "identity_mismatch",
        "a mismatched ESummary uid field must be rejected as identity_mismatch",
    )

    # 5. Exact boundary: offset 9998 has no continuation to 9999 -------------
    http = FakeHttp(
        json_result(
            esearch_payload(
                count=50_000,
                idlist=["999999"],
                offset=9998,
                retmax=1,
                translation=query,
            )
        ),
        json_result(esummary_payload({"999999": esummary_item("999999")}, uids=["999999"])),
    )
    out = run(
        pubmed.map_publications(
            http, compiled_query=query, page_size=25, order="relevance", offset=9998
        )
    )
    check(
        out["count_observation"]["effective_page_size"] == 1,
        "effective_page_size must clamp to exactly one result at position 9998",
    )
    check(
        out["continuation_observation"]["state"] == "source_visibility_limit",
        "position 9998 must not offer an ordinary continuation to 9999",
    )
    check(
        out["continuation_observation"]["next_offset"] is None,
        "no next_offset may be represented past the visibility boundary",
    )
    check(
        any(
            w["code"] == "source_visibility_limit" and w["scope"] == "pubmed.count"
            for w in out["warnings"]
        ),
        "expected a source_visibility_limit warning scoped to pubmed.count",
    )

    # 6. Offset 9999 is rejected outright -------------------------------------
    http = FakeHttp()
    out = run(
        pubmed.map_publications(
            http, compiled_query=query, page_size=10, order="relevance", offset=9999
        )
    )
    check(
        out["failures"][0]["code"] == "source_visibility_limit",
        "offset 9999 must be rejected with source_visibility_limit",
    )
    check(out["stages"]["validation"] == "failed", "offset 9999 must fail validation")
    check(len(http.calls) == 0, "offset 9999 must not reach the transport at all")

    # 7. Unexpected internal exceptions are not advertised as retryable -------
    out = run(
        pubmed.map_publications(
            FakeHttp(RuntimeError("internal transport contract failure")),
            compiled_query=query,
            page_size=10,
            order="relevance",
            offset=0,
        )
    )
    check(
        out["failures"][0]["code"] == "connection_failure"
        and out["failures"][0]["retryable"] is False,
        "an internal PubMed adapter exception was marked retryable",
    )
    out = run(
        pubmed.map_publications(
            FakeHttp(
                json_result(
                    esearch_payload(
                        count=1,
                        idlist=["111"],
                        offset=0,
                        retmax=1,
                        translation=query,
                    )
                ),
                RuntimeError("internal ESummary contract failure"),
            ),
            compiled_query=query,
            page_size=10,
            order="relevance",
            offset=0,
        )
    )
    check(
        out["failures"][0]["code"] == "connection_failure"
        and out["failures"][0]["retryable"] is False,
        "an internal PubMed ESummary exception was marked retryable",
    )

    # 8. AttemptBudgetExceeded propagates, is not swallowed -------------------
    http = FakeHttp(AttemptBudgetExceeded("attempt ceiling exhausted"))
    try:
        run(
            pubmed.map_publications(
                http, compiled_query=query, page_size=10, order="relevance", offset=0
            )
        )
    except AttemptBudgetExceeded:
        check(True, "AttemptBudgetExceeded propagated")
    else:
        check(False, "AttemptBudgetExceeded must not be swallowed by map_publications")


# ---------------------------------------------------------------------------
# PubMed qualify_publication
# ---------------------------------------------------------------------------


def check_pubmed_qualify() -> None:
    # 1. PubmedArticle branch -------------------------------------------------
    http = FakeHttp(xml_result(pubmed_article_xml("555")))
    out = run(
        pubmed.qualify_publication(
            http, pmid="555", projection="standard", include=[]
        )
    )
    check(out["stages"]["result"] == "succeeded", "a plain article must qualify")
    record = out["record"]
    check(record["record_class"] == "publication", "record_class changed for an article")
    check(record["title"] == "Example Title", "title text changed")
    check(record["container"]["title"] == "J Test", "journal title changed")
    check(record["container"]["kind"] == "journal", "container kind must be journal")
    check(
        record["contributors"]["names"] == ["Jane Doe"],
        "author given+family composition changed",
    )

    # 2. PubmedBookArticle branch ----------------------------------------------
    http = FakeHttp(xml_result(pubmed_book_article_xml("777")))
    out = run(
        pubmed.qualify_publication(
            http, pmid="777", projection="standard", include=[]
        )
    )
    check(out["stages"]["result"] == "succeeded", "a book article must qualify")
    record = out["record"]
    check(record["record_class"] == "book_article", "record_class changed for a book article")
    check(record["container"]["title"] == "A Test Book", "book title changed")
    check(record["container"]["kind"] == "book", "container kind must be book")
    check(record["container"]["publisher"] == "Test Publisher", "publisher changed")

    # 3. DeleteCitation branch --------------------------------------------------
    http = FakeHttp(xml_result(delete_citation_xml("888")))
    out = run(
        pubmed.qualify_publication(
            http, pmid="888", projection="standard", include=[]
        )
    )
    check(out["stages"]["result"] == "succeeded", "a delete citation must still qualify")
    record = out["record"]
    check(record["record_class"] == "deleted_citation", "deleted record_class changed")
    check(
        "deleted_citation" in record["integrity"]["labels"],
        "deleted_citation integrity label missing",
    )
    check(
        record["extension"]["abstracts"]["state"] == "not_represented"
        and record["extension"]["indexing"]["state"] == "not_represented",
        "standard deleted record did not represent requested families as unavailable",
    )
    http = FakeHttp(xml_result(delete_citation_xml("888")))
    summary = run(
        pubmed.qualify_publication(
            http, pmid="888", projection="summary", include=[]
        )
    )
    check(
        summary["record"]["extension"]["abstracts"]["state"]
        == "not_requested",
        "summary deleted record mislabeled an unrequested abstract",
    )

    # 4. Alternate OtherAbstract --------------------------------------------
    http = FakeHttp(
        xml_result(
            pubmed_article_xml(
                "999",
                abstract_text=None,
                other_abstracts=[("plain-language-summary", "An alternate abstract.")],
            )
        )
    )
    out = run(
        pubmed.qualify_publication(
            http, pmid="999", projection="standard", include=[]
        )
    )
    abstracts = out["record"]["extension"]["abstracts"]
    check(abstracts["state"] == "represented", "OtherAbstract alone must represent the family")
    check(
        any(item["item_type"] == "other_abstract" for item in abstracts["items"]),
        "OtherAbstract must be represented as an other_abstract item",
    )
    check(
        not any(item["item_type"] == "primary_abstract" for item in abstracts["items"]),
        "no primary Abstract element was present, so none may be represented",
    )

    # 5a. Slim projections do not request relationship metadata ----------------
    overflow_comments = [
        ("CommentOn", str(1_000 + index))
        for index in range(201)
    ]
    out = run(
        pubmed.qualify_publication(
            FakeHttp(
                xml_result(
                    pubmed_article_xml(
                        "111",
                        comments=overflow_comments,
                    )
                )
            ),
            pmid="111",
            projection="standard",
            include=[],
        )
    )
    check(
        out["stages"]["result"] == "succeeded"
        and out["failures"] == []
        and out["record"]["direct_relationships"] == [],
        "a slim PubMed projection still requested relationship metadata",
    )
    check(
        not any(
            family["name"] == "relationships"
            for family in out["record"]["projection"]["families"]
        ),
        "a slim PubMed projection still advertised the relationship family",
    )

    # 5b. Explicit CommentsCorrections outbound direction ---------------------
    http = FakeHttp(
        xml_result(
            pubmed_article_xml("111", comments=[("CommentOn", "222")])
        )
    )
    out = run(
        pubmed.qualify_publication(
            http,
            pmid="111",
            projection="standard",
            include=["relationships"],
        )
    )
    edges = out["record"]["direct_relationships"]
    check(len(edges) == 1, "expected exactly one CommentOn edge")
    check(edges[0]["relation"] == "comments_on", "CommentOn must normalize to comments_on")
    assertion = edges[0]["assertions"][0]
    check(
        assertion["subject"] == {"kind": "pmid", "value": "111"},
        "outbound relation subject must be the seed record",
    )
    check(
        assertion["object"] == {"kind": "pmid", "value": "222"},
        "outbound relation object must be the represented target",
    )

    # 5c. Explicit CommentsCorrections inbound direction ----------------------
    http = FakeHttp(
        xml_result(
            pubmed_article_xml("111", comments=[("CommentIn", "333")])
        )
    )
    out = run(
        pubmed.qualify_publication(
            http,
            pmid="111",
            projection="standard",
            include=["relationships"],
        )
    )
    edges = out["record"]["direct_relationships"]
    check(len(edges) == 1, "expected exactly one CommentIn edge")
    check(
        edges[0]["relation"] == "commented_on_by",
        "CommentIn must normalize to commented_on_by",
    )
    assertion = edges[0]["assertions"][0]
    check(
        assertion["subject"] == {"kind": "pmid", "value": "333"},
        "inbound relation subject must be the commenting record, not the seed",
    )
    check(
        assertion["object"] == {"kind": "pmid", "value": "111"},
        "inbound relation object must be flipped back onto the seed",
    )

    # 6. Mismatched PMID -------------------------------------------------------
    http = FakeHttp(xml_result(pubmed_article_xml("444")))
    out = run(
        pubmed.qualify_publication(
            http, pmid="123", projection="standard", include=[]
        )
    )
    check(
        out["failures"][0]["code"] == "identity_mismatch",
        "a record for a different PMID must be rejected as identity_mismatch",
    )
    check(out["record"] is None, "no record may be represented on identity mismatch")

    # 7. Known external DTD is accepted; unsafe declarations are rejected ------
    nlm_doctype = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE PubmedArticleSet PUBLIC '
        b'"-//NLM//DTD PubMedArticle, 1st January 2026//EN" '
        b'"https://dtd.nlm.nih.gov/ncbi/pubmed/out/pubmed_260101.dtd">'
    )
    out = run(
        pubmed.qualify_publication(
            FakeHttp(
                xml_result(
                    nlm_doctype + pubmed_article_xml("123")
                )
            ),
            pmid="123",
            projection="standard",
            include=[],
        )
    )
    check(
        out["stages"]["result"] == "succeeded",
        "the measured NLM PubMed external DTD was rejected",
    )

    xxe_body = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE PubmedArticleSet ['
        b'<!ENTITY xxe SYSTEM "file:///etc/passwd">'
        b"]>"
        b"<PubmedArticleSet>&xxe;</PubmedArticleSet>"
    )
    http = FakeHttp(xml_result(xxe_body))
    out = run(
        pubmed.qualify_publication(
            http, pmid="123", projection="standard", include=[]
        )
    )
    check(
        out["failures"][0]["code"] == "parser_limit_exceeded",
        "a DTD/ENTITY declaration must be rejected as parser_limit_exceeded",
    )
    untrusted_doctype = (
        b'<!DOCTYPE PubmedArticleSet PUBLIC "-//EXAMPLE//DTD Unknown//EN" '
        b'"https://example.com/pubmed.dtd">'
        + pubmed_article_xml("123")
    )
    out = run(
        pubmed.qualify_publication(
            FakeHttp(xml_result(untrusted_doctype)),
            pmid="123",
            projection="standard",
            include=[],
        )
    )
    check(
        out["failures"][0]["code"] == "parser_limit_exceeded",
        "an unrecognized external DTD location was accepted",
    )
    mismatched_doctype = (
        b'<!DOCTYPE PubmedArticleSet PUBLIC '
        b'"-//NLM//DTD PubMedArticle, 1st January 2026//EN" '
        b'"https://dtd.nlm.nih.gov/ncbi/pubmed/out/pubmed_260101.dtd">'
        b"<DeleteCitation><PMID>123</PMID></DeleteCitation>"
    )
    out = run(
        pubmed.qualify_publication(
            FakeHttp(xml_result(mismatched_doctype)),
            pmid="123",
            projection="standard",
            include=[],
        )
    )
    check(
        out["failures"][0]["code"] == "provider_schema_drift",
        "a PubMed DTD declaration was not bound to the parsed root",
    )
    namespaced_xml = pubmed_article_xml("123").replace(
        b"<PubmedArticleSet>",
        b'<PubmedArticleSet xmlns="urn:foreign">',
        1,
    )
    out = run(
        pubmed.qualify_publication(
            FakeHttp(xml_result(namespaced_xml)),
            pmid="123",
            projection="standard",
            include=[],
        )
    )
    check(
        out["failures"][0]["code"] == "provider_schema_drift",
        "a foreign XML namespace was accepted as a PubMed document root",
    )
    nested_namespaced_xml = pubmed_article_xml("123").replace(
        b"<PubmedArticle>",
        b'<x:PubmedArticle xmlns:x="urn:foreign">',
        1,
    ).replace(
        b"</PubmedArticle>",
        b"</x:PubmedArticle>",
        1,
    )
    out = run(
        pubmed.qualify_publication(
            FakeHttp(xml_result(nested_namespaced_xml)),
            pmid="123",
            projection="standard",
            include=[],
        )
    )
    check(
        out["failures"][0]["code"] == "provider_schema_drift",
        "a nested foreign XML namespace was accepted as PubMed data",
    )

    unknown_encoding = (
        b'<?xml version="1.0" encoding="X-UNKNOWN"?>'
        b"<PubmedArticleSet/>"
    )
    out = run(
        pubmed.qualify_publication(
            FakeHttp(xml_result(unknown_encoding)),
            pmid="123",
            projection="standard",
            include=[],
        )
    )
    check(
        out["failures"][0]["code"] == "provider_schema_drift",
        "an unknown PubMed XML encoding must not escape structured handling",
    )
    unsupported_encoding = (
        b'<?xml version="1.0" encoding="UTF-7"?>'
        b"<PubmedArticleSet/>"
    )
    out = run(
        pubmed.qualify_publication(
            FakeHttp(xml_result(unsupported_encoding)),
            pmid="123",
            projection="standard",
            include=[],
        )
    )
    check(
        out["failures"][0]["code"] == "provider_schema_drift",
        "an unsupported multibyte XML encoding escaped structured handling",
    )

    out = run(
        pubmed.qualify_publication(
            FakeHttp(
                xml_result(
                    b"<html><body>maintenance</body></html>",
                    content_type="text/html",
                )
            ),
            pmid="123",
            projection="standard",
            include=[],
        )
    )
    check(
        out["failures"][0]["code"] == "provider_error_body"
        and out["failures"][0]["absence"] is False,
        "an HTML maintenance body must not be reported as record absence",
    )

    # 8. Parser structure limits (depth, then element count) -------------------
    deep_body = (
        b"<PubmedArticleSet>" + b"<Wrap>" * 200 + b"X" + b"</Wrap>" * 200 + b"</PubmedArticleSet>"
    )
    http = FakeHttp(xml_result(deep_body))
    out = run(
        pubmed.qualify_publication(
            http, pmid="123", projection="standard", include=[]
        )
    )
    check(
        out["failures"][0]["code"] == "parser_limit_exceeded",
        "XML deeper than the structural depth limit must be rejected",
    )

    wide_body = b"<PubmedArticleSet>" + b"<a/>" * 50_005 + b"</PubmedArticleSet>"
    http = FakeHttp(xml_result(wide_body))
    out = run(
        pubmed.qualify_publication(
            http, pmid="123", projection="standard", include=[]
        )
    )
    check(
        out["failures"][0]["code"] == "parser_limit_exceeded",
        "XML wider than the structural element-count limit must be rejected",
    )

    out = run(
        pubmed.qualify_publication(
            FakeHttp(RuntimeError("internal transport contract failure")),
            pmid="123",
            projection="standard",
            include=[],
        )
    )
    check(
        out["failures"][0]["code"] == "connection_failure"
        and out["failures"][0]["retryable"] is False,
        "an internal PubMed EFetch exception was marked retryable",
    )

    # AttemptBudgetExceeded propagates ------------------------------------------
    http = FakeHttp(AttemptBudgetExceeded("attempt ceiling exhausted"))
    try:
        run(
            pubmed.qualify_publication(
                http, pmid="123", projection="standard", include=[]
            )
        )
    except AttemptBudgetExceeded:
        check(True, "AttemptBudgetExceeded propagated")
    else:
        check(False, "AttemptBudgetExceeded must not be swallowed by qualify_publication")


# ---------------------------------------------------------------------------
# Europe PMC fixtures
# ---------------------------------------------------------------------------


def expected_provider_query(query: str, collections: list[str]) -> str:
    if not collections:
        return query
    clauses = [f"SRC:{value}" for value in collections]
    filter_clause = clauses[0] if len(clauses) == 1 else "(" + " OR ".join(clauses) + ")"
    return f"({query}) AND {filter_clause}"


def europe_pmc_row(source: str = "MED", id_: str = "12345678", **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "source": source,
        "id": id_,
        "title": "Example Title",
        "authorList": {"author": [{"fullName": "Jane Doe"}]},
        "journalInfo": {"journal": {"title": "J Test"}},
        "pubYear": "2024",
        "pmid": id_ if source == "MED" else None,
        "doi": "10.1000/xyz",
        "isOpenAccess": "Y",
        "citedByCount": 3,
    }
    row.update(overrides)
    return {key: value for key, value in row.items() if value is not None}


def search_payload(
    *,
    hit_count: int,
    rows: list[dict[str, Any]],
    provider_query: str,
    cursor: str,
    page_size: int,
    sort: str = "",
    next_cursor: str | None = None,
    version: str | None = "6.9",
    synonym: Any = False,
    result_type: str | None = "core",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "hitCount": hit_count,
        "request": {
            "queryString": provider_query,
            "cursorMark": cursor,
            "pageSize": page_size,
            "sort": sort,
            "synonym": synonym,
        },
        "resultList": {"result": rows},
    }
    if version is not None:
        payload["version"] = version
    if result_type is not None:
        payload["request"]["resultType"] = result_type
    if next_cursor is not None:
        payload["nextCursorMark"] = next_cursor
    if extra:
        extra_payload = dict(extra)
        request_overrides = extra_payload.pop("request", None)
        payload.update(extra_payload)
        if request_overrides is not None:
            if isinstance(request_overrides, dict):
                payload["request"].update(request_overrides)
            else:
                payload["request"] = request_overrides
    return payload


# ---------------------------------------------------------------------------
# Europe PMC map_records
# ---------------------------------------------------------------------------


def check_europe_pmc_map() -> None:
    query = "target mediated clearance"

    # 1. Valid page and candidate normalization -------------------------------
    provider_query = expected_provider_query(query, ["MED"])
    rows = [
        europe_pmc_row(id_="11111111"),
        europe_pmc_row(id_="22222222", citedByCount=None, doi=None, pmid=None),
    ]
    http = FakeHttp(
        json_result(
            search_payload(
                hit_count=2,
                rows=rows,
                provider_query=provider_query,
                cursor="%2a",
                page_size=5,
                result_type="CORE",
            )
        )
    )
    out = run(
        europe_pmc.map_records(
            http,
            compiled_query=query,
            page_size=5,
            order="relevance",
            cursor="*",
            searched_scope="literature",
            source_collections=["MED"],
        )
    )
    check(
        out["stages"]["result"] == "succeeded"
        and out["query_observation"]["projection_echo"] == "CORE",
        "a clean page with Europe PMC's uppercase core echo must succeed",
    )
    check(
        out["cursor_observation"]["requested_cursor"] == "*",
        "the URL-encoded initial cursor echo changed the requested cursor",
    )
    check(len(out["candidates"]) == 2, "expected exactly two candidates")
    first, second = out["candidates"]
    check(
        first["identity"] == {"kind": "europe_pmc", "collection": "MED", "value": "11111111"},
        "europe_pmc identity was not normalized",
    )
    check(first["absolute_position"] == 0, "first candidate absolute_position wrong")
    check(second["absolute_position"] == 1, "second candidate absolute_position wrong")
    check(
        {"kind": "doi", "value": "10.1000/xyz"} in first["represented_identifiers"],
        "DOI alias was not normalized",
    )
    check(
        any(s["kind"] == "open_access" and s["value"] is True for s in first["access_signals"]),
        "isOpenAccess=Y must be represented as an open_access access signal",
    )
    check(
        "citation_count_observation" not in first,
        "Europe PMC candidate retained a hidden citation-count diagnostic",
    )
    check(
        not any(w["code"] == "citation_count_not_quality" for w in out["warnings"]),
        "Europe PMC warned about a citation count absent from its public projection",
    )

    opaque_cursor = " cursor-two "
    http = FakeHttp(
        json_result(
            search_payload(
                hit_count=2,
                rows=[europe_pmc_row(id_="11111111")],
                provider_query=provider_query,
                cursor="*",
                page_size=1,
                next_cursor=opaque_cursor,
            )
        )
    )
    out = run(
        europe_pmc.map_records(
            http,
            compiled_query=query,
            page_size=1,
            order="relevance",
            cursor="*",
            searched_scope="literature",
            source_collections=["MED"],
        )
    )
    check(
        out["continuation_observation"]["next_cursor"] == opaque_cursor,
        "Europe PMC changed an opaque next cursor before representation",
    )
    http = FakeHttp(
        json_result(
            search_payload(
                hit_count=2,
                rows=[europe_pmc_row(id_="22222222")],
                provider_query=provider_query,
                cursor=opaque_cursor,
                page_size=1,
            )
        )
    )
    out = run(
        europe_pmc.map_records(
            http,
            compiled_query=query,
            page_size=1,
            order="relevance",
            cursor=opaque_cursor,
            searched_scope="literature",
            source_collections=["MED"],
            start_position=1,
        )
    )
    check(
        out["stages"]["result"] == "succeeded"
        and http.calls[0]["params"]["cursorMark"] == opaque_cursor,
        "Europe PMC did not preserve an opaque cursor on the next request",
    )

    malformed_access = search_payload(
        hit_count=1,
        rows=[
            europe_pmc_row(
                id_="33333333",
                isOpenAccess=[],
            )
        ],
        provider_query=provider_query,
        cursor="*",
        page_size=5,
    )
    out = run(
        europe_pmc.map_records(
            FakeHttp(json_result(malformed_access)),
            compiled_query=query,
            page_size=5,
            order="relevance",
            cursor="*",
            searched_scope="literature",
            source_collections=["MED"],
        )
    )
    check(
        out["stages"]["result"] == "inconsistent"
        and out["failures"][0]["code"] == "provider_schema_drift",
        "malformed Europe PMC access metadata escaped structured handling",
    )

    invalid_cursor_payload = search_payload(
        hit_count=2,
        rows=[europe_pmc_row(id_="44444444")],
        provider_query=provider_query,
        cursor="*",
        page_size=1,
        next_cursor="\ud800",
    )
    out = run(
        europe_pmc.map_records(
            FakeHttp(json_result(invalid_cursor_payload)),
            compiled_query=query,
            page_size=1,
            order="relevance",
            cursor="*",
            searched_scope="literature",
            source_collections=["MED"],
        )
    )
    check(
        out["failures"][0]["code"] == "provider_schema_drift",
        "a cursor that cannot be encoded on the wire was accepted",
    )

    # 2. Exact retrieval can preserve the bounded full abstract ---------------
    long_abstract = "A" * 700
    http = FakeHttp(
        json_result(
            search_payload(
                hit_count=1,
                rows=[
                    europe_pmc_row(
                        id_="11111111",
                        abstractText=long_abstract,
                        firstIndexDate="2025-01-02",
                    )
                ],
                provider_query=provider_query,
                cursor="*",
                page_size=2,
            )
        )
    )
    detailed = run(
        europe_pmc.map_records(
            http,
            compiled_query=query,
            page_size=2,
            order="relevance",
            cursor="*",
            searched_scope="exact_identifier",
            source_collections=["MED"],
            include_abstract=True,
            exact_record=True,
        )
    )
    check(
        detailed["candidates"][0]["abstract"] == long_abstract,
        "exact retrieval truncated the bounded Europe PMC abstract",
    )
    check(
        detailed["candidates"][0]["date_role"] == "first_index_date",
        "firstIndexDate was mislabeled as a publication date",
    )
    check(
        not any(
            item["code"] == "candidate_text_is_triage_only"
            for item in detailed["warnings"]
        ),
        "exact retrieval retained the search-excerpt warning",
    )

    authors = [
        {"fullName": f"Author {index}"}
        for index in range(1, 7)
    ]
    http = FakeHttp(
        json_result(
            search_payload(
                hit_count=1,
                rows=[
                    europe_pmc_row(
                        id_="11111111",
                        authorList={"author": authors},
                        abstractText="Available but omitted.",
                    )
                ],
                provider_query=provider_query,
                cursor="*",
                page_size=2,
            )
        )
    )
    metadata_only = run(
        europe_pmc.map_records(
            http,
            compiled_query=query,
            page_size=2,
            order="relevance",
            cursor="*",
            searched_scope="exact_identifier",
            source_collections=["MED"],
            include_abstract=False,
            exact_record=True,
        )
    )
    check(
        len(metadata_only["candidates"][0]["contributors"]) == 6,
        "metadata-only exact retrieval truncated contributors",
    )
    check(
        metadata_only["candidates"][0]["abstract_state"] == "not_requested"
        and "abstract" not in metadata_only["candidates"][0],
        "metadata-only exact retrieval misstated an omitted abstract",
    )

    # 3. Query/source filter is applied exactly once --------------------------
    echoed_query = out["effective_request"]["fields"][0]
    check(echoed_query["name"] == "query", "first effective_request field is not the query")
    check(
        echoed_query["value"].count("SRC:MED") == 1,
        "the source filter must be applied to the query exactly once",
    )
    two_collections_query = expected_provider_query(query, ["MED", "PMC"])
    check(
        two_collections_query.count(" AND ") == 1 and two_collections_query.count("SRC:") == 2,
        "multiple collections must combine into one AND clause with one SRC: per collection",
    )

    # 4. A source collection is always required ------------------------------
    http = FakeHttp()
    out = run(
        europe_pmc.map_records(
            http,
            compiled_query=query,
            page_size=5,
            order="relevance",
            cursor="*",
            searched_scope="literature",
            source_collections=[],
        )
    )
    check(
        out["failures"][0]["code"] == "invalid_request"
        and out["failures"][0]["scope"] == "source_collections",
        "empty source_collections must be rejected",
    )
    check(len(http.calls) == 0, "a rejected request must never reach the transport")

    # 4. Exact request-echo checks --------------------------------------------
    def page(**overrides: Any) -> dict[str, Any]:
        base = dict(
            hit_count=1,
            rows=[europe_pmc_row()],
            provider_query=provider_query,
            cursor="*",
            page_size=5,
        )
        base.update(overrides)
        return search_payload(**base)

    for label, payload in (
        ("query", page(provider_query=provider_query + " EXTRA")),
        ("cursor", page(extra={"request": {"cursorMark": "WRONG"}})),
        ("page_size", page(page_size=999)),
        ("sort", page(sort="WRONG SORT")),
        ("synonym", page(synonym=True)),
        ("result_type", page(result_type="lite")),
        (
            "result_type_null",
            page(extra={"request": {"resultType": None}}),
        ),
    ):
        http = FakeHttp(json_result(payload))
        out = run(
            europe_pmc.map_records(
                http,
                compiled_query=query,
                page_size=5,
                order="relevance",
                cursor="*",
                searched_scope="literature",
                source_collections=["MED"],
            )
        )
        check(
            out["failures"]
            and out["failures"][0]["code"] == "query_fidelity_unknown",
            f"an incorrect echoed {label} must be rejected as query_fidelity_unknown",
        )

    http = FakeHttp(json_result(page(result_type=None)))
    out = run(
        europe_pmc.map_records(
            http,
            compiled_query=query,
            page_size=5,
            order="relevance",
            cursor="*",
            searched_scope="literature",
            source_collections=["MED"],
        )
    )
    check(
        out["stages"]["result"] == "succeeded"
        and out["query_observation"]["projection_echo"] is None
        and out["query_observation"]["projection_echo_state"] == "omitted"
        and http.calls[0]["params"]["resultType"] == "core",
        "an omitted Europe PMC projection echo made a core response unusable",
    )

    # 5. HTTP-200 errCode --------------------------------------------------------
    http = FakeHttp(
        json_result(
            {
                "version": "6.9",
                "errCode": 1,
                "errMsg": "invalid query syntax",
            }
        )
    )
    out = run(
        europe_pmc.map_records(
            http,
            compiled_query=query,
            page_size=5,
            order="relevance",
            cursor="*",
            searched_scope="literature",
            source_collections=["MED"],
        )
    )
    check(
        out["failures"][0]["code"] == "provider_error_body",
        "an HTTP-200 errCode body must be reported as provider_error_body",
    )

    deep_json = HttpResult(
        status=200,
        body=b"[" * 20_000 + b"0" + b"]" * 20_000,
        content_type="application/json",
        headers={},
        network_bytes=40_001,
    )
    out = run(
        europe_pmc.map_records(
            FakeHttp(deep_json),
            compiled_query=query,
            page_size=5,
            order="relevance",
            cursor="*",
            searched_scope="literature",
            source_collections=["MED"],
        )
    )
    check(
        out["failures"][0]["code"] == "parser_limit_exceeded",
        "deep Europe PMC JSON must become a structured parser-limit failure",
    )

    for invalid_count in ("²", "9" * 5_000):
        payload = search_payload(
            hit_count=0,
            rows=[],
            provider_query=provider_query,
            cursor="*",
            page_size=5,
        )
        payload["hitCount"] = invalid_count
        out = run(
            europe_pmc.map_records(
                FakeHttp(json_result(payload)),
                compiled_query=query,
                page_size=5,
                order="relevance",
                cursor="*",
                searched_scope="literature",
                source_collections=["MED"],
            )
        )
        check(
            out["failures"][0]["code"] == "provider_schema_drift",
            "an unsafe Europe PMC integer escaped structured provider failure",
        )

    nested: Any = "signal"
    for _ in range(80):
        nested = {"child": nested}
    parseable_deep = search_payload(
        hit_count=0,
        rows=[],
        provider_query=provider_query,
        cursor="*",
        page_size=5,
        extra={"nested": nested},
    )
    out = run(
        europe_pmc.map_records(
            FakeHttp(json_result(parseable_deep)),
            compiled_query=query,
            page_size=5,
            order="relevance",
            cursor="*",
            searched_scope="literature",
            source_collections=["MED"],
        )
    )
    check(
        out["failures"][0]["code"] == "parser_limit_exceeded",
        "parseable deep Europe PMC JSON escaped structural limits",
    )

    # 6. Valid zero ----------------------------------------------------------
    http = FakeHttp(
        json_result(
            search_payload(
                hit_count=0,
                rows=[],
                provider_query=provider_query,
                cursor="*",
                page_size=5,
            )
        )
    )
    out = run(
        europe_pmc.map_records(
            http,
            compiled_query=query,
            page_size=5,
            order="relevance",
            cursor="*",
            searched_scope="literature",
            source_collections=["MED"],
        )
    )
    check(out["stages"]["result"] == "valid_zero", "a zero-hit first page must be valid_zero")
    check(out["count_observation"]["hit_count"] == 0, "hit_count must be represented as zero")

    # 7. Invalid cursor / version-only currentness ----------------------------
    http = FakeHttp()
    out = run(
        europe_pmc.map_records(
            http,
            compiled_query=query,
            page_size=5,
            order="relevance",
            cursor="",
            searched_scope="literature",
            source_collections=["MED"],
        )
    )
    check(
        out["failures"][0]["code"] == "invalid_request"
        and out["failures"][0]["scope"] == "cursor",
        "an empty cursor must be rejected before any transport call",
    )
    check(len(http.calls) == 0, "an invalid cursor must never reach the transport")

    http = FakeHttp(
        json_result(
            search_payload(
                hit_count=1,
                rows=[europe_pmc_row()],
                provider_query=provider_query,
                cursor="*",
                page_size=5,
                version="6.9",
            )
        )
    )
    out = run(
        europe_pmc.map_records(
            http,
            compiled_query=query,
            page_size=5,
            order="relevance",
            cursor="*",
            searched_scope="literature",
            source_collections=["MED"],
        )
    )
    check(
        any(
            o["role"] == "provider_version" and o["value"] == "6.9"
            for o in out["currentness"]["observations"]
        ),
        "a represented provider version must appear in currentness observations",
    )

    # 8. Continuation ----------------------------------------------------------
    http = FakeHttp(
        json_result(
            search_payload(
                hit_count=10,
                rows=[europe_pmc_row(id_="33333333")],
                provider_query=expected_provider_query(query, ["MED"]),
                cursor="CURSOR1",
                page_size=1,
                next_cursor="CURSOR2",
            )
        )
    )
    out = run(
        europe_pmc.map_records(
            http,
            compiled_query=query,
            page_size=1,
            order="relevance",
            cursor="CURSOR1",
            searched_scope="literature",
            source_collections=["MED"],
        )
    )
    check(
        out["continuation_observation"]["state"] == "available",
        "an advanced cursor must be represented as an available continuation",
    )
    check(
        out["continuation_observation"]["next_cursor"] == "CURSOR2",
        "the exact next cursor must be represented",
    )

    http = FakeHttp(
        json_result(
            search_payload(
                hit_count=1,
                rows=[europe_pmc_row(id_="44444444")],
                provider_query=provider_query,
                cursor="*",
                page_size=1,
                next_cursor="*",
            )
        )
    )
    out = run(
        europe_pmc.map_records(
            http,
            compiled_query=query,
            page_size=1,
            order="relevance",
            cursor="*",
            searched_scope="literature",
            source_collections=["MED"],
        )
    )
    check(
        out["continuation_observation"]["state"] == "exhausted",
        "a next cursor identical to the requested cursor must be terminal",
    )

    http = FakeHttp(
        json_result(
            search_payload(
                hit_count=10,
                rows=[europe_pmc_row(id_="44444445")],
                provider_query=provider_query,
                cursor="CURSOR1",
                page_size=1,
                next_cursor="CURSOR1",
            )
        )
    )
    out = run(
        europe_pmc.map_records(
            http,
            compiled_query=query,
            page_size=1,
            order="relevance",
            cursor="CURSOR1",
            searched_scope="literature",
            source_collections=["MED"],
            start_position=1,
        )
    )
    check(
        out["continuation_observation"]["state"] == "unavailable"
        and any(
            item["code"] == "pagination_boundary"
            for item in out["failures"]
        ),
        "a repeated later-page cursor silently truncated remaining hits",
    )

    # 9. Absolute positions via start_position ---------------------------------
    http = FakeHttp(
        json_result(
            search_payload(
                hit_count=52,
                rows=[
                    europe_pmc_row(id_="55555555"),
                    europe_pmc_row(id_="66666666"),
                ],
                provider_query=provider_query,
                cursor="*",
                page_size=5,
            )
        )
    )
    out = run(
        europe_pmc.map_records(
            http,
            compiled_query=query,
            page_size=5,
            order="relevance",
            cursor="*",
            searched_scope="literature",
            source_collections=["MED"],
            start_position=50,
        )
    )
    check(
        [c["absolute_position"] for c in out["candidates"]] == [50, 51],
        "start_position must offset absolute_position for every represented candidate",
    )

    # 10. Outside requested collection rejection --------------------------------
    http = FakeHttp(
        json_result(
            search_payload(
                hit_count=1,
                rows=[europe_pmc_row(source="PMC", id_="PMC77777777")],
                provider_query=provider_query,
                cursor="*",
                page_size=5,
            )
        )
    )
    out = run(
        europe_pmc.map_records(
            http,
            compiled_query=query,
            page_size=5,
            order="relevance",
            cursor="*",
            searched_scope="literature",
            source_collections=["MED"],
        )
    )
    check(
        out["failures"][0]["code"] == "query_fidelity_unknown",
        "a result outside the requested collections must be rejected",
    )
    check(
        "outside the requested collections" in out["failures"][0]["detail"],
        "the rejection detail must name the collection boundary",
    )

    # 11. Malformed source identities are rejected ---------------------------
    http = FakeHttp(
        json_result(
            search_payload(
                hit_count=1,
                rows=[europe_pmc_row(source="MED", id_="PMC123")],
                provider_query=provider_query,
                cursor="*",
                page_size=5,
            )
        )
    )
    out = run(
        europe_pmc.map_records(
            http,
            compiled_query=query,
            page_size=5,
            order="relevance",
            cursor="*",
            searched_scope="literature",
            source_collections=["MED"],
        )
    )
    check(
        out["stages"]["result"] == "inconsistent"
        and out["failures"][0]["code"] == "provider_schema_drift",
        "malformed MED identity was accepted",
    )

    # 12. Unexpected internal exceptions are not advertised as retryable ------
    out = run(
        europe_pmc.map_records(
            FakeHttp(RuntimeError("internal transport contract failure")),
            compiled_query=query,
            page_size=5,
            order="relevance",
            cursor="*",
            searched_scope="literature",
            source_collections=["MED"],
        )
    )
    check(
        out["failures"][0]["code"] == "connection_failure"
        and out["failures"][0]["retryable"] is False,
        "an internal Europe PMC adapter exception was marked retryable",
    )

    # 13. AttemptBudgetExceeded propagates -----------------------------------
    http = FakeHttp(AttemptBudgetExceeded("attempt ceiling exhausted"))
    try:
        run(
            europe_pmc.map_records(
                http,
                compiled_query=query,
                page_size=5,
                order="relevance",
                cursor="*",
                searched_scope="literature",
                source_collections=["MED"],
            )
        )
    except AttemptBudgetExceeded:
        check(True, "AttemptBudgetExceeded propagated")
    else:
        check(False, "AttemptBudgetExceeded must not be swallowed by map_records")


def main() -> int:
    check_pubmed_map()
    check_pubmed_qualify()
    check_europe_pmc_map()
    # Parsed by contract_map_check.py, which looks for a line containing
    # "assertions passed" and reads passed/total off it. This harness FAILS FAST -- a
    # failing assertion raises out of main() and this line never prints at all -- so
    # reached and passed are the same number by construction. The total is pinned in
    # contract_map_check.py's EXTERNAL_HARNESSES, deliberately in ONE place: pinning a
    # number twice is only as good as the arithmetic beside it, and that has been wrong
    # here twice.
    print(f"{CHECKS}/{CHECKS} assertions passed  (adapter)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
