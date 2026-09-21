"""Part 4.4 — the ledger: wire calls only, never fails a paid call, and
re-seeds the daily caps at startup."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

import cache
import db
import ledger
from providers.base import (
    LLMAuthFailed, LLMBadResponse, LLMCapExceeded, LLMCooledDown, LLMNotConfigured,
    LLMRateLimited, LLMRefused, LLMRejected, LLMResult, LLMUnavailable,
)
from tests.fake_pool import FakePool
from tests.test_cache import FakeRedis

NOW = datetime(2026, 9, 21, 16, 0, tzinfo=timezone.utc)     # 12:00 ET
DAY = "2026-09-21"


def result(host="Anthropic", **usage):
    return LLMResult(data={}, model="anthropic/claude-sonnet-5", finish_reason="stop",
                     duration_ms=1, usage=usage, host=host)


def caps():
    return {cache.STATE_LLM_CALLS: cache.MemoryCap(),
            cache.STATE_CLASSIFIER_CALLS: cache.MemoryCap()}


def test_build_maps_usage_and_keeps_missing_cost_null():
    row = ledger.build(route="analyze", label="verdict", model="cfg", outcome="ok",
                       counters=["llm_calls"], ticker="AAPL", user_id=db.DEV_USER_ID, now=NOW,
                       result=result(input=6000, output=900, reasoning=300, cacheRead=0,
                                     cacheWrite=1400, cost=0.0412))
    assert set(row) == set(db.LLM_CALL_COLUMNS)
    assert (row["et_day"], row["model"]) == (date(2026, 9, 21), "anthropic/claude-sonnet-5")
    assert (row["tokens_in"], row["tokens_out"], row["tokens_reasoning"]) == (6000, 900, 300)
    assert (row["cache_read_tokens"], row["cache_write_tokens"]) == (0, 1400)
    assert row["cost_usd"] == Decimal("0.0412")
    assert row["host"] == "Anthropic", "which OpenRouter host served the call"
    assert ledger.build(route="analyze", label="l", model="m", outcome="ok", counters=[],
                        result=result(host=None), now=NOW)["host"] is None

    no_cost = ledger.build(route="classify", label="l", model="cfg", outcome="ok",
                           counters=[], result=result(input=1), now=NOW)
    assert no_cost["cost_usd"] is None, "no cost reported is NULL, never 0"
    failed = ledger.build(route="classify", label="l", model="cfg", outcome="unavailable",
                          counters=[], now=NOW)
    assert failed["model"] == "cfg" and failed["tokens_in"] is None and failed["cost_usd"] is None
    assert failed["host"] is None, "a failed call never reported one"


def test_et_day_is_the_et_date_not_utc():
    late = datetime(2026, 9, 22, 2, 0, tzinfo=timezone.utc)          # 22:00 ET on the 21st
    assert ledger.build(route="analyze", label="l", model="m", outcome="ok",
                        counters=[], now=late)["et_day"] == date(2026, 9, 21)


@pytest.mark.parametrize("error, outcome", [
    (LLMRateLimited("x"), "rate_limited"), (LLMUnavailable("x"), "unavailable"),
    (LLMRejected("x"), "rejected"), (LLMRefused("x"), "refused"),
    (LLMBadResponse("x"), "bad_response"), (LLMAuthFailed("x"), "auth_failed"),
    (LLMNotConfigured("x"), None), (LLMCooledDown("x"), None), (LLMCapExceeded("x"), None),
    (ValueError("x"), None),
])
def test_only_post_wire_errors_have_an_outcome(error, outcome):
    assert ledger.outcome_of(error) == outcome


@pytest.mark.asyncio
async def test_pre_wire_refusal_writes_no_row():
    pool = FakePool()
    for error in (LLMNotConfigured("x"), LLMCooledDown("x"), LLMCapExceeded("x")):
        assert await ledger.record_error(pool, FakeRedis(), error, route="analyze",
                                         label="l", model="m", counters=[]) is False
    assert pool.calls == []


@pytest.mark.asyncio
async def test_post_wire_failure_writes_a_row():
    pool = FakePool()
    assert await ledger.record_error(pool, None, LLMUnavailable("x"), route="analyze",
                                     label="verdict", model="m", counters=["llm_calls"],
                                     ticker="AAPL", now=NOW) is True
    args = dict(zip(db.LLM_CALL_COLUMNS, pool.calls[0][2]))
    assert (args["outcome"], args["ticker"], args["counters"]) == ("unavailable", "AAPL", ["llm_calls"])


@pytest.mark.asyncio
@pytest.mark.parametrize("pool", [None, FakePool(raise_on="ai.llm_calls")])
async def test_ledger_failure_never_fails_a_call(pool, caplog):
    r = FakeRedis()
    row = ledger.build(route="analyze", label="verdict", model="m", outcome="ok",
                       counters=["llm_calls"], result=result(input=5, cost=0.01), now=NOW)
    with caplog.at_level("ERROR"):
        assert await ledger.record(pool, r, row) is False
    key = f"tf:ai:state:ledger_missed:{DAY}"
    assert r.store[key] == "1" and r.ttls[key] == cache.TTL_COST
    assert "cost=0.01" in caplog.text, "the unstored row's numbers are in the log"

    assert await ledger.record(pool, FakeRedis(raises=True), row) is False   # Redis down too
    assert await ledger.record(pool, None, row) is False


# ── Seeding ──────────────────────────────────────────────────────

def ledger_pool(llm=7, classifier=3, cost_day="0.21", cost_month="1.5"):
    return FakePool({
        "cost_day": {"llm_calls": llm, "classifier_calls": classifier,
                     "cost_day": Decimal(cost_day)},
        "cost_month": {"cost_month": Decimal(cost_month)},
    })


@pytest.mark.asyncio
async def test_caps_seeded_from_ledger_take_the_max():
    r, c, cost = FakeRedis(), caps(), cache.MemoryCost()
    r.store[f"tf:ai:state:llm_calls:{DAY}"] = "2"                # Redis short: a compose down
    r.store[f"tf:ai:state:classifier_calls:{DAY}"] = "9"         # Redis ahead: ledger missed rows

    assert await ledger.seed_caps(ledger_pool(), r, c, cost, NOW) is True

    assert r.store[f"tf:ai:state:llm_calls:{DAY}"] == "7"
    assert r.store[f"tf:ai:state:classifier_calls:{DAY}"] == "9", "never lowered"
    assert float(r.store[f"tf:ai:state:cost_day:{DAY}"]) == pytest.approx(0.21)
    assert float(r.store["tf:ai:state:cost_month:2026-09"]) == pytest.approx(1.5)
    assert all(nx for _, _, nx in r.expire_calls), "4.1's rule: EXPIRE ... NX only"
    assert {k for k, _, _ in r.expire_calls if "calls" in k} == {f"tf:ai:state:llm_calls:{DAY}"}


@pytest.mark.asyncio
async def test_seeded_cap_refuses_the_next_call():
    """The point of it: after a compose down, a day already at its cap stays
    at its cap."""
    r, c = FakeRedis(), caps()
    await ledger.seed_caps(ledger_pool(llm=100), r, c, cache.MemoryCost(), NOW)
    assert await cache.reserve_call(r, c[cache.STATE_LLM_CALLS], NOW) == 101


@pytest.mark.asyncio
@pytest.mark.parametrize("r", [None, FakeRedis(raises=True)])
async def test_seed_with_redis_down_fills_memory_caps(r):
    c, cost = caps(), cache.MemoryCost()
    assert await ledger.seed_caps(ledger_pool(), r, c, cost, NOW) is True
    assert c[cache.STATE_LLM_CALLS].count(DAY) == 7
    assert c[cache.STATE_CLASSIFIER_CALLS].count(DAY) == 3
    if r is None:
        assert cost.total(DAY) == pytest.approx(0.21)


@pytest.mark.asyncio
async def test_seed_without_pool_warns(caplog):
    r, c = FakeRedis(), caps()
    with caplog.at_level("WARNING"):
        assert await ledger.seed_caps(None, r, c, cache.MemoryCost(), NOW) is False
        assert await ledger.seed_caps(FakePool(raise_on="ai.llm_calls"), r, c,
                                      cache.MemoryCost(), NOW) is False
    assert r.store == {} and c[cache.STATE_LLM_CALLS].count(DAY) == 0
    assert "no database pool" in caplog.text and "read failed" in caplog.text


@pytest.mark.asyncio
async def test_empty_ledger_seeds_nothing():
    r = FakeRedis()
    assert await ledger.seed_caps(ledger_pool(0, 0, "0", "0"), r, caps(),
                                  cache.MemoryCost(), NOW) is True
    assert r.store == {}
