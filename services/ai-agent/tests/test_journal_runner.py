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
# The AAPL targets (day 0 = 2026-09-21), each night the one on which that
# horizon is due; +1 and +5 are due together on 09-28.
TARGET_NIGHTS = {1: datetime(2026, 9, 28, 17, 30, tzinfo=ET), 5: datetime(2026, 9, 28, 17, 30, tzinfo=ET),
                 20: datetime(2026, 10, 19, 17, 30, tzinfo=ET), 30: datetime(2026, 11, 2, 17, 30, tzinfo=ET),
                 60: datetime(2026, 12, 15, 17, 30, tzinfo=ET)}
TARGET_DATES = {1: date(2026, 9, 22), 5: date(2026, 9, 28), 20: date(2026, 10, 19),
                30: date(2026, 11, 2), 60: date(2026, 12, 15)}

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
            if len(scored) < args[2] and v["asked_at"] >= args[1]:
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


def daily_bars(first=date(2026, 9, 17), last=date(2026, 12, 16), price=340.0, skip=()):
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
async def test_scores_every_horizon_after_refresh():
    """One verdict, the five target nights in order: 2 + 1 + 1 + 1 rows."""
    pool, engine = JournalPool([verdict()]), DataEngine()
    state = state_for(pool, engine)
    first = await run(state, NIGHT_0928)
    assert first["scored"] == 2 and first["refreshed"] == 1
    rows = pool.rows_for("AAPL")
    one = rows[(verdict()["id"], 1)]
    assert one["ask_session_bars"] == 1                 # the 15:30 ET bar only
    from decimal import Decimal
    assert one["return_pct"] == Decimal("0.310")        # (340 − 338.95) / 338.95
    assert one["stop_hit"] is False and one["r_multiple"] == Decimal("0.117")   # 1.05 / 8.95
    for h in (20, 30, 60):
        assert (await run(state, TARGET_NIGHTS[h]))["scored"] == 1, h
    rows = pool.rows_for("AAPL")
    assert {h: rows[(verdict()["id"], h)]["session_date"] for h in TARGET_DATES} == TARGET_DATES


@pytest.mark.asyncio
@pytest.mark.parametrize("horizon", sessions.HORIZONS)
async def test_each_horizon_is_scored_on_its_target_night(horizon):
    """Only the horizons due that night; each row names its session N."""
    pool, engine = JournalPool([verdict()]), DataEngine()
    got = await run(state_for(pool, engine), TARGET_NIGHTS[horizon])
    rows = pool.rows_for("AAPL")
    assert (verdict()["id"], horizon) in rows
    assert rows[(verdict()["id"], horizon)]["session_date"] == TARGET_DATES[horizon]
    due = {h for h in TARGET_DATES if TARGET_DATES[h] <= TARGET_NIGHTS[horizon].date()
           and len(sessions.sessions_after(TARGET_DATES[h], TARGET_NIGHTS[horizon].date())) <= 10}
    assert {h for _, h in rows} == due and got["scored"] == len(due)


@pytest.mark.asyncio
async def test_horizon_60_is_selected_within_window():
    """+60 for a 09-21 ask is due on 12-15, 85 calendar days later — past the
    old 60-day pre-filter, inside the 110-day one. A verdict asked 111 days
    before the night is outside it."""
    night = TARGET_NIGHTS[60]
    old = verdict("OLD", 2, asked=night - timedelta(days=111))
    pool, engine = JournalPool([verdict(), old]), DataEngine()
    got = await run(state_for(pool, engine), night)
    (_, _, args), = pool.statements("FROM ai.verdicts v")
    assert args[1] == night.astimezone(timezone.utc) - timedelta(days=110) and args[2] == 5
    assert (night.astimezone(timezone.utc) - verdict()["asked_at"]).days == 85
    assert set(pool.rows_for("AAPL")) == {(verdict()["id"], 60)} and got["scored"] == 1
    assert "OLD" not in engine.refreshes()


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
    # +60 targets 12-15; 12-30 is 10 sessions after it (still due), 12-31 is
    # 11 — and every earlier horizon is older still. 101 days: in the window.
    got = await run(state_for(pool, engine), datetime(2026, 12, 31, 17, 30, tzinfo=ET))
    assert got["expired"] == 5 and got["due"] == 0 and engine.calls == []


# ── Refresh answers ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cooldown_429_requeues_once_never_fresh():
    pool = JournalPool([verdict("AAA", 1), verdict("BBB", 2), verdict("CCC", 3)])
    engine = DataEngine(refresh={"AAA": ["cooldown", "cooldown"], "CCC": ["cooldown", "ok"]})
    got = await run(state_for(pool, engine))
    assert engine.refreshes() == ["AAA", "BBB", "CCC", "AAA", "CCC"]
    assert pool.rows_for("AAA") == {}, "a cooldown is never read as fresh bars"
    assert len(pool.rows_for("BBB")) == 2 and len(pool.rows_for("CCC")) == 2
    assert got["requeued"] == 2 and got["skipped"] == 1 and got["stoppedBy"] is None
    assert not [c for c in engine.calls if c[0] == "bars" and c[1] == "AAA"]


@pytest.mark.asyncio
async def test_first_blank_answer_skips_ticker():
    redis = FakeRedis()
    pool = JournalPool([verdict("DEAD", 1), verdict("LIVE", 2)])
    engine = DataEngine(refresh={"DEAD": ["blank"]})
    got = await run(state_for(pool, engine, redis))
    assert engine.refreshes() == ["DEAD", "LIVE"] and got["stoppedBy"] is None
    assert len(pool.rows_for("LIVE")) == 2 and pool.rows_for("DEAD") == {}
    assert redis.sets[runner.BLANKED_KEY] == {"DEAD"} and redis.ttl[runner.BLANKED_KEY] == 604800


@pytest.mark.asyncio
async def test_second_blank_answer_stops_the_night():
    redis = FakeRedis()
    pool = JournalPool([verdict("AAA", 1), verdict("BBB", 2), verdict("CCC", 3)])
    engine = DataEngine(refresh={"AAA": ["blank"], "BBB": ["blank"]})
    got = await run(state_for(pool, engine, redis))
    assert engine.refreshes() == ["AAA", "BBB"], "CCC is never refreshed"
    assert got["stoppedBy"].startswith("BBB") and got["deferred"] == 1
    assert redis.sets[runner.BLANKED_KEY] == {"AAA", "BBB"}


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [
    httpx.Response(429, json={"detail": "Rate limited by data provider."}),
    httpx.Response(502, json={"detail": "Data provider error"}),
    httpx.Response(503, json={"detail": "Database unavailable"}),
    httpx.ReadTimeout("slow"),
    httpx.ConnectError("refused"),
], ids=["429-no-retry-after", "502", "503", "timeout", "connect"])
async def test_refresh_failure_stops_the_night(answer):
    redis = FakeRedis()
    pool = JournalPool([verdict("AAA", 1), verdict("BBB", 2), verdict("CCC", 3)])
    engine = DataEngine(refresh={"BBB": [answer]})
    got = await run(state_for(pool, engine, redis))
    assert engine.refreshes() == ["AAA", "BBB"]
    assert len(pool.rows_for("AAA")) == 2 and pool.rows_for("BBB") == {} == pool.rows_for("CCC")
    assert got["stoppedBy"].startswith("BBB:") and got["deferred"] == 1
    assert redis.sets[runner.BLANKED_KEY] == {"BBB"}, "the stopper runs last next night"


@pytest.mark.asyncio
async def test_av_cooldown_in_refresh_never_stops_the_night():
    """data-engine's AV refusal lives in earningsDates.reason; the bars are there."""
    av = httpx.Response(200, json={"ticker": "AAA", "dailyBars": 502, "hourlyBars": 455,
                                   "earningsDates": {"source": None, "stored": 0, "dropped": 0,
                                                     "reason": "cooldown"}})
    pool = JournalPool([verdict("AAA", 1), verdict("BBB", 2)])
    engine = DataEngine(refresh={"AAA": [av]})
    got = await run(state_for(pool, engine))
    assert got["stoppedBy"] is None and got["refreshed"] == 2
    assert len(pool.rows_for("AAA")) == 2


# ── Order ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_blanked_tickers_go_last_next_night():
    redis = FakeRedis()
    redis.sets[runner.BLANKED_KEY] = {"AAA"}
    pool = JournalPool([verdict("AAA", 1), verdict("BBB", 2), verdict("CCC", 3)])
    engine = DataEngine()
    await run(state_for(pool, engine, redis))
    assert engine.refreshes() == ["BBB", "CCC", "AAA"]


@pytest.mark.asyncio
async def test_blanked_redis_down_plain_order():
    redis = FakeRedis(broken=True)
    pool = JournalPool([verdict("AAA", 1), verdict("BBB", 2)])
    engine = DataEngine(refresh={"AAA": ["blank"]})
    got = await run(state_for(pool, engine, redis))
    assert engine.refreshes() == ["AAA", "BBB"] and len(pool.rows_for("BBB")) == 2
    assert got["stoppedBy"] is None


@pytest.mark.asyncio
async def test_three_dead_tickers_do_not_starve_the_rest():
    """With one remembered ticker, three dead ones would rotate (A+B stop
    night 1, A+C night 2 …) ahead of everything. With the set, every dead
    ticker runs behind the healthy ones."""
    redis = FakeRedis()
    dead = {"DA": ["blank"] * 5, "DB": ["blank"] * 5, "DC": ["blank"] * 5}
    pool = JournalPool([verdict("DA", 1), verdict("DB", 2), verdict("DC", 3),
                        verdict("HA", 4), verdict("HB", 5)])
    engine = DataEngine(refresh=dead)
    state = state_for(pool, engine, redis)

    night1 = await run(state)
    assert engine.refreshes() == ["DA", "DB"] and night1["stoppedBy"].startswith("DB")
    engine.calls.clear()
    night2 = await run(state)
    assert engine.refreshes() == ["DC", "HA", "HB", "DA"], "healthy before the known dead"
    assert len(pool.rows_for("HA")) == 2 and len(pool.rows_for("HB")) == 2
    assert redis.sets[runner.BLANKED_KEY] == {"DA", "DB", "DC"}
    # A new healthy verdict now runs ahead of all three dead tickers.
    pool.verdicts.append(verdict("HC", 6))
    engine.calls.clear()
    await run(state)
    assert engine.refreshes()[0] == "HC" and len(pool.rows_for("HC")) == 2


# ── Pacing ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_one_refresh_per_ticker():
    pool = JournalPool([verdict("AAA", 1), verdict("AAA", 2, plan=None)])
    engine = DataEngine()
    got = await run(state_for(pool, engine))
    assert engine.refreshes() == ["AAA"] and got["scored"] == 4


@pytest.mark.asyncio
async def test_attempt_cap_includes_retries():
    names = [f"T{chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(20)]
    pool = JournalPool([verdict(t, i + 1) for i, t in enumerate(names)])
    engine = DataEngine(refresh={names[0]: ["cooldown", "ok"]})
    got = await run(state_for(pool, engine))
    assert len(engine.refreshes()) == runner.MAX_ATTEMPTS == 20
    assert got["deferred"] == 1 and engine.refreshes().count(names[0]) == 1


@pytest.mark.asyncio
async def test_refreshes_are_spaced():
    sleeps = []
    pool = JournalPool([verdict("AAA", 1), verdict("BBB", 2), verdict("CCC", 3)])
    await run(state_for(pool, DataEngine()), sleeps=sleeps)
    assert sleeps == [5, 5], "5 s between refreshes, none before the first"


@pytest.mark.asyncio
async def test_run_stops_at_deadline():
    pool = JournalPool([verdict("AAA", 1), verdict("BBB", 2), verdict("CCC", 3)])
    engine = DataEngine()
    start = datetime(2026, 9, 28, 18, 8, tzinfo=ET)
    got = await run(state_for(pool, engine), clock=Clock(start, step=timedelta(minutes=1)),
                    deadline=sessions.deadline_at(date(2026, 9, 28)))
    # Readings: 18:08 (select), 18:09 (AAA starts), 18:10 → deadline.
    assert engine.refreshes() == ["AAA"]
    assert got["stoppedBy"] == "deadline" and got["deferred"] == 2


# ── Bars ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bars_read_failure_skips_ticker():
    pool = JournalPool([verdict("AAA", 1), verdict("BBB", 2)])
    engine = DataEngine(bars={("AAA", "1d"): httpx.Response(503)})
    got = await run(state_for(pool, engine))
    assert pool.rows_for("AAA") == {} and len(pool.rows_for("BBB")) == 2
    assert got["skipped"] == 1 and got["stoppedBy"] is None


@pytest.mark.asyncio
async def test_missing_session_bar_leaves_horizon_unscored(caplog):
    pool = JournalPool([verdict()])
    engine = DataEngine(bars={("AAPL", "1d"): daily_bars(skip={date(2026, 9, 25)})})
    await run(state_for(pool, engine))
    assert set(h for _, h in pool.rows_for("AAPL")) == {1}, "+5 needs 09-25"
    assert "2026-09-25" in caplog.text


@pytest.mark.asyncio
async def test_missing_middle_hour_still_scores(caplog):
    asked = datetime(2026, 9, 21, 10, 5, tzinfo=ET)          # starts 10:30 … 15:30: six
    gap = datetime(2026, 9, 21, 12, 30, tzinfo=ET)
    pool = JournalPool([verdict(asked=asked)])
    engine = DataEngine(bars={("AAPL", "1h"): hourly_bars(skip={gap})})
    await run(state_for(pool, engine))
    rows = pool.rows_for("AAPL")
    assert len(rows) == 2 and all(r["ask_session_bars"] == 5 for r in rows.values())
    assert gap.astimezone(timezone.utc).isoformat() in caplog.text


@pytest.mark.asyncio
async def test_no_hourly_bars_when_expected_defers():
    pool = JournalPool([verdict()])
    engine = DataEngine(bars={("AAPL", "1h"): []})
    got = await run(state_for(pool, engine))
    assert pool.rows_for("AAPL") == {} and got["skipped"] == 1
    # Asked after the last hourly start: none expected, so none needed.
    late = JournalPool([verdict(asked=datetime(2026, 9, 21, 15, 45, tzinfo=ET))])
    await run(state_for(late, DataEngine(bars={("AAPL", "1h"): []})))
    assert [r["ask_session_bars"] for r in late.outcomes.values()] == [0, 0]


@pytest.mark.asyncio
async def test_window_never_includes_bars_before_the_ask():
    """A 10:00 ET low far under the stop happened before the 15:12 ask."""
    def low(start):
        return 300.0 if start < ASKED else 338.0

    pool = JournalPool([verdict()])
    engine = DataEngine(bars={("AAPL", "1h"): hourly_bars(low=low)})
    await run(state_for(pool, engine))
    rows = pool.rows_for("AAPL")
    assert all(r["stop_hit"] is False and r["ask_session_bars"] == 1 for r in rows.values())
    from decimal import Decimal
    # The window's lowest low is 338 (the 15:30 bar and the daily bars):
    # (338 − 338.95) / 338.95. A leaked 300 low would make it −11.49.
    assert rows[(verdict()["id"], 1)]["mae_pct"] == Decimal("-0.280")
    since = [c for c in engine.calls if c[:3] == ("bars", "AAPL", "1h")]
    assert since, "the hourly read happened"


@pytest.mark.asyncio
async def test_price_scale_break_skips_the_verdict_in_the_run(caplog):
    quarter = [{**b, **{k: b[k] / 4 for k in ("open", "high", "low", "close")}} for b in daily_bars()]
    pool = JournalPool([verdict(asked=datetime(2026, 9, 21, 17, 0, tzinfo=ET))])
    engine = DataEngine(bars={("AAPL", "1d"): quarter})
    got = await run(state_for(pool, engine))
    assert pool.outcomes == {} and got["unscored"] == 1
    assert "price-scale break" in caplog.text and "AAPL" in caplog.text


@pytest.mark.asyncio
async def test_unreadable_plan_leaves_verdict_unscored(caplog):
    pool = JournalPool([verdict(plan={"stop": "x"})])
    engine = DataEngine()
    got = await run(state_for(pool, engine))
    assert got["unscored"] == 1 and engine.calls == [] and pool.outcomes == {}
    assert "does not parse" in caplog.text


# ── Storage and the lock ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_store_failure_rolls_back_ticker():
    pool = JournalPool([verdict("AAA", 1), verdict("BBB", 2)], fail_for={verdict("AAA", 1)["id"]})
    got = await run(state_for(pool, DataEngine()))
    assert pool.rows_for("AAA") == {} and len(pool.rows_for("BBB")) == 2
    assert got["skipped"] == 1 and got["scored"] == 2


@pytest.mark.asyncio
async def test_second_scorer_skips_when_locked():
    redis = FakeRedis()
    redis.kv[runner.LOCK_KEY] = "someone"
    engine = DataEngine()
    got = await run(state_for(JournalPool([verdict()]), engine, redis))
    assert got["stoppedBy"] == "locked" and engine.calls == []
    assert redis.kv[runner.LOCK_KEY] == "someone", "never releases a lock it did not take"


@pytest.mark.asyncio
async def test_lock_is_taken_with_its_ttl_and_released():
    redis = FakeRedis()
    await run(state_for(JournalPool([verdict()]), DataEngine(), redis))
    assert redis.ttl[runner.LOCK_KEY] == 2700 and runner.LOCK_KEY not in redis.kv


@pytest.mark.asyncio
async def test_redis_down_scores_without_lock():
    pool = JournalPool([verdict()])
    got = await run(state_for(pool, DataEngine(), FakeRedis(broken=True)))
    assert got["scored"] == 2


# ── No LLM path ──────────────────────────────────────────────────

LLM_MODULES = {"providers", "classifier", "analyst", "analyze", "prompts", "openai", "ledger"}


def test_journal_never_imports_an_llm_path():
    package = Path(__file__).resolve().parent.parent / "journal"
    for src in package.glob("*.py"):
        for node in ast.walk(ast.parse(src.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            assert not set(names) & LLM_MODULES, (src.name, names)
    # And nothing it imports drags one in.
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; import journal.runner, journal.scoring, journal.sessions; "
         f"print(sorted(m for m in {sorted(LLM_MODULES)!r} if m in sys.modules))"],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip() == "[]"
