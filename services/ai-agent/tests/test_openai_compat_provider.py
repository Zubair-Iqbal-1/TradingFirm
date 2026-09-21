"""Part 4.1 — the OpenAI-compatible provider against OpenRouter.

Every test here drives the *real* openai SDK: its request builder and its
response parser both run. The only thing replaced is the socket, through
`httpx2.MockTransport` injected as `http_client` (spec 4.1 decision 7).
respx cannot be used — the SDK runs on httpx2 and respx patches httpx.

No test opens a socket and no test makes a live call.
"""

import inspect
import json
import logging

import httpx2
import pytest
from pydantic import SecretStr

import cache
import config
from config import Settings
from providers import (
    LLMAuthFailed,
    LLMBadResponse,
    LLMCapExceeded,
    LLMCooledDown,
    LLMNotConfigured,
    LLMRateLimited,
    LLMRefused,
    LLMRejected,
    LLMResult,
    LLMUnavailable,
    MODEL_REASONING,
    OpenAICompatProvider,
    build_provider,
)

SCHEMA = {
    "type": "object",
    "required": ["verdict", "confidence"],
    "properties": {"verdict": {"type": "string"}, "confidence": {"type": "integer"}},
}
ANSWER = {"verdict": "hold", "confidence": 61}
GLM = "z-ai/glm-5.3-flash"
SONNET = "anthropic/claude-sonnet-5"
HAIKU = "anthropic/claude-haiku-4.5"


# ── Harness ──────────────────────────────────────────────────────

class Wire:
    """Captures the outgoing request and serves a canned answer."""

    def __init__(self, response=None, raises=None):
        self.requests: list[httpx2.Request] = []
        self.bodies: list[dict] = []
        self._response = response
        self._raises = raises

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        self.bodies.append(json.loads(request.content))
        if self._raises is not None:
            raise self._raises
        if callable(self._response):
            return self._response()
        return self._response

    @property
    def body(self) -> dict:
        assert self.bodies, "no request was sent"
        return self.bodies[-1]


def ok(content=None, *, finish="stop", refusal=None, model=GLM, usage=None):
    if content is None:
        content = json.dumps(ANSWER)
    message = {"role": "assistant", "content": content}
    if refusal is not None:
        message["refusal"] = refusal
    return httpx2.Response(200, json={
        "id": "gen-1", "object": "chat.completion", "model": model,
        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
        "usage": usage if usage is not None else {
            "prompt_tokens": 1204, "completion_tokens": 187, "total_tokens": 1391,
            "cost": 0.00142,
            "prompt_tokens_details": {"cached_tokens": 1100, "cache_write_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": 142},
        },
    })


def err(status, *, body=None, headers=None):
    return httpx2.Response(
        status,
        json=body if body is not None else {"error": {"code": status, "message": "nope"}},
        headers=headers or {},
    )


class FakeRedis:
    """The same fake test_cache.py uses, kept local so the two files cannot
    drift apart silently."""

    def __init__(self, raises=False):
        self.store, self.ttls, self.raises = {}, {}, raises

    def _check(self):
        if self.raises:
            raise RuntimeError("redis is down")

    async def incr(self, key):
        self._check()
        self.store[key] = str(int(self.store.get(key, "0")) + 1)
        return int(self.store[key])

    async def decr(self, key):
        self._check()
        self.store[key] = str(int(self.store.get(key, "0")) - 1)
        return int(self.store[key])

    async def expire(self, key, ttl, nx=False):
        self._check()
        if nx and key in self.ttls:
            return False
        self.ttls[key] = ttl
        return True

    async def set(self, key, value, ex=None):
        self._check()
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex

    async def get(self, key):
        self._check()
        return self.store.get(key)

    async def ttl(self, key):
        self._check()
        return self.ttls.get(key, -2)


def make(wire=None, *, key="sk-or-test", cap=100, redis=None, **over):
    # Every knob the wire assertions depend on is set explicitly: these tests
    # run inside tf-ai-agent-dev, whose env hard-codes SERVICE_NAME,
    # LLM_BASE_URL and LLM_DAILY_CALL_CAP, and _env_file=None does not hide
    # the process environment.
    over.setdefault("llm_base_url", "https://openrouter.ai/api/v1")
    settings = Settings(_env_file=None, llm_api_key=SecretStr(key),
                        llm_daily_call_cap=cap, llm_model=GLM, **over)
    http = None
    if wire is not None:
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(wire))
    return OpenAICompatProvider(settings, redis, http_client=http)


async def call(provider, **kw):
    return await provider.complete_structured(
        "You are an analyst.", "AAPL, swing.", SCHEMA, label="verdict", **kw
    )


# ── The SDK contract ─────────────────────────────────────────────

def test_sdk_accepts_request_kwargs():
    """Every kwarg the provider sends is a real parameter of the SDK method
    it calls. Builds no client, opens nothing — this is the test that catches
    an openai bump that renames a parameter."""
    from openai.resources.chat.completions import AsyncCompletions

    params = set(inspect.signature(AsyncCompletions.create).parameters)
    for name in ("model", "messages", "max_tokens", "response_format",
                 "extra_body", "extra_headers", "timeout"):
        assert name in params, name


def test_model_reasoning_pinned_to_spec():
    """Pinned to OpenRouter's own models API, read 2026-09-20. Changing a row
    means re-reading it there first (spec 4.1 decision 5)."""
    assert MODEL_REASONING[GLM] == {
        "effort": "low", "supported_efforts": ("max", "high", "low"), "mandatory": True}
    assert MODEL_REASONING["z-ai/glm-5.3"] == MODEL_REASONING[GLM]
    assert MODEL_REASONING[SONNET] == {
        "effort": "low",
        "supported_efforts": ("max", "xhigh", "high", "medium", "low"),
        "mandatory": False}
    assert MODEL_REASONING[HAIKU] is None
    assert "anthropic/claude-haiku-4-5" not in MODEL_REASONING   # the hyphenated id does not exist


# ── The wire ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_request_shape_on_the_wire():
    wire = Wire(ok())
    result = await call(make(wire, redis=FakeRedis()))
    req = wire.requests[-1]
    assert str(req.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert req.headers.get("authorization")
    body = wire.body
    assert body["model"] == GLM
    assert body["max_tokens"] == 8000
    assert body["messages"] == [
        {"role": "system", "content": "You are an analyst."},
        {"role": "user", "content": "AAPL, swing."},
    ]
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "verdict", "schema": SCHEMA, "strict": True},
    }
    assert "usage" not in body          # deprecated, has no effect; never sent
    assert "stream" not in body
    assert isinstance(result, LLMResult)
    assert result.data == ANSWER
    assert result.finish_reason == "stop"
    assert result.usage == {"input": 1204, "output": 187, "cacheRead": 1100,
                            "cacheWrite": 0, "reasoning": 142, "cost": 0.00142}


@pytest.mark.asyncio
async def test_glm_body_carries_reasoning_effort_low():
    wire = Wire(ok(model=GLM))
    await call(make(wire, redis=FakeRedis()), model=GLM)
    assert wire.body["reasoning"] == {"effort": "low"}


@pytest.mark.asyncio
async def test_sonnet_body_carries_reasoning_effort_low():
    wire = Wire(ok(model=SONNET))
    await call(make(wire, redis=FakeRedis()), model=SONNET)
    assert wire.body["reasoning"] == {"effort": "low"}


@pytest.mark.asyncio
async def test_haiku_body_has_no_reasoning_key():
    """Haiku 4.5 advertises no supported_efforts and no reasoning_effort;
    sending one is the 400 MODEL_REASONING exists to prevent."""
    wire = Wire(ok(model=HAIKU))
    await call(make(wire, redis=FakeRedis()), model=HAIKU)
    assert "reasoning" not in wire.body


@pytest.mark.asyncio
async def test_unknown_model_sends_no_reasoning_param(caplog):
    wire = Wire(ok(model="someone/else-1"))
    with caplog.at_level(logging.WARNING):
        await call(make(wire, redis=FakeRedis()), model="someone/else-1")
    assert "reasoning" not in wire.body
    assert sum("MODEL_REASONING" in r.message for r in caplog.records) == 1


@pytest.mark.asyncio
async def test_effort_ignored_for_a_model_with_no_reasoning(caplog):
    """Haiku 4.5 advertises no efforts, so a caller override is dropped with
    one WARNING rather than sent and 400'd."""
    wire = Wire(ok(model=HAIKU))
    with caplog.at_level(logging.WARNING):
        await call(make(wire, redis=FakeRedis()), model=HAIKU, effort="low")
    assert "reasoning" not in wire.body
    assert sum("ignored" in r.message for r in caplog.records) == 1


@pytest.mark.asyncio
async def test_attribution_headers_absent_unless_configured():
    wire = Wire(ok())
    await call(make(wire, redis=FakeRedis()))
    assert "http-referer" not in wire.requests[-1].headers
    assert "x-title" not in wire.requests[-1].headers


@pytest.mark.asyncio
async def test_attribution_headers_sent_from_settings():
    wire = Wire(ok())
    await call(make(wire, redis=FakeRedis(),
                    llm_referer="https://tradingfirm.local",
                    llm_title="TradingFirm"))
    assert wire.requests[-1].headers["http-referer"] == "https://tradingfirm.local"
    assert wire.requests[-1].headers["x-title"] == "TradingFirm"


# ── Pre-flight ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_blank_input_rejected_before_http():
    wire = Wire(ok())
    p = make(wire, redis=FakeRedis())
    for bad in (("", "u"), ("s", "   ")):
        with pytest.raises(ValueError):
            await p.complete_structured(bad[0], bad[1], SCHEMA, label="verdict")
    assert wire.requests == []
    assert p._client is None


@pytest.mark.asyncio
async def test_schema_without_required_or_bad_label_rejected():
    """The required list is what the answer is checked against, and the label
    becomes json_schema.name."""
    wire = Wire(ok())
    p = make(wire, redis=FakeRedis())
    bad_schemas = [
        {"type": "object"},                              # no required
        {"type": "object", "required": []},              # empty required
        {"type": "object", "required": ["a", ""]},       # blank entry
        {"type": "array", "required": ["a"]},            # not an object
        ["not", "a", "dict"],
    ]
    for schema in bad_schemas:
        with pytest.raises(ValueError):
            await p.complete_structured("s", "u", schema, label="verdict")
    for label in ("", "bad label", "a" * 65, "verdict!"):
        with pytest.raises(ValueError):
            await p.complete_structured("s", "u", SCHEMA, label=label)
    assert wire.requests == []
    assert p._client is None


@pytest.mark.asyncio
async def test_unsupported_effort_rejected_before_http():
    """A caller override outside the model's advertised efforts is a
    ValueError here, not a 400 from the gateway later."""
    wire = Wire(ok())
    r = FakeRedis()
    p = make(wire, redis=r)
    with pytest.raises(ValueError, match="medium"):
        await call(p, model=GLM, effort="medium")        # GLM: max/high/low only
    with pytest.raises(ValueError, match="none"):
        await call(p, model=GLM, effort="none")          # mandatory: never "none"
    assert wire.requests == []
    assert r.store == {}                                  # no reservation
    assert p._client is None


@pytest.mark.asyncio
async def test_supported_effort_override_reaches_the_wire():
    wire = Wire(ok())
    await call(make(wire, redis=FakeRedis()), model=GLM, effort="high")
    assert wire.body["reasoning"] == {"effort": "high"}


@pytest.mark.asyncio
async def test_no_key_raises_before_any_http():
    wire = Wire(ok())
    p = make(wire, key="", redis=FakeRedis())
    with pytest.raises(LLMNotConfigured):
        await call(p)
    assert wire.requests == []
    assert p._client is None          # no client is built for a keyless service


@pytest.mark.asyncio
async def test_cooldown_blocks_call_before_http():
    wire = Wire(ok())
    r = FakeRedis()
    p = make(wire, redis=r)
    await cache.start_cooldown(r, None, cache.SOURCE_LLM, 900, "402")
    with pytest.raises(LLMCooledDown, match="402"):
        await call(p)
    assert wire.requests == []
    assert cache.day_counter_key() not in r.store        # no reservation


@pytest.mark.asyncio
async def test_cap_reached_blocks_call_and_releases():
    wire = Wire(ok())
    r = FakeRedis()
    p = make(wire, cap=1, redis=r)
    await call(p)                                        # 1 of 1
    with pytest.raises(LLMCapExceeded):
        await call(p)                                    # 2 of 1 -> refuse
    assert len(wire.requests) == 1
    assert r.store[cache.day_counter_key()] == "1"       # released back to 1


@pytest.mark.asyncio
async def test_cap_zero_refuses_everything():
    """The dev twin's setting: even a leaked key sends nothing."""
    wire = Wire(ok())
    r = FakeRedis()
    with pytest.raises(LLMCapExceeded):
        await call(make(wire, cap=0, redis=r))
    assert wire.requests == []
    assert r.store[cache.day_counter_key()] == "0"


@pytest.mark.asyncio
async def test_cap_falls_back_to_memory_when_redis_down(caplog):
    wire = Wire(ok())
    p = make(wire, cap=1, redis=FakeRedis(raises=True))
    with caplog.at_level(logging.WARNING):
        await call(p)
        with pytest.raises(LLMCapExceeded):
            await call(p)
    assert len(wire.requests) == 1
    assert p._memory_cap.count(cache.et_day()) == 1


@pytest.mark.asyncio
async def test_two_calls_increment_counter_twice():
    wire = Wire(ok())
    r = FakeRedis()
    p = make(wire, redis=r)
    await call(p)
    await call(p)
    assert r.store[cache.day_counter_key()] == "2"
    assert len(wire.requests) == 2


@pytest.mark.asyncio
async def test_preflight_order_is_validate_configured_cooldown_incr():
    """Order matters: a keyless service must not burn a reservation, and a
    cooled-down one must not either. Only the over-cap branch releases."""
    r = FakeRedis()

    # bad input beats a missing key
    p = make(Wire(ok()), key="", redis=r)
    with pytest.raises(ValueError):
        await p.complete_structured("", "u", SCHEMA, label="verdict")

    # a missing key beats an active cooldown
    await cache.start_cooldown(r, None, cache.SOURCE_LLM, 900, "429")
    with pytest.raises(LLMNotConfigured):
        await call(p)

    # with a key, the cooldown beats the reservation
    p2 = make(Wire(ok()), redis=r)
    with pytest.raises(LLMCooledDown):
        await call(p2)
    assert cache.day_counter_key() not in r.store


# ── HTTP errors ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_429_sets_cooldown_and_does_not_retry():
    wire = Wire(err(429, headers={"retry-after": "60"}))
    r = FakeRedis()
    with pytest.raises(LLMRateLimited, match="60"):
        await call(make(wire, redis=r))
    assert len(wire.requests) == 1                        # max_retries=0
    assert r.store[cache.cooldown_key(cache.SOURCE_LLM)] == "429"
    assert r.ttls[cache.cooldown_key(cache.SOURCE_LLM)] == 60
    assert r.store[cache.day_counter_key()] == "1"        # the call still counts


@pytest.mark.asyncio
async def test_429_without_retry_after_uses_default_cooldown():
    wire = Wire(err(429))
    r = FakeRedis()
    with pytest.raises(LLMRateLimited):
        await call(make(wire, redis=r))
    assert r.ttls[cache.cooldown_key(cache.SOURCE_LLM)] == 900


@pytest.mark.asyncio
async def test_retry_after_is_capped_at_an_hour():
    wire = Wire(err(429, headers={"retry-after": "99999"}))
    r = FakeRedis()
    with pytest.raises(LLMRateLimited):
        await call(make(wire, redis=r))
    assert r.ttls[cache.cooldown_key(cache.SOURCE_LLM)] == config.LLM_COOLDOWN_MAX == 3600


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["Wed, 21 Oct 2026 07:28:00 GMT", "-5", "0", "soon", ""])
async def test_unparseable_retry_after_uses_default_cooldown(raw):
    """An HTTP-date, a negative, a zero or garbage: the header is a hint from
    the other side and must never be able to park this service."""
    wire = Wire(err(429, headers={"retry-after": raw}))
    r = FakeRedis()
    with pytest.raises(LLMRateLimited):
        await call(make(wire, redis=r))
    assert r.ttls[cache.cooldown_key(cache.SOURCE_LLM)] == 900


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 402, 403])
async def test_auth_error_cools_down_an_hour(status):
    wire = Wire(err(status))
    r = FakeRedis()
    with pytest.raises(LLMAuthFailed):
        await call(make(wire, redis=r))
    assert r.ttls[cache.cooldown_key(cache.SOURCE_LLM)] == 3600
    assert r.store[cache.cooldown_key(cache.SOURCE_LLM)] == str(status)


@pytest.mark.asyncio
async def test_cooldown_message_names_its_cause():
    """An out-of-credits hour must not read as a rate limit."""
    r = FakeRedis()
    await cache.start_cooldown(r, None, cache.SOURCE_LLM, 3600, "402")
    with pytest.raises(LLMCooledDown) as excinfo:
        await call(make(Wire(ok()), redis=r))
    assert "402" in str(excinfo.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 404, 422])
async def test_bad_request_raises_rejected_without_cooldown(status):
    wire = Wire(err(status))
    r = FakeRedis()
    with pytest.raises(LLMRejected, match=str(status)):
        await call(make(wire, redis=r))
    assert cache.cooldown_key(cache.SOURCE_LLM) not in r.store
    assert len(wire.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [408, 500, 502, 503])
async def test_server_error_raises_unavailable_without_retry(status):
    """408 is OpenRouter's documented request-timeout: a wait, not our bug."""
    wire = Wire(err(status))
    r = FakeRedis()
    with pytest.raises(LLMUnavailable, match=str(status)):
        await call(make(wire, redis=r))
    assert len(wire.requests) == 1
    assert cache.cooldown_key(cache.SOURCE_LLM) not in r.store


@pytest.mark.asyncio
async def test_timeout_raises_unavailable():
    wire = Wire(raises=httpx2.ReadTimeout("slow"))
    with pytest.raises(LLMUnavailable, match="timeout"):
        await call(make(wire, redis=FakeRedis()))


@pytest.mark.asyncio
async def test_transport_error_raises_unavailable():
    wire = Wire(raises=httpx2.ConnectError("no route"))
    with pytest.raises(LLMUnavailable, match="transport"):
        await call(make(wire, redis=FakeRedis()))


# ── Response ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_length_finish_reason_raises_before_parsing():
    """OpenRouter documents this as what a reasoning model returns when it
    spends the whole max_tokens budget on reasoning: 200, finish_reason
    "length", empty content."""
    wire = Wire(ok(content="", finish="length"))
    with pytest.raises(LLMBadResponse, match="length"):
        await call(make(wire, redis=FakeRedis()))


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["content_filter", "error", "tool_calls"])
async def test_unexpected_finish_reason_raises(finish):
    wire = Wire(ok(finish=finish))
    with pytest.raises(LLMBadResponse, match=finish):
        await call(make(wire, redis=FakeRedis()))


@pytest.mark.asyncio
async def test_refusal_raises_without_echoing_text():
    secret = "I will not answer that, because <sensitive reason>"
    wire = Wire(ok(content=None, refusal=secret))
    with pytest.raises(LLMRefused) as excinfo:
        await call(make(wire, redis=FakeRedis()))
    assert str(len(secret)) in str(excinfo.value)
    assert "sensitive" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_200_with_error_body_raises_unavailable():
    """OpenRouter sends 200 OK as soon as a provider accepts the request, so a
    later failure arrives in the body: "the status stays 200 even when every
    provider fails". The SDK does not raise — choices is simply None."""
    wire = Wire(httpx2.Response(200, json={
        "error": {"code": 502, "message": "Provider returned error",
                  "metadata": {"provider_name": "X"}},
        "user_id": "u",
    }))
    with pytest.raises(LLMUnavailable, match="502"):
        await call(make(wire, redis=FakeRedis()))


@pytest.mark.asyncio
async def test_200_with_no_choices_and_no_error_raises_bad_response():
    wire = Wire(httpx2.Response(200, json={"id": "x", "object": "chat.completion"}))
    with pytest.raises(LLMBadResponse, match="no choices"):
        await call(make(wire, redis=FakeRedis()))


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["not json at all", "", "   "])
async def test_non_json_response_raises_bad_response(content):
    wire = Wire(ok(content=content))
    with pytest.raises(LLMBadResponse):
        await call(make(wire, redis=FakeRedis()))


@pytest.mark.asyncio
async def test_non_object_json_raises_bad_response():
    wire = Wire(ok(content=json.dumps([1, 2, 3])))
    with pytest.raises(LLMBadResponse, match="not an object"):
        await call(make(wire, redis=FakeRedis()))


@pytest.mark.asyncio
async def test_response_missing_required_key_raises():
    wire = Wire(ok(content=json.dumps({"verdict": "hold"})))
    with pytest.raises(LLMBadResponse, match="confidence"):
        await call(make(wire, redis=FakeRedis()))


@pytest.mark.asyncio
async def test_usage_is_absent_safe():
    wire = Wire(ok(usage={"prompt_tokens": 10, "completion_tokens": 2}))
    result = await call(make(wire, redis=FakeRedis()))
    assert result.usage == {"input": 10, "output": 2}


# ── Logging ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_usage_log_line_carries_cost_and_never_the_prompt(caplog):
    wire = Wire(ok())
    with caplog.at_level(logging.INFO):
        await call(make(wire, redis=FakeRedis()))
    line = next(r.message for r in caplog.records if r.message.startswith("llm call "))
    for token in ("label=verdict", f"model={GLM}", "in=1204", "out=187",
                  "cacheRead=1100", "cacheWrite=0", "reasoning=142",
                  "cost=0.00142", "finish=stop", "callsToday=1/100"):
        assert token in line, (token, line)
    assert "ms=" in line
    assert "AAPL" not in line and "analyst" not in line and "sk-or-test" not in line


@pytest.mark.asyncio
async def test_one_warning_line_per_raised_error(caplog):
    wire = Wire(err(429, headers={"retry-after": "60"}))
    with caplog.at_level(logging.WARNING):
        with pytest.raises(LLMRateLimited):
            await call(make(wire, redis=FakeRedis()))
    lines = [r.message for r in caplog.records if r.message.startswith("llm error ")]
    assert len(lines) == 1
    line = lines[0]
    assert line.startswith("llm error label=verdict ")
    for token in (f"model={GLM}", "err=LLMRateLimited", "status=429", "callsToday=1/100"):
        assert token in line, (token, line)
    assert "ms=" in line
    # the field set, and nothing else
    assert sorted(f.split("=")[0] for f in line.removeprefix("llm error ").split()) == [
        "callsToday", "err", "label", "model", "ms", "status"]
    assert "nope" not in line and "openrouter.ai" not in line and "sk-or-test" not in line


@pytest.mark.asyncio
async def test_preflight_error_logs_one_warning_with_no_status(caplog):
    with caplog.at_level(logging.WARNING):
        with pytest.raises(LLMNotConfigured):
            await call(make(Wire(ok()), key="", redis=FakeRedis()))
    lines = [r.message for r in caplog.records if r.message.startswith("llm error ")]
    assert len(lines) == 1
    assert "status=-" in lines[0] and "callsToday=-/100" in lines[0]


# ── Wiring ───────────────────────────────────────────────────────

def test_build_provider_returns_the_one_provider():
    p = build_provider(Settings(_env_file=None), None)
    assert isinstance(p, OpenAICompatProvider)


# ── Part 4.4: cache_system ───────────────────────────────────────

@pytest.mark.asyncio
async def test_system_is_plain_string_by_default():
    """Every caller before 4.4 sends exactly what it sent before."""
    wire = Wire(ok())
    await call(make(wire, redis=FakeRedis()))
    assert wire.body["messages"][0] == {"role": "system", "content": "You are an analyst."}
    assert "cache_control" not in json.dumps(wire.body)


@pytest.mark.asyncio
async def test_cache_system_sends_cache_control_block():
    wire = Wire(ok(usage={"prompt_tokens": 1500, "completion_tokens": 10,
                          "prompt_tokens_details": {"cached_tokens": 0,
                                                    "cache_write_tokens": 1400}}))
    result = await call(make(wire, redis=FakeRedis()), cache_system=True)
    assert wire.body["messages"][0] == {"role": "system", "content": [
        {"type": "text", "text": "You are an analyst.",
         "cache_control": {"type": "ephemeral"}}]}
    assert wire.body["messages"][1] == {"role": "user", "content": "AAPL, swing."}
    # cacheWrite > 0 is how the live check reads "the prefix cleared 1,024".
    assert result.usage["cacheWrite"] == 1400 and result.usage["cacheRead"] == 0


# ── Part 4.4: LLM_PROVIDER_ORDER and the serving host ────────────

def served(host, **kw):
    """ok() with OpenRouter's top-level `provider` field on the body."""
    response = ok(**kw)
    body = json.loads(response.content)
    if host is not None:
        body["provider"] = host
    return httpx2.Response(200, json=body)


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [GLM, SONNET, HAIKU])
async def test_provider_field_present_with_the_default(model):
    """No setting anywhere: the declared default asks for Anthropic first and
    lets OpenRouter fall back."""
    assert Settings.model_fields["llm_provider_order"].default == "anthropic"
    wire = Wire(ok())
    await call(make(wire, redis=FakeRedis()), model=model)
    assert wire.body["provider"] == {"order": ["anthropic"], "allow_fallbacks": True}
    assert ("reasoning" in wire.body) is (model != HAIKU), "reasoning rides beside it, unchanged"


@pytest.mark.asyncio
async def test_comma_list_keeps_its_order():
    wire = Wire(ok())
    await call(make(wire, redis=FakeRedis(),
                    llm_provider_order=" Google-Vertex , anthropic,amazon-bedrock/us "))
    assert wire.body["provider"]["order"] == ["google-vertex", "anthropic", "amazon-bedrock/us"]


@pytest.mark.parametrize("order", ["anthropic", "a,b,c", "google-vertex", " x "])
def test_allow_fallbacks_is_always_true(order):
    """There is no false path: the order is a preference, never a pin."""
    from providers import openai_compat_provider as module
    assert module.provider_routing(order)["allow_fallbacks"] is True
    import inspect
    assert '"allow_fallbacks": False' not in inspect.getsource(module)


@pytest.mark.parametrize("bad", ["", "   ", ",", "anthropic,", "anthropic; drop", "a b",
                                 "../x", "{}", "anthropic!"])
def test_empty_or_malformed_provider_order_is_refused_at_startup(bad):
    """A config error, not an opt-out: Settings will not construct, so the
    service will not boot, and there is no request without the field."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError) as info:
        make(llm_provider_order=bad)
    assert "LLM_PROVIDER_ORDER" in str(info.value)


@pytest.mark.asyncio
async def test_an_order_that_got_past_settings_still_never_reaches_the_wire():
    provider = make(wire := Wire(ok()), redis=(r := FakeRedis()))
    object.__setattr__(provider._settings, "llm_provider_order", "")
    with pytest.raises(ValueError):
        await call(provider)
    assert wire.requests == [] and r.store == {}, "nothing sent, nothing reserved"


@pytest.mark.asyncio
async def test_host_recorded_from_the_response_body(caplog):
    with caplog.at_level("INFO"):
        result = await call(make(Wire(served("Anthropic")), redis=FakeRedis()))
    assert result.host == "Anthropic"
    assert "host=Anthropic" in caplog.text
    assert not [r for r in caplog.records if r.levelname == "WARNING"], "first choice served: quiet"


@pytest.mark.asyncio
@pytest.mark.parametrize("host", [None, "", "   ", 7, {"name": "x"}])
async def test_host_absent_or_unusable_is_none_and_never_warns(host, caplog):
    with caplog.at_level("WARNING"):
        result = await call(make(Wire(served(host)), redis=FakeRedis()))
    assert result.host is None and "fell back" not in caplog.text


@pytest.mark.asyncio
async def test_fallback_warning_fires_when_another_host_served(caplog):
    with caplog.at_level("WARNING"):
        result = await call(make(Wire(served("Google Vertex")), redis=FakeRedis()))
    assert result.host == "Google Vertex", "still recorded, still a success"
    (record,) = [r for r in caplog.records if r.levelname == "WARNING"]
    assert "'Google Vertex'" in record.message and "'anthropic'" in record.message
    assert "fell back" in record.message


@pytest.mark.asyncio
async def test_fallback_compares_against_the_first_entry_only(caplog):
    provider = make(Wire(served("Google Vertex")), redis=FakeRedis(),
                    llm_provider_order="google-vertex/us,anthropic")
    with caplog.at_level("WARNING"):
        await call(provider)
    assert "fell back" not in caplog.text, "a region suffix on the order entry is ignored"

    provider = make(Wire(served("Anthropic")), redis=FakeRedis(),
                    llm_provider_order="google-vertex,anthropic")
    with caplog.at_level("WARNING"):
        await call(provider)
    assert "fell back" in caplog.text, "the second choice serving IS a fallback"
