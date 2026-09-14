"""Part 3.4c commit 3a — the weekend gate on the live check path (spec D4,
D5, D10, F6, F8, F9). The XNYS calendar is real (offline, frozen time);
compute_health, Redis and the pool are the fakes test_scheduler.py uses."""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import scheduler
import weekend_inputs
from cache import MemoryCooldowns
from scoring import weekend
from scoring.health_calculator import compute_health
from scoring.regime_classifier import classify
from tests.fake_redis import FakeRedis

ET = ZoneInfo("America/New_York")


def et(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=ET)


FRIDAY = datetime(2026, 9, 18).date()


# ── The window (D4, D5) ──────────────────────────────────────────

def test_a_normal_friday_is_a_weekend_eve_session():
    window = scheduler.weekend_window(FRIDAY)
    assert window["closeAt"] == et(2026, 9, 18, 16, 0).astimezone(timezone.utc)
    assert window["nextOpenAt"] == et(2026, 9, 21, 9, 30).astimezone(timezone.utc)
    assert window["gapHours"] == 65.5
    assert window["cutoff"] == et(2026, 9, 18, 15, 30).astimezone(timezone.utc)


def test_friday_before_a_monday_holiday_has_a_longer_gap():
    """Labor Day 2026-09-07: the exposure is longer, not absent (D4)."""
    window = scheduler.weekend_window(datetime(2026, 9, 4).date())
    assert window["nextOpenAt"] == et(2026, 9, 8, 9, 30).astimezone(timezone.utc)
    assert window["gapHours"] == 89.5


def test_thursday_before_a_friday_holiday_is_the_weekend_eve():
    """Christmas Day 2026 falls on a Friday, so Thursday 12-24 is the last
    session — and it is an early close, so the cut-off moves with it."""
    window = scheduler.weekend_window(datetime(2026, 12, 24).date())
    assert window["closeAt"] == et(2026, 12, 24, 13, 0).astimezone(timezone.utc)
    assert window["cutoff"] == et(2026, 12, 24, 12, 30).astimezone(timezone.utc)
    assert window["nextOpenAt"] == et(2026, 12, 28, 9, 30).astimezone(timezone.utc)
    assert scheduler.weekend_window(datetime(2026, 12, 25).date()) is None    # no session


@pytest.mark.parametrize("day, why", [
    (datetime(2026, 9, 16).date(), "a Wednesday"),
    (datetime(2026, 9, 17).date(), "a Thursday with a session the next day"),
    (datetime(2026, 9, 19).date(), "a Saturday: no session at all"),
    (datetime(2026, 9, 21).date(), "a Monday"),
])
def test_ordinary_days_have_no_weekend_window(day, why):
    assert scheduler.weekend_window(day) is None, why


def test_early_close_friday_keeps_its_1620_settle():
    """The named row on a half day (spec D5): the settle is 3 h 20 after a
    13:00 close, and it is still inside the weekend window."""
    day = datetime(2026, 11, 27).date()
    window = scheduler.weekend_window(day)
    assert window["closeAt"] == et(2026, 11, 27, 13, 0).astimezone(timezone.utc)
    settle = scheduler.settle_time(day)
    assert settle == et(2026, 11, 27, 16, 20).astimezone(timezone.utc)
    assert (scheduler.KIND_SETTLE, settle) in scheduler.slots_for_day(day)
    assert scheduler.weekend_due(scheduler.KIND_SETTLE, settle) is not None


@pytest.mark.parametrize("kind, when, due", [
    (scheduler.KIND_MARKET, et(2026, 9, 18, 15, 25), False),
    (scheduler.KIND_MARKET, et(2026, 9, 18, 15, 30), True),
    (scheduler.KIND_MARKET, et(2026, 9, 18, 16, 0), True),
    (scheduler.KIND_SETTLE, et(2026, 9, 18, 16, 20), True),
    (scheduler.KIND_NIGHT, et(2026, 9, 18, 16, 45), False),
    (scheduler.KIND_MARKET, et(2026, 9, 17, 15, 55), False),
])
def test_weekend_due_edges(kind, when, due):
    assert (scheduler.weekend_due(kind, when.astimezone(timezone.utc)) is not None) is due


def test_weekend_due_needs_an_aware_time():
    with pytest.raises(ValueError):
        scheduler.weekend_due(scheduler.KIND_MARKET, datetime(2026, 9, 18, 15, 30))


def test_eight_rows_carry_a_block_on_a_normal_friday():
    """D5's count: seven market rows from the cut-off plus the settle."""
    due = [(kind, at) for kind, at in scheduler.slots_for_day(FRIDAY)
           if scheduler.weekend_due(kind, at) is not None]
    assert len(due) == 8
    assert [k for k, _ in due] == [scheduler.KIND_MARKET] * 7 + [scheduler.KIND_SETTLE]
    assert due[0][1] == et(2026, 9, 18, 15, 30).astimezone(timezone.utc)


# ── compute_health carries the VIX snapshot (D10) ────────────────

@pytest.mark.asyncio
async def test_compute_health_stores_the_vix_snapshot(monkeypatch):
    dates = ["2026-09-10", "2026-09-11", "2026-09-14", "2026-09-15", "2026-09-16",
             "2026-09-17", "2026-09-18"]
    view = {"asOf": "2026-09-18T19:35:00+00:00", "source": "fresh", "reason": None,
            "staleTickers": [], "tickers": {"^VIX": {
                "date": dates, "close": [14.0, 14.5, 15.0, 15.5, 16.0, 16.5, 22.0],
                "asOf": "2026-09-18T19:35:00+00:00", "stale": False}}}

    async def fake_view(r, memory, *, now):
        return view

    monkeypatch.setattr("scoring.health_calculator.get_quotes_view", fake_view)
    health = await compute_health(None, None,
                                  now=lambda: datetime(2026, 9, 18, 19, 35, tzinfo=timezone.utc))
    assert health["vix5d"]["level"] == 22.0 and health["vix5d"]["partial"] is True
    assert health["vix5d"]["closes"][-1] == 16.5          # complete bars only


# ── run_check attaches it (F6, F8, F9) ───────────────────────────

def snapshot(at, score=72, monitors=None):
    return {"score": score, "regime": classify(score), "coverage": 100, "stale": False,
            "staleMonitors": [],
            "monitors": monitors if monitors is not None else
            {"spy_trend": {"score": 70, "raw": {"date": "2026-09-17"}, "detail": "",
                           "stale": False, "weight": 20}},
            "checkedAt": at.astimezone(timezone.utc).isoformat(),
            "inputs": {"asOf": at.isoformat(), "source": "cached", "reason": None,
                       "staleTickers": []},
            "futures": {"ES=F": None, "NQ=F": None},
            "vix5d": {"level": 26.0, "prevClose": 16.0, "date": "2026-09-18", "partial": True,
                      "asOf": at.isoformat(), "source": "cached",
                      "dates": ["2026-09-10"], "closes": [15.0, 15.0, 15.0, 15.0, 15.0, 16.0]}}


def make_state(**over):
    state = SimpleNamespace(redis=FakeRedis(), db_pool=None, cooldowns=MemoryCooldowns(),
                            check_status={"lastCheckAt": None, "lastKind": None,
                                          "lastScore": None, "lastError": None},
                            inputs_http=None)
    for key, value in over.items():
        setattr(state, key, value)
    return state


def patch_compute(monkeypatch, score=72, monitors=None):
    async def fake(r, memory, *, now):
        return snapshot(now(), score, monitors)
    monkeypatch.setattr(scheduler, "compute_health", fake)


def patch_assemble(monkeypatch, result=None, raises=None):
    seen = {}

    async def fake(state, http, *, now, close_at, next_open_at):
        seen.update(now=now, close_at=close_at, next_open_at=next_open_at, http=http)
        if raises is not None:
            raise raises
        return result or {"events": {"status": weekend.EVENTS_OK, "coverageShort": False,
                                     "events": []},
                          "news": {"status": "empty", "hours": 24, "items": []},
                          "situation": None}

    monkeypatch.setattr(weekend_inputs, "assemble", fake)
    return seen


@pytest.mark.asyncio
async def test_friday_check_carries_a_block(monkeypatch):
    patch_compute(monkeypatch)
    seen = patch_assemble(monkeypatch)
    at = et(2026, 9, 18, 15, 30).astimezone(timezone.utc)
    out = await scheduler.run_check(make_state(), scheduler.KIND_MARKET, clock=lambda: at)
    block = out["health"]["weekend"]
    assert block["level"] == weekend.LEVEL_ELEVATED       # VIX 26 alone
    assert block["inputs"]["vixLevel"] == 26.0 and block["inputs"]["vixPartial"] is True
    assert block["inputs"]["baseScoreAsOf"] == "2026-09-17"
    assert block["inputs"]["gapHours"] == 65.5
    assert seen["close_at"] == et(2026, 9, 18, 16, 0).astimezone(timezone.utc)
    assert seen["next_open_at"] == et(2026, 9, 21, 9, 30).astimezone(timezone.utc)


@pytest.mark.asyncio
async def test_block_records_both_scores_with_the_cap_in_force(monkeypatch):
    """Change 2: the reason reads the capped score; base rides along."""
    patch_compute(monkeypatch)
    patch_assemble(monkeypatch)
    at = et(2026, 9, 18, 15, 30).astimezone(timezone.utc)
    state = make_state()
    out = await scheduler.run_check(state, scheduler.KIND_MARKET, clock=lambda: at)
    health, block = out["health"], out["health"]["weekend"]
    assert block["inputs"]["cappedScore"] == health["score"]
    assert block["inputs"]["baseScore"] == health["overlay"]["base"]
    assert block["inputs"]["regimeSource"] == "capped"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind, when", [
    (scheduler.KIND_MARKET, et(2026, 9, 16, 11, 0)),      # a Wednesday
    (scheduler.KIND_MARKET, et(2026, 9, 18, 15, 25)),     # Friday, pre cut-off
])
async def test_block_absent_off_window(monkeypatch, kind, when):
    """F9: off-window the block is None and the assembly is never called."""
    patch_compute(monkeypatch)
    seen = patch_assemble(monkeypatch)
    out = await scheduler.run_check(make_state(), kind,
                                    clock=lambda: when.astimezone(timezone.utc))
    assert out["health"]["weekend"] is None and seen == {}


@pytest.mark.asyncio
async def test_no_score_no_block(monkeypatch):
    """F6: below the coverage floor there is no score to stand a level on."""
    patch_compute(monkeypatch, score=None)
    patch_assemble(monkeypatch)
    at = et(2026, 9, 18, 15, 30).astimezone(timezone.utc)
    out = await scheduler.run_check(make_state(), scheduler.KIND_MARKET, clock=lambda: at)
    assert out["health"]["weekend"] is None


@pytest.mark.asyncio
async def test_assessment_bug_never_breaks_check(monkeypatch, caplog):
    """F8: a raise inside the block is logged as the bug it is; the score,
    the publish and the check's own error list are untouched."""
    patch_compute(monkeypatch)
    patch_assemble(monkeypatch, raises=RuntimeError("boom: assembly"))
    at = et(2026, 9, 18, 15, 30).astimezone(timezone.utc)
    state = make_state()
    with caplog.at_level(logging.ERROR):
        out = await scheduler.run_check(state, scheduler.KIND_MARKET, clock=lambda: at)
    assert out["health"]["weekend"] is None
    assert out["health"]["score"] == 72 and out["errors"] == []
    assert out["published"]["published"] is True
    assert state.check_status["lastError"] is None
    assert any("Weekend block raised RuntimeError" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_nan_in_the_block_drops_it_and_the_publish_survives(monkeypatch):
    """F12 at the seam: a non-finite number never reaches the payload."""
    weekend._nan_logged = False
    patch_compute(monkeypatch)
    patch_assemble(monkeypatch, result={
        "events": {"status": weekend.EVENTS_OK, "coverageShort": float("nan"), "events": []},
        "news": {"status": "empty", "hours": float("nan"), "items": []}, "situation": None})
    at = et(2026, 9, 18, 15, 30).astimezone(timezone.utc)
    state = make_state()
    out = await scheduler.run_check(state, scheduler.KIND_MARKET, clock=lambda: at)
    assert out["health"]["weekend"] is None
    assert state.weekend_dropped == "nan"
    assert out["published"]["published"] is True
    json.loads(state.redis.published[0][1])               # strict JSON, no NaN


@pytest.mark.asyncio
async def test_night_check_has_no_weekend_key(monkeypatch):
    """A night check never runs the gate: it has no quotes view, so it has
    no VIX to read (spec D5 counts eight rows, all market or settle)."""
    seen = patch_assemble(monkeypatch)
    at = et(2026, 9, 18, 16, 45).astimezone(timezone.utc)
    out = await scheduler.run_check(make_state(), scheduler.KIND_NIGHT, clock=lambda: at)
    assert out is None                                    # no pool: the night check skips
    assert seen == {}


class FailingPool:
    """A pool whose insert always fails; the settle read answers nothing."""

    def __init__(self):
        self.inserts = 0

    async def fetchrow(self, sql, *args):
        return None

    async def execute(self, *args):
        self.inserts += 1
        raise RuntimeError("boom: insert")


@pytest.mark.asyncio
async def test_insert_failure_after_block(monkeypatch, caplog):
    """F11: 3.4's unchanged behaviour with a block present — the publish has
    already happened, the failed insert is a WARNING, the block is intact."""
    patch_compute(monkeypatch)
    patch_assemble(monkeypatch)
    at = et(2026, 9, 18, 15, 30).astimezone(timezone.utc)
    state = make_state(db_pool=FailingPool())
    with caplog.at_level(logging.WARNING):
        out = await scheduler.run_check(state, scheduler.KIND_MARKET, clock=lambda: at)
    assert out["health"]["weekend"]["level"] == weekend.LEVEL_ELEVATED
    assert out["published"]["published"] is True
    assert out["errors"] == ["insert: RuntimeError"]
    assert state.check_status["weekendLevel"] == weekend.LEVEL_ELEVATED
    assert state.db_pool.inserts == 1
