"""Part 3.4 — one health check (run_check) and the scheduler loop.
compute_health, the pool and the clock are fakes; FakeRedis records the
publish; db.py's real helpers run over the fake pool. The XNYS calendar is
real (offline). No socket."""

import asyncio
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import asyncpg
import pytest

import scheduler
from cache import MemoryCooldowns
from monitors import quotes
from scoring.regime_classifier import classify
from tests.fake_redis import FakeRedis

ET = ZoneInfo("America/New_York")


def et(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=ET)


MARKET_AT = et(2026, 9, 10, 10, 0)
SETTLE_AT = et(2026, 9, 10, 16, 20)
YESTERDAY_SETTLE = {"checked_at": et(2026, 9, 9, 16, 20), "score": 70, "kind": "settle"}


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class LoggingRedis(FakeRedis):
    def __init__(self, log, **kw):
        super().__init__(**kw)
        self.log = log

    async def publish(self, channel, message):
        result = await super().publish(channel, message)
        self.log.append("publish")
        return result


class FakePool:
    """Rows in memory. settle_base's fetchrow applies the filter the SQL
    encodes (settle, scored, before the cutoff, latest); the SQL text itself
    is asserted in test_db.py."""

    def __init__(self, log, rows=(), *, fail_insert=None, fail_read=None):
        self.log, self.rows = log, [dict(r) for r in rows]
        self.fail_insert, self.fail_read = fail_insert, fail_read
        self.inserts, self.reads = [], []

    async def fetchrow(self, sql, *args):
        self.reads.append((sql, args))
        if self.fail_read:
            raise self.fail_read
        (before,) = args
        matches = [r for r in self.rows
                   if r["kind"] == "settle" and r["score"] is not None and r["checked_at"] < before]
        if not matches:
            return None
        best = max(matches, key=lambda r: r["checked_at"])
        return {"checked_at": best["checked_at"], "score": best["score"]}

    async def execute(self, sql, *args):
        if self.fail_insert:
            raise self.fail_insert
        self.inserts.append(args)
        self.log.append("insert")
        checked_at, score, _regime, _trend, indicators = args
        self.rows.append({"checked_at": checked_at, "score": score, "kind": json.loads(indicators)["kind"]})


def snapshot(score, at, *, nan=False):
    return {
        "score": score,
        "regime": classify(score),
        "coverage": 100 if score is not None else 40,
        "stale": False,
        "staleMonitors": [],
        "checkedAt": at.astimezone(timezone.utc).isoformat(),
        "monitors": {"vix": {"score": score, "raw": {"level": math.nan if nan else 18.0},
                             "detail": "", "stale": False, "weight": 25}},
        "inputs": {"asOf": at.isoformat(), "source": "cached", "reason": None, "staleTickers": []},
    }


def patch_compute(monkeypatch, log, score, *, nan=False):
    calls = []

    async def fake(r, memory, *, now):
        calls.append({"r": r, "memory": memory, "now": now()})
        log.append("compute")
        return snapshot(score, now(), nan=nan)

    monkeypatch.setattr(scheduler, "compute_health", fake)
    return calls


def make_state(log, *, redis="fake", pool="fake", rows=(), **pool_kw):
    return SimpleNamespace(
        redis=LoggingRedis(log) if redis == "fake" else redis,
        db_pool=FakePool(log, rows, **pool_kw) if pool == "fake" else pool,
        cooldowns=MemoryCooldowns(),
        check_status={"lastCheckAt": None, "lastKind": None, "lastScore": None, "lastError": None},
    )


# ── run_check ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_check_order_compute_publish_insert(monkeypatch):
    log = []
    state = make_state(log, rows=[YESTERDAY_SETTLE])
    calls = patch_compute(monkeypatch, log, 72)

    result = await scheduler.run_check(state, "market", clock=Clock(MARKET_AT))

    assert log == ["compute", "publish", "insert"]
    assert calls[0]["r"] is state.redis and calls[0]["memory"] is state.cooldowns
    assert state.db_pool.reads[0][1] == (et(2026, 9, 10, 9, 30),)     # today's open
    checked_at, score, regime, trend, indicators = state.db_pool.inserts[0]
    assert checked_at == MARKET_AT
    assert (score, regime, trend) == (72, "HEALTHY", "stable")
    body = json.loads(indicators)
    assert body["kind"] == "market"
    assert (body["settleScore"], body["settleCheckedAt"]) == (70, et(2026, 9, 9, 16, 20).isoformat())
    assert state.check_status == {"lastCheckAt": MARKET_AT.astimezone(timezone.utc).isoformat(),
                                  "lastKind": "market", "lastScore": 72, "lastError": None}
    assert result["published"] == {"published": True, "reason": "initial"}


@pytest.mark.asyncio
async def test_run_check_without_db_still_publishes(monkeypatch, caplog):
    log = []
    state = make_state(log, pool=None)
    patch_compute(monkeypatch, log, 72)
    with caplog.at_level(logging.WARNING):
        result = await scheduler.run_check(state, "market", clock=Clock(MARKET_AT))
    assert log == ["compute", "publish"]
    assert result["settle"] is None and result["trend"] is None
    assert any("database unavailable" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [asyncpg.InterfaceError("connection lost"), TimeoutError("command timeout")])
async def test_run_check_insert_failure_is_logged_not_raised(monkeypatch, caplog, exc):
    log = []
    state = make_state(log, rows=[YESTERDAY_SETTLE], fail_insert=exc)
    patch_compute(monkeypatch, log, 72)
    with caplog.at_level(logging.WARNING):
        result = await scheduler.run_check(state, "market", clock=Clock(MARKET_AT))
    assert log == ["compute", "publish"]
    assert result["published"]["published"] is True
    assert state.db_pool.inserts == []
    assert [r.levelno for r in caplog.records if "insert failed" in r.getMessage()] == [logging.WARNING]
    assert state.check_status["lastError"] == f"insert: {type(exc).__name__}"


@pytest.mark.asyncio
async def test_run_check_trend_read_failure_trend_null(monkeypatch, caplog):
    log = []
    state = make_state(log, rows=[YESTERDAY_SETTLE], fail_read=asyncpg.InterfaceError("down"))
    patch_compute(monkeypatch, log, 72)
    with caplog.at_level(logging.WARNING):
        result = await scheduler.run_check(state, "market", clock=Clock(MARKET_AT))
    assert log == ["compute", "publish", "insert"]
    assert result["trend"] is None
    _, _, _, trend, indicators = state.db_pool.inserts[0]
    assert trend is None and json.loads(indicators)["settleScore"] is None
    assert any("settle base read failed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_trend_against_previous_session_settle(monkeypatch):
    for score, base, expected in [(75, 70, "improving"), (74, 70, "stable"), (66, 70, "stable"),
                                  (65, 70, "declining"), (70, None, None), (None, 70, None)]:
        assert scheduler.trend_from(score, base) == expected

    # The 16:20 settle check compares against yesterday's settle: its cutoff
    # is today's open, so neither its own row nor today's earlier rows count.
    log = []
    state = make_state(log, rows=[
        {"checked_at": et(2026, 9, 8, 16, 20), "score": 50, "kind": "settle"},
        {"checked_at": et(2026, 9, 9, 16, 20), "score": 60, "kind": "settle"},
        {"checked_at": et(2026, 9, 10, 10, 0), "score": 80, "kind": "market"},
    ])
    patch_compute(monkeypatch, log, 72)
    first = await scheduler.run_check(state, "settle", clock=Clock(SETTLE_AT))
    assert state.db_pool.reads[-1][1] == (et(2026, 9, 10, 9, 30),)
    assert (first["settle"]["score"], first["trend"]) == (60, "improving")
    # Today's settle row now exists; a second settle check still reads yesterday's.
    assert state.db_pool.rows[-1] == {"checked_at": SETTLE_AT, "score": 72, "kind": "settle"}
    again = await scheduler.run_check(state, "settle", clock=Clock(SETTLE_AT + timedelta(minutes=1)))
    assert again["settle"] == {"score": 60, "checkedAt": et(2026, 9, 9, 16, 20)}

    # Yesterday's settle had a null score → the latest scored settle before today's open.
    state = make_state([], rows=[
        {"checked_at": et(2026, 9, 8, 16, 20), "score": 58, "kind": "settle"},
        {"checked_at": et(2026, 9, 9, 16, 20), "score": None, "kind": "settle"},
    ])
    patch_compute(monkeypatch, [], 60)
    result = await scheduler.run_check(state, "market", clock=Clock(MARKET_AT))
    assert result["settle"] == {"score": 58, "checkedAt": et(2026, 9, 8, 16, 20)}
    assert result["trend"] == "stable"
    assert json.loads(state.db_pool.inserts[0][4])["settleCheckedAt"] == et(2026, 9, 8, 16, 20).isoformat()

    # No scored settle at all → trend null.
    state = make_state([], rows=[{"checked_at": et(2026, 9, 9, 10, 0), "score": 65, "kind": "market"}])
    result = await scheduler.run_check(state, "market", clock=Clock(MARKET_AT))
    assert result["settle"] is None and result["trend"] is None


@pytest.mark.asyncio
async def test_run_check_null_score_recorded_not_published(monkeypatch):
    log = []
    state = make_state(log, rows=[YESTERDAY_SETTLE])
    patch_compute(monkeypatch, log, None)
    await scheduler.run_check(state, "market", clock=Clock(MARKET_AT))
    assert log == ["compute", "insert"]
    assert state.redis.published == []
    _, score, regime, trend, _ = state.db_pool.inserts[0]
    assert (score, regime, trend) == (None, None, None)
    assert state.check_status["lastScore"] is None


@pytest.mark.asyncio
async def test_run_check_nan_indicators_no_row(monkeypatch, caplog):
    log = []
    state = make_state(log)
    patch_compute(monkeypatch, log, 72, nan=True)
    with caplog.at_level(logging.ERROR):
        result = await scheduler.run_check(state, "market", clock=Clock(MARKET_AT))
    assert log == ["compute", "publish"]              # the payload carries scores only
    assert result["published"]["published"] is True
    assert state.db_pool.inserts == []
    assert any(r.levelno == logging.ERROR and "insert raised ValueError" in r.getMessage()
               for r in caplog.records)


@pytest.mark.asyncio
async def test_run_check_skips_when_quotes_lock_held(monkeypatch, caplog):
    log = []
    state = make_state(log)
    calls = patch_compute(monkeypatch, log, 72)
    assert quotes.download_in_flight() is False
    async with quotes._download_lock():
        assert quotes.download_in_flight() is True
        with caplog.at_level(logging.WARNING):
            assert await scheduler.run_check(state, "market", clock=Clock(MARKET_AT)) is None
    assert quotes.download_in_flight() is False
    assert calls == [] and log == []
    assert any("skipped" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_run_check_carries_paused_seconds_once(monkeypatch):
    state = make_state([], rows=[YESTERDAY_SETTLE])
    state.pending_paused_seconds = 56844
    patch_compute(monkeypatch, [], 72)
    await scheduler.run_check(state, "market", clock=Clock(MARKET_AT))                            # initial
    assert state.pending_paused_seconds is None
    patch_compute(monkeypatch, [], 40)
    await scheduler.run_check(state, "market", clock=Clock(MARKET_AT + timedelta(minutes=15)))    # regime change
    first, second = [json.loads(raw) for _, raw in state.redis.published]
    assert (first["pausedSeconds"], second["pausedSeconds"]) == (56844, None)

    # A first check after a pause that doesn't publish still consumes it, and its row keeps it (addition 2).
    state.pending_paused_seconds = 600
    await scheduler.run_check(state, "market", clock=Clock(MARKET_AT + timedelta(minutes=20)))    # held
    assert len(state.redis.published) == 2 and state.pending_paused_seconds is None
    assert [json.loads(args[4])["pausedSeconds"] for args in state.db_pool.inserts] == [56844, None, 600]

    # A check skipped by the quotes lock leaves it for the next one, and writes no row.
    state.pending_paused_seconds = 600
    async with quotes._download_lock():
        assert await scheduler.run_check(state, "market", clock=Clock(MARKET_AT + timedelta(minutes=25))) is None
    assert state.pending_paused_seconds == 600 and len(state.db_pool.inserts) == 3


# ── run_scheduler ────────────────────────────────────────────────

def loop_sleep(clock, *, stop_at, freeze=None):
    """A fake chunk sleep that moves the fake clock. The chunk that would
    reach `stop_at` is recorded and cancels instead. freeze=(chunk start,
    wake): the chunk starting then ends at `wake` (the host paused)."""
    calls = []

    async def sleep(seconds):
        calls.append(seconds)
        if clock.t + timedelta(seconds=seconds) >= stop_at:
            raise asyncio.CancelledError
        if freeze is not None and clock.t == freeze[0]:
            clock.t = freeze[1]
        else:
            clock.t += timedelta(seconds=seconds)

    return sleep, calls


def recording_check(monkeypatch):
    """Records (kind, time, the pending pause the check would carry)."""
    runs = []

    async def fake(state, kind, *, clock):
        runs.append((kind, clock(), getattr(state, "pending_paused_seconds", None)))

    monkeypatch.setattr(scheduler, "run_check", fake)
    return runs


def patch_run_check(monkeypatch, *, raise_on=(), overrun=None):
    runs = []

    async def fake(state, kind, *, clock):
        runs.append((kind, clock()))
        if len(runs) in raise_on:
            raise RuntimeError("monitor bug")
        if overrun is not None and len(runs) == 1:
            clock.t += overrun

    monkeypatch.setattr(scheduler, "run_check", fake)
    return runs


@pytest.mark.asyncio
async def test_loop_survives_check_exception(monkeypatch, caplog):
    clock = Clock(et(2026, 9, 10, 9, 30))
    state = make_state([])
    runs = patch_run_check(monkeypatch, raise_on=(1,))
    sleep, sleeps = loop_sleep(clock, stop_at=et(2026, 9, 10, 9, 40))
    with caplog.at_level(logging.ERROR), pytest.raises(asyncio.CancelledError):
        await scheduler.run_scheduler(state, clock=clock, sleep=sleep)
    assert [t for _, t in runs] == [et(2026, 9, 10, 9, 30), et(2026, 9, 10, 9, 35)]
    assert sleeps[:5] == [60] * 5                        # 3.4 follow-up: 300 s in chunks
    assert any("raised RuntimeError" in r.getMessage() for r in caplog.records)
    assert state.check_status["lastError"] == "RuntimeError"


@pytest.mark.asyncio
async def test_loop_runs_slot_within_grace_only(monkeypatch, caplog):
    for offset, ran in ((60, True), (61, False)):
        clock = Clock(et(2026, 9, 10, 9, 30) + timedelta(seconds=offset))
        runs = patch_run_check(monkeypatch)
        sleep, sleeps = loop_sleep(clock, stop_at=et(2026, 9, 10, 9, 35))
        caplog.clear()
        with caplog.at_level(logging.WARNING), pytest.raises(asyncio.CancelledError):
            await scheduler.run_scheduler(make_state([]), clock=clock, sleep=sleep)
        assert bool(runs) is ran, offset
        missed = any("Missed 1 health check slot" in r.getMessage() for r in caplog.records)
        assert missed is (not ran), offset
        assert sum(sleeps) == 300 - offset and max(sleeps) <= 60, offset    # to 09:35, never back to 09:30


@pytest.mark.asyncio
async def test_loop_skips_missed_slots_never_catches_up(monkeypatch, caplog):
    clock = Clock(et(2026, 9, 10, 9, 30))
    runs = patch_run_check(monkeypatch, overrun=timedelta(minutes=11, seconds=30))   # ends 09:41:30
    sleep, sleeps = loop_sleep(clock, stop_at=et(2026, 9, 10, 9, 50))
    with caplog.at_level(logging.WARNING), pytest.raises(asyncio.CancelledError):
        await scheduler.run_scheduler(make_state([]), clock=clock, sleep=sleep)
    assert [t for _, t in runs] == [et(2026, 9, 10, 9, 30), et(2026, 9, 10, 9, 45)]
    assert any("Missed 2 health check slot" in r.getMessage() for r in caplog.records)   # 09:35, 09:40
    assert sleeps[:4] == [60, 60, 60, 30]                                                # 210 s in chunks


@pytest.mark.asyncio
async def test_loop_runs_each_slot_once(monkeypatch):
    # The harness hands back the same instant twice (a wall clock stepped back after a wake).
    clock = Clock(et(2026, 9, 10, 9, 30, 10))
    runs = patch_run_check(monkeypatch)
    waits = []

    async def same_instant(target, *, clock, sleep, log):
        waits.append(target)
        if len(waits) == 3:
            raise asyncio.CancelledError
        return scheduler.wallclock.Wake(clock(), None)

    monkeypatch.setattr(scheduler.wallclock, "sleep_until", same_instant)
    with pytest.raises(asyncio.CancelledError):
        await scheduler.run_scheduler(make_state([]), clock=clock)
    assert len(runs) == 1 and len(waits) == 3


@pytest.mark.asyncio
async def test_loop_mac_sleep_16h_warns_on_wake_and_resumes(monkeypatch, caplog):
    # 2026-09-10: booted 17:07:15 ET after the settle, nothing handled yet;
    # the chunk starting 17:45:15 ET ends when the Mac wakes, Fri 09:32:39 ET.
    clock = Clock(et(2026, 9, 10, 17, 7, 15))
    runs = recording_check(monkeypatch)
    sleep, sleeps = loop_sleep(clock, stop_at=et(2026, 9, 11, 9, 40),
                               freeze=(et(2026, 9, 10, 17, 45, 15), et(2026, 9, 11, 9, 32, 39)))
    with caplog.at_level(logging.WARNING), pytest.raises(asyncio.CancelledError):
        await scheduler.run_scheduler(make_state([]), clock=clock, sleep=sleep)
    assert max(sleeps) == 60
    assert runs == [("market", et(2026, 9, 11, 9, 35), 56844)]          # 09:30 was woken for too late
    assert [r.getMessage() for r in caplog.records] == [
        "host paused ~15h 47m (wall +56844 s, process +0 s)",
        "Missed 1 health check slot(s) up to 2026-09-11T13:30:00+00:00 (woke 159s after that slot)",
    ]


@pytest.mark.asyncio
async def test_loop_wake_between_slots_warns_missed(monkeypatch, caplog):
    # Last check Fri 09:35; the chunk starting 09:36 ET takes 16 h, waking Sat 01:36 ET.
    # Part 3.4b: the slot it woke past is Friday's 16:45 night slot, not the settle,
    # and the next one is Sunday 18:15 — so the window stops before the weekend runs.
    clock = Clock(et(2026, 9, 11, 9, 35))
    runs = recording_check(monkeypatch)
    sleep, sleeps = loop_sleep(clock, stop_at=et(2026, 9, 12, 1, 40),
                               freeze=(et(2026, 9, 11, 9, 36), et(2026, 9, 12, 1, 36)))
    with caplog.at_level(logging.WARNING), pytest.raises(asyncio.CancelledError):
        await scheduler.run_scheduler(make_state([]), clock=clock, sleep=sleep)
    assert runs == [("market", et(2026, 9, 11, 9, 35), None)]
    assert max(sleeps) == 60
    assert [r.getMessage() for r in caplog.records] == [       # 09:40 … 16:00 = 77, settle, 16:45 night
        "host paused ~16h 0m (wall +57600 s, process +0 s)",
        "Missed 79 health check slot(s) up to 2026-09-11T20:45:00+00:00 (woke 31860s after that slot)",
    ]

    # A fresh loop booted at that moment has handled nothing: it reports nothing.
    clock = Clock(et(2026, 9, 12, 1, 36))
    sleep, _ = loop_sleep(clock, stop_at=et(2026, 9, 12, 1, 40))
    caplog.clear()
    with caplog.at_level(logging.WARNING), pytest.raises(asyncio.CancelledError):
        await scheduler.run_scheduler(make_state([]), clock=clock, sleep=sleep)
    assert caplog.records == []


@pytest.mark.asyncio
async def test_loop_full_session_every_slot_once(monkeypatch, caplog):
    clock = Clock(et(2026, 9, 10, 9, 29))
    runs = patch_run_check(monkeypatch)
    sleep, sleeps = loop_sleep(clock, stop_at=et(2026, 9, 10, 16, 26))
    with caplog.at_level(logging.WARNING), pytest.raises(asyncio.CancelledError):
        await scheduler.run_scheduler(make_state([]), clock=clock, sleep=sleep)
    market = [("market", et(2026, 9, 10, 9, 30) + timedelta(minutes=5 * i)) for i in range(79)]
    assert runs == market + [("settle", et(2026, 9, 10, 16, 20))]
    assert max(sleeps) == 60 and caplog.records == []


@pytest.mark.asyncio
async def test_loop_error_fallback_is_chunked(monkeypatch, caplog):
    clock = Clock(et(2026, 9, 10, 16, 10))                # between the close and the settle
    runs = patch_run_check(monkeypatch)
    real_slot_for, calls = scheduler.slot_for, []

    def flaky(now):
        calls.append(now)
        if len(calls) == 1:
            raise RuntimeError("gating bug")
        return real_slot_for(now)

    monkeypatch.setattr(scheduler, "slot_for", flaky)
    sleep, sleeps = loop_sleep(clock, stop_at=et(2026, 9, 10, 16, 25))
    with caplog.at_level(logging.WARNING), pytest.raises(asyncio.CancelledError):
        await scheduler.run_scheduler(make_state([]), clock=clock, sleep=sleep)
    assert [r.getMessage() for r in caplog.records] == ["Scheduler loop error RuntimeError: gating bug"]
    assert sleeps[:10] == [60] * 10                       # the 300 s fallback, then 16:15 → 16:20
    assert runs == [("settle", et(2026, 9, 10, 16, 20))]


@pytest.mark.asyncio
async def test_loop_cancel_is_clean(monkeypatch):
    # During the sleep: Saturday noon, the real asyncio.sleep until Monday.
    task = asyncio.create_task(scheduler.run_scheduler(make_state([]), clock=Clock(et(2026, 9, 12, 12, 0))))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()

    # During a check.
    started = asyncio.Event()

    async def hanging(state, kind, *, clock):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(scheduler, "run_check", hanging)
    task = asyncio.create_task(scheduler.run_scheduler(make_state([]), clock=Clock(et(2026, 9, 10, 9, 30))))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
