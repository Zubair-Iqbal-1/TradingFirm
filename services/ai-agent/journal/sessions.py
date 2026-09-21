"""
TradingFirm — XNYS sessions for the journal (Part 4.5).

`exchange_calendars` is the counter: +1 / +5 / +20 / +30 / +60 are XNYS sessions, never
calendar days or weekdays (spec 4.5 decision 3). Bar dates are the second
check, in the runner.

The calendar is built lazily, at the first lookup, never at import
(`test_importing_main_builds_no_calendar`), with explicit bounds —
2026-01-01 to today + 1 year, not the library's 20-year default — and kept
as plain dates and aware UTC instants: no DataFrame outlives the build (G8).
A process that runs until it is near the end rebuilds once.

Two date rules, and only two:
  - a DAILY bar's date is data-engine's own: `ts.date()` of the stored
    timestamp with no timezone conversion. data-engine stores a daily bar at
    midnight UTC of the session date (spec 4.5 F4: `2026-09-18T00:00:00+00:00`
    is the 2026-09-18 bar); converting to ET first would make it 09-17.
  - an HOURLY bar has no date function at all: its `ts` is the bar's START
    (F4: 13:30Z is the 09:30 ET open), compared as an instant against the
    ask and against the calendar's close.
"""

import bisect
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

CALENDAR_START = date(2026, 1, 1)
CALENDAR_YEARS_AHEAD = 1
# Rebuild when a lookup comes within this many days of the built end, so the
# next slot and "today" are always inside the bounds.
REBUILD_MARGIN_DAYS = 60

SLOT_TIME = time(17, 30)        # ET, every XNYS session (decision 2)
DEADLINE_TIME = time(18, 10)    # ET: no refresh starts after this
EXPIRY_SESSIONS = 10            # a horizon is dropped this many sessions after its target
# The horizons every verdict is scored at, in XNYS sessions. The one list:
# the runner, the stats and db.due_verdicts read it, and 008's CHECK is
# pinned to it (test_migration_008_horizons_match_the_code).
HORIZONS = (1, 5, 20, 30, 60)
HOUR = timedelta(hours=1)


@dataclass(frozen=True)
class _Calendar:
    end: date
    days: tuple            # sorted session dates
    opens: dict            # date -> aware UTC datetime
    closes: dict           # date -> aware UTC datetime


_calendar: Optional[_Calendar] = None
builds = 0                  # how many times the calendar was built (tests read it)


def _build(today: date) -> _Calendar:
    global builds
    import exchange_calendars   # deferred: pandas never loads at import

    end = today.replace(year=today.year + CALENDAR_YEARS_AHEAD) if not (
        today.month == 2 and today.day == 29) else date(today.year + CALENDAR_YEARS_AHEAD, 2, 28)
    cal = exchange_calendars.get_calendar(
        "XNYS", start=CALENDAR_START.isoformat(), end=end.isoformat()
    )
    days, opens, closes = [], {}, {}
    for session, open_ts, close_ts in zip(cal.sessions, cal.opens, cal.closes):
        d = session.date()
        days.append(d)
        opens[d] = open_ts.to_pydatetime().astimezone(timezone.utc)
        closes[d] = close_ts.to_pydatetime().astimezone(timezone.utc)
    del cal
    builds += 1
    return _Calendar(end=end, days=tuple(days), opens=opens, closes=closes)


def _cal(today: date) -> _Calendar:
    global _calendar
    if _calendar is None or today + timedelta(days=REBUILD_MARGIN_DAYS) > _calendar.end:
        _calendar = _build(today)
    return _calendar


def reset() -> None:
    """Drop the built calendar (tests)."""
    global _calendar
    _calendar = None


def _require_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def et_date(instant: datetime) -> date:
    _require_aware(instant, "instant")
    return instant.astimezone(ET).date()


# ── The two bar rules ────────────────────────────────────────────

def bar_date(ts: str) -> date:
    """A DAILY bar's date, by data-engine's rule: the stored timestamp's own
    date, no conversion (`assemble.py:492` does `ts.date()`)."""
    return datetime.fromisoformat(ts).date()


def bar_start(ts: str) -> datetime:
    """An HOURLY bar's start, as an aware instant."""
    value = datetime.fromisoformat(ts)
    _require_aware(value, "hourly bar ts")
    return value


# ── Sessions ─────────────────────────────────────────────────────

def is_session(d: date, today: Optional[date] = None) -> bool:
    return d in _cal(today or d).opens


def session_open(d: date) -> datetime:
    return _cal(d).opens[d]


def session_close(d: date) -> datetime:
    return _cal(d).closes[d]


def latest_closed_session(now: datetime) -> date:
    """The last session whose close is at or before `now`."""
    _require_aware(now, "now")
    cal = _cal(et_date(now))
    i = bisect.bisect_right(cal.days, et_date(now)) - 1
    while i >= 0 and cal.closes[cal.days[i]] > now:
        i -= 1
    if i < 0:
        raise ValueError(f"no closed session on or before {now.isoformat()}")
    return cal.days[i]


def entry_session(asked_at: datetime) -> tuple[date, bool]:
    """Day 0, and whether the ask fell inside it (spec 4.5 decision 4).

    Asked while a session is open → that session, True. Asked outside one
    (before the open, after the close, a weekend, a holiday) → the last
    closed session, False.
    """
    _require_aware(asked_at, "asked_at")
    d = et_date(asked_at)
    cal = _cal(d)
    if d in cal.opens and cal.opens[d] <= asked_at < cal.closes[d]:
        return d, True
    return latest_closed_session(asked_at), False


def nth_session(day0: date, n: int, today: Optional[date] = None) -> date:
    """The Nth session after day 0 (n ≥ 1)."""
    cal = _cal(today or day0)
    i = cal.days.index(day0)
    if i + n >= len(cal.days):
        raise ValueError(f"session {n} after {day0} is past the calendar's end")
    return cal.days[i + n]


def sessions_after(a: date, b: date, today: Optional[date] = None) -> list[date]:
    """Sessions in (a, b], in order."""
    cal = _cal(today or b)
    lo = bisect.bisect_right(cal.days, a)
    hi = bisect.bisect_right(cal.days, b)
    return list(cal.days[lo:hi])


def hourly_starts(day0: date, asked_at: datetime) -> list[datetime]:
    """The hourly bar starts expected on day 0 at or after the ask:
    open + k h, every one before the close. On an early close the last start
    is 12:30 ET; on a full day 15:30 ET."""
    _require_aware(asked_at, "asked_at")
    opens, close = session_open(day0), session_close(day0)
    starts, start = [], opens
    while start < close:
        if start >= asked_at:
            starts.append(start)
        start += HOUR
    return starts


# ── Due, pending, expired ────────────────────────────────────────

DUE, NOT_DUE, EXPIRED = "due", "not_due", "expired"


def horizon_state(day0: date, horizon: int, latest_closed: date,
                  today: Optional[date] = None) -> tuple[str, Optional[date]]:
    """(state, target session). Due = the target has closed and is at most
    EXPIRY_SESSIONS sessions old; expired = older (spec 4.5 decision 5)."""
    target = nth_session(day0, horizon, today or latest_closed)
    if target > latest_closed:
        return NOT_DUE, target
    if len(sessions_after(target, latest_closed, today or latest_closed)) > EXPIRY_SESSIONS:
        return EXPIRED, target
    return DUE, target


# ── The slot ─────────────────────────────────────────────────────

def _et_instant(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=ET).astimezone(timezone.utc)


def slot_at(d: date) -> datetime:
    return _et_instant(d, SLOT_TIME)


def deadline_at(d: date) -> datetime:
    return _et_instant(d, DEADLINE_TIME)


def next_slot(now: datetime) -> tuple[date, datetime]:
    """The next 17:30 ET slot strictly after `now`, on a session."""
    _require_aware(now, "now")
    today = et_date(now)
    cal = _cal(today)
    i = bisect.bisect_left(cal.days, today)
    while True:
        d = cal.days[i]
        slot = slot_at(d)
        if slot > now:
            return d, slot
        i += 1
