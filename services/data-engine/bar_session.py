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


# ── sessionSoFar (spec 4.8b decision 16) ─────────────────────────
#
# The open session's row, dropped above, is still worth reading as what it
# is: today so far, not a candle. The write path that dropped it computes
# this block from the same download and keeps it in Redis until the close;
# readers attach it at read time, never from a cached body.

RVOL_LOOKBACK = 20
RVOL_MIN_ELAPSED_SECONDS = 300      # no scaled RVOL in the first 5 minutes


def session_so_far(
    open_bar: dict, prior_daily: list[dict], now: datetime,
    session_open: datetime, session_close: datetime,
) -> dict:
    """The block for the open session's row `open_bar`, as of `now` (the
    download instant). `prior_daily` are the stored-shape daily rows before
    it, oldest first. Keys carry their units: prices, shares, a 0–1
    fraction of the session, a percent, a ratio like `rvol`."""
    now = _aware(now)
    length = (session_close - session_open).total_seconds()
    elapsed_s = min(max((now - session_open).total_seconds(), 0.0), length)
    elapsed = elapsed_s / length if length > 0 else None
    prior_close = float(prior_daily[-1]["close"]) if prior_daily else None
    last = float(open_bar["close"])
    change = ((last - prior_close) / prior_close * 100) if prior_close else None
    window = [float(r["volume"]) for r in prior_daily[-RVOL_LOOKBACK:]]
    avg = sum(window) / len(window) if len(window) == RVOL_LOOKBACK else None
    volume = float(open_bar["volume"])
    rvol = (volume / elapsed / avg
            if avg and elapsed and elapsed_s >= RVOL_MIN_ELAPSED_SECONDS else None)
    return {
        "open": float(open_bar["open"]),
        "high": float(open_bar["high"]),
        "low": float(open_bar["low"]),
        "last": last,
        "volumeSoFar": int(volume),
        "sessionElapsedFrac": elapsed,
        "changeVsPriorClosePct": change,
        "rvolScaled": rvol,
        "inProgress": True,
    }


async def stash_session_so_far(redis, ticker: str, dropped_daily: list[dict],
                               prior_daily: list[dict], now: datetime) -> Optional[dict]:
    """Compute and keep the block for the dropped open-session row, TTL to
    the close. Only while the session trades (a pre-market row has no
    session yet). Fail-open: a Redis failure is logged, never raised."""
    if not dropped_daily or redis is None:
        return None
    session = open_session(now)
    if session is None or _row_date(dropped_daily[-1]["ts"]) != session[0]:
        return None
    block = session_so_far(dropped_daily[-1], prior_daily, now, session[1], session[2])
    ttl = max(int((session[2] - _aware(now)).total_seconds()), 1)
    try:
        from cache import set_session_so_far
        # `fetchedAt` (the download instant) rides in the stash only, for the
        # read-side freshness rule; readers strip it (`_public`).
        await set_session_so_far(redis, ticker, {**block, FETCHED_AT: _aware(now).isoformat()}, ttl)
    except Exception as e:
        logger.warning(f"sessionSoFar stash failed for {ticker}: {type(e).__name__}")
        return None
    return block


FETCHED_AT = "fetchedAt"


def _public(stash: Optional[dict]) -> Optional[dict]:
    return None if stash is None else {k: v for k, v in stash.items() if k != FETCHED_AT}


async def _get_stash(redis, ticker: str) -> Optional[dict]:
    try:
        from cache import get_session_so_far
        return await get_session_so_far(redis, ticker)
    except Exception as e:
        logger.warning(f"sessionSoFar read failed for {ticker}: {type(e).__name__}")
        return None


async def read_session_so_far(redis, ticker: str, now: Optional[datetime] = None) -> Optional[dict]:
    """The stashed block while a session is open, else None — so a body
    cached before the close never carries it after. No download: the
    read-and-fill path is `session_so_far_on_read`. Fail-open."""
    if redis is None or open_session(_aware(now or utc_now())) is None:
        return None
    return _public(await _get_stash(redis, ticker))


# ── The read-side fill-in (Zubair's fix of 2026-09-25) ───────────
#
# Without it `sessionSoFar` exists only when a refresh or scan happened to
# download this session. On a read in market hours with no stash, or one
# fetched more than 15 minutes ago, one light download of today's row fills
# it: `download_daily([t], "2mo")` (the 20 prior sessions the scaled RVOL
# needs), no bar write, and NOT `refresh_ticker_bars` — the refresh's own
# 15-minute reject (main.py, `refresh_cooldown_name`) is never consulted.
# Budget: at most one download per ticker per 15 minutes, held by an atomic
# SET NX EX 900 gate taken before the call (so ≤ 4 an hour per ticker, and
# two concurrent reads download once); none outside market hours or without
# Redis. A failed download is not retried until the gate expires.

SESSION_FRESH_SECONDS = 900
SESSION_FETCH_PERIOD = "2mo"
SESSION_FETCH_TIMEOUT = 10.0


async def session_so_far_on_read(redis, provider, ticker: str,
                                 now: Optional[datetime] = None) -> Optional[dict]:
    """sessionSoFar for a reader: the stash if fetched ≤ 15 min ago, else one
    gated download of today's row, stashed and returned. None outside a
    session, without Redis, when the gate is held, or when the download
    fails or has no row for today. Never raises."""
    import asyncio

    now = _aware(now or utc_now())
    if redis is None or open_session(now) is None:
        return None
    stash = await _get_stash(redis, ticker)
    if stash is not None:
        try:
            age = (now - datetime.fromisoformat(stash[FETCHED_AT])).total_seconds()
        except (KeyError, TypeError, ValueError):
            age = None
        if age is not None and 0 <= age <= SESSION_FRESH_SECONDS:
            return _public(stash)
    if provider is None:
        return None
    try:
        from cache import session_fetch_key
        if not await redis.set(session_fetch_key(ticker), now.isoformat(),
                               ex=SESSION_FRESH_SECONDS, nx=True):
            return None                     # a download ran < 15 min ago
    except Exception as e:
        logger.warning(f"sessionSoFar gate failed for {ticker}: {type(e).__name__}")
        return None
    try:
        from db import bar_records_from_df
        logger.info(f"sessionSoFar fill-in: 1 daily download for {ticker} ({SESSION_FETCH_PERIOD})")
        bulk = await asyncio.wait_for(
            provider.download_daily([ticker], period=SESSION_FETCH_PERIOD), SESSION_FETCH_TIMEOUT)
        records = bar_records_from_df(provider.extract_ticker_df(bulk, ticker))
        del bulk
    except Exception as e:
        logger.warning(f"sessionSoFar fill-in download failed for {ticker}: {type(e).__name__}")
        return None
    kept, dropped = drop_open_session_bars(records, INTERVAL_DAILY, now)
    return await stash_session_so_far(redis, ticker, dropped, kept, now)
