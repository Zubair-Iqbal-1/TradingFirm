"""Part 3.4c commit 3b — the block on the row, the payload, the two
/market routes and /health (spec W1, W2, W5, F10, F11, F12b). Postgres
through db.py's real helpers over a fake pool; no socket."""

import json
import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import db
import main
import news_poller
import scheduler
from scoring import alert_manager, weekend
from tests.fake_redis import FakeRedis

BLOCK = {
    "version": 1, "level": "HIGH", "points": 4,
    "reasons": [{"code": "scheduled_event", "detail": "1 event(s): EU tariff deadline", "weight": 2},
                {"code": "vix_high", "detail": "VIX at 26.0", "weight": 2}],
    "inputs": {"regime": "CAUTIOUS", "cappedScore": 58, "baseScore": 64, "regimeSource": "capped",
               "baseScoreAsOf": "2026-09-17", "vixLevel": 26.0, "vixPrevClose": 16.0,
               "vix5dChangePct": 62.5, "vixDirection": "rising",
               "vixAsOf": "2026-09-18T19:55:00+00:00", "vixPartial": True, "quotesSource": "fresh",
               "gapHours": 65.5, "closeAt": "2026-09-18T20:00:00+00:00",
               "nextOpenAt": "2026-09-21T13:30:00+00:00",
               "events": {"status": "ok", "coverageShort": False, "items": []},
               "news": {"status": "ok", "hours": 24, "matched": []}, "activeSituation": None},
    "assessedAt": "2026-09-18T19:55:00+00:00",
}
MONITORS = {"vix": {"score": 40, "raw": {"level": 26.0}, "detail": "", "stale": False, "weight": 25}}


def health(**over):
    body = {"score": 58, "regime": "CAUTIOUS", "coverage": 100, "stale": False, "staleMonitors": [],
            "monitors": MONITORS, "checkedAt": "2026-09-18T19:55:00+00:00",
            "inputs": {"asOf": "x", "source": "fresh", "reason": None, "staleTickers": []},
            "futures": {}, "overlay": {"base": 64, "cap": 69, "capped": True, "status": "applied",
                                       "movePct": -1.8, "esPct": -1.8, "nqPct": -1.6},
            "weekend": BLOCK}
    body.update(over)
    return body


# ── W1: the row ──────────────────────────────────────────────────

def test_row_carries_the_block():
    stored = json.loads(db.health_indicators(health(), "market", None, None))
    assert stored["weekend"] == BLOCK


def test_row_weekend_is_null_off_window():
    stored = json.loads(db.health_indicators(health(weekend=None), "market", None, None))
    assert stored["weekend"] is None


def test_row_weekend_is_null_when_the_key_was_never_set():
    """A night check's health dict has no `weekend` key at all."""
    body = health()
    del body["weekend"]
    assert json.loads(db.health_indicators(body, "night", None, None))["weekend"] is None


# ── W2: the payload ──────────────────────────────────────────────

def test_payload_keys_are_append_only():
    """3.4b's order is untouched and `weekend` is appended last."""
    assert alert_manager.PAYLOAD_KEYS == (
        "score", "regime", "reason", "recovery", "previousScore", "previousRegime",
        "trend", "stale", "coverage", "checkedAt", "monitors",
        "newsPollStale", "lastNewsPollAt", "newsLastError",
        "pausedSeconds", "kind", "overlay", "weekend")
    assert alert_manager.NEWS_KEYS == ("newsPollStale", "lastNewsPollAt", "newsLastError")


def test_payload_carries_the_block():
    payload = alert_manager.build_payload(health(), None, "initial", "stable", kind="market")
    assert set(payload) == set(alert_manager.PAYLOAD_KEYS)
    assert payload["weekend"] == BLOCK


@pytest.mark.parametrize("level", ["LOW", "ELEVATED", "HIGH"])
def test_a_block_never_causes_a_publish(level):
    """W2: the block rides along; the throttle's decision is 3.4's alone."""
    last = {"score": 58, "regime": "CAUTIOUS",
            "publishedAt": datetime(2026, 9, 18, 19, 50, tzinfo=timezone.utc)}
    now = datetime(2026, 9, 18, 19, 55, tzinfo=timezone.utc)
    block = {**BLOCK, "level": level}
    assert alert_manager.decide(last, health(weekend=block), now) == (False, None)
    assert alert_manager.decide(last, health(weekend=None), now) == (False, None)


@pytest.mark.asyncio
async def test_payload_never_carries_nonfinite_weekend():
    """F12b at the seam: publish_health uses allow_nan=False, so a block with
    a NaN would kill the publish — drop_if_nonfinite runs before it can."""
    dirty = {**BLOCK, "points": math.nan}
    block, dropped = weekend.drop_if_nonfinite(dirty)
    assert (block, dropped) == (None, "nan")
    r = FakeRedis()
    out = await alert_manager.publish_health(r, health(weekend=block), "stable",
                                             now=datetime(2026, 9, 18, 19, 55, tzinfo=timezone.utc),
                                             kind="market")
    assert out["published"] is True
    assert json.loads(r.published[0][1])["weekend"] is None


@pytest.mark.asyncio
async def test_publish_would_fail_on_a_nan_block_if_it_were_not_dropped():
    """Why F12 exists at all: the unguarded block does break the publish."""
    r = FakeRedis()
    with pytest.raises(ValueError):
        await alert_manager.publish_health(r, health(weekend={**BLOCK, "points": math.nan}), "stable",
                                           now=datetime(2026, 9, 18, 19, 55, tzinfo=timezone.utc),
                                           kind="market")
    assert r.published == []


# ── W5: check_status and /health ─────────────────────────────────

def test_weekend_status_from_a_block():
    assert scheduler.weekend_status(BLOCK, None) == {
        "weekendLevel": "HIGH", "weekendReasonCount": 2, "weekendDropped": None}
    assert scheduler.weekend_status(None, "nan") == {
        "weekendLevel": None, "weekendReasonCount": None, "weekendDropped": "nan"}
    assert scheduler.weekend_status(None, None)["weekendLevel"] is None


@pytest.fixture
def health_client(monkeypatch):
    def _make(status=None, token=""):
        main.app.state.db_pool = None
        main.app.state.redis = None
        main.app.state.check_status = status or {}
        monkeypatch.setattr(main.settings, "weekend_write_token",
                            type(main.settings.weekend_write_token)(token))
        return TestClient(main.app)
    yield _make
    main.app.state.check_status = {}


def test_health_reports_the_last_block(health_client):
    body = health_client(scheduler.weekend_status(BLOCK, None)).get("/health").json()
    assert body["weekendLevel"] == "HIGH" and body["weekendReasonCount"] == 2
    assert body["weekendDropped"] is None and body["weekendWriteConfigured"] is False


def test_health_reports_the_write_route_by_shape_only(health_client):
    body = health_client({}, token="a-secret-value").get("/health").json()
    assert body["weekendWriteConfigured"] is True
    assert "a-secret-value" not in json.dumps(body)


def test_health_before_any_check_has_null_weekend_fields(health_client):
    body = health_client({}).get("/health").json()
    assert body["weekendLevel"] is None and body["weekendReasonCount"] is None


# ── The two /market routes, and F10 ──────────────────────────────

def indicators(**over):
    body = {"kind": "market", "coverage": 100, "stale": False, "staleMonitors": [],
            "monitors": MONITORS, "inputs": {}, "settleScore": 60, "settleCheckedAt": None,
            "overlay": None, "futures": {}, "weekend": BLOCK}
    body.update(over)
    return json.dumps(body)


class ReadPool:
    def __init__(self, latest):
        self.latest = latest

    async def fetchrow(self, sql, *args):
        return self.latest

    async def fetch(self, sql, *args):
        return []


@pytest.fixture
def client_with(monkeypatch):
    monkeypatch.setattr(news_poller.settings, "news_poll_enabled", False)
    monkeypatch.setattr(main.app.state, "news_status", news_poller.initial_news_status(),
                        raising=False)

    def _make(ind):
        main.app.state.db_pool = ReadPool(
            {"checked_at": datetime.now(timezone.utc) - timedelta(minutes=2), "score": 58,
             "regime": "CAUTIOUS", "trend": "stable", "indicators": ind})
        main.app.state.redis = None
        return TestClient(main.app)
    yield _make
    main.app.state.db_pool = None


def test_market_health_serves_the_block(client_with):
    body = client_with(indicators()).get("/market/health").json()
    assert body["weekend"] == BLOCK
    assert body["weekend"]["inputs"]["regimeSource"] == "capped"


def test_market_indicators_serves_the_block(client_with):
    body = client_with(indicators()).get("/market/indicators").json()
    assert body["weekend"] == BLOCK and body["futures"] == {}


def test_old_rows_serve_null_weekend(client_with):
    """F10: a row written before 3.4c has no key; both routes answer null,
    never 500 and never a missing field."""
    pre_3_4c = json.dumps({"kind": "market", "coverage": 100, "stale": False,
                           "staleMonitors": [], "monitors": MONITORS, "inputs": {},
                           "settleScore": 60, "settleCheckedAt": None})
    client = client_with(pre_3_4c)
    assert client.get("/market/health").json()["weekend"] is None
    assert client.get("/market/indicators").json()["weekend"] is None


def test_block_absent_off_window_serves_null(client_with):
    """F9 at the route: a Wednesday row stores null and serves null."""
    client = client_with(indicators(weekend=None))
    assert client.get("/market/health").json()["weekend"] is None
    assert client.get("/market/indicators").json()["weekend"] is None
