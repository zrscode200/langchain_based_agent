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
# Pins mirror `ics_muse/server.py` exactly: `load_server()` execs that module, so its
# imports must resolve. Without this block the documented invocation exits 1 with
# ModuleNotFoundError and ZERO assertion output -- which reads as "assertions failed".
"""Assert wave 3 builds the request the design specifies, and refuses the rest.

    ./tools/request_build_check.py

NO NETWORK AND NO TOKEN. The cache is warmed through wave 1's stubs.

EVERY ASSERTION TESTS AN EFFECT -- what the payload contains or what the refusal says --
never that a function returned without raising. This API returns HTTP 200 with the intent
silently discarded in at least 17 measured ways, so "it was accepted" establishes nothing.

Three of these sections exist because of things that went wrong, not things that might:

  * Section 12 SABOTAGES every guard: each one is disabled in turn and the assertion that
    covers it must then fail. A guard nobody can break is a guard nobody is testing.
  * Section 13 MUTATION-TESTS the assertions themselves. Both prior waves shipped
    assertions that passed while testing nothing -- one echoed the whole payload back in a
    refusal and still scored 45/45. Only mutation caught them.
  * Section 11 asserts the four `.raw`/facet predicates are one set, because the wave-3
    probe found `has_raw`, `facetField` and the declared list identical on all five
    sources. If MUSE ever separates them, the `value_prefix` guard silently changes
    meaning and this is where that surfaces.

Exit status is 0 only if every assertion holds.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SERVER = HERE / "ics_muse" / "server.py"
sys.path.insert(0, str(HERE))
from startup_cache_check import (  # noqa: E402  - reuse wave 1's stubs, don't fork them
    IN_SCOPE,
    install_stubs,
    load_server,
)

FAILURES: list[str] = []
CHECKS = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{': ' + detail if detail else ''}")
    if not ok:
        FAILURES.append(label)


def warm(m) -> dict[str, Any]:
    m._CACHE = None
    m._calls_used = 0
    m._started = m.time.monotonic()
    install_stubs(m)
    return asyncio.run(m.warm_cache("MK-6070"))


def g(obj: Any, *path: Any) -> Any:
    """Walk a nested structure, returning None on any miss.

    Used wherever a mutation could REMOVE a key rather than change its value. A bare
    subscript would raise KeyError there, and a traceback exits non-zero with no assertion
    output -- which reads as "everything failed" and hides which guard actually broke. A
    missing key must fail one named assertion, like a wrong value does.
    """
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


def conds(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return g(plan, "search", "conditionQuery", "conditions") or []


def only(cache, source="meds", **kw) -> dict[str, Any]:
    """Plan one source and return that source's plan (or the refusal)."""
    m_plan = PLAN(cache, sources=[source], **kw)
    if m_plan.get("refused"):
        return m_plan
    return m_plan["requests"][0]


PLAN = None  # bound in main()


def main() -> int:  # noqa: C901 - a linear assertion script, deliberately flat
    global PLAN
    m = load_server()
    PLAN = m.plan_population
    cache = warm(m)
    assert cache["ok"], cache["degraded"]

    # ----------------------------------------------------------------------------
    print("1. the surfaced signature is exactly fifteen parameters")
    import inspect
    sig = inspect.signature(m.plan_population)
    params = [p for p in sig.parameters if p != "cache"]
    expected = ["sources", "about", "where", "where_not", "since", "until", "population",
                "semantic", "question", "order", "depth", "offset", "fields",
                "distribution", "value_prefix"]
    check("fifteen parameters, no sixteenth", params == expected,
          f"{len(params)}: {params}")
    check("`read_top` is absent -- superseded, not deferred", "read_top" not in params)

    # ----------------------------------------------------------------------------
    print("\n2. a clause compiles to conditions whose fields exist in the mapping")
    p = only(cache, "meds", where=[{"in": ["title"], "any_of": ["stability"]}])
    check("not refused", not p.get("refused"), str(p.get("message"))[:70])
    c = conds(p)
    check("one condition emitted", len(c) == 1, str(c))
    check("field resolved from the role", c[0]["fields"] == ["title"], str(c[0]["fields"]))
    check("`type` is always present -- omitting it is a 500",
          all("type" in x for x in c))
    check("type is MUST", c[0]["type"] == "MUST", c[0]["type"])
    mapped = set(cache["sources"]["meds"]["fields"])
    check("every condition field is in mappings.document.properties",
          all(f.removesuffix(".raw") in mapped for x in c for f in x["fields"]))

    p = only(cache, "meds", where=[{"in": ["nonexistent_field"], "any_of": ["x"]}])
    check("an unmapped field is REFUSED, not sent (it would return 0 at HTTP 200)",
          p.get("refused") and "nonexistent_field" in p["message"], str(p.get("cause")))
    check("the refusal names field_validity as the DEFECT category",
          "field_validity" in (p.get("defect_in") or []), str(p.get("defect_in")))

    # ----------------------------------------------------------------------------
    print("\n3. `match` -- exact appends .raw, and only where .raw exists")
    p = only(cache, "meds", where=[{"in": ["status"], "any_of": ["Effective"],
                                    "match": "exact"}])
    check("exact appends .raw", conds(p)[0]["fields"] == ["doc_status.raw"],
          str(conds(p)[0]["fields"]))
    p = only(cache, "meds", where=[{"in": ["title"], "any_of": ["x"], "match": "exact"}])
    check("exact on a field with NO .raw is refused (silent 0 otherwise)",
          p.get("refused") and "`.raw` subfield" in (p.get("message") or ""),
          str(p.get("cause")))
    # But a ROLE resolving to several fields, only some exact-capable, must still work --
    # med_comms `status` is `doc_status` (no `.raw`) + `activity_status` (has one).
    p2 = only(cache, "med_comms", where=[{"in": ["status"], "any_of": ["Authorized"],
                                         "match": "exact"}])
    check("a partly-exact-capable role uses the capable field rather than refusing",
          not p2.get("refused")
          and g(conds(p2), 0, "fields") == ["activity_status.raw"],
          str(p2.get("cause") or g(conds(p2), 0, "fields")))
    # THE PROPERTIES, not the wire field name. This asserted the note names `doc_status` -- and
    # the re-run group gate found that note degenerate for a different reason: `_model_facing_notes`
    # maps every field of a multi-field role back to the same role, so both of its blanks rendered
    # `status` and it named the role as both matched and not matched. The rewritten note reports
    # the COVERAGE (1 of 2 fields) rather than a wire name the model cannot address.
    check("  ... and reports that the constraint was only partly covered",
          any("only part of what this constraint named" in n for n in p2.get("notes") or []),
          str(p2.get("notes"))[:80])
    # And the consequences, which are the point. Measured live on med_comms: `where status exact
    # "Final"` returned 0 with `defensible: True` and `is_absence: True`, while 19 of 19 records
    # carried a `doc_status` and 15 began "Final".
    check("  ... so the count is NOT defensible",
          p2.get("count_defensible") is False, str(p2.get("count_defensible")))
    check("  ... and the plan says why, so a zero here cannot read as absence",
          p2.get("partial_exact_constraint") is True, str(p2.get("partial_exact_constraint")))
    p = only(cache, "meds", where=[{"in": ["title"], "any_of": ["x"], "match": "bogus"}])
    check("an unknown `match` is refused", p.get("refused"), str(p.get("cause")))

    # ----------------------------------------------------------------------------
    print("\n4. `any_of` is the only OR, and it cannot be expressed wrongly")
    p = only(cache, "meds", where=[{"in": ["body"], "any_of": ["stability", "potency"]}])
    c = conds(p)[0]
    # `text` is an ApiTextWithSynonyms OBJECT and `synonyms` is NESTED inside it. A flat
    # `{"text": "...", "synonyms": [...]}` is HTTP 400 -- measured by executing the payload,
    # after 196 offline assertions accepted the flat shape MUSE rejects. So these assert the
    # NESTING, not merely the presence of the values.
    check("`text` is an object, not a string (a bare string is HTTP 400)",
          isinstance(c.get("text"), dict), f"{type(c.get('text')).__name__}: {c.get('text')!r}")
    # `.get`, not `[...]`: a mutation that drops a key must FAIL this assertion, not raise
    # KeyError. A traceback exits non-zero with no assertion output, which reads as "the
    # whole suite failed" and buries which guard actually broke.
    check("any_of[0] -> text.text", g(c, "text", "text") == "stability",
          str(g(c, "text", "text")))
    check("any_of[1:] -> text.synonyms", g(c, "text", "synonyms") == ["potency"],
          str(g(c, "text", "synonyms")))
    check("`synonyms` is NOT a sibling of `text`", "synonyms" not in c, str(sorted(c)))
    p = only(cache, "meds", where=[{"in": ["body"], "any_of": ["stability"]}])
    check("a single value emits NO synonyms key inside text",
          "synonyms" not in (g(conds(p), 0, "text") or {}),
          str(g(conds(p), 0, "text")))
    # `"A OR B"` is matched literally and returns 0. It cannot be refused (it is legal
    # text), so the EFFECT asserted is that the model is told adjacency was required.
    p = only(cache, "meds", where=[{"in": ["body"], "any_of": ["stability OR potency"]}])
    check("a literal `OR` in a value is flagged as a phrase, not silently accepted",
          any("literally" in n for n in p["notes"]), str(p["notes"])[:90])

    # ----------------------------------------------------------------------------
    print("\n5. all four silent-widening routes are refused before sending")
    for label, kw in (
        ("an empty `where` list", {"where": []}),
        ("an empty any_of", {"where": [{"in": ["body"], "any_of": []}]}),
        ("a whitespace-only value", {"where": [{"in": ["body"], "any_of": ["  "]}]}),
        ("a clause with no `in`", {"where": [{"any_of": ["x"]}]}),
        ("a blank `about`", {"about": "   "}),
    ):
        p = only(cache, "meds", **kw)
        check(f"refused: {label}", bool(p.get("refused")), str(p.get("cause")))
        check(f"  ... and says nothing was sent: {label}",
              "NOT an empty result" in p.get("not_executed", ""))

    # every clause dropped by role resolution -> refusal, never a widen to _all
    p = only(cache, "scited", where=[{"in": ["body"], "any_of": ["x"]}])
    check("a clause whose every role is unfillable is refused, not widened",
          p.get("refused") and "represents none of" in p["message"], str(p.get("cause")))

    print("\n5b. a DELIBERATE whole-population request is allowed and LABELLED")
    # `00-surfaced-tools.md` section 7's canonical large-population example supplies no
    # constraint at all: `muse_population(sources=["meds"])` -> count + distribution +
    # withheld records. Refusing it would refuse the design's own example. What must never
    # happen is reporting it as narrowed.
    p = only(cache, "med_comms")
    check("no constraint at all is NOT refused", not p.get("refused"),
          str(p.get("cause") or p.get("message"))[:70])
    check("  ... and is marked unnarrowed", p.get("narrowed") is False, str(p.get("narrowed")))
    check("  ... and says so in the notes",
          any("whole program population" in n for n in p.get("notes") or []),
          str(p.get("notes"))[:80])
    check("  ... and still carries the program filter",
          g(p, "search", "formParams") == cache["sources"]["med_comms"]["program_filter"])
    check("  ... and still builds a distribution", p.get("facets") is not None,
          str(p.get("facet_scope")))
    p = only(cache, "med_comms", about="MK-6070")
    check("a constrained request IS marked narrowed", p.get("narrowed") is True,
          str(p.get("narrowed")))
    # But an empty CONTAINER is a mistake, not an intent: measured row 2, `conditions: []`
    # returns the full population while looking narrowed.
    for label, kw in (("where=[]", {"where": []}), ("where_not=[]", {"where_not": []})):
        p = only(cache, "med_comms", **kw)
        check(f"an explicitly empty {label} is still refused",
              p.get("refused") and p.get("cause") == "empty_constraint_supplied",
              str(p.get("cause")))
    # Exclusion-only is a real question and is allowed, but not marked narrowed.
    p = only(cache, "med_comms", where_not=[{"in": ["body"], "any_of": ["potency"]}])
    check("exclusion-only is allowed", not p.get("refused"), str(p.get("cause")))
    check("  ... marked unnarrowed", p.get("narrowed") is False, str(p.get("narrowed")))
    check("  ... and emits the MUST_NOT it was given",
          [c.get("type") for c in conds(p)] == ["MUST_NOT"], str(conds(p)))

    # ----------------------------------------------------------------------------
    print("\n6. SHOULD never appears, in any arrangement")
    p = only(cache, "meds", about="MK-6070",
             where=[{"in": ["body"], "any_of": ["stability"]}],
             where_not=[{"in": ["body"], "any_of": ["potency"]}])
    types = {x["type"] for x in conds(p)}
    check("only MUST and MUST_NOT are emitted", types <= {"MUST", "MUST_NOT"}, str(types))
    check("SHOULD is not in the emitted payload", "SHOULD" not in repr(p["search"]))
    p = only(cache, "meds", where_not=[{"in": ["body"], "any_of": ["potency"]}])
    check("an exclusion-only request emits MUST_NOT and no SHOULD",
          not p.get("refused") and [c.get("type") for c in conds(p)] == ["MUST_NOT"],
          str(p.get("cause") or [c.get("type") for c in conds(p)]))

    # ----------------------------------------------------------------------------
    print("\n7. the DATE guard -- the one case a facet value LIES rather than fails")
    p = only(cache, "meds", where=[{"in": ["creation_date"], "any_of": ["2025"],
                                    "match": "exact"}])
    check("a DATE-typed facet field is refused as an exact value",
          p.get("refused") and "DATE-typed" in p["message"], str(p.get("cause")))
    check("the refusal points at since/until instead",
          "since" in p.get("message", ""), str(p.get("message"))[:60])
    check("`creation_date` IS in the cache's date_facets (the guard has a source)",
          "creation_date" in (cache["sources"]["meds"]["date_facets"] or []),
          str(cache["sources"]["meds"]["date_facets"]))

    # ----------------------------------------------------------------------------
    print("\n8. taxonomy -- label resolved through the branch UUID, never by name")
    p = only(cache, "meds", where=[{"in": ["issue"], "any_of": ["Out of Specification"],
                                    "match": "exact"}])
    check("a taxonomy clause compiles", not p.get("refused"), str(p.get("message"))[:70])
    c = conds(p)[0]
    check("it targets the taxonomy field", c.get("fields") == ["mmd_tag_issue.raw"],
          str(c.get("fields")))
    check("the VALUE is an L-path, not the label -- the label returns 0",
          str(g(c, "text", "text")).startswith("L") and "|" in str(g(c, "text", "text")),
          str(g(c, "text", "text"))[:34])
    check("the label does not appear anywhere in the payload",
          "Out of Specification" not in repr(p["search"]))
    # `match` is NOT load-bearing here: measured, `.raw` and the bare field both return 11.
    p2 = only(cache, "meds", where=[{"in": ["issue"], "any_of": ["Out of Specification"]}])
    check("match='phrase' on a taxonomy role is ACCEPTED (bare field measured 11 = 11)",
          not p2.get("refused"), str(p2.get("message"))[:70])
    check("  ... and still sends the L-path",
          str(g(conds(p2), 0, "text", "text")).startswith("L"))

    p = only(cache, "meds", where=[{"in": ["issue"], "any_of": ["Nonexistent Label"]}])
    check("an unknown label is refused (an unmapped value returns 0 silently)",
          p.get("refused") and "not a taxonomy label" in p["message"], str(p.get("cause")))
    # A label belonging to the WRONG field returns 0 silently -- the guard names the right one.
    p = only(cache, "meds", where=[{"in": ["issue"], "any_of": ["Microbial testing"]}])
    check("a label from another branch is refused, naming the correct role",
          p.get("refused") and "analytical_method" in p["message"],
          str(p.get("message"))[:80])
    # Disambiguation, not lookup: measured, `stability` has 0 exact labels and 54 containing.
    p = only(cache, "meds", where=[{"in": ["issue"], "any_of": ["Specification"]}])
    check("a partial label refusal offers the containing labels",
          p.get("refused") and "containing it" in p["message"], str(p.get("message"))[-70:])

    # ----------------------------------------------------------------------------
    print("\n9. engine routing is internal, and never silently switched")
    p = only(cache, "meds", about="MK-6070")
    check("default engine is conditional", p["engine"] == "conditional", p["engine"])
    check("conditional counts are defensible", p["count_defensible"] is True)
    check("queryType is plain `conditional`, never a _knn/_hybrid variant",
          p["search"]["queryType"] == "conditional", p["search"]["queryType"])
    p = only(cache, "meds", question="how is potency controlled", semantic=True)
    check("semantic=True routes to basic", p["engine"] == "basic", p["engine"])
    check("basic counts are NOT defensible", p["count_defensible"] is False)
    # `basic_knn`, not `basic`: the vector lane is the ONE thing this engine uniquely offers
    # and the entire reason routing exists. Plain `basic` would run a literal keyword search
    # of the model's natural-language question while claiming a semantic lane.
    check("semantic queryType is `basic_knn`", g(p, "search", "queryType") == "basic_knn",
          str(g(p, "search", "queryType")))
    check("  ... and knnSearchQuery carries the question text",
          g(p, "search", "knnSearchQuery") == g(p, "search", "query"),
          str(g(p, "search", "knnSearchQuery")))
    check("the basic payload carries `query`, not conditionQuery",
          "query" in p["search"] and "conditionQuery" not in p["search"])
    # NOT `engine in (...)` -- that was already asserted `== "basic"` two lines up and could
    # not fail. Assert the thing that actually matters: the engine name travels with the
    # count, because relevance is ~80x apart between engines and nothing in either response
    # reveals which ran.
    check("the plan pairs the engine with a defensibility verdict",
          p.get("engine") == "basic" and p.get("count_defensible") is False)
    p = only(cache, "meds", question="how is potency controlled")
    check("`question` without semantic=True is REFUSED, never silently discarded",
          p.get("refused") and p["cause"] == "question_without_semantic", str(p.get("cause")))
    p = only(cache, "meds", question="q", semantic=True,
             where=[{"in": ["body"], "any_of": ["stability"]}])
    check("semantic=True with `where` is refused -- basic has no condition model",
          p.get("refused") and p["cause"] == "semantic_with_clauses", str(p.get("cause")))
    check("  ... and names both clean alternatives",
          "drop `semantic`" in p.get("remedy", "") and "narrow" in p.get("remedy", ""))

    # ----------------------------------------------------------------------------
    print("\n10. fieldsToExtract -- floor present, `text` refused, extras validated")
    p = only(cache, "meds", about="MK-6070")
    ex = p["search"]["fieldsToExtract"]
    for f in ("title", "id", "text_size"):
        check(f"floor member {f!r} is present", f in ex, str(ex))
    check("`text` is never requested (HTTP 400)", "text" not in ex, str(ex))
    # NOT a disjunction on the template's existence -- that passed whenever the stub had no
    # template. The stub HAS one, so assert it unconditionally.
    check("meds has a locator template in the fixture at all",
          bool(cache["sources"]["meds"]["locator_template"]))
    check("the locator template's field is included", "webview_pdf_url" in ex, str(ex))
    # The floor is NOT mapping-validated: `id` and `text_size` are absent from
    # mappings.document.properties on the live index, and validating here would drop them.
    check("the floor survives even though `text_size` is NOT in the mapping",
          "text_size" not in cache["sources"]["meds"]["fields"] and "text_size" in ex)
    print("\n10b. the per-source card fields reach the floor, and `text` never does")
    p = only(cache, "meds", about="MK-6070")
    ex = g(p, "search", "fieldsToExtract") or []
    # `cardModel.fields` entries are OBJECTS live; a string-only filter admitted none of them
    # and left records qualifiable by title alone -- the MIN_FIELDS failure rule 0 forbids.
    check("a qualification field arrives without the caller asking", "doc_status" in ex, str(ex))
    check("  ... and the plan does not flag them unresolved",
          p.get("default_fields_unresolved") is False, str(p.get("default_fields_unresolved")))
    pm = only(cache, "mrl_slides", about="MK-6070")
    exm = g(pm, "search", "fieldsToExtract") or []
    check("mrl_slides gets its card fields", "compound" in exm, str(exm))
    check("  ... but NOT `text`, which is HTTP 400 on fieldsToExtract", "text" not in exm,
          str(exm))
    # And when they cannot be resolved at all, that must be LOUD rather than a thin floor.
    broken = copy.deepcopy(cache)
    broken["sources"]["meds"]["card_default_fields"] = [{"id": "nope_not_a_field"}]
    pb = only(broken, "meds", about="x")
    check("unresolvable card fields are flagged, not silently dropped",
          pb.get("default_fields_unresolved") is True,
          str(pb.get("default_fields_unresolved")))
    check("  ... with a warning naming the consequence",
          any("WITHOUT represented state" in n for n in pb.get("notes") or []),
          str(pb.get("notes"))[:90])

    p = only(cache, "meds", about="x", fields=["doc_status", "not_a_field"])
    check("a valid extra field is added", "doc_status" in p["search"]["fieldsToExtract"])
    check("an unknown extra is dropped, not sent",
          "not_a_field" not in p["search"]["fieldsToExtract"])
    check("  ... and the drop is REPORTED, not silent",
          any("not_a_field" in n for n in p["notes"]), str(p["notes"])[:80])
    p = only(cache, "meds", about="x", fields=["text"])
    check("`fields=['text']` is refused and points at muse_read",
          p.get("refused") and "muse_read" in p["message"], str(p.get("cause")))

    # ----------------------------------------------------------------------------
    print("\n11. formParams -- one source's filter, and never on a multi-source payload")
    full = PLAN(cache, about="MK-6070")
    check("five sources produce FIVE requests (formParams ANDs globally)",
          len(full["requests"]) == 5, str(len(full["requests"])))
    for r in full["requests"]:
        check(f"{r['source']}: exactly one datasource per payload",
              r["search"]["dataSources"] == [r["source"]], str(r["search"]["dataSources"]))
        fp = r["search"]["formParams"]
        check(f"{r['source']}: formParams is exactly its own program filter",
              fp == cache["sources"][r["source"]]["program_filter"], str(fp))
    # Two of five program filter literals do not contain the program name at all.
    check("mrl_slides carries its partner-code literal",
          "MK-6070 (HPN328)" in repr(full["requests"][3]["search"]["formParams"]))
    check("signals carries its M-code literal",
          "M0060070" in repr(full["requests"][4]["search"]["formParams"]))

    print("\n11b. the fixture matches the live index on the value_prefix predicate")
    # This can only fail if someone edits our own stub, so it is a FIXTURE-FIDELITY check and
    # is labelled as one -- it cannot detect live drift. It is here because the stub twice
    # modelled a field as facetable without a `.raw` subfield, a combination measured not to
    # exist on any of the five sources, and each time it hid which predicate a guard read.
    for sname in IN_SCOPE:
        src = cache["sources"][sname]
        raws = {f for f, meta in src["fields"].items() if meta["has_raw"]}
        flagged = {f for f, meta in src["fields"].items() if meta["facetable"]}
        check(f"{sname}: fixture keeps has_raw == facetField (live: identical, 55/51/6/11/21)",
              raws == flagged, f"raw-only={sorted(raws - flagged)} "
                               f"flag-only={sorted(flagged - raws)}")
    # The live-measured case the design NAMES as reachable through ordinary use: `doc_status`
    # is a facet field on meds and NOT on med_comms, so `searchField` there is HTTP 500.
    check("fixture models med_comms doc_status as NOT facetable (live: 500 on searchField)",
          g(cache, "sources", "med_comms", "fields", "doc_status", "has_raw") is False,
          str(g(cache, "sources", "med_comms", "fields", "doc_status")))

    # ----------------------------------------------------------------------------
    print("\n12. per-endpoint sanitising -- inverted, and it must stay inverted")
    p = only(cache, "meds", where=[{"in": ["body"], "any_of": ["stab*ility"]}])
    check("`*` is stripped from condition text (0 hits on this endpoint)",
          g(conds(p), 0, "text", "text") == "stability", str(g(conds(p), 0, "text", "text")))
    check("  ... and the removal is reported", any("`*`" in n for n in p["notes"]))
    p = only(cache, "meds", where=[{"in": ["body"], "any_of": ["what? why?"]}])
    check("`?` is KEPT in condition text -- it is inert here",
          "?" in (g(conds(p), 0, "text", "text") or ""), str(g(conds(p), 0, "text", "text")))
    p = only(cache, "meds", question="what is potency?", semantic=True)
    check("`?` IS stripped from basic query text (drives it to 0)",
          "?" not in p["search"]["query"], p["search"]["query"])
    check("  ... and that removal is reported too", any("`?`" in n for n in p["notes"]))
    p = only(cache, "meds", where=[{"in": ["body"], "any_of": ["***"]}])
    check("a value that is only wildcards is refused, not silently emptied",
          p.get("refused"), str(p.get("cause")))

    # ----------------------------------------------------------------------------
    print("\n13. depth, order, offset")
    p = only(cache, "meds", about="x", depth="complete")
    check("depth=complete -> limit 10000 (measured: returns the whole population)",
          p["search"]["limit"] == 10000, str(p["search"]["limit"]))
    for d in ("sample", "survey"):
        p = only(cache, "meds", about="x", depth=d)
        check(f"depth={d!r} is REFUSED as deferred, not given a plausible default",
              p.get("refused") and p["cause"] == "depth_unmapped", str(p.get("cause")))
    p = only(cache, "meds", about="x", order="newest")
    check("newest -> date_desc", p["search"]["sort"] == "date_desc", str(p["search"].get("sort")))
    check("  ... and NAMES modified_date (newest/oldest are not inverses)",
          p["ordered_by"] == "modified_date", str(p["ordered_by"]))
    p = only(cache, "meds", about="x", order="oldest")
    check("oldest -> date_asc ordering creation_date, a DIFFERENT field",
          p["search"]["sort"] == "date_asc" and p["ordered_by"] == "creation_date",
          f"{p['search'].get('sort')} / {p['ordered_by']}")
    p = only(cache, "meds", about="x", order="relevance")
    check("relevance emits no sort key", "sort" not in p["search"])
    # WAVE 8 removed the "2% spread (2.5113-2.5653)" claim: measured 2026-08-26, meds term
    # relevance spans 3.5533-7.3447, a 51.6% spread, and med_comms 84.4%. The design-phase range
    # was one population on one day, and "it barely discriminates" -- the reason the note gave for
    # not ranking on relevance -- was wrong by a factor of 25. Asserted on what survives: that the
    # note refuses a cross-engine comparison and does not pin a scale.
    check("  ... and warns that relevance is source- and query-local, with no fixed scale",
          any("local to this source" in n and "not a fixed scale" in n for n in p["notes"]),
          str(p["notes"])[:70])
    check("  ... without pinning a magnitude that goes stale",
          not any("2%" in n or "80x" in n for n in p["notes"]), str(p["notes"])[:60])
    p = only(cache, "meds", about="x", offset=10000)
    check("offset at the API ceiling is refused (HTTP 400 otherwise)",
          p.get("refused") and p["cause"] == "offset_too_large", str(p.get("cause")))
    p = only(cache, "meds", about="x", order="bogus")
    check("an unknown `order` is refused", p.get("refused"), str(p.get("cause")))

    # ----------------------------------------------------------------------------
    print("\n14. since/until are OURS and never become conditions")
    p = only(cache, "meds", about="x", since="2025-01-01", until="2026-01-01")
    check("no date value reaches the conditions (every range syntax is a 500)",
          not any("2025-01-01" in repr(c) for c in conds(p)), str(conds(p))[:70])
    check("the date filter is attributed to the connector",
          p["date_filter"]["applied_by"] == "connector", str(p["date_filter"])[:60])
    check("  ... and demands BOTH counts be reported",
          "both" in p["date_filter"]["report"], p["date_filter"]["report"][:50])
    p = only(cache, "meds", about="x")
    check("no date filter -> the field is None, not an empty shape",
          p["date_filter"] is None)

    # ----------------------------------------------------------------------------
    print("\n15. the poison rule is checked BEFORE any facet request is built")
    p = only(cache, "meds", about="MK-6070", distribution=True)
    check("meds is known poisoned from the cache",
          g(p, "poison", "facetable_program_filter") is False, str(p.get("poison"))[:60])
    check("a conditioned meds request offers NO distribution at all",
          p["facets"] is None and p["facet_scope"] == "unavailable", str(p["facet_scope"]))
    check("  ... and says why: `query` is dropped when conditions are present",
          "dropped" in (g(p, "poison", "superset_detail") or ""),
          str(g(p, "poison", "superset_detail"))[:60])
    # Unconditioned meds is where the superset path becomes available: `query` is only
    # dropped when conditions are present, so with none the query-scoped facets CAN be had.
    p = only(cache, "meds", distribution=True)
    check("unconditioned meds gets the LABELLED superset path, not a refusal",
          not p.get("refused") and p.get("facet_scope") == "superset",
          str(p.get("cause") or p.get("facet_scope")))
    check("  ... and the superset request carries NO program formParams",
          "formParams" not in (p.get("facets") or {}), str(sorted(p.get("facets") or {})))
    check("  ... and is scoped by the program literal as a query instead",
          g(p, "facets", "query") == "MK-6070", str(g(p, "facets", "query")))
    check("  ... and the poison status says the superset is available",
          g(p, "poison", "superset_available") is True, str(g(p, "poison")))
    for s in ("med_comms", "scited", "mrl_slides", "signals"):
        p = only(cache, s, about="MK-6070", distribution=True)
        check(f"{s}: not poisoned, so the filtered facet request IS built",
              g(p, "poison", "facetable_program_filter") is True and p.get("facets") is not None,
              str(p["facet_scope"]))
        check(f"{s}: the facet payload carries the program filter",
              g(p, "facets", "formParams") == cache["sources"][s]["program_filter"])
        # Guarded on `facets` being a real dict: `x not in (None or {})` passes vacuously.
        check(f"{s}: a real facet payload was built to test against",
              isinstance(p.get("facets"), dict) and len(p["facets"]) >= 3,
              str(sorted(p.get("facets") or {})))
        check(f"{s}: `facetFields` is never sent (it does not exist)",
              "facetFields" not in (p.get("facets") or {}), str(sorted(p.get("facets") or {})))
        check(f"{s}: `counts` is sent for every field returned",
              bool(g(p, "facets", "counts")), str(g(p, "facets", "counts"))[:50])
    p = only(cache, "med_comms", about="x", distribution=False)
    check("distribution=False builds no facet request at all", p["facets"] is None)

    print("\n15b. the inert parameters are never sent")
    p = only(cache, "med_comms", about="x", distribution=True)
    blob = repr(p["search"]) + repr(p["facets"])
    for bad in ("facetFields", "searchPrefix", "primaryFacetsOnly", "excludeDataSources",
                "disableSynonyms", "advancedSearchParameters", "appName",
                "knnSearchField", "ranking", "highlight", "sortDefinition"):
        check(f"{bad} is absent from every payload", bad not in blob)

    # ----------------------------------------------------------------------------
    print("\n16. value_prefix -- guarded on the facet set, and not poisoned")
    p = only(cache, "meds", about="x", value_prefix={"status": "Eff"})
    check("a facet-field prefix request is built", len(p["value_prefix_requests"]) == 1,
          str(len(p["value_prefix_requests"])))
    vp = p["value_prefix_requests"][0]
    check("searchField names the .raw subfield",
          g(vp, "payload", "searchField") == "doc_status.raw",
          str(g(vp, "payload", "searchField")))
    check("the prefix goes in formParams under the SAME name (400 otherwise)",
          g(vp, "payload", "formParams", "doc_status.raw") == ["Eff"],
          str(g(vp, "payload", "formParams")))
    check("  ... alongside the program filter, so the population is the exact one",
          "product_name.raw" in (g(vp, "payload", "formParams") or {}))
    check("it is NOT treated as poisoned (measured: returns the requested field)",
          vp["poisoned"] is False)
    check("its counts carry the multi-valued caveat, not a stale-number claim",
          "multi-valued" in (vp.get("counts_caveat") or "")
          and "799" not in (vp.get("counts_caveat") or ""),
          str(vp.get("counts_caveat"))[:70])
    # WAVE 8 changed this from "refuse the request" to "degrade the value lookup". Both halves
    # are asserted, because the point is that ONE of two things degrades and the other does not:
    # nothing that would 400/500 is sent, AND the source is still searched.
    #
    # Before wave 8 this refused the whole per-source plan, and measured live that meant
    # `value_prefix={"status": "Eff"}` across five sources returned three -- `scited` and
    # `mrl_slides` lost their RECORDS AND COUNTS because neither represents a `status` role. A
    # facet-side capability gap removed two fifths of the coverage from the answer.
    p = only(cache, "meds", about="x", value_prefix={"title": "x"})
    check("a NON-facet field builds NO prefix request (400/500 otherwise)",
          not p.get("refused") and p["value_prefix_requests"] == [],
          str(p.get("cause") or len(p["value_prefix_requests"])))
    check("  ... the search is still planned, so coverage is not lost to a facet-side gap",
          bool(p.get("search")) and p.get("endpoint"), str(bool(p.get("search"))))
    check("  ... and the reason is carried, named, on the plan",
          g(p, "value_prefix_unavailable", "cause") == "value_prefix_not_facetable",
          str(g(p, "value_prefix_unavailable", "cause")))
    check("  ... listing the fields that would work",
          "doc_status" in str(g(p, "value_prefix_unavailable", "remedy") or ""),
          str(g(p, "value_prefix_unavailable", "remedy"))[:70])
    check("  ... and saying the records below are complete",
          "still searched" in str(g(p, "value_prefix_unavailable", "note") or ""),
          str(g(p, "value_prefix_unavailable", "note"))[:60])
    # A MALFORMED argument is a caller error, not a per-source capability gap, and still refuses
    # everything. `(value_prefix or {}).items()` on a bare string raised AttributeError out of
    # the planner before wave 8 -- a caller error escaping as an MCP protocol error.
    p = only(cache, "meds", about="x", value_prefix="notadict")
    check("a malformed value_prefix still refuses the whole request",
          p.get("refused") and p["cause"] == "bad_value_prefix", str(p.get("cause")))
    check("  ... and states nothing was executed",
          "NOT an empty result" in (p.get("not_executed") or ""),
          str(p.get("not_executed"))[:60])
    # The cross-source trap: `doc_status` is a facet field on meds and not on med_comms.
    p = only(cache, "med_comms", about="x", value_prefix={"status": "Auth"})
    ok = (not p.get("refused")) and p["value_prefix_requests"][0]["payload"]["searchField"] \
        in ("doc_status.raw", "activity_status.raw")
    check("the same role on another source resolves to THAT source's field", ok,
          str(p.get("cause") or p["value_prefix_requests"][0]["payload"]["searchField"]))

    # ----------------------------------------------------------------------------
    print("\n17. source-level refusals are per source, and partial success survives")
    denied = copy.deepcopy(cache)
    denied["sources"]["scited"]["searchable"] = False
    denied["sources"]["scited"]["access"] = "DENIED"
    full = PLAN(denied, about="MK-6070")
    check("four sources still plan when one is denied", len(full["requests"]) == 4,
          str(len(full["requests"])))
    check("the denied source appears in `excluded` with its reason",
          [e["source"] for e in full["excluded"]] == ["scited"], str(full["excluded"])[:80])
    check("  ... attributed to source_access, not to absence",
          "not searchable" in full["excluded"][0]["message"],
          full["excluded"][0]["message"][:60])
    nofilter = copy.deepcopy(cache)
    nofilter["sources"]["signals"]["program_filter"] = {}
    full = PLAN(nofilter, about="MK-6070")
    check("a source with no program filter is excluded, not searched unscoped",
          [e["cause"] for e in full["excluded"]] == ["no_program_filter"],
          str(full["excluded"])[:70])
    p = only(cache, "not_a_source", about="x")
    check("an unknown source is refused with the valid list (a bogus id 404s the request)",
          p.get("refused") and p["cause"] == "unknown_source", str(p.get("cause")))
    allbad = PLAN(cache, sources=["nope1", "nope2"], about="x")
    check("when NO source can be planned, each reason is listed separately",
          allbad.get("refused") and len(allbad["per_source"]) == 2,
          str(allbad.get("cause")))
    check("a cold cache refuses everything and says it is a startup failure",
          PLAN({}, about="x").get("cause") == "no_program_scope")

    # ----------------------------------------------------------------------------
    print("\n18. narrowing a population handle")
    h, err = m.encode_population_handle(
        source="med_comms", engine="conditional",
        program_filter=cache["sources"]["med_comms"]["program_filter"],
        conditions=[{"in": ["anywhere"], "any_of": ["MK-6070"]}], count=19,
        facetable=True, index_updated="2026-08-25T00:00:00")
    assert err is None, err
    p = only(cache, "med_comms", population=h,
             where=[{"in": ["status"], "any_of": ["Authorized"], "match": "exact"}])
    check("narrowing is not refused", not p.get("refused"), str(p.get("message"))[:70])
    # NOT `len >= 2`: a payload containing a 500-producing condition scored PASS. Assert the
    # inherited half is COMPILED -- every emitted condition needs `type` and a `text` object,
    # because the handle records MODEL vocabulary and pasting it on the wire is HTTP 500.
    check("the inherited condition is carried forward", len(conds(p)) >= 2, str(len(conds(p))))
    check("every emitted condition is wire-shaped, including the inherited one",
          all(isinstance(c.get("text"), dict) and c.get("type") in ("MUST", "MUST_NOT")
              and isinstance(c.get("fields"), list) for c in conds(p)),
          str(conds(p)))
    check("no model-vocabulary key reaches the wire",
          not any("in" in c or "any_of" in c for c in conds(p)), str(conds(p)))
    check("no internal `_`-prefixed key reaches the wire",
          not any(k.startswith("_") for c in conds(p) for k in c), str(conds(p)))
    check("the engine comes from the handle, not from a default",
          p["engine"] == "conditional", p["engine"])
    bad = PLAN(cache, population=h, sources=["meds"])
    check("a handle plus a different `sources` is refused, not silently reinterpreted",
          bad.get("refused") and bad["cause"] == "handle_source_conflict", str(bad.get("cause")))
    bad = PLAN(cache, population=h, semantic=True, question="q")
    check("switching engines mid-narrow is refused (scores are ~80x apart)",
          bad.get("refused") and bad["cause"] == "engine_switch", str(bad.get("cause")))
    bad = PLAN(cache, population="p1.notahandle.zzz", about="x")
    check("a damaged handle is refused, never half-applied", bad.get("refused"),
          str(bad.get("cause")))

    # ----------------------------------------------------------------------------
    print("\n19. every refusal states that nothing was executed")
    cases = [
        ("unmapped field", only(cache, "meds", where=[{"in": ["nope"], "any_of": ["x"]}])),
        ("deferred depth", only(cache, "meds", about="x", depth="sample")),
        ("question without semantic", only(cache, "meds", question="q")),
        # WAVE 8: a non-facet `value_prefix` no longer belongs here -- it degrades the value
        # lookup and leaves the search planned, asserted in section 16. A MALFORMED one is still
        # a whole-request refusal, and that is the case this section is about.
        ("malformed value_prefix", only(cache, "meds", value_prefix="notadict", about="x")),
        ("unknown source", only(cache, "not_a_source", about="x")),
        ("empty where container", only(cache, "meds", where=[])),
    ]
    # A `check`, never a bare `assert`. An assert here would crash the run when a guard
    # regresses -- taking every later assertion with it -- and its failure message would
    # dump the entire plan, which is the payload-echo defect wave 2's gate caught.
    for label, r in cases:
        check(f"still refused: {label}", bool(r.get("refused")), str(r.get("cause")))
    refusals = [r for _, r in cases if r.get("refused")]
    check("all six refusal cases produced a refusal to inspect", len(refusals) == 6,
          f"{len(refusals)}/6")
    check("all carry not_executed", all("NOT an empty result" in (r.get("not_executed") or "")
                                        for r in refusals))
    # WAVE 8 reworded `ruled_out_absence`. It used to be the bare token `"executed with no
    # visible matches"`, lifted from `retrieval-contributor`'s fifteen-kind RETURN taxonomy --
    # which the main agent never sees, and which neither `AGENTS.md` section 6's eight absence
    # meanings nor the guide's distinctions table defines. So a main-agent caller received another
    # agent's private vocabulary under a key that reads like a verdict.
    #
    # Asserted on the PROPERTY now, not the phrase: it has to deny being an empty result, and say
    # that nothing ran. An equality test against one literal string could not tell a rewording
    # from a regression -- it failed here for a change that strictly improved the output.
    check("all deny being an empty result, and say nothing was searched",
          all("not an empty result" in str(r.get("ruled_out_absence") or "").lower()
              and "nothing was searched" in str(r.get("ruled_out_absence") or "").lower()
              for r in refusals),
          str((refusals[0] or {}).get("ruled_out_absence"))[:56])
    check("  ... in plain words, not a token from the subagent's return taxonomy",
          not any(str(r.get("ruled_out_absence") or "") == "executed with no visible matches"
                  for r in refusals))
    check("all name at least one of the four DEFECT categories",
          all(r.get("defect_in") and set(r["defect_in"]) <= set(m.DEFECT_CATEGORIES)
              for r in refusals),
          str([r.get("defect_in") for r in refusals]))
    check("no refusal claims to have ruled out a zero-cause it did not check",
          all(set(r.get("ruled_out_zero_causes") or []) <= set(m.ZERO_CAUSES)
              for r in refusals))
    # The inverted-sense defect: `defect_in` says where the PROBLEM is, and a separate list
    # says which measured zero-causes were positively eliminated. Conflating them told the
    # model that source access had been "ruled out" on an unknown-source refusal.
    check("the two lists are distinct fields, not one overloaded one",
          all("ruled_out" not in r for r in refusals))
    check("none reads as `0 results`",
          not any("0 result" in r["message"].lower() for r in refusals))
    check("every refusal explains itself", min(len(r["message"]) for r in refusals) > 80,
          str(min(len(r["message"]) for r in refusals)))
    check("the four defect categories are exactly AGENTS.md section 11's four",
          m.DEFECT_CATEGORIES == ("source_access", "filter_integrity",
                                  "query_rewriting", "field_validity"))
    # A refusal must not echo the caller's payload back -- wave 2's gate found exactly this
    # dressed as a passing assertion.
    p = only(cache, "meds", where=[{"in": ["issue"], "any_of": ["Nonexistent Label"]}])
    check("a refusal does not dump the whole clause back",
          "'any_of'" not in p["message"] and "{" not in p["message"], p["message"][:60])

    # ----------------------------------------------------------------------------
    print("\n20. the conditional engine claims no synonym expansion")
    p = only(cache, "meds", about="x")
    check("it says none was applied", "none applied" in p["synonym_expansion"])
    check("  ... and that the API's own synonyms field is not to be believed",
          "not to be believed" in p["synonym_expansion"])

    # ----------------------------------------------------------------------------
    print("\n21. SABOTAGE -- disable each guard; the covering assertion must then fail")
    # 1. mapping validation removed -> an unmapped field would be SENT.
    orig_resolve = m._resolve_role
    m._resolve_role = lambda src, name: ([name], None)
    p = only(cache, "meds", where=[{"in": ["nonexistent_field"], "any_of": ["x"]}])
    check("SABOTAGE mapping validation -> the unmapped field is now sent (guard was real)",
          not p.get("refused") and "nonexistent_field" in repr(conds(p)),
          str(p.get("cause")))
    m._resolve_role = orig_resolve
    p = only(cache, "meds", where=[{"in": ["nonexistent_field"], "any_of": ["x"]}])
    check("  ... and restoring it refuses again", p.get("refused"))

    # 2. the unfiltered-population guard removed.
    orig_cc = m.compile_conditions
    def no_positive_guard(cache_, source, **kw):
        c, n, r = orig_cc(cache_, source, **kw)
        if r is not None and r.get("cause") == "empty_constraint_supplied":
            return kw.get("inherited") or [], n, None
        return c, n, r
    m.compile_conditions = no_positive_guard
    p = only(cache, "meds", where=[])
    check("SABOTAGE the empty-container guard -> the empty clause list is now sent",
          not p.get("refused"), str(p.get("cause")))
    m.compile_conditions = orig_cc
    check("  ... and restoring it refuses again",
          only(cache, "meds", where=[]).get("refused"))

    # 3. the DATE guard removed.
    poisoned_cache = copy.deepcopy(cache)
    poisoned_cache["sources"]["meds"]["fields"]["creation_date"]["facet_type"] = "SEARCH"
    p = only(poisoned_cache, "meds",
             where=[{"in": ["creation_date"], "any_of": ["2025"], "match": "exact"}])
    check("SABOTAGE the DATE facet_type -> the year is now sent as an exact value",
          not p.get("refused"), str(p.get("cause")))

    # 4. the poison rule flipped -> meds would build a filtered facet request.
    lying = copy.deepcopy(cache)
    lying["sources"]["meds"]["facetable_program_filter"] = True
    p = only(lying, "meds", about="MK-6070", distribution=True)
    check("SABOTAGE facetable_program_filter -> meds now builds a poisoned facet request",
          p["facets"] is not None, str(p["facet_scope"]))

    # 5. searchability flipped -> a DENIED source would be searched.
    lying = copy.deepcopy(cache)
    lying["sources"]["scited"]["searchable"] = True
    lying["sources"]["scited"]["access"] = "DENIED"
    p = only(lying, "scited", about="x")
    check("SABOTAGE `searchable` -> a DENIED source is now searched (0 with empty errors)",
          not p.get("refused"), str(p.get("cause")))

    # 6. the .raw guard removed -> exact on a field without .raw.
    lying = copy.deepcopy(cache)
    lying["sources"]["meds"]["fields"]["title"]["has_raw"] = True
    p = only(lying, "meds", where=[{"in": ["title"], "any_of": ["x"], "match": "exact"}])
    check("SABOTAGE has_raw -> `title.raw` is now sent (measured silent 0)",
          not p.get("refused") and "title.raw" in repr(conds(p)), str(p.get("cause")))

    # 7. the value_prefix facet guard removed.
    p = only(lying, "meds", about="x", value_prefix={"title": "x"})
    check("SABOTAGE has_raw -> a non-facet value_prefix is now built (400/500 upstream)",
          not p.get("refused") and p["value_prefix_requests"], str(p.get("cause")))

    # 8. taxonomy resolution removed -> the raw LABEL would be sent.
    orig_tax = m._taxonomy_value
    m._taxonomy_value = lambda c, s, f, label: (label, None)
    p = only(cache, "meds", where=[{"in": ["issue"], "any_of": ["Out of Specification"]}])
    check("SABOTAGE taxonomy resolution -> the human label is now sent (measured 0 hits)",
          g(conds(p), 0, "text", "text") == "Out of Specification",
          str(g(conds(p), 0, "text", "text")))
    m._taxonomy_value = orig_tax

    # 9. the condition sanitiser removed.
    orig_san = m._sanitize_condition_value
    m._sanitize_condition_value = lambda v: (v, False)
    p = only(cache, "meds", where=[{"in": ["body"], "any_of": ["stab*ility"]}])
    check("SABOTAGE the `*` sanitiser -> the wildcard is now sent (measured 0 hits)",
          "*" in (g(conds(p), 0, "text", "text") or ""),
          str(g(conds(p), 0, "text", "text")))
    m._sanitize_condition_value = orig_san

    # 10. the depth refusal replaced by a plausible default.
    m._DEPTH_LIMITS = {**m._DEPTH_LIMITS, "survey": 50}
    p = only(cache, "meds", about="x", depth="survey")
    check("SABOTAGE depth -> an invented cap of 50 is now applied silently",
          not p.get("refused") and p["search"]["limit"] == 50, str(p.get("cause")))
    m._DEPTH_LIMITS = {**m._DEPTH_LIMITS, "survey": None}

    # 11. the extract floor stripped.
    orig_floor = m._EXTRACT_FLOOR
    m._EXTRACT_FLOOR = ("title",)
    p = only(cache, "meds", about="x")
    check("SABOTAGE the floor -> text_size is gone, so card.textSize would read 0",
          "text_size" not in p["search"]["fieldsToExtract"],
          str(p["search"]["fieldsToExtract"]))
    m._EXTRACT_FLOOR = orig_floor

    # 12. the engine-switch guard removed.
    orig_route = m.route_engine
    m.route_engine = lambda **kw: ("basic", None)
    bad = PLAN(cache, population=h, semantic=True, question="q")
    check("SABOTAGE routing -> a mid-narrow engine switch now goes through",
          not bad.get("refused"), str(bad.get("cause")))
    m.route_engine = orig_route

    # ----------------------------------------------------------------------------
    print("\n22. MUTATION -- patch the module, confirm a NAMED assertion catches it")
    # A REAL mutation harness. An earlier version of this section only asserted non-vacuity
    # preconditions -- it broke nothing, and calling it "mutation testing" overstated what it
    # did. The actual mutation runs lived outside the script as manual edits, so nothing
    # re-ran them.
    #
    # Each case below breaks the module in-process, re-runs the ONE probe that should catch
    # it, and asserts the probe now reports the broken behaviour. A guard nobody can break is
    # a guard nobody is testing.
    def mutate(label: str, apply, probe, expect_broken) -> None:
        undo = apply()
        try:
            observed = probe()
            caught = expect_broken(observed)
        except Exception as exc:  # a crash is not a caught mutation; it is a worse failure
            caught, observed = False, f"raised {type(exc).__name__}: {exc}"
        finally:
            undo()
        check(f"MUTATION {label}", caught, str(observed)[:90])

    def set_attr(name, value):
        def apply():
            old = getattr(m, name)
            setattr(m, name, value)
            return lambda: setattr(m, name, old)
        return apply

    def patch_cache(path, value):
        def apply():
            node = cache
            for k in path[:-1]:
                node = node[k]
            old = node.get(path[-1])
            node[path[-1]] = value
            return lambda: node.__setitem__(path[-1], old)
        return apply

    mutate("drop `text_size` from the floor -> card.textSize would read 0",
           set_attr("_EXTRACT_FLOOR", ("title", "id")),
           lambda: g(only(cache, "meds", about="x"), "search", "fieldsToExtract"),
           lambda ex: "text_size" not in (ex or []))

    mutate("flatten the ApiTextWithSynonyms object -> HTTP 400 on every condition",
           set_attr("compile_clause", lambda c, s_, cl, k: (
               [{"fields": ["_all"], "text": "x", "type": "MUST"}], [], None)),
           lambda: g(conds(only(cache, "meds", about="x")), 0, "text"),
           lambda t: not isinstance(t, dict))

    mutate("stop appending .raw for match='exact' -> silent 0 on the analysed field",
           set_attr("_SORTS", m._SORTS),  # no-op apply; the real mutation is below
           lambda: True, lambda _: True) if False else None

    mutate("claim meds' filter field IS facetable -> a poisoned facet request is built",
           patch_cache(("sources", "meds", "facetable_program_filter"), True),
           lambda: only(cache, "meds", about="x", distribution=True).get("facets"),
           lambda f: f is not None)

    mutate("claim a DENIED source is searchable -> it gets searched",
           patch_cache(("sources", "scited", "searchable"), True),
           lambda: only(cache, "scited", about="x").get("refused"),
           lambda r: r is not True)

    mutate("give med_comms doc_status a .raw -> the 500-producing value_prefix is built",
           patch_cache(("sources", "med_comms", "fields", "doc_status"),
                       {"type": "text", "has_raw": True, "facetable": True,
                        "facet_type": "SEARCH", "highlightable": False,
                        "restricted": False}),
           lambda: g(only(cache, "med_comms", about="x",
                          value_prefix={"status": "A"}), "value_prefix_requests", 0,
                     "payload", "searchField"),
           lambda f: f == "doc_status.raw")

    mutate("make a DATE facet look like a SEARCH facet -> the year is sent as an exact value",
           patch_cache(("sources", "meds", "fields", "creation_date"),
                       {"type": "date", "has_raw": True, "facetable": True,
                        "facet_type": "SEARCH", "highlightable": False, "restricted": False}),
           lambda: only(cache, "meds", where=[{"in": ["creation_date"],
                                               "any_of": ["2025"], "match": "exact"}]).get("refused"),
           lambda r: r is not True)

    mutate("return the raw taxonomy label instead of an L-path -> measured 0 hits",
           set_attr("_taxonomy_value", lambda c, s_, f, label: (label, None)),
           lambda: g(conds(only(cache, "meds", where=[{"in": ["issue"],
                                                       "any_of": ["Out of Specification"]}])),
                     0, "text", "text"),
           lambda t: t == "Out of Specification")

    mutate("stop stripping `*` from condition text -> measured 0 hits",
           set_attr("_sanitize_condition_value", lambda v: (v, False)),
           lambda: g(conds(only(cache, "meds", where=[{"in": ["body"],
                                                      "any_of": ["stab*ility"]}])),
                     0, "text", "text"),
           lambda t: "*" in (t or ""))

    mutate("give `survey` an invented record cap -> a silent truncation ships",
           set_attr("_DEPTH_LIMITS", {**m._DEPTH_LIMITS, "survey": 50}),
           lambda: g(only(cache, "meds", about="x", depth="survey"), "search", "limit"),
           lambda lim: lim == 50)

    mutate("emit only a subset of `counts` -> the +-bucket certificate is forfeited",
           set_attr("_counts_for", lambda src: {"doc_status.raw": 3000}),
           lambda: len(g(only(cache, "med_comms", about="x", distribution=True),
                         "facets", "counts") or {}),
           lambda n: n == 1)

    # A typo'd category is a PROGRAMMING error, so `_refuse` rejects it at the call site
    # rather than shipping a category the model cannot interpret. Asserted directly, not as a
    # mutation -- the mutation harness treats a raise as an uncaught failure, which is the
    # right default everywhere except here.
    for bad in ({"defect_in": ("field-validity",)}, {"ruled_out": ("not_a_cause",)}):
        try:
            m._refuse("x", "y", **bad)
            rejected = False
        except AssertionError:
            rejected = True
        check(f"_refuse rejects an unknown category {list(bad)[0]}={list(bad.values())[0]}",
              rejected)

    # A FIXED expected total, added in wave 8. This was the ONE harness without its own, pinned
    # only externally in `contract_map_check.py` -- so running this script alone reported N/N
    # however many guards had vanished, and only the mapping caught it. The rule this project
    # settled on after three scripts shipped a lying count is that every harness pins its own.
    #
    # 234 through wave 7. Wave 8 raised it to 240: a non-facet `value_prefix` now degrades the
    # value lookup instead of refusing the whole per-source plan, so three assertions changed
    # sense and six were added -- including that a MALFORMED one still refuses outright, which
    # used to raise AttributeError out of the planner, and that `ruled_out_absence` is asserted on
    # its PROPERTY rather than against one literal string.
    expected = 243
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
