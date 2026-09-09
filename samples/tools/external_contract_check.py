#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx>=0.27",
# ]
# ///
"""Offline checks for the trimmed literature connector's shared boundaries."""

from __future__ import annotations

import asyncio
import gzip
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parent / "ics_public_science"
sys.path.insert(0, str(ROOT))

from contracts import normalize_doi  # noqa: E402
from disclosure import (  # noqa: E402
    AttemptBudgetExceeded,
    DisclosureBlocked,
    DisclosureConfigurationError,
    DisclosureTracker,
)
from envelopes import failure, stages, validate_adapter_metadata, warning  # noqa: E402
from http_client import BASE_URLS, HttpResult, ProviderHttpClient, status_failure  # noqa: E402
from validation import (  # noqa: E402
    QuerySyntaxError,
    compile_boolean,
    parse_normal_query,
    unique_identities,
)


CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(message)


def rejects(callable_: Any, error: type[Exception]) -> None:
    try:
        callable_()
    except error:
        check(True, f"rejected with {error.__name__}")
    else:
        check(False, f"expected {error.__name__}")


def check_queries() -> None:
    implicit = parse_normal_query("target turnover variability")
    check(
        compile_boolean(
            implicit,
            provider="pubmed",
            target="title_abstract",
        )
        == "((target[Title/Abstract] AND turnover[Title/Abstract]) AND variability[Title/Abstract])",
        "implicit AND compilation changed",
    )
    explicit = parse_normal_query(
        '"target mediated clearance" AND (variability OR exposure)'
    )
    check(
        compile_boolean(explicit, provider="europe_pmc", target="title")
        == '(TITLE:"target mediated clearance" AND (TITLE:variability OR TITLE:exposure))',
        "Europe PMC Boolean compilation changed",
    )
    check(
        compile_boolean(explicit, provider="pubmed", target="all_fields")
        == (
            '("target mediated clearance"[All Fields] AND '
            '("variability"[All Fields] OR "exposure"[All Fields]))'
        ),
        "exact PubMed all-fields compilation changed",
    )
    rejects(lambda: parse_normal_query("title:private"), QuerySyntaxError)
    rejects(lambda: parse_normal_query("alpha||beta"), QuerySyntaxError)
    rejects(lambda: parse_normal_query("alpha&&beta"), QuerySyntaxError)
    rejects(lambda: parse_normal_query("!alpha"), QuerySyntaxError)
    rejects(lambda: parse_normal_query(r"alpha\beta"), QuerySyntaxError)
    rejects(lambda: parse_normal_query('"unclosed'), QuerySyntaxError)
    rejects(lambda: parse_normal_query("alpha AND"), QuerySyntaxError)
    rejects(
        lambda: parse_normal_query("(" * 17 + "alpha" + ")" * 17),
        QuerySyntaxError,
    )
    rejects(
        lambda: parse_normal_query(" ".join(["alpha"] * 65)),
        QuerySyntaxError,
    )
    check(
        compile_boolean(
            parse_normal_query("CD4+ T-cell PK/PD"),
            provider="europe_pmc",
            target="title_abstract",
        )
        == r"((TITLE_ABS:CD4\+ AND TITLE_ABS:T\-cell) AND TITLE_ABS:PK\/PD)",
        "Europe PMC term escaping changed",
    )
    rejects(
        lambda: compile_boolean(
            parse_normal_query("alpha"),
            provider="crossref",
            target="title",
        ),
        QuerySyntaxError,
    )
    rejects(
        lambda: compile_boolean(
            parse_normal_query("alpha"),
            provider="pubmed",
            target="body",
        ),
        QuerySyntaxError,
    )


def check_value_normalization() -> None:
    check(
        normalize_doi("https://doi.org/10.1000/XYZ") == "10.1000/xyz",
        "DOI normalization changed",
    )
    rejects(lambda: normalize_doi("not-a-doi"), ValueError)
    identities = unique_identities(
        [
            {"kind": "pmid", "value": "123"},
            {"kind": "pmid", "value": "123"},
            {"kind": "pmcid", "value": "PMC456"},
        ]
    )
    check(len(identities) == 2, "identity deduplication changed")


def check_disclosure() -> None:
    tracker = DisclosureTracker(attempt_ceiling=1)
    tracker.preflight({"query": "public mechanism study"})
    tracker.record_call(["query"])
    result = tracker.result()
    check(result["decision"] == "permitted", "public query was not permitted")
    check(result["outbound_calls"] == 1, "outbound call was not counted")
    check(
        result["blocked_rule_ids"] == [],
        "permitted disclosure retained blocked rule categories",
    )
    rejects(lambda: tracker.record_call(["query"]), AttemptBudgetExceeded)

    blocked = DisclosureTracker()
    rejects(
        lambda: blocked.preflight(
            {
                "query": (
                    "experiment EXP22000794 and internal "
                    "batch number 4471727-A"
                )
            }
        ),
        DisclosureBlocked,
    )
    check(blocked.result()["decision"] == "blocked", "blocked state changed")
    check(
        blocked.result()["outbound_calls"] == 0,
        "blocked disclosure counted a provider call",
    )
    check(
        blocked.result()["blocked_rule_ids"]
        == ["batch-lot-id", "internal-experiment-id"],
        "blocked disclosure lost its safe sorted rule categories",
    )
    rejects(
        lambda: blocked.preflight({"query": "public mechanism"}),
        RuntimeError,
    )
    rejects(lambda: blocked.record_call([]), RuntimeError)

    previous = os.environ.get("ICS_DISCLOSURE_DENY_FILE")
    with tempfile.TemporaryDirectory() as directory:
        policy = Path(directory) / "unsafe-regex.txt"
        try:
            for pattern in (
                "(a+)+$",
                "(a*)(a*)(a*)(a*)(a*)Z",
                "a*Z",
                "a|b",
                "^(?:a|b)*Z",
                "(" * 2_000 + "x" + ")" * 2_000,
            ):
                policy.write_text(pattern, encoding="utf-8")
                os.environ["ICS_DISCLOSURE_DENY_FILE"] = str(policy)
                rejects(DisclosureTracker, DisclosureConfigurationError)
            policy.write_text(
                "\n".join(f"operator-secret-{index}" for index in range(257)),
                encoding="utf-8",
            )
            rejects(DisclosureTracker, DisclosureConfigurationError)
            policy.write_text("^a*Z$", encoding="utf-8")
            anchored = DisclosureTracker()
            rejects(
                lambda: anchored.preflight({"query": "aaaZ"}),
                DisclosureBlocked,
            )
        finally:
            if previous is None:
                os.environ.pop("ICS_DISCLOSURE_DENY_FILE", None)
            else:
                os.environ["ICS_DISCLOSURE_DENY_FILE"] = previous


def check_transport_boundary() -> None:
    check(
        set(BASE_URLS) == {"pubmed", "europe_pmc"},
        "transport allowlist contains a removed provider",
    )
    check(
        ProviderHttpClient._url("pubmed", "esearch.fcgi").startswith(
            "https://eutils.ncbi.nlm.nih.gov/"
        ),
        "PubMed URL changed",
    )
    check(
        ProviderHttpClient._url("europe_pmc", "search").startswith(
            "https://www.ebi.ac.uk/europepmc/"
        ),
        "Europe PMC URL changed",
    )
    rejects(
        lambda: ProviderHttpClient._url("crossref", "works"),
        ValueError,
    )
    rejects(
        lambda: ProviderHttpClient._url("pubmed", "../escape"),
        ValueError,
    )

    check(
        status_failure(
            HttpResult(200, b"{}", "application/json", {}, 2)
        )
        is None,
        "HTTP success classification changed",
    )
    throttled = status_failure(
        HttpResult(429, b"", "application/json", {}, 0)
    )
    check(
        throttled is not None
        and throttled[0] == "throttled"
        and throttled[2] is True,
        "HTTP throttling classification changed",
    )


async def check_transport_execution() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/cross-host"):
            return httpx.Response(
                302,
                headers={"location": "https://evil.example/content"},
            )
        if request.url.path.endswith("/same-host"):
            return httpx.Response(302, headers={"location": "/limited"})
        if request.url.path.endswith("/declared-large"):
            return httpx.Response(
                200,
                headers={"content-length": "100"},
                content=b"x",
            )
        if request.url.path.endswith("/streamed-large"):
            return httpx.Response(200, content=b"x" * 33)
        if request.url.path.endswith("/encoded"):
            encoded = gzip.compress(b"x" * (5 * 1024 * 1024))
            return httpx.Response(
                200,
                content=encoded,
                headers={
                    "content-encoding": "gzip",
                    "content-length": str(len(encoded)),
                },
            )
        return httpx.Response(429, json={"message": "slow down"})

    client = ProviderHttpClient(transport=httpx.MockTransport(handler))
    redirected = await client.get("pubmed", "cross-host", max_bytes=100)
    check(
        redirected.failure_code == "redirect_blocked"
        and redirected.network_bytes == 0
        and status_failure(redirected)[2] is False,
        "cross-host redirect was not blocked",
    )
    declared = await client.get("pubmed", "declared-large", max_bytes=32)
    check(
        declared.failure_code == "response_too_large"
        and declared.body == b"",
        "declared oversized response was not stopped",
    )
    streamed = await client.get("pubmed", "streamed-large", max_bytes=32)
    check(
        streamed.failure_code == "response_too_large"
        and streamed.body == b"",
        "streamed oversized response was not stopped",
    )
    encoded = await client.get("pubmed", "encoded", max_bytes=32_768)
    check(
        encoded.failure_code == "unexpected_content_encoding"
        and encoded.network_bytes == 0,
        "an encoded response was decompressed before rejection",
    )

    tracker = DisclosureTracker(attempt_ceiling=1)
    tracker.preflight({"query": "public"})
    try:
        await client.get(
            "pubmed",
            "same-host",
            max_bytes=100,
            before_attempt=lambda: tracker.record_call(["query"]),
        )
    except AttemptBudgetExceeded:
        check(
            tracker.result()["outbound_calls"] == 1,
            "redirect attempt accounting changed",
        )
    else:
        check(False, "same-host redirect bypassed the attempt ceiling")

    old = os.environ.get("ICS_CA_BUNDLE")
    os.environ["ICS_CA_BUNDLE"] = "/definitely/not/a/ca/bundle"
    try:
        unavailable = await client.get("pubmed", "limited", max_bytes=100)
        check(
            unavailable.failure_code == "configuration_unavailable"
            and unavailable.network_bytes == 0,
            "invalid configured CA bundle did not fail closed",
        )
    finally:
        if old is None:
            os.environ.pop("ICS_CA_BUNDLE", None)
        else:
            os.environ["ICS_CA_BUNDLE"] = old

    with tempfile.NamedTemporaryFile("w", delete=False) as invalid_ca:
        invalid_ca.write("not a certificate")
        invalid_ca_path = invalid_ca.name
    os.environ["ICS_CA_BUNDLE"] = invalid_ca_path
    try:
        unavailable = await client.get("pubmed", "limited", max_bytes=100)
        check(
            unavailable.failure_code == "configuration_unavailable"
            and unavailable.network_bytes == 0,
            "an existing malformed CA bundle did not fail closed",
        )
    finally:
        Path(invalid_ca_path).unlink()
        if old is None:
            os.environ.pop("ICS_CA_BUNDLE", None)
        else:
            os.environ["ICS_CA_BUNDLE"] = old

    configuration = status_failure(
        HttpResult(
            0,
            b"",
            "",
            {},
            0,
            "configuration_unavailable",
            "bad CA",
        )
    )
    check(
        configuration is not None and configuration[2] is False,
        "configuration failures must not be marked retryable",
    )
    timeout = status_failure(
        HttpResult(
            408,
            b"",
            "application/json",
            {},
            0,
        )
    )
    check(
        timeout is not None
        and timeout[0] == "timeout"
        and timeout[2] is True,
        "HTTP 408 was not classified as a retryable timeout",
    )


def check_adapter_metadata() -> None:
    value = {
        "stages": stages(
            validation="succeeded",
            disclosure="not_requested",
            request_build="succeeded",
            transport="succeeded",
            provider_body="succeeded",
            identity_or_query_fidelity="succeeded",
            projection_or_selection="succeeded",
            result="succeeded",
            currentness="succeeded",
        ),
        "failures": [],
        "warnings": [
            warning(
                "provider_rank_local_only",
                "records",
                "rank is local to one provider",
            )
        ],
        "currentness": {"observations": [], "drift": "unknown"},
    }
    validate_adapter_metadata(value)
    missing_result_stage = {
        **value,
        "stages": {
            name: state
            for name, state in value["stages"].items()
            if name != "result"
        },
    }
    rejects(
        lambda: validate_adapter_metadata(missing_result_stage),
        ValueError,
    )
    value["failures"].append(
        failure(
            "provider_error_body",
            "provider_body",
            "fixture",
            "provider error",
        )
    )
    validate_adapter_metadata(value)
    missing_absence = {
        **value,
        "failures": [
            {
                name: field_value
                for name, field_value in value["failures"][0].items()
                if name != "absence"
            }
        ],
    }
    rejects(
        lambda: validate_adapter_metadata(missing_absence),
        ValueError,
    )


def main() -> None:
    check_queries()
    check_value_normalization()
    check_disclosure()
    check_transport_boundary()
    asyncio.run(check_transport_execution())
    check_adapter_metadata()
    # Parsed by contract_map_check.py, which looks for a line containing
    # "assertions passed" and reads passed/total off it. This harness FAILS FAST -- a
    # failing assertion raises out of main() and this line never prints at all -- so
    # reached and passed are the same number by construction. The total is pinned in
    # contract_map_check.py's EXTERNAL_HARNESSES, deliberately in ONE place: pinning a
    # number twice is only as good as the arithmetic beside it, and that has been wrong
    # here twice.
    print(f"{CHECKS}/{CHECKS} assertions passed  (contract)")


if __name__ == "__main__":
    main()
