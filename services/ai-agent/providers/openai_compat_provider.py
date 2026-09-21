"""
TradingFirm — the one LLM provider (Part 4.1).

An OpenAI-compatible client with a configurable base_url, pointed at
OpenRouter (decisions.md 2026-09-20, supersedes D16). One non-streaming
`POST /chat/completions` per call, a JSON schema in `response_format`, a
per-model reasoning table, and a daily cap plus a cooldown in Redis that are
checked *before* the request, so a rate limit stops this service instead of
starting a retry storm.

Pre-flight order, once, for the whole module:

    1. validate input        -> ValueError            (no reservation, no HTTP)
    2. configured?           -> LLMNotConfigured      (no client built)
    3. cooldown active?      -> LLMCooledDown         (no HTTP)
    4. INCR the day counter                           (the reservation)
    5. over cap?             -> DECR, LLMCapExceeded  (the only release)
    6. send

Steps 1-3 raise before the counter is touched, so there is nothing to
release. Step 5 is the one and only DECR. A request that goes out at step 6
counts against the day, whatever it answers: the cap bounds calls *made*, and
releasing failures is what turns a cap into a retry budget.

The SDK's own retries are off (`max_retries=0`). Nothing here retries
anything, ever (G6).
"""

import json
import logging
import re
import time
from typing import Any, Optional

import openai
from openai import AsyncOpenAI

import cache
import config
from config import settings

from .base import (
    LLMAuthFailed,
    LLMBadResponse,
    LLMCapExceeded,
    LLMCooledDown,
    LLMError,
    LLMNotConfigured,
    LLMProvider,
    LLMRateLimited,
    LLMRefused,
    LLMRejected,
    LLMResult,
    LLMUnavailable,
    validate_request,
)

logger = logging.getLogger(__name__)


# ── Per-model reasoning ──────────────────────────────────────────
#
# Every row comes from OpenRouter's own `GET /api/v1/models` `reasoning`
# object, read on 2026-09-20, not from memory. Pinned by
# test_model_reasoning_pinned_to_spec.
#
#   effort            what this provider sends by default
#   supported_efforts what a caller's `effort=` override may be
#   mandatory         reasoning cannot be turned off; never send "none"
#
# A value of None means: send no `reasoning` field at all. That is the entry
# for a model that advertises no effort levels, and the behaviour for any id
# not in the table. An id that answers without reasoning is recoverable; a
# 400 on every call is not.
MODEL_REASONING: dict[str, Optional[dict]] = {
    # mandatory: true, default_effort "max" — "low" is the cheapest accepted
    # value, and "none" is rejected outright.
    "z-ai/glm-5.3-flash": {
        "effort": "low",
        "supported_efforts": ("max", "high", "low"),
        "mandatory": True,
    },
    "z-ai/glm-5.3": {
        "effort": "low",
        "supported_efforts": ("max", "high", "low"),
        "mandatory": True,
    },
    # mandatory: false, default_effort "high" — "low" is a deliberate
    # down-shift, and OpenRouter's unified `reasoning` param is the *only*
    # way to reach reasoning on an Anthropic id.
    "anthropic/claude-sonnet-5": {
        "effort": "low",
        "supported_efforts": ("max", "xhigh", "high", "medium", "low"),
        "mandatory": False,
    },
    # Its reasoning object is {"mandatory": false} with no supported_efforts,
    # and `reasoning_effort` is absent from its supported_parameters. Sending
    # an effort it does not advertise is exactly the 400 this table prevents.
    "anthropic/claude-haiku-4.5": None,
}

# The source name the cap and the cooldown hang off.
SOURCE = cache.SOURCE_LLM

# finish_reason values this provider will parse an answer from. Anything else
# is a refusal to answer in some form and is named in the error.
FINISH_OK = (None, "stop")


def resolve_effort(model: str, effort: Optional[str]) -> Optional[str]:
    """The reasoning effort to send for `model`, or None to send no
    `reasoning` field. Runs at pre-flight step 1.

    A caller override outside the model's advertised `supported_efforts` is a
    ValueError here rather than a 400 from the gateway later. An override for
    a model the table maps to nothing is ignored with one WARNING — sending
    it is the failure we are avoiding.
    """
    entry = MODEL_REASONING.get(model)
    if entry is None:
        if model not in MODEL_REASONING:
            logger.warning(f"llm model {model} is not in MODEL_REASONING; sending no reasoning param")
        if effort is not None:
            logger.warning(f"llm effort {effort!r} ignored: {model} advertises no reasoning efforts")
        return None
    if effort is None:
        return entry["effort"]
    if effort not in entry["supported_efforts"]:
        raise ValueError(
            f"effort {effort!r} is not supported by {model}; "
            f"supported: {', '.join(entry['supported_efforts'])}"
        )
    return effort


def _retry_after_seconds(raw: Any, default: int) -> int:
    """`retry-after` in seconds, bounded.

    OpenRouter sends the delta-seconds form. An HTTP-date, a negative number,
    a zero or anything unparseable falls back to `default` — the header is a
    hint from the other side and must never be able to park this service.
    The result is capped at config.LLM_COOLDOWN_MAX either way.
    """
    seconds = default
    try:
        parsed = int(str(raw).strip())
    except (TypeError, ValueError):
        parsed = 0
    if parsed > 0:
        seconds = parsed
    return min(seconds, config.LLM_COOLDOWN_MAX)


# One provider slug as OpenRouter writes them ("anthropic", "google-vertex",
# "amazon-bedrock/us"): a charset that cannot carry anything but a name.
_PROVIDER_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,63}$")


def provider_routing(order: str) -> Optional[dict]:
    """The request's `provider` object for LLM_PROVIDER_ORDER, or None when
    the setting is empty (the field is then absent and OpenRouter routes as
    it always has). `allow_fallbacks` is false on purpose: the point of
    naming a host is to stay on it. Raises ValueError on a malformed slug —
    pre-flight step 1, so a bad setting sends nothing and reserves nothing."""
    if not isinstance(order, str) or not order.strip():
        return None
    slugs = [part.strip().lower() for part in order.split(",") if part.strip()]
    for slug in slugs:
        if not _PROVIDER_SLUG_RE.match(slug):
            raise ValueError("LLM_PROVIDER_ORDER must be provider slugs, comma-separated")
    return {"order": slugs, "allow_fallbacks": False}


def _system_content(system: str, cache_system: bool):
    """The system message's content. A plain string unless the caller asked
    for prompt caching, in which case it is one text block carrying
    `cache_control` — the per-block form OpenRouter passes to Anthropic
    models. A prefix under the model's minimum (1,024 tokens on Sonnet 5) is
    not an error: the marker is ignored and `cacheWrite` stays absent, which
    is how a live call measures whether the prefix cleared it."""
    if not cache_system:
        return system
    return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]


def _usage_dict(raw: Optional[dict]) -> dict:
    """OpenRouter's usage object flattened to the repo's camelCase, every key
    absent-safe. `cost` is OpenRouter's own accounting and is simply not there
    on a gateway that does not report it (it is always returned by OpenRouter
    itself: `usage: {"include": true}` is documented as deprecated and having
    no effect, so this provider does not send it)."""
    raw = raw or {}
    prompt = raw.get("prompt_tokens_details") or {}
    completion = raw.get("completion_tokens_details") or {}
    out = {
        "input": raw.get("prompt_tokens"),
        "output": raw.get("completion_tokens"),
        "cacheRead": prompt.get("cached_tokens"),
        "cacheWrite": prompt.get("cache_write_tokens"),
        "reasoning": completion.get("reasoning_tokens"),
        "cost": raw.get("cost"),
    }
    return {k: v for k, v in out.items() if v is not None}


class OpenAICompatProvider(LLMProvider):
    """One AsyncOpenAI client, built lazily so "no key -> no client" holds.

    `http_client` is the injection seam the tests use: an
    `httpx2.AsyncClient` wrapping an `httpx2.MockTransport`. The openai SDK
    runs on httpx2, not httpx, so respx cannot intercept it and nothing here
    needs patching.
    """

    def __init__(self, settings_obj=settings, redis=None, *, http_client=None):
        self._settings = settings_obj
        self._redis = redis
        self._http_client = http_client
        self._client: Optional[AsyncOpenAI] = None
        self._memory_cap = cache.MemoryCap()
        self._memory_cooldowns = cache.MemoryCooldowns()

    # ── the client ───────────────────────────────────────────────

    def _client_or_build(self) -> AsyncOpenAI:
        """Built on first use, which is after pre-flight step 2. Nothing
        constructs a client for a service with no key."""
        if self._client is None:
            self._client = AsyncOpenAI(
                base_url=self._settings.llm_base_url,
                api_key=self._settings.llm_api_key.get_secret_value(),
                max_retries=0,
                timeout=self._settings.llm_timeout,
                http_client=self._http_client,
            )
        return self._client

    def _headers(self) -> Optional[dict]:
        """OpenRouter's app-attribution headers, only when configured. Never
        a hard-coded value: an unset setting means the header is absent."""
        headers = {}
        if self._settings.llm_referer:
            headers["HTTP-Referer"] = self._settings.llm_referer
        if self._settings.llm_title:
            headers["X-Title"] = self._settings.llm_title
        return headers or None

    # ── logging ──────────────────────────────────────────────────

    def _fail(
        self,
        exc: Exception,
        *,
        label: str,
        model: str,
        status: Any = None,
        started: float,
        calls: Optional[int],
    ) -> Exception:
        """Log exactly one WARNING for a raised error and hand the error back
        for the caller to `raise ... from None`. Label, model, error class,
        status, ms, callsToday — never a body, never a URL, never the key."""
        cap = self._settings.llm_daily_call_cap
        logger.warning(
            f"llm error label={label} model={model} err={type(exc).__name__} "
            f"status={status if status is not None else '-'} "
            f"ms={int((time.monotonic() - started) * 1000)} "
            f"callsToday={calls if calls is not None else '-'}/{cap}"
        )
        return exc

    # ── the call ─────────────────────────────────────────────────

    async def complete_structured(
        self,
        system: str,
        user: str,
        schema: dict,
        *,
        label: str = "llm",
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        effort: Optional[str] = None,
        cache_system: bool = False,
    ) -> LLMResult:
        started = time.monotonic()
        model = model or self._settings.llm_model
        max_tokens = max_tokens or self._settings.llm_max_tokens
        calls: Optional[int] = None

        def fail(exc, status=None):
            return self._fail(exc, label=label, model=model, status=status,
                              started=started, calls=calls)

        # 1. validate input ──────────────────────────────────────
        try:
            validate_request(system, user, schema, label)
            send_effort = resolve_effort(model, effort)
            routing = provider_routing(self._settings.llm_provider_order)
        except ValueError as e:
            raise fail(e) from None

        # 2. configured? ─────────────────────────────────────────
        if not self._settings.llm_configured:
            raise fail(LLMNotConfigured("no API key configured")) from None

        # 3. cooldown active? ────────────────────────────────────
        left, cause = await cache.cooldown_remaining(
            self._redis, self._memory_cooldowns, SOURCE, self._settings.llm_cooldown_seconds
        )
        if left:
            raise fail(
                LLMCooledDown(f"cooling down {left}s after HTTP {cause or 'refusal'}"),
                status=cause,
            ) from None

        # 4. INCR the day counter — the reservation ──────────────
        calls = await cache.reserve_call(self._redis, self._memory_cap)

        # 5. over cap? the only release ──────────────────────────
        cap = self._settings.llm_daily_call_cap
        if calls > cap:
            await cache.release_call(self._redis, self._memory_cap)
            calls -= 1
            raise fail(LLMCapExceeded(f"daily call cap {cap} reached")) from None

        # 6. send ────────────────────────────────────────────────
        request: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": _system_content(system, cache_system)},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": label, "schema": schema, "strict": True},
            },
        }
        extra_body: dict[str, Any] = {}
        if send_effort is not None:
            extra_body["reasoning"] = {"effort": send_effort}
        if routing is not None:
            extra_body["provider"] = routing
        if extra_body:
            request["extra_body"] = extra_body
        headers = self._headers()
        if headers is not None:
            request["extra_headers"] = headers

        try:
            response = await self._client_or_build().chat.completions.create(**request)
        except openai.APIStatusError as e:
            raise (await self._on_status_error(e, fail)) from None
        except openai.APITimeoutError:
            raise fail(LLMUnavailable("timeout")) from None
        except openai.APIConnectionError as e:
            raise fail(LLMUnavailable(f"transport: {type(e).__name__}")) from None
        except openai.OpenAIError as e:
            raise fail(LLMUnavailable(f"sdk: {type(e).__name__}")) from None

        data, finish_reason, usage = self._parse(response, schema, fail)

        duration_ms = int((time.monotonic() - started) * 1000)
        parts = " ".join(f"{k}={v}" for k, v in (
            ("in", usage.get("input")),
            ("out", usage.get("output")),
            ("cacheRead", usage.get("cacheRead")),
            ("cacheWrite", usage.get("cacheWrite")),
            ("reasoning", usage.get("reasoning")),
            ("cost", usage.get("cost")),
        ) if v is not None)
        logger.info(
            f"llm call label={label} model={model} {parts} "
            f"finish={finish_reason} ms={duration_ms} callsToday={calls}/{cap}"
        )
        return LLMResult(
            data=data,
            model=model,
            finish_reason=finish_reason,
            duration_ms=duration_ms,
            usage=usage,
        )

    # ── error mapping ────────────────────────────────────────────

    async def _on_status_error(self, e: openai.APIStatusError, fail) -> LLMError:
        """Map an HTTP status to a typed error, starting a cooldown where the
        status will not clear on its own. Never retries, never chains the SDK
        exception (its str() can carry the URL)."""
        status = e.status_code
        headers = getattr(getattr(e, "response", None), "headers", None) or {}

        if status == 429:
            seconds = _retry_after_seconds(
                headers.get("retry-after"), self._settings.llm_cooldown_seconds
            )
            await cache.start_cooldown(
                self._redis, self._memory_cooldowns, SOURCE, seconds, "429"
            )
            return fail(LLMRateLimited(f"HTTP 429, cooling down {seconds}s"), status=status)

        if status in (401, 402, 403):
            await cache.start_cooldown(
                self._redis, self._memory_cooldowns, SOURCE,
                config.LLM_COOLDOWN_AUTH, str(status),
            )
            return fail(
                LLMAuthFailed(f"HTTP {status}, cooling down {config.LLM_COOLDOWN_AUTH}s"),
                status=status,
            )

        # 408 is OpenRouter's documented "your request timed out": a wait, not
        # a request we got wrong.
        if status == 408:
            return fail(LLMUnavailable("HTTP 408"), status=status)

        if 400 <= status < 500:
            return fail(LLMRejected(f"HTTP {status}"), status=status)

        return fail(LLMUnavailable(f"HTTP {status}"), status=status)

    # ── response ─────────────────────────────────────────────────

    def _parse(self, response, schema: dict, fail) -> tuple[dict, Optional[str], dict]:
        """(data, finish_reason, usage), or a typed error. The response object
        is not kept: everything needed is copied out here (G8)."""
        usage = _usage_dict(
            response.usage.model_dump() if getattr(response, "usage", None) else None
        )

        choices = getattr(response, "choices", None)
        if not choices:
            # OpenRouter sends 200 OK as soon as a provider accepts the
            # request, so every failure after that arrives in the body:
            # "the status stays 200 even when every provider fails — the last
            # error reaches you in the response body". The SDK does not raise
            # on it; `choices` is simply None.
            error = (getattr(response, "model_extra", None) or {}).get("error")
            if isinstance(error, dict):
                code = error.get("code")
                status = code if isinstance(code, int) else None
                raise fail(LLMUnavailable(f"HTTP 200 with upstream error {status or '?'}"),
                           status=status) from None
            raise fail(LLMBadResponse("no choices")) from None

        choice = choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        message = getattr(choice, "message", None)

        # Truncated first: a cut answer is never valid JSON, and the error
        # should say why rather than "not JSON". OpenRouter documents this as
        # the shape a reasoning model takes when it spends the whole budget
        # on reasoning.
        if finish_reason == "length":
            raise fail(LLMBadResponse("length")) from None

        # Then the refusal, so a content filter that also sets a refusal reads
        # as a refusal rather than as an odd finish_reason.
        refusal = getattr(message, "refusal", None) if message is not None else None
        if refusal is not None:
            raise fail(LLMRefused(f"refused ({len(str(refusal))} chars)")) from None

        if finish_reason not in FINISH_OK:
            raise fail(LLMBadResponse(f"finish_reason {finish_reason}")) from None

        content = getattr(message, "content", None) if message is not None else None
        if not isinstance(content, str) or not content.strip():
            raise fail(LLMBadResponse("empty content")) from None
        try:
            data = json.loads(content)
        except ValueError:
            raise fail(LLMBadResponse("not JSON")) from None
        if not isinstance(data, dict):
            raise fail(LLMBadResponse("not an object")) from None
        for name in schema["required"]:
            if name not in data:
                raise fail(LLMBadResponse(f"missing required key {name}")) from None

        return data, finish_reason, usage
