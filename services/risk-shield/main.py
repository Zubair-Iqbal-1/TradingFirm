"""
TradingFirm — Risk Shield (Service 3)

Responsibilities:
  - Market health scoring (VIX, breadth, sector rotation, etc.)
  - Regime detection (HEALTHY / CAUTIOUS / DANGER / CRITICAL)
  - Crash guard alerts
  - Health check scheduling (every 5 min during market hours)

Endpoints:
  GET /health             — service health: dependency state at boot, scheduler state
  GET /                   — service info
  GET /market/health      — the latest health check (Postgres only, Part 3.4),
                            with its weekend-exposure block (Part 3.4c)
  GET /market/indicators  — the six monitors of the latest check
  GET /market/history     — checks over the last ?days=1..90 (default 30)
  GET /market/calendar    — FOMC / CPI / jobs dates for ?days=1..31 (default 7),
                            from data/econ_calendar.json only (Part 3.5)
  GET /macro/brief/inputs — the macro brief's inputs document with freshness
                            flags, reused for 60 s (Part 3.6a)
  GET /macro/brief        — the latest stored macro brief, Postgres only (Part 3.6b)
  POST /macro/brief/generate — one manual brief through ai-agent; 503 while
                            MACRO_BRIEF_ENABLED is false (Part 3.6b)
  PUT /market/weekend/situation    — set the active-situation flag the weekend
  DELETE /market/weekend/situation   block reads. The service's only write
                            routes: X-TF-Token, 503 without a secret (Part 3.4c)

Port: 8003
"""

import asyncio
import hmac
import json
import logging
import math
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import cache
import config
import db
import econ_calendar
import macro_brief
import macro_inputs
import news_poller
import scheduler
import weekend_inputs
from config import settings
from scoring import weekend

# ── Logging ──────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s | %(name)-20s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("risk-shield")


# ── Lifespan (startup/shutdown) ──────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Initialize Redis and the DB pool on startup, close them on shutdown.

    Fail-open and bounded (Part 3.1 decision 6): each attempt gets
    config.STARTUP_TIMEOUT seconds, a failure or a timeout logs a warning
    and leaves the dependency as None, and the service still boots and
    serves /health. Worst-case boot is two timeouts, ~10 s.

    config.STARTUP_TIMEOUT is read here at call time, never bound at
    import: the lifespan tests monkeypatch it down to keep the slow paths
    fast, and a from-import would copy the value past the patch.
    """
    logger.info("Starting Risk Shield...")

    # Redis (optional). The factory and its PING are bounded together: a
    # Redis that accepts the socket and then never answers is as bad as one
    # that never accepts it.
    try:
        from cache import create_redis
        app.state.redis = await asyncio.wait_for(
            create_redis(), timeout=config.STARTUP_TIMEOUT
        )
        logger.info("✅ Redis connection ready")
    except Exception as e:
        logger.warning(f"⚠️  Redis unavailable (cache disabled): {e!r}")
        app.state.redis = None

    # Database (optional).
    try:
        from db import create_db_pool
        app.state.db_pool = await asyncio.wait_for(
            create_db_pool(), timeout=config.STARTUP_TIMEOUT
        )
        logger.info("✅ Database pool ready")
    except Exception as e:
        logger.warning(f"⚠️  Database unavailable (health checks not persisted): {e!r}")
        app.state.db_pool = None

    # Regime scheduler (Part 3.4 decision 8). The cooldown clock and the
    # status dict exist either way (small, and /health reads the status);
    # the task only when SCHEDULER_ENABLED is true. It starts even with both
    # dependencies down: checks then run without cache, publish or rows.
    from cache import MemoryCooldowns
    app.state.cooldowns = MemoryCooldowns()
    app.state.check_status = {"lastCheckAt": None, "lastKind": None, "lastScore": None, "lastError": None}
    app.state.scheduler_task = None
    if settings.scheduler_enabled:
        app.state.scheduler_task = asyncio.create_task(scheduler.run_scheduler(app.state))
        logger.info("✅ Regime scheduler started")
    else:
        logger.info("Regime scheduler disabled (SCHEDULER_ENABLED is not true)")

    # Market news poller (Part 3.5). news_status exists either way (small;
    # /health and /market/health read it); the task and its two HTTP clients
    # only when NEWS_POLL_ENABLED is true. Like the scheduler, it starts with
    # dependencies down: polls then skip or fail and say so in lastError.
    import news_poller
    app.state.news_status = news_poller.initial_news_status()
    app.state.news_task = None
    app.state.news_clients = ()
    if settings.news_poll_enabled:
        import httpx
        from monitors.finnhub_client import FinnhubClient
        finnhub = FinnhubClient(settings.finnhub_api_key.get_secret_value())
        ingest_http = httpx.AsyncClient(timeout=news_poller.INGEST_TIMEOUT)
        app.state.news_clients = (finnhub, ingest_http)
        app.state.news_task = asyncio.create_task(
            news_poller.run_news_poller(app.state, finnhub, ingest_http)
        )
        logger.info(f"✅ Market news poller started (Finnhub configured: {finnhub.configured})")
    else:
        logger.info("Market news poller disabled (NEWS_POLL_ENABLED is not true)")

    # Macro brief inputs (Part 3.6a decision 7). Built whatever the flags say:
    # neither client makes a request at construction, and GET
    # /macro/brief/inputs serves with the brief off. The lock is created here,
    # on the serving loop (a lock is per event loop, Part 3.2).
    import httpx
    from monitors.fred_client import FredClient
    app.state.fred_client = FredClient(settings.fred_api_key.get_secret_value())
    app.state.inputs_http = httpx.AsyncClient(timeout=macro_inputs.NEWS_TIMEOUT, follow_redirects=False)
    app.state.inputs_lock = asyncio.Lock()
    app.state.inputs_last = None

    # Macro brief generation (Part 3.6b decision 7). brief_status and the lock
    # always exist (small; /health and POST /macro/brief/generate read them),
    # the lock on the serving loop. The ai-agent client only when
    # MACRO_BRIEF_ENABLED is true; it makes no request at construction.
    from ai_agent_client import AiAgentClient
    app.state.brief_status = macro_brief.initial_brief_status()
    app.state.brief_lock = asyncio.Lock()
    app.state.ai_agent_client = AiAgentClient(settings.ai_agent_url) if settings.macro_brief_enabled else None
    app.state.brief_task = app.state.brief_queue = None
    if settings.macro_brief_enabled:
        # The regime trigger (decision 5): run_check's publish hook feeds a size-1 queue the loop waits on.
        app.state.brief_queue = asyncio.Queue(maxsize=1)
        scheduler.on_check_published = macro_brief.request_brief
        app.state.brief_task = asyncio.create_task(macro_brief.run_brief_loop(app.state, app.state.ai_agent_client))
        logger.info("✅ Macro brief loop started")
    else:
        logger.info("Macro brief generation disabled (MACRO_BRIEF_ENABLED is not true)")

    logger.info(f"Risk Shield ready on port {settings.service_port}")
    yield

    # Shutdown
    logger.info("Shutting down Risk Shield...")
    scheduler.on_check_published = None       # no brief request into a loop that is stopping (3.6b)
    # The scheduler and the news poller stop before the pool and Redis close,
    # so neither runs on a closed connection. One bounded wait for both.
    # asyncio.wait, not wait_for: wait_for would block on a task that does not
    # honour the cancel.
    running = {
        name: task
        for name, task in (("Regime scheduler", getattr(app.state, "scheduler_task", None)),
                           ("News poller", getattr(app.state, "news_task", None)),
                           ("Macro brief", getattr(app.state, "brief_task", None)))
        if task is not None
    }
    for task in running.values():
        task.cancel()
    if running:
        done, _ = await asyncio.wait(set(running.values()), timeout=config.SCHEDULER_SHUTDOWN_TIMEOUT)
        for name, task in running.items():
            if task not in done:
                logger.warning(
                    f"{name} did not stop within {config.SCHEDULER_SHUTDOWN_TIMEOUT}s; "
                    "closing connections anyway"
                )
            elif not task.cancelled() and task.exception() is not None:
                logger.warning(f"{name} ended with {task.exception()!r}")
            else:
                logger.info(f"{name} stopped")
    for client in getattr(app.state, "news_clients", ()) or ():
        try:
            await client.aclose()
        except Exception as e:
            logger.warning(f"News poller client close failed: {e!r}")
    # The macro brief's clients: after the tasks, before the pool and Redis (3.6b decision 7).
    for name in ("fred_client", "inputs_http", "ai_agent_client"):
        client = getattr(app.state, name, None)
        if client is None:
            continue
        try:
            await client.aclose()
        except Exception as e:
            logger.warning(f"Macro brief client close failed ({name}): {e!r}")
    if getattr(app.state, "db_pool", None) is not None:
        await app.state.db_pool.close()
        logger.info("Database pool closed")
    if getattr(app.state, "redis", None) is not None:
        await app.state.redis.close()
        logger.info("Redis connection closed")


# ── App ──────────────────────────────────────────────────────────

app = FastAPI(
    title="TradingFirm — Risk Shield",
    description="Market health monitoring, regime detection, and crash guard",
    version="0.2.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    """
    Health check endpoint.

    `db_connected` / `redis_connected` report the outcome of the startup
    attempt, not a live probe — the same contract data-engine's /health
    has. A dependency that dies after boot still reads true until the
    service restarts (Part 3.1 decision 7, deferred).
    """
    return {
        "service": settings.service_name,
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "0.2.0",
        "db_connected": getattr(app.state, "db_pool", None) is not None,
        "redis_connected": getattr(app.state, "redis", None) is not None,
        "fredConfigured": settings.fred_configured,
        # Part 3.4: whether this process schedules checks, and its last one.
        "schedulerEnabled": settings.scheduler_enabled,
        "lastCheckAt": (getattr(app.state, "check_status", None) or {}).get("lastCheckAt"),
        # Part 3.4c: the last weekend block, and whether the write route is on.
        **_weekend_health(),
        # Part 3.5: calendar coverage, recomputed against today on every call.
        **_calendar_health(),
        # Part 3.5: the market news poller.
        **_news_health(),
        # Part 3.6a: whether this process may generate macro briefs (3.6b).
        "macroBriefEnabled": settings.macro_brief_enabled,
        # Part 3.6b: the last stored brief, and the last attempt's error.
        **_brief_health(),
    }


@app.get("/")
async def root():
    """Root endpoint with service info."""
    return {
        "service": settings.service_name,
        "description": "Market health monitoring, regime detection, and crash guard",
        "docs": "/docs",
        "endpoints": [
            "GET  /health",
            "GET  /market/health",
            "GET  /market/indicators",
            "GET  /market/history?days=30",
            "GET  /market/calendar?days=7",
            "GET  /macro/brief/inputs",
            "GET  /macro/brief",
            "POST /macro/brief/generate",
        ],
    }


# ── /market (Part 3.4, spec decision 7) ──────────────────────────
# Postgres only: an endpoint never computes a score or downloads, so the
# current answer and the history cannot disagree, and a failed insert shows
# up as an older checkedAt.

REGIME_MESSAGES = {
    "HEALTHY": "Market conditions are favorable",
    "CAUTIOUS": "Elevated risk — trade with caution",
    "DANGER": "High risk — consider reducing exposure",
    "CRITICAL": "⚠️ PROTECT CAPITAL — market in distress",
}
NO_CHECKS_DETAIL = "no health checks yet"      # never FastAPI's "Not Found" of a wrong route
DB_UNAVAILABLE_DETAIL = "database unavailable"
HISTORY_DAYS_DEFAULT = 30
HISTORY_DAYS_MAX = 90


async def _read(helper, *args):
    """One db read helper; no pool or a database failure is a 503."""
    pool = getattr(app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail=DB_UNAVAILABLE_DETAIL)
    try:
        return await helper(pool, *args)
    except db.DB_FAILURES as e:
        logger.warning(f"Postgres read failed ({helper.__name__}): {e!r}")
        raise HTTPException(status_code=503, detail=DB_UNAVAILABLE_DETAIL) from None


async def _latest_row() -> dict:
    row = await _read(db.latest_health_check)
    if row is None:
        raise HTTPException(status_code=404, detail=NO_CHECKS_DETAIL)
    return row


def _indicators(row: dict) -> dict:
    """The row's indicators JSONB as a dict, {} when it is not a JSON object
    (asyncpg returns jsonb as text when no codec is set)."""
    value = row.get("indicators")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _valid_monitors(value: Any) -> Optional[dict]:
    if isinstance(value, dict) and all(isinstance(m, dict) for m in value.values()):
        return value
    return None


@app.get("/market/health")
async def market_health():
    """
    The latest check. settleScore / settleCheckedAt are the trend base (the
    latest scored settle before that check's session open), not the last
    published score, which lives only in the tf:risk:health payload.
    lastScored appears only when the latest check has no score.
    """
    row = await _latest_row()
    ind = _indicators(row)
    checked_at = row["checked_at"]
    body = {
        "score": row["score"],
        "regime": row["regime"],
        "trend": row["trend"],
        "settleScore": ind.get("settleScore"),
        "settleCheckedAt": ind.get("settleCheckedAt"),
        "message": REGIME_MESSAGES.get(row["regime"]),
        "checkedAt": checked_at.isoformat(),
        "ageSeconds": int((datetime.now(timezone.utc) - checked_at).total_seconds()),
        "kind": ind.get("kind"),
        "coverage": ind.get("coverage"),
        "stale": ind.get("stale"),
        # Part 3.4b: the futures cap this check applied, null on a settle row
        # and on every row written before 3.4b.
        "overlay": ind.get("overlay"),
        # Part 3.4c: the weekend-exposure block, null except on a weekend-eve
        # session's last eight rows, and on every row written before 3.4c.
        "weekend": ind.get("weekend"),
        # Part 3.5 addition 8: the news feed's state, from process memory at
        # request time — the only values on this route not from Postgres.
        **news_poller.stale_view(app.state, _now()),
    }
    if row["score"] is None:
        scored = await _read(db.latest_scored_health_check)
        body["lastScored"] = (
            {"score": scored["score"], "regime": scored["regime"],
             "checkedAt": scored["checked_at"].isoformat()}
            if scored is not None else None
        )
    return body


@app.get("/market/indicators")
async def market_indicators():
    """The six monitors of the latest check, each 3.3's contract + weight.
    A row whose JSONB has the wrong shape answers monitors: null, never 500."""
    row = await _latest_row()
    ind = _indicators(row)
    return {
        "checkedAt": row["checked_at"].isoformat(),
        "kind": ind.get("kind"),
        "coverage": ind.get("coverage"),
        "inputs": ind.get("inputs"),
        "monitors": _valid_monitors(ind.get("monitors")),
        # Part 3.4b: the futures prices the check saw, and the cap it applied.
        "futures": ind.get("futures"),
        "overlay": ind.get("overlay"),
        # Part 3.4c: the weekend-exposure block of that same row.
        "weekend": ind.get("weekend"),
    }


@app.get("/market/history")
async def market_history(days: int = Query(HISTORY_DAYS_DEFAULT, ge=1, le=HISTORY_DAYS_MAX)):
    """Checks over the last `days`, ascending, null scores included. An
    empty window is 200 with rows: [] (Part 1.4's rule), not a 404."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = await _read(db.health_history, since)
    return {
        "days": days,
        "rows": [
            {"checkedAt": r["checked_at"].isoformat(), "score": r["score"], "regime": r["regime"],
             "trend": r["trend"], "kind": r["kind"], "stale": r["stale"]}
            for r in rows
        ],
    }


# ── /market/calendar (Part 3.5, spec decisions 8–9) ──────────────
# The hand-maintained file only: no Postgres, no Redis, no network.

CALENDAR_UNAVAILABLE_DETAIL = "calendar unavailable"
CALENDAR_DAYS_DEFAULT = 7
CALENDAR_DAYS_MAX = 31


def _now() -> datetime:
    """The clock for /market/calendar and /health's calendar fields (tests patch it)."""
    return datetime.now(timezone.utc)


def _news_health() -> dict:
    """/health's news poller fields (Part 3.5 decision 7). lastNewsPollAt is
    the last *successful* poll."""
    status = getattr(app.state, "news_status", None) or {}
    return {
        "newsPollEnabled": settings.news_poll_enabled,
        "lastNewsPollAt": status.get("lastSuccessAt"),
        "newsPageSpanMinutes": status.get("pageSpanMinutes"),
        "newsOldestAt": status.get("oldestAt"),
        "newsLastError": status.get("lastError"),
        "finnhubConfigured": settings.finnhub_configured,
    }


def _weekend_health() -> dict:
    """/health's weekend fields (W5): the last block this process built, and
    whether the situation route has a secret — the boolean only, never the
    value (G14). The flag's own text is not read here: /health runs on every
    Docker healthcheck, and a Redis round trip per 10 s buys nothing the
    block and the PUT response do not already show."""
    status = getattr(app.state, "check_status", None) or {}
    return {"weekendLevel": status.get("weekendLevel"),
            "weekendReasonCount": status.get("weekendReasonCount"),
            "weekendDropped": status.get("weekendDropped"),
            "weekendWriteConfigured": settings.weekend_write_configured}


def _calendar_health() -> dict:
    """/health's calendar fields. coverage short is recomputed against today,
    never frozen at load; both are null when the file is unavailable."""
    try:
        calendar = econ_calendar.load()
    except econ_calendar.CalendarUnavailable:
        return {"calendarCoversThrough": None, "calendarCoverageShort": None}
    return {
        "calendarCoversThrough": calendar["coversThrough"].isoformat(),
        "calendarCoverageShort": econ_calendar.coverage_short(calendar, econ_calendar.et_today(_now())),
    }


@app.get("/market/calendar")
async def market_calendar(days: int = Query(CALENDAR_DAYS_DEFAULT, ge=1, le=CALENDAR_DAYS_MAX)):
    """FOMC decisions, CPI releases and jobs reports on ET dates today …
    today + days − 1. Past the file's coverage: 200 with coverageShort, never
    a 404. A missing or invalid file is a 503."""
    try:
        calendar = econ_calendar.load()
    except econ_calendar.CalendarUnavailable:
        raise HTTPException(status_code=503, detail=CALENDAR_UNAVAILABLE_DETAIL) from None
    return econ_calendar.window(calendar, _now(), days)


# ── /market/weekend/situation (Part 3.4c decision 2) ─────────────
# The operator's "an unresolved thing is live" flag, which the weekend block
# reads as one of its six inputs. These are the service's first *write*
# routes, so they carry a shared secret from the start rather than waiting
# for going-public: X-TF-Token, compared with hmac.compare_digest against
# WEEKEND_WRITE_TOKEN. An empty token disables the routes (503) — it never
# falls open, which is what the dev twin relies on.

SITUATION_DISABLED_DETAIL = "weekend situation route disabled: no WEEKEND_WRITE_TOKEN"
SITUATION_UNAUTHORIZED_DETAIL = "invalid or missing X-TF-Token"
SITUATION_REDIS_DETAIL = "Redis unavailable: the situation flag cannot be stored"
SITUATION_DEFAULT_HOURS = 72


class SituationBody(BaseModel):
    """The flag's text and how long it stands. `hours` is mandatory-by-default
    and hard-bounded, so a forgotten flag dies on its own (spec D2)."""
    text: str = Field(min_length=1, max_length=weekend.SITUATION_TEXT_MAX)
    hours: int = Field(SITUATION_DEFAULT_HOURS, ge=1, le=cache.SITUATION_MAX_HOURS)


def _require_write_token(token: Optional[str]) -> None:
    """503 with no secret configured, 401 on a wrong one. The secret itself
    never reaches a response, a log or an exception (G14)."""
    secret = settings.weekend_write_token.get_secret_value()
    if not secret:
        raise HTTPException(status_code=503, detail=SITUATION_DISABLED_DETAIL)
    if not token or not hmac.compare_digest(token, secret):
        raise HTTPException(status_code=401, detail=SITUATION_UNAUTHORIZED_DETAIL)


def _situation_redis():
    r = getattr(app.state, "redis", None)
    if r is None:
        raise HTTPException(status_code=503, detail=SITUATION_REDIS_DETAIL)
    return r


@app.put("/market/weekend/situation")
async def set_weekend_situation(body: SituationBody,
                                x_tf_token: Optional[str] = Header(default=None)):
    """Set the flag. 200 with the stored record (never the token)."""
    _require_write_token(x_tf_token)
    r = _situation_redis()
    record = weekend_inputs.build_situation(body.text, body.hours, _now())
    try:
        await weekend_inputs.write_situation(r, record, body.hours)
    except Exception as e:
        logger.warning(f"Weekend situation write failed: {e!r}")
        raise HTTPException(status_code=503, detail=SITUATION_REDIS_DETAIL) from None
    logger.info(f"Weekend situation set for {body.hours}h, expires {record['expiresAt']}")
    return record


@app.delete("/market/weekend/situation")
async def clear_weekend_situation(x_tf_token: Optional[str] = Header(default=None)):
    """Clear the flag. 200 either way, `cleared` says whether one was set."""
    _require_write_token(x_tf_token)
    r = _situation_redis()
    try:
        cleared = await weekend_inputs.clear_situation(r)
    except Exception as e:
        logger.warning(f"Weekend situation delete failed: {e!r}")
        raise HTTPException(status_code=503, detail=SITUATION_REDIS_DETAIL) from None
    logger.info(f"Weekend situation cleared (was set: {cleared})")
    return {"cleared": cleared}


# ── /macro/brief/inputs (Part 3.6a, spec decision 7) ─────────────
# The document 3.6b will store, served so the inputs can be checked without an
# LLM. Unauthenticated, and a cold call can start FRED requests, so one
# assembly at a time (the lock) and the last document reused for 60 s (the
# lock alone does not stop a slow loop of calls). `cached` is added on the
# way out only: assemble_inputs() never returns it, so 3.6b stores none.

INPUTS_REUSE_SECONDS = 60


def _monotonic() -> float:
    """The reuse clock (tests patch it)."""
    return time.monotonic()


@app.get("/macro/brief/inputs")
async def macro_brief_inputs():
    """200 with the inputs document plus `cached`; sections say why they are
    degraded. Works whatever MACRO_BRIEF_ENABLED says. A raise during
    assembly is a 500 and stores nothing for reuse."""
    state = app.state
    if getattr(state, "inputs_lock", None) is None:
        state.inputs_lock = asyncio.Lock()
    async with state.inputs_lock:
        last = getattr(state, "inputs_last", None)
        if last is not None and _monotonic() - last[0] < INPUTS_REUSE_SECONDS:
            return {**last[1], "cached": True}
        doc = await macro_inputs.assemble_inputs(state, state.fred_client, state.inputs_http, now=_now())
        state.inputs_last = (_monotonic(), doc)
    return {**doc, "cached": False}


# ── /macro/brief (Part 3.6b, spec decision 6) ────────────────────
# The stored brief only: GET never generates, whatever MACRO_BRIEF_ENABLED
# says. brief_status is process memory, written by each generation.

NO_BRIEF_DETAIL = "no macro brief yet"


def _brief_health() -> dict:
    """/health's brief fields: lastBriefAt / lastBriefTrigger describe the last
    stored brief, lastBriefError the last attempt (null after a success)."""
    status = getattr(app.state, "brief_status", None) or {}
    return {"lastBriefAt": status.get("lastBriefAt"), "lastBriefTrigger": status.get("lastTrigger"),
            "lastBriefError": status.get("lastError")}


def _brief_body(row: dict, *, include_inputs: bool = False) -> dict:
    """The body for one stored brief. freshness is the stored inputs' own
    block; the inputs themselves only when asked."""
    inputs = row["inputs"]
    body = {
        "id": str(row["id"]),
        "generatedAt": row["generated_at"].isoformat(),
        "ageMinutes": int((_now() - row["generated_at"]).total_seconds() // 60),
        "trigger": row["trigger"],
        "regime": row["regime"],
        "healthScore": row["health_score"],
        "briefText": row["brief_text"],
        "brief": row["brief"],
        "freshness": inputs.get("freshness") if isinstance(inputs, dict) else None,
    }
    if include_inputs:
        body["inputs"] = inputs
    return body


@app.get("/macro/brief")
async def get_macro_brief(include_inputs: bool = Query(False, alias="includeInputs")):
    """The latest stored brief. No row is a 404, distinct from a wrong route;
    no pool or a database failure is a 503."""
    row = await _read(db.latest_macro_brief)
    if row is None:
        raise HTTPException(status_code=404, detail=NO_BRIEF_DETAIL)
    return _brief_body(row, include_inputs=include_inputs)


BRIEF_DISABLED_DETAIL = "macro brief disabled"
GENERATION_BUSY_DETAIL = "generation in progress"
BRIEF_LOST_DETAIL = "brief lost: database unavailable"
MANUAL_COOLDOWN_SECONDS = 600     # since the last stored brief of any trigger


@app.post("/macro/brief/generate", status_code=201)
async def macro_brief_generate():
    """
    One manual brief (trigger "manual"), blocking for up to the ai-agent
    timeout. In order: flag off 503, no pool 503, a generation running 409, a
    stored brief under 600 s old 429 + Retry-After. Then 201 with GET's body,
    422 inputs not ready (no LLM call, and not worth retrying on a timer), 502
    an ai-agent failure, 503 an insert failure. Unauthenticated: carried to
    going public.
    """
    state = app.state
    if not settings.macro_brief_enabled:
        raise HTTPException(status_code=503, detail=BRIEF_DISABLED_DETAIL)
    if getattr(state, "db_pool", None) is None:
        raise HTTPException(status_code=503, detail=DB_UNAVAILABLE_DETAIL)
    if state.brief_lock.locked():
        raise HTTPException(status_code=409, detail=GENERATION_BUSY_DETAIL)
    last = await _read(db.last_brief_at)
    age = (_now() - last).total_seconds() if last is not None else None
    if age is not None and age < MANUAL_COOLDOWN_SECONDS:
        raise HTTPException(status_code=429, detail="last macro brief too recent",
                            headers={"Retry-After": str(math.ceil(MANUAL_COOLDOWN_SECONDS - age))})

    result = await macro_brief.generate_once(state, state.ai_agent_client, trigger="manual", clock=_now)
    if result["outcome"] == macro_brief.GENERATED:
        return _brief_body(result["row"])
    cause = result["cause"]
    if cause.startswith("insert:"):
        raise HTTPException(status_code=503, detail=BRIEF_LOST_DETAIL)
    status, detail = {"busy": (409, GENERATION_BUSY_DETAIL), "inputs not ready": (422, cause),
                      "database unavailable": (503, DB_UNAVAILABLE_DETAIL)}.get(cause, (502, cause))
    raise HTTPException(status_code=status, detail=detail)
