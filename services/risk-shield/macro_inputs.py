"""
TradingFirm — the macro brief inputs (Part 3.6a, spec decisions 5–6).

assemble_inputs() gathers what the brief reads into one bounded document:
health, settle, news, calendar and FRED, each with its own status, plus a
`freshness` block. 3.6b stores the document unchanged as
risk.macro_briefs.inputs and sends it to ai-agent; GET /macro/brief/inputs
serves it so the inputs can be checked without an LLM.

Health comes from risk.health_checks rows only: this module never computes a
check or downloads quotes.

Each section has a *_freshness() fragment; the fragments merge into
`freshness`, so a test on one section asserts exactly what the brief sees.
"""

import asyncio
import json
import logging
import math
from datetime import datetime, timedelta
from typing import Any, Optional

import httpx

import db
import econ_calendar
import news_poller
import scheduler
from config import settings
from monitors.fred import get_fred_view

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# ── Health + settle (decision 5) ─────────────────────────────────

# One slot of grace: the check the scheduler should already have recorded is
# the latest slot starting at or before now − this. A check takes ~5 s cold.
SLOT_GRACE_SECONDS = 300

HEALTH_STATUSES = ("ok", "no_checks", "unavailable")


def _indicators(row: dict) -> dict:
    """The row's indicators JSONB as a dict; {} when it is not an object
    (asyncpg returns jsonb as text without a codec)."""
    value = row.get("indicators")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _monitors(value: Any) -> Optional[dict]:
    """{name: {score, weight, stale, detail}}, without the raw series; None
    when the stored shape is wrong."""
    if not isinstance(value, dict) or not all(isinstance(m, dict) for m in value.values()):
        return None
    return {
        name: {"score": m.get("score"), "weight": m.get("weight"),
               "stale": m.get("stale"), "detail": m.get("detail")}
        for name, m in value.items()
    }


def _settle_part(settle: Optional[dict]) -> dict:
    if settle is None:
        return {"present": False, "checkedAt": None, "score": None}
    return {"present": True, "checkedAt": settle["checkedAt"].isoformat(), "score": settle["score"]}


async def health_section(pool, now: datetime) -> tuple[dict, dict]:
    """
    (health, settle). Health is the latest risk.health_checks row; when its
    score is null, lastScored is the latest scored row (as /market/health).
    Settle is the latest scored settle before `now`. No pool or a database
    failure is status "unavailable"; no row at all is "no_checks". Never raises
    for a dependency state.
    """
    expected = scheduler.last_slot_before(now - timedelta(seconds=SLOT_GRACE_SECONDS))
    expected_at = expected[1].isoformat() if expected is not None else None

    if pool is None:
        return {"status": "unavailable", "lastExpectedSlotAt": expected_at}, _settle_part(None)
    try:
        row = await db.latest_health_check(pool)
        settle = await db.settle_base(pool, now)
        scored = None
        if row is not None and row["score"] is None:
            scored = await db.latest_scored_health_check(pool)
    except db.DB_FAILURES as e:
        logger.warning(f"Macro inputs: health read failed ({type(e).__name__})")
        return {"status": "unavailable", "lastExpectedSlotAt": expected_at}, _settle_part(None)

    if row is None:
        return {"status": "no_checks", "lastExpectedSlotAt": expected_at}, _settle_part(settle)

    ind = _indicators(row)
    checked_at = row["checked_at"]
    health = {
        "status": "ok",
        "checkedAt": checked_at.isoformat(),
        "kind": ind.get("kind"),
        "score": row["score"],
        "regime": row["regime"],
        "trend": row["trend"],
        "coverage": ind.get("coverage"),
        "stale": ind.get("stale"),
        "staleMonitors": ind.get("staleMonitors"),
        "monitors": _monitors(ind.get("monitors")),
        # Part 3.4b: the futures cap this check applied, so the brief can say why
        # a night score sits below what the monitors alone would give. Null on a
        # settle row and on every row written before 3.4b.
        "overlay": ind.get("overlay"),
        "ageMinutes": int((now - checked_at).total_seconds() // 60),
        "lastExpectedSlotAt": expected_at,
    }
    if row["score"] is None:
        health["lastScored"] = (
            {"score": scored["score"], "regime": scored["regime"],
             "checkedAt": scored["checked_at"].isoformat()}
            if scored is not None else None
        )
    return health, _settle_part(settle)


def health_ready(health: dict) -> bool:
    """3.6b's precondition to spend an LLM call: the database answered and a
    scored row exists (the latest, or lastScored)."""
    if health.get("status") != "ok":
        return False
    return health.get("score") is not None or health.get("lastScored") is not None


def health_freshness(health: dict) -> dict:
    """The health fragment of `freshness`. healthStale: no usable row, or the
    row is older than the slot that should already have produced one. The age
    is carried as-is, so a fresh 07:30 verdict still shows ~670 minutes."""
    ok = health.get("status") == "ok"
    expected = health.get("lastExpectedSlotAt")
    behind = not ok or (
        expected is not None
        and datetime.fromisoformat(health["checkedAt"]) < datetime.fromisoformat(expected)
    )
    return {
        "healthStatus": health.get("status"),
        "healthStale": behind,
        "healthMonitorsStale": bool(health.get("stale")) if ok else False,
        "healthAgeMinutes": health.get("ageMinutes"),
    }


# ── News (decision 5) ────────────────────────────────────────────

# The window (3.6b decision 4) is news_hours(), clamped to these and kept
# beside the data-engine bound it must stay under.
NEWS_HOURS_MIN = 24
NEWS_HOURS_MAX = 96
NEWS_LIMIT = 50
# data-engine's GET /news/market bounds (spec 3.6a decision 2), a pinned copy:
# the two services share no package. data-engine pins its side in
# test_news_market_bounds_pinned_for_risk_shield. A request outside them is a
# 422 on every brief.
DATA_ENGINE_NEWS_MAX_HOURS = 168
DATA_ENGINE_NEWS_MAX_LIMIT = 100
NEWS_TIMEOUT = 10.0      # hard bound per request; read at call time (tests patch it)
NEWS_TEXT_MAX = 300      # title and summary, per item
NEWS_STATUSES = ("ok", "empty", "unavailable")

# A bad body is our own route drifting: ERROR once per distinct problem, then
# DEBUG, so a brief every few hours does not bury the first report.
_bad_body_logged: set[str] = set()


def news_hours(now: datetime) -> int:
    """Hours since the latest XNYS close before now's ET date, rounded up and
    clamped to NEWS_HOURS_MIN…NEWS_HOURS_MAX, so Monday's 07:30 brief reaches
    back to Friday's close (3.6b decision 4). No close in reach gives the
    minimum (scheduler has logged the ERROR)."""
    close = scheduler.previous_close_before(now.astimezone(scheduler.ET).date())
    if close is None:
        return NEWS_HOURS_MIN
    return max(NEWS_HOURS_MIN, min(NEWS_HOURS_MAX, math.ceil((now - close).total_seconds() / 3600)))


def _news(status: str, hours: int, cause: Optional[str] = None, items: Optional[list] = None,
          truncated: int = 0) -> dict:
    items = items or []
    return {"status": status, "cause": cause, "hours": hours, "limit": NEWS_LIMIT,
            "count": len(items), "truncated": truncated, "trimmedForSize": 0, "items": items}


def _bad_body(hours: int, problem: str) -> dict:
    if problem not in _bad_body_logged:
        _bad_body_logged.add(problem)
        logger.error(f"Macro inputs: data-engine /news/market answered a bad body ({problem})")
    else:
        logger.debug(f"Macro inputs: data-engine /news/market bad body again ({problem})")
    return _news("unavailable", hours, "bad body")


async def news_section(http, hours: int) -> dict:
    """
    One GET {data_engine_url}/news/market?hours=<hours>&limit=50, no retry. 200 with
    a list is "ok" ("empty" for []); a non-200, a transport error or a timeout
    is "unavailable" with the cause (WARNING); a body that is not a list of
    items is "unavailable" / "bad body" (ERROR once per problem). Items keep
    publishedAt, source, title and summary (≤ 300 chars each, counted), newest
    first; the url stays in data-engine.
    """
    url = f"{settings.data_engine_url.rstrip('/')}/news/market"
    params = {"hours": hours, "limit": NEWS_LIMIT}
    try:
        resp = await asyncio.wait_for(http.get(url, params=params), timeout=NEWS_TIMEOUT)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        # First: asyncio.TimeoutError is TimeoutError, an OSError.
        logger.warning("Macro inputs: data-engine /news/market timed out")
        return _news("unavailable", hours, "timeout")
    except (httpx.HTTPError, OSError) as e:
        logger.warning(f"Macro inputs: data-engine /news/market unreachable ({type(e).__name__})")
        return _news("unavailable", hours, type(e).__name__)
    if resp.status_code != 200:
        logger.warning(f"Macro inputs: data-engine /news/market answered HTTP {resp.status_code}")
        return _news("unavailable", hours, f"HTTP {resp.status_code}")

    try:
        body = resp.json()
    except ValueError:
        return _bad_body(hours, "not JSON")
    if not isinstance(body, list):
        return _bad_body(hours, "not a list")

    items, truncated = [], 0
    for raw in body:
        if not isinstance(raw, dict):
            return _bad_body(hours, "item not an object")
        missing = [k for k in ("publishedAt", "source", "title", "summary") if k not in raw]
        if missing:
            return _bad_body(hours, f"item missing {missing[0]}")
        title, summary, source = raw["title"], raw["summary"] or "", raw["source"]
        if not isinstance(raw["publishedAt"], str) or not isinstance(title, str) or not isinstance(summary, str):
            return _bad_body(hours, "item field not a string")
        if len(title) > NEWS_TEXT_MAX or len(summary) > NEWS_TEXT_MAX:
            truncated += 1
        items.append({"publishedAt": raw["publishedAt"],
                      "source": source if isinstance(source, str) else None,
                      "title": title[:NEWS_TEXT_MAX], "summary": summary[:NEWS_TEXT_MAX]})
    return _news("ok" if items else "empty", hours, items=items, truncated=truncated)


def news_freshness(news: dict, state, now: datetime) -> dict:
    """The news fragment of `freshness`: the section's status plus the news
    poller's three keys, unchanged from news_poller.stale_view (null = the
    poller is off in this process, never "unknown")."""
    return {"newsStatus": news.get("status"), **news_poller.stale_view(state, now)}


# ── Calendar and FRED (decision 5) ───────────────────────────────

CALENDAR_DAYS = 7


def calendar_section(now: datetime) -> dict:
    """econ_calendar.window for the next 7 ET days plus renewalDue (the
    14-day renewal flag). An unavailable file is status "unavailable"
    (load() already logged it)."""
    try:
        calendar = econ_calendar.load()
    except econ_calendar.CalendarUnavailable:
        return {"status": "unavailable", "from": None, "to": None, "coversThrough": None,
                "coverageShort": None, "renewalDue": None, "events": []}
    return {"status": "ok", **econ_calendar.window(calendar, now, CALENDAR_DAYS),
            "renewalDue": econ_calendar.coverage_short(calendar, econ_calendar.et_today(now))}


def calendar_freshness(calendar: dict) -> dict:
    return {"calendarStatus": calendar["status"],
            "calendarWindowShort": bool(calendar["coverageShort"]),
            "calendarRenewalDue": calendar["renewalDue"]}


def fred_freshness(fred: dict) -> dict:
    return {"fredStaleSeries": [sid for sid, entry in fred.items() if entry["stale"]]}


# ── The document (decisions 5–6) ─────────────────────────────────

INPUTS_MAX_BYTES = 64_000     # compact UTF-8 JSON; read at call time (tests patch it)


class InputsTooLarge(RuntimeError):
    """Still over INPUTS_MAX_BYTES with no news left: a bug, never a data state."""


def any_stale(freshness: dict) -> bool:
    """True when any input the brief reads is missing, late or empty.
    calendarRenewalDue is maintenance (the inputs are still right) and a null
    newsPollStale is no evidence, so neither counts."""
    return bool(
        freshness["healthStatus"] != "ok" or freshness["healthStale"] or freshness["healthMonitorsStale"]
        or not freshness["settlePresent"]
        or freshness["newsStatus"] != "ok" or freshness["newsPollStale"] is True
        or freshness["calendarStatus"] != "ok" or freshness["calendarWindowShort"]
        or freshness["fredStaleSeries"]
    )


def encoded_size(doc: dict) -> int:
    """Compact JSON bytes. allow_nan=False: a NaN raises ValueError here."""
    return len(json.dumps(doc, allow_nan=False, separators=(",", ":")).encode("utf-8"))


def bound(doc: dict) -> dict:
    """Drop the oldest news items (the list is newest first) until the
    document fits INPUTS_MAX_BYTES, counting them in news.trimmedForSize."""
    news = doc["news"]
    while encoded_size(doc) > INPUTS_MAX_BYTES:
        if not news["items"]:
            raise InputsTooLarge(f"macro inputs over {INPUTS_MAX_BYTES} bytes with no news left")
        news["items"].pop()
        news["trimmedForSize"] += 1
        news["count"] = len(news["items"])
    if news["trimmedForSize"]:
        logger.warning(f"Macro inputs: {news['trimmedForSize']} oldest news item(s) dropped for size")
    return doc


async def assemble_inputs(state, fred_client, http, *, now: datetime) -> dict:
    """
    The inputs document on `state` (db_pool, redis, cooldowns, news_status).
    Sections fail on their own and say why; only a bug raises. Reads only:
    Postgres, data-engine, the calendar file and FRED through its cache.
    """
    health, settle = await health_section(getattr(state, "db_pool", None), now)
    news = await news_section(http, news_hours(now))
    calendar = calendar_section(now)
    fred = await get_fred_view(getattr(state, "redis", None), getattr(state, "cooldowns", None),
                               fred_client, now=lambda: now)
    freshness = {
        **health_freshness(health),
        "settlePresent": settle["present"],
        **news_freshness(news, state, now),
        **calendar_freshness(calendar),
        **fred_freshness(fred),
    }
    freshness["anyStale"] = any_stale(freshness)
    return bound({
        "schemaVersion": SCHEMA_VERSION,
        "assembledAt": now.isoformat(),
        "ready": health_ready(health),
        "health": health,
        "settle": settle,
        "news": news,
        "calendar": calendar,
        "fred": fred,
        "freshness": freshness,
    })
