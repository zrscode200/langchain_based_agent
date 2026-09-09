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
# The pins mirror `ics_muse/server.py` exactly. They are needed because `load_server()`
# execs that module, so its imports must resolve. Without this block the documented
# invocation exits 1 with ModuleNotFoundError and ZERO assertion output -- which reads
# as "assertions failed" to anything scripting it.
"""Assert the startup cache builds what it claims, and degrades when it cannot.

    ./tools/startup_cache_check.py          # or: uv run --script tools/startup_cache_check.py

NO NETWORK AND NO SESSION. Every response is stubbed from shapes measured live on
2026-08-25 (`<hub>/work/design/model-facing-tools/07-startup-cache-endpoints.md`).

WHY STUBBED RATHER THAN LIVE
----------------------------
Two reasons, and the second is the important one.

1. Authentication availability varies. A check that requires a live SESSION cannot
   be run on demand, so it is not a check anyone will always run.

2. **The cases that matter cannot be provoked live.** A source cannot be made to
   404 on request; the index cannot be made to report `dynamic: dynamic`; no
   in-scope source currently carries an epoch-zero index date or is missing from
   `data-updates`. Those are exactly the branches where a silent wrong answer
   would be produced, so they are exactly the branches that need asserting.

EVERY ASSERTION TESTS AN EFFECT, not that a request was accepted. This API returns
HTTP 200 with the intent silently discarded in at least 17 measured ways, so a
status code establishes nothing.

Exit status is 0 only if every assertion holds.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import sys
from pathlib import Path
from typing import Any
import os
import pathlib

# PINNED SCOPE FIXTURE. These checks assert connector BEHAVIOUR, so they must not read the
# production `config/program-scope/` -- when MK-6070 gained `mmdkx` and `teamspace` on
# 2026-09-03 all five MUSE harnesses died with `KeyError` from their own stub mapping tables,
# which are keyed by source name. The failure was in the coupling, not the scope change: a
# harness that reads live config re-fails on every future source decision. `_scope_dir()`
# reads this at call time, and `contract_map_check.py`'s `SCRUBBED_ENV` does not strip it, so
# setting it here works for direct execution and through the suite runner alike.
os.environ.setdefault(
    "ICS_PROGRAM_SCOPE_DIR",
    str(pathlib.Path(__file__).resolve().parent.parent / "evals" / "fixtures" / "program-scope"),
)

HERE = Path(__file__).resolve().parent
SERVER = HERE / "ics_muse" / "server.py"


def load_server():
    spec = importlib.util.spec_from_file_location("ics_muse_server", SERVER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ics_muse_server"] = mod
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------
# Stub responses, shaped from live measurement
# --------------------------------------------------------------------------
IN_SCOPE = ["meds", "med_comms", "scited", "mrl_slides", "signals"]

# Two of the ten meds taxonomy branches are enough to prove the mechanism.
BRANCH_ISSUE = "aaaaaaaa-0000-0000-0000-000000000001"
BRANCH_METHOD = "bbbbbbbb-0000-0000-0000-000000000002"
NODE_OOS = "cccccccc-0000-0000-0000-000000000003"
NODE_MICRO = "dddddddd-0000-0000-0000-000000000004"

TAXONOMY_BODY = {
    "root": {
        "id": "root", "name": "root",
        "children": {
            # Note: the branch LABEL deliberately does not match its field name.
            # Four of the ten real branches are like this, which is why field-to-branch
            # must be matched by root UUID.
            "Topics and Issues": {
                "id": BRANCH_ISSUE, "name": "Topics and Issues",
                "children": {
                    "Testing issue": {
                        "id": "eeeeeeee-0000-0000-0000-000000000005",
                        "name": "Testing issue",
                        "children": {
                            "Out of Specification": {
                                "id": NODE_OOS, "name": "Out of Specification",
                                "synonyms": ["OOS", "out-of-spec"],
                            }
                        },
                    }
                },
            },
            "Analytical Method": {
                "id": BRANCH_METHOD, "name": "Analytical Method",
                "children": {
                    "Microbial testing": {"id": NODE_MICRO, "name": "Microbial testing"}
                },
            },
        },
    }
}

CONFIG_BODY: dict[str, Any] = {
    "categories": [], "chatRetrievalMode": "pull", "infopanelRetrievalMode": "pull",
    "properties": {"search": {"cardsPerPage": 50, "maxCardsPerSearch": 1000}},
    "customization": {
        "projectKnnSettings": {
            "embedding_model": "text-embedding-3-small",
            "long_text_vectorized_fields": ["text"],
            "dimension": 384,
            # scited/mrl_slides/signals disabled, meds/med_comms enabled — as measured.
            "knn_disabled_datasources": ["scited", "mrl_slides", "signals", "radr_x"],
        }
    },
    "facets": [
        {"id": "doc_status", "label": "Status", "type": "SEARCH", "primary": False},
        {"id": "restricted", "label": "Access", "type": "SIMPLE", "primary": True,
         "valueLabels": {"Accessible": "Full Access", "Restricted": "Limited Access"}},
        {"id": "creation_date", "label": "Created", "type": "DATE", "primary": True},
    ],
    "dataSources": [
        {"id": "meds", "title": "MEDS", "category": "DOCUMENTS", "hidden": False,
         "fullDocumentTextRetrievalEnabled": True, "maxExportResults": 10000,
         "facets": ["doc_status.raw", "restricted.raw", "creation_date.raw"],
         "advancedSearchModel": {"fields": []},
         # `cardModel.fields` entries are OBJECTS live -- `{id, label, showInSummary,
         # startsNewSection}` -- and every `id` resolves in the mapping (21/21 on meds,
         # measured 2026-08-25). An earlier version of this stub used bare strings, which is
         # a shape that does not exist, and it hid a filter that admitted none of them.
         "cardModel": {"link": "{webview_pdf_url}", "fields": [
             {"id": "doc_status", "label": "Status", "showInSummary": True,
              "startsNewSection": False},
             {"id": "product_name", "label": "Product", "showInSummary": True,
              "startsNewSection": False}]}},
        {"id": "med_comms", "title": "Medical Communications", "category": "DOCUMENTS",
         "hidden": False, "fullDocumentTextRetrievalEnabled": True,
         "maxExportResults": 10000, "facets": ["doc_status.raw"],
         "cardModel": {"link": "/resources/attachments/v1/DO-NOT-SHARE-THIS-URL/{title}"
                               "?datasource=med_comms&documentId={muse_raw_id}",
                       "fields": [{"id": "activity_status", "label": "Status",
                                   "showInSummary": True, "startsNewSection": False}]}},
        {"id": "scited", "title": "SciTed", "category": "REFERENCE", "hidden": False,
         "fullDocumentTextRetrievalEnabled": True, "maxExportResults": 10000,
         "facets": ["author.raw"], "cardModel": {"link": "{link}", "fields": ["title"]}},
        {"id": "mrl_slides", "title": "MRL Slides", "category": "REFERENCE",
         "hidden": False, "fullDocumentTextRetrievalEnabled": True,
         "maxExportResults": 10000, "facets": ["year.raw"],
         # `text` is in mrl_slides' real list and is REJECTED by fieldsToExtract with HTTP
         # 400, so the constructor must drop it rather than fail every request here.
         "cardModel": {"link": "{link}", "fields": [
             {"id": "text", "label": "Other Content", "startsNewSection": False},
             {"id": "compound", "label": "Compound", "startsNewSection": False}]}},
        # `fullDocumentTextRetrievalEnabled` DELIBERATELY ABSENT. One unsupported
        # document returns HTTP 403 for the whole read batch, so absent must never
        # read as True.
        {"id": "signals", "title": "Signals", "category": "RESEARCH", "hidden": False,
         "maxExportResults": 10000, "facets": ["experiment_status.raw"],
         "cardModel": {"link": "{link}", "fields": ["title"]}},
        # Listed, hidden:False, indistinguishable from a usable source — and DENIED.
        {"id": "radr_x", "title": "RADR", "category": "DOCUMENTS", "hidden": False,
         "fullDocumentTextRetrievalEnabled": True, "maxExportResults": 10000,
         "facets": [], "cardModel": {"link": None, "fields": []}},
    ],
}

UPDATES_BODY = [
    {"id": "meds", "type": "index", "date": "2026-08-25T00:53:37"},
    {"id": "med_comms", "type": "index", "date": "2026-08-25T05:35:52"},
    {"id": "scited", "type": "index", "date": "2026-08-23T05:34:23"},
    # The only `ingest` in the real response, and the anomaly is unexplained.
    {"id": "mrl_slides", "type": "ingest", "date": "2026-08-25T03:50:18"},
    # `signals` DELIBERATELY ABSENT -> freshness unknown, must be flagged.
    # An epoch sentinel on a non-source entry: "never indexed", not "1970".
    {"id": "reds.min", "type": "index", "date": "1970-01-01T00:00:00"},
]

ACCESS_BODY = {
    "access": {"meds": "ALLOWED", "med_comms": "ALLOWED", "scited": "ALLOWED",
               "mrl_slides": "ALLOWED", "signals": "ALLOWED", "radr_x": "DENIED"},
    # Present in the real response and must never be stored.
    "info": {"displayName": "SHOULD NOT BE CACHED", "department": "X",
             "postalCode": "Y", "location": "Z"},
    "user": "isid00", "mmdKxId": "x" * 64, "projectTeam": True,
}


def ingest_body(ds: str, *, strict: bool = True) -> dict[str, Any]:
    """A per-source ingest body. `fields[]` carries facetType for EVERY type."""
    # Measured live 2026-08-25 on all five sources: `.raw` presence and `facetField: true`
    # are the SAME SET (55/51/6/11/21, zero difference either way), so a stub field may not
    # carry a facetType unless it also carries `.raw`. `title` in particular is
    # `has_raw: False, facetField: False, facetType: None, highlightable: True` on every
    # source -- an earlier version of this stub typed it `"SEARCH"`, which made `title` look
    # facetable without a `.raw` subfield. That combination does not exist live, and it hid
    # which predicate a guard was reading.
    common = {
        "_all": ({"type": "text"}, None),
        "title": ({"type": "text"}, None),
        "creation_date": ({"type": "date", "fields": {"raw": {"type": "keyword"}}}, "DATE"),
        "modified_date": ({"type": "date", "fields": {"raw": {"type": "keyword"}}}, "DATE"),
    }
    per: dict[str, tuple[dict[str, Any], str | None]] = {
        "meds": {
            "text": ({"type": "text"}, None),
            "doc_status": ({"type": "text", "fields": {"raw": {"type": "keyword"}}}, "SEARCH"),
            # `product_name` IS a flagged facet field live. Measured 2026-08-25: `.raw`
            # presence and `facetField: true` are the SAME SET on all five sources -- 55 on
            # meds, zero difference either way. It is merely not among the 35 the endpoint
            # COMPUTES, and that is the poison rule; `facets_body` models it by omitting
            # `product_name.raw` from the computed set. An earlier version of this stub set
            # facetType None here, conflating "not computed" with "not flagged". That made
            # the stub disagree with the live index and hid which of the two predicates a
            # guard was actually reading -- wave 3's `value_prefix` guard reads the flag.
            "product_name": ({"type": "text", "fields": {"raw": {"type": "keyword"}}}, "SEARCH"),
            "mmd_tag_issue": ({"type": "text", "fields": {"raw": {"type": "keyword"}}}, "TAXONOMY"),
            "mmd_tag_analytical_method": ({"type": "text", "fields": {"raw": {"type": "keyword"}}}, "TAXONOMY"),
        },
        # med_comms carries BOTH status candidates live, so a role can legitimately
        # resolve to more than one field. The clause compiler treats a field list as a
        # union, which is measured to widen (`["title","text"]` -> 135 vs 13 and 133).
        # An earlier version of this stub omitted `doc_status` and therefore never
        # exercised the multi-field case; the live run caught that.
        "med_comms": {
            "text": ({"type": "text"}, None),
            # `doc_status` exists on med_comms but has NO `.raw` and is NOT a facet field --
            # measured live 2026-08-25, and `searchField` on it returns HTTP 500. It is the
            # design's named example of a failure reachable through ordinary use, because the
            # `status` role resolves to it on both meds and med_comms while only meds can
            # facet it. An earlier version of this stub gave it a `.raw` and a facetType,
            # which disabled the one guard that case exists to exercise.
            "doc_status": ({"type": "text"}, None),
            "activity_status": ({"type": "text", "fields": {"raw": {"type": "keyword"}}}, "SEARCH"),
            "primary_mkv_number1": ({"type": "text", "fields": {"raw": {"type": "keyword"}}}, "SEARCH"),
        },
        # scited has NO `text` field and NO status field — both roles unfillable.
        "scited": {
            "compound_id": ({"type": "text", "fields": {"raw": {"type": "keyword"}}}, "SEARCH"),
        },
        "mrl_slides": {
            "text": ({"type": "text"}, None),
            "compound": ({"type": "text", "fields": {"raw": {"type": "keyword"}}}, "SEARCH"),
        },
        "signals": {
            "text": ({"type": "text"}, None),
            "experiment_status": ({"type": "text", "fields": {"raw": {"type": "keyword"}}}, "SEARCH"),
            "project_m_code": ({"type": "text", "fields": {"raw": {"type": "keyword"}}}, "SEARCH"),
        },
    }[ds]
    merged = {**common, **per}
    return {
        "mappings": {"document": {
            "dynamic": "strict" if strict else "dynamic",
            "properties": {k: v[0] for k, v in merged.items()},
        }},
        "fields": [
            {"id": k, "facetField": v[1] is not None, "facetType": v[1],
             "highlightable": k == "title", "restricted": k == "text" and ds == "meds"}
            for k, v in merged.items()
        ],
        "knnSettings": {"enabled": True} if ds in ("meds", "med_comms") else None,
    }


def facets_body(ds: str) -> dict[str, Any]:
    """The COMPUTED facet set. The poison-rule predicate.

    meds deliberately OMITS `product_name.raw` — its own program filter field — which is
    the measured situation: filtering on it zeroes the whole distribution. The other four
    include theirs.
    """
    base: dict[str, Any] = {"doc_status.raw": [{"term": "Effective", "count": 1014}],
                            "restricted.raw": [{"term": "Accessible", "count": 1014}],
                            "creation_date.raw": [{"term": "2025", "count": 400}]}
    if ds == "meds":
        base["mmd_tag_issue.raw"] = [
            {"term": f"L1|{BRANCH_ISSUE}|eeeeeeee-0000-0000-0000-000000000005", "count": 93},
            {"term": f"L2|{BRANCH_ISSUE}|eeeeeeee-0000-0000-0000-000000000005|{NODE_OOS}", "count": 53},
        ]
        base["mmd_tag_analytical_method.raw"] = [
            {"term": f"L1|{BRANCH_METHOD}|{NODE_MICRO}", "count": 68},
        ]
        return {"facets": base}
    field = {"med_comms": "primary_mkv_number1", "scited": "compound_id",
             "mrl_slides": "compound", "signals": "project_m_code"}[ds]
    base[f"{field}.raw"] = [{"term": "MK-6070", "count": 19}]
    return {"facets": base}


def install_stubs(m, *, fail: dict[str, str] | None = None,
                  strict_for: set[str] | None = None) -> list[tuple[str, str]]:
    """Replace `_request` with a stub. Returns the call log the test inspects."""
    fail = fail or {}
    strict_for = strict_for if strict_for is not None else set(IN_SCOPE)
    log: list[tuple[str, str]] = []

    async def stub(method: str, endpoint: str, *, payload=None, params=None):
        log.append((method, endpoint))
        for frag, reason in fail.items():
            if frag in endpoint:
                return {"_ok": False, "_status": {"executed": False, "reason": reason}}
        if endpoint == m.EP_UPDATES:
            return {"_ok": True, "_status": {"executed": True}, "_body": UPDATES_BODY}
        if endpoint == m.EP_ACCESS:
            return {"_ok": True, "_status": {"executed": True}, "_body": ACCESS_BODY}
        if endpoint == m.EP_CONFIG:
            return {"_ok": True, "_status": {"executed": True}, "_body": CONFIG_BODY}
        if endpoint == m.EP_TAXONOMY:
            return {"_ok": True, "_status": {"executed": True}, "_body": TAXONOMY_BODY}
        if "/data-source/" in endpoint:
            ds = endpoint.rsplit("/", 1)[-1]
            return {"_ok": True, "_status": {"executed": True},
                    "_body": ingest_body(ds, strict=ds in strict_for)}
        if endpoint == m.EP_FACETS:
            ds = (payload or {}).get(m.F_DATASOURCES, ["?"])[0]
            return {"_ok": True, "_status": {"executed": True}, "_body": facets_body(ds)}
        raise AssertionError(f"stub reached an unexpected endpoint: {endpoint}")

    # ONLY `_request` is replaced. `_post` and `_get` resolve it by global lookup, so
    # the real wrappers still run and their delegation is covered for free -- a future
    # duplicated `_calls_used` inside `_post` would be caught here rather than doubling
    # the effective budget in silence.
    m._request = stub
    return log


# --------------------------------------------------------------------------
FAILURES: list[str] = []
CHECKS = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{': ' + detail if detail else ''}")
    if not ok:
        FAILURES.append(label)


def fresh(m):
    m._CACHE = None
    m._calls_used = 0
    m._started = m.time.monotonic()


def main() -> int:
    m = load_server()

    print("1. happy path — the cache builds what it claims")
    log = install_stubs(m)
    fresh(m)
    c = asyncio.run(m.warm_cache("MK-6070"))
    check("ok is True", c["ok"] is True, f"degraded={c['degraded']}")
    check("14 calls issued", len(log) == 14, f"{len(log)} calls: "
          f"{sum(1 for x in log if x[0]=='GET')} GET / "
          f"{sum(1 for x in log if x[0]=='POST')} POST")
    check("data-updates is called FIRST", log[0][1] == m.EP_UPDATES, f"first={log[0]}")
    check("user-access-check is called SECOND, before any fan-out",
          log[1][1] == m.EP_ACCESS, f"second={log[1]}")
    check("all five in-scope sources cached", sorted(c["sources"]) == sorted(IN_SCOPE),
          str(sorted(c["sources"])))

    print("\n2. searchability comes from `access`, never from being listed")
    # NOT an access assertion. `radr_x` is absent because it has no entry in the scope
    # YAML, so this would still pass with the access logic deleted. Labelled for what it
    # actually tests; the DENIED-flip below is what covers the criterion.
    check("[scope boundary] a source outside the scope YAML is never cached",
          "radr_x" not in c["sources"],
          "it IS listed in /v2/configuration with hidden:False — listing is not scope")
    check("every cached source is ALLOWED",
          all(s["searchable"] for s in c["sources"].values()))
    fresh(m)
    install_stubs(m)
    denied = dict(ACCESS_BODY["access"])
    denied["scited"] = "DENIED"
    orig = ACCESS_BODY["access"]
    ACCESS_BODY["access"] = denied
    c2 = asyncio.run(m.warm_cache("MK-6070"))
    check("flipping a source to DENIED marks it unsearchable",
          c2["sources"]["scited"]["searchable"] is False,
          f"access={c2['sources']['scited']['access']}")
    ACCESS_BODY["access"] = orig

    print("\n3. caller PII never enters the cache")
    blob = repr(c)
    check("happy path: no `info` / displayName / postalCode / ISID in the cache",
          "SHOULD NOT BE CACHED" not in blob and "isid00" not in blob
          and "postalCode" not in blob)
    # The realistic leak is not the happy path. On 400/5xx/non-200/non-JSON the real
    # `_status` carries `source_response`: up to 400 chars of the upstream body, which for
    # THIS endpoint is a body full of caller PII. That lands in `cache["calls"]`. The
    # happy-path assertion above cannot see it, because a success envelope has no such key.
    fresh(m)
    leak = ("{'access':{},'info':{'displayName':'SHOULD NOT BE CACHED',"
            "'postalCode':'12345'},'user':'isid00'}")
    log = install_stubs(m)
    real_stub = m._request

    async def leaky(method, endpoint, *, payload=None, params=None):
        if endpoint == m.EP_ACCESS:
            return {"_ok": False, "_status": {
                "executed": False, "reason": "source_failure", "http_status": 500,
                "source_response": leak}}
        return await real_stub(method, endpoint, payload=payload, params=params)

    m._request = leaky
    c_leak = asyncio.run(m.warm_cache("MK-6070"))
    check("failure path: an upstream error body reaches `calls` (this is why it exists)",
          leak in repr(c_leak.get("calls", {})),
          "confirms the leak vector is real, so the next assertion is not vacuous")
    st = m.cache_status()
    check("failure path: it does NOT reach `cache_status()`",
          "SHOULD NOT BE CACHED" not in repr(st) and "isid00" not in repr(st))

    print("\n4. the poison rule is known BEFORE any filtered facet request")
    check("meds program filter is NOT facetable",
          c["sources"]["meds"]["facetable_program_filter"] is False,
          "product_name.raw is absent from the computed set")
    for s in ("med_comms", "scited", "mrl_slides", "signals"):
        check(f"{s} program filter IS facetable",
              c["sources"][s]["facetable_program_filter"] is True)

    print("\n5. readability: absent must never read as True")
    check("signals `readable` is False when the flag is absent",
          c["sources"]["signals"]["readable"] is False,
          f"declared={c['sources']['signals']['readable_declared']!r}")
    check("meds `readable` is True when the flag is True",
          c["sources"]["meds"]["readable"] is True)

    print("\n6. roles resolve per source, and an unfillable role is reported")
    check("scited cannot fill `body` or `status`",
          sorted(c["sources"]["scited"]["unfillable_roles"]) == ["body", "status"],
          str(c["sources"]["scited"]["unfillable_roles"]))
    check("scited is NOT given a substitute body field",
          "body" not in c["sources"]["scited"]["roles"])
    check("signals `status` resolves to experiment_status",
          c["sources"]["signals"]["roles"].get("status") == ["experiment_status"])
    check("med_comms `status` keeps BOTH candidates, in candidate order",
          c["sources"]["med_comms"]["roles"].get("status") == ["doc_status", "activity_status"],
          str(c["sources"]["med_comms"]["roles"].get("status")))
    check("mrl_slides cannot fill `status` but can fill `body`",
          c["sources"]["mrl_slides"]["unfillable_roles"] == ["status"]
          and c["sources"]["mrl_slides"]["roles"].get("body") == ["text"],
          str(c["sources"]["mrl_slides"]["unfillable_roles"]))

    print("\n7. the DATE guard reads facetType from /v3/ingest — no join needed")
    check("meds DATE facets identified",
          c["sources"]["meds"]["date_facets"] == ["creation_date", "modified_date"],
          str(c["sources"]["meds"]["date_facets"]))
    check("a SEARCH facet is not marked DATE",
          "doc_status" not in c["sources"]["meds"]["date_facets"])

    print("\n8. taxonomy roles are derived, never hardcoded")
    check("meds carries exactly the two stubbed taxonomy roles",
          sorted(c["sources"]["meds"]["taxonomy_roles"]) == ["analytical_method", "issue"],
          str(sorted(c["sources"]["meds"]["taxonomy_roles"])))
    check("no other source claims a taxonomy role",
          all(not c["sources"][s]["taxonomy_roles"]
              for s in IN_SCOPE if s != "meds"))

    print("\n9. taxonomy decodes both ways, and field↔branch is by ROOT UUID")
    tax = c["taxonomy"]
    check("tree loaded", tax["loaded"] is True, f"{tax.get('node_count')} nodes")
    check("uuid decodes to a readable path",
          tax["uuid_to_path"].get(NODE_OOS)
          == ("Topics and Issues", "Testing issue", "Out of Specification"),
          str(tax["uuid_to_path"].get(NODE_OOS)))
    check("label encodes to the right L-path depth",
          tax["label_to_lpath"].get("Out of Specification")
          == f"L2|{BRANCH_ISSUE}|eeeeeeee-0000-0000-0000-000000000005|{NODE_OOS}",
          str(tax["label_to_lpath"].get("Out of Specification")))
    check("a top-level branch is L0",
          tax["label_to_lpath"].get("Analytical Method") == f"L0|{BRANCH_METHOD}")
    check("synonyms captured", tax["synonyms"].get("Out of Specification") == ["OOS", "out-of-spec"])
    # Per source, not flat — see §27. Two sources both carrying `mmd_tag_issue` would
    # collide in a flat map and one branch would silently win.
    f2b = tax["field_to_branch"].get("meds", {})
    check("field→branch matched by root UUID, not by name",
          f2b.get("mmd_tag_issue") == BRANCH_ISSUE
          and f2b.get("mmd_tag_analytical_method") == BRANCH_METHOD,
          f"{f2b} — note the branch label 'Topics and Issues' does not match its field")

    print("\n10. freshness guards are internal, and neither lies")
    check("a source missing from data-updates is flagged absent",
          c["sources"]["signals"]["freshness"].get("absent") is True
          and c["sources"]["signals"]["freshness"]["date"] is None)
    check("mrl_slides' `ingest` kind is carried, not equated with `index`",
          c["sources"]["mrl_slides"]["freshness"]["kind"] == "ingest")
    check("a real date survives", c["sources"]["meds"]["freshness"]["date"]
          == "2026-08-25T00:53:37")
    check("naive timestamps are labelled as such",
          c["sources"]["meds"]["freshness"]["tz"] == "naive")

    print("\n11. KNN capability, and the two sources agree")
    check("meds and med_comms are KNN-enabled",
          c["sources"]["meds"]["knn"] and c["sources"]["med_comms"]["knn"])
    check("the other three are not",
          not any(c["sources"][s]["knn"] for s in ("scited", "mrl_slides", "signals")))
    check("the vectorized SOURCE field is recorded, not mistaken for the index field",
          c["knn_project"]["vectorized_source_fields"] == ["text"])

    print("\n12. locator templates, including the one that cannot resolve")
    check("meds template is the pdf url field",
          c["sources"]["meds"]["locator_template"] == "{webview_pdf_url}")
    check("med_comms template is the DO-NOT-SHARE attachments path",
          "DO-NOT-SHARE-THIS-URL" in (c["sources"]["med_comms"]["locator_template"] or ""))

    print("\n13. drift signal: the configuration is fingerprinted")
    check("fingerprint recorded", bool(c.get("config_fingerprint")),
          "no etag or last-modified exists, so a content hash is the only signal")

    print("\n=== SABOTAGE: every guard, made to fire ===")

    print("\n14. expired SESSION short-circuits after ONE call")
    fresh(m)
    log = install_stubs(m, fail={"data-updates": "authentication_failure"})
    c3 = asyncio.run(m.warm_cache("MK-6070"))
    check("stops after the auth gate", len(log) == 1, f"{len(log)} calls made")
    check("ok is False", c3["ok"] is False)
    check("the reason is stated", any(d["call"] == "data_updates" and d["fatal"]
                                     for d in c3["degraded"]), str(c3["degraded"]))

    print("\n15. user-access-check failure is FATAL — a denied zero would read as absence")
    fresh(m)
    install_stubs(m, fail={"user-access-check": "source_failure"})
    c4 = asyncio.run(m.warm_cache("MK-6070"))
    check("ok is False", c4["ok"] is False)
    check("reason names the call",
          any(d["call"] == "user_access_check" and d["fatal"] for d in c4["degraded"]))

    print("\n16. configuration failure is FATAL and stops the fan-out")
    fresh(m)
    log = install_stubs(m, fail={"v2/configuration": "source_failure"})
    c5 = asyncio.run(m.warm_cache("MK-6070"))
    check("ok is False", c5["ok"] is False)
    check("no per-source calls were made", len(log) == 3, f"{len(log)} calls")
    check("no sources cached", c5["sources"] == {})

    print("\n17. one source's ingest 404 excludes only that source")
    fresh(m)
    install_stubs(m, fail={"data-source/scited": "unexpected_status"})
    c6 = asyncio.run(m.warm_cache("MK-6070"))
    check("not fatal", c6["ok"] is True, f"degraded={[d['call'] for d in c6['degraded']]}")
    check("scited records the reason",
          c6["sources"]["scited"].get("degraded") == "unexpected_status")
    check("scited has no fields, so no clause can validate against it",
          c6["sources"]["scited"]["fields"] == {})
    check("the other four are intact",
          all(c6["sources"][s]["fields"] for s in IN_SCOPE if s != "scited"))

    print("\n18. a non-strict mapping degrades but does not stop")
    fresh(m)
    install_stubs(m, strict_for=set(IN_SCOPE) - {"meds"})
    c7 = asyncio.run(m.warm_cache("MK-6070"))
    check("ok is True", c7["ok"] is True)
    check("meds mapping marked incomplete",
          c7["sources"]["meds"]["mapping_complete"] is False)
    check("the reason says field validation is best-effort",
          any("best-effort" in d["reason"] for d in c7["degraded"]), str(c7["degraded"]))

    print("\n19. facets failure loses the distribution, not the records")
    fresh(m)
    install_stubs(m, fail={"v1/facets": "throttled_or_quota_exhausted"})
    c8 = asyncio.run(m.warm_cache("MK-6070"))
    check("not fatal", c8["ok"] is True)
    check("poison-rule status is UNKNOWN, not assumed",
          all(c8["sources"][s]["facetable_program_filter"] is None for s in IN_SCOPE))
    check("fields still present", bool(c8["sources"]["meds"]["fields"]))

    print("\n20. taxonomy failure is degraded, not broken")
    fresh(m)
    install_stubs(m, fail={"v1/taxonomy": "timeout"})
    c9 = asyncio.run(m.warm_cache("MK-6070"))
    check("not fatal", c9["ok"] is True)
    check("tree not loaded", c9["taxonomy"]["loaded"] is False)
    check("taxonomy ROLES still known, so the refusal can name them",
          bool(c9["sources"]["meds"]["taxonomy_roles"]))

    print("\n=== the review gate's findings, each with an assertion ===")

    print("\n22. [2.2] a NON-fatal data-updates failure must say UNREAD, not ABSENT")
    fresh(m)
    install_stubs(m, fail={"data-updates": "timeout"})
    ct = asyncio.run(m.warm_cache("MK-6070"))
    f = ct["sources"]["meds"]["freshness"]
    check("not fatal, so the wave still builds", ct["ok"] is True)
    check("`unread` is True", f.get("unread") is True, str(f))
    check("`absent` is None — NOT True; meds does have an entry",
          f.get("absent") is None)
    check("`never_indexed` is None — nobody read it, so it is not False",
          f.get("never_indexed") is None)
    check("cache_status reports freshness was never read",
          m.cache_status()["index_freshness_read"] is False)

    print("\n23. [2.3] an ingest failure must leave NOTHING that reads as usable")
    fresh(m)
    install_stubs(m, fail={"data-source/scited": "unexpected_status"})
    ci = asyncio.run(m.warm_cache("MK-6070"))
    s = ci["sources"]["scited"]
    check("every role is unfillable, not an empty list",
          sorted(s["unfillable_roles"]) == sorted(m._ROLE_CANDIDATES), str(s["unfillable_roles"]))
    check("`roles_resolved` is False", s["roles_resolved"] is False)
    check("`date_facets` is None, not [] — the DATE guard's state is unknown",
          s["date_facets"] is None)
    check("`facetable_program_filter` is None, not True",
          s["facetable_program_filter"] is None)
    check("`computed_facets` is None, not []", s["computed_facets"] is None)
    check("`mapping_retrieved` False is distinct from `mapping_complete`",
          s["mapping_retrieved"] is False and s["mapping_complete"] is None)
    check("no facets call was made for it", "facets:scited" not in ci["calls"])
    check("the other four are fully resolved",
          all(ci["sources"][x]["roles_resolved"] for x in IN_SCOPE if x != "scited"))

    print("\n24. [2.4] `_all` resolves from the mapping like every other field")
    fresh(m)
    install_stubs(m)
    orig_ingest = m._request

    async def no_all(method, endpoint, *, payload=None, params=None):
        r = await orig_ingest(method, endpoint, payload=payload, params=params)
        if "/data-source/meds" in endpoint and r["_ok"]:
            r["_body"]["mappings"]["document"]["properties"].pop("_all", None)
            r["_body"]["fields"] = [x for x in r["_body"]["fields"] if x["id"] != "_all"]
        return r

    m._request = no_all
    ca = asyncio.run(m.warm_cache("MK-6070"))
    check("with `_all` removed from the mapping, `anywhere` is UNFILLABLE",
          "anywhere" in ca["sources"]["meds"]["unfillable_roles"],
          f"roles={ca['sources']['meds']['roles']}")
    check("and it is not substituted",
          "anywhere" not in ca["sources"]["meds"]["roles"])

    print("\n25. [2.5] a transient fatal failure is NOT persisted, so recapture recovers")
    fresh(m)
    install_stubs(m, fail={"data-updates": "authentication_failure"})
    c_dead = asyncio.run(m.warm_cache("MK-6070"))
    check("marked retryable", c_dead.get("retryable") is True)
    check("_CACHE was left unset", m._CACHE is None,
          "otherwise an expired-session cache pins `searchable: False` until the host restarts")
    install_stubs(m)          # the human replaces the SESSION
    c_live = asyncio.run(m.warm_cache("MK-6070"))
    check("the next warm succeeds without force=True", c_live["ok"] is True)
    fresh(m)
    install_stubs(m, fail={"program_scope_never_matches": "x"})
    m._load_scope = lambda p=None: (None, "no scope mapping for 'NOPE'")
    c_scope = asyncio.run(m.warm_cache("NOPE"))
    check("[2.10] a scope failure is fatal and stated",
          c_scope["ok"] is False and any(d["call"] == "program_scope"
                                         for d in c_scope["degraded"]), str(c_scope["degraded"]))
    check("a NON-transient fatal failure IS persisted", m._CACHE is not None,
          "a missing YAML will not fix itself mid-process")
    m._load_scope = load_server()._load_scope  # restore

    print("\n26. [2.6] HTTP 200 with no facet map is unknown, not empty")
    fresh(m)
    install_stubs(m)
    base = m._request

    async def no_facets(method, endpoint, *, payload=None, params=None):
        if endpoint == m.EP_FACETS:
            return {"_ok": True, "_status": {"executed": True}, "_body": {"queryId": "x"}}
        return await base(method, endpoint, payload=payload, params=params)

    m._request = no_facets
    cf = asyncio.run(m.warm_cache("MK-6070"))
    check("poison-rule status stays None for every source",
          all(cf["sources"][x]["facetable_program_filter"] is None for x in IN_SCOPE))
    check("`computed_facets` stays None, not []",
          all(cf["sources"][x]["computed_facets"] is None for x in IN_SCOPE))
    check("and the reason is stated per source",
          sum(1 for d in cf["degraded"] if "no facet map" in d["reason"]) == 5,
          str([d["call"] for d in cf["degraded"]]))

    print("\n27. [2.7 / 1.3] taxonomy shape is uniform, and label→(field, L-path) exists")
    fresh(m)
    install_stubs(m)
    c27 = asyncio.run(m.warm_cache("MK-6070"))
    check("`field_to_branch` is always present, never lazily created",
          isinstance(c27["taxonomy"]["field_to_branch"], dict))
    check("it is keyed PER SOURCE, so two sources cannot collide",
          "meds" in c27["taxonomy"]["field_to_branch"],
          str(list(c27["taxonomy"]["field_to_branch"])))
    lfl = c27["taxonomy"]["label_to_field_lpath"]
    check("label→(field, L-path) is built, per source",
          lfl["meds"]["Out of Specification"]
          == ("mmd_tag_issue",
              f"L2|{BRANCH_ISSUE}|eeeeeeee-0000-0000-0000-000000000005|{NODE_OOS}"),
          str(lfl["meds"].get("Out of Specification")))
    check("a label resolves to the field its BRANCH belongs to, not its name",
          lfl["meds"]["Microbial testing"][0] == "mmd_tag_analytical_method")
    check("an unknown label is simply absent, so it can be refused",
          "Not A Real Label" not in lfl["meds"])

    print("\n28. [2.1] duplicate labels are DETECTED, not silently last-writer-wins")
    fresh(m)
    dup = {"id": "ffffffff-0000-0000-0000-000000000009", "name": "Other"}
    TAXONOMY_BODY["root"]["children"]["Topics and Issues"]["children"]["Other"] = dict(dup)
    TAXONOMY_BODY["root"]["children"]["Analytical Method"]["children"]["Other"] = dict(
        dup, id="ffffffff-0000-0000-0000-00000000000a")
    install_stubs(m)
    cd = asyncio.run(m.warm_cache("MK-6070"))
    check("the ambiguity is counted",
          cd["taxonomy"]["ambiguous_label_count"] == 1,
          f"nodes={cd['taxonomy']['node_count']} distinct={cd['taxonomy']['distinct_labels']}")
    check("and stated in `degraded`, so a later wave can refuse the label",
          any("ambiguous" in d["reason"] for d in cd["degraded"]), str(cd["degraded"]))
    check("cache_status surfaces it",
          m.cache_status()["taxonomy_ambiguous_labels"] == 1)
    del TAXONOMY_BODY["root"]["children"]["Topics and Issues"]["children"]["Other"]
    del TAXONOMY_BODY["root"]["children"]["Analytical Method"]["children"]["Other"]

    print("\n29. [2.11] an empty MUSE project is refused, never sent")
    fresh(m)
    install_stubs(m)
    real_project = m._project
    m._project = lambda scope: ""
    c29 = asyncio.run(m.warm_cache("MK-6070"))
    check("fatal, and no call is made",
          c29["ok"] is False and m._calls_used == 0,
          f"calls={m._calls_used} degraded={c29['degraded']}")
    m._project = real_project

    print("\n30. [2.10] cache_status is safe on every shape, and omits `calls`")
    for label, kw in [("never warmed", None),
                      ("expired SESSION", {"data-updates": "authentication_failure"}),
                      ("config down", {"v2/configuration": "source_failure"}),
                      ("healthy", {})]:
        fresh(m)
        if kw is None:
            m._CACHE = None
            st = m.cache_status()
            check("never warmed -> warm False, no KeyError", st["warm"] is False)
            continue
        install_stubs(m, fail=kw)
        asyncio.run(m.warm_cache("MK-6070"))
        try:
            st = m.cache_status()
            ok = "calls" not in st
        except KeyError as exc:
            st, ok = {}, False
            check(f"{label} -> cache_status KeyError {exc}", False)
            continue
        check(f"{label} -> no KeyError, and `calls` is omitted", ok, str(sorted(st))[:90])

    print("\n21. the cache is loaded once per process")
    fresh(m)
    log = install_stubs(m)
    asyncio.run(m.warm_cache("MK-6070"))
    n = len(log)
    asyncio.run(m.warm_cache("MK-6070"))
    check("a second call issues no requests", len(log) == n, f"{len(log) - n} extra")
    asyncio.run(m.warm_cache("MK-6070", force=True))
    check("force=True reloads", len(log) == 2 * n, f"{len(log)} total")

    # 31, because 22 through 30 are already taken further up. Numbering here is append-order, not
    # a sequence, and a colliding heading makes a section unfindable by its own label. (An earlier
    # version of this comment claimed the "21. the cache is loaded once" heading above was a
    # duplicate. It is not -- there is one 21; it is out of sequence.)
    print("\n31. WAVE 10 -- refresh_freshness, which no harness reached at all")
    # `grep -l refresh_freshness *_check.py` returned NOTHING before this section. Wave 8 added the
    # function to fix a set-and-never-read defect, three review gates found three defects inside
    # it, and no assertion anywhere touched it -- which is also why `mutation_gate.py`'s REV-A
    # mutant, gutting the re-read while still reporting success, survives the whole suite.
    fresh(m)
    log = install_stubs(m)
    cache = asyncio.run(m.warm_cache("MK-6070"))
    before = len([1 for _method, ep in log if ep == m.EP_UPDATES])

    # The re-read actually happens. Not "the report says reread: True" -- the CALL.
    report = asyncio.run(m.refresh_freshness(cache))
    after = len([1 for _method, ep in log if ep == m.EP_UPDATES])
    check("refresh_freshness issues its own data-updates GET", after == before + 1,
          f"{before} -> {after}")
    check("  ... and says so", report.get("reread") is True, str(report.get("reread")))

    # R2b. A source that HAD a date and drops out of the response must be flagged, and the ones
    # that remained must NOT be -- a blanket flag on everything satisfies the first half alone.
    # `signals` is deliberately absent from UPDATES_BODY from the start, so it is excluded from
    # the control: it never appeared, which is a different fact from having vanished.
    gone = [e for e in UPDATES_BODY if e["id"] != "meds"]
    still_listed = [e["id"] for e in gone if e["id"] in cache["sources"]]

    async def without_meds(method, endpoint, *, payload=None, params=None):
        if endpoint == m.EP_UPDATES:
            return {"_ok": True, "_status": {"executed": True}, "_body": gone}
        raise AssertionError(f"unexpected endpoint in this check: {endpoint}")

    m._request = without_meds
    report = asyncio.run(m.refresh_freshness(cache))
    meds_f = cache["sources"]["meds"].get("freshness") or {}
    check("R2b a source that vanishes from data-updates is flagged absent",
          meds_f.get("absent") is True and meds_f.get("vanished_since_startup") is True,
          str({k: v for k, v in meds_f.items() if k != "kind"}))
    check("  ... and named in the report, not only in the cache",
          "meds" in (report.get("vanished_since_startup") or []),
          str(report.get("vanished_since_startup")))
    check("  ... while the sources that DID answer are left alone",
          all(not ((cache["sources"][s].get("freshness") or {}).get("vanished_since_startup"))
              for s in still_listed), str(still_listed))
    check("  ... and its stale date is no longer presented as current",
          bool(meds_f.get("absent")), str(meds_f.get("date")))

    # R2a. A move invalidates every fact DERIVED FROM THE INDEX, and only those. The negative half
    # matters as much as the positive: blanking `knn` or `readable` would refuse semantic retrieval
    # and document reads for no reason, because both come from the CONFIGURATION endpoint and a
    # reindex says nothing about them.
    fresh(m)
    install_stubs(m)
    cache = asyncio.run(m.warm_cache("MK-6070"))
    src = cache["sources"]["meds"]
    was = {f: src.get(f) for f in ("mapping_retrieved", "computed_facets", "knn", "readable")}
    check("index-derived facts are established before the move",
          was["mapping_retrieved"] is not None and was["computed_facets"] is not None,
          f"mapping={was['mapping_retrieved']}, facets={bool(was['computed_facets'])}")
    moved_body = [dict(e, date="2026-09-09T00:00:00") if e["id"] == "meds" else e
                  for e in copy.deepcopy(UPDATES_BODY)]

    async def moved_stub(method, endpoint, *, payload=None, params=None):
        if endpoint == m.EP_UPDATES:
            return {"_ok": True, "_status": {"executed": True}, "_body": moved_body}
        raise AssertionError(f"unexpected endpoint in this check: {endpoint}")

    m._request = moved_stub
    report = asyncio.run(m.refresh_freshness(cache))
    check("R2a a reindex is reported as movement", "meds" in (report.get("moved_since_startup") or []),
          str(report.get("moved_since_startup")))
    check("  ... and blanks the index-derived facts to UNKNOWN",
          all(src.get(f) is None for f in ("mapping_retrieved", "computed_facets",
                                           "filter_field_terms")),
          f"mapping={src.get('mapping_retrieved')}, facets={src.get('computed_facets')}, "
          f"filter_terms={src.get('filter_field_terms')}")
    check("  ... and leaves the CONFIGURATION-derived facts alone -- a reindex says nothing of them",
          src.get("knn") == was["knn"] and src.get("readable") == was["readable"],
          f"knn {was['knn']} -> {src.get('knn')}, readable {was['readable']} -> "
          f"{src.get('readable')}")
    check("  ... and marks the source so first use can re-read it",
          src.get("index_moved") is True, str(src.get("index_moved")))

    # R2c. `diagnose_zero` rules `nonexistent_condition_field` out from `mapping_retrieved`. With
    # that blanked, the rule-out must not happen -- until a re-read re-establishes it.
    plan = {"source": "meds", "engine": "conditional", "program_filter": {}, "conditions": [],
            "search": {}, "count_defensible": True, "narrowed": True}
    az = m.diagnose_zero({}, plan, cache["sources"]["meds"])
    check("R2c a blanked mapping stops `nonexistent_condition_field` being ruled out",
          "nonexistent_condition_field" not in (az.get("ruled_out") or []),
          str(az.get("ruled_out")))

    # S2. The integrated review found `mapping_complete` surviving a move while
    # `field_validation` read it FIRST, so a source whose mapping was blanked reported
    # `authoritative` beside prose saying the mapping was never retrieved. Every fact from the same
    # two calls goes UNKNOWN together, and `field_validation` tests UNKNOWN before it tests
    # completeness -- either alone closes it, and the pair holds if a third such fact is missed.
    check("S2 every fact from the ingest and facet calls goes UNKNOWN together",
          all(src.get(f) is None for f in ("mapping_retrieved", "mapping_complete",
                                          "computed_facets", "filter_field_terms",
                                          "facetable_program_filter")),
          f"complete={src.get('mapping_complete')}, "
          f"facetable={src.get('facetable_program_filter')}")
    # M1. `still_unread_note` was mis-indented into the `vanished` block, so `still_unread` shipped
    # with no note and the note shipped beside `vanished_note` saying the OPPOSITE of the vanished
    # finding -- "currency is unknown rather than old", where the vanished finding is that a date
    # WAS recorded and IS now stale. The two must travel with their own keys and never with each
    # other's.
    #
    # `still_unread` needs the STARTUP data-updates call to have failed, not merely a source
    # missing from the response: `_parse_updates` marks an absent source `absent`, and only a failed
    # read marks one `unread`. The first version of this assertion warmed normally and got an empty
    # list -- the wrong scenario, which is the fault this file has caught three times.
    fresh(m)
    install_stubs(m, fail={"data-updates": "timeout"})
    c_m1 = asyncio.run(m.warm_cache("MK-6070"))
    check("the startup freshness read failed, so sources are UNREAD not absent",
          all((c_m1["sources"][s].get("freshness") or {}).get("unread") for s in IN_SCOPE),
          str({s: (c_m1["sources"][s].get("freshness") or {}).get("unread") for s in IN_SCOPE}))
    install_stubs(m)
    rep_m1 = asyncio.run(m.refresh_freshness(c_m1))
    check("M1 `still_unread` travels with its own note",
          bool(rep_m1.get("still_unread")) and bool(rep_m1.get("still_unread_note")),
          f"unread={rep_m1.get('still_unread')}, note={bool(rep_m1.get('still_unread_note'))}")
    check("  ... and the note is present exactly when its own key is, never with the vanished one",
          ("still_unread_note" in rep_m1) == ("still_unread" in rep_m1),
          "the two travel together and only together")


    # ----------------------------------------------------------------------
    # 12. PREFIX-DERIVED PROGRAM FILTER. `kneat`'s boundary is a folder path, so its filter
    #     VALUES are re-derived by prefix search at every startup instead of stored. The
    #     dangerous branch is the one where derivation yields nothing: an empty values list is
    #     a request with NO program scope, and this source's whole population is other
    #     programmes' documents. These assertions exist because the live check that found this
    #     safe is not repeatable, and the shared fixture has no prefix source -- so without
    #     them the path ships with zero offline coverage.
    # ----------------------------------------------------------------------
    print("\n12. prefix-derived program filter")

    import tempfile as _tempfile
    import yaml as _yaml

    def _scope_with_prefix(prefixes, source="signals", field="project_m_code.raw"):
        """A scope dir whose `source` derives its filter by prefix instead of storing it."""
        base = _yaml.safe_load(
            (pathlib.Path(__file__).resolve().parent.parent
             / "evals" / "fixtures" / "program-scope" / "MK-6070.yaml").read_text())
        cfg = base["sources"][source]
        cfg["program_filter_field"] = field
        cfg["program_filter_value_prefixes"] = list(prefixes)
        cfg["program_filter_values"] = []
        d = pathlib.Path(_tempfile.mkdtemp())
        (d / "MK-6070.yaml").write_text(_yaml.safe_dump(base, sort_keys=False))
        return d

    def _prefix_aware_stub(m, *, prefix_terms=None, prefix_fails=False, no_value_list=False):
        """Stub that answers a `searchField` prefix request separately from a plain facet one."""
        log = []

        async def stub(method, endpoint, *, payload=None, params=None):
            log.append((method, endpoint))
            if endpoint == m.EP_UPDATES:
                return {"_ok": True, "_status": {"executed": True}, "_body": UPDATES_BODY}
            if endpoint == m.EP_ACCESS:
                return {"_ok": True, "_status": {"executed": True}, "_body": ACCESS_BODY}
            if endpoint == m.EP_CONFIG:
                return {"_ok": True, "_status": {"executed": True}, "_body": CONFIG_BODY}
            if endpoint == m.EP_TAXONOMY:
                return {"_ok": True, "_status": {"executed": True}, "_body": TAXONOMY_BODY}
            if "/data-source/" in endpoint:
                ds = endpoint.rsplit("/", 1)[-1]
                return {"_ok": True, "_status": {"executed": True},
                        "_body": ingest_body(ds, strict=ds in set(IN_SCOPE))}
            if endpoint == m.EP_FACETS:
                # A prefix request is the one carrying `searchField`. Answering both from one
                # body would make this section pass on the ordinary facet response and prove
                # nothing about the derivation.
                if (payload or {}).get("searchField"):
                    if prefix_fails:
                        return {"_ok": False,
                                "_status": {"executed": False, "reason": "timeout"}}
                    if no_value_list:
                        return {"_ok": True, "_status": {"executed": True}, "_body": {"facets": {}}}
                    field = payload["searchField"]
                    return {"_ok": True, "_status": {"executed": True},
                            "_body": {"facets": {field: [{"term": t, "count": 1}
                                                         for t in (prefix_terms or [])]}}}
                ds = (payload or {}).get(m.F_DATASOURCES, ["?"])[0]
                return {"_ok": True, "_status": {"executed": True}, "_body": facets_body(ds)}
            raise AssertionError(f"stub reached an unexpected endpoint: {endpoint}")

        m._request = stub
        return log

    _real_scope_dir = os.environ["ICS_PROGRAM_SCOPE_DIR"]

    # 12a. derivation succeeds -> the derived terms ARE the filter
    os.environ["ICS_PROGRAM_SCOPE_DIR"] = str(
        _scope_with_prefix(["MK-6070", "HPN328"]))
    fresh(m)
    log_a = _prefix_aware_stub(m, prefix_terms=["MK-6070 (A-PV)", "MK-6070 (B-AMV)"])
    c_p = asyncio.run(m.warm_cache("MK-6070"))
    sp = c_p["sources"]["signals"]
    check("a prefix-configured source derives its filter values from the field's own terms",
          list((sp.get("program_filter") or {}).values())[0]
          == ["MK-6070 (A-PV)", "MK-6070 (B-AMV)"],
          str(list((sp.get("program_filter") or {}).values())[0]))
    check("  ... one prefix search per configured prefix, and no more",
          sum(1 for _, e in log_a if e == m.EP_FACETS) == len(IN_SCOPE) + 2,
          f"{sum(1 for _, e in log_a if e == m.EP_FACETS)} facet calls for "
          f"{len(IN_SCOPE)} sources + 2 prefixes")
    check("  ... and the derivation is recorded, not just applied",
          (sp.get("filter_values_derived") or {}).get("term_count") == 2
          and (sp.get("filter_values_derived") or {}).get("complete") is True,
          str(sp.get("filter_values_derived")))
    est_a, _ = m._population_established({"program_filter": sp["program_filter"]}, sp)
    check("  ... `population_established` is True on the DERIVATION, not on a circular "
          "comparison against the list it came from",
          est_a is True, repr(est_a))

    # 12b. THE DANGEROUS ONE. Prefix matches nothing -> empty filter -> the source must be
    #      refused, never sent. An unscoped request here returns other programmes' documents
    #      under a programme-scoped label.
    os.environ["ICS_PROGRAM_SCOPE_DIR"] = str(_scope_with_prefix(["NO-SUCH-FOLDER"]))
    fresh(m)
    _prefix_aware_stub(m, prefix_terms=[])
    c_z = asyncio.run(m.warm_cache("MK-6070"))
    sz = c_z["sources"]["signals"]
    check("a prefix matching nothing leaves the filter EMPTY rather than unset or widened",
          list((sz.get("program_filter") or {}).values()) == [[]],
          str(list((sz.get("program_filter") or {}).values())))
    _plan_z = m.plan_source_request(c_z, "signals")
    check("  ... and that empty filter REFUSES the search instead of sending it unscoped",
          _plan_z.get("refused") is True and _plan_z.get("cause") == "no_program_filter",
          f"refused={_plan_z.get('refused')} cause={_plan_z.get('cause')}")
    check("  ... and the exclusion is reported, not silent",
          any("filter_prefix:signals" in str(d) for d in (c_z.get("degraded") or [])),
          str(c_z.get("degraded"))[:120])
    est_z, why_z = m._population_established({"program_filter": sz["program_filter"]}, sz)
    check("  ... `population_established` is False -- affirmative evidence, not unknown",
          est_z is False and "prefix" in (why_z or ""), repr(est_z))

    # 12c. the prefix search itself fails -> UNKNOWN, never False and never a partial filter
    os.environ["ICS_PROGRAM_SCOPE_DIR"] = str(_scope_with_prefix(["MK-6070"]))
    fresh(m)
    _prefix_aware_stub(m, prefix_fails=True)
    c_f = asyncio.run(m.warm_cache("MK-6070"))
    sf = c_f["sources"]["signals"]
    check("a failed prefix search records `complete: False`",
          (sf.get("filter_values_derived") or {}).get("complete") is False,
          str(sf.get("filter_values_derived")))
    est_f, _ = m._population_established({"program_filter": sf["program_filter"]}, sf)
    check("  ... and `population_established` is None -- unknown, never False",
          est_f is None, repr(est_f))

    # 12d. HTTP 200 carrying no value list is unknown too, not an empty derivation
    os.environ["ICS_PROGRAM_SCOPE_DIR"] = str(_scope_with_prefix(["MK-6070"]))
    fresh(m)
    _prefix_aware_stub(m, no_value_list=True)
    c_n = asyncio.run(m.warm_cache("MK-6070"))
    sn = c_n["sources"]["signals"]
    check("HTTP 200 with no value list for the field is UNKNOWN, not an empty prefix result",
          (sn.get("filter_values_derived") or {}).get("complete") is False,
          str(sn.get("filter_values_derived")))

    # 12e. a source with NO prefixes configured is untouched by any of this
    os.environ["ICS_PROGRAM_SCOPE_DIR"] = _real_scope_dir
    fresh(m)
    install_stubs(m)
    c_np = asyncio.run(m.warm_cache("MK-6070"))
    check("a source with no configured prefixes carries no derivation record at all",
          all("filter_values_derived" not in c_np["sources"][s] for s in IN_SCOPE),
          str([s for s in IN_SCOPE if "filter_values_derived" in c_np["sources"][s]]))

    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} assertions passed")
    if FAILURES:
        print("FAILED: " + ", ".join(FAILURES))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
