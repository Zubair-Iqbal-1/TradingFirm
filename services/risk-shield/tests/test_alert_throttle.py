"""Part 3.4 — the publish throttle (spec decision 5). decide() under
frozen time; publish_health() against FakeRedis, no socket."""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import cache
import config
import news_poller
import scheduler
from scoring import alert_manager
from scoring.alert_manager import decide, publish_health
from scoring.regime_classifier import classify
from tests.fake_redis import FakeRedis

NOW = datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)
STATE_KEY = "tf:risk:state:health_published"
PROD_CHANNEL = "tf:risk:health"


def health(score, *, stale=False):
    return {
        "score": score,
        "regime": classify(score),
        "coverage": 100,
        "stale": stale,
        "staleMonitors": [],
        "checkedAt": NOW.isoformat(),
        "monitors": {"vix": {"score": score, "raw": {}, "detail": "", "stale": stale, "weight": 25},
                     "breadth": {"score": None, "raw": {}, "detail": "", "stale": False, "weight": 20}},
    }


def last(score, *, ago=timedelta(minutes=30), regime=None):
    return {"score": score, "regime": regime or classify(score), "publishedAt": NOW - ago}


def store_state(r, score, *, ago=timedelta(minutes=30)):
    r.store[STATE_KEY] = json.dumps(
        {"score": score, "regime": classify(score), "publishedAt": (NOW - ago).isoformat()})


def messages(r):
    return [(ch, json.loads(m)) for ch, m in r.published]


# ── decide() ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_throttle_first_check_publishes():
    assert decide(None, health(75), NOW) == (True, "initial")
    r = FakeRedis()
    assert await publish_health(r, health(75), None, now=NOW) == {"published": True, "reason": "initial"}
    [(_, payload)] = messages(r)
    assert payload["previousScore"] is None and payload["previousRegime"] is None
    assert json.loads(r.store[STATE_KEY]) == {"score": 75, "regime": "HEALTHY", "publishedAt": NOW.isoformat()}
    assert r.ttls[STATE_KEY] == 7 * 86400


def test_throttle_score_move_threshold_is_ten_inclusive():
    prev = last(55)                                  # CAUTIOUS
    assert decide(prev, health(64), NOW) == (False, None)
    assert decide(prev, health(65), NOW) == (True, "score_move")
    assert decide(prev, health(45), NOW) == (True, "score_move")


def test_throttle_regime_change_publishes():
    assert decide(last(72), health(68), NOW) == (True, "regime_change")


def test_throttle_min_interval_fifteen_minutes():
    assert decide(last(72, ago=timedelta(minutes=14, seconds=59)), health(68), NOW) == (False, None)
    assert decide(last(72, ago=timedelta(minutes=15)), health(68), NOW) == (True, "regime_change")
    # A score move is held by the interval too.
    assert decide(last(55, ago=timedelta(minutes=5)), health(65), NOW) == (False, None)


@pytest.mark.asyncio
async def test_throttle_held_change_publishes_when_interval_opens():
    r = FakeRedis()
    t0 = NOW
    await publish_health(r, health(72), None, now=t0)
    assert (await publish_health(r, health(68), None, now=t0 + timedelta(minutes=5)))["published"] is False
    assert json.loads(r.store[STATE_KEY])["score"] == 72          # state untouched while held
    result = await publish_health(r, health(68), None, now=t0 + timedelta(minutes=15))
    assert result == {"published": True, "reason": "regime_change"}
    assert messages(r)[-1][1]["previousScore"] == 72

    # Reverted before the interval opens: nothing is published.
    r = FakeRedis()
    await publish_health(r, health(72), None, now=t0)
    await publish_health(r, health(68), None, now=t0 + timedelta(minutes=5))
    assert (await publish_health(r, health(72), None, now=t0 + timedelta(minutes=15)))["published"] is False
    assert len(r.published) == 1


def test_throttle_critical_bypasses_interval():
    assert decide(last(25, ago=timedelta(minutes=1)), health(15), NOW) == (True, "critical")


def test_throttle_critical_does_not_repeat_every_check():
    assert decide(last(15, ago=timedelta(minutes=1)), health(10), NOW) == (False, None)
    assert decide(last(15, ago=timedelta(minutes=30)), health(10), NOW) == (False, None)
    assert decide(last(15, ago=timedelta(minutes=1)), health(5), NOW) == (True, "critical")


def test_throttle_leaving_critical_is_held_by_interval():
    # 15 → 25 is also a 10-point move: leaving CRITICAL still gets no bypass.
    assert decide(last(15, ago=timedelta(minutes=1)), health(25), NOW) == (False, None)
    assert decide(last(15, ago=timedelta(minutes=15)), health(25), NOW) == (True, "regime_change")


@pytest.mark.asyncio
async def test_throttle_recovery_flag_on_return_to_healthy():
    r = FakeRedis()
    store_state(r, 65)
    await publish_health(r, health(72), None, now=NOW)
    assert messages(r)[-1][1]["recovery"] is True

    r = FakeRedis()
    store_state(r, 72)
    await publish_health(r, health(90), None, now=NOW)           # HEALTHY → HEALTHY move
    assert messages(r)[-1][1]["recovery"] is False

    r = FakeRedis()
    await publish_health(r, health(90), None, now=NOW)           # initial
    assert messages(r)[-1][1]["recovery"] is False


@pytest.mark.asyncio
async def test_throttle_null_score_never_publishes():
    empty = health(50)
    empty["score"], empty["regime"] = None, None
    assert decide(None, empty, NOW) == (False, None)
    assert decide(last(15, ago=timedelta(minutes=1)), empty, NOW) == (False, None)
    r = FakeRedis()
    store_state(r, 60)
    before = dict(r.store)
    assert await publish_health(r, empty, None, now=NOW) == {"published": False, "reason": None}
    assert r.published == [] and r.store == before


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [
    "not json {",
    json.dumps({"score": "70", "regime": "HEALTHY", "publishedAt": NOW.isoformat()}),
    json.dumps({"score": 70, "regime": "FINE", "publishedAt": NOW.isoformat()}),
    json.dumps({"score": 70, "regime": "HEALTHY"}),
    json.dumps({"score": 70, "regime": "HEALTHY", "publishedAt": "2026-09-10T15:00:00"}),   # naive
    json.dumps([70, "HEALTHY"]),
])
async def test_throttle_corrupt_state_treated_as_absent(stored, caplog):
    r = FakeRedis()
    r.store[STATE_KEY] = stored
    with caplog.at_level(logging.WARNING):
        result = await publish_health(r, health(71), None, now=NOW)
    assert result == {"published": True, "reason": "initial"}
    assert any(r_.levelno == logging.WARNING for r_ in caplog.records)


# ── publish_health() failure branches ────────────────────────────

@pytest.mark.asyncio
async def test_publish_without_redis_skips(caplog):
    with caplog.at_level(logging.WARNING):
        assert await publish_health(None, health(15), None, now=NOW) == {"published": False, "reason": None}
    assert any("Redis unavailable" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_publish_raise_leaves_state_for_retry(caplog):
    r = FakeRedis(fail_publish=True)
    store_state(r, 72)
    before = r.store[STATE_KEY]
    with caplog.at_level(logging.WARNING):
        result = await publish_health(r, health(68), None, now=NOW)
    assert result == {"published": False, "reason": "regime_change"}
    assert r.store[STATE_KEY] == before
    assert any("publish failed" in m.getMessage() for m in caplog.records)

    r.fail_publish = False
    later = NOW + timedelta(minutes=5)
    assert (await publish_health(r, health(68), None, now=later))["published"] is True


@pytest.mark.asyncio
async def test_publish_state_write_raise_is_at_least_once(caplog):
    r = FakeRedis(fail_set=True)
    with caplog.at_level(logging.WARNING):
        assert (await publish_health(r, health(72), None, now=NOW))["published"] is True
    assert len(r.published) == 1
    assert any("may publish again" in m.getMessage() for m in caplog.records)
    # No state landed, so the same health publishes again next check.
    assert (await publish_health(r, health(72), None, now=NOW + timedelta(minutes=5)))["published"] is True
    assert len(r.published) == 2


@pytest.mark.asyncio
async def test_throttle_repeat_check_no_republish():
    r = FakeRedis()
    await publish_health(r, health(72), None, now=NOW)
    for minutes in (5, 30, 120):
        result = await publish_health(r, health(72), None, now=NOW + timedelta(minutes=minutes))
        assert result["published"] is False
    assert len(r.published) == 1


@pytest.mark.asyncio
async def test_publish_payload_shape_and_channel(monkeypatch):
    # Pinned, not read from the container: the twin runs with its own channel.
    monkeypatch.setattr(alert_manager.settings, "health_channel", PROD_CHANNEL)
    r = FakeRedis()
    store_state(r, 45)
    await publish_health(r, health(30, stale=True), "declining", now=NOW)
    [(channel, raw)] = r.published
    assert channel == PROD_CHANNEL
    payload = json.loads(raw, parse_constant=lambda c: pytest.fail(f"non-strict JSON {c}"))
    assert tuple(payload) == alert_manager.PAYLOAD_KEYS
    assert payload == {
        "score": 30, "regime": "DANGER", "reason": "regime_change", "recovery": False,
        "previousScore": 45, "previousRegime": "CAUTIOUS", "trend": "declining", "stale": True,
        "coverage": 100, "checkedAt": NOW.isoformat(), "monitors": {"vix": 30, "breadth": None},
        "newsPollStale": None, "lastNewsPollAt": None, "newsLastError": None,     # Part 3.5, no view passed
        "pausedSeconds": None,                                                    # 3.4 follow-up, no pause
        "kind": None, "overlay": None,                                            # Part 3.4b, no kind passed
        "weekend": None,                                                          # Part 3.4c, off-window
    }
    bad = health(30)
    bad["coverage"] = float("nan")
    with pytest.raises(ValueError):
        await publish_health(FakeRedis(), bad, None, now=NOW)


@pytest.mark.asyncio
async def test_payload_keys_append_only_paused_seconds():
    """3.4 follow-up addition 1 and 3.4b: keys are appended, never reordered."""
    assert alert_manager.PAYLOAD_KEYS == (
        "score", "regime", "reason", "recovery", "previousScore", "previousRegime",
        "trend", "stale", "coverage", "checkedAt", "monitors",
        "newsPollStale", "lastNewsPollAt", "newsLastError", "pausedSeconds",
        "kind", "overlay", "weekend")
    assert alert_manager.NEWS_KEYS == ("newsPollStale", "lastNewsPollAt", "newsLastError")
    r = FakeRedis()
    await publish_health(r, health(72), None, now=NOW)
    await publish_health(r, health(40), None, now=NOW + timedelta(minutes=15), paused_seconds=56844)
    first, second = [payload for _, payload in messages(r)]
    assert first["pausedSeconds"] is None
    assert second["pausedSeconds"] == 56844 and tuple(second) == alert_manager.PAYLOAD_KEYS


@pytest.mark.asyncio
async def test_health_channel_default_and_override(monkeypatch):
    """The code default, independent of the container's environment."""
    monkeypatch.delenv("HEALTH_CHANNEL", raising=False)
    assert config.Settings(_env_file=None).health_channel == PROD_CHANNEL
    monkeypatch.setattr(alert_manager.settings, "health_channel", "tf:risk:dev:health")
    r = FakeRedis()
    await publish_health(r, health(72), None, now=NOW)
    assert [ch for ch, _ in r.published] == ["tf:risk:dev:health"]


@pytest.mark.asyncio
async def test_publish_payload_carries_news_poll_stale(monkeypatch):
    """Part 3.5 addition 8: a scheduled check publishes the news feed's state,
    read from app.state.news_status through news_poller.stale_view."""
    monkeypatch.setattr(news_poller.settings, "news_poll_enabled", True)
    status = news_poller.initial_news_status()
    status.update(startedAt=(NOW - timedelta(hours=3)).isoformat(),
                  lastSuccessAt=(NOW - timedelta(minutes=61)).isoformat(),
                  lastError="ingest: HTTP 422")
    state = SimpleNamespace(redis=FakeRedis(), db_pool=None, cooldowns=cache.MemoryCooldowns(),
                            check_status={}, news_status=status)

    async def fake_compute(r, cooldowns, now=None):
        return health(72)

    monkeypatch.setattr(scheduler, "compute_health", fake_compute)
    monkeypatch.setattr(scheduler.quotes, "download_in_flight", lambda: False)
    await scheduler.run_check(state, "market", clock=lambda: NOW)
    [(_, payload)] = messages(state.redis)
    assert tuple(payload) == alert_manager.PAYLOAD_KEYS
    assert (payload["newsPollStale"], payload["lastNewsPollAt"], payload["newsLastError"]) == (
        True, status["lastSuccessAt"], "ingest: HTTP 422")

    fresh = {**status, "lastSuccessAt": (NOW - timedelta(minutes=5)).isoformat(), "lastError": None}
    r = FakeRedis()
    await publish_health(r, health(72), None, now=NOW,
                         news=news_poller.stale_view(SimpleNamespace(news_status=fresh), NOW))
    assert {k: messages(r)[0][1][k] for k in alert_manager.NEWS_KEYS} == {
        "newsPollStale": False, "lastNewsPollAt": fresh["lastSuccessAt"], "newsLastError": None}

    monkeypatch.setattr(news_poller.settings, "news_poll_enabled", False)
    r = FakeRedis()
    await publish_health(r, health(72), None, now=NOW,
                         news=news_poller.stale_view(SimpleNamespace(news_status=fresh), NOW))
    assert messages(r)[0][1]["newsPollStale"] is None


def test_twin_never_publishes_on_prod_channel():
    """Redis pub/sub ignores the DB index, so the dev twin (Redis DB 1) would
    reach prod subscribers on tf:risk:health. Guards the compose override
    HEALTH_CHANNEL=tf:risk:dev:health: fails if it is dropped. Runs only
    inside the twin (SERVICE_NAME=risk-shield-dev), where that env exists."""
    if os.environ.get("SERVICE_NAME") != "risk-shield-dev":
        pytest.skip("not the risk-shield dev twin")
    for live in (config.settings, alert_manager.settings):
        assert live.health_channel == "tf:risk:dev:health"
        assert live.health_channel != PROD_CHANNEL


def test_state_key_namespace():
    assert cache.state_key(cache.STATE_HEALTH_PUBLISHED) == STATE_KEY
    assert cache.state_key(" Health_Published ") == STATE_KEY
    assert not STATE_KEY.startswith(cache.CACHE_PREFIX)
    for bad in ("", "  ", None, 3):
        with pytest.raises(ValueError):
            cache.state_key(bad)


@pytest.mark.asyncio
async def test_payload_keys_append_only_kind_overlay():
    """Part 3.4b: kind and overlay are appended after pausedSeconds."""
    assert alert_manager.PAYLOAD_KEYS[-3:] == ("kind", "overlay", "weekend")
    record = {"status": "applied", "movePct": -3.2, "esPct": -3.2, "nqPct": -3.0,
              "base": 68, "cap": 39, "capped": True}
    snapshot = health(39)
    snapshot["overlay"] = record
    r = FakeRedis()
    await publish_health(r, snapshot, "declining", now=NOW, kind="night")
    [(_, payload)] = messages(r)
    assert tuple(payload) == alert_manager.PAYLOAD_KEYS
    assert payload["kind"] == "night" and payload["overlay"] == record
    # A check that passes neither (3.4's callers) still publishes both as null.
    r = FakeRedis()
    await publish_health(r, health(72), None, now=NOW)
    assert messages(r)[0][1]["kind"] is None and messages(r)[0][1]["overlay"] is None
