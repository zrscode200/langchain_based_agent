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
# Pins mirror `ics_muse/server.py`: `load_server()` execs that module, so its imports must
# resolve. Without this block the documented invocation exits 1 with ModuleNotFoundError and
# ZERO assertion output -- which reads as "assertions failed".
"""Assert wave 4 says only what the response supports, and never more.

    ./tools/response_assembly_check.py

NO NETWORK AND NO TOKEN. Response bodies are stubbed from shapes measured live.

Wave 3's discipline was "do not send a wrong request". This wave's is "do not make a claim
the response does not support", which is a different failure surface: the API's envelope
contains two fields that actively mislead.

  `synonyms`          on the conditional engine describes an expansion that did NOT happen,
                      byte-identical to what basic returns and applies. Trusting it means
                      believing six aliases were searched when one term was.
  `datasourceErrors`  is `[]` even for a DENIED source returning 0 records, so it can never
                      attribute a zero.

And one that means less than it looks: `FULLTEXT_ZERO_RESULTS_*` says the query was relaxed
and nothing about whether the relaxation found anything -- measured, a token appearing nowhere
returned 2 hits under exactly those strategies while a longer one returned 0 under the same
two.

EVERY ASSERTION TESTS AN EFFECT. Sections 20 and 21 sabotage every guard and then mutate the
module in-process, because all three prior waves shipped assertions that passed while testing
nothing, and wave 3 additionally shipped a fix that looked applied and did nothing.

Exit status is 0 only if every assertion holds.
"""

from __future__ import annotations

import asyncio
import copy
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from startup_cache_check import (  # noqa: E402  - reuse wave 1's stubs, don't fork them
    IN_SCOPE,
    facets_body,
    ingest_body,
    UPDATES_BODY,
    install_stubs,
    load_server,
)

FAILURES: list[str] = []
CHECKS = 0
_PRISTINE = None

# Every assertion this script is expected to run. Guards can otherwise vanish -- a `continue`
# that skips a source, or a conditionally-registered check -- and the summary still prints
# "N/N passed" with nothing saying N shrank.
EXPECTED_CHECKS = None   # set after the first clean run; see the tail of main()


def check(label: str, ok: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{': ' + detail if detail else ''}")
    if not ok:
        FAILURES.append(label)


def g(obj: Any, *path: Any) -> Any:
    """Walk nested structure, None on any miss -- so a removed key FAILS one assertion
    rather than raising and taking every later assertion with it."""
    for key in path:
        if isinstance(obj, dict):
            obj = obj.get(key)
        elif isinstance(obj, (list, tuple)) and isinstance(key, int) and -len(obj) <= key < len(obj):
            obj = obj[key]
        else:
            return None
        if obj is None:
            return None
    return obj


# --------------------------------------------------------------------------
# Response bodies, shaped from live measurement
# --------------------------------------------------------------------------
TAXO_OOS = "L2|aaaaaaaa-0000-0000-0000-000000000001|eeeeeeee-0000-0000-0000-000000000005|cccccccc-0000-0000-0000-000000000003"
TAXO_UNKNOWN = "L1|aaaaaaaa-0000-0000-0000-000000000001|ffffffff-dead-beef-0000-000000000099"


def card(n: int, source: str, *, knn: bool = False, no_locator: bool = False) -> dict[str, Any]:
    """One card. Shapes measured: highlight key `text` on four sources and NONE on scited;
    `textSize` at card level in camelCase; mrl_slides' `link` rendering `?link?`."""
    body: dict[str, Any] = {
        "title": f"{source} document {n}",
        "doc_status": "Effective",
        "document_id": f"DOC-{source}-{n}",
        "modified_date": "2025-06-15T00:00:00",
        "creation_date": "2024-01-02T00:00:00",
    }
    if source == "meds":
        # A REAL meds URL carries a query string. That is the input most likely to trip
        # `_UNINTERPOLATED` as a false positive and silently drop a real `source_fields`
        # value, and no fixture exercised it.
        body["webview_pdf_url"] = (
            f"https://webview.merck.com/webview/getContentById/CLS-{n}.pdf"
            "?version=CURRENT&format=pdf&DocbaseName=meds")
        # And meds' own `link` is a template that may arrive UNINTERPOLATED in the
        # curly-brace form -- the other half of `_UNINTERPOLATED`, which `?link?` alone
        # never exercised.
        if n == 1:
            body["link"] = "{webview_pdf_url}"
    elif source == "mrl_slides":
        # The measured trap: the template field comes back UNINTERPOLATED.
        body["link"] = "?link?"
        body["slide_source_url"] = f"https://slides.merck.com/slide/{n}"
        body["deck_url"] = f"https://slides.merck.com/deck/{n}"
    elif source == "med_comms":
        body["link"] = f"/resources/attachments/v1/DO-NOT-SHARE-THIS-URL/doc{n}"
        # A DOSE-PREFIXED title, because med_comms' locator template interpolates `{title}` and
        # `{muse_raw_id}` -- so both are tried as locator candidates before `link`. A plain title
        # is rejected by the value-shape test, which made the shape test look sufficient; a title
        # beginning "10." passes it, and only the field-name guard stops it. The wave-8 gate found
        # exactly this, and no fixture could show it while every title here read like prose.
        body["title"] = f"10.{n} mg tablet labelling update"
        body["muse_raw_id"] = f"{700000 + n}"
    else:
        body["link"] = f"https://example.internal/{source}/{n}"
    if no_locator:
        for k in ("link", "webview_pdf_url", "slide_source_url", "deck_url"):
            body.pop(k, None)
    out: dict[str, Any] = {
        "id": f"card-{source}-{n}",
        "title": body["title"],
        "body": body,
        "textSize": 7398,
        "relevance": 2373.0 - n,
        "restricted": False,
        "searchMethod": "KEYWORD",
    }
    # scited returns NO highlight map at all -- measured twice.
    if source != "scited":
        out["highlightFragments"] = {
            "text": [f'a fragment with <em class="hlt1">stability</em> in it, doc {n}']}
    if knn:
        # NO top-level `chunkScore`. Measured on a live KNN card: the top-level keys are
        # `@class, chunkData, dataSource, highlightFragments, id, matchingTerms, missingTerms,
        # queryId, relevance, restricted, searchMethod, textSize, title` -- neither `chunkScore`
        # nor `score` among them. Leaving one here would let `assemble_record` read it and hide
        # that the live path depends on the chunk and on `relevance` instead.
        out["searchMethod"] = "KNN"
        # A LIST, not a flat dict. Measured on a live KNN card 2026-08-26: `chunkData` is a
        # list, and the card carries NO top-level `chunkScore` and no `score` -- the cosine is
        # inside the chunk and, identically, on `relevance` (0.669722318649292 in both). The
        # flat-dict fixture is why wave 4 shipped `score: {"value": null, "kind": "chunkScore"}`
        # on every similarity record: the stub had a `chunkScore` at top level to read, and live
        # there is none. Fifth stub-fidelity gap of this exact shape.
        out["chunkData"] = [{"chunkText": f"the matching passage for {n}",
                             "chunkScore": 0.8368597, "chunkIndex": 3}]
        out["relevance"] = 0.8368597
    return out


def search_body(source: str, total: int, *, n_cards: int = 3, knn: bool = False,
                strategies: tuple[str, ...] = ("FULLTEXT_ENHANCERS",),
                no_locator: bool = False, downgraded: bool = False) -> dict[str, Any]:
    """The NINE-key envelope, identical on both endpoints.

    `downgraded` models the measured KNN SILENT DOWNGRADE: a semantic request to a source
    without vector support returns `totalHitsKnn: 0` WITH cards -- term matches under a
    similarity request. That shape is what the detector keys on, and no fixture produced it,
    so the detector could not be exercised offline at all.
    """
    if downgraded:
        return {
            "cards": [card(i, source, knn=False, no_locator=no_locator)
                      for i in range(1, n_cards + 1)],
            "datasourceErrors": [], "hints": [], "query": "", "queryId": "q-dg",
            "searchStrategy": list(strategies),
            "synonyms": {}, "totalHits": total, "totalHitsKnn": 0,
        }
    return {
        "cards": [card(i, source, knn=knn, no_locator=no_locator)
                  for i in range(1, n_cards + 1)] if total else [],
        # `[]` even for a DENIED source returning 0 -- it can never attribute a zero.
        "datasourceErrors": [],
        "hints": [],
        "query": "",
        "queryId": "q-1234",
        "searchStrategy": list(strategies),
        # Describes an expansion the conditional endpoint did NOT perform.
        # All SIX measured aliases (`02` section 10), not three. The docstring and a mutation
        # label both said "six" while the fixture carried half of them.
        "synonyms": {"MK-6070": ["unii-u6ckl88c8h", "B-000488104-000T", "gocatamig",
                                 "B-000488104", "cas-2851862-91-2", "hpn328"]},
        "totalHits": 0 if knn else total,
        "totalHitsKnn": total if knn else 0,
    }


def rich_facets(source: str, *, overflow: bool = False, multivalued: bool = False,
                taxonomy: bool = False, empty: bool = False) -> dict[str, Any]:
    if empty:
        return {"facets": {}}
    # Counts consistent with the population the test pairs them with, so the
    # partitions/multi-valued assertions test the LOGIC rather than a fixture mismatch.
    f: dict[str, Any] = {
        "doc_status.raw": [{"term": "Effective", "count": 19}],
        # A DATE facet: its terms are rendered years that return 0 as a condition.
        "creation_date.raw": [{"term": "2025", "count": 12},
                              {"term": "2024", "count": 7}],
    }
    if overflow:
        f["author.raw"] = [{"term": "A", "count": 3}, {"term": "B", "count": 2},
                           {"term": "+", "count": 60}]
    if multivalued:
        # Sums ABOVE the population -> multi-valued, does not partition.
        f["applicable_areas.raw"] = [{"term": "X", "count": 15},
                                     {"term": "Y", "count": 14}]
    if taxonomy:
        f["mmd_tag_issue.raw"] = [{"term": TAXO_OOS, "count": 53},
                                  {"term": TAXO_UNKNOWN, "count": 7}]
    return {"facets": f}


def install_search_stubs(m, *, plans: dict[str, Any] | None = None,
                         fail: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """Extend wave 1's stub with the search and facet endpoints it does not cover."""
    plans = plans or {}
    fail = fail or {}
    # Capture wave 1's PRISTINE stub once, not whatever stub is currently installed. Each call
    # previously captured the previous layer, so 27 layers nested and a `fail=` from one
    # section stayed live in an inner layer for every later section -- inert only because the
    # outer layer never delegated. `log` was built and returned and every one of 27 call sites
    # discarded it, so it is gone.
    global _PRISTINE
    if _PRISTINE is None:
        _PRISTINE = m._request
    base_stub = _PRISTINE

    async def stub(method: str, endpoint: str, *, payload=None, params=None):
        for frag, reason in fail.items():
            if frag in endpoint or frag in str((payload or {}).get(m.F_DATASOURCES, "")):
                return {"_ok": False, "_status": {"executed": False, "reason": reason}}
        ds = (payload or {}).get(m.F_DATASOURCES, ["?"])[0]
        if endpoint in (m.EP_QUERY, m.EP_CONDITIONAL):
            spec = plans.get(ds) or {}
            # The SUPERSET count call: query-scoped with no program filter. It describes a
            # LARGER population than the records (measured live on meds: 3,051 against 799),
            # and that difference is the entire point of labelling the fork. An earlier fixture
            # returned the same number for both, so the assertion that they differ could not
            # be exercised.
            if payload and m.F_QUERY in payload and not payload.get(m.F_FORM_PARAMS):
                return {"_ok": True, "_status": {"executed": True},
                        "_body": search_body(ds, spec.get("superset_total",
                                                          spec.get("total", 3) * 4),
                                             n_cards=1)}
            return {"_ok": True, "_status": {"executed": True},
                    "_body": search_body(ds, spec.get("total", 3),
                                         n_cards=spec.get("n_cards", 3),
                                         knn=spec.get("knn", False),
                                         strategies=spec.get("strategies",
                                                             ("FULLTEXT_ENHANCERS",)),
                                         no_locator=spec.get("no_locator", False),
                                         downgraded=spec.get("downgraded", False))}
        if endpoint == m.EP_FACETS:
            spec = plans.get(ds) or {}
            if spec.get("facet_fail"):
                return {"_ok": False, "_status": {"executed": False,
                                                  "reason": "invalid_query_or_filter"}}
            body = rich_facets(ds, **(spec.get("facets") or {}))
            # A `searchField` request is a DIFFERENT response, and the stub returned the bulk
            # one. Measured (`06-muse-facets.md` section 1): the response collapses to exactly
            # ONE facet field -- 7 fields to 1 -- and its terms are filtered PREFIX-ONLY, with a
            # mid-string substring matching nothing (`"Belt"` -> 2 terms, `"eltran"` -> 0).
            #
            # Without this the fixture returned every field's full term list for any prefix, so
            # neither the one-field collapse nor the no-match branch could be exercised offline
            # at all -- and the no-match branch is the one that distinguishes "no value starts
            # with this" from "this field has no values".
            sf = (payload or {}).get("searchField")
            if sf:
                prefix = ((payload or {}).get(m.F_FORM_PARAMS) or {}).get(sf) or [""]
                pfx = str(prefix[0] if isinstance(prefix, list) else prefix)
                terms = (m._first(body, m.R_FACETS) or {}).get(sf) or []
                kept = [t for t in terms
                        if str(t.get("term", "")).lower().startswith(pfx.lower())]
                return {"_ok": True, "_status": {"executed": True},
                        "_body": {**body, "facets": {sf: kept}}}
            return {"_ok": True, "_status": {"executed": True}, "_body": body}
        return await base_stub(method, endpoint, payload=payload, params=params)

    m._request = stub
    return None


def per(result: dict[str, Any], source: str) -> dict[str, Any]:
    """One source's result, or an empty dict.

    Never `next(p for p in ... if ...)`: a missing source raises StopIteration, the script dies
    with no assertion summary, and which guard broke is invisible. A missing source must fail
    ONE named assertion.
    """
    for entry in result.get("per_source") or []:
        if entry.get("source") == source:
            return entry
    return {}


def warm(m) -> dict[str, Any]:
    m._CACHE = None
    m._calls_used = 0
    m._started = m.time.monotonic()
    install_stubs(m)
    return asyncio.run(m.warm_cache("MK-6070"))


def run(m, cache, **kw) -> dict[str, Any]:
    return asyncio.run(m.run_population(cache, **kw))


def main() -> int:  # noqa: C901 - a linear assertion script, deliberately flat
    m = load_server()
    cache = warm(m)
    if not cache["ok"]:
        # A `check`, not a bare `assert`: an assert exits with NO assertion output, which reads
        # as "everything failed" rather than "the fixture could not be built".
        check("the stub cache warms", False, str(cache["degraded"]))
        print("\n0/1 assertions passed")
        return 1

    # ----------------------------------------------------------------------------
    print("1. the surfaced tool exists with exactly fifteen parameters")
    import inspect
    params = [p for p in inspect.signature(m.muse_population).parameters]
    expected = ["sources", "about", "where", "where_not", "since", "until", "population",
                "semantic", "question", "order", "depth", "offset", "fields",
                "distribution", "value_prefix"]
    check("muse_population is a registered tool", callable(m.muse_population))
    check("fifteen parameters, no sixteenth", params == expected, f"{len(params)}: {params}")
    check("it is annotated read-only",
          m._READ_ONLY.readOnlyHint is True and m._READ_ONLY.destructiveHint is False)

    # ----------------------------------------------------------------------------
    print("\n2. results are per source and never merged into one total")
    install_search_stubs(m, plans={s: {"total": 10 + i} for i, s in enumerate(IN_SCOPE)})
    r = run(m, cache, about="MK-6070")
    check("five sources answered", len(r["per_source"]) == 5, str(len(r["per_source"])))
    check("no aggregate total key on the result",
          not any(k in r for k in ("total", "totalHits", "totalHitsKnn", "count")),
          str(sorted(r)))
    counts = {p["source"]: g(p, "count", "records_matched") for p in r["per_source"]}
    # `len(set(...)) == 5` would pass even if the source-to-count mapping were SWAPPED.
    check("each source's count is its own, not another's",
          counts == {s: 10 + i for i, s in enumerate(IN_SCOPE)}, str(counts))
    check("the grouping rule is stated", "never merged" in r["grouping"], r["grouping"])
    check("sources_searched names them", sorted(r["sources_searched"]) == sorted(IN_SCOPE))

    # ----------------------------------------------------------------------------
    print("\n3. every count says what it counts and whether it is defensible")
    p0 = r["per_source"][0]
    check("count carries `counts_what`", bool(g(p0, "count", "counts_what")),
          str(g(p0, "count", "counts_what"))[:60])
    check("conditional counts are defensible", g(p0, "count", "defensible") is True)
    check("the engine is named on the count", g(p0, "count", "engine") == "conditional")

    print("\n3b. basic_knn returns totalHits 0 -- the real count is totalHitsKnn")
    install_search_stubs(m, plans={"meds": {"total": 334, "knn": True}})
    rk = run(m, cache, sources=["meds"], semantic=True, question="how is stability shown")
    pk = rk["per_source"][0]
    # Reporting `totalHits` here would report ZERO records for a search that found 334.
    check("the reported count is 334, not 0", g(pk, "count", "records_matched") == 334,
          str(g(pk, "count", "records_matched")))
    check("semantic counts are NOT defensible", g(pk, "count", "defensible") is False)
    check("  ... and it says why", "query rewriting" in (g(pk, "count",
                                                          "not_defensible_because") or ""))
    # `basic_knn` returns `totalHits: 0` REGARDLESS, so a bare `term_matches_also: 0` reads as
    # "nothing matched by term" when it is just how this engine always answers. The artifact is
    # explained instead of surfaced.
    check("a zero term count is explained, not emitted as a measurement",
          g(pk, "count", "term_matches_also") is None
          and "how it always answers" in (g(pk, "count", "term_matches_note") or ""),
          str(g(pk, "count", "term_matches_note"))[:70])
    check("the match kind is similarity", pk["match_kind"] == "similarity", pk["match_kind"])

    # ----------------------------------------------------------------------------
    print("\n4. every similarity match carries its passage")
    recs = pk["records"]
    check("records came back", bool(recs), str(len(recs)))
    check("each carries the passage that matched",
          all(g(x, "passage", "text") for x in recs), str(g(recs, 0, "passage")))
    check("the score is labelled a cosine similarity, not a relevance",
          g(recs, 0, "score", "kind") == "chunkScore", str(g(recs, 0, "score")))
    check("  ... and says it is not comparable with a term score",
          "not comparable" in (g(recs, 0, "score", "meaning") or ""))
    # And when the passage is missing, that must be STATED -- a KNN hit without it is
    # unverifiable.
    install_search_stubs(m, plans={"meds": {"total": 5, "knn": True}})
    orig_chunk = m._chunk
    m._chunk = lambda c: None
    rmiss = run(m, cache, sources=["meds"], semantic=True, question="q")
    m._chunk = orig_chunk
    check("a similarity match with NO passage says so",
          bool(g(rmiss, "per_source", 0, "records", 0, "passage_missing")),
          str(g(rmiss, "per_source", 0, "records", 0, "passage_missing"))[:60])

    print("\n4b. term matches are labelled with the other kind of score")
    install_search_stubs(m, plans={"meds": {"total": 799}})
    rt = run(m, cache, sources=["meds"], about="MK-6070")
    check("a term match's score is `relevance`",
          g(rt, "per_source", 0, "records", 0, "score", "kind") == "relevance")
    check("  ... labelled comparable only within source, query and engine",
          "only within this source" in
          (g(rt, "per_source", 0, "records", 0, "score", "meaning") or ""))

    # ----------------------------------------------------------------------------
    print("\n5. the API's `synonyms` map is never passed through unqualified")
    blob = repr(rt)
    check("the raw synonym list does not reach the model",
          "gocatamig" not in blob and "cas-2851862-91-2" not in blob)
    check("instead the result states no expansion was applied",
          "none applied" in (g(rt, "per_source", 0, "synonym_expansion") or ""),
          str(g(rt, "per_source", 0, "synonym_expansion"))[:60])
    check("  ... and that the API's own field is not to be believed",
          "not to be believed" in (g(rt, "per_source", 0, "synonym_expansion") or ""))

    # ----------------------------------------------------------------------------
    print("\n6. highlights are iterated and stripped; scited has none")
    check("the highlight fragment is present", bool(g(rt, "per_source", 0, "records", 0,
                                                     "why_it_matched")))
    check("markup is stripped", "hlt1" not in blob and "<em" not in blob)
    install_search_stubs(m, plans={s: {"total": 5} for s in IN_SCOPE})
    rall = run(m, cache, about="MK-6070")
    sc = per(rall, "scited")
    check("scited returns an empty highlight map, not a crash",
          g(sc, "records", 0, "why_it_matched") == {},
          str(g(sc, "records", 0, "why_it_matched")))
    md = per(rall, "meds")
    check("meds' highlight key is iterated, not hardcoded",
          "text" in (g(md, "records", 0, "why_it_matched") or {}))

    # ----------------------------------------------------------------------------
    print("\n7. no locator containing template markers is ever emitted")
    ms = per(rall, "mrl_slides")
    loc = g(ms, "records", 0, "locator")
    check("mrl_slides' `?link?` is NOT emitted", "?link?" not in repr(loc), str(loc))
    check("it fell back to a real locator", bool(g(loc, "value")), str(loc))
    # WAVE 8 replaced `fell_back_from: ["link"]` with `fell_back: {count, why}`. The list emitted
    # wire field names, and on med_comms one of them was `muse_raw_id` -- a retrieval identifier
    # `_RETRIEVAL_IDS` keeps out of records by KEY and could not see as a VALUE. That a fallback
    # happened, and why, is the finding; which field failed is mechanics `01` section 1e hides.
    check("  ... and reports THAT it fell back, with the reason",
          g(loc, "fell_back", "count") == 1
          and "template markers" in str(g(loc, "fell_back", "why") or ""),
          str(g(loc, "fell_back")))
    check("  ... without naming the wire field it fell back from",
          "link" not in repr(g(loc, "fell_back") or {}), str(g(loc, "fell_back")))
    check("the granularity is named -- a slide is not its deck",
          "slide" in (g(loc, "granularity") or ""), str(g(loc, "granularity")))
    mc = per(rall, "med_comms")
    check("med_comms' relative attachment path carries its caution",
          "not a browser" in (g(mc, "records", 0, "locator", "caution") or ""),
          str(g(mc, "records", 0, "locator", "caution")))
    check("no `?...?` anywhere in the whole result", "?link?" not in repr(rall))
    install_search_stubs(m, plans={"mrl_slides": {"total": 3, "no_locator": True}})
    rnl = run(m, cache, sources=["mrl_slides"], about="MK-6070")
    check("with NO resolvable locator, it refuses rather than inventing one",
          g(rnl, "per_source", 0, "records", 0, "locator", "value") is None
          and bool(g(rnl, "per_source", 0, "records", 0, "locator", "refused")),
          str(g(rnl, "per_source", 0, "records", 0, "locator", "refused"))[:60])

    # ----------------------------------------------------------------------------
    print("\n8. card.id never reaches the model; a document handle does")
    check("no raw card id in the result", "card-meds-1" not in repr(rall))
    doc = g(md, "records", 0, "document")
    check("a document handle is emitted", isinstance(doc, str) and doc.startswith("d1."),
          str(doc)[:30])
    decoded, err = m.decode_document_handle(doc)
    check("  ... and it round-trips to the real id",
          err is None and decoded.get("id") == "card-meds-1", str(err or decoded))

    # ----------------------------------------------------------------------------
    print("\n9. the distribution states completeness, partitioning, and conditionability")
    install_search_stubs(m, plans={"med_comms": {
        "total": 19, "facets": {"overflow": True, "multivalued": True}}})
    rd = run(m, cache, sources=["med_comms"], about="MK-6070", distribution=True)
    dist = g(rd, "per_source", 0, "distribution")
    check("a distribution was assembled", bool(g(dist, "fields")), str(sorted(dist or {})))
    check("a field with no `+` is marked complete",
          g(dist, "fields", "doc_status", "complete") is True,
          str(g(dist, "fields", "doc_status")))
    check("a field WITH a `+` is marked truncated",
          g(dist, "fields", "author", "complete") is False
          and bool(g(dist, "fields", "author", "truncated")),
          str(g(dist, "fields", "author"))[:70])
    check("  ... and the `+` is not emitted as a value",
          "+" not in [v["value"] for v in (g(dist, "fields", "author", "values") or [])])
    check("a DATE facet is marked NOT usable as an exact condition",
          g(dist, "fields", "creation_date", "usable_as_exact_condition") is False,
          str(g(dist, "fields", "creation_date", "usable_as_exact_condition")))
    check("  ... and says why (rendered years return 0 silently)",
          "rendered years" in (g(dist, "fields", "creation_date", "not_conditionable") or ""))
    check("a multi-valued field is flagged as not partitioning the population",
          g(dist, "fields", "applicable_areas", "partitions_population") is False,
          str(g(dist, "fields", "applicable_areas", "multi_valued"))[:70])
    check("a single-valued field IS marked as partitioning",
          g(dist, "fields", "doc_status", "partitions_population") is True)

    # ----------------------------------------------------------------------------
    print("\n10. taxonomy L-paths decode, and the undecodable are kept and labelled")
    install_search_stubs(m, plans={"meds": {"total": 799, "facets": {"taxonomy": True}}})
    rtx = run(m, cache, sources=["meds"], distribution=True)
    dtx = g(rtx, "per_source", 0, "distribution")
    vals = [v["value"] for v in (g(dtx, "fields", "issue", "values") or [])]
    check("the taxonomy field is keyed by its ROLE, not `mmd_tag_issue`",
          "issue" in (g(dtx, "fields") or {}), str(sorted((g(dtx, "fields") or {}))))
    check("a known L-path decodes to a readable path",
          any(">" in v and "Out of Specification" in v for v in vals), str(vals))
    check("no raw L-path is shown for a decodable value",
          not any(v.startswith("L2|") for v in vals), str(vals))
    check("an UNDECODABLE value is kept and labelled, not dropped",
          any("<unnamed taxonomy value>" in v for v in vals), str(vals))
    check("  ... and counted, because it is the only drift signal",
          g(dtx, "undecodable_taxonomy_values") == 1,
          str(g(dtx, "undecodable_taxonomy_values")))
    check("the axis is labelled assigned-tag, not text-match",
          "assigned" in (g(dtx, "fields", "issue", "axis") or ""))

    # ----------------------------------------------------------------------------
    print("\n11. the meds fork -- the distribution's population is labelled and both counts given")
    check("meds' distribution scope is `superset`", g(dtx, "scope") == "superset",
          str(g(dtx, "scope")))
    check("both counts are reported", bool(g(dtx, "both_counts")), str(g(dtx, "both_counts")))
    check("  ... and the records' population differs from the faceted one",
          isinstance(g(dtx, "records_population"), int)
          and g(dtx, "population_described") != g(dtx, "records_population"),
          f"{g(dtx, 'population_described')} vs {g(dtx, 'records_population')}")
    check("the poison reason is stated", "computes a facet for" in (g(dtx, "poison") or ""),
          str(g(dtx, "poison"))[:70])
    # And the field NAME is a mechanic to hide -- the explanation must not name it.
    check("  ... without naming the program filter field",
          "product_name" not in (g(dtx, "poison") or ""), str(g(dtx, "poison"))[:70])
    # With conditions, meds cannot be faceted at all -- and must SAY it cannot enumerate.
    install_search_stubs(m, plans={"meds": {"total": 106}})
    rcond = run(m, cache, sources=["meds"], about="MK-6070",
                where=[{"in": ["anywhere"], "any_of": ["stability"]}], distribution=True)
    dc = g(rcond, "per_source", 0, "distribution")
    check("a conditioned meds population gets NO distribution",
          not g(dc, "fields"), str(g(dc, "fields")))
    check("  ... and says the source cannot enumerate its values",
          "cannot enumerate" in (g(dc, "unavailable") or ""), str(g(dc, "unavailable"))[:70])
    check("  ... which is NOT phrased as an absence",
          "no values" not in (g(dc, "unavailable") or "").lower())

    # ----------------------------------------------------------------------------
    print("\n12. an empty facet reports its population count FIRST")
    install_search_stubs(m, plans={"med_comms": {"total": 0, "facets": {"empty": True}}})
    re0 = run(m, cache, sources=["med_comms"], about="nothing")
    d0 = g(re0, "per_source", 0, "distribution")
    check("an empty population is named as the cause",
          "population is empty" in (g(d0, "empty_because") or ""),
          str(g(d0, "empty_because")))
    check("  ... and the population count is carried", g(d0, "population_described") == 0,
          str(g(d0, "population_described")))
    install_search_stubs(m, plans={"med_comms": {"total": 19, "facets": {"empty": True}}})
    dne = g(run(m, cache, sources=["med_comms"], about="x"), "per_source", 0, "distribution")
    # Was `"..." not in (x or "")`, which passes when `empty_because` is absent entirely.
    check("a NON-empty population with no facets says so positively",
          "no facet field came back" in (g(dne, "empty_because") or ""),
          str(g(dne, "empty_because")))

    # ----------------------------------------------------------------------------
    print("\n13. a zero result never ships bare")
    install_search_stubs(m, plans={"meds": {"total": 0}})
    rz = run(m, cache, sources=["meds"], about="__ICS_W4_IMPOSSIBLE__")
    az = g(rz, "per_source", 0, "absence")
    check("an absence block is present", bool(az), str(sorted(az or {})))
    # REWRITTEN, wave 10 (R1), and this is the one collision the pre-wave sweep predicted. The
    # load-bearing "NOT proof" sentence exists ONLY on the fall-through that now requires the
    # program filter to be positively established -- and on meds it never can be, because
    # `product_name` is not among its computed facets. So this assertion, on meds, was asserting
    # the defect: a zero called an absence while its own `checks` said the filter was not ruled out.
    #
    # Both sides are now covered. meds must REFUSE the absence claim and name why; a source whose
    # filter evidence IS live must still make it, with the sentence intact. Asserting only the
    # refusal would let a connector that never claims absence anywhere pass.
    check("meds does NOT claim absence -- its filter literal cannot be established",
          g(az, "is_absence") is False and g(az, "cause") == "program_filter_unverified",
          f"is_absence={g(az, 'is_absence')}, cause={g(az, 'cause')}")
    check("  ... and the unresolved cause is named, not merely omitted",
          "broken_program_filter" in (g(az, "unresolved") or []), str(g(az, "unresolved")))
    install_search_stubs(m, plans={"med_comms": {"total": 0}})
    az_ok = g(run(m, cache, sources=["med_comms"], about="__ICS_W4_IMPOSSIBLE__"),
              "per_source", 0, "absence")
    check("a source WITH live filter evidence does claim absence",
          g(az_ok, "is_absence") is True, f"is_absence={g(az_ok, 'is_absence')}")
    check("  ... and states this is NOT proof of nonexistence",
          "NOT proof" in (g(az_ok, "statement") or ""), str(g(az_ok, "statement"))[:60])
    install_search_stubs(m, plans={"meds": {"total": 0}})
    ro = g(az, "ruled_out") or []
    check("it names which zero-causes were ruled out", len(ro) >= 4, str(ro))
    check("  ... all from the measured six", set(ro) <= set(m.ZERO_CAUSES), str(ro))
    check("silent widening is ruled out (refused at construction)",
          "silent_widening" in ro)
    check("a denied datasource is ruled out from the access map",
          "denied_datasource" in ro)
    check("the index date travels with the absence, WITH a value",
          bool(g(az, "index_date")), str(g(az, "index_date")))
    check("and the conditional engine's stronger claim is made",
          any("exactly as stated" in c for c in (g(az, "checks") or [])),
          str(g(az, "checks"))[-90:])

    print("\n13b. a DENIED source's zero is attributed to permission, not absence")
    denied = copy.deepcopy(cache)
    denied["sources"]["scited"]["searchable"] = False
    denied["sources"]["scited"]["access"] = "DENIED"
    rdn = run(m, denied, sources=["scited"], about="x")
    check("it is excluded before searching, with a reason",
          bool(rdn.get("excluded_before_searching")) or rdn.get("refused"),
          str(rdn.get("excluded_before_searching") or rdn.get("cause")))

    print("\n13c. relaxation is read WITH the hit count, never as an absence claim")
    install_search_stubs(m, plans={"meds": {
        "total": 0, "strategies": ("FULLTEXT_ZERO_RESULTS_PREFIX",
                                   "FULLTEXT_ZERO_RESULTS_FUZZINESS")}})
    rr = run(m, cache, sources=["meds"], semantic=True, question="impossible")
    ar = g(rr, "per_source", 0, "absence")
    txt = " ".join(g(ar, "checks") or [])
    check("relaxation is reported", "relaxed" in txt, txt[-90:])
    check("  ... explicitly as NEITHER stronger nor weaker",
          "neither strengthens" in txt, txt[-90:])
    install_search_stubs(m, plans={"meds": {
        "total": 0, "strategies": ("FULLTEXT_WRONG_SYNTAX_REMOVED",)}})
    rw = run(m, cache, sources=["meds"], semantic=True, question="bad(syntax")
    check("a MUTILATED query is not an absence at all",
          g(rw, "per_source", 0, "absence", "is_absence") is False,
          str(g(rw, "per_source", 0, "absence", "cause")))

    # ----------------------------------------------------------------------------
    print("\n14. per-source status is first-class -- four of five never reads as complete")
    install_search_stubs(m, plans={s: {"total": 5} for s in IN_SCOPE},
                         fail={"signals": "timeout"})
    rp = run(m, cache, about="MK-6070")
    ok = [p for p in rp["per_source"] if p["status"] == "ok"]
    bad = [p for p in rp["per_source"] if p["status"] != "ok"]
    check("four sources ok, one failed", len(ok) == 4 and len(bad) == 1,
          f"{len(ok)} ok, {len(bad)} failed")
    failed = bad[0] if bad else {}
    check("the failure is NOT reported as absence",
          failed.get("is_absence") is False, str(failed)[:70])
    check("  ... and says so in words", "not the same as" in (failed.get("note") or ""))
    check("a coverage caution is raised", bool(rp.get("coverage_caution")),
          str(rp.get("coverage_caution"))[:70])
    check("sources_not_searched names it with a reason",
          [x["source"] for x in rp["sources_not_searched"]] == ["signals"],
          str(rp["sources_not_searched"]))
    check("index freshness is per source, with a value or an explicit unknown",
          all(p.get("index_date") or p.get("index_date_unknown") for p in ok),
          str({p["source"]: p.get("index_date") for p in ok}))

    # ----------------------------------------------------------------------------
    print("\n15. our date filter reports BOTH counts and never drops undated records")
    install_search_stubs(m, plans={"meds": {"total": 799, "n_cards": 3}})
    rdf = run(m, cache, sources=["meds"], about="MK-6070",
              since="2026-01-01", until="2026-12-31")
    df = g(rdf, "per_source", 0, "date_filter")
    check("the filter is attributed to the connector", g(df, "applied_by") == "connector")
    check("MUSE's count is reported", g(df, "matched_by_muse") == 799,
          str(g(df, "matched_by_muse")))
    check("  ... alongside what survived OUR filter", g(df, "surviving_our_filter") == 0,
          str(g(df, "surviving_our_filter")))
    check("the two numbers are different, so both are needed",
          g(df, "matched_by_muse") != g(df, "surviving_our_filter"))
    check("and a zero caused by OUR filter is not absence",
          g(rdf, "per_source", 0, "absence", "cause") == "our_date_filter",
          str(g(rdf, "per_source", 0, "absence", "cause")))
    rdf2 = run(m, cache, sources=["meds"], about="MK-6070", since="2024-01-01")
    check("a window that keeps records reports both counts too",
          g(rdf2, "per_source", 0, "date_filter", "surviving_our_filter") == 3,
          str(g(rdf2, "per_source", 0, "date_filter")))

    # ----------------------------------------------------------------------------
    print("\n16. PLAN_INTERNAL_KEYS never reach the model")
    install_search_stubs(m, plans={s: {"total": 7} for s in IN_SCOPE})
    rfull = run(m, cache, about="MK-6070", distribution=True,
                value_prefix={"status": "Eff"})
    text = repr(rfull)
    for key in ("conditionQuery", "formParams", "fieldsToExtract", "queryType",
                "searchField", "dataSources"):
        check(f"the wire key `{key}` is absent from the result", key not in text)
    # `counts` appears in PROSE (`counts_what`, `both_counts`), so assert the wire SHAPE --
    # a dict mapping a `.raw` field to a depth integer -- rather than the bare word.
    check("no `counts` depth map is exposed", "'counts':" not in text and '"counts":' not in text)
    check("no endpoint path is exposed", "/resources/v2/" not in text)
    check("the program filter FIELD is not exposed",
          "product_name.raw" not in text and "primary_mkv_number1.raw" not in text)
    print("\n16b. retrieval identifiers never reach a record")
    # Indexed by SOURCE, not by position in IN_SCOPE. `rfull` passes `value_prefix`, which
    # excludes two sources, so positional indexing inspected `signals` under the label
    # `scited` and silently skipped `mrl_slides` and `signals` via `continue` -- and
    # `mrl_slides` is precisely the source the `?link?` leak came back through. "132/132" was
    # 132 of a possible 134, with nothing saying so.
    by_source = {p["source"]: p for p in rall["per_source"]}
    check("all five sources are present to inspect", sorted(by_source) == sorted(IN_SCOPE),
          str(sorted(by_source)))
    for src_name in IN_SCOPE:
        sf = g(by_source, src_name, "records", 0, "source_fields")
        check(f"{src_name}: a record was available to inspect", isinstance(sf, dict),
              type(sf).__name__)
        leaked = sorted(set(sf or {}) & m._RETRIEVAL_IDS)
        check(f"{src_name}: no retrieval id in source_fields", not leaked, str(leaked))
    check("_RETRIEVAL_IDS covers the measured leaks",
          {"id", "muse_raw_id", "document_id"} <= m._RETRIEVAL_IDS,
          str(sorted(m._RETRIEVAL_IDS)))

    # Every PLAN_INTERNAL_KEY, asserted against a REAL result rather than against the filter.
    # These two assertions used to be `len(PLAN_INTERNAL_KEYS) >= 6` -- which cannot fail while
    # the constant exists -- and a direct call to `_strip_internal`, which had zero call sites
    # in the connector from the day it was written. So the pair tested a constant and a
    # function nothing used, while the property they were named for went unasserted. Wave 7
    # deleted the filter; enforcement is by construction (`_execute_plan` builds a fresh dict),
    # and this is where that construction is checked.
    #
    # On the wire SHAPE, not the bare word: `conditions_as_asked` is a legitimate model-facing
    # key, and prose contains "search" and "facets". A leaked plan key appears as `'search':`.
    for key in sorted(m.PLAN_INTERNAL_KEYS):
        check(f"the plan-internal key `{key}` never reaches the model",
              f"'{key}':" not in text and f'"{key}":' not in text)

    # ----------------------------------------------------------------------------
    print("\n17. wave 3's labels all surface, and the ISID never does")
    p = rfull["per_source"][0]
    check("the engine is named", bool(p.get("engine")))
    check("field validation quality is stated", p.get("field_validation") in
          ("authoritative", "best-effort", "unavailable"), str(p.get("field_validation")))
    check("conditions are echoed in the tool's OWN vocabulary, not the wire's",
          all("fields" in c and "value" in c for c in (p.get("conditions_as_asked") or [])),
          str(p.get("conditions_as_asked"))[:80])
    check("a population handle is minted per source",
          isinstance(p.get("population"), str) and p["population"].startswith("p1."),
          str(p.get("population"))[:24])
    payload, perr = m.decode_population_handle(p["population"])
    check("  ... and it decodes to the population that was actually run",
          perr is None and payload["source"] == p["source"], str(perr))
    check("the transience rule is stated on the result",
          "creates no work" in (rfull.get("status_of_this_result") or ""))
    # Was `"user" not in text.lower().split('"user"')[0:1] or True` -- an `or True` on top of a
    # list-membership test against a single-element list. Unconditionally true twice over.
    check("no `user` key and no ISID-shaped value reaches the model",
          '"user"' not in text and "'user'" not in text and "isid" not in text.lower(),
          "found" if '"user"' in text else "clean")
    check("`queryValid` is never emitted", "queryValid" not in text)
    check("`statistics` is never emitted raw", '"statistics"' not in text)
    check("`datasourceErrors` is not passed through", "datasourceErrors" not in text)

    # WHICH FIELD ordered the result. `newest` and `oldest` are not inverses -- measured at
    # n=60, `date_desc` orders `modified_date` and `date_asc` orders `creation_date`, each
    # leaving the other unordered -- so "the newest five" is a different set depending on which
    # ran, and nothing else in the response says. Wave 3 set `ordered_by` on the PLAN and
    # asserted it there; wave 4 never carried it to the result, so the requirement stated at
    # `_SORTS` went unmet for three waves. Found because wave 7 wrote a parameter description
    # promising the result would name it. Asserted HERE, on the result, for that reason.
    install_search_stubs(m, plans={"meds": {"total": 7}})
    for order, expect_field in (("newest", "modified_date"), ("oldest", "creation_date")):
        rord = run(m, cache, sources=["meds"], about="x", order=order)
        check(f"order={order!r} names the field that ordered it: {expect_field}",
              g(rord, "per_source", 0, "ordered_by") == expect_field,
              str(g(rord, "per_source", 0, "ordered_by")))
    # Relevance emits no sort key at all, so the honest answer is None rather than the word
    # "relevance" -- which is not a wire value either (the measured one is `relevancy`).
    rrel = run(m, cache, sources=["meds"], about="x", order="relevance")
    check("order='relevance' reports no ordering field, rather than inventing one",
          g(rrel, "per_source", 0, "ordered_by") is None
          and "ordered_by" in (rrel["per_source"][0]),
          str(g(rrel, "per_source", 0, "ordered_by")))

    print("\n17b. boundary_strength and unresolved default fields surface")
    broad = copy.deepcopy(cache)
    broad["sources"]["signals"]["boundary_strength"] = "broad"
    rb = run(m, broad, sources=["signals"], about="MK-6070")
    check("a broad-code source cautions per record",
          bool(g(rb, "per_source", 0, "records", 0, "scope_caution")),
          str(g(rb, "per_source", 0, "records", 0, "scope_caution"))[:70])
    nofields = copy.deepcopy(cache)
    nofields["sources"]["meds"]["card_default_fields"] = [{"id": "nope"}]
    rnf = run(m, nofields, sources=["meds"], about="x")
    check("unresolved qualification fields are surfaced as a move",
          any("qualify" in x for x in (g(rnf, "per_source", 0, "next_moves") or [])),
          str(g(rnf, "per_source", 0, "next_moves"))[:90])

    # ----------------------------------------------------------------------------
    print("\n18. in-band guidance is result-conditional, and fills only its four slots")
    install_search_stubs(m, plans={"med_comms": {"total": 19}})
    small = run(m, cache, sources=["med_comms"], about="MK-6070")
    check("a SMALL result gets no narrowing hint",
          not any("population, not an answer" in x
                  for x in (g(small, "per_source", 0, "next_moves") or [])),
          str(g(small, "per_source", 0, "next_moves")))
    install_search_stubs(m, plans={"med_comms": {"total": 5000}})
    big = run(m, cache, sources=["med_comms"], about="MK-6070", distribution=True)
    mv = g(big, "per_source", 0, "next_moves") or []
    check("a LARGE result gets one", any("population, not an answer" in x for x in mv),
          str(mv)[:80])
    check("  ... and offers real values from the distribution it already holds",
          any("real values to narrow on" in x for x in mv), str(mv)[:120])
    check("the superset case warns that shares are about the larger set",
          any("DIFFERENT populations" in x
              for x in (g(rtx, "per_source", 0, "next_moves") or [])),
          str(g(rtx, "per_source", 0, "next_moves"))[:90])
    check("the semantic case tells you to read the passage first",
          any("Read the passage" in x for x in (g(rk, "per_source", 0, "next_moves") or [])),
          str(g(rk, "per_source", 0, "next_moves"))[:90])

    # ----------------------------------------------------------------------------
    print("\n18b. a facet failure must not lose the records that succeeded")
    install_search_stubs(m, plans={"med_comms": {"total": 19, "facet_fail": True}})
    rff = run(m, cache, sources=["med_comms"], about="MK-6070", distribution=True)
    check("the records survive a failed facet call",
          g(rff, "per_source", 0, "records_returned") == 3,
          str(g(rff, "per_source", 0, "records_returned")))
    check("  ... and the distribution says it failed rather than reading as empty",
          bool(g(rff, "per_source", 0, "distribution", "failed")),
          str(g(rff, "per_source", 0, "distribution", "failed")))
    check("  ... and the count is still reported",
          g(rff, "per_source", 0, "count", "records_matched") == 19,
          str(g(rff, "per_source", 0, "count", "records_matched")))

    print("\n18c. the semantic downgrade is detected from the EFFECTIVE mode")
    # KNN silently downgrades on three of five sources. Deriving the lane from the request
    # alone produced a count of 0 beside N records, all labelled cosine similarities.
    install_search_stubs(m, plans={"meds": {"total": 12, "downgraded": True, "n_cards": 3}})
    rdg = run(m, cache, sources=["meds"], semantic=True, question="stability")
    if not rdg.get("refused"):
        pdg = g(rdg, "per_source", 0)
        check("cards with no vector hits are relabelled TERM matches",
              pdg.get("match_kind") == "term", str(pdg.get("match_kind")))
        check("  ... the requested kind is still reported",
              pdg.get("requested_match_kind") == "similarity",
              str(pdg.get("requested_match_kind")))
        check("  ... and the downgrade is stated",
              "different question" in (pdg.get("semantic_downgraded") or ""),
              str(pdg.get("semantic_downgraded"))[:70])
        check("  ... so no record claims a cosine similarity",
              all(g(x, "score", "kind") == "relevance" for x in (pdg.get("records") or [])),
              str(g(pdg, "records", 0, "score", "kind")))

    print("\n19. records are always returned -- nothing truncates silently")
    install_search_stubs(m, plans={"meds": {"total": 799, "n_cards": 25}})
    rbig = run(m, cache, sources=["meds"], about="MK-6070")
    check("all retrieved records are returned",
          g(rbig, "per_source", 0, "records_returned") == 25,
          str(g(rbig, "per_source", 0, "records_returned")))
    # Was `repr(rbig).lower()[:2000]` -- 9% of a 22kB payload, so both halves were inert.
    check("nothing is withheld silently", "withheld" not in repr(rbig).lower(),
          "found `withheld`")
    check("when matched exceeds one call's ceiling, incompleteness is STATED",
          g(rbig, "per_source", 0, "retrieval_incomplete") is None,
          str(g(rbig, "per_source", 0, "retrieval_incomplete"))[:70])

    # ----------------------------------------------------------------------------
    print("\n19b. WAVE 8 -- what the group review gate found, each asserted at the RESULT")
    # Every one of these was found by the group gate, not by a per-wave gate, because each is a
    # claim the result makes rather than a request it builds. Where a per-wave harness had an
    # assertion at all, it asserted the plan.
    install_search_stubs(m, plans={s: {"total": 7} for s in IN_SCOPE})

    # -- value_prefix EXECUTES. The gap that let this ship: this file passed `value_prefix` in
    # sections 16 and 20 for the express purpose of asserting the key was absent from the
    # result, so the harness affirmatively locked in a parameter that never ran.
    rvp = run(m, cache, sources=["meds"], about="MK-6070", value_prefix={"status": "Eff"},
              distribution=False)
    vp = g(rvp, "per_source", 0, "value_prefix")
    check("value_prefix EXECUTES and reaches the result", isinstance(vp, list) and bool(vp),
          str(type(vp).__name__))
    check("  ... the field is named as a ROLE, never the .raw wire name",
          g(vp, 0, "field") == "status", str(g(vp, 0, "field")))
    # THE VALUE, not its type. This was `isinstance(g(vp, 0, "values"), list)`, and the wave-8
    # gate broke it by mutation: reading `t.get("NOT_A_KEY")` instead of `t.get("term")` shipped
    # `[{"value": null, "count": 19}]`, and reading no terms at all shipped `[]` beside a
    # confident `empty_because` about the prefix -- and the suite reported 178/178 in BOTH cases.
    # `[]` is a list, and `complete` is `not any(term == "+")`, which an empty list satisfies. The
    # assertions described the payload's shape while the flagship fix of the wave went untested.
    check("  ... the value itself comes back, matched to its count",
          g(vp, 0, "values") == [{"value": "Effective", "count": 19}],
          str(g(vp, 0, "values"))[:70])
    check("  ... and no emptiness is claimed when values were found",
          "empty_because" not in (g(vp, 0) or {}), str(g(vp, 0, "empty_because"))[:50])
    check("  ... with the multi-valued caveat, so a count is not read as a population share",
          "multi-valued" in str(g(vp, 0, "counts_caveat") or ""),
          str(g(vp, 0, "counts_caveat"))[:50])
    check("  ... and completeness is stated from the `+` bucket's absence",
          g(vp, 0, "complete") is True, str(g(vp, 0, "complete")))
    check("  ... and no .raw name rides along", ".raw" not in repr(vp))
    # 0 terms at HTTP 200 is indistinguishable from "no such field" unless said so.
    rvz = run(m, cache, sources=["meds"], about="MK-6070",
              value_prefix={"status": "ZZnotaprefix"}, distribution=False)
    vz = g(rvz, "per_source", 0, "value_prefix")
    check("an empty value list says it is about the PREFIX, not the field",
          "starts with" in str(g(vz, 0, "empty_because") or ""),
          str(g(vz, 0, "empty_because"))[:60])
    # A source that cannot serve the role keeps its records. Before wave 8 it was excluded from
    # the SEARCH, so `value_prefix` cost two of five sources' counts.
    rall = run(m, cache, value_prefix={"status": "Eff"}, distribution=False)
    searched = rall.get("sources_searched") or []
    check("a value_prefix a source cannot serve does NOT cost its records",
          sorted(searched) == sorted(IN_SCOPE), str(sorted(searched)))
    unfillable = [p for p in rall["per_source"] if p.get("value_prefix_unavailable")]
    check("  ... and that source says why the lookup did not happen",
          bool(unfillable) and all("still searched" in str(
              (p.get("value_prefix_unavailable") or {}).get("note") or "")
              for p in unfillable),
          f"{len(unfillable)} sources")

    # -- the locator shape guard. A template INPUT is not a locator.
    rloc = run(m, cache, about="MK-6070", distribution=False)
    locs = {p["source"]: g(p, "records", 0, "locator") for p in rloc["per_source"]}
    # LITERAL shapes, not `m._LOCATOR_SHAPES`. Testing against the same constant the
    # implementation uses cannot fail: neuter the constant and both sides move together. The
    # wave-8 gate demonstrated it -- setting `_LOCATOR_SHAPES = ("",)` was caught by exactly one
    # assertion in this block, and not this one.
    check("every resolved locator is a place, not a value",
          all(str((l or {}).get("value") or "").startswith(("http://", "https://", "/"))
              for l in locs.values() if (l or {}).get("value")),
          str({s: str((l or {}).get("value"))[:18] for s, l in locs.items()}))
    check("  ... and no locator field is `title`",
          all((l or {}).get("field") != "title" for l in locs.values()),
          str({s: (l or {}).get("field") for s, l in locs.items()}))

    # -- the program filter field is a mechanic to hide (`01` section 1e).
    for p in rloc["per_source"]:
        sf = g(p, "records", 0, "source_fields") or {}
        bare = {str(k).removesuffix(".raw")
                for k in (cache["sources"][p["source"]].get("program_filter") or {})}
        check(f"{p['source']}: the program filter field is not on the record",
              not (bare & set(sf)), str(sorted(bare & set(sf))))

    # -- the similarity score has a VALUE, not just a description.
    install_search_stubs(m, plans={"meds": {"total": 5, "knn": True}})
    rsim = run(m, cache, sources=["meds"], semantic=True, question="stability")
    if not rsim.get("refused"):
        sc = g(rsim, "per_source", 0, "records", 0, "score") or {}
        check("a similarity record's score carries its value",
              isinstance(sc.get("value"), (int, float)),
              f"{sc.get('kind')}={sc.get('value')}")
        check("  ... and it is the chunk's own score",
              sc.get("value") == g(rsim, "per_source", 0, "records", 0, "passage", "score"),
              str(sc.get("value")))

    # -- .raw wire names must not reach the model through `declared_but_absent`, because
    # passing one back into `where` strips `.raw` and silently degrades `exact` to `phrase`.
    install_search_stubs(m, plans={"meds": {"total": 7}})
    rdb = run(m, cache, sources=["meds"], distribution=True)
    dist = g(rdb, "per_source", 0, "distribution") or {}
    check("no .raw name anywhere in the distribution", ".raw" not in repr(dist),
          [t for t in repr(dist).split("'") if t.endswith(".raw")][:3])

    # ----------------------------------------------------------------------------
    print("\n20. SABOTAGE -- disable each guard; the covering behaviour must change")
    orig = {}

    def sab(name, value):
        orig[name] = getattr(m, name)
        setattr(m, name, value)

    def restore():
        for k, v in orig.items():
            setattr(m, k, v)
        orig.clear()

    def sabotage(label, name, value, probe, broken):
        """Sabotage inside try/finally, so a raising probe cannot leave the module broken.

        `mutate` already had this shape; section 20 did not, so a sabotaged `run()` that
        raised would leave `_UNINTERPOLATED` or `_HLT_MARKUP` broken for every later assertion.
        """
        sab(name, value)
        try:
            observed = probe()
            caught = broken(observed)
        except Exception as exc:
            caught, observed = False, f"raised {type(exc).__name__}: {exc}"
        finally:
            restore()
        check(f"SABOTAGE {label}", caught, str(observed)[:80])

    install_search_stubs(m, plans={"mrl_slides": {"total": 3}})
    sab("_UNINTERPOLATED", __import__("re").compile(r"^$"))
    bad_loc = run(m, cache, sources=["mrl_slides"], about="x")
    check("SABOTAGE the locator guard -> `?link?` is now emitted",
          "?link?" in repr(bad_loc),
          "emitted" if "?link?" in repr(bad_loc) else "STILL REFUSED -- guard not exercised")
    restore()

    sab("_HLT_MARKUP", __import__("re").compile(r"^$"))
    install_search_stubs(m, plans={"meds": {"total": 5}})
    bad_hl = run(m, cache, sources=["meds"], about="x")
    check("SABOTAGE the markup stripper -> `hlt1` now reaches the model",
          "hlt1" in repr(bad_hl))
    restore()

    install_search_stubs(m, plans={"meds": {"total": 799, "facets": {"taxonomy": True}}})
    cold = copy.deepcopy(cache)
    cold["taxonomy"]["uuid_to_path"] = {}
    bad_tx = run(m, cold, sources=["meds"], distribution=True)
    v2 = [v["value"] for v in
          (g(bad_tx, "per_source", 0, "distribution", "fields", "issue", "values") or [])]
    check("SABOTAGE the taxonomy tree -> every value is labelled undecodable, not silently raw",
          bool(v2) and all("<unnamed taxonomy value>" in v for v in v2), str(v2)[:80])

    # AC14's real guard is that wire vocabulary does not reach the model, and the mechanism is
    # `_role_of` / `_model_facing_notes` rewriting it -- NOT `_strip_internal`, which the gate
    # measured to have zero call sites. An earlier version asserted that an identity lambda is
    # the identity, which passes whether or not anything is wired anywhere.
    install_search_stubs(m, plans={"meds": {"total": 5}})
    exact_clause = [{"in": ["status"], "any_of": ["Effective"], "match": "exact"}]
    sab("_role_of", lambda src, f: f)
    leaky = run(m, cache, sources=["meds"], where=exact_clause)
    check("SABOTAGE the role rewriter -> a `.raw` wire field now reaches the model",
          "doc_status.raw" in repr(leaky), "still clean")
    restore()
    clean = run(m, cache, sources=["meds"], where=exact_clause)
    check("  ... and with it restored, no `.raw` wire name appears",
          ".raw" not in repr(clean), "still leaking")
    check("  ... while the role the caller used IS reported",
          "status" in repr(g(clean, "per_source", 0, "conditions_as_asked")),
          str(g(clean, "per_source", 0, "conditions_as_asked"))[:80])

    # WAVE 8's two new locator guards. Every other guard in this neighbourhood has a sabotage
    # entry; these arrived without one, and the gate showed why that matters -- neutering
    # `_LOCATOR_SHAPES` was caught by a single assertion, and neutering `_NEVER_A_LOCATOR` was
    # caught by none, because the assertion that would have was written against the same constant
    # the implementation reads.
    install_search_stubs(m, plans={"med_comms": {"total": 3}})
    sab("_NEVER_A_LOCATOR", frozenset())
    try:
        bad_loc = run(m, cache, sources=["med_comms"], about="x")
        chose_title = g(bad_loc, "per_source", 0, "records", 0, "locator", "field")
    finally:
        restore()
    check("SABOTAGE the never-a-locator list -> a TITLE is chosen as the locator",
          chose_title == "title", f"chose {chose_title!r}")
    # The shape test is tested DIRECTLY, not through `run()`. For all five configured sources
    # every template input is either `title` or a retrieval id, so the name list already covers
    # them and neutering the shape test changes nothing observable -- which is worth knowing
    # rather than papering over. The shape test's job is the source we do not have yet: a template
    # interpolating an ordinary field that is not a place. So it is exercised against exactly that.
    synthetic = {"id": "future_source",
                 "locator_template": "https://x/{department}", "fields": {}}
    future_card = {"id": "c1", "body": {"department": "Regulatory Affairs",
                                        "link": "https://real/doc/1"}}
    check("a template input that is neither a title nor an id is still shape-rejected",
          m._locator(future_card, synthetic).get("field") == "link",
          str(m._locator(future_card, synthetic).get("field")))
    sab("_LOCATOR_SHAPES", ("",))
    try:
        loose_field = m._locator(future_card, synthetic).get("field")
    finally:
        restore()
    check("SABOTAGE the shape test -> that field is accepted as a locator",
          loose_field == "department", f"chose {loose_field!r}")

    sab("diagnose_zero", lambda *a, **k: {"is_absence": True})
    install_search_stubs(m, plans={"meds": {"total": 0}})
    bare = run(m, cache, sources=["meds"], about="x")
    check("SABOTAGE diagnose_zero -> the absence ships with no ruled-out list",
          not g(bare, "per_source", 0, "absence", "ruled_out"))
    restore()

    # Wave 7. `PLAN_INTERNAL_KEYS` is enforced BY CONSTRUCTION -- `_execute_plan` builds a fresh
    # dict rather than spreading the plan -- so there is no guard to disable. Sabotage the
    # construction instead: make one per-source result carry the plan, and confirm section 16's
    # loop would see it. Without this, that loop passes on any assembler that happens not to
    # leak, including one that never could, which is the "assertion that cannot fail" shape.
    # THE SAME ARGUMENTS section 16 asserts over, so what this demonstrates is observable is
    # exactly what that loop checks. An earlier version used a different, narrower call
    # (`sources=["meds"], about="x"` with no `distribution` and no `value_prefix`) and accepted
    # `len(leaked) >= 3` of six. That threshold was the only thing bridging the gap: tidy
    # `_plan_facets` to omit `facets`/`value_prefix_requests` when empty -- a plausible change --
    # and the sabotage would stop producing them while still passing, leaving section 16's
    # assertions on those two keys with nothing showing they can fail.
    def _leaked_for(sabotaged: bool) -> list[str]:
        install_search_stubs(m, plans={s: {"total": 7} for s in IN_SCOPE})
        if sabotaged:
            sab("_execute_plan", _leaky)
        try:
            blob = repr(run(m, cache, about="MK-6070", distribution=True,
                            value_prefix={"status": "Eff"}))
        finally:
            if sabotaged:
                restore()
        return sorted(k for k in m.PLAN_INTERNAL_KEYS
                      if f"'{k}':" in blob or f'"{k}":' in blob)

    real_execute = m._execute_plan

    async def _leaky(plan, cache_):
        out = await real_execute(plan, cache_)
        out.update({k: plan.get(k) for k in m.PLAN_INTERNAL_KEYS if k in plan})
        return out

    leaked_keys = _leaked_for(True)
    check("SABOTAGE the assembler's fresh-dict construction -> EVERY plan key reaches the result",
          set(leaked_keys) == set(m.PLAN_INTERNAL_KEYS),
          f"leaked {leaked_keys}, missing {sorted(set(m.PLAN_INTERNAL_KEYS) - set(leaked_keys))}")
    # The control. Without it the detector could be reporting on something other than the
    # sabotage -- a substring that is present either way -- and section 16 and this case would
    # both be passing for reasons unrelated to each other.
    check("  ... and the identical un-sabotaged call leaks none of them",
          _leaked_for(False) == [], f"leaked {_leaked_for(False)}")

    # ----------------------------------------------------------------------------
    print("\n21. MUTATION -- patch the module, confirm the mutation is OBSERVABLE at the output")
    # Retitled honestly. This does not re-run the covering assertion from sections 1-19; it
    # confirms the broken behaviour reaches the result, which is the necessary precondition for
    # any assertion to be able to catch it.
    def mutate(label, apply_fn, probe, broken):
        undo = apply_fn()
        try:
            observed = probe()
            caught = broken(observed)
        except Exception as exc:
            caught, observed = False, f"raised {type(exc).__name__}: {exc}"
        finally:
            undo()
        check(f"MUTATION {label}", caught, str(observed)[:80])

    def set_attr(name, value):
        def apply_fn():
            old = getattr(m, name)
            setattr(m, name, value)
            return lambda: setattr(m, name, old)
        return apply_fn

    install_search_stubs(m, plans={"meds": {"total": 334, "knn": True}})
    mutate("report totalHits as the count -> 0 records for a search that found 334",
           # `lane=` accepted because wave 10 (R3) made the effective lane an argument: the
           # downgrade is now detected BEFORE the count is reconciled, so the caller passes the
           # lane that actually ran. A double that does not accept it raises TypeError, and
           # `mutate()` reports a raise as CAUGHT -- so the mutation would score as covered while
           # never having executed. Signature changes break doubles silently in the direction
           # that looks like success.
           set_attr("reconcile_count", lambda body, plan, lane=None: {
               "records_matched": m._first(body, m.R_TOTAL), "defensible": True,
               "engine": plan["engine"], "narrowed": plan["narrowed"],
               "counts_what": "x"}),
           lambda: g(run(m, cache, sources=["meds"], semantic=True, question="q"),
                     "per_source", 0, "count", "records_matched"),
           lambda n: n == 0)

    install_search_stubs(m, plans={"meds": {"total": 5, "knn": True}})
    mutate("drop the chunk passage -> similarity matches become unverifiable",
           set_attr("_chunk", lambda c: None),
           lambda: g(run(m, cache, sources=["meds"], semantic=True, question="q"),
                     "per_source", 0, "records", 0, "passage"),
           lambda p: p is None)

    install_search_stubs(m, plans={"med_comms": {"total": 19, "facets": {"overflow": True}}})
    # Mutate ONLY the `+` detection, keeping the rest of the assembler real. Replacing the whole
    # function with a lambda returning `{"complete": True}` and asserting `complete is True`
    # tested the monkeypatch plumbing, not the bucket handling.
    def blind_to_overflow():
        old = m.assemble_distribution

        def patched(body, plan, cache_, *, population):
            hidden = {"facets": {k: [t for t in v if str(t.get("term")) != "+"]
                                 for k, v in ((body or {}).get("facets") or {}).items()}}
            return old(hidden, plan, cache_, population=population)
        m.assemble_distribution = patched
        return lambda: setattr(m, "assemble_distribution", old)

    mutate("hide the `+` bucket -> a truncated list reads as complete",
           blind_to_overflow,
           lambda: g(run(m, cache, sources=["med_comms"], about="x", distribution=True),
                     "per_source", 0, "distribution", "fields", "author", "complete"),
           lambda c: c is True)

    install_search_stubs(m, plans={"meds": {"total": 799}})
    # Mutate the ACTUAL path. An earlier version patched `_TRANSIENCE`, a prose constant, so it
    # proved only that a string placed into prose appears in the output -- while
    # `cas-2851862-91-2`, a real member of the stub's synonym map, stayed absent, confirming
    # synonym pass-through was never exercised. The `if False else` was dead scaffolding.
    def leak_synonyms():
        old = m.reconcile_count

        # `lane` forwarded, not dropped -- see the note on the previous mutation. Passing it
        # through keeps this double faithful to the real signature rather than to the one it was
        # written against.
        def patched(body, plan, lane=None):
            out = old(body, plan, lane=lane)
            out["synonyms"] = body.get("synonyms")     # the raw map, straight through
            return out
        m.reconcile_count = patched
        return lambda: setattr(m, "reconcile_count", old)

    mutate("pass the API's raw synonym map through -> aliases claimed for one term",
           leak_synonyms,
           lambda: "cas-2851862-91-2" in repr(run(m, cache, sources=["meds"], about="x")),
           lambda leaked: leaked is True)

    # ----------------------------------------------------------------------------
    print("\n22. WAVE 10 -- the independent review's findings, asserted in the SUITE")
    # These properties are asserted in `mutation_gate.py` as pinned cases, which is what judged
    # each fix. They are asserted HERE as well, and the reason is the point of wave 9: Part A's
    # mutants are judged by this suite, not by the gate, so a property the gate covers and the
    # suite does not leaves its mutant surviving forever. The survivor count would then measure
    # the pre-wave-10 suite in perpetuity and stop being informative.
    #
    # Each of these is sabotage-tested EXTERNALLY rather than here: `mutation_gate.py` breaks the
    # real code and requires the verdict to flip to `killed`. An assertion that is decoration
    # cannot pass that, and it is a stronger check than a sabotage written beside the assertion by
    # the same hand.
    install_search_stubs(m, plans={"med_comms": {"total": 19, "n_cards": 19}})
    w10 = per(run(m, cache, sources=["med_comms"], where=[
        {"in": ["doc_status"], "any_of": ["Effective"], "match": "phrase"}],
        distribution=True), "med_comms")

    # R5. THE VALUE, not the presence of a key: the executed field must be recoverable exactly.
    cond = (w10.get("conditions_as_asked") or [{}])[0]
    check("R5 the executed field is named, not collapsed into its role",
          cond.get("executed_fields") == ["doc_status"], str(cond.get("executed_fields")))
    check("  ... and the role's unconstrained siblings are named separately, not silently",
          "activity_status" in (cond.get("role_covers_also") or []),
          str(cond.get("role_covers_also")))
    check("  ... while `fields` still carries the ROLE, which is the stable vocabulary",
          cond.get("fields") == ["status"], str(cond.get("fields")))

    # R4. 19 matched, 19 returned, our filter removes all 19 -- both numbers established.
    dated = per(run(m, cache, sources=["med_comms"], since="9999-01-01", where=[
        {"in": ["doc_status"], "any_of": ["Effective"], "match": "phrase"}]), "med_comms")
    check("R4 a date filter removing every record reports no count discrepancy",
          "count_discrepancy" not in dated,
          str((dated.get("count_discrepancy") or {}).get("detail"))[:60] or "none, correctly")
    check("  ... and states MUSE's count against the survivors, both established",
          g(dated, "date_filter", "matched_by_muse") == 19
          and g(dated, "date_filter", "surviving_our_filter") == 0,
          f"{g(dated, 'date_filter', 'matched_by_muse')} matched, "
          f"{g(dated, 'date_filter', 'surviving_our_filter')} surviving")

    # R7. `restricted` is the one stubbed definition carrying valueLabels, and `rich_facets` never
    # returned it -- which is why a permanently empty label map went unnoticed for six waves.
    base_req = m._request

    async def with_restricted(method, endpoint, *, payload=None, params=None):
        env = await base_req(method, endpoint, payload=payload, params=params)
        if endpoint == m.EP_FACETS and env.get("_ok"):
            body = copy.deepcopy(env["_body"])
            body.setdefault("facets", {})["restricted.raw"] = [
                {"term": "Accessible", "count": 12}, {"term": "Restricted", "count": 7}]
            return {**env, "_body": body}
        return env

    m._request = with_restricted
    try:
        lab = per(run(m, cache, sources=["med_comms"], distribution=True, where=[
            {"in": ["doc_status"], "any_of": ["Effective"], "match": "phrase"}]), "med_comms")
    finally:
        m._request = base_req
    rvals = [v["value"] for v in (g(lab, "distribution", "fields", "restricted", "values") or [])]
    svals = [v["value"] for v in (g(lab, "distribution", "fields", "doc_status", "values") or [])]
    check("R7 a configured facet label reaches the distribution", rvals == ["Full Access",
                                                                           "Limited Access"],
          str(rvals))
    check("  ... on its own field only -- doc_status declares none and stays raw",
          svals == ["Effective"], str(svals))

    # R3. The IDENTITY: a downgraded semantic result must report the same count and the same
    # description as a genuine term-lane request over the same fixture. A count of 0 beside
    # returned records is the failure this whole group exists to remove.
    install_search_stubs(m, plans={"meds": {"total": 7, "n_cards": 3, "downgraded": True}})
    dg = per(run(m, cache, sources=["meds"], semantic=True, question="stability"), "meds")
    install_search_stubs(m, plans={"meds": {"total": 7, "n_cards": 3}})
    tm = per(run(m, cache, sources=["meds"], about="stability"), "meds")
    check("R3 a downgraded count equals the term-lane count for the same response",
          g(dg, "count", "records_matched") == g(tm, "count", "records_matched") == 7,
          f"downgraded {g(dg, 'count', 'records_matched')} / term "
          f"{g(tm, 'count', 'records_matched')}")
    check("  ... and describes what it counted the same way, not as embeddings",
          g(dg, "count", "counts_what") == g(tm, "count", "counts_what"),
          str(g(dg, "count", "counts_what"))[:52])

    # R6 / R6b. No handle on the semantic engine, and the text it matched is named.
    install_search_stubs(m, plans={"med_comms": {"total": 6, "knn": True}})
    sem = per(run(m, cache, sources=["med_comms"], semantic=True,
                  question="how is potency controlled"), "med_comms")
    check("R6 no population handle is minted on the semantic engine",
          "population" not in sem and bool(sem.get("population_not_offered")),
          str(sem.get("population_not_offered"))[:52])
    check("R6b the text the population was matched against is echoed back",
          sem.get("question_asked") == "how is potency controlled",
          str(sem.get("question_asked")))

    # R1b. THREE states, and the distinction between False and None is the whole point: `None` is
    # "cannot be checked", `False` is affirmative evidence the filter is broken. Asserted per
    # source against what the fixture makes true, not as a shape.
    #   meds       filter field not among its computed facets -> None, cannot be checked
    #   med_comms  the literal is in its own value list        -> True
    install_search_stubs(m, plans={s: {"total": 4} for s in IN_SCOPE})
    both = run(m, cache, about="MK-6070")
    est = {p["source"]: g(p, "count", "population_established")
           for p in (both.get("per_source") or [])}
    check("R1b the count says whether the population was established, per source",
          est.get("meds") is None and est.get("med_comms") is True,
          f"meds={est.get('meds')} (unfacetable filter field), "
          f"med_comms={est.get('med_comms')}")
    check("  ... and never leaves an unestablished one unexplained",
          all(bool(g(p, "count", "population_established_note"))
              for p in (both.get("per_source") or [])
              if g(p, "count", "population_established") is not True),
          "every non-True verdict carries its reason")
    check("  ... while `defensible` still means engine purity only (decisions.md D15)",
          all(g(p, "count", "defensible") is True for p in (both.get("per_source") or [])),
          "unchanged by establishment, as decided")

    # ----------------------------------------------------------------------------
    print("\n22b. the integrated review's findings -- every one was invisible to 764 assertions")
    # These five shipped in wave 10 and neither the suite nor the gate's 22 fault cases changed
    # state when they were fixed. A note that describes the wrong lane is not covered by anything
    # that checks counts and keys, which is exactly how four of them got in.

    # S1. On a silent downgrade the count and match_kind were right and three sibling NOTES still
    # described similarity. Asserted as a DIFFERENCE between two live runs over the same fixture --
    # one that genuinely uses the vector lane, one that downgrades. Each branched surface must
    # differ. Not a keyword scan: the first version of this assertion banned the phrase "by
    # meaning", and the corrected note says "the text that was SENT to be matched by meaning",
    # which is true. A blocklist cannot tell a true use of a word from a false one.
    install_search_stubs(m, plans={"meds": {"total": 9, "n_cards": 3, "downgraded": True}})
    dgr = per(run(m, cache, sources=["meds"], semantic=True, question="how is potency controlled"),
              "meds")
    install_search_stubs(m, plans={"meds": {"total": 9, "n_cards": 3, "knn": True}})
    simr = per(run(m, cache, sources=["meds"], semantic=True, question="how is potency controlled"),
               "meds")
    check("S1 the downgrade and the true similarity lane are distinguishable at all",
          dgr.get("match_kind") == "term" and simr.get("match_kind") == "similarity",
          f"downgraded={dgr.get('match_kind')}, similarity={simr.get('match_kind')}")
    differs = {k for k in ("question_asked_note", "population_not_offered", "next_moves")
               if str(dgr.get(k)) != str(simr.get(k))}
    check("  ... and every note that describes the lane differs between them",
          differs == {"question_asked_note", "population_not_offered", "next_moves"},
          f"identical on: {sorted({'question_asked_note', 'population_not_offered', 'next_moves'} - differs)}"
          if differs != {"question_asked_note", "population_not_offered", "next_moves"}
          else "all three branch on the effective lane")
    check("  ... and the downgraded one says the records are TERM matches",
          "TERM matches" in str(dgr.get("question_asked_note")) + str(dgr.get("population_not_offered")),
          str(dgr.get("question_asked_note"))[:60])
    check("  ... and still says what text was sent, because the request is otherwise unrecoverable",
          dgr.get("question_asked") == "how is potency controlled",
          str(dgr.get("question_asked")))

    # S2, the model-facing half. `field_validation` is produced by `_execute_plan`, so it is
    # asserted where a real result exists rather than from cache state. A blanked mapping must read
    # `unavailable` -- it read `authoritative`, because `mapping_complete` survived the move and was
    # tested first.
    blanked = copy.deepcopy(cache)
    for _f in ("mapping_retrieved", "mapping_complete", "computed_facets", "filter_field_terms",
               "facetable_program_filter"):
        blanked["sources"]["med_comms"][_f] = None
    install_search_stubs(m, plans={"med_comms": {"total": 4}})
    fvres = per(run(m, blanked, sources=["med_comms"], about="MK-6070"), "med_comms")
    check("S2 a blanked mapping reports field validation as unavailable, not authoritative",
          fvres.get("field_validation") == "unavailable", str(fvres.get("field_validation")))
    # The ORDERING fix in isolation. Sabotage found the two halves of S2 mask each other: with
    # `mapping_complete` blanked, reverting the ordering changes nothing, so nothing was testing it
    # -- and if a later change un-blanks that fact the ordering becomes the only thing holding.
    # `mapping_complete: True` beside `mapping_retrieved: None` is the exact state that shipped.
    half = copy.deepcopy(cache)
    half["sources"]["med_comms"]["mapping_retrieved"] = None
    half["sources"]["med_comms"]["mapping_complete"] = True
    hres = per(run(m, half, sources=["med_comms"], about="MK-6070"), "med_comms")
    check("  ... and UNKNOWN is read before completeness, so one surviving fact cannot override it",
          hres.get("field_validation") == "unavailable", str(hres.get("field_validation")))

    # S3. A non-zero count is positive proof the filter matched, so `False` -- which asserts the
    # filter IS broken -- must never be returned beside records. Driven by erasing the evidence,
    # which is the only way to reach the branch: no fixture source produces it naturally.
    stripped = copy.deepcopy(cache)
    stripped["sources"]["med_comms"]["filter_field_terms"] = {"SOMETHING-ELSE": 4}
    install_search_stubs(m, plans={"med_comms": {"total": 19, "n_cards": 3}})
    nonzero = per(run(m, stripped, sources=["med_comms"], about="MK-6070"), "med_comms")
    check("S3 `population_established` is never False beside a non-zero count",
          g(nonzero, "count", "records_matched") == 19
          and g(nonzero, "count", "population_established") is not False,
          f"matched={g(nonzero, 'count', 'records_matched')}, "
          f"established={g(nonzero, 'count', 'population_established')}")
    check("  ... and the reason says the count is what settles it",
          "came back through that filter" in str(g(nonzero, "count",
                                                   "population_established_note")),
          str(g(nonzero, "count", "population_established_note"))[:60])

    # S4. R2b's finding must REACH the model. It was computed, reported by `refresh_freshness`, and
    # dropped by the gate that decides whether `index_freshness` is emitted.
    m._CACHE = None
    c4 = warm(m)
    install_search_stubs(m, plans={"med_comms": {"total": 3}})
    gone = [e for e in UPDATES_BODY if e["id"] != "med_comms"]
    base4 = m._request

    async def drop_med_comms(method, endpoint, *, payload=None, params=None):
        if endpoint == m.EP_UPDATES:
            return {"_ok": True, "_status": {"executed": True}, "_body": gone}
        return await base4(method, endpoint, payload=payload, params=params)

    m._request = drop_med_comms
    try:
        vres = run(m, c4, sources=["med_comms"], about="MK-6070")
    finally:
        m._request = base4
    check("S4 a vanished source reaches the model, not just the cache",
          "med_comms" in (g(vres, "index_freshness", "vanished_since_startup") or []),
          str(g(vres, "index_freshness", "vanished_since_startup")))

    # A FIXED expected total. Guards can otherwise vanish silently -- a `continue` that skips
    # a source, a conditionally-registered check -- and the summary still reads "N/N passed"
    # with nothing saying N shrank. That is exactly how "132/132" was 132 of 134.
    # 150 through wave 6. Wave 7 raised it to 156, and the arithmetic is written out because
    # this is the one comment whose whole job is making the pin auditable:
    #
    #   -2  the two PLAN_INTERNAL_KEYS assertions that tested nothing -- `len(...) >= 6` on a
    #       constant, and a direct call to `_strip_internal`, a filter with zero call sites
    #   +6  one assertion per PLAN_INTERNAL_KEYS member, over a real result
    #   +1  section 20 sabotage: break the fresh-dict construction, every key must leak
    #   +1  its control: the same call un-sabotaged must leak none
    #
    #   +3  `ordered_by` on the RESULT: newest -> modified_date, oldest -> creation_date, and
    #       relevance -> None rather than an invented value. Wave 3 asserted it on the plan
    #       only, so it never reached the model for three waves.
    #
    #   150 - 2 + 6 + 1 + 1 + 3 = 159
    #
    #  +19  WAVE 8, section 19b: one per group-review-gate finding, asserted at the RESULT
    #       rather than at the plan -- value_prefix executing at all, the locator shape guard,
    #       the program filter field off every record, the similarity score's value, and no
    #       `.raw` name in the distribution.
    #
    #   159 + 19 = 178
    # +11 in wave 10 section 22, one per fixed property, so the SUITE covers what the
    # mutation gate's cases cover -- Part A's mutants are judged by this suite, not by the
    # gate, so a gate-only property leaves its mutant surviving forever.
    # +6 more in wave 10: section 13 rewritten for R1 (both sides of the absence gate,
    # net +4) and R1b's three-state verdict in section 22 (+3, minus one old).
    # +8 in the wave-10 follow-up, section 22b: S1 (four, as a difference between a
    # true-similarity run and a downgraded one), S2, S3 (two), S4. Every one of those
    # five defects was invisible to all 764 assertions AND to the gate's 22 fault
    # cases -- nothing changed state when they were fixed, which is how they shipped.
    expected = 209
    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} assertions passed")
    if CHECKS != expected:
        print(f"HARNESS: expected {expected} assertions, ran {CHECKS}. A guard was added or "
              f"vanished -- update `expected` deliberately, never to match a shrunken run.")
        return 1
    if FAILURES:
        print("FAILED: " + ", ".join(FAILURES))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
