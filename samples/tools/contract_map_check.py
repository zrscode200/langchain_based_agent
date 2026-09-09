#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp==1.28.1","httpx>=0.27","pydantic>=2","pyyaml>=6.0","truststore>=0.9",
#   "typing-extensions>=4.12",
# ]
# ///
"""The contract-assertion mapping, as executable code rather than prose.

    ./tools/contract_map_check.py

The design docs carry a contract-assertion inventory across five sections.
`00-surfaced-tools.md` §9 left "which ones become preconditions on which internal lane" as
*implementation work*, and no mapping existed -- so "79 assertions exist" was a number in a
document with nothing checking that any of them was covered.

This file is that mapping. Every entry either NAMES the harness that covers it, or is marked
`not_offline_testable` with what would establish it. An entry may not be silently absent: the point
of a mapping is to make an uncovered contract visible, which a total cannot do.

**The inventory is 79.** Counted by PARSING the five design-doc sections at runtime, not by
comparing one hand-written literal against another in this file. An earlier revision did the
latter, claimed 77, and was wrong: `07-startup-cache-endpoints.md` §7 numbers its items
`1..14` plus `7a` and `7b`, and both the count and the contiguity check used `^[0-9]+\.`, which
cannot see a sub-letter. Only `00` §9's tally of 21 for tool 1 was ever wrong; it has 13.

That miscount was not harmless. The two entries it dropped include **`07`.7a -- the only contract
in the whole inventory covered by nothing.** So the file whose stated purpose is to surface an
uncovered contract hid the only one, and then printed "0 unimplemented". Runtime parsing exists so
that cannot recur: if a doc gains an assertion, this file fails until it is mapped.

It also closes coverage debt waves 4 and 5 recorded -- four `facet_scope` states never produced,
and the `BudgetExhausted` handlers in `run_population` and `run_read`.

**Two harness families, and the mapping covers only one.** `HARNESSES` is the five MUSE harnesses,
cross-checked against `MAPPING` above. `EXTERNAL_HARNESSES` is the three that came with the
`ics-public-science` rewrite -- `literature_search` / `literature_get` -- and they are deliberately
NOT in `MAPPING`, because the inventory this file parses is the MUSE contract only. There is no
design-doc inventory for the external connector, so its 341 assertions are held by a pin and by the
disk-versus-pin check in section 3b, and by nothing else. **No mutant targets the external package.**

Exit status is 0 only if every parsed assertion has a verdict, every named harness in BOTH families
runs at its pinned total, every pinned harness is executable and declares its dependencies through
`uv`, every `external_*_check.py` on disk is pinned, and every gap-closing check holds.

The suite is **1,135**: 130 / 80 / 243 / 209 / 82 MUSE, 50 of this file, and 54 / 158 / 129 external.

**This script's green is necessary and NOT sufficient, and wave 9 measured how far from
sufficient.** `tools/mutation_gate.py` mutates the shipped `server.py` as source text and lets
this whole suite judge each mutant. **Wave 9 measured five of seven real mutants SURVIVING** --
among them per-call freshness refresh disabled while still claiming success, every scrap of
program-filter literal evidence erased, and a silently-downgraded semantic count forced to
999999, all at 729 assertions and exit 0. **After wave 10: 0 of 13 survive**, and an independent
gate confirmed 12 of the 13 die for their own property rather than incidentally.

That is the number that means something. A green total here proves the harnesses ran at their
pinned sizes; it has never proved that an invariant holds, and the gate is what does.

`mutation_gate.py` is deliberately NOT in `HARNESSES` below: it runs this script once per mutant,
so listing it would recurse. Run it as its own step before accepting a group.
"""

from __future__ import annotations

import asyncio
import copy
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from startup_cache_check import install_stubs, load_server  # noqa: E402

FAILURES: list[str] = []
CHECKS = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{': ' + detail if detail else ''}")
    if not ok:
        FAILURES.append(label)


def g(obj: Any, *path: Any) -> Any:
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


# The external connector's seven configuration variables, removed from every harness subprocess.
# `ICS_CA_BUNDLE` and `ICS_DISCLOSURE_DENY_FILE` demonstrably break fixture runs when set to
# legitimate operator values. The other five change disclosure policy or provider request identity,
# which fixture-driven harnesses must not inherit either.
SCRUBBED_ENV = frozenset({
    "ICS_CA_BUNDLE", "ICS_DISCLOSURE_DENY_FILE", "ICS_DISCLOSURE_ALLOW_TERMS",
    "ICS_EXTERNAL_CONTACT", "ICS_NCBI_API_KEY", "ICS_NCBI_EMAIL", "ICS_NCBI_TOOL",
})

# Wave 1 fixed all three external harnesses from `#!/usr/bin/env python3`, which BYPASSES the PEP-723
# dependency block: one failed outright with `ModuleNotFoundError: mcp`, and two passed only because
# the ambient interpreter happened to carry httpx and pydantic. A green total from an unpinned
# interpreter is exactly this project's failure class, and the pin below defends the COUNT, not the
# interpreter -- revert a shebang and section 3b still reports 157/157. So it is asserted.
REQUIRED_SHEBANG = "#!/usr/bin/env -S uv run --script"


def run_pinned_harness(name: str, pinned: int) -> None:
    """Run one harness and assert it reported exactly its pinned total, all passing.

    ONE runner for both harness families. A second copy of this parse is the obvious way to
    wire in a new family, and it is how a gate ends up scoring a crash as a pass -- this
    project has already fixed that once, in `mutation_gate.py`.

    Every ordinary failure routes into the -1 sentinel rather than a traceback: a timeout, a
    renamed file, a lost exec bit or an unexpected summary line otherwise kills this gate with no
    assertion output at all, which reads as "everything failed".

    The environment is SCRUBBED of the external connector's seven configuration variables, because
    the harnesses inherit them: two can break the fixture run, and five change policy or provider
    request identity. Measured, from a clean tree:

        ICS_CA_BUNDLE=/missing/ca.pem       -> `_tls_verification` raises OSError even on a
                                               MockTransport client, so every fixture GET returns
                                               `configuration_unavailable` and the redirect-blocking
                                               assertion fails. exit 1, no summary line.
        ICS_DISCLOSURE_DENY_FILE=<real>     -> `check_disclosure` builds three trackers BEFORE it
                                               overrides the variable, so an operator's own deny
                                               rule blocks the fixture query. TWO harnesses die and
                                               the suite exits 1.
        ICS_DISCLOSURE_DENY_FILE=<missing>  -> DisclosureConfigurationError, same shape.

    A legitimately-configured operator would otherwise get an unusable suite -- and worse, via
    `mutation_gate.py`'s `-1/-1` sentinel, EVERY mutant including the null-control would return
    `error`, tripping "CONTROLS BROKEN" and making the release gate report nothing at all. Scrubbing
    here rather than inside each harness covers a fourth harness for free.
    """
    env = {k: v for k, v in os.environ.items() if k not in SCRUBBED_ENV}
    stderr = ""
    try:
        proc = subprocess.run([str(HERE / name)], capture_output=True, text=True, timeout=600,
                              env=env)
        stderr = (proc.stderr or "")[-300:]
        line = [x for x in proc.stdout.splitlines() if "assertions passed" in x]
        head = line[-1].split("/")[0].strip() if line else ""
        got = int(line[-1].split("/")[1].split()[0]) if line else -1
        passed = int(head) if head.isdigit() else -1
        rc = proc.returncode
    except (subprocess.TimeoutExpired, OSError, ValueError, IndexError) as exc:
        got, passed, rc, stderr = -1, -1, -1, f"{type(exc).__name__}: {exc}"
    check(f"{name}: {passed}/{got} at pinned {pinned}",
          rc == 0 and got == pinned and passed == pinned,
          f"exit {rc}" + (f", got {got}" if got != pinned else "")
          + (f" | {stderr}" if stderr and rc != 0 else ""))


# --------------------------------------------------------------------------
# The five harnesses, with their totals PINNED
# --------------------------------------------------------------------------
# Three separate scripts have already shipped a count that was lying -- one reported "132/132"
# that was 132 of a possible 134, and two derived their own expectation from the data they were
# checking. So the totals are pinned here as well as inside each script: coverage that shrinks
# must fail, not report N/N.
HARNESSES: dict[str, int] = {
    # 102 through wave 9. Wave 10 added 12 in section 31 and the follow-up 4 more.
    # `refresh_freshness` had NO coverage in any harness at all before that -- `grep -l` returned
    # nothing -- which is why the gate's REV-A mutant used to survive. It is now pinned `killed`
    # and dies on the first assertion section 31 introduces.
    "startup_cache_check.py": 130,      # wave 1, +12 W10, +4 W10 follow-up, +12 prefix-derived filter
    "handle_check.py": 80,              # wave 2
    # 234 through wave 7; wave 8 raised it to 240 -- 234 - 3 + 9. A non-facet `value_prefix` no
    # longer refuses the whole per-source plan (it degrades the value lookup and leaves the search
    # planned), so three assertions changed sense and NINE were added: seven in section 16 and two
    # in section 19, including that a MALFORMED `value_prefix` refuses outright, which used to
    # raise AttributeError out of the planner. The wave-8 gate caught this comment saying 239 and
    # "three added" against the 240 pinned below -- pinning a total in two places is worthless if
    # the arithmetic beside it disagrees with the number.
    "request_build_check.py": 243,      # wave 3, +9 net in wave 8
    # 150 through wave 6; wave 7 raised it to 159 (this said 156, against arithmetic below
    # totalling +9 -- corrected in the wave-10 follow-up). Arithmetic, spelled out, in that file's own
    # comment at `expected`: -2 for two assertions that tested nothing (a constant, and a direct
    # call to `_strip_internal` -- a filter with zero call sites in the connector), +6 for one
    # assertion per PLAN_INTERNAL_KEYS member over a real result, +1 sabotage of the fresh-dict
    # construction that actually enforces it, +1 its un-sabotaged control, +3 for `ordered_by`
    # reaching the RESULT rather than only the plan. Wave 8 adds 19 in section 19b, one per
    # group-gate finding: value_prefix executing at all, the locator shape guard, the program
    # filter field off records, the similarity score's value, and no `.raw` in the distribution.
    # Then +4 more after its own gate: the value assertions became value-level rather than
    # type-level (proven by mutation), and both new locator guards gained sabotage entries.
    "response_assembly_check.py": 209,  # wave 4, +9 W7, +24 W8, +17 W10, +9 follow-up
    "document_read_check.py": 82,       # wave 5, +6 in wave 8 (no_passage_matched)
}

# The external connector's harnesses, pinned the same way but kept SEPARATE from `HARNESSES`.
# `HARNESSES` is cross-checked against `MAPPING`, which inventories the MUSE contract only, so an
# external harness listed there would fail "every pinned harness is named by at least one entry"
# -- a false alarm about a real gap, which is worse than no alarm.
#
# These three arrived from the external fork with NO pin anywhere and no entry in this file: 317
# assertions that could silently drop to zero and still read as a pass. Unlike the MUSE harnesses
# they FAIL FAST -- a failing assertion raises out of `main()` and the summary line never prints --
# so the runner below sees no total, scores -1, and fails. The number is pinned HERE ONLY, not also
# inside each script: this file has twice shipped a pinned total with wrong arithmetic beside it.
EXTERNAL_HARNESSES: dict[str, int] = {
    "external_contract_check.py": 54,    # shared boundaries: query compilation, disclosure, transport
    "external_adapter_check.py": 158,    # the two provider adapters against hand-built fixtures
    "external_behavior_check.py": 129,   # literature_search / literature_get orchestration
}
# MEASURED with `python -m trace --count`, not counted by eye. An earlier revision of this line said
# SEVEN, which was the number of `check(True, ...)` SOURCE SITES against a denominator of executed
# assertions -- two of the sites sit inside loops, so the real figure is 4.7x higher. That is the
# very failure this file exists to prevent, printed to the operator on every run.
#
#   external_contract_check.py   line  57 x28 (inside `rejects()`)                         = 28
#   external_adapter_check.py    lines 957, 1378, 2217, x1 each                            =  3
#   external_behavior_check.py   line 437 x4 (once per invalid-argument case)               =  4
EXTERNAL_CAN_ONLY_PASS = 35
# And "can only pass" is the wrong description for all 35. Those are the SUCCESS BRANCH of a
# `try / except SpecificError: check(True) / else: check(False)` pair -- the discriminating logic is
# which branch runs, and the `else` fires a real failure, so they are sound.
EXTERNAL_UNDISCRIMINATED = 0

# --------------------------------------------------------------------------
# THE MAPPING. 79 entries -- the number the docstring above spends four paragraphs establishing.
# Said 77 for three waves, which is the miscount that hid `07`.7a. Every one has a verdict.
# --------------------------------------------------------------------------
# `by` names the harness that covers it. `not_offline_testable` states what would establish it
# instead -- a live probe, or a condition the API will not produce on request.
W1, W2, W3, W4, W5 = (
    "startup_cache_check.py", "handle_check.py", "request_build_check.py",
    "response_assembly_check.py", "document_read_check.py")
LIVE = "not_offline_testable"
# Covered by NOTHING. Distinct from `LIVE`: an untestable-offline assertion has a
# reason, an UNCOVERED one is a gap. Conflating them is how the only gap in the
# inventory was reported as "0 unimplemented".
UNCOVERED = "uncovered"

MAPPING: list[tuple[str, str, str]] = [
    # -- 01-muse-search.md §7 (13) --------------------------------------------------
    ("01.1", UNCOVERED, "request keys validated by NAME against the OpenAPI schema, never by enum value -- the "
                  "dataSources enum is a malformed string excluding all five sources. NO ASSERTION EXISTS"
                  ": W3 validates the connector's own Python signature, not MUSE request keys"),
    ("01.2", W3, "program filter: real value >0, impossible ==0, bare ==0"),
    ("01.3", LIVE, "basic_knn with cards implies totalHitsKnn > 0. WEAKER offline surrogate exists; probe_"
                  "w4_live_result.py asserts the derived count is non-zero but never totalHitsKnn from th"
                  "e body"),
    ("01.4", W3, "the conditional _knn/_hybrid variants are never sent -- the prohibition IS asserted of"
                  "fline, so this is not untestable. basic_hybrid's 2x tolerance is moot because hybrid i"
                  "s never sent"),
    ("01.5", LIVE, "a KNN-incapable source is refused rather than reported as effectively keyword -- asser"
                  "ted at probe_w4_live_result.py section 5, which is the only place the three downgrade "
                  "sources are exercised"),
    ("01.6", W3, "every fieldsToExtract name exists, or is dropped and reported"),
    ("01.7", W4, "no locator containing ?...? is emitted"),
    ("01.8", W4, "searchStrategy values are mapped; an unknown one does not pass silently"),
    ("01.9", UNCOVERED, "~NOT~<impossible> equals the unfiltered population, proving the prefix is interpreted."
                  " NO ASSERTION EXISTS, and the connector never emits ~NOT~ at all, so this is a live co"
                  "unt identity with nothing standing in for it"),
    ("01.10", UNCOVERED, "a ?-bearing query returns 0 AND a hint whose internalLink.query recovers it. NO ASSERT"
                  "ION EXISTS: `internalLink` appears nowhere and `hints` only as empty fixture data. W4 "
                  "covers relaxation strategies, which is a different assertion"),
    ("01.11", W4, "no cross-kind ranking -- relevance and chunkScore are never one order"),
    ("01.12", W4, "no cross-query ranking -- scores are labelled query-local"),
    ("01.13", W4, "per-source status is present on every result"),
    # -- 02-muse-search-conditional.md §17 (21) ------------------------------------
    ("02.1", LIVE, "MUST _all IMPOSSIBLE == 0 PER SOURCE. Partially covered: probe_w3_live_plans.py uses t"
                  "he impossible token as a synonym on meds only, so the per-source form is not asserted"),
    ("02.2", W3, "empty or whitespace condition text is refused before sending"),
    ("02.3", W3, "a request with zero effective conditions is refused before sending"),
    ("02.4", W3, "a SHOULD-only request is refused; SHOULD is never emitted at all"),
    ("02.5", LIVE, "text.synonyms is honoured -- asserted at probe_w3_live_plans.py section 3, impossible "
                  "primary plus real synonym equals the real term's count"),
    ("02.6", LIVE, "`fields` is honoured -- the doc's assertion is a live count comparison (meds title 13 "
                  "against _all 135). W3 asserts the emitted field list instead, which section 17's own p"
                  "reamble forbids: assert an effect, never that a request was accepted"),
    ("02.7", UNCOVERED, "multi-field is a union -- ['title','text'] at least each alone and at most their sum. "
                  "NO ASSERTION EXISTS in any harness"),
    ("02.8", LIVE, "MUST_NOT negates the whole disjunction -- asserted at probe_w3_live_plans.py section 4"
                  " as population minus (A OR B), with the impossible control"),
    ("02.9", LIVE, "reversed multi-word text returns 0 while forward returns non-zero, proving phrase "
                   "semantics are still in force. Needs two live counts on the same population; no "
                   "offline surrogate exists because the API does the matching"),
    ("02.10", W3, "every condition field exists in that source's mapping"),
    ("02.11", W3, "no condition is emitted without text or without type"),
    ("02.12", W3, "* is stripped from condition text; ? need not be"),
    ("02.13", W3, "conditional_knn / conditional_hybrid are never sent"),
    ("02.14", W3, "a DATE facet term is never used as an exact condition value"),
    ("02.15", W4, "the response's synonyms map is never reported as applied"),
    ("02.16", W3, "text_size is in the fieldsToExtract floor"),
    ("02.17", W4, "no locator containing ?...? is ever emitted"),
    ("02.18", W3, "excludeDataSources is never sent"),
    ("02.19", W3, "sort: custom is never sent"),
    ("02.20", W4, "a zero result is never returned without an access-map lookup"),
    ("02.21", UNCOVERED, "any retrieval cap is a stated choice, reported, never silent. NO ASSERTION EXISTS -- a"
                  "nd W3 asserts limit==10000 for depth=complete, an unlabelled unreported cap, which is "
                  "the thing this prohibits"),
    # -- 03-muse-fetch-document.md §9 (15) ----------------------------------------
    ("03.1", W5, "requested-vs-returned diff: 2 valid + 1 bogus names the third"),
    ("03.2", W5, "never zip by position -- a reordered response still matches by id"),
    ("03.3", W5, "duplicates do not read as missing"),
    ("03.4", W5, "a bogus-only 404 is distinguished from a partially-fulfilled 200"),
    ("03.5", W5, "the 403 batch-killer is refused client-side and never sent"),
    ("03.6", W5, "len(text) == text_size per document"),
    ("03.7", W5, "a DENIED source's omission is attributed, not inferred"),
    ("03.8", W1, "user-access-check is projected to `access` only -- no caller PII"),
    ("03.9", W5, "no non-breaking space survives normalisation"),
    ("03.10", W5, "tab-run detection fires on tabular material"),
    ("03.11", W5, "sensitivity is carried when present and never defaulted when absent"),
    ("03.12", W5, "document_id is never emitted as an identifier"),
    ("03.13", W5, "handles only -- a raw card.id is refused"),
    ("03.14", W5, "a missing slice is never filled with the nearest-looking field"),
    ("03.15", W3, "text is never requested through a search fieldsToExtract"),
    # -- 06-muse-facets.md §12 (14) ------------------------------------------------
    ("06.1", W3, "facetFields is never sent -- it does not exist"),
    ("06.2", W3, "the poison rule is checked before sending"),
    ("06.3", W4, "a poisoned source's superset path reports both populations"),
    ("06.4", W3, "counts is sent for every field returned -- in W3, not W4. W4 covers only the + bucket "
                  "half"),
    ("06.5", LIVE, "the facet-to-condition loop holds -- a returned non-date term used as an exact "
                   "condition returns the facet's own count, 25 of 25 measured. Needs two live calls "
                   "whose counts are compared; a stub would compare our own fixtures"),
    ("06.6", W3, "date-typed terms are never offered as condition values"),
    ("06.7", W4, "sigma counts is never presented as a population -- a fair surrogate is asserted; the d"
                  "oc's author.raw paired control (65 against a population of 5) is not"),
    ("06.8", W3, "one request per source -- no multi-source facet request"),
    ("06.9", W4, "user (an ISID) never leaves the connector"),
    ("06.10", W4, "statistics.matches is relabelled, never emitted as this result's count. PARTIAL: the n"
                  "ever-emitted half is asserted, the relabelling is not"),
    ("06.11", W4, "an empty facet is reported with its population count first"),
    ("06.12", W3, "the inert six are never sent"),
    ("06.13", UNCOVERED, "valueLabels are used when configured and never invented when absent. Implemented at se"
                  "rver.py:3646, asserted NOWHERE -- one fixture literal, zero checks"),
    ("06.14", W3, "a bogus source is a request error, not an empty distribution"),
    # -- 07-startup-cache-endpoints.md §7 (14) ------------------------------------
    ("07.1", W1, "the DATE guard reads facetType from /v3/ingest, not from a join"),
    ("07.2", W3, "DATE-typed terms are never offered as condition values"),
    ("07.3", UNCOVERED, "a facet with no definition is reported as untyped, not defaulted -- 21 such on meds. N"
                  "O ASSERTION EXISTS for the untyped path"),
    ("07.4", W1, "fullDocumentTextRetrievalEnabled absent or null never reads as True"),
    ("07.5", W4, "a locator containing ?...? is never emitted; a null link reports it"),
    ("07.6", W1, "presence in /v2/configuration is never treated as searchability"),
    ("07.7", W1, "/v3/ingest returning 200 for a DENIED source is not read as access"),
    # THE ONE CONTRACT COVERED BY NOTHING. `grep -n "v1/acl|EP_ACL"` across server.py and all six
    # harnesses returns a single comment. The doc asks for an assertion that the code path does
    # not exist; there is none. Named here rather than dropped -- dropping it is precisely what
    # the earlier miscount did, and it was the only entry that would have printed as uncovered.
    ("07.7a", UNCOVERED, "/v1/acl is never consulted for searchability -- it reports ACL-group "
                         "membership, and returns accessible:false for meds, which serves 1,014 "
                         "records. NO ASSERTION EXISTS; only a comment at server.py:549"),
    ("07.7b", W1, "the cache holds no key from the user-access-check response other than "
                  "`access` -- asserted with a non-vacuity control that first proves the leak "
                  "vector is real"),
    ("07.8", W1, "dynamic: strict is asserted per source. PARTIAL: only the negative is asserted (mappin"
                  "g_complete False when non-strict); the per-source True on the happy path is not"),
    ("07.9", UNCOVERED, "an epoch-zero data-updates date renders 'never indexed', never a formatted date. The f"
                  "ixture puts the sentinel on a source OUTSIDE IN_SCOPE, so warm_cache never renders it "
                  "and no assertion fires -- a branch W1's own docstring says must be asserted because it"
                  " cannot be provoked live"),
    ("07.10", W1, "a source missing from data-updates yields 'freshness unknown'"),
    ("07.11", W1, "a naive timestamp is never compared to a document date without a stated zone. PARTIAL:"
                  " the `tz: naive` label is asserted, not the comparison prohibition"),
    ("07.12", LIVE, "a bad project fails loudly on both config calls -- 404 on /v2/configuration and 403 "
                   "on /v3/ingest. Needs a deliberately wrong project against the live API, which is the "
                   "one thing a stub cannot establish about an upstream's error behaviour"),
    # Already implemented as `config_fingerprint` and already asserted in W1 before this wave.
    # An earlier revision of this file called it the one unimplemented assertion and added a
    # byte-identical duplicate 31 lines away in warm_cache; that has been reverted.
    ("07.13", W1, "the configuration body fingerprint is recorded -- the only drift signal that "
                  "endpoint offers, since it has no etag and no last-modified"),
    ("07.14", W1, "KNN capability from the two independent sources agrees"),
]

# Wave 2's handle codec is asserted by 80 checks that map to no LETTERED design assertion --
# the handle contract lives in `00-surfaced-tools.md` §4, which carries no numbered list. Recorded
# rather than left as a harness nobody references, which is the same invisible state from the
# other side.
HARNESS_NOT_MAPPED = {"handle_check.py": (
    "wave 2's handle codec. Its contract is `00-surfaced-tools.md` §4, which is prose with no "
    "numbered assertions, so it maps to no lettered entry")}

# Every value `_plan_facets` can emit.
ALL_FACET_SCOPES = {"not_requested", "unknown", "unavailable", "filtered", "narrowed",
                    "query_scoped", "superset"}

# `server.py` has TWO `unavailable` sites. The poisoned-with-conditions one is reached above; the
# poisoned-no-conditions one, where the program literal sanitises to empty, is not -- and
# `superset_literal_sanitised` with it. Stated rather than implied covered by producing the value.
KNOWN_UNREACHED_BRANCHES = {"sanitised_program_literal"}

CONTRACT_SECTIONS = {
    "01": ("01-muse-search.md", "## 7. Contract-check assertions"),
    "02": ("02-muse-search-conditional.md", "## 17. Contract assertions"),
    "03": ("03-muse-fetch-document.md", "## 9. Contract assertions"),
    "06": ("06-muse-facets.md", "## 12. Contract assertions"),
    "07": ("07-startup-cache-endpoints.md", "## 7. Contract assertions"),
}
DESIGN_DIR = Path("/Users/suray2/Ray_workspace/assistant_agent_space/gas_town/"
                  "piper_station_studio/projects/ddt-program-companion/work/design/"
                  "model-facing-tools")


def parse_inventory() -> tuple[dict[str, int], set[str]]:
    """Count the numbered assertions in each design section, from the docs themselves.

    `[0-9]+[a-z]?` and not `[0-9]+`: `07` §7 numbers its items 1..14 plus **7a** and **7b**, and a
    digits-only pattern is exactly what made an earlier revision claim 77, miss two assertions, and
    drop the only uncovered contract in the inventory.
    """
    counts: dict[str, int] = {}
    ids: set[str] = set()
    for doc, (fname, heading) in CONTRACT_SECTIONS.items():
        text = (DESIGN_DIR / fname).read_text()
        if heading not in text:
            counts[doc] = -1
            continue
        body = text.split(heading, 1)[1]
        body = body.split("\n## ", 1)[0]
        found = re.findall(r"^([0-9]+[a-z]?)\.\s", body, re.MULTILINE)
        counts[doc] = len(found)
        ids |= {f"{doc}.{n}" for n in found}
    return counts, ids


EXPECTED_INVENTORY = {"01": 13, "02": 21, "03": 15, "06": 14, "07": 14}


def warm(m) -> dict[str, Any]:
    m._CACHE = None
    m._calls_used = 0
    m._started = m.time.monotonic()
    install_stubs(m)
    return asyncio.run(m.warm_cache("MK-6070"))


def main() -> int:  # noqa: C901
    m = load_server()
    # The REAL `_request`, captured before any stub replaces it. Section 4's deadline checks need
    # it: every stub replaces `_request` wholesale, so the guard inside it is unreachable through
    # one -- which is why the deadline defect below shipped. Restored in a `finally` there.
    real_request = m._request

    # ----------------------------------------------------------------------------
    print("1. the inventory is PARSED from the design docs, not from a literal here")
    # This is the fix for how the earlier revision got its central claim wrong. Counting a
    # hand-written literal against a second hand-written literal in the same file catches an
    # accidental edit and nothing else -- it is a pin, not a count. Parsing means a doc that
    # gains an assertion fails this file until the assertion is mapped.
    doc_counts, doc_ids = parse_inventory()
    check("all five contract sections were found and parsed",
          sorted(doc_counts) == ["01", "02", "03", "06", "07"], str(doc_counts))
    mapped_ids = {e[0] for e in MAPPING}
    check(f"the docs declare {sum(doc_counts.values())} assertions",
          sum(doc_counts.values()) == 79, str(doc_counts))
    check("every assertion in the docs is mapped",
          doc_ids <= mapped_ids, str(sorted(doc_ids - mapped_ids)))
    check("and the mapping invents none that the docs do not declare",
          mapped_ids <= doc_ids, str(sorted(mapped_ids - doc_ids)))
    check("no assertion id appears twice", len(mapped_ids) == len(MAPPING),
          f"{len(mapped_ids)} of {len(MAPPING)}")
    # Sub-lettered ids are the specific thing the earlier contiguity check could not see.
    check("sub-lettered assertions are parsed, not skipped",
          {"07.7a", "07.7b"} <= doc_ids, str(sorted(x for x in doc_ids if x[-1].isalpha())))

    print("\n2. every entry has a verdict, and an UNCOVERED one is named as a gap")
    bad_target = [e[0] for e in MAPPING
                  if e[1] not in HARNESSES and e[1] not in (LIVE, UNCOVERED)]
    check("every named harness resolves to a pinned one", not bad_target, str(bad_target))
    # A harness pinned but named by no entry is the same invisible state this file exists to
    # prevent, from the other direction.
    unnamed = sorted(set(HARNESSES) - {e[1] for e in MAPPING})
    check("every pinned harness is named by at least one entry",
          unnamed == ["handle_check.py"], str(unnamed))
    check("  ... and handle_check.py's exemption is recorded, not silent",
          "handle_check.py" in HARNESS_NOT_MAPPED, str(sorted(HARNESS_NOT_MAPPED)))
    uncovered = [e for e in MAPPING if e[1] == UNCOVERED]
    live_only = [e for e in MAPPING if e[1] == LIVE]
    # SHAPE, not content -- and labelled as such. Asserting on a prose substring is only as good
    # as the wording, and the wording is what a reviewer reads anyway; the machine's job here is
    # to ensure no entry is EMPTY, and that an UNCOVERED one never also claims a harness.
    check(f"{len(uncovered)} assertions are UNCOVERED, each with a substantive reason",
          all(len(e[2]) > 60 for e in uncovered), str([e[0] for e in uncovered]))
    check("  ... and no UNCOVERED entry also names a harness",
          all(e[1] == UNCOVERED for e in uncovered))
    check(f"{len(live_only)} are not offline-testable, each with a substantive reason",
          all(len(e[2]) > 60 for e in live_only), str([e[0] for e in live_only]))
    check("  ... and UNCOVERED is kept distinct from not-offline-testable",
          not (set(e[0] for e in uncovered) & set(e[0] for e in live_only)))
    check("the one contract covered by nothing at all is named",
          any(e[0] == "07.7a" and e[1] == UNCOVERED for e in MAPPING))

    print("\n3. every named harness runs at its pinned total")
    for name, pinned in HARNESSES.items():
        run_pinned_harness(name, pinned)

    print("\n3b. the external connector's harnesses run at their pinned totals")
    for name, pinned in EXTERNAL_HARNESSES.items():
        run_pinned_harness(name, pinned)

    # The gap wave 1 actually found: harness FILES sitting in `tools/` with no pin anywhere, so 317
    # assertions nobody was counting. Nothing detected that. A fourth `external_*_check.py` copied
    # over from the fork would land in exactly the same invisible state.
    on_disk = sorted(p.name for p in HERE.glob("external_*_check.py"))
    check("every external_*_check.py on disk is pinned, and every pin exists on disk",
          on_disk == sorted(EXTERNAL_HARNESSES),
          f"disk {on_disk} vs pinned {sorted(EXTERNAL_HARNESSES)}")

    # Wave 1's shebang fix, guarded. Without this, reverting one line restores an unpinned-interpreter
    # run that still reports its full total -- a fix with no assertion is undefended, and five defects
    # in one wave of this project proved it.
    wrong = []
    for name in list(HARNESSES) + list(EXTERNAL_HARNESSES):
        path = HERE / name
        first = path.read_text().splitlines()[0] if path.is_file() else "<missing>"
        if first != REQUIRED_SHEBANG or not os.access(path, os.X_OK):
            wrong.append(f"{name}: {first[:40]!r} exec={os.access(path, os.X_OK)}")
    check("every pinned harness declares its deps via uv and is executable", not wrong, str(wrong))

    # ----------------------------------------------------------------------------
    print("\n4. GAP: the BudgetExhausted handlers, which no harness reached")
    # Honest about what this does and does not close. It exercises the HANDLERS in
    # `run_population` and `run_read`. It does NOT exercise the raiser: the stub raises directly
    # rather than incrementing `_calls_used`, so `_request`'s own `>= CALL_BUDGET` boundary and
    # its `deadline_exceeded` branch remain unexecuted. Two of seven handler sites are touched.
    cache = warm(m)
    check("the fixture warms", cache["ok"], str(cache["degraded"]))
    base = m._request

    def counting_stub(budget_after: int):
        state = {"n": 0}

        async def stub(method, endpoint, *, payload=None, params=None):
            # `data-updates` is NOT counted. Wave 8 re-reads index freshness once per tool call,
            # before planning, so with a positional budget this stub spent its whole allowance on
            # that call and raised on the SEARCH -- turning a test about exhaustion during the
            # FACET call into one about exhaustion during the search, whereupon the assertion
            # below correctly reported the wave-4 regression it exists to catch. Excluding the
            # endpoint pins the scenario to what its label says, whatever else calls first.
            if endpoint == m.EP_UPDATES:
                return await base(method, endpoint, payload=payload, params=params)
            state["n"] += 1
            if state["n"] > budget_after:
                raise m.BudgetExhausted("call_budget_exhausted")
            if endpoint in (m.EP_QUERY, m.EP_CONDITIONAL):
                return {"_ok": True, "_status": {"executed": True}, "_body": {
                    "cards": [{"id": "c1", "title": "t", "body": {"title": "t"},
                               "textSize": 10, "relevance": 1.0}],
                    "datasourceErrors": [], "hints": [], "query": "", "queryId": "q",
                    "searchStrategy": ["FULLTEXT_ENHANCERS"], "synonyms": {},
                    "totalHits": 19, "totalHitsKnn": 0}}
            if endpoint == m.EP_READ:
                return {"_ok": True, "_status": {"executed": True}, "_body": [
                    {"id": "card-meds-1", "datasource": "meds", "title": "t",
                     "text": "prose", "text_size": 5, "acl_group": ["g"]}]}
            return await base(method, endpoint, payload=payload, params=params)
        return stub

    m._request = counting_stub(1)
    try:
        r = asyncio.run(m.run_population(cache, sources=["med_comms"], about="MK-6070",
                                         distribution=True))
    finally:
        m._request = base
    per_source = g(r, "per_source", 0) or {}
    # UNCONDITIONAL, and `not_searched` is a FAILURE. It is the wave-4 regression itself --
    # unwinding past a completed search and reporting the source as never searched. An earlier
    # revision accepted either outcome and then supplied a passing sub-assertion for whichever
    # branch ran, so the regression printed all-PASS at the pinned total.
    check("exhaustion during the facet call keeps the completed search",
          per_source.get("status") == "ok", str(per_source.get("status")))
    check("  ... and the distribution names the exhaustion",
          g(per_source, "distribution", "failed") == "call_budget_exhausted",
          str(g(per_source, "distribution", "failed")))
    h, _ = m.encode_document_handle("meds", "card-meds-1")
    m._request = counting_stub(0)
    try:
        rr = asyncio.run(m.run_read(cache, [h], "check budget handling"))
        raised = False
    except m.BudgetExhausted:
        rr, raised = {}, True
    finally:
        m._request = base
    check("muse_read does not raise BudgetExhausted to the MCP layer", not raised,
          "raised" if raised else "returned a result")
    check("  ... and reports it as unknown coverage, not absence",
          rr.get("is_absence") is False and rr.get("read_count") == 0, str(rr.get("status")))

    # ----------------------------------------------------------------------------
    print("\n4b. the deadline branch itself — the one the note above said was unreached")
    # It was unreached, and a defect lived there. `DEADLINE_S` bounded the PROCESS, because
    # `_started` was set at import and never reassigned, so a server alive longer than
    # `DEADLINE_S` refused every call in `_request` BEFORE the network. Found in use: six
    # consecutive calls returned `not_searched` / `deadline_exceeded` with no outbound HTTP.
    #
    # No harness could see it. Every fixture -- `warm()` in this file included -- sets
    # `m._started` by hand, which is the reset the product was missing.
    #
    # Tested against the REAL `_request`, not a stub. Safe offline in both directions: the
    # deadline check raises before any network use, and once past it `_session_cookie` is
    # stubbed to fail
    # so the call returns `authentication_failure` instead of reaching MUSE. That pair is also
    # the diagnostic the misreading of this bug turned on -- `deadline_exceeded` means the cookie
    # was never consulted.
    real_session_cookie = m._session_cookie
    try:
        m._request = real_request
        m._session_cookie = lambda: (
            None,
            "stubbed: this check must not reach the network",
        )
        m._started = m.time.monotonic() - (m.DEADLINE_S + 60)
        try:
            asyncio.run(m._request("GET", m.EP_UPDATES))
            kind = None
        except m.BudgetExhausted as exc:
            kind = exc.kind
        check("a process older than DEADLINE_S is refused by the real _request",
              kind == "deadline_exceeded", str(kind))

        m._begin_call()
        try:
            env = asyncio.run(m._request("GET", m.EP_UPDATES))
            kind2, reason = None, g(env, "_status", "reason")
        except m.BudgetExhausted as exc:
            kind2, reason = exc.kind, None
        # Past the deadline and stopped at the cookie: the reset moved the guard, and it moved
        # THIS guard rather than some other refusal appearing.
        check("  ... and _begin_call clears it, reaching the cookie check instead",
              kind2 is None and reason == "authentication_failure", f"{kind2} / {reason}")

        # The entry points. `_started` is driven ancient before each, so a body that does not
        # reset leaves it ancient -- which is the shipped defect, and what the sabotage below
        # confirms this can still detect.
        #
        # `counting_stub` rather than `base`: wave 1's stub raises on a search endpoint by design,
        # and a raise would abort this section rather than fail one named assertion. The budget is
        # set far above anything these two calls spend, so exhaustion cannot confound the result.
        m._request = counting_stub(10_000)
        for label, drive in (
            ("run_population", lambda: asyncio.run(
                m.run_population(cache, sources=["med_comms"], about="MK-6070"))),
            ("run_read", lambda: asyncio.run(
                m.run_read(cache, [m.encode_document_handle("meds", "card-meds-1")[0]], "p"))),
        ):
            m._started = m.time.monotonic() - (m.DEADLINE_S + 60)
            drive()
            elapsed = m.time.monotonic() - m._started
            check(f"{label} begins a fresh deadline", elapsed < m.DEADLINE_S,
                  f"{round(elapsed, 1)}s elapsed against a {m.DEADLINE_S}s budget")

        # SABOTAGE the reset. Without this pair the two assertions above could be passing because
        # something else happens to touch `_started` -- and an assertion that cannot be made to
        # fail is decoration. Restored in the outer `finally`.
        real_begin = m._begin_call
        try:
            m._begin_call = lambda: None
            m._request = counting_stub(10_000)
            m._started = m.time.monotonic() - (m.DEADLINE_S + 60)
            asyncio.run(m.run_population(cache, sources=["med_comms"], about="MK-6070"))
            still_stale = (m.time.monotonic() - m._started) > m.DEADLINE_S
        finally:
            m._begin_call = real_begin
        check("  ... and with the reset removed the deadline stays expired, so this can fail",
              still_stale, "the reset is not what keeps the deadline fresh" if not still_stale
              else "")

        # A source-order check, and no more than that: the MCP handlers cannot be driven offline
        # because the lazy `warm_cache()` they guard is the thing under test. Stated as what it
        # is rather than dressed as behaviour.
        import inspect
        order_ok = []
        for fn in (m.muse_population, m.muse_read):
            src = inspect.getsource(getattr(fn, "fn", fn))
            order_ok.append("_begin_call()" in src
                            and src.index("_begin_call()") < src.index("warm_cache()"))
        check("both MCP handlers call _begin_call before their lazy warm (source order only)",
              all(order_ok), str(order_ok))
    finally:
        m._request = base
        m._session_cookie = real_session_cookie
        m._begin_call()

    print("\n5. GAP: the four facet_scope states no test produced")
    install_stubs(m)
    seen_scopes: set[str] = set()

    def scope_of(**kw):
        plan = m.plan_population(cache, **kw)
        if plan.get("refused"):
            return plan.get("cause")
        sc = plan["requests"][0].get("facet_scope")
        if sc:
            seen_scopes.add(sc)
        return sc

    check("`not_requested` when distribution=False",
          scope_of(sources=["med_comms"], about="x", distribution=False) == "not_requested")
    check("`filtered` on an unpoisoned source with no conditions",
          scope_of(sources=["med_comms"], distribution=True) == "filtered")
    check("`narrowed` on an unpoisoned source with conditions",
          scope_of(sources=["med_comms"], about="x", distribution=True) == "narrowed")
    check("`superset` on the poisoned source with no conditions",
          scope_of(sources=["meds"], distribution=True) == "superset")
    check("`unavailable` on the poisoned source WITH conditions",
          scope_of(sources=["meds"], about="x", distribution=True) == "unavailable")
    check("`query_scoped` on the semantic engine, unpoisoned source",
          scope_of(sources=["med_comms"], semantic=True, question="how is stability shown",
                   distribution=True) == "query_scoped")
    unread = copy.deepcopy(cache)
    unread["sources"]["med_comms"]["facetable_program_filter"] = None
    p_unknown = m.plan_population(unread, sources=["med_comms"], about="x", distribution=True)
    if g(p_unknown, "requests", 0, "facet_scope"):
        seen_scopes.add(g(p_unknown, "requests", 0, "facet_scope"))
    check("`unknown` when the poison predicate was never read",
          g(p_unknown, "requests", 0, "facet_scope") == "unknown",
          str(g(p_unknown, "requests", 0, "facet_scope")))
    check("  ... and no facet request is built on an unknown predicate",
          g(p_unknown, "requests", 0, "facets") is None)
    # `seen_scopes` was previously written and never read -- the completeness assertion the
    # section needed was set up and abandoned.
    check("every facet_scope value the module can emit was produced",
          seen_scopes == ALL_FACET_SCOPES, str(sorted(ALL_FACET_SCOPES - seen_scopes)))
    check("and the one `unavailable` BRANCH still unreached is recorded, not implied covered",
          "sanitised_program_literal" in KNOWN_UNREACHED_BRANCHES)

    print("\n6. the one parameter assertion W3 does not already make")
    # The other five parameters -- order, depth, offset, where_not, population -- ARE exercised by
    # request_build_check.py, several of them more strongly than a duplicate here would. An earlier
    # revision claimed "nothing exercised them", which was false, and added six duplicate checks.
    p = m.plan_population(cache, sources=["med_comms"], about="x",
                          fields=["primary_mkv_number1", "not_a_field"])
    extract = g(p, "requests", 0, "search", m.F_FIELDS) or []
    check("a valid extra field reaches fieldsToExtract even when NOT in the default floor",
          "primary_mkv_number1" in extract, str(extract))
    check("  ... and an unknown extra is dropped and reported, not sent",
          "not_a_field" not in extract
          and any("not_a_field" in n for n in (g(p, "requests", 0, "notes") or [])),
          str(g(p, "requests", 0, "notes"))[:70])

    print("\n7. the drift fingerprint changes when the body does")
    # Asserting a 16-char string that is stable across two identical warms passes for a constant.
    # The property that makes it a drift signal is that a CHANGED body hashes differently.
    first = cache.get("config_fingerprint")
    check("a fingerprint is recorded", isinstance(first, str) and len(first) == 16, str(first))
    import startup_cache_check as scc
    scc.CONFIG_BODY["dataSources"][0]["title"] = "MEDS (renamed)"
    try:
        moved = warm(m)
    finally:
        scc.CONFIG_BODY["dataSources"][0]["title"] = "MEDS"
    check("  ... and a changed configuration body yields a different one",
          moved.get("config_fingerprint") != first,
          f"{first} -> {moved.get('config_fingerprint')}")
    restored = warm(m)
    check("  ... and restoring the body restores the fingerprint",
          restored.get("config_fingerprint") == first, str(restored.get("config_fingerprint")))

    # 39 through wave 9. Section 4b adds SIX: the real `_request` refusing an old process, and
    # `_begin_call` clearing it into the cookie check instead; one per tool body beginning a fresh
    # deadline; the sabotage that proves those two can fail; and the handler source-order check.
    # Written as 46 first, which this pin caught on the next run -- the arithmetic beside a pinned
    # total is worth exactly as much as the pin.
    # 45 through the tt6 path fix. Section 3b adds FIVE: one per external harness (3), the
    # disk-versus-pin inventory check, and the shebang/exec-bit check. 45 + 3 + 1 + 1 = 50.
    #
    # A LITERAL, deliberately. This was briefly written `45 + len(EXTERNAL_HARNESSES)`, which is
    # tautological and disarmed the one pin in this file whose job is to not be: delete an entry from
    # the dict and `CHECKS` and `expected` both fall, the guard never fires, and the suite reports
    # exit 0 with 317 assertions silently gone -- the exact state section 3b was added to prevent.
    # Derivation is legitimate here only from EXTERNAL ground truth, which is why `parse_inventory()`
    # beats a literal for the docs; an in-file dict on the other side of the same equation is not
    # that. This pin's own message says "update deliberately", and deriving means never deliberately.
    expected = 50
    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} assertions passed")
    if CHECKS != expected:
        print(f"HARNESS: expected {expected} assertions, ran {CHECKS}. Update deliberately, "
              "never to match a shrunken run.")
        return 1
    if FAILURES:
        print("FAILED: " + ", ".join(FAILURES))
        return 1
    muse_total = sum(HARNESSES.values()) + CHECKS
    ext_total = sum(EXTERNAL_HARNESSES.values())
    print(f"\nsuite total: {muse_total + ext_total} offline assertions across "
          f"{len(HARNESSES) + len(EXTERNAL_HARNESSES) + 1} harnesses "
          f"-- {muse_total} MUSE + this file, {ext_total} external")
    print(f"external caveat: {EXTERNAL_CAN_ONLY_PASS} of the {ext_total} executed assertions are "
          f"`check(True, ...)`; {EXTERNAL_CAN_ONLY_PASS - EXTERNAL_UNDISCRIMINATED} are the success "
          f"branch of an exception-discriminated pair and DO fail via their `else`. "
          f"{EXTERNAL_UNDISCRIMINATED} are genuinely undiscriminating.")
    print("external caveat (accepted debt, D17): no mutant targets the external package, so "
          "0-of-13 says nothing about it. The pinned external harnesses remain the release gate.")
    print(f"contract inventory: {len(MAPPING)} assertions -- "
          f"{len([e for e in MAPPING if e[1] in HARNESSES])} covered offline, "
          f"{len(live_only)} live or not offline-testable, "
          f"{len(uncovered)} UNCOVERED (07.7a has no assertion at all)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
