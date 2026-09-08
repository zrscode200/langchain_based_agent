"""Enterprise gateway adapters over OG's pinned model interfaces.

Adapted from zrscode200-update d57a0ea1d10ac53093e51c4011cddf075797eda4.
Metadata and tests are reconstructed; no live gateway validation is claimed.
"""

from __future__ import annotations

import contextlib
import json
import logging
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

import requests
from urllib3.exceptions import ReadTimeoutError
from langchain_core.language_models import BaseChatModel
from langchain_core.exceptions import ModelError
from deepagents_code.config import active_environment
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    ToolMessage as LCToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from langchain_core.callbacks import CallbackManagerForLLMRun
    from langchain_core.language_models import LanguageModelInput
    from langchain_core.runnables import Runnable
    from langchain_core.tools import BaseTool


class MerckEndpointError(requests.HTTPError, ModelError, RuntimeError):
    """Status-bearing error understood by both requests and host model retries."""
    @property
    def is_retryable(self):
        return self.response is not None and _is_retryable_status(self.response.status_code)


class MerckEndpointUnreachableError(requests.ConnectionError, ModelError, RuntimeError):
    """Connection failed before a response was accepted."""
    is_retryable = True


class MerckEndpointReadTimeoutError(requests.ReadTimeout, ModelError, RuntimeError):
    """Request may have been accepted; never replay it through host retries."""
    is_retryable = False


class MerckEndpointStreamError(requests.RequestException, ModelError, RuntimeError):
    """An accepted response was interrupted; never replay it, even via its cause."""
    is_retryable = False


def _is_read_timeout(exc):
    pending, seen = [exc], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, (requests.ReadTimeout, ReadTimeoutError)):
            return True
        pending.extend(value for value in (current.__cause__, current.__context__, *current.args)
                       if isinstance(value, BaseException))
    return False


def _body_failure(exc):
    if _is_read_timeout(exc):
        return MerckEndpointReadTimeoutError("Enterprise response body timed out; request was not replayed")
    return MerckEndpointStreamError("Enterprise response body was interrupted; request was not replayed")


def _read_json(response):
    try:
        return response.json()
    except requests.RequestException as exc:
        raise _body_failure(exc) from exc


class _StreamInterruptedError(RuntimeError):
    """Internal signal for an SSE error event or missing message_stop."""


_STREAM_INTERRUPTIONS: tuple[type[BaseException], ...] = (
    _StreamInterruptedError,
    requests.exceptions.ChunkedEncodingError,
    requests.ConnectionError,
    requests.Timeout,
)
"""Failures that can strike an already-accepted stream mid-flight.

`ChunkedEncodingError` is listed explicitly because it descends from
`RequestException` rather than `ConnectionError`, so the broader entries below do
not cover it. `Timeout` covers `ReadTimeout`, which -- unlike in
`_post_with_retry` -- is worth catching here: a read timeout partway through a
stream is a real interruption of output already in progress, not a request whose
fate is unknown.
"""

_REDACTED_HEADERS = frozenset(
    {"set-cookie", "authorization", "proxy-authorization", "x-merck-apikey"}
)
"""Response headers withheld from failure logs.

Every other header is logged on failure rather than a guessed allowlist: the
proxy states *which* limit a 429 hit only in headers, never in the body, and the
names it uses are not documented to us.
"""


def _warn_unknown_kwargs(cls_name: str, extra: dict[str, Any]) -> None:
    """Note constructor kwargs that will reach the endpoint as body fields.

    `_payload` spreads `model_kwargs` into the request body, so an unrecognized
    kwarg is either deliberate API passthrough or a mistyped `config.toml` key
    that will be POSTed verbatim. Only the names are logged; values may be
    credentials.
    """
    logger.warning(
        "%s: kwargs %s are not adapter fields and will be sent as request body "
        "fields (deliberate for API passthrough, a typo otherwise)",
        cls_name,
        sorted(extra),
    )


def _loggable_headers(response: requests.Response) -> dict[str, str]:
    """Summarize `response` headers for a log line, redacting credential carriers.

    Returns:
        The headers, with `_REDACTED_HEADERS` values replaced.
    """
    return {
        key: "<redacted>" if key.lower() in _REDACTED_HEADERS else value
        for key, value in response.headers.items()
    }


def _is_retryable_status(status: int) -> bool:
    """Report whether HTTP `status` is worth another attempt.

    Retries 408 (request timeout), 429 (rate limited), and every 5xx -- which
    covers the 529 "overloaded" this proxy also emits. Other 4xx are
    deterministic (bad key, malformed body, wrong URL); retrying those only
    delays a clear error behind several minutes of backoff.

    Returns:
        True when a retry could plausibly succeed.
    """
    return status in {408, 429} or 500 <= status < 600


def _retry_after_seconds(response: requests.Response) -> float | None:
    """Read `Retry-After` off `response` as a non-negative number of seconds.

    Accepts both forms the header is defined with -- a delay in seconds and an
    HTTP-date.

    Returns:
        The delay in seconds, or None when the header is absent, unparseable, or
            names a moment already past.
    """
    raw = (response.headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _retry_delay(
    attempt: int,
    retry_after: float | None,
    *,
    backoff: float,
    backoff_max: float,
) -> float:
    """Compute how long to wait before retrying a zero-indexed `attempt`.

    An explicit `Retry-After` wins, clamped to `backoff_max` so a large or
    hostile value cannot wedge a session. Otherwise the window grows
    exponentially and is drawn with *equal* jitter -- half fixed, half random.
    Full jitter (`uniform(0, window)`) can draw a near-zero wait and burn an
    attempt against a limit that has not moved; equal jitter keeps a floor while
    still decorrelating this client from every other one backing off against the
    same shared bucket.

    Returns:
        Seconds to sleep.
    """
    if retry_after is not None:
        # Floored at `backoff`: a stale or malformed HTTP-date computes to ~0, and
        # honoring that literally would fire every remaining attempt in one burst
        # at an endpoint that just refused us.
        return min(max(retry_after, backoff), backoff_max)
    window = min(backoff_max, backoff * 2**attempt)
    # Jitter only -- `random` is not used for anything security-bearing here.
    return window / 2 + random.uniform(0, window / 2)  # noqa: S311


def _post_with_retry(
    *,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
    endpoint_label: str,
    max_retries: int,
    backoff: float,
    backoff_max: float,
    total_max: float,
) -> requests.Response:
    """Return response headers, retrying only pre-body transient failures.

    Body consumption always belongs to the adapter, even for blocking calls.
    This keeps Requests' wrapped body errors on the accepted-response side of
    the retry boundary. OG disables this optional standalone loop via
    max_retries=0; its model middleware owns the hosted budget. total_max bounds
    backoff time, not network time. Failed status responses are closed here;
    callers close successful responses after consuming JSON or SSE.
    """
    attempts = max(1, max_retries + 1)
    spent = 0.0
    for attempt in range(attempts):
        try:
            response = requests.post(
                # Own body consumption even for blocking calls. requests wraps
                # a body ReadTimeoutError as ConnectionError when it eagerly
                # consumes stream=False, losing the response-acceptance boundary.
                url, headers=headers, json=payload, timeout=timeout, stream=True
            )
        except requests.ReadTimeout as exc:
            raise MerckEndpointReadTimeoutError(f"{endpoint_label} read timed out; request was not replayed") from exc
        except requests.ConnectionError as exc:
            if _is_read_timeout(exc):
                raise _body_failure(exc) from exc
            remaining = total_max - spent
            if attempt < attempts - 1 and remaining > 0:
                delay = min(
                    _retry_delay(
                        attempt, None, backoff=backoff, backoff_max=backoff_max
                    ),
                    remaining,
                )
                logger.warning(
                    "%s unreachable (%s); retrying in %.1fs (attempt %d/%d)",
                    endpoint_label,
                    exc,
                    delay,
                    attempt + 1,
                    attempts,
                )
                time.sleep(delay)
                spent += delay
                continue
            msg = f"{endpoint_label} unreachable: {exc}"
            if attempt:
                msg = f"{msg} (after {attempt + 1} attempts)"
            logger.error(msg)
            raise MerckEndpointUnreachableError(msg) from exc
        if response.ok:
            if attempt:
                logger.info(
                    "%s recovered on attempt %d/%d",
                    endpoint_label,
                    attempt + 1,
                    attempts,
                )
            return response
        # Reading `.text` drains a streamed body; close it so the connection is
        # released rather than held by a response nobody will iterate.
        try:
            detail = response.text[:2000]
        except requests.RequestException as exc:
            raise _body_failure(exc) from exc
        finally:
            response.close()
        remaining = total_max - spent
        if (
            _is_retryable_status(response.status_code)
            and attempt < attempts - 1
            and remaining > 0
        ):
            delay = min(
                _retry_delay(
                    attempt,
                    _retry_after_seconds(response),
                    backoff=backoff,
                    backoff_max=backoff_max,
                ),
                remaining,
            )
            logger.warning(
                "%s returned HTTP %d; retrying in %.1fs (attempt %d/%d). "
                "headers=%r body=%s",
                endpoint_label,
                response.status_code,
                delay,
                attempt + 1,
                attempts,
                _loggable_headers(response),
                detail,
            )
            time.sleep(delay)
            spent += delay
            continue
        if _is_retryable_status(response.status_code) and remaining <= 0:
            logger.warning(
                "%s: %.0fs retry budget spent after %d attempt(s); giving up",
                endpoint_label,
                total_max,
                attempt + 1,
            )
        msg = f"{endpoint_label} returned HTTP {response.status_code}: {detail}"
        if attempt:
            msg = f"{msg} (after {attempt + 1} attempts)"
        logger.error("%s headers=%r", msg, _loggable_headers(response))
        raise MerckEndpointError(msg, response=response)
    # Unreachable: `attempts >= 1`, and every iteration returns, continues, or
    # raises -- the last one cannot continue.
    msg = f"{endpoint_label}: retry loop exited without a result"
    raise RuntimeError(msg)


def _resolve_constructor(kwargs):
    environment = active_environment()
    if "api_key" not in kwargs:
        key_name = "GPT_API_Key"
        scoped_name = f"DEEPAGENTS_CODE_{key_name}"
        kwargs["api_key"] = environment.get(scoped_name, environment.get(key_name))
    key = kwargs.get("api_key")
    if hasattr(key, "get_secret_value"):
        key = key.get_secret_value()
    if not isinstance(key, str) or not key.strip():
        raise ValueError("Enterprise model requires a non-empty workspace API key")
    kwargs["api_key"] = key
    base_url = kwargs.pop("base_url", None)
    if base_url and kwargs.get("api_root") and base_url.rstrip("/") != kwargs["api_root"].rstrip("/"):
        raise ValueError("Enterprise model base_url and api_root conflict")
    if base_url and not kwargs.get("api_root"):
        kwargs["api_root"] = base_url


class MerckChatModel(BaseChatModel):
    """Merck chat model adapter for the custom GPT endpoint."""

    model: str
    api_model: str | None = None
    api_root: str = "https://iapi-test.merck.com/gpt/v2"
    api_version: str = "2024-12-01-preview"
    api_key: str | None = Field(default=None, repr=False)
    timeout: float = 120.0
    # OG owns retries. Standalone callers may explicitly enable this loop;
    # configure [retries.<provider>].param = "max_retries" when hosted by OG.
    max_retries: int = Field(default=0, ge=0)
    retry_backoff: float = Field(default=2.0, gt=0)
    retry_backoff_max: float = Field(default=60.0, gt=0)
    retry_total_max: float = Field(default=240.0, gt=0)
    profile: dict[str, Any] | None = Field(
        default_factory=lambda: {
            "tool_calling": True,
            "max_input_tokens": 272000,
            "max_output_tokens": 128000,
            "reasoning_output": True,
        }
    )
    model_kwargs: dict[str, Any] = Field(default_factory=dict)
    bound_tools: list[dict[str, Any]] = Field(default_factory=list)
    tool_choice: str | dict[str, Any] | None = None

    def __init__(self, **kwargs: Any) -> None:
        """Initialize the model and resolve the Merck API key."""
        _resolve_constructor(kwargs)
        known = set(type(self).model_fields)
        extra = {key: kwargs.pop(key) for key in list(kwargs) if key not in known}
        super().__init__(**kwargs)
        if extra:
            _warn_unknown_kwargs(type(self).__name__, extra)
            self.model_kwargs.update(extra)

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable | BaseTool],
        *,
        tool_choice: str | dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        """Bind OpenAI-format tool schemas for the Merck endpoint.

        Returns:
            A runnable model with tool schemas attached.
        """
        bound = self.model_copy(deep=True)
        bound.bound_tools = [convert_to_openai_tool(tool) for tool in tools]
        bound.tool_choice = tool_choice
        if kwargs:
            # Keep bind-time kwargs in LangChain's invocation lane. Core consumes
            # tracing-only structured-output metadata there before provider calls.
            return bound.bind(**kwargs)
        return cast("Runnable[LanguageModelInput, AIMessage]", bound)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,  # noqa: ARG002
        **kwargs: Any,
    ) -> ChatResult:
        """Call the Merck chat completions endpoint.

        Transient failures are retried per `_post_with_retry`.

        Returns:
            The generated chat result.

        Raises:
            MerckEndpointError: If the endpoint returns a non-2xx response that
                is not retryable, or stays non-2xx across every attempt.
            MerckEndpointUnreachableError: If the endpoint stays unreachable.
        """
        payload = self._payload(messages, stop=stop, **kwargs)
        response = _post_with_retry(
            url=self._url,
            headers=self._headers,
            payload=payload,
            timeout=self.timeout,
            endpoint_label="Merck chat endpoint",
            max_retries=self.max_retries,
            backoff=self.retry_backoff,
            backoff_max=self.retry_backoff_max,
            total_max=self.retry_total_max,
        )
        try:
            message = self._parse_message(_read_json(response))
            return ChatResult(generations=[ChatGeneration(message=message)])
        finally:
            response.close()

    @property
    def _llm_type(self) -> str:
        """Return the LangChain model type identifier."""
        return "merck-chat"

    @property
    def _url(self) -> str:
        root = self.api_root.rstrip("/")
        model = self.api_model or self.model
        return f"{root}/{model}/chat/completions?api-version={self.api_version}"

    @property
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-Merck-APIKey"] = self.api_key
        return headers

    def _payload(
        self,
        messages: list[BaseMessage],
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        safe_kwargs = {
            key: value
            for key, value in kwargs.items()
            if key
            not in {
                "run_id",
                "config",
                "callbacks",
                "tags",
                "metadata",
                "run_name",
            }
        }
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [self._convert_message(message) for message in messages],
            **self.model_kwargs,
            **safe_kwargs,
        }
        if stop:
            payload["stop"] = stop
        if self.bound_tools:
            payload["tools"] = self.bound_tools
        if self.tool_choice is not None:
            choice = self.tool_choice
            if choice == "any":
                choice = "required"
            elif isinstance(choice, str) and choice not in {"auto", "none", "required"}:
                choice = {"type": "function", "function": {"name": choice}}
            payload["tool_choice"] = choice
        return payload

    @staticmethod
    def _convert_message(message: BaseMessage) -> dict[str, Any]:
        role = _message_role(message)
        converted: dict[str, Any] = {"role": role, "content": message.content}
        if isinstance(message, AIMessage) and isinstance(message.content, list):
            converted["content"] = [block for block in message.content
                if not (isinstance(block, dict) and block.get("type") in
                        {"reasoning", "thinking", "redacted_thinking"})]
        if isinstance(message, AIMessage) and message.tool_calls:
            converted["tool_calls"] = [
                {
                    "id": tool_call.get("id"),
                    "type": "function",
                    "function": {
                        "name": tool_call["name"],
                        "arguments": json.dumps(tool_call.get("args", {})),
                    },
                }
                for tool_call in message.tool_calls
            ]
        if isinstance(message, LCToolMessage):
            converted["tool_call_id"] = message.tool_call_id
        return converted

    @staticmethod
    def _parse_message(data: dict[str, Any]) -> AIMessage:
        choice = (data.get("choices") or [{}])[0]
        raw = choice.get("message") or {}
        content = raw.get("content") or ""
        tool_calls = []
        for raw_call in raw.get("tool_calls") or []:
            function = raw_call.get("function") or {}
            arguments = function.get("arguments") or "{}"
            try:
                args = (
                    json.loads(arguments) if isinstance(arguments, str) else arguments
                )
            except json.JSONDecodeError:
                args = {"arguments": arguments}
            tool_calls.append(
                {
                    "id": raw_call.get("id"),
                    "name": function.get("name", ""),
                    "args": args,
                }
            )
        usage = data.get("usage") or {}
        usage_metadata = None
        if usage:
            usage_metadata = {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            }
        return AIMessage(
            content=content,
            tool_calls=tool_calls,
            response_metadata={"raw_response": data},
            usage_metadata=usage_metadata,
        )


def _message_role(message: BaseMessage) -> str:
    if message.type == "human":
        return "user"
    if message.type == "ai":
        return "assistant"
    if message.type == "system":
        return "system"
    if message.type == "tool":
        return "tool"
    return message.type


MerckChatOpenAI = MerckChatModel


def _sse_events(
    response: requests.Response, *, endpoint_label: str
) -> Iterator[dict[str, Any]]:
    """Yield decoded `data:` payloads from an SSE response.

    The `event:` lines are ignored: every payload repeats its own kind in a
    ``type`` field, so parsing both would mean reconciling two sources for one
    fact. An unparsable payload is logged and skipped rather than raised on --
    dropping one malformed frame degrades the reply, while aborting discards a
    reply that is otherwise arriving fine.

    Decoding happens here rather than via `iter_lines(decode_unicode=True)`, which
    defers to `response.encoding`. This proxy sends `text/event-stream` with no
    charset, so `requests` falls back to Latin-1 and every multi-byte character
    arrives doubly decoded -- a live probe returned ``17 Ã\\x97 23`` for ``17 × 23``.
    SSE is UTF-8 by specification, so decoding explicitly is both correct and
    independent of a header the proxy does not send. Newline splitting cannot
    bisect a UTF-8 sequence, so decoding per line is safe.
    """
    for line in response.iter_lines():
        raw = (
            line.decode("utf-8", errors="replace")
            if isinstance(line, bytes)
            else line
        )
        if not raw or not raw.startswith("data:"):
            continue
        payload = raw[len("data:") :].strip()
        if not payload:
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning(
                "%s: skipping unparsable SSE payload %r", endpoint_label, payload[:200]
            )
            continue
        if isinstance(event, dict):
            yield event


def _anthropic_stream_chunks(
    response: requests.Response, *, endpoint_label: str
) -> Iterator[ChatGenerationChunk]:
    """Translate SSE into native text/reasoning/tool chunks; require message_stop."""
    input_tokens = 0
    saw_stop = False
    for event in _sse_events(response, endpoint_label=endpoint_label):
        etype = event.get("type")
        if etype == "error":
            error = event.get("error") or {}
            reason = ": ".join(
                part
                for part in (error.get("type"), error.get("message"))
                if isinstance(part, str) and part
            )
            msg = f"in-stream error event ({reason or 'no detail'})"
            raise _StreamInterruptedError(msg)
        if etype == "message_start":
            usage = ((event.get("message") or {}).get("usage")) or {}
            input_tokens = usage.get("input_tokens") or 0
        elif etype == "content_block_start":
            block = event.get("content_block") or {}
            btype = block.get("type")
            if btype == "tool_use":
                # `name` and `id` ride only on this opening chunk. Chunk merging
                # concatenates same-key strings, so repeating the name on every
                # delta builds "get_weatherget_weather" -- a silently corrupted
                # tool name, verified before this was written.
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content="",
                        tool_call_chunks=[
                            {
                                "name": block.get("name", ""),
                                "args": "",
                                "id": block.get("id"),
                                "index": event.get("index", 0),
                            }
                        ],
                    )
                )
            elif btype == "redacted_thinking":
                yield _reasoning_chunk("[redacted thinking]")
        elif etype == "content_block_delta":
            delta = event.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                text = delta.get("text") or ""
                if text:
                    yield ChatGenerationChunk(message=AIMessageChunk(content=text))
            elif dtype == "thinking_delta":
                thinking = delta.get("thinking") or ""
                if thinking:
                    yield _reasoning_chunk(thinking)
            elif dtype == "input_json_delta":
                partial = delta.get("partial_json") or ""
                if partial:
                    yield ChatGenerationChunk(
                        message=AIMessageChunk(
                            content="",
                            tool_call_chunks=[
                                {
                                    "name": None,
                                    "args": partial,
                                    "id": None,
                                    "index": event.get("index", 0),
                                }
                            ],
                        )
                    )
            # `signature_delta` is dropped on purpose: `_convert_message` strips
            # thinking blocks when replaying history, so a signature would sign
            # something that is never sent back.
        elif etype == "message_delta":
            usage = event.get("usage") or {}
            output_tokens = usage.get("output_tokens") or 0
            stop_reason = (event.get("delta") or {}).get("stop_reason")
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    response_metadata={"stop_reason": stop_reason},
                    usage_metadata={
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "total_tokens": input_tokens + output_tokens,
                    },
                )
            )
        elif etype == "message_stop":
            saw_stop = True
    if not saw_stop:
        # The quiet failure: iteration finished, no exception was raised, and
        # without this the partial reply reads as a complete one.
        msg = "stream ended without message_stop"
        raise _StreamInterruptedError(msg)


def _reasoning_chunk(text: str) -> ChatGenerationChunk:
    """Wrap reasoning `text` in a chunk that carries no visible content."""
    return ChatGenerationChunk(
        message=AIMessageChunk(
            content=[{"type": "reasoning", "reasoning": text}],
        )
    )


_UNTRANSLATABLE_BIND_KWARGS = {
    "response_format": "Use langchain.agents.structured_output.ToolStrategy; this gateway does not accept provider-native response_format. OG verification and declared subagents use ToolStrategy."
}


def _reject_untranslatable_bind_kwargs(kwargs: Mapping[str, Any]) -> None:
    """Refuse bound kwargs the Anthropic Messages API cannot accept.

    Args:
        kwargs: Extra kwargs passed to `bind_tools`.

    Raises:
        ValueError: When any kwarg is in `_UNTRANSLATABLE_BIND_KWARGS`.
    """
    for name, reason in _UNTRANSLATABLE_BIND_KWARGS.items():
        if name in kwargs:
            msg = (
                f"Cannot bind {name!r} onto the Merck Anthropic adapter: {reason}"
            )
            raise ValueError(msg)


class MerckAnthropicChatModel(BaseChatModel):
    """Merck chat model adapter for Anthropic (Claude) models on the iAPI proxy.

    The proxy exposes Claude on Bedrock at the *bare model URL*
    (``{api_root}/{api_model}``) using the Anthropic Messages API body shape and
    authenticates via ``X-Merck-APIKey``. Both a blocking POST (`_generate`) and
    a server-sent-event stream (`_stream`) are supported; streaming is requested
    with a ``stream`` body field, which is the only thing that turns it on -- an
    `Accept` header or a query parameter does not.

    Only `_stream` is defined, not `_astream`: `BaseChatModel` routes `astream`
    through `_stream` on a worker thread, which is how the async consumers reach
    it.
    """

    model: str
    api_model: str | None = None
    api_root: str = "https://iapi-test.merck.com/gpt/v2"
    api_key: str | None = Field(default=None, repr=False)
    timeout: float = 300.0
    max_tokens: int = 8192
    # Host retries own the budget; explicit standalone callers may opt in here.
    max_retries: int = Field(default=0, ge=0)
    retry_backoff: float = Field(default=2.0, gt=0)
    retry_backoff_max: float = Field(default=60.0, gt=0)
    retry_total_max: float = Field(default=240.0, gt=0)
    # Gateway-specific adaptive thinking option retained from the upload.
    # Validate supported effort values against the approved company endpoint.
    thinking_effort: str | None = None
    profile: dict[str, Any] | None = Field(
        default_factory=lambda: {
            "tool_calling": True,
            "max_input_tokens": 550000,
            "max_output_tokens": 128000,
            "reasoning_output": True,
        }
    )
    model_kwargs: dict[str, Any] = Field(default_factory=dict)
    bound_tools: list[dict[str, Any]] = Field(default_factory=list)
    tool_choice: str | dict[str, Any] | None = None

    def __init__(self, **kwargs: Any) -> None:
        """Initialize the model and resolve the Merck API key."""
        _resolve_constructor(kwargs)
        known = set(type(self).model_fields)
        extra = {key: kwargs.pop(key) for key in list(kwargs) if key not in known}
        super().__init__(**kwargs)
        if extra:
            _warn_unknown_kwargs(type(self).__name__, extra)
            self.model_kwargs.update(extra)

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable | BaseTool],
        *,
        tool_choice: str | dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        """Bind tool schemas, converting to the Anthropic tool format.

        Returns:
            A runnable model with tool schemas attached.

        Raises:
            ValueError: When a bound kwarg names a field the Anthropic Messages
                API does not accept, which would otherwise reach the endpoint as
                an opaque HTTP 400.
        """
        bound = self.model_copy(deep=True)
        bound.bound_tools = [_to_anthropic_tool(tool) for tool in tools]
        bound.tool_choice = tool_choice
        if kwargs:
            _reject_untranslatable_bind_kwargs(kwargs)
            # Keep bind-time kwargs in LangChain's invocation lane. Core consumes
            # tracing-only structured-output metadata there before provider calls.
            return bound.bind(**kwargs)
        return cast("Runnable[LanguageModelInput, AIMessage]", bound)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,  # noqa: ARG002
        **kwargs: Any,
    ) -> ChatResult:
        """Call the Merck Anthropic Messages endpoint with one blocking POST.

        Transient failures are retried per `_post_with_retry`. See `_stream` for
        the incremental path, which the TUI uses.

        Returns:
            The generated chat result.

        Raises:
            MerckEndpointError: If the endpoint returns a non-2xx response that
                is not retryable, or stays non-2xx across every attempt.
            MerckEndpointUnreachableError: If the endpoint stays unreachable.
        """
        payload = self._payload(messages, stop=stop, **kwargs)
        response = _post_with_retry(
            url=self._url,
            headers=self._headers,
            payload=payload,
            timeout=self.timeout,
            endpoint_label="Merck Anthropic endpoint",
            max_retries=self.max_retries,
            backoff=self.retry_backoff,
            backoff_max=self.retry_backoff_max,
            total_max=self.retry_total_max,
        )
        try:
            message = self._parse_message(_read_json(response))
            return ChatResult(generations=[ChatGeneration(message=message)])
        finally:
            response.close()

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        """Stream once; accepted-stream failures are never automatically replayed."""
        payload = self._payload(messages, stop=stop, **kwargs)
        payload["stream"] = True
        label = "Merck Anthropic endpoint"
        response = _post_with_retry(
            url=self._url,
            headers=self._headers,
            payload=payload,
            timeout=self.timeout,
            endpoint_label=label,
            max_retries=self.max_retries,
            backoff=self.retry_backoff,
            backoff_max=self.retry_backoff_max,
            total_max=self.retry_total_max,
        )
        emitted = 0
        try:
            for chunk in _anthropic_stream_chunks(response, endpoint_label=label):
                emitted += 1
                text = chunk.message.content
                if run_manager is not None and isinstance(text, str) and text:
                    run_manager.on_llm_new_token(text, chunk=chunk)
                yield chunk
        except _STREAM_INTERRUPTIONS as exc:
            if emitted:
                msg = (
                    f"{label} stream ended early after {emitted} chunk(s) of "
                    f"the reply ({exc}). The reply is incomplete."
                )
            else:
                # Claiming a partial reply when none was emitted would send the
                # user looking for text that is not there.
                msg = f"{label} stream produced no output ({exc})"
            logger.error(msg)
            raise MerckEndpointStreamError(msg) from exc
        else:
            return
        finally:
            # Best-effort on purpose. This `finally` runs while a
            # `MerckEndpointStreamError` may be propagating, and an exception
            # raised here would replace it -- trading the message the user
            # needs for a connection-teardown detail they cannot act on.
            with contextlib.suppress(Exception):
                response.close()

    @property
    def _llm_type(self) -> str:
        """Return the LangChain model type identifier."""
        return "merck-anthropic-chat"

    @property
    def _url(self) -> str:
        root = self.api_root.rstrip("/")
        model = self.api_model or self.model
        return f"{root}/{model}"

    @property
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-Merck-APIKey"] = self.api_key
        return headers

    def _payload(
        self,
        messages: list[BaseMessage],
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        safe_kwargs = {
            key: value
            for key, value in kwargs.items()
            if key
            not in {
                "run_id",
                "config",
                "callbacks",
                "tags",
                "metadata",
                "run_name",
                "stream",
            }
        }
        system_blocks: list[str] = []
        converted: list[dict[str, Any]] = []
        for message in messages:
            if message.type == "system":
                if isinstance(message.content, str):
                    system_blocks.append(message.content)
                else:
                    system_blocks.append(str(message.content))
                continue
            converted.append(self._convert_message(message))
        merged = _merge_consecutive_roles(converted)
        payload: dict[str, Any] = {
            "anthropic_version": "bedrock-2023-05-31",
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": merged,
            **self.model_kwargs,
            **safe_kwargs,
        }
        if system_blocks:
            payload["system"] = "\n\n".join(system_blocks)
        if stop:
            payload["stop_sequences"] = stop
        if self.bound_tools:
            payload["tools"] = self.bound_tools
        if self.tool_choice is not None:
            payload["tool_choice"] = _normalize_tool_choice(self.tool_choice)
        if self.thinking_effort:
            # This proxy requires adaptive thinking + output_config.effort
            # (Bedrock rejects thinking.type="enabled" and a top-level "effort").
            payload.setdefault(
                "thinking", {"type": "adaptive", "display": "summarized"}
            )
            output_config = dict(payload.get("output_config") or {})
            output_config.setdefault("effort", self.thinking_effort)
            payload["output_config"] = output_config
        # Anthropic cannot combine forced tool selection with thinking. Keep
        # the requested tool/structured-result contract and omit reasoning for
        # this call only, including thinking supplied through passthrough kwargs.
        choice = payload.get("tool_choice")
        thinking = payload.get("thinking")
        if (isinstance(choice, dict) and choice.get("type") in {"any", "tool"}
                and isinstance(thinking, dict) and thinking.get("type") in {"enabled", "adaptive"}):
            payload.pop("thinking", None)
            output_config = dict(payload.get("output_config") or {})
            output_config.pop("effort", None)
            if output_config:
                payload["output_config"] = output_config
            else:
                payload.pop("output_config", None)
        return payload

    @staticmethod
    def _convert_message(message: BaseMessage) -> dict[str, Any]:
        role = _message_role(message)
        if isinstance(message, LCToolMessage):
            content = message.content
            text = content if isinstance(content, str) else json.dumps(content)
            return {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": message.tool_call_id,
                        "content": text,
                    }
                ],
            }
        if isinstance(message, AIMessage):
            blocks: list[dict[str, Any]] = []
            if isinstance(message.content, str) and message.content:
                blocks.append({"type": "text", "text": message.content})
            elif isinstance(message.content, list):
                for block in message.content:
                    if isinstance(block, dict):
                        # Drop thinking blocks on replay: the proxy does not
                        # require (and may reject un-signed) thinking echoes.
                        if block.get("type") in {"thinking", "redacted_thinking", "reasoning"}:
                            continue
                        blocks.append(block)
                    elif isinstance(block, str) and block:
                        blocks.append({"type": "text", "text": block})
            for tool_call in message.tool_calls or []:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tool_call.get("id"),
                        "name": tool_call["name"],
                        "input": tool_call.get("args", {}),
                    }
                )
            if not blocks:
                blocks.append({"type": "text", "text": ""})
            return {"role": "assistant", "content": blocks}
        # human / other
        content = message.content
        if isinstance(content, str):
            return {"role": role, "content": content}
        return {"role": role, "content": content}

    @staticmethod
    def _parse_message(data: dict[str, Any]) -> AIMessage:
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in data.get("content") or []:
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "thinking":
                thinking_parts.append(block.get("thinking", ""))
            elif btype == "redacted_thinking":
                thinking_parts.append("[redacted thinking]")
            elif btype == "tool_use":
                tool_calls.append(
                    {
                        "id": block.get("id"),
                        "name": block.get("name", ""),
                        "args": block.get("input", {}) or {},
                    }
                )
        usage = data.get("usage") or {}
        usage_metadata = None
        if usage:
            usage_metadata = {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("input_tokens", 0)
                + usage.get("output_tokens", 0),
            }
        content = []
        if thinking_parts:
            content.append({"type": "reasoning", "reasoning": "\n".join(thinking_parts)})
        if text_parts:
            content.append({"type": "text", "text": "".join(text_parts)})
        return AIMessage(
            content=content, tool_calls=tool_calls,
            response_metadata={"stop_reason": data.get("stop_reason")},
            usage_metadata=usage_metadata,
        )


def _to_anthropic_tool(tool: Any) -> dict[str, Any]:
    """Convert a tool spec into the Anthropic tools format."""
    oai = convert_to_openai_tool(tool)
    fn = oai.get("function", oai)
    return {
        "name": fn.get("name"),
        "description": fn.get("description", ""),
        "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
    }


def _normalize_tool_choice(choice: str | dict[str, Any]) -> dict[str, Any]:
    """Map an OpenAI-style tool_choice onto the Anthropic format."""
    if isinstance(choice, dict):
        if choice.get("type") == "function":
            name = (choice.get("function") or {}).get("name")
            if name:
                return {"type": "tool", "name": name}
        return choice
    if choice == "auto":
        return {"type": "auto"}
    if choice == "any" or choice == "required":
        return {"type": "any"}
    if choice == "none":
        return {"type": "none"}
    return {"type": "tool", "name": choice}


def _merge_consecutive_roles(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge adjacent same-role messages, as Anthropic requires alternation.

    Tool results (role ``user`` with ``tool_result`` blocks) must be combined
    with any following user turn into one user message.
    """
    merged: list[dict[str, Any]] = []
    for msg in messages:
        if merged and merged[-1]["role"] == msg["role"]:
            prev = merged[-1]
            prev_content = _as_block_list(prev["content"])
            cur_content = _as_block_list(msg["content"])
            prev["content"] = prev_content + cur_content
        else:
            merged.append(dict(msg))
    return merged


def _as_block_list(content: Any) -> list[dict[str, Any]]:
    """Coerce message content into a list of content blocks."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return list(content)
    return [{"type": "text", "text": str(content)}]


MerckChatAnthropic = MerckAnthropicChatModel
