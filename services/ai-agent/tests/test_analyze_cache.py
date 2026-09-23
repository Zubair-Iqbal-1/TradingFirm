"""
Part 4.4 — POST /analyze/{ticker}: the verdict cache, the input fingerprint,
the in-flight lock, and what happens when a paid verdict cannot be stored.
Same fakes as test_analyze.py (split from it for size). Nothing opens a socket.
"""

import json
from decimal import Decimal

import httpx
import pytest

import cache
import db
import main
from providers.base import LLMResult
from tests.test_analyze import (  # noqa: F401  (`app` is a fixture)
    GO, HEALTH, LABEL, MODEL, Store, app, news_item, post,
)
from tests.test_cache import FakeRedis


# ── Cache, fingerprint, lock ─────────────────────────────────────

def test_repeat_analyze_is_served_from_cache(app):
    client, _, state = app()
    first = post(client).json()
    second = post(client).json()

    assert state.provider.count("verdict") == 1
    assert second["cached"] is True and second["verdictId"] == first["verdictId"]
    assert second["verdict"] == first["verdict"] and second["usage"] == {}
    assert second["servedCount"] == 1 and state.db_pool.served == 1
    assert len(state.db_pool.ledger()) == 1, "a cache hit is not a wire call: no ledger row"
    key = [k for k in state.redis.store if "verdict:" in k][0]
    assert key == f"tf:ai:verdict:{db.DEV_USER_ID}:AAPL:swing:auto"
    assert state.redis.ttls[key] == main.settings.verdict_cache_ttl == 14400


CHANGES = {
    "new high-relevance event": lambda w: w.dossier["sections"]["news"]["items"].append(
        news_item(7, {**LABEL, "eventKey": "aapl-ceo-resigns"})),
    "earnings date moved": lambda w: w.dossier["sections"]["events"]["items"].__setitem__(
        0, {"type": "earnings", "at": "2026-11-05T00:00:00Z", "meta": {}}),
    "regime changed": lambda w: setattr(w, "health", httpx.Response(200, json={**HEALTH, "regime": "DANGER"})),
    "new bar": lambda w: w.dossier.__setitem__("asOf", "2026-09-21T00:00:00Z"),
    "new macro brief": lambda w: setattr(w, "brief", httpx.Response(200, json={
        "id": "7d0c0000-0000-4000-8000-000000000001", "brief": {}})),
}


@pytest.mark.parametrize("name", list(CHANGES))
def test_fingerprint_change_invalidates(app, name):
    client, world, state = app()
    post(client)
    CHANGES[name](world)
    out = post(client).json()
    assert out["cached"] is False and state.provider.count("verdict") == 2, name


def test_settings_or_prompt_change_invalidates(app, monkeypatch):
    client, _, state = app()
    post(client)
    state.db_pool.answers["users.settings"] = {"account_size": Decimal("40000"),
                                               "risk_per_trade_pct": Decimal("1.0")}
    assert post(client).json()["cached"] is False
    monkeypatch.setattr(main.settings, "llm_model", "z-ai/glm-5.3")
    assert post(client).json()["cached"] is False


def test_projection_version_bump_invalidates(app, monkeypatch):
    """Spec verdict-units decision 3: a cache entry written under an older
    projection is a miss, whatever the prompt's sha says."""
    import analyze
    client, _, state = app()
    post(client)
    monkeypatch.setattr(analyze, "PROJECTION_VERSION", analyze.PROJECTION_VERSION + 1)
    out = post(client).json()
    assert out["cached"] is False and state.provider.count("verdict") == 2


def test_low_relevance_headline_keeps_cache(app):
    client, world, state = app()
    post(client)
    world.dossier["sections"]["news"]["items"].append(
        news_item(8, {**LABEL, "relevance": "medium", "eventKey": "aapl-analyst-note"}))
    assert post(client).json()["cached"] is True and state.provider.count("verdict") == 1


def test_a_cached_analyze_can_still_pay_one_classifier_call(app):
    """That is how a new headline is found. It is low relevance here, so the
    verdict is still served from cache."""
    client, world, state = app()
    post(client)
    world.dossier["sections"]["news"]["items"].append(news_item(9))
    state.provider.verdict = GO

    async def low(system, user, schema, **kw):
        state.provider.calls.append({"label": kw["label"], "user": user})
        return LLMResult(data={"items": [{"index": 0, "relevance": "low", "sentiment": 0.0,
                                          "category": "other", "oneLine": "Noise.",
                                          "eventKey": "aapl-listicle-mention"}]},
                         model=MODEL, finish_reason="stop", duration_ms=1, usage={"cost": 0.004})
    state.provider.complete_structured = low
    out = post(client).json()
    assert out["cached"] is True and out["classifier"]["calls"] == 1


def test_price_move_of_one_atr_invalidates(app):
    """Same bar date, same everything; only the close moved. Under one ATR
    (1.2) from the cached verdict's entry keeps it; one ATR or more does not."""
    client, world, state = app()
    assert post(client).json()["entry"] == 50.0
    world.dossier["sections"]["indicators"]["close"] = 51.19
    assert post(client).json()["cached"] is True
    world.dossier["sections"]["indicators"]["close"] = 51.20
    out = post(client).json()
    assert out["cached"] is False and out["entry"] == 51.2
    assert state.provider.count("verdict") == 2
    # The new verdict is the reference now: 51.20 -> 50.10 is under one ATR.
    world.dossier["sections"]["indicators"]["close"] = 50.10
    assert post(client).json()["cached"] is True


def test_fresh_bypasses_cache(app):
    client, _, state = app()
    post(client)
    out = post(client, fresh="true").json()
    assert out["cached"] is False and state.provider.count("verdict") == 2


def test_cache_entry_pointing_at_no_row_is_a_miss(app):
    client, _, state = app()
    post(client)
    state.db_pool.row = None
    assert post(client).json()["cached"] is False


def test_concurrent_analyze_is_409(app):
    client, _, state = app()
    state.redis.store[f"tf:ai:lock:analyze:{db.DEV_USER_ID}:AAPL:swing:auto"] = "1"
    resp = post(client)
    assert resp.status_code == 409 and "already running" in resp.json()["detail"]
    assert state.provider.count("verdict") == 0 and state.db_pool.row is None


def test_lock_ttl_outlives_the_llm_timeout(app):
    assert cache.TTL_ANALYZE_LOCK == 200 > main.settings.llm_timeout


@pytest.mark.parametrize("redis", [None, "raising"])
def test_redis_down_analyze_still_works(app, redis):
    client, _, state = app(redis=FakeRedis(raises=True) if redis == "raising" else None)
    assert post(client).json()["stored"] is True
    assert post(client).json()["cached"] is False, "no cache without Redis: it pays again"
    assert state.provider.count("verdict") == 2


# ── Storage failures after a paid answer ─────────────────────────

def test_store_failure_returns_verdict_unstored(app, caplog):
    pool = Store(raise_on="INSERT INTO ai.verdicts")
    client, _, state = app(pool=pool)
    with caplog.at_level("ERROR"):
        resp = post(client)
    out = resp.json()

    assert resp.status_code == 200
    assert out["stored"] is False and out["verdictId"] is None and out["verdict"]["verdict"] == "wait"
    assert not any("verdict:" in k for k in state.redis.store), "never cache what has no row"
    assert state.redis.store[f"tf:ai:state:ledger_missed:{cache.et_day()}"] == "1"

    # D5: the whole row is in the log, so it can be backfilled by hand.
    line = [r.message for r in caplog.records if "VERDICT NOT STORED" in r.message][0]
    payload = json.loads(line[line.index("{"):])
    assert set(payload) == {"verdict", "llmCall"}
    assert set(db.VERDICT_COLUMNS) <= set(payload["verdict"])
    assert payload["verdict"]["plan_proposed"]["stop"] == 46.6
    assert payload["verdict"]["dossier"]["ticker"] == "AAPL"
    assert payload["llmCall"]["cost_usd"] == "0.0212" and payload["llmCall"]["tokens_in"] == 6100


def test_ledger_row_failure_rolls_the_verdict_back_too(app):
    """One transaction: a verdict without its ledger row would make the cap
    seed under-count, so neither is kept and the payload goes to the log."""
    pool = Store(raise_on="INSERT INTO ai.llm_calls")
    client, _, state = app(pool=pool)
    out = post(client).json()
    assert out["stored"] is False and pool.statements("INSERT INTO ai.verdicts") == []


def test_bump_failure_does_not_fail_a_cache_hit(app):
    client, _, state = app()
    post(client)
    state.db_pool.raise_on = "served_count = served_count + 1"
    assert post(client).json()["cached"] is True


def test_cached_verdict_read_failure_is_a_miss(app):
    client, _, state = app()
    post(client)
    state.db_pool.raise_on = "FROM ai.verdicts"
    out = post(client).json()
    assert out["cached"] is False and state.provider.count("verdict") == 2


def test_plan_math_version_in_fingerprint(app, monkeypatch):
    """A verdict cached under plan math v1 is never served after the v2
    deploy: the version is in the fingerprint (4.8a decision 4)."""
    import analyze
    client, _, state = app()
    assert post(client).json()["cached"] is False
    assert post(client).json()["cached"] is True
    monkeypatch.setattr(analyze, "PLAN_MATH_VERSION", analyze.PLAN_MATH_VERSION + 1)
    out = post(client).json()
    assert out["cached"] is False and len(state.provider.calls) == 2
