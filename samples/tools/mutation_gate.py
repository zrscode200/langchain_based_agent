#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp==1.28.1","httpx>=0.27","pydantic>=2","pyyaml>=6.0","truststore>=0.9",
#   "typing-extensions>=4.12",
# ]
# ///
"""The release gate that cannot agree with itself.  (group BUILD waves 9 and 9b)

    ./tools/mutation_gate.py            # the gate, ~5 min
    ./tools/mutation_gate.py --part b   # fault injection alone, ~2 s, EXITS NON-ZERO BY DESIGN

Waves 1-8 shipped 729 offline assertions across six harnesses, all green at pinned totals, and an
independent review then found eight defects -- two of them making the connector state something
false. It also demonstrated why ten review gates had not converged: **a green total proves the
harnesses ran, not that any invariant holds.**

The demonstration, reproduced as `REV-A/B/C`: a copy of `server.py` with per-call freshness
refresh disabled while still claiming success, all program-filter literal evidence erased, and the
silently-downgraded semantic count forced to 999999 passes the whole suite at every pinned total,
exit 0.

Design prose, implementation, fixtures and assertions in this codebase are dependent restatements
of one interpretation, so they agree with each other. A fix adds an assertion written from the fix.
That loop introduced roughly one new defect per round across four rounds.

PART A -- MUTATION. Mutate the REAL shipped `server.py` as source text, in a throwaway copy of the
tree, and let the WHOLE existing suite judge it. The verdict is the suite's exit status, not an
assertion written here. This differs from `response_assembly_check.py`'s `mutate()` in the way that
matters: that helper swaps a module ATTRIBUTE for a substitute the test author wrote, so it cannot
reach a line inside a function -- and all three of the review's mutations are lines inside
functions.

  survived     the suite did not detect it, AND the mutation demonstrably changed behaviour
  killed       the suite detected it -- a non-zero exit WITH at least one failing assertion
  inert        the mutation changed no observable behaviour, so its survival measures NOTHING
  not-applied  the anchor did not match exactly once
  error        the run failed for a reason that is not a verdict (timeout, crash, copy failure)

Some mutants are INVERSE: they repair a known defect. An inverse mutant that survives proves the
suite is blind to the property in BOTH directions -- it cannot tell broken from working.

`inert` and `error` exist because wave 9 shipped without them and both bit immediately.
`REV-B`'s replacement was `{} or {<comprehension>}`, which returns the comprehension -- it erased
nothing, and its `survived` verdict was reported as evidence for a claim it did not support. And a
`killed` inferred from a bare non-zero exit cannot tell "the suite caught it" from "the mutated
file crashed on import", which is the opposite of coverage.

PART B -- FAULT INJECTION. Drive the real connector into states and TRANSITIONS the harnesses
never produce (warm the cache, move the index, then call) and assert the correct behaviour
directly. Every case was written from the review's statement of correct behaviour, before any fix
exists.

    CASES ASSERT PROPERTIES, NOT PROXIES. Five of wave 9's nine cases could be turned green
    without fixing anything -- by ruling a cause out without checking it, by never establishing it
    at all, by rewording a string, by flagging every source, or by adding a comment. Each has been
    rewritten. Where possible a case now asserts an IDENTITY between two live outputs rather than a
    shape or a substring, because an identity cannot be satisfied by cosmetics.

    EVERY CASE HAS A CONTROL. A case that can only fail is worth as little as one that can only
    pass. The `C*` controls below pass today and exist so a case's failure is attributable: they
    establish that the fixture reached the code, that the pre-fault state was sound, and that the
    scenario isolates the defect it names.

WHY BOTH VERDICT SETS ARE PINNED. Part B's cases FAIL today -- they are the review's findings, made
executable. A gate that merely goes red cannot distinguish known debt from a new regression. So
every mutant's verdict and every case's outcome is pinned, and this file exits non-zero when actual
and pinned disagree IN EITHER DIRECTION.

Fixing a finding has THREE obligations, not two:

  1. make the case pass;
  2. delete its entry from `KNOWN_FAIL`;
  3. re-anchor its mutant. Six of the seven real anchors sit on the exact lines a fix must
     change, so a correct fix turns its mutant into `not-applied`. That is the fix landing, not a
     bug -- but the message reads like a stale anchor, so update the anchor in the same commit.

    Do not make a case pass by editing the case. If an assertion here is wrong, say why in the
    entry and change the pin deliberately.

FOUR CONTROLS ON PART A, because this file has exactly the failure mode it exists to catch. A
runner that silently does nothing reports every mutant as `survived`; a broken tree copy reports
every mutant as `killed`; a stale anchor reports one as `survived`. Two of those three read as
FINDINGS rather than as breakage.

  null-control    no change at all              MUST survive     -> the copied tree is green
  runner-control  a break the suite does cover  MUST be killed   -> the suite really ran
  anchor-control  an anchor that cannot match   MUST be not-applied
  inert-control   a whitespace-only change      MUST be inert    -> the liveness probe works

If any control comes out wrong, the summary says to disregard every Part A verdict.

NOT wired into `contract_map_check.py`'s harness list: this runs that script once per mutant, so
nesting it would recurse. It is a separate gate, run before accepting a group.
"""

from __future__ import annotations

import copy
import inspect
import re
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import asyncio

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parent
sys.path.insert(0, str(HERE))
from response_assembly_check import (  # noqa: E402
    install_search_stubs, per, run, warm,
)
from startup_cache_check import UPDATES_BODY, load_server  # noqa: E402

FAILURES: list[str] = []
CHECKS = 0

# Positive attribution boundary for source mutants in `ics_muse/server.py`. A shortfall from any
# other harness can make the suite non-zero, but it cannot prove that a MUSE mutation was detected.
# Keep this literal fail-closed: a new MUSE harness must be added deliberately before its failures
# can count as mutation coverage.
MUSE_HARNESS_NAMES = frozenset({
    "startup_cache_check.py",
    "handle_check.py",
    "request_build_check.py",
    "response_assembly_check.py",
    "document_read_check.py",
})
_HARNESS_FAILURE = re.compile(
    r"^\s*FAIL\s+(?P<name>[A-Za-z0-9_]+\.py):\s+"
    r"(?P<passed>-?\d+)/(?P<ran>-?\d+)\s+at pinned\s+(?P<pinned>\d+)"
)


def check(label: str, ok: bool, detail: str = "") -> None:
    """One assertion. `detail` is printed either way, so it must read correctly on a PASS.

    Wave 9 wrote failure-shaped details -- a green R7 printed "configured labels absent" -- which
    tells a reader confirming a fix the opposite of what happened.
    """
    global CHECKS
    CHECKS += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{': ' + detail if detail else ''}")
    if not ok:
        FAILURES.append(label)


# ==========================================================================
# PART A -- source mutation, judged by the whole suite
# ==========================================================================
# The liveness probe. Written into each copied tree and run against the mutated module, its output
# compared byte-for-byte against the pristine baseline. A mutant whose fingerprint matches the
# baseline changed nothing observable, so its survival says nothing about the suite -- that is the
# `inert` verdict, and wave 9 shipped a mutant that needed it.
#
# The signals are chosen to cover what the `survived`-pinned mutants touch: the startup filter
# evidence, the zero-diagnosis rule-outs, the downgraded count, the configured-label map, and the
# freshness re-read report. A `killed` mutant needs no probe -- the kill IS its liveness.
_LIVENESS_PROBE = '''#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp==1.28.1","httpx>=0.27","pydantic>=2","pyyaml>=6.0","truststore>=0.9",
#   "typing-extensions>=4.12",
# ]
# ///
import asyncio, copy, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from response_assembly_check import install_search_stubs, per, run, warm
from startup_cache_check import UPDATES_BODY, load_server

OUT = {"error": None}
try:
    m = load_server()
    cache = warm(m)
    OUT["filter_field_terms"] = {k: (v.get("filter_field_terms") or None)
                                for k, v in sorted(cache["sources"].items())}
    OUT["computed_facets"] = {k: sorted(v.get("computed_facets") or [])
                              for k, v in sorted(cache["sources"].items())}
    ZZZ = [{"in": ["anywhere"], "any_of": ["zzzqqqnotatoken"], "match": "phrase"}]

    install_search_stubs(m, plans={s: {"total": 0} for s in cache["sources"]})
    OUT["ruled_out"] = {}
    for s in sorted(cache["sources"]):
        a = (per(run(m, cache, sources=[s], where=ZZZ), s).get("absence") or {})
        OUT["ruled_out"][s] = [sorted(a.get("ruled_out") or []), a.get("is_absence")]

    install_search_stubs(m, plans={"meds": {"total": 7, "n_cards": 3, "downgraded": True}})
    r = per(run(m, cache, sources=["meds"], semantic=True, question="q"), "meds")
    OUT["downgraded"] = [(r.get("count") or {}).get("records_matched"),
                         (r.get("count") or {}).get("counts_what"),
                         bool(r.get("semantic_downgraded")), r.get("match_kind")]

    install_search_stubs(m, plans={"med_comms": {"total": 9}})
    base = m._request
    async def facet_stub(method, endpoint, *, payload=None, params=None):
        env = await base(method, endpoint, payload=payload, params=params)
        if endpoint == m.EP_FACETS and env.get("_ok"):
            b = copy.deepcopy(env["_body"])
            b.setdefault("facets", {})["restricted.raw"] = [
                {"term": "Accessible", "count": 6}, {"term": "Restricted", "count": 3}]
            return {**env, "_body": b}
        return env
    m._request = facet_stub
    d = per(run(m, cache, sources=["med_comms"], where=ZZZ, distribution=True),
            "med_comms").get("distribution") or {}
    OUT["labels"] = json.dumps((d.get("fields") or {}).get("restricted"), sort_keys=True)

    m._CACHE = None
    cache = warm(m)
    moved = [dict(e, date="2026-09-01T00:00:00") if e["id"] == "meds" else e
             for e in copy.deepcopy(UPDATES_BODY)]
    base2 = m._request
    async def upd_stub(method, endpoint, *, payload=None, params=None):
        if endpoint == m.EP_UPDATES:
            return {"_ok": True, "_status": {"executed": True}, "_body": moved}
        return await base2(method, endpoint, payload=payload, params=params)
    m._request = upd_stub
    rep = asyncio.run(m.refresh_freshness(cache))
    OUT["refresh"] = json.dumps(rep, sort_keys=True, default=str)
except Exception as exc:  # a mutation that breaks the probe IS a behavioural difference
    OUT["error"] = f"{type(exc).__name__}: {exc}"[:300]
print(json.dumps(OUT, sort_keys=True, default=str))
'''

# `anchor` must match the shipped source EXACTLY ONCE. Zero or many is `not-applied`, never a
# coverage verdict: a refactor that moved an anchor would otherwise read as a finding.
MUTANTS: list[dict[str, Any]] = [
    {
        "id": "null-control",
        "finding": "control",
        "invariant": "the copied tree is green before anything is mutated",
        "anchor": None, "replacement": None, "expect": "survived",
    },
    {
        "id": "inert-control",
        "finding": "control",
        # The control wave 9 lacked, and the one that would have caught REV-B on the first run: a
        # change that provably alters nothing must be reported as `inert`, not `survived`.
        "invariant": "a change with no behavioural effect is reported as inert, not as survived",
        "anchor": "def _workspace_root() -> Path:\n",
        "replacement": "def _workspace_root() -> Path:\n    # inert-control\n",
        "expect": "inert",
    },
    {
        "id": "anchor-control",
        "finding": "control",
        # Guards the quietest failure: an anchor that stops matching after a refactor. The anchor
        # check runs after the tree copy -- cheap, but not free, and wave 9's comment claimed
        # otherwise.
        "invariant": "a stale anchor is reported as not-applied, never as survived",
        "anchor": "    # THIS LINE DOES NOT EXIST IN server.py AND MUST NOT BE ADDED\n",
        "replacement": "    pass\n",
        "expect": "not-applied",
    },
    {
        "id": "runner-control",
        "finding": "control",
        # `response_assembly_check.py` section 3b covers this explicitly: `basic_knn` returns
        # `totalHits: 0` with the real figure in `totalHitsKnn`.
        "invariant": "the semantic lane's count comes from totalHitsKnn, not totalHits",
        "anchor": "    primary = semantic if lane == LANE_SIMILARITY else keyword\n",
        "replacement": "    primary = keyword\n",
        "expect": "killed",
    },
    {
        "id": "REV-A-no-refresh",
        "finding": "R2",
        "invariant": ("freshness is re-read per call, so a process that straddles a reindex "
                      "cannot affirm the index has not moved"),
        "anchor": ("    is a fact about this call, not about the source.\n"
                   "    \"\"\"\n"
                   "    env = await _get(EP_UPDATES)\n"),
        "replacement": ("    is a fact about this call, not about the source.\n"
                        "    \"\"\"\n"
                        "    return {\"reread\": True}\n"
                        "    env = await _get(EP_UPDATES)\n"),
        # PIN CORRECTED from measurement, wave 10: was `survived`, now `killed`. Nothing about the
        # connector changed here -- `refresh_freshness` was already right. What changed is that it
        # is finally ASSERTED: `grep -l refresh_freshness *_check.py` returned nothing at all
        # before wave 10, so the function wave 8 added to fix a set-and-never-read defect, and in
        # which three review gates then found three more, had zero coverage in any harness.
        # `startup_cache_check.py` section 31 now requires the data-updates GET to actually happen,
        # so gutting it is caught. This is the survivor count improving for the right reason.
        "expect": "killed",
    },
    {
        "id": "REV-B-no-literal-evidence",
        "finding": "R1",
        # `and`, not `or`. Wave 9 shipped `{} or {<comprehension>}`, which returns the
        # comprehension -- so the mutant erased nothing and its `survived` verdict was cited as
        # evidence for a claim it could not support. `{} and {...}` returns `{}`. Measured:
        # survives, so the conclusion held and only the evidence was missing.
        "invariant": ("the program filter's stored literal is established from live evidence "
                      "before any zero is called an absence"),
        "anchor": "                src[\"filter_field_terms\"] = {\n",
        "replacement": "                src[\"filter_field_terms\"] = {} and {\n",
        # PIN CORRECTED from measurement, wave 10: was `survived`. The mutation is
        # unchanged -- what changed is that section 13 of `response_assembly_check.py` now
        # asserts BOTH sides of the absence gate, so a source losing its filter evidence,
        # or having that evidence asserted without a check, is caught. These two were the
        # last surviving mutants in the group.
        "expect": "killed",
    },
    {
        "id": "REV-C-downgrade-count",
        "finding": "R3",
        "invariant": "a count describes the records beside it",
        # RE-POINTED, wave 10. The old anchor still matched, but the fix moved the downgrade
        # detection ABOVE `reconcile_count` -- so inserting `count[...]` there now references a
        # name that does not exist yet and the mutant would report `error`, not a verdict. Moved
        # below the assignment, and narrowed to fire only on a downgrade: forcing every count to
        # 999999 would be caught by a dozen assertions and prove nothing about this one.
        # RE-POINTED TWICE in wave 10. The first re-point spanned the `reconcile_count` call and
        # the `matched =` line; R1b's fix then inserted the establishment verdict BETWEEN them and
        # the two-line anchor stopped matching -- reported as `not-applied`, correctly, by the guard
        # that exists for exactly this. Anchored on the single `matched =` line now, the narrowest
        # span that still sits after `count` exists. Six of seven anchors sit on lines a fix must
        # touch, and this is what that costs.
        "anchor": '    matched = count.get("records_matched")\n',
        "replacement": ('    if downgraded:\n'
                        '        count["records_matched"] = 999999\n'
                        '    matched = count.get("records_matched")\n'),
        "expect": "killed",
    },
    {
        "id": "R1-false-rule-out",
        "finding": "R1",
        # `ZERO_CAUSES`' own comment: "Nothing may be listed here that was not actually checked."
        # This mutant lists it without checking. It is also the cheapest way to green wave 9's R1
        # case, which is why that case was rewritten to demand positive evidence instead.
        "invariant": "a cause is only listed as ruled out when it was actually checked",
        "anchor": ("        if literal_live:\n"
                   "            ruled_out.append(\"broken_program_filter\")\n"),
        "replacement": ("        if True:\n"
                        "            ruled_out.append(\"broken_program_filter\")\n"),
        # PIN CORRECTED from measurement, wave 10: was `survived`. The mutation is
        # unchanged -- what changed is that section 13 of `response_assembly_check.py` now
        # asserts BOTH sides of the absence gate, so a source losing its filter evidence,
        # or having that evidence asserted without a check, is caught. These two were the
        # last surviving mutants in the group.
        "expect": "killed",
    },
    {
        "id": "R4-date-filter-inert",
        "finding": "R4",
        "invariant": "our own since/until filter actually removes the records it reports removing",
        "anchor": "        records = kept\n",
        "replacement": "        records = list(records)\n",
        # Pin corrected from measurement in wave 9: predicted `survived`, measured `killed`. The
        # suite covers the filter doing its job. R4's defect is narrower -- the BASIS of the
        # `count_discrepancy` comparison -- and no text mutation reaches it. Part B's R4 does.
        "expect": "killed",
    },
    {
        "id": "R7-labels-dead-again",
        "finding": "R7",
        # WAS the inverse mutant `R7-labels-repaired`, which APPLIED the fix and survived -- no
        # assertion could tell a working label map from a permanently empty one, in either
        # direction. Wave 10 landed that fix, so the old anchor is stale and the mutant must now
        # run FORWARDS: put back the wrong key and require the suite to notice.
        #
        # One character of the two original breaks is enough: `warm_cache` stores `value_labels`,
        # so reading `valueLabels` empties the map exactly as before.
        "invariant": "configured facet labels reach the distribution",
        "anchor": ('            declared_labels[str(_fid)] = '
                   'definition.get("value_labels") or {}\n'),
        "replacement": ('            declared_labels[str(_fid)] = '
                        'definition.get("valueLabels") or {}\n'),
        "expect": "killed",
    },
    {
        "id": "R5-collapse-executed-field",
        "finding": "R5",
        # Re-pointed: wave 10 replaced the anchored line with `_condition_as_asked`. Collapse
        # `executed_fields` back through `_role_of` -- the erasure itself, now that a suite
        # assertion demands the exact executed set.
        "invariant": "the fields reported back are the fields the request actually constrained",
        "anchor": '        "executed_fields": bare,\n',
        "replacement": '        "executed_fields": [_role_of(src, f) for f in wire],\n',
        "expect": "killed",
    },
    {
        "id": "R2b-vanished-unflagged",
        "finding": "R2b",
        # A source that had a real date and drops out of the response, left silently dated.
        "invariant": "a source that vanishes from data-updates is flagged, not silently dated",
        "anchor": ('                src["freshness"] = {**prior, "absent": True, '
                   '"vanished_since_startup": True}\n'),
        "replacement": "                pass\n",
        "expect": "killed",
    },
    {
        "id": "R6-mint-on-semantic",
        "finding": "R6",
        # Mint a handle on the semantic engine again -- the token that can only ever be refused.
        "invariant": "no population handle is minted on an engine that cannot replay it",
        "anchor": ('        if plan["engine"] == "basic":\n'
                   '            r["population_not_offered"] = (\n'),
        "replacement": ('        if False:\n'
                        '            r["population_not_offered"] = (\n'),
        "expect": "killed",
    },
    {
        "id": "R6b-drop-question-echo",
        "finding": "R6b",
        # Drop the echo, leaving a semantic population that cannot say what produced it.
        "invariant": "a semantic population names the text it was matched against",
        "anchor": '        result["question_asked"] = plan["search"]["knnSearchQuery"]\n',
        "replacement": '        pass\n',
        "expect": "killed",
    },
    {
        "id": "R1-absence-ungated",
        "finding": "R1",
        # Remove the gate itself: claim absence again without establishing the filter.
        "invariant": "a zero is called an absence only when the program filter is established",
        "anchor": '    if "broken_program_filter" not in ruled_out:\n',
        "replacement": "    if False:\n",
        "expect": "killed",
    },
    {
        "id": "R1b-claim-established",
        "finding": "R1b",
        # Claim the population was established regardless of the evidence -- the exact lie the
        # three-state verdict exists to prevent, and the reason the case asserts an identity
        # against independently computed evidence rather than the presence of a key.
        "invariant": "the reported establishment verdict equals the evidence",
        "anchor": '    count["population_established"] = established\n',
        "replacement": '    count["population_established"] = True\n',
        "expect": "killed",
    },
    {
        "id": "R2a-move-invalidates-nothing",
        "finding": "R2a",
        # Detect the move, report it, and carry the pre-move facts forward anyway -- which is
        # exactly what shipped before wave 10.
        "invariant": "a detected reindex invalidates the facts derived from that index",
        # RE-POINTED in the wave-10 follow-up: S2's fix added `mapping_complete` and
        # `facetable_program_filter` to this loop, so the three-fact anchor stopped matching and the
        # guard reported `not-applied` -- correctly. Third time a fix has moved an anchor in this
        # group; the third obligation is real work, not bookkeeping.
        "anchor": ('            for fact in ("mapping_retrieved", "mapping_complete", '
                   '"computed_facets",\n'
                   '                         "filter_field_terms", "facetable_program_filter"):\n'
                   "                src[fact] = None\n"),
        "replacement": "            pass\n",
        "expect": "killed",
    },
]


def _stage(root: Path) -> Path:
    """Copy `tools/` and `config/` into a throwaway root, and return the server file.

    `config/` comes too because `_workspace_root()` is `parents[2]` of `server.py` and the
    program-scope YAML resolves from it. Copy only `tools/` and every source drops out of the
    cache, which reads as `killed` for every mutant.
    """
    shutil.copytree(HERE, root / "tools",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(WORKSPACE / "config", root / "config")
    probe = root / "tools" / "_liveness_probe.py"
    probe.write_text(_LIVENESS_PROBE, encoding="utf-8")
    probe.chmod(0o755)
    return root / "tools" / "ics_muse" / "server.py"


def _fingerprint(root: Path) -> str:
    """Observable behaviour of the staged tree, as a stable string."""
    try:
        proc = subprocess.run([str(root / "tools" / "_liveness_probe.py")],
                              capture_output=True, text=True, timeout=600)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return f"PROBE-FAILED {type(exc).__name__}: {exc}"
    lines = [x for x in proc.stdout.splitlines() if x.startswith("{")]
    if not lines:
        return f"PROBE-NO-OUTPUT rc={proc.returncode} {(proc.stderr or '')[-200:]}"
    return lines[-1]


BASELINE: str | None = None


def _classify_nonzero_suite(returncode: int, stdout: str, stderr: str) -> tuple[str, str]:
    """Classify a failed suite without attributing unrelated failures to a MUSE mutant."""
    failures = [
        line for line in stdout.splitlines()
        if re.match(r"^\s*FAIL\s", line)
    ]
    parsed = [
        (line, match)
        for line in failures
        if (match := _HARNESS_FAILURE.match(line))
    ]
    sentinel = [
        line for line, match in parsed
        if int(match.group("passed")) < 0 or int(match.group("ran")) < 0
    ]
    attributable = [
        line for line, match in parsed
        if match.group("name") in MUSE_HARNESS_NAMES
        and int(match.group("passed")) < int(match.group("pinned"))
    ]
    if sentinel:
        return "error", (f"a harness crashed rather than failing an assertion: "
                         f"{sentinel[0].strip()[:90]}")
    if not failures:
        return "error", (f"exit {returncode} with no failing assertion -- a crash, "
                         f"not a detection | {(stderr or '')[-200:]}")
    if not attributable:
        return "error", ("exit non-zero with no named MUSE harness reporting a shortfall against "
                         "its pin -- not attributable to the MUSE mutation")
    return "killed", f"exit {returncode}, {len(attributable)} attributable MUSE shortfall(s)"


def run_mutant(mut: dict[str, Any]) -> tuple[str, str]:
    """Copy the tree, apply one text mutation, let the suite judge it.

    Verdicts: survived | killed | inert | not-applied | error. See the module docstring.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        target = _stage(root)
        if mut["anchor"] is not None:
            text = target.read_text(encoding="utf-8")
            hits = text.count(mut["anchor"])
            if hits != 1:
                return "not-applied", f"anchor matched {hits} times, expected exactly 1"
            target.write_text(text.replace(mut["anchor"], mut["replacement"]), encoding="utf-8")
        try:
            proc = subprocess.run([str(root / "tools" / "contract_map_check.py")],
                                  capture_output=True, text=True, timeout=900)
        except (subprocess.TimeoutExpired, OSError) as exc:
            return "error", f"{type(exc).__name__}: {exc}"

        if proc.returncode != 0:
            # A `killed` verdict claims the suite DETECTED the mutation. A non-zero exit with no
            # failing assertion is a crash, which is the opposite of coverage -- wave 9 called
            # both `killed`. Measured then: a syntax error and an import-time NameError both
            # reported `exit 1, 0 failing assertion(s)`, while all three genuine kills reported 2.
            # CORRECTED after the integrated review. Counting FAIL lines was not enough: when a
            # HARNESS crashes, `contract_map_check` section 3 routes it to a `-1` sentinel and
            # prints `FAIL  <harness>: -1/-1 at pinned 118`. That is a FAIL line, so the previous
            # guard passed and the crash was laundered into `killed` -- with zero assertions
            # executed, which is the opposite of coverage. Measured by injecting a raise into
            # `_parse_updates`: five FAIL lines, no assertions run.
            #
            # The old guard only caught a crash BEFORE section 3 ran. A `killed` verdict now
            # requires a harness that actually reported a total AND fell short of its pin.
            return _classify_nonzero_suite(proc.returncode, proc.stdout, proc.stderr or "")

        if not [x for x in proc.stdout.splitlines() if "suite total" in x]:
            return "error", "exit 0 without the suite-total line, so the suite may not have run"

        # Survived the suite -- but did it do anything? Only asked of mutants that survive,
        # because a kill is its own proof of liveness.
        fp = mut["anchor"] is None and BASELINE or _fingerprint(root)
        if fp.startswith("PROBE-"):
            return "error", fp
        if mut["anchor"] is not None and fp == BASELINE:
            return "inert", "identical observable behaviour to the pristine tree"
        return "survived", "suite green, and the mutation is observable"


def part_a() -> None:
    global BASELINE
    print("PART A -- source mutation, judged by the whole existing suite\n")
    with tempfile.TemporaryDirectory() as td:
        _stage(Path(td))
        BASELINE = _fingerprint(Path(td))
    # CORRECTED after the integrated review, which found the failure mode ALL FOUR CONTROLS MISS:
    # a probe that goes blind in a way that is stable across trees. Break its fixture and it emits
    # `{"error": "..."}` -- 39 bytes, no `PROBE-` prefix -- so it was accepted, every mutant then
    # compared equal to that constant, and every one was reported `inert`. The controls pin
    # survive/kill/anchor/inert-DETECTION; nothing pinned the probe's ability to say DIFFERENT.
    bad = None
    if BASELINE.startswith("PROBE-"):
        bad = BASELINE
    else:
        try:
            parsed = json.loads(BASELINE)
        except ValueError as exc:
            parsed, bad = {}, f"baseline is not JSON: {exc}"
        if not bad and parsed.get("error"):
            bad = f"the probe raised: {parsed['error']}"
        missing = [k for k in ("filter_field_terms", "computed_facets", "ruled_out", "downgraded",
                              "labels", "refresh") if not parsed.get(k)]
        if not bad and missing:
            bad = f"signals absent or empty: {missing}"
    if bad:
        check("the liveness baseline was established", False, bad)
        return
    check("the liveness baseline was established", True, f"{len(BASELINE)} bytes of signal")

    ids = [m["id"] for m in MUTANTS]
    check("every mutant id is unique", len(set(ids)) == len(ids),
          f"{len(ids)} mutants")

    external = "  FAIL  external_adapter_check.py: 156/156 at pinned 157: exit 1"
    muse = "  FAIL  response_assembly_check.py: 208/208 at pinned 209: exit 1"
    aggregate = "FAILED: response_assembly_check.py: 208/208 at pinned 209"
    crashed = "  FAIL  response_assembly_check.py: -1/-1 at pinned 209: exit -1"
    verdict, _ = _classify_nonzero_suite(1, external, "")
    check("an external harness shortfall is not attributed to a MUSE mutant",
          verdict == "error", verdict)
    verdict, _ = _classify_nonzero_suite(1, muse, "")
    check("a named MUSE harness shortfall kills a MUSE mutant",
          verdict == "killed", verdict)
    verdict, detail = _classify_nonzero_suite(1, f"{muse}\n{aggregate}", "")
    check("the aggregate FAILED line cannot double-count a MUSE shortfall",
          verdict == "killed" and "1 attributable" in detail, f"{verdict}: {detail}")
    verdict, _ = _classify_nonzero_suite(1, crashed, "")
    check("a harness crash remains an error rather than mutation coverage",
          verdict == "error", verdict)

    verdicts: dict[str, str] = {}
    for mut in MUTANTS:
        verdict, detail = run_mutant(mut)
        verdicts[mut["id"]] = verdict
        check(f"{mut['id']} [{mut['finding']}] -> {verdict}, pinned {mut['expect']}",
              verdict == mut["expect"], detail)

    controls = {"null-control": "survived", "runner-control": "killed",
                "anchor-control": "not-applied", "inert-control": "inert"}
    bad = {k: verdicts.get(k) for k, v in controls.items() if verdicts.get(k) != v}
    check("the gate's own controls hold, so the verdicts above are attributable",
          not bad, "CONTROLS BROKEN -- disregard every Part A verdict: "
                   f"{bad}" if bad else "all four")

    real = [m for m in MUTANTS if m["finding"] != "control"]
    survived = [m["id"] for m in real if verdicts.get(m["id"]) == "survived"]
    prefix = "" if not bad else "[CONTROLS BROKEN, DISREGARD] "
    print(f"\n  {prefix}{len(survived)} of {len(real)} real mutants survived the whole suite: "
          f"{', '.join(survived) or 'none'}")


# ==========================================================================
# PART B -- fault injection, asserted here
# ==========================================================================
# Pinned. Every entry is a KNOWN defect from the independent review: it fails today, and that is
# the pinned outcome. Fixing it must also delete its entry and re-anchor its mutant.
KNOWN_FAIL: dict[str, str] = {
    "R8  a has-access call site exists on the locator path":
        "design/model-facing-tools/README.md line 284 requires POST /v1/document/has-access "
        "before emitting a locator; no endpoint constant and no call site exist. Openability is "
        "UNVERIFIED, not disproved -- has-access also returns false for a nonexistent id, and "
        "needs a card.id in hand",
}


def wrap_updates(m: Any, body: Any) -> None:
    """Layer a different /data-updates response over the installed stub, leaving the rest.

    Never torn down. Every case that follows calls `warm()` or `install_search_stubs`, both of
    which rebuild from wave 1's pristine stub, so a layer cannot outlive its case -- but a case
    inserted between them that does neither would inherit this one.
    """
    base = m._request

    async def stub(method: str, endpoint: str, *, payload=None, params=None):
        if endpoint == m.EP_UPDATES:
            return {"_ok": True, "_status": {"executed": True}, "_body": body}
        return await base(method, endpoint, payload=payload, params=params)

    m._request = stub


def wrap_facets(m: Any, extra: dict[str, Any]) -> None:
    """Add facet fields to whatever the facet stub returns, leaving the rest."""
    base = m._request

    async def stub(method: str, endpoint: str, *, payload=None, params=None):
        env = await base(method, endpoint, payload=payload, params=params)
        if endpoint == m.EP_FACETS and env.get("_ok"):
            body = copy.deepcopy(env["_body"])
            body.setdefault("facets", {}).update(extra)
            return {**env, "_body": body}
        return env

    m._request = stub


ZZZ = [{"in": ["anywhere"], "any_of": ["zzzqqqnotatoken"], "match": "phrase"}]
# The seven silent-zero causes, written out rather than read from `m.ZERO_CAUSES`. Wave 9's R1
# computed `set(m.ZERO_CAUSES) - ruled_out`, so deleting a name from the connector's own tuple
# turned the case green -- measured, with the suite still at 729. A gate may not take its
# yardstick from the thing it measures.
CAUSES = ("denied_datasource", "broken_program_filter", "nonexistent_condition_field",
          "multi_word_phrase", "question_mark_in_query", "asterisk_in_condition",
          "silent_widening")
# The startup facts that describe the INDEX and therefore cannot outlive a reindex.
#
# NARROWED, wave 10, and the correction matters: `knn` and `readable` were in this tuple and do not
# belong. They come from the CONFIGURATION endpoint -- `projectKnnSettings` and
# `fullDocumentTextRetrievalEnabled` -- not from the index, so a reindex says nothing about them and
# requiring them to go UNKNOWN would have forced a fix that refuses semantic retrieval and document
# reads for no reason. Configuration drift has its own detector, `config_fingerprint`.
#
# The independent review listed all six startup snapshots in one breath, which is fair as a
# complaint about staleness in general; only these three are invalidated by the specific event this
# case injects. Third time in this group an assertion of mine would have forced a wrong fix.
INDEX_FACTS = ("mapping_retrieved", "computed_facets", "filter_field_terms")


def part_b() -> None:  # noqa: C901 - a linear assertion script, deliberately flat
    print("\n\nPART B -- fault injection. C* are controls and pass today; R* are the findings.\n")
    m = load_server()
    cache = warm(m)
    sources = sorted(cache.get("sources") or {})

    # ---- R1 / R1b -------------------------------------------------------------
    # An absence claim must rest on POSITIVE evidence that the filter is live, read from the
    # cache rather than from the connector's own `ruled_out` list. Wave 9 asserted only that the
    # unresolved set was empty, which two separate lies satisfied: ruling the cause out without
    # checking it, and deleting the cause's name from the vocabulary.
    install_search_stubs(m, plans={s: {"total": 0} for s in sources})
    seen, bad, undefensible = 0, [], []
    for source in sources:
        res = per(run(m, cache, sources=[source], where=ZZZ), source)
        absence = res.get("absence") or {}
        if absence:
            seen += 1
        ruled = set(absence.get("ruled_out") or [])
        src = cache["sources"][source]
        # Evidence computed HERE from configuration plus startup facts, not taken from the
        # connector's own claim: the filter field's term list holding a configured literal with a
        # non-zero count. Used only to check that an EXISTING rule-out is backed -- R1 itself does
        # not require this particular mechanism, because a fix may establish liveness another way
        # (a `searchField` prefix probe, say) and an assertion that forecloses a legitimate fix is
        # the mistake R6 was written wrong for.
        # THREE states, computed here and independently: True the literal is in the source's own
        # value list with a non-zero count; False the field IS facetable and its terms came back
        # and no literal matches, which is affirmative evidence of a broken filter; None it cannot
        # be checked at all -- meds, whose filter field is not among its computed facets.
        pf = src.get("program_filter") or {}
        field = next(iter(pf), "")
        wanted = {str(v) for vals in pf.values()
                  for v in (vals if isinstance(vals, list) else [vals])}
        terms = src.get("filter_field_terms") or {}
        if not pf or field not in (src.get("computed_facets") or []) or not terms:
            evidence = None
        else:
            evidence = any(t in wanted and (terms.get(t) or 0) > 0 for t in terms)
        if absence.get("is_absence") is True and "broken_program_filter" not in ruled:
            bad.append(source)
        # ASSERTION CORRECTED TWICE, wave 10. Both corrections are on the record because the second
        # one caught the first.
        #
        # v1 required `defensible` to go False when the literal was unestablished -- exactly what
        # `decisions.md` D15 then rejected. `defensible` means the count is a pure function of the
        # request; establishment is an independent fact; folding them is the
        # one-name-two-quantities shape this API already punishes. v1 would have forced the design
        # D15 argues against and broken two existing assertions that are right about engine purity.
        #
        # v2 required only that no key claim establishment when the evidence denies it -- which a
        # connector reporting NOTHING satisfies. A vacuous pass, in a file whose subject is
        # assertions that pass while testing nothing.
        #
        # v3, an IDENTITY: exactly one key must carry the three-state verdict, and it must EQUAL
        # the evidence computed above. No key fails. A wrong verdict fails. Any key NAME passes, so
        # the shape stays free.
        verdicts = {k: v for k, v in (res.get("count") or {}).items()
                    if "establish" in k and (v is None or isinstance(v, bool))}
        if len(verdicts) != 1 or next(iter(verdicts.values())) is not evidence:
            undefensible.append(f"{source}: evidence={evidence} reported={verdicts or 'nothing'}")
    check("C1  an absence block came back for every in-scope source",
          seen == len(sources), f"{seen} of {len(sources)}")
    # C1b, REWRITTEN TWICE. v1 tested `not absence.get("checks")` and could never fail, because
    # `diagnose_zero` appends its first finding unconditionally. v2 tested for the backing finding
    # text -- better, but STILL unfailable, and for a deeper reason the sabotage found: no fixture
    # source ever reaches the state this control is about. `broken_program_filter` is only ruled out
    # where the evidence is present, so "ruled out while evidence is absent" never arises unless the
    # connector lies, and the connector only lies under a Part A mutant.
    #
    # v3 CONSTRUCTS the state. Strip the evidence from a source that has it, re-run the zero, and
    # require the rule-out to disappear with it. That can fail, and it fails under exactly the lie
    # `R1-false-rule-out` injects.
    with_ev = next((s for s in sources
                    if (cache["sources"][s].get("filter_field_terms") or {})), None)
    if with_ev is None:
        check("C1b a rule-out disappears when its evidence does", False,
              "STUB GAP: no fixture source carries filter-field evidence to strip")
    else:
        stripped_ev = copy.deepcopy(cache)
        stripped_ev["sources"][with_ev]["filter_field_terms"] = {}
        still = set((per(run(m, stripped_ev, sources=[with_ev], where=ZZZ), with_ev)
                     .get("absence") or {}).get("ruled_out") or [])
        check("C1b a rule-out disappears when its evidence does",
              "broken_program_filter" not in still,
              f"{with_ev} still ruled it out with an empty value list" if
              "broken_program_filter" in still else
              f"{with_ev}: rule-out present with evidence, absent without it")
    check("R1  an absence claim carries positive evidence that the program filter is live",
          not bad, f"claimed absence without ruling the filter out on: {bad}" if bad
          else "no absence is claimed while the filter is unestablished")
    check("R1b the count reports whether the population was established, and reports it right",
          not undefensible, "; ".join(undefensible)[:180] if undefensible
          else "every source's reported verdict equals the evidence")

    # ---- C2 / R2a / R2c -------------------------------------------------------
    # A TRANSITION no harness produces: warm, move the index, then call. The negative control is
    # what makes this attributable -- without it, never establishing the facts at all satisfies
    # the case, which is the opposite of the fix.
    # Which facts are established is read from the cache, not assumed. meds legitimately has no
    # `filter_field_terms` at all -- `product_name` is not among its computed facets, the poison
    # rule -- so demanding all five was wrong, and this control caught it on the first run. R2a
    # then requires exactly the established ones to go UNKNOWN, which makes it non-vacuous by
    # construction rather than by my choosing the right source.
    before = {f: cache["sources"]["meds"].get(f) for f in INDEX_FACTS}
    established_before = sorted(f for f, v in before.items() if v is not None)
    # >= 2, not >= 3. meds has only two of the three: `filter_field_terms` is never populated for
    # it, because `product_name` is not among its computed facets -- the poison rule. The threshold
    # was 3 when this tuple still wrongly included `knn` and `readable`.
    check("C2  meds carries index-dependent startup facts before the move",
          len(established_before) >= 2, f"established: {established_before}")

    moved_body = [dict(e, date="2026-09-01T00:00:00") if e["id"] == "meds" else e
                  for e in copy.deepcopy(UPDATES_BODY)]
    wrap_updates(m, moved_body)
    report = asyncio.run(m.refresh_freshness(cache))
    detected = "meds" in (report.get("moved_since_startup") or [])
    check("C2b the move is detected at all", detected, str(report.get("moved_since_startup")))
    retained = [f for f in established_before if cache["sources"]["meds"].get(f) is not None]
    check("R2a a detected index move makes index-dependent startup facts UNKNOWN",
          not retained,
          f"of {established_before}, still carried across the move: {retained}" if retained
          else f"all of {established_before} became UNKNOWN after the move")

    # ASSERTION CORRECTED, wave 10, and by the design the owner chose rather than by the code.
    # v1 required the causes to be UNRULED after a move, full stop. That is right for a
    # blank-and-leave-blank fix and WRONG for blank-then-re-read: if the mapping and facet list have
    # been re-established against the new index, ruling those causes out is correct and refusing to
    # is a capability loss for nothing. Asserting v1 would have forced the design that was not
    # chosen.
    #
    # The property is a DISJUNCTION: either the facts were re-established, or the causes they
    # support are not ruled out. What is forbidden is the third state -- ruling out on pre-move
    # evidence -- which is the defect.
    install_search_stubs(m, plans={"meds": {"total": 0}})
    wrap_updates(m, moved_body)
    res = per(run(m, cache, sources=["meds"], where=ZZZ), "meds")
    ruled = set((res.get("absence") or {}).get("ruled_out") or [])
    stale = sorted(ruled & {"nonexistent_condition_field", "broken_program_filter",
                            "denied_datasource"})
    # Read from the live cache the call actually planned against, not from the one warmed above --
    # a re-read REBUILDS the cache object, so the old reference would report the blanked state
    # forever and the disjunction would pass without anything having been re-established.
    current = (m._CACHE or {}).get("sources", {}).get("meds", {})
    reestablished = (not current.get("index_moved")
                     and all(current.get(f) is not None for f in established_before))
    check("R2c after a move, a cause is ruled out only on evidence from the CURRENT index",
          reestablished or not stale,
          f"re-established={reestablished}, ruled out={stale or 'none'}")

    # ---- C3 / R2b -------------------------------------------------------------
    # meds disappears from a later response. Its date must not silently stand -- and the four
    # that DID appear must not be flagged, or flagging everything satisfies the case. Measured in
    # wave 9: a blanket `absent: True` on every source passed, with the suite still green.
    # `warm()` reinstalls wave 1's pristine stub, which discards the layer above.
    m._CACHE = None
    cache2 = warm(m)
    others = [s for s in sources if s != "meds"]
    wrap_updates(m, [e for e in copy.deepcopy(UPDATES_BODY) if e["id"] != "meds"])
    asyncio.run(m.refresh_freshness(cache2))
    meds_f = cache2["sources"]["meds"].get("freshness") or {}
    other_f = {s: (cache2["sources"][s].get("freshness") or {}) for s in others}
    # `signals` is deliberately absent from UPDATES_BODY, so it is legitimately flagged and is not
    # part of the control.
    control_sources = [s for s in others
                       if any(e["id"] == s for e in UPDATES_BODY)]
    check("C3  the sources that remained in the response were present to begin with",
          len(control_sources) >= 2, str(control_sources))
    meds_flagged = bool(meds_f.get("absent") or meds_f.get("unread"))
    others_clean = [s for s in control_sources
                    if not (other_f[s].get("absent") or other_f[s].get("unread"))]
    check("R2b a source that vanishes from /data-updates is flagged, and only it",
          meds_flagged and len(others_clean) == len(control_sources),
          f"meds flagged={meds_flagged}, still-present sources left unflagged="
          f"{len(others_clean)}/{len(control_sources)}")

    # ---- R3 -------------------------------------------------------------------
    # An IDENTITY between two live outputs: the downgraded result must report the same count and
    # the same description as a genuine term-lane request over the same fixture. Wave 9 asserted
    # `"embedding" not in counts_what`, a blocklist -- measured, rewording it to "vector" while
    # keeping the wrong number passed. An identity cannot be satisfied by rewording, because both
    # sides would move together.
    m._CACHE = None
    cache = warm(m)
    install_search_stubs(m, plans={"meds": {"total": 7, "n_cards": 3, "downgraded": True}})
    dg = per(run(m, cache, sources=["meds"], semantic=True, question="q"), "meds")
    install_search_stubs(m, plans={"meds": {"total": 7, "n_cards": 3}})
    term = per(run(m, cache, sources=["meds"], where=ZZZ), "meds")
    dgc, tc = (dg.get("count") or {}), (term.get("count") or {})
    check("C4  the downgrade fixture actually downgrades",
          bool(dg.get("semantic_downgraded")) and dg.get("match_kind") == "term",
          f"flag={bool(dg.get('semantic_downgraded'))}, match_kind={dg.get('match_kind')}")
    check("R3  a semantic downgrade reports the same count and description as the term lane",
          dgc.get("records_matched") == tc.get("records_matched")
          and dgc.get("counts_what") == tc.get("counts_what"),
          f"downgraded {dgc.get('records_matched')} / term {tc.get('records_matched')}; "
          f"descriptions {'match' if dgc.get('counts_what') == tc.get('counts_what') else 'differ'}")

    # ---- C5 / R4 --------------------------------------------------------------
    # `n_cards == total`. Wave 9 used 3 cards against a total of 19, so the discrepancy guard
    # fired on records-vs-count with no date filter present at all -- measured -- which means the
    # case could never have judged its own fix, and the correct fix left it red. C5 is that
    # measurement, kept permanently so the fixture cannot drift back.
    install_search_stubs(m, plans={"med_comms": {"total": 19, "n_cards": 19}})
    plain = per(run(m, cache, sources=["med_comms"], where=ZZZ), "med_comms")
    check("C5  the fixture reports no discrepancy without a date filter, so R4 isolates one",
          "count_discrepancy" not in plain,
          "records and count agree before the filter is applied" if "count_discrepancy" not in plain
          else str((plain.get("count_discrepancy") or {}).get("detail", ""))[:120])
    dated = per(run(m, cache, sources=["med_comms"], where=ZZZ, since="9999-01-01"), "med_comms")
    df = dated.get("date_filter") or {}
    check("R4  a date filter that removes every record does not manufacture a discrepancy",
          "count_discrepancy" not in dated
          and df.get("matched_by_muse") == 19 and df.get("surviving_our_filter") == 0,
          f"discrepancy={'yes' if 'count_discrepancy' in dated else 'no'}, "
          f"matched={df.get('matched_by_muse')}, surviving={df.get('surviving_our_filter')}")

    # ---- C6 / R5 --------------------------------------------------------------
    # The executed field must survive AND its siblings must not appear. Wave 9 required only that
    # the named field appear, which a wrong fix expanding every field to its whole role satisfied
    # -- the same erasure with more words.
    multi = next(((s, role, fields)
                  for s, src in sorted((cache.get("sources") or {}).items())
                  for role, fields in sorted((src.get("roles") or {}).items())
                  if isinstance(fields, list) and len(fields) > 1), None)
    check("C6  the fixture has a role spanning more than one field",
          multi is not None,
          f"{multi[0]}.{multi[1]} spans {multi[2]}" if multi else
          "STUB GAP: no source has a multi-field role, so the collapse cannot be exercised")
    if multi is None:
        check("R5  the field a request constrained survives, and its siblings do not",
              False, "not exercisable -- see C6")
    else:
        source, role, fields = multi
        install_search_stubs(m, plans={source: {"total": 4}})

        def entry(where_in: str) -> dict[str, Any]:
            got = per(run(m, cache, sources=[source],
                          where=[{"in": [where_in], "any_of": ["Effective"],
                                  "match": "phrase"}]), source).get("conditions_as_asked") or []
            return got[0] if got else {}

        def recoverable(e: dict[str, Any], expected: set[str]) -> bool:
            """Is the executed field set recoverable as the VALUE of some key?

            ASSERTION CORRECTED, wave 10, and the reason belongs on the record. The first version
            required the sibling field to be absent from the entry ENTIRELY. That killed the
            wrong-fix it was aimed at -- expanding every requested field to its whole role's list,
            the same erasure with more words -- but it also forbade a BETTER answer: naming the
            siblings under a separate key precisely to warn that restating by role would describe
            a larger population. Foreclosing a legitimate fix is the fault R6 was written wrong
            for, and this was the same fault one case over.
            Now: some key must hold exactly the executed set. Whole-role expansion still fails,
            because then no key holds exactly `{doc_status}`. Any key NAME is accepted, so the
            shape of the fix stays free.
            """
            return any(isinstance(v, list) and v and all(isinstance(x, str) for x in v)
                       and set(v) == expected for v in e.values())

        e_one, e_role = entry(fields[0]), entry(role)
        one_ok = recoverable(e_one, {fields[0]})
        role_ok = recoverable(e_role, set(fields))
        check("R5  the field a request constrained survives, and its siblings do not",
              one_ok and role_ok,
              f"constraining {fields[0]}: executed set recoverable={one_ok}; "
              f"constraining the role {role}: recoverable={role_ok}")

    # ---- R6 / R6b -------------------------------------------------------------
    # EITHER resolution is acceptable: encode the recipe and replay it, or decline to mint and say
    # why. Wave 9 required a handle to exist, which reported a legitimate fix as an unfixed defect.
    install_search_stubs(m, plans={"med_comms": {"total": 6, "knn": True}})
    question = "what is known about hepatotoxicity"
    first = per(run(m, cache, sources=["med_comms"], semantic=True, question=question), "med_comms")
    handle = first.get("population")
    check("C7  the semantic request itself succeeded",
          first.get("status") == "ok", str(first.get("status")))
    if handle:
        replayed = run(m, cache, sources=["med_comms"], population=handle)
        again = per(replayed, "med_comms")
        ok = again.get("status") == "ok" and not replayed.get("refusal")
        why = str(again.get("cause") or replayed.get("refusal") or "")[:70]
        detail = f"minted, replay {'succeeded' if ok else 'refused: ' + why}"
    else:
        ok = bool(first.get("population_not_offered") or first.get("population_error"))
        detail = ("not minted, with a stated reason" if ok
                  else "not minted and no reason given")
    check("R6  a minted population handle is replayable, or is not minted and says why", ok, detail)
    # RE-PINNED, wave 10, deliberately and with the reason on the record. This case required the
    # HANDLE to carry the question -- which is unsatisfiable once R6 is resolved by not minting a
    # handle on this engine, and those two are the same finding's two halves. The owner chose
    # don't-mint-plus-echo, so the property moves to where it can hold: the RESULT must name the
    # text it was matched against. Restated rather than deleted, because "a semantic population is
    # not self-describing" is a real complaint independent of replay.
    #
    # Asserted against the whole result, not one key, so the shape of the echo stays free.
    echoed = question in json.dumps(first, default=str)
    check("R6b a semantic population names the text it was matched against",
          echoed,
          "the question is echoed on the result" if echoed
          else f"absent from every key: {sorted(first)}")

    # ---- R7 -------------------------------------------------------------------
    # The label must land on ITS OWN field, and other fields must be untouched. Wave 9 tested for
    # the label text anywhere in the distribution, which a global merge of every definition's
    # labels would also satisfy -- rendering doc_status' values with restricted's labels.
    m._CACHE = None
    cache = warm(m)
    install_search_stubs(m, plans={"med_comms": {"total": 9}})
    wrap_facets(m, {"restricted.raw": [{"term": "Accessible", "count": 6},
                                       {"term": "Restricted", "count": 3}]})
    dist = (per(run(m, cache, sources=["med_comms"], where=ZZZ, distribution=True),
                "med_comms").get("distribution") or {}).get("fields") or {}
    rvals = [v.get("value") for v in ((dist.get("restricted") or {}).get("values") or [])]
    svals = [v.get("value") for v in ((dist.get("doc_status") or {}).get("values") or [])]
    check("C8  the injected facet field reached the distribution",
          bool(rvals), f"restricted values: {rvals}")
    check("R7  configured facet labels reach their own field, and only their own",
          "Full Access" in rvals and "Effective" in svals,
          f"restricted={rvals}, doc_status={svals} "
          f"(configured labels: Accessible->Full Access; doc_status declares none)")

    # ---- R8 -------------------------------------------------------------------
    # A CALL SITE, not a substring. Wave 9 grepped the whole file for "has-access", which a single
    # TODO comment satisfied -- measured. The requirement is real and traceable:
    # `design/model-facing-tools/README.md` line 284, "before emitting a locator". It is not
    # behaviourally testable offline: has-access needs a card.id in hand and returns false for a
    # nonexistent id as well as a denied one, so openability stays UNVERIFIED either way.
    ep = getattr(m, "EP_HAS_ACCESS", None)
    locator_src = inspect.getsource(m._locator)
    check("R8  a has-access call site exists on the locator path",
          isinstance(ep, str) and "EP_HAS_ACCESS" in locator_src,
          f"EP_HAS_ACCESS={'defined' if isinstance(ep, str) else 'undefined'}, "
          f"referenced from _locator={'EP_HAS_ACCESS' in locator_src}")


def main() -> int:
    args = [a for a in sys.argv[1:]]
    only_b = args == ["--part", "b"]
    if args and not only_b:
        print(f"unrecognised arguments: {args}\nusage: mutation_gate.py [--part b]")
        return 2
    print(__doc__.strip().splitlines()[0])
    print("=" * 78)
    if not only_b:
        part_a()
    part_b()

    print("\n" + "=" * 78)
    failed = set(FAILURES)
    pinned = set(KNOWN_FAIL)
    unexpected = sorted(failed - pinned)
    resolved = sorted(pinned - failed)

    # Part A is 24: the liveness baseline, id uniqueness, four classifier controls, 17 mutants,
    # and the controls verdict. Part B is 22: 10 controls and 12 cases. 24 + 22 = 46.
    #
    # CORRECTED after the integrated review, which found this comment reading "14 ... 11 mutants"
    # against a pin of 42 -- wrong by 6, in the file that says arithmetic beside a pin is worth
    # exactly as much as the pin. It was right when written and was not updated when mutants were
    # added. Recount it here whenever MUTANTS changes. Pinned for the same reason every harness here pins
    # its own size -- coverage that shrinks must fail rather than report N/N. A deleted mutant
    # otherwise costs nothing but a number in a line nothing checks, measured in wave 9 where
    # dropping one still exited 0.
    expected = 46 if not only_b else 22
    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} assertions passed")
    if CHECKS != expected:
        print(f"GATE: expected {expected} assertions, ran {CHECKS}. Update deliberately, never "
              "to match a shrunken run.")
        return 1
    print(f"{len(pinned & failed)} of {len(pinned)} pinned known defects still reproduce")

    if resolved:
        print("\nNO LONGER FAILING -- check the fix, then delete these from KNOWN_FAIL and "
              "re-anchor the matching mutant:")
        for name in resolved:
            print(f"  + {name}")
    if unexpected:
        print("\nUNEXPECTED FAILURES -- not pinned as known debt:")
        for name in unexpected:
            print(f"  ! {name}")

    if only_b:
        # NON-ZERO even when every pin matches. `--part b` skipped the mutation coverage
        # entirely, and an exit code is what automation reads; wave 9 returned 0 here, so a
        # quarter of the gate was indistinguishable from the gate.
        print("\nPART A SKIPPED (--part b): mutation coverage was NOT measured. This is not a "
              "gate result. Exiting 2.")
        return 2
    if not resolved and not unexpected:
        print("\nEvery outcome matches its pin: the known defects reproduce and nothing new "
              "broke.")
        print(f"This is NOT a clean bill of health -- it is {len(pinned)} known defects holding "
              "still. See KNOWN_FAIL.")
        return 0
    print("\nThe pinned set and the actual set disagree. Update the pins deliberately, or fix "
          "the regression.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
