#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp==1.28.1","httpx>=0.27","pydantic>=2","pyyaml>=6.0","truststore>=0.9",
#   "typing-extensions>=4.12",
# ]
# ///
# Pins mirror `ics_muse/server.py`: `load_server()` execs that module. Without this block the
# documented invocation exits 1 with ModuleNotFoundError and ZERO assertion output, which reads
# as "assertions failed".
"""Assert `muse_read` diffs what it asked for against what came back.

    ./tools/document_read_check.py

NO NETWORK AND NO TOKEN.

The failure this endpoint specialises in is **HTTP 200 with fewer documents than asked for**. No
status code reveals it, and there are three ways a document goes missing with only one of them
loud:

    a bogus id                 silently filtered -- 200, fewer returned
    a DENIED source            silently filtered -- 200, fewer returned
    a source without full text **403, the ENTIRE batch fails**

So every assertion here tests an EFFECT, and the two that matter most are about what happens
AROUND the call: pre-filter so the 403 never fires, and diff so a silent omission cannot pass.

Carried forward from earlier waves, because each caught a real defect: a FIXED expected assertion
total (one script reported "132/132" that was 132 of 134, and another "76 expected, 74 ran");
sabotage inside try/finally; and mutations that patch the REAL path rather than a prose constant.

Exit status is 0 only if every assertion holds and the count matches.
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
    install_stubs,
    load_server,
)

FAILURES: list[str] = []
CHECKS = 0
_PRISTINE = None


def check(label: str, ok: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{': ' + detail if detail else ''}")
    if not ok:
        FAILURES.append(label)


def g(obj: Any, *path: Any) -> Any:
    """None on any miss, so a removed key fails ONE assertion instead of raising."""
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
# Document bodies, shaped from live measurement (`03` sections 1 and 4)
# --------------------------------------------------------------------------
# Table cells arrive as SEPARATE tab-prefixed lines in document order with no row or column
# grouping. These are the measured values: endotoxin limits, a process designation, lot numbers
# and durations, arriving flat with nothing tying a limit to its lot.
TABULAR = ("Specification and stability summary\n\n"
           "\t<0.2 EU/mL\n\tProcess 1\n\t56 M\n"
           "\t< 0.4 EU/mL\n\t047-004-001\n\t18 M\n"
           "\t< 0.5 EU/mL\n\t0000543558\n\t1 M\n\n"
           "The rationale for the limit is described in the referenced report.\n")
# 241 occurrences across 30 documents; naive splitting on " " silently misses content.
NBSP_TEXT = "A limit of\xa0100 ppm applies\xa0to this attribute.\n"
# Russian and Vietnamese both appear inside meds documents.
MULTILINGUAL = "Общие сведения о стабильности препарата.\ndùng cho thử nghiệm lâm sàng\n"
PROSE = ("Introduction\n\nThe justification for the control strategy is set out below.\n\n"
         "Stability was assessed under accelerated conditions and the rationale is narrative.\n")


def document(source: str, n: int, *, text: str = PROSE, size: int | None = None,
             classified: bool = False, no_version: bool = False,
             experiment_status: bool = False, indexed_later: bool = False) -> dict[str, Any]:
    """One returned document. Shape is per DOCUMENT, not per source -- a field is absent when
    unpopulated, so two meds documents returned 54 and 58 keys. Only SEVEN keys are common to all
    five sources: acl_group, datasource, id, modified_date, text, text_size, title."""
    doc: dict[str, Any] = {
        # The seven universal keys.
        "id": f"card-{source}-{n}", "datasource": source,
        "title": f"{source} document {n}", "text": text,
        "text_size": len(text) if size is None else size,
        "modified_date": "2025-03-24T00:00:00", "acl_group": ["some_group"],
    }
    if not no_version:
        doc["current_version_doc_id"] = f"CVD-{n}"
    if experiment_status:
        # The lifecycle of an EXPERIMENT, not of a document.
        doc["experiment_status"] = "Completed"
        doc["experiment_review_status"] = "Reviewed"
    elif not no_version:
        doc["doc_status"] = "Effective"
        doc["retention_class"] = "Official"
    if classified:
        # A data classification, and it exists only on meds.
        doc["sensitivity"] = "Proprietary"
    doc["webview_pdf_url"] = f"https://webview.merck.com/getContentById/CLS-{n}.pdf?version=CURRENT"
    doc["creation_date"] = "2024-01-02T00:00:00"
    if indexed_later:
        doc["timestamp_indexed"] = "2026-06-05T00:00:00"
    return doc


def install_read_stubs(m, *, returns=None, fail: str | None = None,
                       reorder: bool = False) -> dict[str, Any]:
    """Extend wave 1's stub with the read endpoint. `returns` is what comes BACK, which is
    deliberately not what was asked for -- that difference is the whole product."""
    global _PRISTINE
    if _PRISTINE is None:
        _PRISTINE = m._request
    base = _PRISTINE
    seen: dict[str, Any] = {}

    async def stub(method: str, endpoint: str, *, payload=None, params=None):
        if endpoint == m.EP_READ:
            seen["payload"] = payload
            if fail:
                return {"_ok": False, "_status": {"executed": False, "reason": fail,
                                                  "detail": "stubbed failure"}}
            docs = list(returns or [])
            if reorder:
                # MEASURED: a [meds, scited] batch came back ['scited','meds']. Response order
                # is not the request's, so anything zipping by index misattributes text.
                docs = list(reversed(docs))
            return {"_ok": True, "_status": {"executed": True}, "_body": docs}
        return await base(method, endpoint, payload=payload, params=params)

    m._request = stub
    return seen


def warm(m) -> dict[str, Any]:
    m._CACHE = None
    m._calls_used = 0
    m._started = m.time.monotonic()
    install_stubs(m)
    return asyncio.run(m.warm_cache("MK-6070"))


def read(m, cache, docs, purpose="establish the documented basis", passages=None):
    return asyncio.run(m.run_read(cache, docs, purpose, passages))


def handle(m, source, card_id):
    h, err = m.encode_document_handle(source, card_id)
    assert err is None, err
    return h


def main() -> int:  # noqa: C901 - a linear assertion script, deliberately flat
    m = load_server()
    cache = warm(m)
    if not cache["ok"]:
        check("the stub cache warms", False, str(cache["degraded"]))
        print("\n0/1 assertions passed")
        return 1

    # ----------------------------------------------------------------------------
    print("1. the surfaced tool is exactly three parameters")
    import inspect
    params = list(inspect.signature(m.muse_read).parameters)
    check("three parameters, no fourth", params == ["documents", "purpose", "passages"],
          str(params))
    check("it is annotated read-only", m._READ_ONLY.readOnlyHint is True)

    # ----------------------------------------------------------------------------
    print("\n2. THE DIFF -- requested versus returned, and every absence attributed")
    h_a = handle(m, "meds", "card-meds-1")
    h_b = handle(m, "meds", "card-meds-2")
    h_bogus = handle(m, "meds", "card-meds-nonexistent")
    install_read_stubs(m, returns=[document("meds", 1), document("meds", 2)])
    r = read(m, cache, [h_a, h_b, h_bogus])
    check("requested 3, read 2", r["requested_count"] == 3 and r["read_count"] == 2,
          f"{r['requested_count']} requested, {r['read_count']} read")
    check("the third is NAMED as not read", len(r["not_read"]) == 1, str(r["not_read"])[:70])
    check("  ... with an attribution", bool(g(r, "not_read", 0, "attribution")),
          str(g(r, "not_read", 0, "attribution")))
    check("  ... and it says the endpoint gives no signal of its own",
          "no signal" in (g(r, "not_read", 0, "detail") or ""),
          str(g(r, "not_read", 0, "detail"))[:70])
    check("  ... and that this endpoint cannot tell denial from a bad id",
          "cannot distinguish" in (g(r, "not_read", 0, "detail") or ""))
    check("a coverage caution is raised", bool(r.get("coverage_caution")),
          str(r.get("coverage_caution"))[:70])

    print("\n2b. matched on returned `id`, NEVER on position")
    install_read_stubs(m, returns=[document("meds", 1, text="TEXT-ONE"),
                                   document("meds", 2, text="TEXT-TWO")], reorder=True)
    r = read(m, cache, [h_a, h_b])
    # Zipping by index would attribute one document's text to the other -- a provenance failure
    # nothing downstream would catch.
    by_title = {d["title"]: d.get("text") for d in r["read"]}
    check("the response came back in the OTHER order and text still matches its own document",
          by_title.get("meds document 1") == "TEXT-ONE"
          and by_title.get("meds document 2") == "TEXT-TWO", str(by_title))

    print("\n2c. duplicates do not read as missing")
    install_read_stubs(m, returns=[document("meds", 1)])
    r = read(m, cache, [h_a, h_a, h_a])
    check("the same handle three times reports NOTHING absent", r["not_read"] == [],
          str(r["not_read"]))
    check("  ... and the dedupe is reported", r.get("duplicates_removed") == 2,
          str(r.get("duplicates_removed")))
    check("  ... with the reason the server would have hidden it",
          "would have shown up below as missing" in (r.get("duplicates_note") or ""))

    # ----------------------------------------------------------------------------
    print("\n3. the 403 batch-killer is never triggered")
    noread = copy.deepcopy(cache)
    # BOTH, because `readable` is wave 1's collapse of the declared value and the two cannot
    # disagree in real data. Moving only one created a state that does not exist and let the
    # fixture bypass a guard that is correct.
    noread["sources"]["scited"]["readable_declared"] = False
    noread["sources"]["scited"]["readable"] = False
    h_scited = handle(m, "scited", "card-scited-1")
    seen = install_read_stubs(m, returns=[document("meds", 1)])
    r = read(m, noread, [h_a, h_scited])
    check("the unsupported document is excluded BEFORE sending",
          [e["cause"] for e in r["excluded_before_sending"]] == ["source_has_no_full_text"],
          str(r["excluded_before_sending"])[:70])
    check("  ... and it says why -- one such document fails the whole batch",
          "entire batch" in (g(r, "excluded_before_sending", 0, "detail") or ""))
    # `document_entries`, the shape the live OpenAPI schema declares.
    sent = [e["document_id"] for e in (g(seen, "payload", "document_entries") or [])]
    check("  ... so the request that WAS sent contains only the readable one",
          sent == ["card-meds-1"], str(sent))
    check("  ... and the readable document was still read", r["read_count"] == 1,
          str(r["read_count"]))
    # UNKNOWN is not False, and it is not risked either.
    unknown = copy.deepcopy(cache)
    unknown["sources"]["scited"]["readable_declared"] = None
    unknown["sources"]["scited"]["readable"] = False
    r = read(m, unknown, [h_scited])
    check("a source whose full-text support is UNKNOWN is excluded, not risked",
          r.get("cause") == "no_readable_document"
          or [e["cause"] for e in r.get("excluded_before_sending", [])]
          == ["full_text_support_unknown"], str(r.get("cause")))

    print("\n3b. a 403 that does happen is not reported as absence")
    install_read_stubs(m, fail="restricted_or_forbidden")
    r = read(m, cache, [h_a])
    check("the failure is NOT absence", r.get("is_absence") is False, str(r.get("status")))
    check("  ... and names the whole-batch rejection",
          "whole batch was rejected" in (r.get("note") or ""), str(r.get("note"))[:70])

    # ----------------------------------------------------------------------------
    print("\n4. handles only -- identifiers are not the model's to construct")
    r = read(m, cache, ["card-meds-1"])
    check("a raw card.id string is REFUSED", r.get("refused") is True, str(r.get("cause")))
    check("  ... and says identifiers are not the model's to construct",
          "not the model's to construct" in str(r), str(r.get("excluded"))[:70])
    install_read_stubs(m, returns=[document("meds", 1)])
    r = read(m, cache, [h_a])
    blob = repr(r)
    check("no raw card id reaches the model", "card-meds-1" not in blob)
    check("`document_id` is never emitted as an identifier", "document_id" not in blob)
    check("a document handle IS emitted", str(g(r, "read", 0, "document")).startswith("d1."),
          str(g(r, "read", 0, "document"))[:24])

    print("\n4b. `purpose` is required and recorded")
    r = read(m, cache, [h_a], purpose="   ")
    check("a blank purpose is refused", r.get("cause") == "no_purpose", str(r.get("cause")))
    check("  ... because a read with no purpose cannot be qualified later",
          "cannot be qualified" in (r.get("message") or ""))
    install_read_stubs(m, returns=[document("meds", 1)])
    r = read(m, cache, [h_a], purpose="establish the stability basis")
    check("the purpose is recorded on the result",
          r["purpose"] == "establish the stability basis", r["purpose"])
    check("  ... and on each document", g(r, "read", 0, "read_for") == "establish the stability basis")

    # ----------------------------------------------------------------------------
    print("\n5. `this source cannot say` is an ANSWER, not a blank")
    install_read_stubs(m, returns=[document("scited", 1, no_version=True)])
    r = read(m, cache, [h_scited])
    sl = g(r, "read", 0, "slices")
    check("scited's version slice is explicitly unavailable",
          g(sl, "version_identity", "available") is False,
          str(g(sl, "version_identity"))[:70])
    check("  ... and says the source does not represent it",
          "does not represent" in (g(sl, "version_identity", "detail") or ""))
    check("scited's status slice is likewise unavailable",
          g(sl, "represented_status", "available") is False,
          str(g(sl, "represented_status"))[:60])
    check("no nearest-looking field was substituted",
          "doc_status" not in repr(sl) and "activity_status" not in repr(sl))
    print("\n5b. an experiment's lifecycle is not a document's status")
    h_sig = handle(m, "signals", "card-signals-1")
    # Wave 1's fixture deliberately OMITS `fullDocumentTextRetrievalEnabled` on signals, so the
    # pre-filter correctly excludes it -- which is asserted in section 3. Here the slice logic is
    # what is under test, so declare it readable explicitly.
    sig_cache = copy.deepcopy(cache)
    sig_cache["sources"]["signals"]["readable_declared"] = True
    sig_cache["sources"]["signals"]["readable"] = True
    install_read_stubs(m, returns=[document("signals", 1, no_version=True,
                                            experiment_status=True)])
    r = read(m, sig_cache, [h_sig])
    st = g(r, "read", 0, "slices", "represented_status")
    check("the experiment status fills the slice", g(st, "available") is True, str(st)[:60])
    check("  ... but carries the caution that it is an EXPERIMENT's lifecycle",
          "EXPERIMENT" in (g(st, "caution") or ""), str(g(st, "caution"))[:70])

    # ----------------------------------------------------------------------------
    print("\n6. classification and access list are different things")
    install_read_stubs(m, returns=[document("meds", 1, classified=True)])
    r = read(m, cache, [h_a])
    rs = g(r, "read", 0, "restriction")
    check("meds carries the classification", g(rs, "classification") == "Proprietary",
          str(g(rs, "classification")))
    check("  ... and it is stated to travel with derived meaning",
          "travels with any meaning" in (g(rs, "classification_travels") or ""))
    check("  ... and the result says so at the top", bool(r.get("disclosure")),
          str(r.get("disclosure"))[:70])
    install_read_stubs(m, returns=[document("scited", 1, no_version=True)])
    r = read(m, cache, [h_scited])
    rs = g(r, "read", 0, "restriction")
    check("a source without one reports NO classification", g(rs, "classification") is None)
    check("  ... explicitly NOT defaulted to unclassified",
          "not a default" in (g(rs, "classification_absent") or ""),
          str(g(rs, "classification_absent"))[:70])
    check("  ... and does not assert an access list that was not found",
          g(rs, "access_list_present") in (True, False), str(g(rs, "access_list_present")))
    check("  ... and where one IS present it is named as a different question",
          g(rs, "access_list_present") is not True
          or "different question" in (g(rs, "access_list_note") or ""),
          str(g(rs, "access_list_note"))[:60])

    # ----------------------------------------------------------------------------
    print("\n7. text fidelity -- three flags, and the locator when the number matters")
    install_read_stubs(m, returns=[document("meds", 1, text=TABULAR, classified=True)])
    r = read(m, cache, [h_a])
    fid = g(r, "read", 0, "text_fidelity")
    check("tabular material is detected", g(fid, "tabular_material") is True, str(fid)[:60])
    check("  ... and says associations were NOT preserved",
          "NO row or column grouping" in (g(fid, "tabular_note") or ""))
    check("numeric criteria inside it are flagged association-uncertain",
          g(fid, "association_uncertain") is True, str(g(fid, "association_note"))[:60])
    check("  ... as REAL numbers with lost associations",
          "REAL numbers whose associations were lost" in (g(fid, "association_note") or ""))
    check("and the locator is offered INSTEAD of the extracted number",
          bool(g(r, "read", 0, "use_the_locator_instead", "locators")),
          str(g(r, "read", 0, "use_the_locator_instead", "why"))[:60])
    check("the result raises it at the top too", bool(r.get("fidelity_caution")),
          str(r.get("fidelity_caution"))[:60])
    print("\n7b. prose does NOT trip the flags")
    install_read_stubs(m, returns=[document("meds", 1, text=PROSE)])
    r = read(m, cache, [h_a])
    fid = g(r, "read", 0, "text_fidelity")
    check("plain prose is not flagged tabular", g(fid, "tabular_material") is False)
    check("  ... nor association-uncertain", g(fid, "association_uncertain") is False)
    check("  ... and no locator substitution is offered",
          g(r, "read", 0, "use_the_locator_instead") is None)
    print("\n7c. non-Latin content is noted")
    install_read_stubs(m, returns=[document("meds", 1, text=MULTILINGUAL)])
    r = read(m, cache, [h_a])
    check("non-Latin content is flagged",
          g(r, "read", 0, "text_fidelity", "non_latin_content") is True)
    check("  ... because a passage quoted without it can be unreadable",
          "unreadable to its reader" in
          (g(r, "read", 0, "text_fidelity", "language_note") or ""))

    print("\n8. the non-breaking space is normalised, not flagged")
    install_read_stubs(m, returns=[document("meds", 1, text=NBSP_TEXT)])
    r = read(m, cache, [h_a])
    got = g(r, "read", 0, "text")
    check("no non-breaking space survives", "\xa0" not in str(got), repr(str(got)[:40]))
    check("  ... and the content is intact", "100 ppm" in str(got), repr(str(got)[:40]))

    print("\n9. extent is asserted against what the source declared")
    install_read_stubs(m, returns=[document("meds", 1, text=PROSE)])
    r = read(m, cache, [h_a])
    check("len(text) is reported", g(r, "read", 0, "extent_chars") == len(PROSE),
          str(g(r, "read", 0, "extent_chars")))
    check("  ... and no mismatch is claimed when they agree",
          g(r, "read", 0, "extent_mismatch") is None)
    install_read_stubs(m, returns=[document("meds", 1, text=PROSE, size=999999)])
    r = read(m, cache, [h_a])
    check("a declared size that disagrees is reported as possible truncation",
          "may be truncated" in (g(r, "read", 0, "extent_mismatch") or ""),
          str(g(r, "read", 0, "extent_mismatch"))[:60])

    print("\n10. index currency and document currency are different facts")
    install_read_stubs(m, returns=[document("meds", 1, indexed_later=True)])
    r = read(m, cache, [h_a])
    cur = g(r, "read", 0, "currency")
    check("both dates are reported", g(cur, "document_modified") and g(cur, "index_saw_it"),
          f"{g(cur, 'document_modified')} / {g(cur, 'index_saw_it')}")
    check("  ... and index staleness is not document staleness",
          "not document staleness" in (g(cur, "note") or ""))

    print("\n11. passages carry position AND which material they came from")
    install_read_stubs(m, returns=[document("meds", 1, text=TABULAR)])
    # A term inside the TAB BLOCK. "limit" appears only in the prose sentence, so searching for
    # it returned a prose passage and the tabular branch went unexercised.
    r = read(m, cache, [h_a], passages="Process")
    ps = g(r, "read", 0, "passages") or []
    check("passages were returned instead of the whole text",
          bool(ps) and g(r, "read", 0, "text") is None, f"{len(ps)} passages")
    check("each carries its position", all("position_chars" in x for x in ps), str(ps[:1])[:60])
    check("each says which material it came from",
          all(x.get("material") in ("prose", "tabular") for x in ps),
          str([x.get("material") for x in ps]))
    tab = [x for x in ps if x.get("material") == "tabular"]
    check("a tabular passage carries the association warning WITH it",
          bool(tab) and all(x.get("association_uncertain") for x in tab),
          str(len(tab)))

    print("\n11b. WAVE 8 -- no match is a CLAIM, and it has to be stated")
    # Found by the group review gate. Measured: `passages="acceptance criterion"` against a
    # 7,971-character meds document returned `passages: []` with NO `text` key -- a document with
    # no content and no statement, which is a zero presented as absence, the one rule with no
    # exceptions here. The unusable-query branch three lines up in the connector already carried
    # a sentence naming this hazard in those words; the no-match case, which is the common one,
    # did not get it.
    install_read_stubs(m, returns=[document("meds", 1, text=TABULAR)])
    r = read(m, cache, [h_a], passages="zzqqxnotinthisdocument")
    doc = g(r, "read", 0) or {}
    check("an unmatched passage query returns no passages", doc.get("passages") == [],
          str(doc.get("passages"))[:40])
    check("  ... and does NOT silently fall back to the whole text",
          "text" not in doc, str("text" in doc))
    nm = doc.get("no_passage_matched") or {}
    check("  ... the query that found nothing is named back",
          nm.get("searched_for") == "zzqqxnotinthisdocument", str(nm.get("searched_for")))
    check("  ... with how much text was actually searched, so an empty document is "
          "distinguishable from an absent term",
          nm.get("searched_chars") == len(TABULAR), str(nm.get("searched_chars")))
    check("  ... and it is stated as NOT absence",
          "not the same as" in str(nm.get("meaning") or ""), str(nm.get("meaning"))[:60])
    # The complement: a term that IS present must not trip the new branch.
    r2 = read(m, cache, [h_a], passages="Process")
    check("a matching query still returns passages and no no-match block",
          bool(g(r2, "read", 0, "passages")) and "no_passage_matched" not in (g(r2, "read", 0) or {}),
          str(len(g(r2, "read", 0, "passages") or [])))

    print("\n12. no size-keyed warning -- extent is a fact, the judgment is the model's")
    install_read_stubs(m, returns=[document("meds", 1, text="x" * 200000)])
    r = read(m, cache, [h_a])
    check("a very large document draws no size caution",
          not any("too large" in str(v).lower() or "consider" in str(v).lower()
                  for v in r.values() if isinstance(v, str)),
          "clean")
    check("  ... but its extent IS reported", g(r, "read", 0, "extent_chars") == 200000,
          str(g(r, "read", 0, "extent_chars")))

    print("\n13. a DENIED source's omission is attributed, not inferred")
    denied = copy.deepcopy(cache)
    denied["sources"]["mrl_slides"]["searchable"] = False
    denied["sources"]["mrl_slides"]["access"] = "DENIED"
    h_mrl = handle(m, "mrl_slides", "card-mrl_slides-1")
    r = read(m, denied, [h_mrl])
    check("it is excluded before sending, with the reason",
          r.get("cause") == "no_readable_document"
          or [e["cause"] for e in r.get("excluded_before_sending", [])] == ["source_denied"],
          str(r.get("cause")))
    check("  ... and says the endpoint would have dropped it silently",
          "silently" in str(r), str(r.get("excluded"))[:70])

    print("\n14. the transience rule and no aggregate claim")
    install_read_stubs(m, returns=[document("meds", 1)])
    r = read(m, cache, [h_a])
    check("the transience rule is stated", "creates no work" in (r.get("status_of_this_result") or ""))
    check("no endpoint path is exposed", "/resources/v1/" not in repr(r))
    check("no wire key is exposed",
          not any(k in repr(r) for k in ("document_entries", "dataSources")))

    # ----------------------------------------------------------------------------
    print("\n15. SABOTAGE -- inside try/finally, so a raise cannot leave the module broken")
    orig: dict[str, Any] = {}

    def sabotage(label, name, value, probe, broken):
        orig[name] = getattr(m, name)
        setattr(m, name, value)
        try:
            observed = probe()
            caught = broken(observed)
        except Exception as exc:
            caught, observed = False, f"raised {type(exc).__name__}: {exc}"
        finally:
            setattr(m, name, orig.pop(name))
        check(f"SABOTAGE {label}", caught, str(observed)[:80])

    install_read_stubs(m, returns=[document("meds", 1, text=TABULAR)])
    sabotage("the tab-run detector -> tabular material goes unflagged",
             "_TAB_RUN", __import__("re").compile(r"^$"),
             lambda: g(read(m, cache, [h_a]), "read", 0, "text_fidelity", "tabular_material"),
             lambda v: v is False)
    install_read_stubs(m, returns=[document("meds", 1, text=NBSP_TEXT)])
    sabotage("the nbsp normaliser -> the non-breaking space survives",
             "_normalise_text", lambda v: v,
             lambda: "\xa0" in str(g(read(m, cache, [h_a]), "read", 0, "text")),
             lambda v: v is True)
    install_read_stubs(m, returns=[document("scited", 1, no_version=True)])
    sabotage("the unfillable-slice rule -> a missing slice is silently blank",
             "_read_slices", lambda doc, source, src_meta=None: {},
             lambda: g(read(m, cache, [h_scited]), "read", 0, "slices"),
             lambda v: v == {})

    # ----------------------------------------------------------------------------
    print("\n16. MUTATION -- patch the real path, confirm the break is observable")

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

    # THE defect this wave exists to prevent: zip by position instead of matching on id.
    def zip_by_position():
        old = m.run_read

        async def patched(cache_, documents, purpose, passages=None):
            plan = m.plan_read(cache_, documents, purpose, passages)
            if plan.get("refused"):
                return plan
            env = await m._post(plan["endpoint"], plan["payload"])
            docs = env["_body"] if isinstance(env["_body"], list) else []
            out = []
            for want, got in zip(plan["requested"], docs):
                asm = m.assemble_document(got, cache_, purpose=plan["purpose"], passages=None)
                if asm:
                    asm["requested_as"] = want["id"]
                    out.append(asm)
            return {"refused": False, "read": out, "not_read": [],
                    "requested_count": len(plan["requested"]), "read_count": len(out)}
        m.run_read = patched
        return lambda: setattr(m, "run_read", old)

    install_read_stubs(m, returns=[document("meds", 1, text="TEXT-ONE"),
                                   document("meds", 2, text="TEXT-TWO")], reorder=True)
    mutate("zip response to request by POSITION -> text attributed to the wrong document",
           zip_by_position,
           lambda: [(d.get("requested_as"), d.get("text")) for d in
                    (read(m, cache, [h_a, h_b]).get("read") or [])],
           lambda pairs: any(rid == "card-meds-1" and txt == "TEXT-TWO" for rid, txt in pairs))

    # And dropping the diff entirely: a missing document becomes invisible.
    def no_diff():
        old = m.run_read

        async def patched(cache_, documents, purpose, passages=None):
            r = await old(cache_, documents, purpose, passages)
            r.pop("not_read", None)
            r.pop("coverage_caution", None)
            return r
        m.run_read = patched
        return lambda: setattr(m, "run_read", old)

    install_read_stubs(m, returns=[document("meds", 1)])
    mutate("drop the diff -> a requested document goes missing silently",
           no_diff,
           lambda: read(m, cache, [h_a, h_bogus]),
           lambda r: "not_read" not in r and r.get("read_count") == 1)

    # And skipping the pre-filter: the 403 batch-killer becomes reachable.
    def no_prefilter():
        old = m.plan_read

        def patched(cache_, documents, purpose, passages=None):
            stripped = copy.deepcopy(cache_)
            for src in stripped["sources"].values():
                src["readable_declared"] = True
                src["readable"] = True
            return old(stripped, documents, purpose, passages)
        m.plan_read = patched
        return lambda: setattr(m, "plan_read", old)

    seen2 = install_read_stubs(m, returns=[document("meds", 1)])
    mutate("skip the full-text pre-filter -> an unsupported source reaches the payload",
           no_prefilter,
           lambda: (read(m, noread, [h_a, h_scited]),
                    [e["document_id"] for e in
                     (g(seen2, "payload", "document_entries") or [])])[1],
           lambda ids: bool(ids) and "card-scited-1" in ids)

    # A FIXED expected total. Guards can otherwise vanish -- a `continue` that skips a case, a
    # conditionally-registered check -- and the summary still reads "N/N passed". That is exactly
    # how one earlier script reported 132 of a possible 134.
    # 76 through wave 7. Wave 8 adds 6 in section 11b: an unmatched passage query is a claim and
    # must say so -- it returned an empty list beside an absent `text` key, which is a zero
    # presented as absence. Found by the group review gate.
    expected = 82
    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} assertions passed")
    if CHECKS != expected:
        print(f"HARNESS: expected {expected} assertions, ran {CHECKS}. A guard was added or "
              "vanished -- update `expected` deliberately, never to match a shrunken run.")
        return 1
    if FAILURES:
        print("FAILED: " + ", ".join(FAILURES))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
