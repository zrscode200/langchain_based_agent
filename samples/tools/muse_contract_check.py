#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx>=0.27",
#   "pyyaml>=6.0",
#   "truststore>=0.9",
# ]
# ///
"""Assert that MUSE actually applies what `ics_muse/server.py` sends.

WHY THIS EXISTS
---------------
Three request field names in this connector were once wrong — `datasources`
instead of `dataSources`, `searchMode` instead of `queryType`, and a
`{must/should/mustNot}` envelope where MUSE wanted `{conditions: [...]}`. Every
one of them returned **HTTP 200 with plausible records** while the intent was
discarded: source selection fell back to a default set, and conditional search
returned the entire program population labelled as a precise fielded retrieval.

No amount of reading catches that. The only thing that does is a control which
asserts an impossible request returns *nothing*. That is what this script is.

It talks to MUSE directly rather than through the MCP layer, so a failure here
is unambiguously the wire contract and not the adapter or the agent.

USAGE
-----
    python3 auth/muse_session.py check
    python3 tools/muse_contract_check.py      # add --program to change program

Exit status is 0 only if every control holds.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml

IMPOSSIBLE = "__ICS_CONTRACT_CHECK_IMPOSSIBLE__"


def _verify() -> Any:
    """Prefer the OS trust store; corporate TLS interception re-signs this host."""
    bundle = os.environ.get("ICS_CA_BUNDLE", "").strip()
    if bundle and os.path.isfile(bundle):
        return bundle
    try:
        import ssl

        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # noqa: BLE001
        return True


def _session_cookie() -> str:
    default = (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        / "ics-companion" / "secrets" / "muse-qa-session"
    )
    path = Path(os.environ.get("ICS_MUSE_SESSION_FILE", str(default)))
    if not path.is_file():
        sys.exit(f"No MUSE SESSION cookie at {path}. Follow auth/README.md first.")
    session = path.read_text(encoding="utf-8").strip()
    if not session:
        sys.exit(f"MUSE SESSION cookie file {path} is empty.")
    return session


def _scope(program: str) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    path = root / "config" / "program-scope" / f"{program}.yaml"
    if not path.is_file():
        sys.exit(f"No scope mapping at {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--program", default=os.environ.get("ICS_PROGRAM", "MK-6070"))
    ap.add_argument(
        "--base", default=os.environ.get("ICS_MUSE_BASE_URL", "https://qa.muse.merck.com")
    )
    args = ap.parse_args()

    scope = _scope(args.program)
    project = scope.get("muse_project") or os.environ.get("ICS_MUSE_PROJECT") or "muse"
    sources = {
        name: cfg
        for name, cfg in (scope.get("sources") or {}).items()
        if isinstance(cfg, dict) and cfg.get("enabled", True) and cfg.get("program_filter_field")
    }
    if not sources:
        sys.exit(f"No scopeable sources for {args.program}")

    session = _session_cookie()
    base = args.base.rstrip("/")
    client = httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0), verify=_verify())
    headers = {
        "Cookie": f"SESSION={session}",
        "Content-Type": "application/json",
        "X-Client-App": "ics-ddt-contract",
    }

    def post(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        r = client.post(f"{base}{endpoint}", json=payload, headers=headers)
        if r.status_code == 401:
            sys.exit("HTTP 401 — authentication failed. Follow auth/README.md.")
        if r.status_code != 200:
            return {"_http": r.status_code, "_body": r.text[:300]}
        return r.json()

    def total(body: dict[str, Any]) -> int | None:
        if "_http" in body:
            return None
        for key in ("totalHits", "total", "hitCount"):
            if key in body:
                return body[key]
        return None

    failures: list[str] = []
    checks = 0

    def check(label: str, ok: bool, detail: str) -> None:
        nonlocal checks
        checks += 1
        print(f"  {'PASS' if ok else 'FAIL'}  {label}: {detail}")
        if not ok:
            failures.append(label)

    def filters(cfg: dict[str, Any]) -> dict[str, Any]:
        vals = cfg.get("program_filter_values") or []
        return {cfg["program_filter_field"]: [vals] if isinstance(vals, str) else vals}

    print(f"program={args.program}  project={project}  base={base}\n")

    # --- 1. Datasource names are real API ids, not display labels -----------
    # A display label 404s with "Unknown datasource". Silence here would mean
    # `dataSources` is being ignored and a default set searched instead.
    print("datasource identity")
    per_source_total: dict[str, int | None] = {}
    for name, cfg in sources.items():
        body = post(
            "/resources/v2/query",
            {
                "project": project,
                "query": (cfg.get("program_filter_values") or [args.program])[0],
                "dataSources": [name],
                "queryType": "basic",
                "limit": 1,
                "formParams": filters(cfg),
            },
        )
        t = total(body)
        per_source_total[name] = t
        check(
            f"{name} resolves",
            t is not None,
            f"totalHits={t}" if t is not None else f"HTTP {body.get('_http')} {body.get('_body','')[:80]}",
        )

    # Distinct sources must not all report the same population. Identical totals
    # across every source is the signature of `dataSources` being ignored.
    known = [t for t in per_source_total.values() if isinstance(t, int)]
    if len(known) > 1:
        check(
            "sources are distinguishable",
            len(set(known)) > 1,
            f"totals={per_source_total}",
        )

    # --- 2. Conditions are actually applied --------------------------------
    # THE control. An impossible MUST must return zero. If it returns the
    # program population, conditions are being discarded and every "precise"
    # retrieval is a lie.
    print("\nconditional application")
    for name, cfg in sources.items():
        fields = [f for f in (cfg.get("searchable_fields") or []) if f != cfg["program_filter_field"]]
        if not fields:
            continue
        field = "title" if "title" in fields else fields[0]
        base_payload = {
            "project": project,
            "dataSources": [name],
            "queryType": "conditional",
            "limit": 1,
            "formParams": filters(cfg),
        }
        impossible = post(
            "/resources/v2/query/conditional",
            {
                **base_payload,
                "conditionQuery": {
                    "conditions": [
                        {"type": "MUST", "fields": [field], "text": {"text": IMPOSSIBLE}}
                    ]
                },
            },
        )
        t = total(impossible)
        check(
            f"{name}: impossible MUST returns zero",
            t == 0,
            f"totalHits={t} (expected 0; a nonzero value means conditions are ignored)",
        )

    # --- 3. MUST_NOT complements MUST --------------------------------------
    # Exclusion is the capability the design records as never verified, and the
    # arithmetic is self-checking: MUST + MUST_NOT on one term must equal the
    # unconditioned population.
    print("\nexclusion arithmetic")
    name, cfg = next(iter(sources.items()))
    fields = cfg.get("searchable_fields") or []
    if "title" in fields:
        base_payload = {
            "project": project,
            "dataSources": [name],
            "queryType": "conditional",
            "limit": 1,
            "formParams": filters(cfg),
        }
        term = "Program"

        def cond(ctype: str) -> int | None:
            return total(
                post(
                    "/resources/v2/query/conditional",
                    {
                        **base_payload,
                        "conditionQuery": {
                            "conditions": [
                                {"type": ctype, "fields": ["title"], "text": {"text": term}}
                            ]
                        },
                    },
                )
            )

        inc, exc = cond("MUST"), cond("MUST_NOT")
        whole = per_source_total.get(name)
        ok = all(isinstance(v, int) for v in (inc, exc, whole)) and inc + exc == whole
        check(
            f"{name}: MUST + MUST_NOT == population",
            ok,
            f"{inc} + {exc} == {whole}",
        )

    client.close()
    print(f"\n{checks - len(failures)}/{checks} controls passed")
    if failures:
        print("FAILED: " + ", ".join(failures))
        print(
            "\nA failure here means MUSE is not applying what this connector sends. "
            "Compare the request against GET {base}/v3/api-docs before changing "
            "anything in the adapter.".format(base=base)
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
