"""Allowlisted, bounded HTTP transport for private provider adapters."""

from __future__ import annotations

import json
import os
import ssl
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import httpx


BASE_URLS = {
    "pubmed": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/",
    "europe_pmc": "https://www.ebi.ac.uk/europepmc/webservices/rest/",
}
_ALLOWED_HOSTS = {urlparse(value).hostname for value in BASE_URLS.values()}


def _tls_verification() -> Any:
    bundle = os.environ.get("ICS_CA_BUNDLE", "").strip()
    if bundle:
        if not os.path.isfile(bundle):
            raise OSError("ICS_CA_BUNDLE does not identify a readable file")
        return ssl.create_default_context(cafile=bundle)
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        return True


@dataclass(frozen=True, slots=True)
class HttpResult:
    status: int
    body: bytes
    content_type: str
    headers: dict[str, str]
    network_bytes: int
    failure_code: str | None = None
    failure_detail: str | None = None
    redirects: tuple[str, ...] = ()

    def json(self) -> Any:
        return json.loads(self.body)

    def text(self) -> str:
        return self.body.decode("utf-8")


class ProviderHttpClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.timeout = httpx.Timeout(timeout_seconds, connect=10.0)
        self.transport = transport
        contact = os.environ.get("ICS_EXTERNAL_CONTACT", "").strip()
        self.user_agent = (
            f"ics-program-companion/2.0 (+{contact})"
            if contact
            else "ics-program-companion/2.0"
        )

    @staticmethod
    def _url(provider: str, path: str) -> str:
        if provider not in BASE_URLS:
            raise ValueError(f"provider {provider!r} is not allowlisted")
        if "\\" in path or ".." in path.split("/"):
            raise ValueError("provider path is unsafe")
        base = BASE_URLS[provider]
        url = urljoin(base, path.lstrip("/"))
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_HOSTS:
            raise ValueError("provider URL left the allowlist")
        return url

    async def get(
        self,
        provider: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        accept: str = "application/json",
        max_bytes: int = 2 * 1024 * 1024,
        before_attempt: Callable[[], None] | None = None,
    ) -> HttpResult:
        url = self._url(provider, path)
        original_params = dict(params) if params is not None else None
        redirects: list[str] = []
        headers = {
            "User-Agent": self.user_agent,
            "Accept": accept,
            "Accept-Encoding": "identity",
        }
        original_host = urlparse(url).hostname
        try:
            verification = _tls_verification()
        except (OSError, ssl.SSLError):
            return HttpResult(
                0,
                b"",
                "",
                {},
                0,
                "configuration_unavailable",
                "the configured CA bundle could not be loaded",
            )
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                verify=verification,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                for _ in range(3):
                    if before_attempt is not None:
                        before_attempt()
                    async with client.stream(
                        "GET",
                        url,
                        params=params,
                        headers=headers,
                    ) as response:
                        if response.is_redirect:
                            location = response.headers.get("location", "")
                            redirected = urljoin(url, location)
                            parsed = urlparse(redirected)
                            if (
                                parsed.scheme != "https"
                                or parsed.hostname not in _ALLOWED_HOSTS
                                or parsed.hostname != original_host
                            ):
                                return HttpResult(
                                    response.status_code,
                                    b"",
                                    "",
                                    {},
                                    0,
                                    "redirect_blocked",
                                    "provider redirect left the endpoint allowlist",
                                    tuple(redirects),
                                )
                            redirects.append(parsed.path)
                            url = redirected
                            params = None if parsed.query else original_params
                            continue
                        content_encoding = response.headers.get(
                            "content-encoding", ""
                        ).strip().casefold()
                        if content_encoding not in {"", "identity"}:
                            return HttpResult(
                                response.status_code,
                                b"",
                                response.headers.get("content-type", ""),
                                {},
                                0,
                                "unexpected_content_encoding",
                                "provider ignored the identity encoding requirement",
                                tuple(redirects),
                            )
                        declared = response.headers.get("content-length")
                        try:
                            declared_size = int(declared) if declared else None
                        except ValueError:
                            declared_size = None
                        if declared_size is not None and declared_size > max_bytes:
                            return HttpResult(
                                response.status_code,
                                b"",
                                response.headers.get("content-type", ""),
                                {},
                                0,
                                "response_too_large",
                                "provider response exceeded the byte ceiling",
                                tuple(redirects),
                            )
                        chunks: list[bytes] = []
                        size = 0
                        async for chunk in response.aiter_raw():
                            size += len(chunk)
                            if size > max_bytes:
                                return HttpResult(
                                    response.status_code,
                                    b"",
                                    response.headers.get("content-type", ""),
                                    {},
                                    size,
                                    "response_too_large",
                                    "provider response exceeded the byte ceiling",
                                    tuple(redirects),
                                )
                            chunks.append(chunk)
                        selected_headers = {
                            key: value[:300]
                            for key, value in response.headers.items()
                            if key.lower()
                            in {
                                "content-type",
                                "content-length",
                                "etag",
                                "last-modified",
                                "content-encoding",
                                "retry-after",
                                "x-request-id",
                            }
                        }
                        return HttpResult(
                            response.status_code,
                            b"".join(chunks),
                            response.headers.get("content-type", ""),
                            selected_headers,
                            size,
                            redirects=tuple(redirects),
                        )
                return HttpResult(
                    0,
                    b"",
                    "",
                    {},
                    0,
                    "redirect_blocked",
                    "provider returned too many redirects",
                    tuple(redirects),
                )
        except httpx.TimeoutException:
            return HttpResult(0, b"", "", {}, 0, "timeout", "provider request timed out")
        except httpx.ConnectError as exc:
            text = str(exc)
            code = (
                "tls_failure"
                if "CERTIFICATE_VERIFY_FAILED" in text or "SSL" in text
                else "connection_failure"
            )
            return HttpResult(0, b"", "", {}, 0, code, "provider connection failed")
        except ssl.SSLError:
            return HttpResult(
                0,
                b"",
                "",
                {},
                0,
                (
                    "configuration_unavailable"
                    if os.environ.get("ICS_CA_BUNDLE", "").strip()
                    else "tls_failure"
                ),
                "TLS configuration could not be loaded",
            )
        except httpx.HTTPError:
            return HttpResult(
                0,
                b"",
                "",
                {},
                0,
                "connection_failure",
                "provider transport failed",
            )


def status_failure(result: HttpResult) -> tuple[str, str, bool] | None:
    if result.failure_code:
        return (
            result.failure_code,
            result.failure_detail or "transport failed",
            result.failure_code in {"connection_failure", "dns_failure", "timeout"},
        )
    if 200 <= result.status < 300:
        return None
    code = {
        400: "provider_invalid_request",
        401: "provider_forbidden",
        403: "provider_forbidden",
        404: "provider_not_found",
        408: "timeout",
        413: "response_too_large",
        429: "throttled",
    }.get(
        result.status,
        "provider_server_error" if result.status >= 500 else "provider_invalid_request",
    )
    return (
        code,
        f"provider returned HTTP {result.status}",
        result.status in {408, 429} or result.status >= 500,
    )
