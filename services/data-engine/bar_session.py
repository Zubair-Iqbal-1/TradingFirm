"""
TradingFirm — the open session's bar is never stored (Part 4.8b-de, spec
docs/specs/4.8b.md decision 15).

yfinance returns today's daily row while the session is still trading, and
the last hourly row before its hour has ended. Stored, either one becomes a
"bar" that later readers take as closed: AAPL's verdicts of 2026-09-21 read
rvol 0.45 on a partial day that closed at 0.80. So every bar write path
(the scanner's winner persist, POST /stock/{t}/refresh and the dossier's
stale refresh, which calls the same helper) passes its records through
`drop_open_session_bars` before `db.upsert_bars`.

Two rules, the same ones ai-agent's journal/sessions.py uses to read bars:
  - a DAILY row's date is its own `ts.date()`, no timezone conversion (the
    stored convention: midnight UTC of the session date). It is dropped while
    that date is today's XNYS session and `now` is before the session's close
    (early closes included, pre-market included).
  - an HOURLY row's `ts` is the bar's START. It is dropped while its hour has
    not ended: `min(start + 1 h, its session's close) > now`.

The XNYS calendar (`exchange_calendars`, the pin risk-shield and ai-agent
use) is built lazily at the first lookup, never at import
(`test_importing_main_builds_no_calendar`), with narrow explicit bounds, and
kept as plain dates and aware UTC instants: no DataFrame outlives the build
(G8). Only today's session is ever asked for.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
HOUR = timedelta(hours=1)

# The calendar spans [today - CALENDAR_DAYS_BACK, today + CALENDAR_DAYS_AHEAD]
# and is rebuilt when a lookup falls outside it.
CALENDAR_DAYS_BACK = 10
CALENDAR_DAYS_AHEAD = 400

INTERVAL_DAILY = "1d"
INTERVAL_HOURLY = "1h"


def utc_now() -> datetime:
    """The clock every write path reads. Tests freeze it by patching this."""
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class _Calendar:
    start: date
    end: date
    opens: dict            # date -> aware UTC datetime
    closes: dict           # date -> aware UTC datetime


_calendar: Optional[_Calendar] = None
builds = 0                  # how many times the calendar was built (tests read it)


def _build(today: date) -> _Calendar:
    global builds
    import exchange_calendars   # deferred: no calendar at import

    start = today - timedelta(days=CALENDAR_DAYS_BACK)
    end = today + timedelta(days=CALENDAR_DAYS_AHEAD)
    cal = exchange_calendars.get_calendar("XNYS", start=start.isoformat(), end=end.isoformat())
    opens, closes = {}, {}
    for session, open_ts, close_ts in zip(cal.sessions, cal.opens, cal.closes):
        d = session.date()
        opens[d] = open_ts.to_pydatetime().astimezone(timezone.utc)
        closes[d] = close_ts.to_pydatetime().astimezone(timezone.utc)
    del cal
    builds += 1
    return _Calendar(start=start, end=end, opens=opens, closes=closes)


def _cal(d: date) -> _Calendar:
    global _calendar
    if _calendar is None or not _calendar.start <= d <= _calendar.end:
        _calendar = _build(d)
    return _calendar


def reset() -> None:
    """Drop the built calendar (tests)."""
    global _calendar
    _calendar = None


def _aware(value: datetime) -> datetime:
    """A naive instant is UTC (yfinance hourly rows are aware; a naive one
    would be the provider's UTC convention)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def et_date(instant: datetime) -> date:
    return _aware(instant).astimezone(ET).date()


def session_times(d: date) -> Optional[tuple[datetime, datetime]]:
    """(open, close) of XNYS session `d` as aware UTC instants, or None."""
    cal = _cal(d)
    if d not in cal.opens:
        return None
    return cal.opens[d], cal.closes[d]


def unclosed_session(now: datetime) -> Optional[tuple[date, datetime, datetime]]:
    """Today's session while it has not closed yet (pre-market included):
    (date, open, close), or None on a non-session day or after the close."""
    now = _aware(now)
    d = et_date(now)
    times = session_times(d)
    if times is None or now >= times[1]:
        return None
    return d, times[0], times[1]


def open_session(now: datetime) -> Optional[tuple[date, datetime, datetime]]:
    """The session trading right now (open <= now < close), or None."""
    found = unclosed_session(now)
    if found is None or _aware(now) < found[1]:
        return None
    return found


def _row_date(ts) -> date:
    return ts.date()


def drop_open_session_bars(
    records: list[dict], interval: str, now: Optional[datetime] = None,
) -> tuple[list[dict], list[dict]]:
    """(kept, dropped) for one interval's records (`db.bar_records_from_df`
    shape). The kept list is what may be stored; the dropped rows are the
    open session's, which the caller may use in memory but never saves."""
    if not records:
        return [], []
    now = _aware(now or utc_now())
    kept, dropped = [], []
    if interval == INTERVAL_DAILY:
        session = unclosed_session(now)
        for row in records:
            (dropped if session is not None and _row_date(row["ts"]) == session[0] else kept).append(row)
    elif interval == INTERVAL_HOURLY:
        for row in records:
            start = _aware(row["ts"])
            end = start + HOUR
            if end > now:
                # Only a row near `now` needs the calendar: its hour may be
                # cut short by the session's close (15:30 ET, or an early close).
                times = session_times(et_date(start))
                if times is not None:
                    end = min(end, times[1])
            (dropped if end > now else kept).append(row)
    else:
        raise ValueError(f"unknown interval {interval!r}")
    if dropped:
        logger.info(
            f"open-session bars not stored: {len(dropped)} {interval} row(s), "
            f"newest {dropped[-1]['ts'].isoformat()}"
        )
    return kept, dropped
