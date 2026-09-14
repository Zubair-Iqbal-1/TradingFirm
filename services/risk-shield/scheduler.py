"""
TradingFirm — the regime scheduler (Part 3.4).

When a health check runs (spec 3.4 decision 3), from the XNYS calendar:
  market  every 5-minute slot of the regular session, open … close inclusive
          (09:30 … 16:00 = 79 slots; 43 on an early close)
  settle  16:20 ET on every session day, early closes included. That is after
          3.3's 16:15 partial-bar cut-off, so the day's bars are complete and
          3.3's rule stays unchanged.
Nothing runs outside those slots (night mode is 3.4b).

One check (decision 4) is compute → trend base → publish → insert, each
side effect isolated, so a Postgres failure never delays a publish and a
Redis failure never stops a row.

Every function takes an aware `now` or a `clock`; nothing reads the time
directly.
"""

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

import db
import news_poller
import wallclock
import weekend_inputs
from monitors import quotes
from scoring import overlay, weekend
from scoring.alert_manager import publish_health
from scoring.health_calculator import compute_health
from scoring.regime_classifier import classify

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
CALENDAR_NAME = "XNYS"
FUTURES_CALENDAR_NAME = "CMES"  # CME Globex equity futures (spec 3.4b decision 10)
SLOT_MINUTES = 5
SETTLE_TIME_ET = time(16, 20)   # provisional (spec 3.4 decision 3)
GRACE_SECONDS = 60              # provisional: a slot runs only this late
KIND_MARKET = "market"
KIND_SETTLE = "settle"
KIND_NIGHT = "night"

# Night slots (spec 3.4b decision 2): :15 and :45 ET while CME equity futures
# trade and no XNYS session is under way.
NIGHT_MINUTES = (15, 45)
HALT_START_ET = time(17, 0)     # CMES models neither the daily 17:00-18:00 ET
HALT_END_ET = time(18, 0)       # halt nor the Friday 17:00 close: cut by hand
# 08:45 is the last morning slot, so a refusal's 900 s yfinance cooldown clears
# before the open and the 09:30 check still downloads (3.4b decision 7).
PRE_OPEN_QUIET = timedelta(minutes=45)

# The calendar is built around the date asked about, so one build covers
# every date the scheduler looks at for about a year.
CALENDAR_DAYS_BEFORE = 30
CALENDAR_DAYS_AFTER = 400
NEXT_SLOT_HORIZON_DAYS = 15     # the longest gap between sessions is 4 days
SLOTS_CACHE_MAX = 60            # dates kept in the slot cache (G8); the loop looks 15 days out


class CalendarOutOfBounds(RuntimeError):
    """A calendar cannot cover the date even after a rebuild."""


# One calendar of each kind per process, built lazily (3.4 decision 2, 3.4b
# decision 10). Pure data, no network. _slots_cache holds one day's slots, since
# night slots ask the futures calendar 48 times a day; a rebuild clears it.
_calendar_state: dict[str, Any] = {"cal": None}
_futures_calendar_state: dict[str, Any] = {"cal": None}
_slots_cache: dict[date, list] = {}


def _build(name: str, day: date):
    return xcals.get_calendar(
        name,
        start=day - timedelta(days=CALENDAR_DAYS_BEFORE),
        end=day + timedelta(days=CALENDAR_DAYS_AFTER),
    )


def _build_calendar(day: date):
    return _build(CALENDAR_NAME, day)


def _build_futures_calendar(day: date):
    return _build(FUTURES_CALENDAR_NAME, day)


def _covers(cal, day: date) -> bool:
    ts = pd.Timestamp(day)
    return cal.first_session <= ts <= cal.last_session


def _cached_calendar(state: dict, build, name: str, day: date):
    """The cached calendar if it covers `day`; otherwise one rebuild around
    `day`. Still not covering it raises CalendarOutOfBounds."""
    cal = state["cal"]
    if cal is not None and _covers(cal, day):
        return cal
    if cal is not None:
        logger.warning(f"{name} calendar does not cover {day}, rebuilding")
    cal = build(day)
    state["cal"] = cal
    _slots_cache.clear()
    if not _covers(cal, day):
        raise CalendarOutOfBounds(f"{name} calendar cannot cover {day}")
    return cal


def market_calendar(day: date):
    """The XNYS calendar covering `day` (3.4 decision 2)."""
    return _cached_calendar(_calendar_state, _build_calendar, CALENDAR_NAME, day)


def futures_calendar(day: date):
    """The CMES calendar covering `day` (3.4b decision 10). It models neither
    the daily halt nor the Friday close, which night_slots_for_day cuts."""
    return _cached_calendar(_futures_calendar_state, _build_futures_calendar,
                            FUTURES_CALENDAR_NAME, day)


def _require_aware(now: Any) -> None:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("scheduler times must be timezone-aware datetimes")


def session_bounds(day: date) -> Optional[tuple[datetime, datetime]]:
    """(open, close) in UTC for an XNYS session day, None otherwise."""
    cal = market_calendar(day)
    ts = pd.Timestamp(day)
    if not cal.is_session(ts):
        return None
    return cal.session_open(ts).to_pydatetime(), cal.session_close(ts).to_pydatetime()


def settle_time(day: date) -> datetime:
    """16:20 ET of an ET date, in UTC."""
    return datetime.combine(day, SETTLE_TIME_ET, tzinfo=ET).astimezone(timezone.utc)


def night_slots_for_day(day: date) -> list[datetime]:
    """The :15 / :45 ET minutes of an ET date when CME equity futures trade and
    no XNYS session is under way (3.4b decision 2). Cut: the 17:00-18:00 ET
    halt, everything from 45 min before an XNYS open through its 16:20 settle.
    A date with no XNYS session has no open to stand clear of."""
    cal = futures_calendar(day)
    bounds = session_bounds(day)
    quiet = (bounds[0] - PRE_OPEN_QUIET, settle_time(day)) if bounds is not None else None
    slots = []
    for hour in range(24):
        for minute in NIGHT_MINUTES:
            et_time = time(hour, minute)
            if HALT_START_ET <= et_time < HALT_END_ET:
                continue
            start = datetime.combine(day, et_time, tzinfo=ET).astimezone(timezone.utc)
            if quiet is not None and quiet[0] < start <= quiet[1]:
                continue
            if cal.is_open_on_minute(pd.Timestamp(start)):
                slots.append(start)
    return slots


def slots_for_day(day: date) -> list[tuple[str, datetime]]:
    """Every slot of an ET date in time order: night slots, the market slots,
    settle (3.4b decision 2). Cached per ET date, bounded at SLOTS_CACHE_MAX."""
    market_calendar(day)      # both calendars are validated before the cache is read, so a
    futures_calendar(day)     # rebuilt or out-of-range calendar is never served from it
    cached = _slots_cache.get(day)
    if cached is not None:
        return cached
    slots: list[tuple[str, datetime]] = [(KIND_NIGHT, t) for t in night_slots_for_day(day)]
    bounds = session_bounds(day)
    if bounds is not None:
        open_, close = bounds
        step = timedelta(minutes=SLOT_MINUTES)
        t = open_
        while t <= close:
            slots.append((KIND_MARKET, t))
            t += step
        slots.append((KIND_SETTLE, settle_time(day)))
    slots.sort(key=lambda slot: slot[1])
    if len(_slots_cache) >= SLOTS_CACHE_MAX:
        _slots_cache.clear()
    _slots_cache[day] = slots
    return slots


def slot_for(now: datetime) -> Optional[tuple[str, datetime]]:
    """(kind, slot start) for the slot whose 5-minute window holds `now`, or
    None. Fails closed: a date the calendar cannot cover gives None + ERROR."""
    _require_aware(now)
    day = now.astimezone(ET).date()
    try:
        slots = slots_for_day(day)
    except CalendarOutOfBounds as e:
        logger.error(f"No health check slot: {e}")
        return None
    window = timedelta(minutes=SLOT_MINUTES)
    for kind, start in slots:
        if start <= now < start + window:
            return kind, start
    return None


def next_slot_after(now: datetime) -> Optional[tuple[str, datetime]]:
    """The first slot starting strictly after `now`, or None (ERROR) when
    the calendar cannot cover the dates ahead."""
    _require_aware(now)
    day = now.astimezone(ET).date()
    try:
        for offset in range(NEXT_SLOT_HORIZON_DAYS):
            for kind, start in slots_for_day(day + timedelta(days=offset)):
                if start > now:
                    return kind, start
    except CalendarOutOfBounds as e:
        logger.error(f"No next health check slot: {e}")
        return None
    logger.error(f"No health check slot within {NEXT_SLOT_HORIZON_DAYS} days of {now.isoformat()}")
    return None


def last_slot_before(now: datetime) -> Optional[tuple[str, datetime]]:
    """The latest slot starting at or before `now` (Part 3.6a decision 5:
    the check that should already have produced a row), or None (ERROR)
    when the calendar cannot cover the dates behind."""
    _require_aware(now)
    day = now.astimezone(ET).date()
    try:
        for offset in range(NEXT_SLOT_HORIZON_DAYS):
            for kind, start in reversed(slots_for_day(day - timedelta(days=offset))):
                if start <= now:
                    return kind, start
    except CalendarOutOfBounds as e:
        logger.error(f"No previous health check slot: {e}")
        return None
    logger.error(f"No health check slot within {NEXT_SLOT_HORIZON_DAYS} days before {now.isoformat()}")
    return None


def previous_close_before(day: date) -> Optional[datetime]:
    """The close (UTC) of the latest XNYS session on an ET date before `day`,
    early closes included (Part 3.6b decision 4), or None (ERROR) when the
    calendar cannot cover the dates behind."""
    try:
        for offset in range(1, NEXT_SLOT_HORIZON_DAYS + 1):
            bounds = session_bounds(day - timedelta(days=offset))
            if bounds is not None:
                return bounds[1]
    except CalendarOutOfBounds as e:
        logger.error(f"No previous XNYS close: {e}")
        return None
    logger.error(f"No XNYS close within {NEXT_SLOT_HORIZON_DAYS} days before {day}")
    return None


# ── The weekend window (Part 3.4c decisions 4 and 5) ─────────────
# A weekend-eve session is the last XNYS session before a gap of ≥ 2
# calendar days with no session: Friday in a normal week, Thursday before
# a Friday holiday, Friday before a Monday holiday — where the exposure is
# longer, not absent. From 30 minutes before its close, every market check
# and that day's settle carries a weekend block.

WEEKEND_GAP_DAYS = 3        # days to the next session: Fri → Mon is 3
WEEKEND_CUTOFF = timedelta(minutes=30)
WEEKEND_KINDS = (KIND_MARKET, KIND_SETTLE)


def next_session_after(day: date) -> Optional[tuple[date, datetime]]:
    """(date, open in UTC) of the first XNYS session after `day`, or None
    (ERROR) when the calendar cannot reach one."""
    try:
        for offset in range(1, NEXT_SLOT_HORIZON_DAYS + 1):
            ahead = day + timedelta(days=offset)
            bounds = session_bounds(ahead)
            if bounds is not None:
                return ahead, bounds[0]
    except CalendarOutOfBounds as e:
        logger.error(f"No next XNYS session after {day}: {e}")
        return None
    logger.error(f"No XNYS session within {NEXT_SLOT_HORIZON_DAYS} days after {day}")
    return None


def weekend_window(day: date) -> Optional[dict]:
    """
    {closeAt, nextOpenAt, gapHours, cutoff} for a weekend-eve session, else
    None. `cutoff` is close − 30 min: the first check that carries a block.
    """
    try:
        bounds = session_bounds(day)
    except CalendarOutOfBounds as e:
        logger.error(f"No weekend window for {day}: {e}")
        return None
    if bounds is None:
        return None
    nxt = next_session_after(day)
    if nxt is None or (nxt[0] - day).days < WEEKEND_GAP_DAYS:
        return None
    close, next_open = bounds[1], nxt[1]
    return {"closeAt": close, "nextOpenAt": next_open,
            "gapHours": round((next_open - close).total_seconds() / 3600, 2),
            "cutoff": close - WEEKEND_CUTOFF}


def weekend_due(kind: str, now: datetime) -> Optional[dict]:
    """The window when this check should carry a block, else None: a market
    or settle check, on a weekend-eve session, at or after the cut-off."""
    _require_aware(now)
    if kind not in WEEKEND_KINDS:
        return None
    window = weekend_window(now.astimezone(ET).date())
    if window is None or now < window["cutoff"]:
        return None
    return window

# ── One check (decision 4) ───────────────────────────────────────

TREND_POINTS = 5     # provisional: ± points against the settle base

# Called as on_check_published(state, reason) after a check publishes (Part 3.6b
# decision 5). None unless the lifespan sets it (MACRO_BRIEF_ENABLED), so the
# scheduler knows nothing of the macro brief. It must not block; a raise is logged.
on_check_published: Optional[Callable[[Any, str], None]] = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def trend_from(score: Optional[int], settle_score: Optional[int]) -> Optional[str]:
    if score is None or settle_score is None:
        return None
    delta = score - settle_score
    if delta >= TREND_POINTS:
        return "improving"
    if delta <= -TREND_POINTS:
        return "declining"
    return "stable"


def settle_cutoff(now: datetime, kind: str = KIND_MARKET) -> datetime:
    """The trend base and the overlay reference must be older than this: the
    XNYS open of `now`'s ET date, so the 16:20 settle check reads an earlier
    session's settle and never its own. ET midnight on a date that is not a
    session. A night check reads the latest settle there is — that day's, once
    it exists — so its cutoff is `now` (3.4b decision 6)."""
    _require_aware(now)
    if kind == KIND_NIGHT:
        return now
    day = now.astimezone(ET).date()
    try:
        bounds = session_bounds(day)
    except CalendarOutOfBounds:
        bounds = None
    if bounds is not None:
        return bounds[0]
    return datetime.combine(day, time(0), tzinfo=ET).astimezone(timezone.utc)


def _failure(step: str, e: Exception, errors: list[str]) -> None:
    errors.append(f"{step}: {type(e).__name__}")
    if isinstance(e, db.DB_FAILURES):
        logger.warning(f"Health check {step} failed: {e!r}")
    else:
        logger.error(f"Health check {step} raised {type(e).__name__}: {e}")


def latest_settle_time(now: datetime) -> Optional[datetime]:
    """The 16:20 ET settle of the most recent XNYS session at or before `now`:
    the row a night check expects its reference to be (3.4b decision 6)."""
    day = now.astimezone(ET).date()
    try:
        for offset in range(NEXT_SLOT_HORIZON_DAYS):
            behind = day - timedelta(days=offset)
            if session_bounds(behind) is not None and settle_time(behind) <= now:
                return settle_time(behind)
    except CalendarOutOfBounds as e:
        logger.error(f"No settle time before {now.isoformat()}: {e}")
    return None


async def run_check(state, kind: str, *, clock: Callable[[], datetime] = _utc_now) -> Optional[dict]:
    """
    One health check on `state` (redis, db_pool, cooldowns, check_status):
    compute → settle base → publish → insert. Skips (None) while a quotes
    download holds the lock. A dependency failure in steps 2–4 is WARNING, a
    bug ERROR, and the next step still runs; compute itself never raises for
    a source state (3.3), so a raise there is a bug for the loop to log.
    """
    if kind == KIND_NIGHT:
        return await run_night_check(state, clock=clock)
    if quotes.download_in_flight():
        logger.warning(f"Health check ({kind}) skipped: a core quotes download holds the lock")
        return None

    r = getattr(state, "redis", None)
    pool = getattr(state, "db_pool", None)
    errors: list[str] = []

    health = await compute_health(r, state.cooldowns, now=clock)
    checked_at = datetime.fromisoformat(health["checkedAt"])

    settle = None
    if pool is not None:
        try:
            settle = await db.settle_reference(pool, settle_cutoff(checked_at, kind))
        except Exception as e:
            _failure("settle base read", e, errors)

    # The futures cap (3.4b decisions 4 and 5), on the prices this check already
    # downloaded. The settle check is exempt: it is the reference, and its
    # monitors read the day's complete bars.
    health["overlay"] = None
    if kind != KIND_SETTLE:
        health["score"], health["overlay"] = overlay.apply_overlay(
            health["score"], (settle or {}).get("futures"), health.get("futures") or {})
        health["regime"] = classify(health["score"])

    trend = trend_from(health["score"], settle["score"] if settle else None)

    # The weekend block (3.4c). Off-window this is None and nothing else
    # in the check changes; a raise inside it is a bug, never the check's.
    health["weekend"] = await weekend_block(state, health, kind, checked_at)

    published = None
    # The first check after a host pause carries it, published or not (3.4 follow-up addition 1).
    paused = getattr(state, "pending_paused_seconds", None)
    state.pending_paused_seconds = None
    try:
        published = await publish_health(r, health, trend, now=checked_at,
                                         news=news_poller.stale_view(state, checked_at),
                                         paused_seconds=paused, kind=kind)
    except Exception as e:
        _failure("publish", e, errors)

    hook = on_check_published
    if hook is not None and published and published["published"]:
        try:
            hook(state, published["reason"])
        except Exception as e:
            _failure("publish hook", e, errors)

    if pool is None:
        logger.warning(f"Health check ({kind}) not recorded: database unavailable")
    else:
        try:
            await db.insert_health_check(pool, health, kind, trend, settle, paused_seconds=paused)
        except Exception as e:
            _failure("insert", e, errors)

    state.check_status.update(
        lastCheckAt=health["checkedAt"],
        lastKind=kind,
        lastScore=health["score"],
        lastError="; ".join(errors) or None,
        **weekend_status(health.get("weekend"), getattr(state, "weekend_dropped", None)),
    )
    logger.info(
        f"Health check ({kind}): {health['regime']} {health['score']}, trend {trend}, "
        f"published {bool(published and published['published'])}"
    )
    return {"health": health, "trend": trend, "settle": settle, "published": published, "errors": errors}


# ── The weekend block (Part 3.4c) ────────────────────────────────

def _base_score_as_of(health: dict) -> Optional[str]:
    """The ET date of the complete bars the five complete-bar monitors read
    — spy_trend's own bar date. `vix` is live and says so separately."""
    raw = ((health.get("monitors") or {}).get("spy_trend") or {}).get("raw") or {}
    date_ = raw.get("date")
    return date_ if isinstance(date_, str) else None


def weekend_status(block: Optional[dict], dropped: Optional[str]) -> dict:
    """/health's three weekend keys (W5), from the block this check built."""
    return {"weekendLevel": block["level"] if block else None,
            "weekendReasonCount": len(block["reasons"]) if block else None,
            "weekendDropped": dropped}


async def weekend_block(state, health: dict, kind: str, now: datetime) -> Optional[dict]:
    """
    The `weekend` block for this check, or None off-window. Nothing here can
    fail the check: the assembly degrades every section to a status (F1-F5),
    a non-finite number drops the block before it can reach a publish (F12),
    and a raise is caught and logged as the bug it would be (F8).
    """
    try:
        window = weekend_due(kind, now)
        if window is None:
            return None
        inputs = await weekend_inputs.assemble(
            state, getattr(state, "inputs_http", None), now=now,
            close_at=window["closeAt"], next_open_at=window["nextOpenAt"])
        overlay_record = health.get("overlay") or {}
        block = weekend.assess(
            capped_score=health.get("score"),
            base_score=overlay_record.get("base", health.get("score")),
            regime=health.get("regime"),
            vix=health.get("vix5d"),
            window={"gapHours": window["gapHours"],
                    "closeAt": window["closeAt"].isoformat(),
                    "nextOpenAt": window["nextOpenAt"].isoformat(),
                    "baseScoreAsOf": _base_score_as_of(health)},
            assessed_at=health.get("checkedAt") or now.isoformat(),
            **inputs)
        block, dropped = weekend.drop_if_nonfinite(block)
        state.weekend_dropped = dropped
        if block is not None:
            logger.info(f"Weekend exposure {block['level']} ({block['points']} pts): "
                        f"{', '.join(r['code'] for r in block['reasons']) or 'no reasons'}")
        return block
    except Exception as e:
        logger.error(f"Weekend block raised {type(e).__name__}: {e}")
        return None


# ── One night check (3.4b decision 6) ────────────────────────────

async def run_night_check(state, *, clock: Callable[[], datetime] = _utc_now) -> Optional[dict]:
    """
    One night check: the latest settle's score, capped by how far the futures
    have moved since that settle. It never re-runs the monitors — they read
    complete bars, which have not changed — and it never spends a request it
    cannot use: no pool, no scored settle, or a settle with no stored futures
    means no download at all. Publish and insert follow 3.4's rules unchanged.
    """
    if quotes.download_in_flight():
        logger.warning("Night check skipped: a core quotes download holds the lock")
        return None
    pool = getattr(state, "db_pool", None)
    if pool is None:
        logger.warning("Night check skipped: database unavailable, nothing to store it in")
        return None

    now = clock()
    try:
        settle = await db.settle_reference(pool, settle_cutoff(now, KIND_NIGHT))
    except Exception as e:
        logger.warning(f"Night check skipped: the settle read failed ({type(e).__name__})")
        return None
    if settle is None:
        logger.warning("Night check skipped: no scored settle to overlay")
        return None

    r = getattr(state, "redis", None)
    errors: list[str] = []
    reference = settle["futures"]
    view = {"asOf": None, "source": "none", "reason": "no_reference",
            "tickers": {}, "staleTickers": list(quotes.NIGHT_TICKERS)}
    if any(reference.values()):          # nothing to compare against → no request (G6)
        view = await quotes.get_night_view(r, state.cooldowns, now=lambda: now)

    score, record = overlay.apply_overlay(settle["score"], reference, overlay.futures_prices(view))
    expected = latest_settle_time(now)
    missed_settle = expected is not None and settle["checkedAt"] < expected
    indicators = settle["indicators"]
    health = {
        "score": score,
        "regime": classify(score),
        "coverage": indicators.get("coverage"),
        # The score is the settle's, so it is stale whenever the cap could not
        # be measured or the settle it copies is not the latest session's.
        "stale": bool(indicators.get("stale")) or missed_settle
        or record["status"] in (overlay.STATUS_NO_REFERENCE, overlay.STATUS_UNAVAILABLE),
        "staleMonitors": indicators.get("staleMonitors") or [],
        "monitors": indicators.get("monitors") or {},
        "checkedAt": now.isoformat(),
        "inputs": {key: view[key] for key in ("asOf", "source", "reason", "staleTickers")},
        "futures": overlay.futures_prices(view),
        "overlay": record,
    }
    trend = trend_from(score, settle["score"])

    paused = getattr(state, "pending_paused_seconds", None)
    state.pending_paused_seconds = None
    published = None
    try:
        published = await publish_health(r, health, trend, now=now,
                                         news=news_poller.stale_view(state, now),
                                         paused_seconds=paused, kind=KIND_NIGHT)
    except Exception as e:
        _failure("publish", e, errors)

    hook = on_check_published
    if hook is not None and published and published["published"]:
        try:
            hook(state, published["reason"])
        except Exception as e:
            _failure("publish hook", e, errors)

    try:
        await db.insert_health_check(pool, health, KIND_NIGHT, trend, settle, paused_seconds=paused)
    except Exception as e:
        _failure("insert", e, errors)

    # A night check never carries a block (3.4c decision 5's ruling): it runs
    # no monitors and has no quotes view, so it has no live input to read.
    state.check_status.update(lastCheckAt=health["checkedAt"], lastKind=KIND_NIGHT,
                              lastScore=score, lastError="; ".join(errors) or None,
                              **weekend_status(None, None))
    logger.info(
        f"Night check: {health['regime']} {score} (settle {settle['score']}, "
        f"{record['status']} {record['movePct'] if record['movePct'] is None else round(record['movePct'], 2)}%), "
        f"published {bool(published and published['published'])}"
    )
    return {"health": health, "trend": trend, "settle": settle, "published": published, "errors": errors}


# ── The loop (decision 3) ────────────────────────────────────────

FALLBACK_SLEEP_SECONDS = 300    # when no next slot can be computed
MAX_MISSED_SCAN = 2000          # bounds the missed-slot count after a long sleep


def _missed_between(last_start: Optional[datetime], due_start: datetime) -> int:
    """Slots strictly between the last slot handled and `due_start`."""
    if last_start is None:
        return 0
    missed, cursor = 0, last_start
    for _ in range(MAX_MISSED_SCAN):
        nxt = next_slot_after(cursor)
        if nxt is None or nxt[1] >= due_start:
            break
        missed += 1
        cursor = nxt[1]
    return missed


async def _guarded_check(state, kind: str, clock: Callable[[], datetime]) -> None:
    """A raise out of a check is a bug: logged, and the loop goes on."""
    try:
        await run_check(state, kind, clock=clock)
    except Exception as e:
        logger.error(f"Health check ({kind}) raised {type(e).__name__}: {e}")
        status = getattr(state, "check_status", None)
        if isinstance(status, dict):
            status["lastError"] = type(e).__name__


async def run_scheduler(state, *, clock: Callable[[], datetime] = _utc_now, sleep=asyncio.sleep) -> None:
    """
    Forever: wait for the next slot through wallclock (sleeps of ≤ 60 s, the
    clock re-read after each; 3.4 follow-up), then run the slot if it is due
    and at most GRACE_SECONDS late. Checks are awaited in sequence, so two
    never overlap. A late slot (a check overran it, the laptop slept, a
    restart) is skipped with a WARNING naming how many were missed, never
    caught up; so are the slots a wake between slots passed. A loop that has
    handled no slot yet reports only the one it lands in (the restart rule).
    After a check the clock is re-read before waiting, so a check that ran
    into the next slot's grace window still runs it. A host pause the harness
    saw is kept for the next check's payload (addition 1). Cancellation
    (shutdown) propagates.
    """
    logger.info("Regime scheduler running")
    last_start: Optional[datetime] = None
    while True:
        try:
            now = clock()
            due = slot_for(now)
            if due is not None and due[1] != last_start:
                kind, start = due
                late = (now - start).total_seconds()
                missed = _missed_between(last_start, start) + (1 if late > GRACE_SECONDS else 0)
                last_start = start
                if missed:
                    logger.warning(
                        f"Missed {missed} health check slot(s) up to {start.isoformat()} "
                        f"(woke {late:.0f}s after that slot)"
                    )
                if late <= GRACE_SECONDS:
                    await _guarded_check(state, kind, clock)
                    continue
            elif last_start is not None:
                passed = last_slot_before(now)
                if passed is not None and passed[1] > last_start:
                    start = passed[1]
                    logger.warning(
                        f"Missed {_missed_between(last_start, start) + 1} health check slot(s) up to "
                        f"{start.isoformat()} (woke {(now - start).total_seconds():.0f}s after that slot)"
                    )
                    last_start = start
            nxt = next_slot_after(clock())
            target = nxt[1] if nxt is not None else clock() + timedelta(seconds=FALLBACK_SLEEP_SECONDS)
        except Exception as e:
            logger.error(f"Scheduler loop error {type(e).__name__}: {e}")
            target = clock() + timedelta(seconds=FALLBACK_SLEEP_SECONDS)
        wake = await wallclock.sleep_until(target, clock=clock, sleep=sleep, log=logger)
        if wake.paused_seconds:
            state.pending_paused_seconds = (getattr(state, "pending_paused_seconds", None) or 0) + wake.paused_seconds
