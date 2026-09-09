#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp==1.28.1","httpx>=0.27","pydantic>=2","pyyaml>=6.0","truststore>=0.9",
#   "typing-extensions>=4.12",
# ]
# ///
"""Regenerate the derived half of `config/program-scope/<program>.yaml` from the live index.

    ./tools/regen_program_scope.py            # print the regenerated file, change nothing
    ./tools/regen_program_scope.py --write    # replace it

NEEDS A LIVE TOKEN. There is no offline mode on purpose: the whole reason this exists is that
the hand-written field lists were measured wrong, and a generator that could run without the
index would just be a second way to write them by hand.

**Why this file exists.** `02-muse-search-conditional.md` section 11 measured our own scope
YAML against each source's real mapping: **of 27 claimed `searchable_fields`, 19 do not
exist.** `indexed_text` exists on NO source and `date` exists on NO source, and a condition on
a nonexistent field returns 0 records at HTTP 200 -- indistinguishable from absence. Two more
recorded claims were also measured false: that a `.raw` field returns zero hits (it returns the
full population), and that facets are defined project-wide rather than per datasource.

So the file steered every reader toward silent zeros while looking authoritative.

**Two halves, and only one is generated.**

  AUTHORED   the program filter field and literal per source, `boundary_strength` and its
             caveat, `record_classes`, `display_name`, typed `identifiers`, and the
             scientific `known_limitations`. None of this is derivable from an index: a
             filter LITERAL is a binding decision, and `signals`' broad-boundary caveat is a
             measured scientific judgement about what the filter admits. Preserved verbatim.

  GENERATED  every field list, the computed facet set, KNN availability, full-text
             retrieval, the field roles that resolve, the taxonomy roles, and whether the
             program filter field is itself facetable. Overwritten from the live index.

**Derived from `warm_cache()`, not from fresh calls of its own.** The connector already makes
exactly these calls at startup -- 9 GET + 5 POST -- and reading its cache means the generated
file cannot describe a different index from the one the connector is using. A second
implementation of the same measurement is a second thing to be wrong.

It refuses to write from a degraded cache. A file half-measured and wholly authoritative-looking
is the failure mode being fixed here, so a partial regeneration is worse than none.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
WS = HERE.parent
SERVER = HERE / "ics_muse" / "server.py"

spec = importlib.util.spec_from_file_location("srv", SERVER)
m = importlib.util.module_from_spec(spec)
sys.modules["srv"] = m
spec.loader.exec_module(m)

# Keys that are AUTHORED per source. Anything else under a source is regenerated, so a key
# added to the YAML by hand and not listed here is dropped -- deliberately: a hand-written
# field list surviving a regeneration is the exact thing this script exists to stop.
AUTHORED_SOURCE_KEYS = (
    "display_name", "enabled", "program_filter_field", "program_filter_values",
    # AUTHORED, and why it is not generated is the point. A source whose programme boundary is a
    # folder path has no single stored literal, so its filter VALUES are re-derived by prefix
    # search at every startup rather than recorded here. What is authored is the PREFIX set --
    # a binding decision about which programme aliases name a folder. `kneat` is the first:
    # measured 2026-09-03, 70 folder terms begin with an alias, one per site and activity, and a
    # new site folder appears as a new term that a recorded list would silently miss. Where this
    # key is present, `program_filter_values` is deliberately empty in the file.
    "program_filter_value_prefixes",
    "boundary_strength", "caveat", "record_classes",
    # Per-source `known_limitations` are AUTHORED and survive regeneration. They carry
    # findings no index can supply -- that `mrl_slides`' filter value embeds a partner name,
    # that one study linked six slide records and no literature records, that a Trop2
    # exclusion was attempted and never actually verified. Dropping them as "not derived"
    # would lose measured science to a tidiness rule.
    "known_limitations",
)
AUTHORED_TOP_KEYS = (
    "program", "muse_project", "identifiers",
    # What is still open before this binding can be relied on for completeness. AUTHORED:
    # these are review and ownership questions, and no measurement closes them. The first
    # regeneration dropped them along with the old `STATUS: PROVISIONAL` header, which turned
    # "provisional and unreviewed" into "generated", i.e. into something that reads as
    # authoritative. Wave 7's own review gate caught that.
    "verification_open_items",
    "known_limitations",
)


# Every key `derive()` emits. Named once so `dropped_keys()` cannot drift out of step with it
# and start reporting a generated key as authored-and-dropped, or worse, stop reporting a real
# one. `derive()` asserts against this on every call.
GENERATED_SOURCE_KEYS = frozenset({
    "mapped_fields", "mapping_complete", "field_roles", "unfillable_roles", "taxonomy_roles",
    "searchable_fields", "searchable_fields_note", "facet_fields", "program_filter_facetable",
    "program_filter_facetable_note", "knn_available", "full_text_retrieval", "not_measured",
})


def _bare(name: str) -> str:
    return name[:-4] if name.endswith(".raw") else name


def derive(src: dict[str, Any]) -> dict[str, Any]:
    """The generated half for one source, from the startup cache's own measurement."""
    fields: dict[str, Any] = src.get("fields") or {}
    computed = src.get("computed_facets")
    # `None` is UNKNOWN throughout the cache and must stay UNKNOWN here. Rendering it as an
    # empty list would assert "this source computes no facets", which is a different claim and
    # is the poison rule's predicate -- the one thing that must never be guessed.
    if computed is None:
        facet_fields: Any = None
    else:
        facet_fields = sorted({_bare(str(f)) for f in computed} & set(fields))

    role_targets: set[str] = set()
    for targets in (src.get("roles") or {}).values():
        role_targets.update(t for t in targets if t in fields)

    # Every name here is present in `mappings.document.properties`, checked against `fields`
    # rather than assumed. That is the whole difference from the hand-written list.
    #
    # UNKNOWN propagates. `facet_fields or ()` would have quietly turned an unmeasured facet
    # set into "role targets only", indistinguishable from a measured result -- the project's
    # own recorded `x or {}` failure. Today no path reaches here with `computed_facets` None,
    # because every one calls `fail()` and `main()` refuses on any degraded entry; that is
    # coupling, not a local guarantee, and `derive()` has to be safe on its own terms.
    if facet_fields is None:
        searchable: Any = None
        searchable_note: str | None = (
            "UNKNOWN, not empty. The facet half of this list could not be measured, so the "
            "list is not reported rather than reported short.")
    else:
        searchable = sorted(role_targets | set(facet_fields))
        searchable_note = None

    out: dict[str, Any] = {
        "mapped_fields": len(fields),
        "mapping_complete": src.get("mapping_complete"),
        "field_roles": {r: sorted(t) for r, t in sorted((src.get("roles") or {}).items())},
        "unfillable_roles": sorted(src.get("unfillable_roles") or []),
        "taxonomy_roles": sorted(src.get("taxonomy_roles") or {}),
        "searchable_fields": searchable,
        "facet_fields": facet_fields,
        "program_filter_facetable": src.get("facetable_program_filter"),
        "knn_available": src.get("knn"),
        "full_text_retrieval": src.get("readable"),
    }
    if searchable_note:
        out["searchable_fields_note"] = searchable_note
    if src.get("facetable_program_filter") is False:
        out["program_filter_facetable_note"] = (
            "The poison rule: this source's program filter field is not one the facets "
            "endpoint computes a facet for, so filtering on it zeroes the whole "
            "distribution. A distribution request here is answered over a query-scoped "
            "superset with both counts reported, or refused with the reason -- never as one "
            "sparse-looking field.")
    # A key emitted here but absent from GENERATED_SOURCE_KEYS would be reported by
    # `dropped_keys()` as authored content about to be lost -- a false alarm that trains the
    # reader to ignore a real one.
    unlisted = set(out) - GENERATED_SOURCE_KEYS
    if unlisted:
        raise AssertionError(
            f"derive() emits {sorted(unlisted)}, which GENERATED_SOURCE_KEYS does not list")
    return out


def dropped_keys(existing: dict[str, Any]) -> list[str]:
    """Authored keys this regeneration would NOT carry forward.

    The doctrine at the top of this file is that authored content is preserved verbatim and
    that a partial regeneration is worse than none. Copying only listed keys quietly violated
    both: the first run dropped `mapping_version`, `validated_at`, `validation_basis`,
    `verification_status` and every source's `search_modes` with nothing said. Nothing read
    them, so nothing broke -- but `verification_status: provisional` was a hedge that vanished
    without a replacement, and the next authored key added by hand would go the same way.
    """
    out = sorted(set(existing) - set(AUTHORED_TOP_KEYS) - {"sources", "generated_from"})
    out = [f"(top) {k}" for k in out]
    for name, cfg in (existing.get("sources") or {}).items():
        if not isinstance(cfg, dict):
            continue
        extra = sorted(set(cfg) - set(AUTHORED_SOURCE_KEYS) - GENERATED_SOURCE_KEYS)
        out += [f"{name}.{k}" for k in extra]
    return out


def build(existing: dict[str, Any], cache: dict[str, Any], *, stamp: str) -> str:
    src_cache = cache["sources"]
    authored_sources = existing.get("sources") or {}

    doc: dict[str, Any] = {}
    for k in AUTHORED_TOP_KEYS:
        if k in existing:
            doc[k] = existing[k]
    doc["generated_from"] = {
        "index_fingerprint": cache.get("config_fingerprint"),
        "sources_measured": sorted(src_cache),
        "index_dates": {n: s["freshness"].get("date") for n, s in sorted(src_cache.items())},
        "regenerate_with": "./tools/regen_program_scope.py --write",
    }

    doc["sources"] = {}
    for name, authored in authored_sources.items():
        entry = {k: authored[k] for k in AUTHORED_SOURCE_KEYS if k in authored}
        if name in src_cache:
            entry.update(derive(src_cache[name]))
        else:
            # In the YAML and not in scope. Say so rather than emitting a source with no
            # measurement and no marker, which would read as "measured, and empty".
            entry["not_measured"] = (
                "this source is in the scope file but was not in the connector's in-scope "
                "set when this file was generated, so nothing below it is measured")
        doc["sources"][name] = entry

    unauthored = sorted(set(src_cache) - set(authored_sources))
    if unauthored:
        doc["measured_but_not_in_this_file"] = unauthored

    header = f"""\
# Program scope mapping -- {doc.get('program', '?')}
#
# PARTLY GENERATED. Do not hand-edit anything below a source's `record_classes`.
#   regenerate:  ./tools/regen_program_scope.py --write
#   generated:   {stamp}
#
# Every field list here was read from each source's real ingest mapping
# (`mappings.document.properties`, `dynamic: strict`) and each source's own unfiltered facet
# response, through the connector's startup cache -- so this file and the running connector
# cannot disagree about the index.
#
# WHY IT IS GENERATED. The hand-written version claimed 27 `searchable_fields` of which
# **19 did not exist**; `indexed_text` and `date` existed on no source at all. A condition on
# a nonexistent field returns 0 records at HTTP 200, indistinguishable from absence, so the
# file steered readers toward silent zeros while looking authoritative.
#
# WHAT IS STILL AUTHORED, and is not derivable from any index: the program filter field and
# literal per source, `boundary_strength` and its caveat, `record_classes`, `display_name`,
# typed `identifiers`, and the scientific `known_limitations`.
#
# STILL PROVISIONAL AND NOT PRODUCTION-OWNED OR REVIEWED. Generating the field lists closed
# some of the original open items and none of the ownership ones -- see `verification_open_items`.
# Being generated is not being reviewed, and the first regeneration of this file lost that
# distinction by dropping the old `STATUS: PROVISIONAL` header.
#
# `null` means UNKNOWN, never "none" -- an unmeasured facet set and a source that computes no
# facets are different facts, and only the second may be relied on.
#
# The connector reads only `muse_project`, and per source `enabled`,
# `program_filter_field`, `program_filter_values` and `boundary_strength`. Everything else is
# orientation for a human reader and for `tools/muse_contract_check.py`. The authoritative
# field list is always the live mapping, never this file.
"""
    body = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, width=96,
                          allow_unicode=True)
    return header + "\n" + body


async def main() -> int:
    write = "--write" in sys.argv
    positional = [a for a in sys.argv[1:] if not a.startswith("--")]
    # `ICS_PROGRAM` is how the connector is configured, so honour it -- but it is unset in an
    # ordinary shell, and an empty program silently resolved to `.yaml`, i.e. no file. Falling
    # back to the single scope file on disk keeps the documented invocation working, and a
    # positional argument overrides both.
    program = (positional[0] if positional else (getattr(m, "PROGRAM", "") or "").strip())
    scope_dir = WS / "config" / "program-scope"
    if not program:
        candidates = sorted(scope_dir.glob("*.yaml"))
        if len(candidates) != 1:
            print(f"set ICS_PROGRAM or name the program: {scope_dir} holds "
                  f"{[c.stem for c in candidates]}", file=sys.stderr)
            return 1
        program = candidates[0].stem
        print(f"no ICS_PROGRAM set; using the only scope file present: {program}",
              file=sys.stderr)

    path = scope_dir / f"{program}.yaml"
    if not path.exists():
        print(f"no scope file at {path}", file=sys.stderr)
        return 1
    existing = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    # PRESENT BUT MALFORMED is not the same as absent, and `path.exists()` cannot tell them
    # apart. With `sources:` empty, null, or not a mapping, `build()` iterated nothing and
    # `--write` produced a scope file with ZERO sources -- while the success line reported
    # "wrote ... 5 sources" from the CACHE's count. A connector reading that file would find no
    # source program-scopeable and report retrieval unavailable, from a run that said it
    # succeeded.
    if not isinstance(existing.get("sources"), dict) or not existing["sources"]:
        print(f"REFUSING: {path} has no usable `sources:` mapping. This script regenerates the "
              "derived half of an existing binding; it does not author one.", file=sys.stderr)
        return 1

    lost = dropped_keys(existing)
    if lost:
        print("REFUSING to write: these authored keys would not be carried forward, and this "
              "script's own doctrine is that authored content is preserved verbatim. Either add "
              "them to AUTHORED_TOP_KEYS / AUTHORED_SOURCE_KEYS, or delete them deliberately "
              "first:", file=sys.stderr)
        for k in lost:
            print(f"  {k}", file=sys.stderr)
        return 1

    cache = await m.warm_cache(program)
    if not cache["ok"] or cache["degraded"]:
        print("REFUSING to generate: the startup cache is degraded, so some of what would be "
              "written was not measured. A half-measured file that looks authoritative is the "
              "failure this script exists to fix.", file=sys.stderr)
        for d in cache["degraded"]:
            print(f"  {'FATAL' if d['fatal'] else 'degraded'} {d['call']}: {d['reason']}",
                  file=sys.stderr)
        return 1

    # Stamped from the INDEX, not from the wall clock: what dates this file is the state of the
    # index it describes, and an index date is a fact the run can cite.
    dates = [s["freshness"].get("date") for s in cache["sources"].values()
             if s["freshness"].get("date")]
    stamp = f"against index state {min(dates)} .. {max(dates)}" if dates \
        else "index dates unavailable"

    out = build(existing, cache, stamp=stamp)

    # Never regenerate away the binding the connector actually reads. A YAML round-trip is the
    # realistic way one of these could change -- an unquoted value re-parsing as a different
    # type -- rather than `build()` rewriting it, since `derive()` emits none of these keys.
    check = yaml.safe_load(out) or {}
    for name, cfg in existing["sources"].items():
        new = (check.get("sources") or {}).get(name) or {}
        for key in ("program_filter_field", "program_filter_values", "boundary_strength",
                    "enabled"):
            if key in cfg and new.get(key) != cfg[key]:
                print(f"REFUSING to write: {name}.{key} does not survive the round trip -- "
                      f"{cfg[key]!r} became {new.get(key)!r}. That is an authored binding the "
                      "connector reads, not a derived value.", file=sys.stderr)
                return 1

    # Against the MEASURED set, not against `build()`'s output. Comparing the output was
    # structurally always false: `build()` constructs `doc["sources"]` by iterating
    # `existing["sources"]`, so the two key sets are equal by construction and "the source list
    # would change" could never print. What actually matters is scope drift -- a source the
    # connector no longer serves, or one it now serves that this file does not bind.
    only_in_file = sorted(set(existing["sources"]) - set(cache["sources"]))
    only_measured = sorted(set(cache["sources"]) - set(existing["sources"]))
    if only_in_file or only_measured:
        print("The scope file and the connector's in-scope set disagree. Writing anyway, with "
              "each side marked in the file -- but this is a binding question, not a "
              "measurement one, and it needs an authored decision:", file=sys.stderr)
        for s in only_in_file:
            print(f"  in this file, NOT served by the connector: {s}", file=sys.stderr)
        for s in only_measured:
            print(f"  served by the connector, NOT bound here:   {s}", file=sys.stderr)

    written = len(check.get("sources") or {})
    if not write:
        print(out)
        print(f"--- dry run. {written} sources would be written, {len(cache['sources'])} "
              f"measured, {m._calls_used} calls. Pass --write to replace the file. ---",
              file=sys.stderr)
        return 0

    path.write_text(out, encoding="utf-8")
    # `written`, not the cache's count. Reporting the cache's meant a file containing zero
    # sources could be announced as "wrote 5 sources".
    print(f"wrote {path} -- {written} sources, {stamp}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
