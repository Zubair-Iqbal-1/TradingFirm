"""Part 3.4 — when a health check runs (spec decision 3). The real XNYS
calendar (bundled rules, no network) under frozen time: every `now` is
passed in, nothing reads the clock."""

import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd
import pytest

import scheduler

ET = ZoneInfo("America/New_York")


def et(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=ET)


@pytest.fixture(autouse=True)
def fresh_calendar():
    _reset_calendars()
    yield
    _reset_calendars()


def _reset_calendars():
    scheduler._calendar_state["cal"] = None
    scheduler._futures_calendar_state["cal"] = None
    scheduler._slots_cache.clear()


def night_starts(day):
    return [start for kind, start in scheduler.slots_for_day(day) if kind == scheduler.KIND_NIGHT]


def _counting_factory(monkeypatch, factory=None):
    calls = []
    real = factory or scheduler._build_calendar

    def build(day):
        calls.append(day)
        return real(day)

    monkeypatch.setattr(scheduler, "_build_calendar", build)
    return calls


def test_slot_market_every_five_minutes_inclusive_of_close():
    d = (2026, 9, 10)    # Thursday, regular session
    assert scheduler.slot_for(et(*d, 9, 29, 59)) is None
    assert scheduler.slot_for(et(*d, 9, 30)) == ("market", et(*d, 9, 30))
    assert scheduler.slot_for(et(*d, 9, 35)) == ("market", et(*d, 9, 35))
    assert scheduler.slot_for(et(*d, 9, 37, 30)) == ("market", et(*d, 9, 35))
    assert scheduler.slot_for(et(*d, 16, 0)) == ("market", et(*d, 16, 0))
    assert scheduler.slot_for(et(*d, 16, 5)) is None


def test_slot_settle_1620_on_session_days_only():
    assert scheduler.slot_for(et(2026, 9, 10, 16, 20)) == ("settle", et(2026, 9, 10, 16, 20))
    assert scheduler.slot_for(et(2026, 9, 10, 16, 19, 59)) is None
    assert scheduler.slot_for(et(2026, 9, 7, 16, 20)) is None     # Labor Day
    assert scheduler.slot_for(et(2026, 9, 12, 16, 20)) is None    # Saturday


def test_slot_early_close_uses_calendar_close():
    d = (2026, 11, 27)   # day after Thanksgiving, closes 13:00
    assert scheduler.slot_for(et(*d, 13, 0)) == ("market", et(*d, 13, 0))
    assert scheduler.slot_for(et(*d, 13, 5)) is None
    assert scheduler.slot_for(et(*d, 15, 0)) is None
    assert scheduler.slot_for(et(*d, 16, 20)) == ("settle", et(*d, 16, 20))


@pytest.mark.parametrize("day", [date(2026, 9, 7), date(2026, 11, 26), date(2026, 9, 12), date(2026, 9, 13)])
def test_slot_holiday_and_weekend_have_none(day):
    # Part 3.4b: CME may trade on these dates, so night slots can exist; a
    # market or settle slot never does.
    assert {kind for kind, _ in scheduler.slots_for_day(day)} <= {scheduler.KIND_NIGHT}
    start = datetime(day.year, day.month, day.day, tzinfo=ET)
    for minutes in range(0, 24 * 60, 5):
        due = scheduler.slot_for(start + timedelta(minutes=minutes))
        assert due is None or due[0] == scheduler.KIND_NIGHT


def test_slot_counts_per_session_day():
    normal = scheduler.slots_for_day(date(2026, 9, 10))
    assert [k for k, _ in normal].count("market") == 79
    assert [k for k, _ in normal].count("settle") == 1
    # Part 3.4b: night slots share the list, so the market run is read by kind.
    market = [s for k, s in normal if k == "market"]
    assert market[0] == et(2026, 9, 10, 9, 30) and market[-1] == et(2026, 9, 10, 16, 0)
    assert [s for k, s in normal if k == "settle"] == [et(2026, 9, 10, 16, 20)]
    assert normal[0] == (scheduler.KIND_NIGHT, et(2026, 9, 10, 0, 15))

    early = scheduler.slots_for_day(date(2026, 11, 27))
    assert [k for k, _ in early].count("market") == 43
    assert [k for k, _ in early].count("settle") == 1
    starts = [s for _, s in normal]
    assert starts == sorted(starts)


def test_slot_uses_eastern_time_across_dst():
    # 2026-11-01 DST ends: Monday 11-02 09:30 EST is 14:30 UTC.
    utc = timezone.utc
    assert scheduler.slot_for(datetime(2026, 11, 2, 14, 30, tzinfo=utc)) == (
        "market", datetime(2026, 11, 2, 14, 30, tzinfo=utc))
    assert scheduler.slot_for(datetime(2026, 11, 2, 13, 30, tzinfo=utc)) is None
    # The Friday before, still EDT: 09:30 is 13:30 UTC.
    assert scheduler.slot_for(datetime(2026, 10, 30, 13, 30, tzinfo=utc)) == (
        "market", datetime(2026, 10, 30, 13, 30, tzinfo=utc))
    # Settle follows the Eastern wall clock too.
    assert scheduler.slot_for(datetime(2026, 11, 2, 21, 20, tzinfo=utc))[0] == "settle"
    assert scheduler.slot_for(datetime(2026, 10, 30, 20, 20, tzinfo=utc))[0] == "settle"


def test_next_slot_after_skips_weekend_and_holiday():
    assert scheduler.next_slot_after(et(2026, 9, 10, 9, 31, 10)) == ("market", et(2026, 9, 10, 9, 35))
    # Strictly after: from a slot start, the next one.
    assert scheduler.next_slot_after(et(2026, 9, 10, 9, 35)) == ("market", et(2026, 9, 10, 9, 40))
    assert scheduler.next_slot_after(et(2026, 9, 10, 16, 0)) == ("settle", et(2026, 9, 10, 16, 20))
    # Part 3.4b: Friday after settle → that evening's 16:45 night slot.
    assert scheduler.next_slot_after(et(2026, 9, 11, 16, 21)) == ("night", et(2026, 9, 11, 16, 45))
    assert scheduler.next_slot_after(et(2026, 9, 4, 16, 21)) == ("night", et(2026, 9, 4, 16, 45))
    # Friday evening → Sunday 18:15, the first slot of the new CME week.
    assert scheduler.next_slot_after(et(2026, 9, 11, 16, 46)) == ("night", et(2026, 9, 13, 18, 15))
    # The last night slot before an open → that open.
    assert scheduler.next_slot_after(et(2026, 9, 14, 8, 45)) == ("market", et(2026, 9, 14, 9, 30))
    # Early close: after 13:00 the next slot is that day's settle.
    assert scheduler.next_slot_after(et(2026, 11, 27, 13, 1)) == ("settle", et(2026, 11, 27, 16, 20))


@pytest.mark.parametrize("bad", [datetime(2026, 9, 10, 14, 0), "2026-09-10T14:00:00+00:00", None])
def test_slot_rejects_naive_datetime(bad):
    with pytest.raises(ValueError):
        scheduler.slot_for(bad)
    with pytest.raises(ValueError):
        scheduler.next_slot_after(bad)


def test_calendar_out_of_bounds_rebuilds_then_fails_closed(monkeypatch, caplog):
    # A cached calendar that no longer covers the date is rebuilt once, and
    # the rebuild serves the slot.
    scheduler._calendar_state["cal"] = xcals.get_calendar("XNYS", start="2026-01-02", end="2026-03-31")
    calls = _counting_factory(monkeypatch)
    with caplog.at_level(logging.WARNING):
        assert scheduler.slot_for(et(2026, 9, 10, 9, 30)) == ("market", et(2026, 9, 10, 9, 30))
    assert len(calls) == 1
    assert any("rebuilding" in r.getMessage() for r in caplog.records)

    # A rebuild that still cannot cover it: no slot, ERROR, no second rebuild.
    scheduler._calendar_state["cal"] = None
    narrow = lambda day: xcals.get_calendar("XNYS", start="2026-01-02", end="2026-03-31")
    calls = _counting_factory(monkeypatch, narrow)
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        assert scheduler.slot_for(et(2026, 9, 10, 9, 30)) is None
        assert scheduler.next_slot_after(et(2026, 9, 10, 9, 30)) is None
    assert len(calls) == 2    # exactly one rebuild per call
    assert sum(r.levelno == logging.ERROR for r in caplog.records) == 2


def test_slot_for_is_pure_and_calendar_cached(monkeypatch):
    calls = _counting_factory(monkeypatch)
    now = et(2026, 9, 10, 11, 2, 30)
    first = scheduler.slot_for(now)
    assert scheduler.slot_for(now) == first == ("market", et(2026, 9, 10, 11, 0))
    assert scheduler.next_slot_after(now) == scheduler.next_slot_after(now)
    assert len(calls) == 1


# ── Night slots (Part 3.4b decision 2) ───────────────────────────

def test_night_slots_every_thirty_minutes_outside_session():
    d = (2026, 9, 10)                         # Thursday, regular session
    starts = night_starts(date(*d))
    assert starts[0] == et(*d, 0, 15) and starts[-1] == et(*d, 23, 45)
    assert all(s.astimezone(ET).minute in scheduler.NIGHT_MINUTES for s in starts)
    assert et(*d, 8, 45) in starts and et(*d, 16, 45) in starts
    for inside in (et(*d, 9, 45), et(*d, 12, 15), et(*d, 16, 15)):
        assert inside not in starts           # the session and the close-to-settle gap
    assert scheduler.slot_for(et(*d, 20, 15)) == ("night", et(*d, 20, 15))
    assert scheduler.slot_for(et(*d, 20, 21)) is None


@pytest.mark.parametrize("day", [date(2026, 9, 10), date(2026, 9, 11)], ids=["thursday", "friday"])
def test_night_skips_the_1700_1800_halt(day):
    starts = night_starts(day)
    y, m, d = day.year, day.month, day.day
    assert et(y, m, d, 16, 45) in starts
    assert et(y, m, d, 17, 15) not in starts and et(y, m, d, 17, 45) not in starts
    # CMES models neither the halt nor the Friday close: the cut is ours.
    assert scheduler.futures_calendar(day).is_open_on_minute(pd.Timestamp(et(y, m, d, 17, 30)))


def test_night_weekend_closed_until_sunday_1815():
    assert night_starts(date(2026, 9, 11))[-1] == et(2026, 9, 11, 16, 45)
    assert night_starts(date(2026, 9, 12)) == []
    sunday = night_starts(date(2026, 9, 13))
    assert sunday[0] == et(2026, 9, 13, 18, 15) and sunday[-1] == et(2026, 9, 13, 23, 45)


def test_night_follows_cme_holiday_hours():
    labor = night_starts(date(2026, 9, 7))            # XNYS closed, CME closes 13:00
    assert labor[0] == et(2026, 9, 7, 0, 15) and labor[-1] == et(2026, 9, 7, 23, 45)
    assert et(2026, 9, 7, 12, 45) in labor and et(2026, 9, 7, 13, 15) not in labor
    assert night_starts(date(2026, 11, 27))[-1] == et(2026, 11, 27, 8, 45)   # CME 13:00 close
    assert night_starts(date(2026, 12, 24))[-1] == et(2026, 12, 24, 8, 45)
    assert night_starts(date(2026, 12, 25)) == []


def test_night_slot_counts_per_day():
    assert {d: len(night_starts(date(2026, 9, d))) for d in (7, 10, 11, 12, 13)} == {
        7: 38, 10: 31, 11: 19, 12: 0, 13: 12}
    assert len(night_starts(date(2026, 11, 26))) == 38                        # Thanksgiving
    week = sum(len(night_starts(date(2026, 9, d))) for d in range(14, 21))    # Mon–Sun
    assert week == 155


def test_night_stops_45_minutes_before_the_open():
    starts = night_starts(date(2026, 9, 10))
    assert et(2026, 9, 10, 8, 45) in starts        # clears its cooldown by 09:00:30
    assert et(2026, 9, 10, 9, 15) not in starts    # would still be cooling at 09:30
    assert scheduler.slot_for(et(2026, 9, 10, 9, 15)) is None
    # A date with no XNYS open has nothing to stand clear of.
    assert et(2026, 9, 7, 9, 15) in night_starts(date(2026, 9, 7))


def test_night_slots_use_eastern_time_across_dst():
    utc = timezone.utc
    # 2026-11-01, DST ends: Sunday 18:15 EST = 23:15 UTC.
    assert scheduler.slot_for(datetime(2026, 11, 1, 23, 15, tzinfo=utc)) == (
        "night", datetime(2026, 11, 1, 23, 15, tzinfo=utc))
    # The Friday before, still EDT: 16:45 = 20:45 UTC.
    assert scheduler.slot_for(datetime(2026, 10, 30, 20, 45, tzinfo=utc)) == (
        "night", datetime(2026, 10, 30, 20, 45, tzinfo=utc))


def test_futures_calendar_out_of_bounds_fails_closed(monkeypatch, caplog):
    narrow = lambda day: xcals.get_calendar("CMES", start="2026-01-02", end="2026-03-31")
    monkeypatch.setattr(scheduler, "_build_futures_calendar", narrow)
    with caplog.at_level(logging.ERROR):
        assert scheduler.slot_for(et(2026, 9, 10, 20, 15)) is None
        assert scheduler.next_slot_after(et(2026, 9, 10, 20, 15)) is None
    assert sum(r.levelno == logging.ERROR for r in caplog.records) == 2
    # The market calendar is unaffected by the futures calendar failing.
    assert scheduler.market_calendar(date(2026, 9, 10)).is_session(pd.Timestamp("2026-09-10"))


def test_night_slot_for_is_pure_and_calendars_cached(monkeypatch):
    market_calls = _counting_factory(monkeypatch)
    futures_calls = []
    real = scheduler._build_futures_calendar
    monkeypatch.setattr(scheduler, "_build_futures_calendar",
                        lambda day: (futures_calls.append(day), real(day))[1])
    now = et(2026, 9, 10, 20, 15)
    first = scheduler.slot_for(now)
    assert scheduler.slot_for(now) == first == ("night", et(2026, 9, 10, 20, 15))
    assert scheduler.next_slot_after(now) == scheduler.next_slot_after(now)
    assert len(market_calls) == 1 and len(futures_calls) == 1
