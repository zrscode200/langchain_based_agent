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
# Pins mirror `ics_muse/server.py`, because this execs that module.
"""Assert handles carry their recipe, and refuse rather than half-decode.

    ./tools/handle_check.py

NO NETWORK AND NO TOKEN.

WHAT A HANDLE IS FOR
--------------------
Restating a population is where this API's measured failures come from. On meds, four
restatements of the same intent return **135 / 3,832 / 1,014 / ~5.9 million** records —
every one HTTP 200, every one plausible, and nothing in any response says which
population it described. A handle carries the recipe so there is nothing to restate.

TWO THINGS THIS FILE LEARNED THE HARD WAY
-----------------------------------------
The wave-2 review gate ran mutation testing against an earlier version of this script and
found two assertions that passed while testing nothing:

* a "no refusal leaks the recipe" check whose six inputs all failed *before* the payload
  was decoded — so no message could have contained a recipe. Making a refusal echo the
  whole payload still gave 45/45 green. It is now asserted on the three scope refusals,
  which are the only messages holding a decoded payload.
* a "the fresh process holds no handle state" check that inspected the *predecessor*
  `_handles` dict on a module that had never encoded anything. Reintroducing process
  state inside `_handle_encode` still gave 45/45. It now snapshots the ENCODING module.

Both were the same defect wave 1's gate had already found in `startup_cache_check.py`
("the PII check ran against a stub shape that structurally could not leak"). An assertion
that cannot fail is worse than a missing one, because it reads as coverage.

The cache is built by stubbing `_request` and letting the real `warm_cache` run — the same
pattern as `startup_cache_check.py`, and for a specific reason: a hand-built `_CACHE`
literal structurally could not express `never_indexed` / `unread` / `absent`, and that is
exactly why the gate's finding about collapsed freshness states was invisible here.

Exit status is 0 only if every assertion holds.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import zlib
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SERVER = HERE / "ics_muse" / "server.py"
sys.path.insert(0, str(HERE))
from startup_cache_check import (  # noqa: E402  - reuse wave 1's stubs, don't fork them
    IN_SCOPE, UPDATES_BODY, install_stubs,
)

FAILURES: list[str] = []
CHECKS = 0


def load_server(name: str = "ics_muse_server"):
    """Load a FRESH module instance. Two instances model a process restart."""
    spec = importlib.util.spec_from_file_location(name, SERVER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def check(label: str, ok: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{': ' + detail if detail else ''}")
    if not ok:
        FAILURES.append(label)


def warm(m, **kw) -> dict[str, Any]:
    """Build a REAL cache through `warm_cache`, so its freshness states are real."""
    m._CACHE = None
    m._calls_used = 0
    m._started = m.time.monotonic()
    install_stubs(m, **kw)
    import asyncio
    return asyncio.run(m.warm_cache("MK-6070"))


RECIPE: dict[str, Any] = dict(
    source="med_comms",
    engine="conditional",
    program_filter={"primary_mkv_number1.raw": ["MK-6070"]},
    conditions=[{"in": ["anywhere"], "any_of": ["MK-6070"], "match": "phrase"}],
    count=19,
    facetable=True,
    index_updated="2026-08-25T05:35:52",
)


_B64 = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def peek(handle: str) -> dict[str, Any]:
    """Decode a handle without the connector's validation, to inspect the raw payload.

    Out-of-alphabet characters are filtered BEFORE padding is computed, because that is
    what `base64.urlsafe_b64decode` does in its default non-strict mode — silently
    discarding them. Reproducing that is the whole point of the injection assertion below:
    the junk vanishes and the ORIGINAL recipe comes back.
    """
    body = "".join(c for c in handle.split(".")[1] if c in _B64)
    return json.loads(zlib.decompress(
        base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))))


def main() -> int:  # noqa: PLR0915 - a check script is a list of checks
    m = load_server()
    warm(m)

    print("1. a population handle round-trips its whole recipe")
    h, err = m.encode_population_handle(**RECIPE)
    check("mints without error", err is None and h, str(err))
    p, err = m.decode_population_handle(h)
    check("decodes without error", err is None, str(err))
    check("field names are the DESIGN's, not shortened ones",
          set(peek(h)) == set(m._POPULATION_KEYS),
          "00-surfaced-tools.md §4 — shortening them saved 19% and is an economy argument")
    for key, want in [("source", "med_comms"), ("engine", "conditional"),
                      ("program_filter", RECIPE["program_filter"]),
                      ("conditions", RECIPE["conditions"]), ("count", 19),
                      ("facetable", True), ("index_updated", RECIPE["index_updated"])]:
        check(f"`{key}` survives verbatim", p and p[key] == want, repr(p and p.get(key)))
    check("as_of is stamped", bool(p and p.get("as_of")))
    check("decoding reports that it WAS validated against the live cache",
          p and p.get("_validated") is True,
          "(payload, None) alone cannot distinguish validated from unvalidatable")

    print("\n2. opaque, versioned, and compact enough to hand around")
    check("version prefix present", h.startswith("p1."), h[:3])
    check("no recipe content readable in the handle",
          "med_comms" not in h and "MK-6070" not in h and "conditional" not in h)
    check("length workable", len(h) < 500, f"{len(h)} chars")

    print("\n3. NO PROCESS STATE — this is the whole point")
    m2 = load_server("ics_muse_server_restarted")
    m2._CACHE = None
    p2, err2 = m2.decode_population_handle(h)
    check("a handle minted by one process decodes in a fresh one, unwarmed",
          err2 is None and p2 and p2["source"] == "med_comms", str(err2))
    # Snapshot the ENCODING module. The earlier version inspected the fresh module's
    # `_handles`, which is empty by construction — reintroducing state in `_handle_encode`
    # passed anyway.
    before = dict(getattr(m, "_handles", {}))
    m.encode_population_handle(**RECIPE)
    m.encode_document_handle("meds", "CLS-1")
    check("minting stores NOTHING in the encoding process",
          dict(getattr(m, "_handles", {})) == before,
          "mutation-tested: adding `_handles[out] = payload` to _handle_encode must fail here")

    print("\n=== SABOTAGE: every way a handle could describe a DIFFERENT population ===")

    print("\n4. corruption of the BODY, not just the checksum")
    body = h.split(".")[1]
    # The earlier version's two headline cases (`h[:-4]` and a borrowed checksum) leave the
    # body byte-identical, so they exercised only the checksum layer and could not
    # demonstrate the failure the wave claimed. These mutate the body.
    flipped = "p1." + body[:20] + ("A" if body[20] != "A" else "B") + body[21:]
    flipped += "." + m.hashlib.sha256(flipped.encode()).hexdigest()[:m._HANDLE_CHECK_LEN]
    pf, ef = m.decode_population_handle(flipped)
    check("body byte flipped AND checksum recomputed -> still refused",
          pf is None, (ef or "")[:70])
    check("  ...and it is zlib/JSON that refuses, not the checksum",
          "could not be decoded" in (ef or ""),
          "measured by the gate: 0 of 14,742 body substitutions decoded to a "
          "different recipe")
    # What the checksum measurably DOES add: base64 silently discards out-of-alphabet
    # characters, so this decodes cleanly to the ORIGINAL recipe without it.
    injected = f"p1.{body[:20]}*!{body[20:]}.{h.split('.')[2]}"
    pi, ei = m.decode_population_handle(injected)
    check("non-base64 characters injected mid-body -> refused BY THE CHECKSUM",
          pi is None and "checksum" in (ei or ""),
          "base64.urlsafe_b64decode would otherwise discard them and decode the original")
    check("  ...proving the checksum must cover the STRING, before any decode",
          peek(f"p1.{body[:20]}*!{body[20:]}.x") == peek(h),
          "the injected body decodes to the identical recipe if you skip the checksum")

    print("\n5. other malformed inputs fail closed")
    for label, bad in [("truncated", h[:-4]), ("borrowed checksum", h.rsplit(".", 1)[0] + ".0123456789"),
                       ("invented", "p1.eyJzIjoibWVkcyJ9.0123456789"), ("empty", ""),
                       ("not a string", 12345), ("a bare card id", "CLS-12345-A"),
                       ("two parts", "p1.abcdef")]:
        pp, ee = m.decode_population_handle(bad)
        check(f"{label} -> refused", pp is None and bool(ee), (ee or "")[:56])

    print("\n6. a compression bomb is refused BEFORE it is materialised")
    bomb_raw = zlib.compress(json.dumps({"source": "x" * 300_000}).encode(), 9)
    bomb_body = base64.urlsafe_b64encode(bomb_raw).decode().rstrip("=")
    bomb = f"p1.{bomb_body}.{m.hashlib.sha256(f'p1.{bomb_body}'.encode()).hexdigest()[:10]}"
    pb, eb = m.decode_population_handle(bomb)
    check("an oversized handle is refused on LENGTH, before decompressing",
          pb is None and "refusing to decode" in (eb or ""),
          f"{len(bomb)} chars; deflate reaches ~1032:1, so 1 MB in is ~1 GB of RSS")
    small = json.dumps({"source": "y" * 200_000}).encode()
    sb = base64.urlsafe_b64encode(zlib.compress(small, 9)).decode().rstrip("=")
    if len(sb) + 14 <= m._HANDLE_MAX_ENCODED:
        hh = f"p1.{sb}.{m.hashlib.sha256(f'p1.{sb}'.encode()).hexdigest()[:10]}"
        ps, es = m.decode_population_handle(hh)
        check("a short handle that inflates past the bound is also refused",
              ps is None, (es or "")[:60])

    print("\n7. the two kinds, and an unknown format tag, are distinguished")
    d, derr = m.encode_document_handle("meds", "CLS-999-A")
    check("a document handle mints", derr is None and d)
    pp, ee = m.decode_population_handle(d)
    check("a document handle is not accepted as a population handle",
          pp is None and "wrong handle kind" in (ee or ""), (ee or "")[:64])
    pp, ee = m.decode_document_handle(h)
    check("and the reverse", pp is None and "wrong handle kind" in (ee or ""))
    pp, ee = m.decode_population_handle("p9." + body + "." + h.split(".")[2])
    check("an unknown format tag says SO, rather than 'wrong kind'",
          pp is None and "format tag" in (ee or ""), (ee or "")[:64])

    print("\n8. a malformed payload is refused, not read")
    for bad, why in [({**peek(h), "source": None}, "source null"),
                     ({**peek(h), "source": ""}, "source empty"),
                     ({**peek(h), "source": 5}, "source not a string"),
                     ({**peek(h), "source": []}, "source a list — used to raise TypeError"),
                     ({**peek(h), "engine": None}, "engine null"),
                     ({**peek(h), "program_filter": "MK-6070"}, "filter a string"),
                     ({k: v for k, v in peek(h).items() if k != "count"}, "count missing")]:
        bh, _ = m._handle_encode(m._HANDLE_POPULATION, bad)
        pp, ee = m.decode_population_handle(bh)
        check(f"{why} -> refused", pp is None and bool(ee), (ee or "")[:56])

    print("\n9. a population that no longer exists as described is REFUSED")
    scope_msgs = []
    warm(m)
    m._CACHE["sources"]["med_comms"]["searchable"] = False
    m._CACHE["sources"]["med_comms"]["access"] = "DENIED"
    pp, ee = m.decode_population_handle(h)
    scope_msgs.append(ee)
    check("source lost access -> refused, saying why a zero would lie",
          pp is None and "not the same as finding none" in (ee or ""), (ee or "")[:70])
    warm(m)
    m._CACHE["sources"]["med_comms"]["program_filter"] = {"primary_mkv_number1.raw": ["MK-9999"]}
    pp, ee = m.decode_population_handle(h)
    scope_msgs.append(ee)
    check("program boundary changed -> refused", pp is None and "program boundary" in (ee or ""))
    warm(m)
    m._CACHE["sources"]["med_comms"]["program_filter"] = {}
    pp, ee = m.decode_population_handle(h)
    check("an EMPTY configured filter still triggers the check, not a truthiness skip",
          pp is None and "program boundary" in (ee or ""), (ee or "")[:60])
    warm(m)
    del m._CACHE["sources"]["med_comms"]
    pp, ee = m.decode_population_handle(h)
    scope_msgs.append(ee)
    check("source left scope -> refused", pp is None and "no longer" in (ee or ""))

    print("\n10. the scope checks do NOT depend on cache['ok']")
    warm(m)
    # An ingest 403 or budget exhaustion condemns the cache without being transient, so
    # it persists with ok False — and `access` still came from a 200.
    m._CACHE["ok"] = False
    m._CACHE["sources"]["med_comms"]["searchable"] = False
    m._CACHE["sources"]["med_comms"]["access"] = "DENIED"
    pp, ee = m.decode_population_handle(h)
    check("a DENIED source is refused even when cache['ok'] is False",
          pp is None and "no longer accessible" in (ee or ""),
          "gating on `ok` meant ignoring `access` exactly where it was trustworthy")

    print("\n11. a cold cache decodes but SAYS it could not validate")
    m._CACHE = None
    pp, ee = m.decode_population_handle(h)
    check("decodes", ee is None, str(ee))
    check("_validated is False, not absent", pp and pp.get("_validated") is False)
    check("and it says what was not checked",
          pp and "could NOT be re-checked" in pp.get("_validation_note", ""))

    print("\n12. STALENESS: three directions, and which unknown")
    warm(m)
    p, _ = m.decode_population_handle(h)
    s = m.population_staleness(p)
    check("index unmoved -> stale False, moved 'unchanged'",
          s["stale"] is False and s["moved"] == "unchanged", s["detail"][:50])
    m._CACHE["sources"]["med_comms"]["freshness"]["date"] = "2026-08-26T01:00:00"
    s = m.population_staleness(p)
    check("index forward -> stale True, moved 'forward'",
          s["stale"] is True and s["moved"] == "forward")
    m._CACHE["sources"]["med_comms"]["freshness"]["date"] = "2026-08-01T00:00:00"
    s = m.population_staleness(p)
    check("index BACKWARDS -> stale True, moved 'backwards' — not 'has not moved'",
          s["stale"] is True and s["moved"] == "backwards", s["detail"][:60])
    check("both dates always reported", s["minted_index_date"] and s["current_index_date"])

    print("\n13. the three freshness unknowns stay distinct")
    warm(m)                                        # signals is absent from UPDATES_BODY
    hs, _ = m.encode_population_handle(**{**RECIPE, "source": "signals",
                                          "program_filter": m._CACHE["sources"]["signals"]["program_filter"]})
    ps, _ = m.decode_population_handle(hs)
    s = m.population_staleness(ps)
    check("no data-updates ENTRY -> named as such",
          s["stale"] is None and "no entry in data-updates" in s["detail"], s["detail"][:60])
    warm(m, fail={"data-updates": "timeout"})
    ph, _ = m.decode_population_handle(h)
    s = m.population_staleness(ph)
    check("freshness never READ -> named differently",
          s["stale"] is None and "never read this session" in s["detail"], s["detail"][:60])
    warm(m)
    m._CACHE["sources"]["med_comms"]["freshness"] = {
        "date": None, "never_indexed": True, "unread": False, "absent": False, "kind": "index"}
    s = m.population_staleness(p)
    check("NEVER INDEXED -> named, and called stronger than unknown",
          s["stale"] is None and "never been indexed" in s["detail"], s["detail"][:60])

    print("\n14. staleness never raises, whatever a handle carries")
    warm(m)
    for bad, why in [({**peek(h), "index_updated": 1756100000}, "an int"),
                     ({**peek(h), "index_updated": True}, "a bool"),
                     ({**peek(h), "index_updated": "2026-08-25"}, "date-only"),
                     ({**peek(h), "index_updated": "25/08/2026 05:35"}, "a foreign format"),
                     ({**peek(h), "source": []}, "a list source")]:
        try:
            s = m.population_staleness(bad)
            ok = s["stale"] is None
        except Exception as exc:  # noqa: BLE001
            s, ok = {"detail": f"RAISED {type(exc).__name__}"}, False
        check(f"index_updated {why} -> stale None, no raise", ok, str(s.get("detail"))[:56])

    print("\n15. mint-side refusals, so a bad handle never reaches the model")
    for kw, why in [({"index_updated": "2026-08-25"}, "a date-only index date"),
                    ({"index_updated": "2026-08-25T05:35:52Z"}, "a trailing Z, like as_of"),
                    ({"source": ""}, "no source"), ({"engine": ""}, "no engine")]:
        hh, ee = m.encode_population_handle(**{**RECIPE, **kw})
        check(f"minting refuses {why}", hh is None and bool(ee), (ee or "")[:56])
    for args, why in [(("meds", ""), "an empty card.id"), (("", "CLS-1"), "no datasource"),
                      (("meds", None), "a null card.id")]:
        hh, ee = m.encode_document_handle(*args)
        check(f"minting a document handle refuses {why}", hh is None and bool(ee),
              (ee or "")[:56])
    bad_filter, ee = m.encode_population_handle(
        **{**RECIPE, "program_filter": {"x.raw": [object()]}})
    check("an unencodable recipe is refused at mint, not raised",
          bad_filter is None and "cannot be encoded" in (ee or ""), (ee or "")[:56])

    print("\n16. document handles keep card.id out of the model's hands")
    d, _ = m.encode_document_handle("meds", "CLS-0123456-A")
    check("the id is not readable in the handle", "CLS-0123456-A" not in d)
    pd, ed = m.decode_document_handle(d)
    check("it round-trips", ed is None and pd["datasource"] == "meds"
          and pd["id"] == "CLS-0123456-A")
    check("obfuscated, NOT protected — one line recovers it",
          peek(d)["id"] == "CLS-0123456-A",
          "asserted so nothing later mistakes this for a confidentiality boundary")
    for bad, why in [({"datasource": "meds", "id": ["a"]}, "a list id"),
                     ({"datasource": 5, "id": "x"}, "a non-string datasource")]:
        bh, _ = m._handle_encode(m._HANDLE_DOCUMENT, bad)
        pd2, ed2 = m.decode_document_handle(bh)
        check(f"a well-formed handle with {why} is refused", pd2 is None, (ed2 or "")[:50])

    print("\n17. the cold path: a document handle decodes with NO scope at all")
    m._CACHE = None
    check("no cache -> decodes", m.decode_document_handle(d)[1] is None)
    warm(m)
    del m._CACHE["sources"]["meds"]
    check("source outside the current scope -> STILL decodes",
          m.decode_document_handle(d)[1] is None,
          "muse_read's cold path reads refs from ground/ recorded in earlier sessions")

    print("\n18. no refusal leaks the recipe — asserted on the messages that HOLD one")
    # The earlier version checked six inputs that all failed before decoding, so no
    # message could have contained a recipe. Mutation-tested: echoing the payload still
    # passed. These three are the only refusals that run AFTER a successful decode.
    check("three scope refusals were captured", len([x for x in scope_msgs if x]) == 3,
          str(len(scope_msgs)))
    check("none names the program filter literal",
          not any("MK-6070" in (msg or "") or "MK-9999" in (msg or "")
                  for msg in scope_msgs),
          "mutation-tested: echoing payload!r here must fail this")
    check("none names the condition text the model supplied",
          not any("anywhere" in (msg or "") for msg in scope_msgs))
    check("every refusal still explains itself",
          all(msg and len(msg) > 40 for msg in scope_msgs),
          f"shortest {min(len(x or '') for x in scope_msgs)} chars")

    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} assertions passed")
    if FAILURES:
        print("FAILED: " + ", ".join(FAILURES))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
