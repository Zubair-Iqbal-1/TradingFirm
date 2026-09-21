"""Part 4.5 — XNYS sessions for the journal (journal/sessions.py).

The real exchange_calendars 4.13.2 runs here: it is a pure computation with
no I/O. Stored bar values are the ones spec 4.5 F4 read from prod."""

import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from journal import sessions

ET = ZoneInfo("America/New_York")


def et(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def utc(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


# ── The two bar rules ────────────────────────────────────────────

def test_bar_date_matches_data_engine_on_a_stored_row():
    """F4: AAPL's 2026-09-18 daily bar is stored at midnight UTC, and
    data-engine's GET /stock/{t}/bars answers ts.isoformat()."""
    stored = "2026-09-18T00:00:00+00:00"
    assert sessions.bar_date(stored) == date(2026, 9, 18)
    # The rule v1 had — convert to ET first — would name the day before.
    assert datetime.fromisoformat(stored).astimezone(ET).date() == date(2026, 9, 17)


def test_hourly_window_uses_bar_start_instants():
    """F4: the 2026-09-18 hourly rows run 13:30Z … 19:30Z. 13:30Z is the
    09:30 ET open, so a ts is the bar's START; 19:30Z (15:30 ET) is the last."""
    day = date(2026, 9, 18)
    first = sessions.bar_start("2026-09-18T13:30:00+00:00")
    last = sessions.bar_start("2026-09-18T19:30:00+00:00")
    assert first == sessions.session_open(day)
    starts = sessions.hourly_starts(day, sessions.session_open(day))
    assert len(starts) == 7 and starts[0] == first and starts[-1] == last
    assert last < sessions.session_close(day) <= last + sessions.HOUR
    # An early close: 13:00 ET, so the last start is 12:30 ET.
    early = date(2026, 11, 27)
    early_starts = sessions.hourly_starts(early, sessions.session_open(early))
    assert early_starts[-1] == et(2026, 11, 27, 12, 30)
    assert sessions.hourly_starts(early, et(2026, 11, 27, 12, 40)) == []
    with pytest.raises(ValueError):
        sessions.bar_start("2026-09-18T13:30:00")


# ── Day 0 ────────────────────────────────────────────────────────

@pytest.mark.parametrize("asked, day0, in_session, starts", [
    (et(2026, 9, 21, 9, 0), date(2026, 9, 18), False, None),     # before the open
    (et(2026, 9, 21, 15, 12), date(2026, 9, 21), True, 1),       # intraday: the AAPL asks
    (et(2026, 9, 21, 15, 45), date(2026, 9, 21), True, 0),       # after the last hourly start
    (et(2026, 9, 21, 16, 30), date(2026, 9, 21), False, None),   # after the close
    (et(2026, 9, 19, 12, 0), date(2026, 9, 18), False, None),    # Saturday
    (et(2026, 11, 26, 11, 0), date(2026, 11, 25), False, None),  # Thanksgiving
    (et(2026, 11, 27, 14, 0), date(2026, 11, 27), False, None),  # early close, afternoon
], ids=["before-open", "intraday", "after-last-hour", "after-close", "weekend",
        "holiday", "early-close-afternoon"])
def test_entry_session_rule(asked, day0, in_session, starts):
    got_day0, got_in = sessions.entry_session(asked)
    assert (got_day0, got_in) == (day0, in_session)
    if in_session:
        assert len(sessions.hourly_starts(got_day0, asked)) == starts


def test_entry_session_rejects_naive_datetimes():
    with pytest.raises(ValueError):
        sessions.entry_session(datetime(2026, 9, 21, 15, 12))


# ── Counting ─────────────────────────────────────────────────────

def test_horizon_counts_xnys_sessions_not_calendar_days():
    assert sessions.nth_session(date(2026, 11, 25), 1) == date(2026, 11, 27)
    assert sessions.nth_session(date(2026, 11, 20), 5) == date(2026, 11, 30)
    assert sessions.sessions_after(date(2026, 11, 24), date(2026, 11, 30)) == [
        date(2026, 11, 25), date(2026, 11, 27), date(2026, 11, 30)]
    # The two AAPL verdicts (spec 4.5 decision 4): day 0 = 2026-09-21.
    assert [sessions.nth_session(date(2026, 9, 21), n) for n in (1, 5, 20)] == [
        date(2026, 9, 22), date(2026, 9, 28), date(2026, 10, 19)]


def test_horizon_expires_after_10_sessions():
    day0 = date(2026, 9, 21)
    target = date(2026, 9, 22)                        # +1
    assert sessions.horizon_state(day0, 1, date(2026, 9, 21)) == (sessions.NOT_DUE, target)
    assert sessions.horizon_state(day0, 1, target) == (sessions.DUE, target)
    tenth = sessions.nth_session(target, 10)
    assert sessions.horizon_state(day0, 1, tenth) == (sessions.DUE, target)
    eleventh = sessions.nth_session(target, 11)
    assert sessions.horizon_state(day0, 1, eleventh) == (sessions.EXPIRED, target)


def test_latest_closed_session():
    assert sessions.latest_closed_session(et(2026, 9, 21, 15, 59)) == date(2026, 9, 18)
    assert sessions.latest_closed_session(et(2026, 9, 21, 16, 0)) == date(2026, 9, 21)
    assert sessions.latest_closed_session(et(2026, 11, 27, 13, 0)) == date(2026, 11, 27)


# ── The slot ─────────────────────────────────────────────────────

def test_slot_is_1730_et_on_sessions_only():
    # A Friday evening → Monday 17:30 EDT = 21:30Z.
    assert sessions.next_slot(et(2026, 9, 18, 18, 0)) == (date(2026, 9, 21), utc(2026, 9, 21, 21, 30))
    # Same day, before the slot.
    assert sessions.next_slot(et(2026, 9, 21, 12, 0)) == (date(2026, 9, 21), utc(2026, 9, 21, 21, 30))
    # Exactly at the slot → the next session's.
    assert sessions.next_slot(et(2026, 9, 21, 17, 30))[0] == date(2026, 9, 22)
    # Across Thanksgiving; the early-close Friday still gets 17:30 EST = 22:30Z.
    assert sessions.next_slot(et(2026, 11, 25, 18, 0)) == (date(2026, 11, 27), utc(2026, 11, 27, 22, 30))
    assert sessions.deadline_at(date(2026, 9, 21)) == utc(2026, 9, 21, 22, 10)


# ── The calendar build ───────────────────────────────────────────

def test_calendar_built_with_explicit_bounds(monkeypatch):
    import exchange_calendars
    seen = []
    real = exchange_calendars.get_calendar

    def spy(name, **kw):
        seen.append((name, kw))
        return real(name, **kw)

    monkeypatch.setattr(exchange_calendars, "get_calendar", spy)
    sessions.reset()
    today = date(2026, 9, 21)
    assert sessions.is_session(today)
    assert seen == [("XNYS", {"start": "2026-01-01", "end": "2027-09-21"})]
    # Cached: a second lookup builds nothing.
    sessions.is_session(date(2026, 9, 22))
    assert len(seen) == 1
    # Near the end (inside the 60-day margin) it rebuilds once, further out.
    sessions.is_session(date(2027, 8, 2))
    assert len(seen) == 2 and seen[1][1]["end"] == "2028-08-02"
    sessions.reset()


def test_importing_main_builds_no_calendar():
    """risk-shield's rule: importing the app loads no calendar and no pandas."""
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys, main; from journal import sessions; "
         "print('exchange_calendars' in sys.modules, 'pandas' in sys.modules, sessions.builds)"],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == "False False 0"
