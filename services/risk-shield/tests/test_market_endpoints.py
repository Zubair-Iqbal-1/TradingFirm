"""Part 3.4 — GET /market/health, /market/indicators, /market/history
(spec decision 7). Postgres only: db.py's real read helpers run over a fake
pool; app.state is stubbed without the lifespan, as in test_health.py."""

import json
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
from fastapi.testclient import TestClient

import main
import scheduler
from monitors import quotes
from scoring import health_calculator
from scoring.regime_classifier import REGIMES

SETTLE_AT = "2026-09-09T20:20:00+00:00"
MONITORS = {
    name: {"score": score, "raw": {"x": 1}, "detail": f"{name} detail", "stale": False, "weight": weight}
    for name, score, weight in (("vix", 60, 25), ("breadth", 60, 20), ("spy_trend", 70, 20),
                                ("sector_rotation", 65, 15), ("volume", 85, 10), ("cross_asset", 80, 10))
}
INPUTS = {"asOf": "2026-09-10T14:00:00+00:00", "source": "cached", "reason": None, "staleTickers": []}


def indicators(**over):
    body = {"kind": "market", "coverage": 100, "stale": False, "staleMonitors": [], "monitors": MONITORS,
            "inputs": INPUTS, "settleScore": 60, "settleCheckedAt": SETTLE_AT}
    body.update(over)
    return json.dumps(body)


def row(*, score=64, regime="CAUTIOUS", trend="stable", ind=None, at=None):
    return {"checked_at": at or datetime.now(timezone.utc) - timedelta(minutes=2), "score": score,
            "regime": regime, "trend": trend, "indicators": indicators() if ind is None else ind}


class ReadPool:
    """Answers the read helpers; any write fails the test."""

    def __init__(self, *, latest=None, scored=None, history=(), fail=None):
        self.latest, self.scored, self.history, self.fail = latest, scored, list(history), fail
        self.calls = []

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        if self.fail:
            raise self.fail
        return self.scored if "score IS NOT NULL" in sql else self.latest

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        if self.fail:
            raise self.fail
        return self.history

    async def execute(self, *args):
        raise AssertionError("the /market endpoints never write")


@pytest.fixture(autouse=True)
def _news_poller_off(monkeypatch):
    """The news keys on /market/health read the poller flag and news_status;
    pin both so a row test never depends on another test's app.state."""
    import news_poller
    monkeypatch.setattr(news_poller.settings, "news_poll_enabled", False)
    monkeypatch.setattr(main.app.state, "news_status", news_poller.initial_news_status(), raising=False)


def test_market_health_carries_news_poll_stale(client_with, monkeypatch):
    """Part 3.5 addition 8: newsPollStale / lastNewsPollAt / newsLastError come
    from process memory at request time, never from Postgres."""
    import news_poller
    now = datetime.now(timezone.utc)
    status = news_poller.initial_news_status()
    status.update(startedAt=(now - timedelta(hours=5)).isoformat(),
                  lastSuccessAt=(now - timedelta(hours=2)).isoformat(), lastError="ingest: HTTP 503")
    monkeypatch.setattr(main.app.state, "news_status", status, raising=False)
    monkeypatch.setattr(news_poller.settings, "news_poll_enabled", True)
    pool = ReadPool(latest=row())
    client = client_with(pool)

    body = client.get("/market/health").json()
    assert (body["newsPollStale"], body["lastNewsPollAt"], body["newsLastError"]) == (
        True, status["lastSuccessAt"], "ingest: HTTP 503")

    status.update(lastSuccessAt=(now - timedelta(minutes=5)).isoformat(), lastError=None)
    body = client.get("/market/health").json()
    assert (body["newsPollStale"], body["lastNewsPollAt"], body["newsLastError"]) == (
        False, status["lastSuccessAt"], None)

    monkeypatch.setattr(news_poller.settings, "news_poll_enabled", False)
    assert client.get("/market/health").json()["newsPollStale"] is None
    assert len(pool.calls) == 3                       # one row read per request, nothing more


@pytest.fixture
def client_with():
    def _make(pool):
        main.app.state.db_pool = pool
        main.app.state.redis = None
        return TestClient(main.app)
    yield _make
    main.app.state.db_pool = None
    main.app.state.redis = None


def test_market_health_returns_latest_row(client_with):
    at = datetime.now(timezone.utc) - timedelta(seconds=150)
    pool = ReadPool(latest=row(at=at))
    resp = client_with(pool).get("/market/health")
    assert resp.status_code == 200
    body = resp.json()
    age = body.pop("ageSeconds")
    assert isinstance(age, int) and 150 <= age <= 165
    assert body == {
        "score": 64, "regime": "CAUTIOUS", "trend": "stable",
        "settleScore": 60, "settleCheckedAt": SETTLE_AT,
        "message": "Elevated risk — trade with caution",
        "checkedAt": at.isoformat(), "kind": "market", "coverage": 100, "stale": False,
        "newsPollStale": None, "lastNewsPollAt": None, "newsLastError": None,   # Part 3.5, poller off
        "overlay": None,                                                        # Part 3.4b, a pre-3.4b row
        "weekend": None,                                                        # Part 3.4c, a pre-3.4c row
    }
    assert "previousScore" not in body               # the last publish lives in pub/sub only
    assert len(pool.calls) == 1                       # no lastScored query for a scored row
    assert set(main.REGIME_MESSAGES) == set(REGIMES)


def test_market_health_null_latest_includes_last_scored(client_with):
    scored_at = datetime.now(timezone.utc) - timedelta(minutes=30)
    latest = row(score=None, regime=None, trend=None, ind=indicators(coverage=45, settleScore=None,
                                                                      settleCheckedAt=None))
    pool = ReadPool(latest=latest, scored={"checked_at": scored_at, "score": 71, "regime": "HEALTHY"})
    body = client_with(pool).get("/market/health").json()
    assert (body["score"], body["regime"], body["message"], body["trend"]) == (None, None, None, None)
    assert body["coverage"] == 45
    assert body["lastScored"] == {"score": 71, "regime": "HEALTHY", "checkedAt": scored_at.isoformat()}

    never_scored = ReadPool(latest=latest, scored=None)
    assert client_with(never_scored).get("/market/health").json()["lastScored"] is None


def test_market_endpoints_no_rows_404(client_with):
    client = client_with(ReadPool(latest=None))
    for path in ("/market/health", "/market/indicators"):
        resp = client.get(path)
        assert resp.status_code == 404
        assert resp.json() == {"detail": "no health checks yet"}
    # Distinguishable from a wrong route.
    assert client.get("/market/healthz").json() == {"detail": "Not Found"}


@pytest.mark.parametrize("pool", [
    None,
    ReadPool(fail=asyncpg.InterfaceError("connection lost")),
    ReadPool(fail=TimeoutError("command timeout")),
])
def test_market_endpoints_db_unavailable_503(client_with, pool):
    client = client_with(pool)
    for path in ("/market/health", "/market/indicators", "/market/history"):
        resp = client.get(path)
        assert resp.status_code == 503, path
        assert resp.json() == {"detail": "database unavailable"}


def test_market_indicators_returns_monitors(client_with):
    at = datetime.now(timezone.utc) - timedelta(minutes=1)
    body = client_with(ReadPool(latest=row(at=at))).get("/market/indicators").json()
    assert body == {"checkedAt": at.isoformat(), "kind": "market", "coverage": 100,
                    "inputs": INPUTS, "monitors": MONITORS,
                    "futures": None, "overlay": None,        # Part 3.4b, a pre-3.4b row
                    "weekend": None}                         # Part 3.4c, a pre-3.4c row
    assert {m["weight"] for m in body["monitors"].values()} == {25, 20, 15, 10}


@pytest.mark.parametrize("bad", ["not json {", "[1, 2]", json.dumps({"kind": "market", "monitors": [1]}),
                                 json.dumps({"kind": "market", "monitors": {"vix": 5}})])
def test_market_indicators_malformed_row_is_null_not_500(client_with, bad):
    client = client_with(ReadPool(latest=row(ind=bad)))
    resp = client.get("/market/indicators")
    assert resp.status_code == 200
    assert resp.json()["monitors"] is None
    health = client.get("/market/health")
    assert health.status_code == 200 and health.json()["score"] == 64


def test_market_history_days_validation(client_with):
    client = client_with(ReadPool(history=[]))
    for bad in ("0", "91", "x", "-1"):
        assert client.get(f"/market/history?days={bad}").status_code == 422
    for days in (1, 90):
        resp = client.get(f"/market/history?days={days}")
        assert resp.status_code == 200 and resp.json()["days"] == days
    before = datetime.now(timezone.utc)
    resp = client.get("/market/history")
    assert resp.json() == {"days": 30, "rows": []}
    (_, _, (since,)) = client.app.state.db_pool.calls[-1]
    assert abs((before - timedelta(days=30) - since).total_seconds()) < 5


def test_market_history_rows_ascending_with_nulls(client_with):
    t0 = datetime(2026, 9, 9, 20, 20, tzinfo=timezone.utc)
    history = [
        {"checked_at": t0, "score": 64, "regime": "CAUTIOUS", "trend": "stable", "kind": "settle", "stale": "false"},
        {"checked_at": t0 + timedelta(hours=17, minutes=10), "score": None, "regime": None, "trend": None,
         "kind": "market", "stale": "true"},
        {"checked_at": t0 + timedelta(hours=17, minutes=15), "score": 70, "regime": "HEALTHY",
         "trend": "improving", "kind": "market", "stale": "false"},
    ]
    body = client_with(ReadPool(history=history)).get("/market/history?days=7").json()
    assert body["days"] == 7
    assert body["rows"] == [
        {"checkedAt": t0.isoformat(), "score": 64, "regime": "CAUTIOUS", "trend": "stable",
         "kind": "settle", "stale": False},
        {"checkedAt": (t0 + timedelta(hours=17, minutes=10)).isoformat(), "score": None, "regime": None,
         "trend": None, "kind": "market", "stale": True},
        {"checkedAt": (t0 + timedelta(hours=17, minutes=15)).isoformat(), "score": 70, "regime": "HEALTHY",
         "trend": "improving", "kind": "market", "stale": False},
    ]
    assert all("indicators" not in r and "monitors" not in r for r in body["rows"])


def test_market_history_empty_window_is_empty_list(client_with):
    resp = client_with(ReadPool(latest=row(), history=[])).get("/market/history?days=1")
    assert resp.status_code == 200
    assert resp.json() == {"days": 1, "rows": []}


def test_market_endpoints_never_compute_or_download(client_with, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("an endpoint tried to compute or download")

    monkeypatch.setattr(scheduler, "compute_health", forbidden)
    monkeypatch.setattr(health_calculator, "compute_health", forbidden)
    monkeypatch.setattr(quotes, "get_core_quotes", forbidden)
    monkeypatch.setattr(quotes, "download_frame", forbidden)
    client = client_with(ReadPool(latest=row(), history=[row()]))
    history_row = {**row(), "kind": "market", "stale": "false"}
    client.app.state.db_pool.history = [history_row]
    for path in ("/market/health", "/market/indicators", "/market/history"):
        assert client.get(path).status_code == 200, path


def test_market_endpoints_read_only_repeat_call(client_with):
    pool = ReadPool(latest=row(), history=[{**row(), "kind": "market", "stale": "false"}])
    client = client_with(pool)
    for path in ("/market/health", "/market/indicators", "/market/history"):
        first, second = client.get(path).json(), client.get(path).json()
        if path == "/market/health":
            first.pop("ageSeconds"), second.pop("ageSeconds")
        assert first == second, path
    assert all(op in ("fetchrow", "fetch") for op, _, _ in pool.calls)


OVERLAY = {"status": "applied", "movePct": -3.2, "esPct": -3.2, "nqPct": -3.0,
           "base": 68, "cap": 39, "capped": True}
FUTURES = {"ES=F": {"price": 4840.0, "date": "2026-09-10", "asOf": INPUTS["asOf"], "stale": False},
           "NQ=F": None}


def test_market_health_serves_night_overlay(client_with):
    """Part 3.4b: a night row carries its cap; a row written before 3.4b has none."""
    night = row(score=39, regime="DANGER", trend="declining",
                ind=indicators(kind="night", overlay=OVERLAY, futures=FUTURES))
    body = client_with(ReadPool(latest=night)).get("/market/health").json()
    assert (body["kind"], body["score"], body["regime"]) == ("night", 39, "DANGER")
    assert body["overlay"] == OVERLAY
    assert client_with(ReadPool(latest=row())).get("/market/health").json()["overlay"] is None


def test_market_indicators_futures_and_overlay(client_with):
    night = row(score=39, regime="DANGER", ind=indicators(kind="night", overlay=OVERLAY, futures=FUTURES))
    body = client_with(ReadPool(latest=night)).get("/market/indicators").json()
    assert body["futures"] == FUTURES and body["overlay"] == OVERLAY
    assert body["monitors"] == MONITORS                     # the settle's monitors, copied by the night check
    old = client_with(ReadPool(latest=row())).get("/market/indicators").json()
    assert old["futures"] is None and old["overlay"] is None
