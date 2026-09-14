"""
TradingFirm — assembling the weekend block's inputs (Part 3.4c, D2/D6/D7).

`scoring/weekend.py` is pure. This module is the part that touches the
world for it: the econ calendar's window between a Friday close and the
next open, the news items the afternoon's blocks share, and the
operator's active-situation flag.

Three rules hold everywhere here:

  * **Nothing raises at the caller.** Every section degrades to a status
    the block can carry, because a weekend read must never delay, skip or
    fail a health check (spec F1-F5).
  * **One fetch per afternoon, not one per row.** The news is cached in
    Redis for TTL_WEEKEND_NEWS, so eight Friday rows cost about two calls
    to data-engine. A Redis outage costs a fetch, never the block.
  * **The flag is written only by the route.** A check reads it; nothing
    in the check path ever writes it.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import econ_calendar
import macro_inputs
from cache import (
    KIND_WEEKEND_NEWS,
    SITUATION_MAX_HOURS,
    STATE_WEEKEND_SITUATION,
    TTL_WEEKEND_NEWS,
    get_cached_json,
    risk_key,
    set_cached_json,
    state_key,
)
from scoring import weekend

logger = logging.getLogger(__name__)

# The news window (spec D6): 24 h, inside data-engine's pinned 168 h bound.
WEEKEND_NEWS_HOURS = 24
# The calendar window can never need more than this many ET days to reach
# the next open (the longest XNYS gap is 4 days).
MAX_WINDOW_DAYS = 8


# ── The events between the close and the next open (D7) ──────────

def events_between(now: datetime, close_at: Optional[datetime],
                   next_open_at: Optional[datetime]) -> dict:
    """
    {status, coverageShort, events} for the calendar events that land while
    the market is shut, close and open inclusive: a Monday 08:30 CPI before
    the open counts, because it is priced in before anyone can react.

    An unreadable calendar is status "unavailable" with no events (F1);
    load() has already logged it. A window running past the file's coverage
    keeps what the file has and says so (F2).
    """
    unavailable = {"status": weekend.EVENTS_UNAVAILABLE, "coverageShort": False, "events": []}
    if close_at is None or next_open_at is None:
        return unavailable
    try:
        calendar = econ_calendar.load()
    except econ_calendar.CalendarUnavailable:
        return unavailable

    start = econ_calendar.et_today(now)
    end = next_open_at.astimezone(econ_calendar.ET).date()
    days = min(max((end - start).days + 1, 1), MAX_WINDOW_DAYS)
    body = econ_calendar.window(calendar, now, days)

    inside = []
    for event in body["events"]:
        at = datetime.fromisoformat(event["datetimeUtc"])
        if close_at <= at <= next_open_at:
            inside.append(event)
    return {"status": weekend.EVENTS_OK, "coverageShort": bool(body["coverageShort"]),
            "events": inside}


# ── The news the afternoon shares (D6) ───────────────────────────

def _news_key() -> str:
    return risk_key(KIND_WEEKEND_NEWS)


async def news_view(r, http) -> dict:
    """
    macro_inputs.news_section over data-engine's existing GET /news/market,
    24 h, cached for TTL_WEEKEND_NEWS so a Friday afternoon costs about two
    calls. A cached body is returned with `cached: true`.

    Redis is fail-open in both directions (F5): a read that raises fetches,
    a write that raises is one WARNING and the next row fetches again. An
    "unavailable" section is never cached — a data-engine blip must not
    stand for five minutes.
    """
    if r is not None:
        try:
            cached = await get_cached_json(r, _news_key())
            if isinstance(cached, dict) and cached.get("status"):
                return {**cached, "cached": True}
        except Exception as e:
            logger.warning(f"Weekend news cache read failed, fetching: {e!r}")

    section = await macro_inputs.news_section(http, WEEKEND_NEWS_HOURS)
    view = {"status": section["status"], "hours": section["hours"], "items": section["items"]}
    if r is not None and view["status"] != "unavailable":
        try:
            await set_cached_json(r, _news_key(), view, TTL_WEEKEND_NEWS)
        except Exception as e:
            logger.warning(f"Weekend news cache write failed: {e!r}")
    return {**view, "cached": False}


# ── The operator's active situation (D2) ─────────────────────────

def _situation_key() -> str:
    return state_key(STATE_WEEKEND_SITUATION)


def build_situation(text: str, hours: int, now: datetime) -> dict:
    """The stored record. `hours` is bounded by the route; `expiresAt` is
    mandatory, so a forgotten flag dies on its own."""
    expires = now + timedelta(hours=hours)
    return {"text": text.strip()[:weekend.SITUATION_TEXT_MAX],
            "setAt": now.astimezone(timezone.utc).isoformat(),
            "expiresAt": expires.astimezone(timezone.utc).isoformat(),
            "setBy": "operator"}


async def read_situation(r) -> Optional[dict]:
    """The stored flag, or None. Never raises: Redis down reads as absent,
    and so does a value of the wrong shape (F5). Expiry itself is
    `weekend.situation_active`, so the block and the route agree."""
    if r is None:
        return None
    try:
        raw = await get_cached_json(r, _situation_key())
    except Exception as e:
        logger.warning(f"Weekend situation read failed, treating as absent: {e!r}")
        return None
    if raw is not None and not isinstance(raw, dict):
        logger.warning("Weekend situation has the wrong shape, treating as absent")
        return None
    return raw


async def write_situation(r, record: dict, hours: int) -> None:
    """Store the flag with a TTL from the same `hours` as `expiresAt`, so an
    expired key cannot linger (F14). Raises for the route to turn into a 503:
    a write endpoint that silently stores nothing would be worse."""
    ttl = max(int(min(hours, SITUATION_MAX_HOURS) * 3600), 1)
    await set_cached_json(r, _situation_key(), record, ttl)


async def clear_situation(r) -> bool:
    """Delete the flag. True when one was there."""
    return bool(await r.delete(_situation_key()))


# ── One assembly (what run_check calls, 3a) ──────────────────────

async def assemble(state, http, *, now: datetime, close_at: Optional[datetime],
                   next_open_at: Optional[datetime]) -> dict:
    """
    {events, news, situation} for one block. Every branch is already
    degraded to a status; this only ever returns a dict, and the one thing
    it can cost is the news fetch's 10 s timeout inside a 5-minute slot.
    """
    r = getattr(state, "redis", None)
    events = events_between(now, close_at, next_open_at)
    news = await news_view(r, http)
    situation = await read_situation(r)
    return {"events": events, "news": news, "situation": situation}
