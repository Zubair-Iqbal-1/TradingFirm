"""Part 4.5 — the journal scorer (journal/runner.py).

A stateful fake pool (outcome rows it stores come back as `scored`), a tiny
fake Redis and data-engine behind httpx.MockTransport. The real XNYS calendar.
Nothing opens a socket; no clock is real (a fake clock and a recording
sleep are injected)."""

import ast
import json
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

import db
from journal import runner, sessions
from tests.fake_pool import FakePool

ET = ZoneInfo("America/New_York")
DE = "http://data-engine-dev:8001"
ASKED = datetime(2026, 9, 21, 15, 12, tzinfo=ET)          # the AAPL asks: in session
NIGHT_0928 = datetime(2026, 9, 28, 17, 30, tzinfo=ET)     # +1 and +5 due
NIGHT_1019 = datetime(2026, 10, 19, 17, 30, tzinfo=ET)    # +20 due, +1 expired by now

PLAN = {"entry": 338.95, "stop": 330.0, "stopBasis": "support minus 1 ATR",
        "disasterLine": 325.0, "invalidation": "daily close below the 20 EMA",
        "targets": [{"price": 350.0, "r": 1.23}], "sizeShares": 3,
        "sizeBasis": "risk over stop distance", "horizonDays": 10}


# ── Fakes ────────────────────────────────────────────────────────

class JournalPool(FakePool):
    """Serves ai.verdicts rows and keeps every outcome row it is given, so a
    second pass sees what the first stored."""

    def __init__(self, verdicts, fail_for=None):
        super().__init__()
        self.verdicts, self.outcomes, self.fail_for = verdicts, {}, fail_for or set()
        self.answers = {"FROM ai.verdicts v": self._due,
                        "INSERT INTO ai.verdict_outcomes": self._insert}

    def _due(self, args):
        out = []
        for v in self.verdicts:
            scored = [h for (vid, h) in self.outcomes if vid == v["id"]]
            if len(scored) < 3 and v["asked_at"] >= args[1]:
                out.append({**v, "plan_proposed": json.dumps(v["plan_proposed"])
                            if v["plan_proposed"] is not None else None, "scored": scored})
        return out

    def _insert(self, args):
        row = dict(zip(db.OUTCOME_COLUMNS, args))
        if row["verdict_id"] in self.fail_for:
            import asyncpg
            raise asyncpg.PostgresError("fake insert failure")
        key = (row["verdict_id"], row["horizon_days"])
        if key in self.outcomes:
            return "INSERT 0 0"
        self.outcomes[key] = row
        return "INSERT 0 1"

    def rows_for(self, ticker):
        ids = {v["id"] for v in self.verdicts if v["ticker"] == ticker}
        return {k: r for k, r in self.outcomes.items() if k[0] in ids}


class FakeRedis:
    def __init__(self, broken=False):
        self.kv, self.sets, self.ttl, self.broken = {}, {}, {}, broken

    def _check(self):
        if self.broken:
            raise ConnectionError("redis is down")

    async def set(self, key, value, nx=False, ex=None):
        self._check()
        if nx and key in self.kv:
            return None
        self.kv[key], self.ttl[key] = value, ex
        return True

    async def delete(self, key):
        self._check()
        self.kv.pop(key, None)

    async def smembers(self, key):
        self._check()
        return set(self.sets.get(key, set()))

    async def sadd(self, key, member):
        self._check()
        self.sets.setdefault(key, set()).add(member)

    async def expire(self, key, seconds):
        self._check()
        self.ttl[key] = seconds


def daily_bars(first=date(2026, 9, 17), last=date(2026, 10, 20), price=340.0, skip=()):
    out, d = [], first
    while d <= last:
        if sessions.is_session(d) and d not in skip:
            out.append({"ts": f"{d.isoformat()}T00:00:00+00:00", "open": price,
                        "high": price + 2, "low": price - 2, "close": price, "volume": 1})
        d += timedelta(days=1)
    return out


def hourly_bars(day=date(2026, 9, 21), skip=(), low=None, price=339.0):
    out, start = [], sessions.session_open(day)
    close = sessions.session_close(day)
    while start < close:
        if start not in skip:
            out.append({"ts": start.isoformat(), "open": price, "high": price + 1,
                        "low": low(start) if low else price - 1, "close": price, "volume": 1})
        start += timedelta(hours=1)
    return out


class DataEngine:
    """Scripted data-engine. `refresh[t]` is a list of answers popped in
    order (default: ok); `bars[(t, interval)]` is a list or a Response."""

    def __init__(self, refresh=None, bars=None):
        self.refresh, self.bars = refresh or {}, bars or {}
        self.calls = []

    def __call__(self, request):
        parts = request.url.path.strip("/").split("/")
        ticker = parts[1]
        if request.method == "POST":
            self.calls.append(("refresh", ticker))
            script = self.refresh.get(ticker)
            answer = script.pop(0) if script else "ok"
            if isinstance(answer, Exception):
                raise answer
            if isinstance(answer, httpx.Response):
                return answer
            return {
                "ok": httpx.Response(200, json={"ticker": ticker, "dailyBars": 502, "hourlyBars": 455,
                                                "earningsDates": {"reason": None}}),
                "blank": httpx.Response(200, json={"ticker": ticker, "dailyBars": 0, "hourlyBars": 0,
                                                   "earningsDates": {"reason": "error"}}),
                "cooldown": httpx.Response(429, headers={"Retry-After": "300"}, json={"detail": "recently"}),
            }[answer]
        interval = request.url.params["interval"]
        self.calls.append(("bars", ticker, interval))
        got = self.bars.get((ticker, interval))
        if got is None:
            got = daily_bars() if interval == "1d" else hourly_bars()
        if isinstance(got, httpx.Response):
            return got
        return httpx.Response(200, json={"ticker": ticker, "interval": interval, "bars": got})

    def refreshes(self):
        return [c[1] for c in self.calls if c[0] == "refresh"]


def verdict(ticker="AAPL", n=1, asked=ASKED, plan=PLAN, entry="338.95"):
    from decimal import Decimal
    return {"id": f"{n:08d}-0000-4000-8000-000000000000", "ticker": ticker,
            "asked_at": asked.astimezone(timezone.utc), "entry": Decimal(entry), "plan_proposed": plan}


class Clock:
    def __init__(self, start, step=timedelta(0)):
        self.now, self.step = start.astimezone(timezone.utc), step

    def __call__(self):
        value = self.now
        self.now += self.step
        return value


def state_for(pool, engine, redis=None):
    return SimpleNamespace(db_pool=pool, redis=redis, http=httpx.AsyncClient(
        transport=httpx.MockTransport(engine)))


SETTINGS = SimpleNamespace(data_engine_url=DE)


async def run(state, when=NIGHT_0928, sleeps=None, **kw):
    async def fake_sleep(seconds):
        if sleeps is not None:
            sleeps.append(seconds)

    kw.setdefault("clock", Clock(when))
    return await runner.score_once(state, SETTINGS, sleep=fake_sleep, **kw)


# ── Slot-level branches ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_score_once_without_pool_skips(caplog):
    engine = DataEngine()
    got = await run(state_for(None, engine))
    assert got["stoppedBy"] == "no database" and engine.calls == []
    assert "no database pool" in caplog.text


@pytest.mark.asyncio
async def test_score_once_db_error_skips_night():
    engine = DataEngine()
    pool = FakePool(raise_on="FROM ai.verdicts v")
    got = await run(state_for(pool, engine))
    assert got["stoppedBy"] == "database error" and engine.calls == []


@pytest.mark.asyncio
async def test_nothing_due_makes_no_call():
    engine = DataEngine()
    pool = JournalPool([verdict(asked=datetime(2026, 9, 28, 10, 0, tzinfo=ET))])
    got = await run(state_for(pool, engine))       # asked today: +1 is tomorrow
    assert got["due"] == 0 and engine.calls == []
    assert pool.statements("INSERT") == []
    empty = JournalPool([])
    assert (await run(state_for(empty, engine)))["tickers"] == 0 and engine.calls == []


@pytest.mark.asyncio
async def test_scores_three_horizons_after_refresh():
    pool, engine = JournalPool([verdict()]), DataEngine()
    state = state_for(pool, engine)
    first = await run(state, NIGHT_0928)
    assert first["scored"] == 2 and first["refreshed"] == 1
    rows = pool.rows_for("AAPL")
    one, five = rows[(verdict()["id"], 1)], rows[(verdict()["id"], 5)]
    assert one["session_date"] == date(2026, 9, 22) and five["session_date"] == date(2026, 9, 28)
    assert one["ask_session_bars"] == 1                 # the 15:30 ET bar only
    from decimal import Decimal
    assert one["return_pct"] == Decimal("0.310")        # (340 − 338.95) / 338.95
    assert one["stop_hit"] is False and one["r_multiple"] == Decimal("0.117")   # 1.05 / 8.95
    third = await run(state, NIGHT_1019)
    assert third["scored"] == 1
    assert pool.rows_for("AAPL")[(verdict()["id"], 20)]["session_date"] == date(2026, 10, 19)


@pytest.mark.asyncio
async def test_rerun_is_a_noop():
    pool, engine = JournalPool([verdict()]), DataEngine()
    state = state_for(pool, engine)
    await run(state)
    engine.calls.clear()
    again = await run(state)
    assert again["scored"] == 0 and engine.calls == [], "0 refreshes, 0 inserts"
    assert len(pool.outcomes) == 2


@pytest.mark.asyncio
async def test_expired_horizon_is_never_refreshed():
    engine = DataEngine()
    pool = JournalPool([verdict()])
    # +20 targets 10-19; 11-02 is 10 sessions after it (still due), 11-03 is 11.
    got = await run(state_for(pool, engine), datetime(2026, 11, 3, 17, 30, tzinfo=ET))
    assert got["expired"] == 3 and got["due"] == 0 and engine.calls == []
