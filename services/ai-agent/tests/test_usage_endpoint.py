"""
Part 4.2 — GET /usage: the running cost total, readable without logging into
OpenRouter (spec gate iii).

TestClient over the app without the lifespan, state set by hand, so the route
is exercised exactly as it runs. No socket, no LLM call.
"""

import pytest
from fastapi.testclient import TestClient

import cache
import main
from tests.test_cache import NOON, FakeRedis


@pytest.fixture
def client():
    saved = {k: getattr(main.app.state, k, None)
             for k in ("redis", "memory_caps", "memory_cost", "provider")}

    def _make(redis=None, caps=None, cost=None, provider=None):
        main.app.state.redis = redis
        main.app.state.memory_caps = caps if caps is not None else {
            cache.STATE_LLM_CALLS: cache.MemoryCap(),
            cache.STATE_CLASSIFIER_CALLS: cache.MemoryCap(),
        }
        main.app.state.memory_cost = cost if cost is not None else cache.MemoryCost()
        main.app.state.provider = provider
        return TestClient(main.app)

    yield _make
    for k, v in saved.items():
        setattr(main.app.state, k, v)


def test_usage_reports_calls_caps_and_cost(client):
    r = FakeRedis()
    day = cache.day_counter_key(name=cache.STATE_LLM_CALLS)
    r.store[day] = "12"
    r.store[cache.day_counter_key(name=cache.STATE_CLASSIFIER_CALLS)] = "3"
    r.store[cache.day_counter_key(name=cache.STATE_COST_DAY)] = "0.0412"
    r.store[cache.month_counter_key()] = "0.9137"

    body = client(redis=r).get("/usage").json()

    assert body["source"] == "redis"
    assert body["day"] == cache.et_day() and body["month"] == cache.et_month()
    assert body["calls"] == {"llmToday": 12, "classifierToday": 3, "costMissingToday": 0,
                             "ledgerMissedToday": 0}
    assert body["caps"] == {
        "llmDaily": main.settings.llm_daily_call_cap,
        "classifierDaily": main.settings.llm_classifier_daily_call_cap,
    }
    assert body["costUsd"] == {"today": 0.0412, "month": 0.9137}
    assert body["cooldown"] == {"source": "openrouter", "secondsLeft": None, "cause": None}


def test_usage_endpoint_reports_memory_source(client):
    """Redis down: the answer is this process's own counting, and `source`
    says so — a small number there means Redis is down, not a quiet day."""
    caps = {cache.STATE_LLM_CALLS: cache.MemoryCap(),
            cache.STATE_CLASSIFIER_CALLS: cache.MemoryCap()}
    caps[cache.STATE_CLASSIFIER_CALLS].incr(cache.et_day())
    cost = cache.MemoryCost()
    cost.add(cache.et_day(), cache.et_month(), 0.0057)
    cost.add_missing(cache.et_day())

    for redis in (None, FakeRedis(raises=True)):
        body = client(redis=redis, caps=caps, cost=cost).get("/usage").json()
        assert body["source"] == "memory"
        assert body["calls"]["classifierToday"] == 1
        assert body["calls"]["costMissingToday"] == 1
        assert body["costUsd"]["today"] == 0.0057


def test_usage_shows_an_active_cooldown_and_its_cause(client):
    r = FakeRedis()
    r.store[cache.cooldown_key(cache.SOURCE_LLM)] = "429"
    r.ttls[cache.cooldown_key(cache.SOURCE_LLM)] = 851

    body = client(redis=r).get("/usage").json()
    assert body["cooldown"] == {"source": "openrouter", "secondsLeft": 851, "cause": "429"}


def test_usage_reads_the_providers_in_process_cooldown_when_redis_is_down(client):
    """With Redis down the provider's own clock is the only record that the
    gateway refused us."""
    class FakeProvider:
        def __init__(self):
            self._memory_cooldowns = cache.MemoryCooldowns()

    provider = FakeProvider()
    provider._memory_cooldowns.start(cache.canonical(cache.SOURCE_LLM), "402")

    body = client(redis=None, provider=provider).get("/usage").json()
    assert body["cooldown"]["cause"] == "402"
    assert body["cooldown"]["secondsLeft"] > 0


def test_usage_makes_no_llm_call_and_writes_nothing(client):
    r = FakeRedis()
    client(redis=r).get("/usage")
    assert r.store == {}, "GET /usage is a read: it must not create keys"
    assert r.expire_calls == []


def test_usage_never_exposes_the_key(client):
    body = client(redis=FakeRedis()).get("/usage").text
    assert "sk-" not in body
    assert "api_key" not in body.lower()
