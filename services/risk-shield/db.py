"""
TradingFirm — Risk Shield Database Layer

Async PostgreSQL access using asyncpg. Part 3.1 ships the connection pool
and the error tuple only: a table helper belongs to the part that uses it
(risk.health_checks writes in 3.4, risk.macro_briefs in 3.6).

This service writes only the `risk` schema. Cross-service communication is
HTTP + Redis pub/sub, never another service's tables.
"""

import json
import logging
from datetime import datetime
from typing import Any, Optional

import asyncpg

from config import settings

logger = logging.getLogger(__name__)

# Every exception that means "the database, not an upstream source, failed".
# Copied verbatim from data-engine (Part 2.4), including the reason OSError
# is absent: asyncio.TimeoutError *is* the builtin TimeoutError, which
# subclasses OSError, so an OSError-based tuple reports every timed-out call
# as a dead database.
DB_ERRORS = (asyncpg.PostgresError, asyncpg.InterfaceError, ConnectionError)

# "The database did not answer", timeouts included (Part 3.4). asyncpg's
# command_timeout raises TimeoutError, which DB_ERRORS leaves out on purpose
# (above). The health-check writes and the /market endpoints catch this one.
DB_FAILURES = (*DB_ERRORS, TimeoutError)


def _safe_dsn(dsn: str) -> str:
    """The DSN with the credentials half removed, for logging (G14)."""
    return dsn.split("@")[1] if "@" in dsn else dsn


async def create_db_pool(timeout: float = None) -> asyncpg.Pool:
    """
    Create and return an asyncpg connection pool.

    `timeout` bounds establishing each connection (asyncpg's default is
    60 s). The pool opens `min_size` connections, so this bound alone
    allows ~2x — the lifespan's asyncio.wait_for is the hard one
    (Part 3.1 decision 6).
    """
    import config

    if timeout is None:
        timeout = config.STARTUP_TIMEOUT
    dsn = settings.asyncpg_url
    logger.info(f"Connecting to database: {_safe_dsn(dsn)}")
    pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=2,
        max_size=10,
        command_timeout=30,
        timeout=timeout,
    )
    logger.info("Database connection pool created")
    return pool


# ── risk.health_checks (Part 3.4, spec decision 6) ───────────────
# The table exists since 001. `kind` ("market" | "settle") lives in the
# indicators JSONB, not a column, so 3.4 needs no migration.

INSERT_HEALTH_CHECK_SQL = """
INSERT INTO risk.health_checks (checked_at, score, regime, trend, indicators)
VALUES ($1, $2, $3, $4, $5::jsonb)
"""

LATEST_HEALTH_CHECK_SQL = """
SELECT checked_at, score, regime, trend, indicators
FROM risk.health_checks
ORDER BY checked_at DESC
LIMIT 1
"""

LATEST_SCORED_HEALTH_CHECK_SQL = """
SELECT checked_at, score, regime
FROM risk.health_checks
WHERE score IS NOT NULL
ORDER BY checked_at DESC
LIMIT 1
"""

# The trend base: the latest scored settle before a cutoff (the session open
# of the check's date), so even the 16:20 check reads an earlier session.
SETTLE_BASE_SQL = """
SELECT checked_at, score
FROM risk.health_checks
WHERE indicators->>'kind' = 'settle' AND score IS NOT NULL AND checked_at < $1
ORDER BY checked_at DESC
LIMIT 1
"""

# The overlay reference (Part 3.4b decision 6): the same row settle_base finds,
# with its indicators, so a check can read the futures prices that settle saw.
SETTLE_REFERENCE_SQL = """
SELECT checked_at, score, regime, indicators
FROM risk.health_checks
WHERE indicators->>'kind' = 'settle' AND score IS NOT NULL AND checked_at < $1
ORDER BY checked_at DESC
LIMIT 1
"""

# Two fields out of the JSONB as text, never the whole blob (~4 KB a row).
HEALTH_HISTORY_SQL = """
SELECT checked_at, score, regime, trend,
       indicators->>'kind' AS kind, indicators->>'stale' AS stale
FROM risk.health_checks
WHERE checked_at >= $1
ORDER BY checked_at ASC
"""


# Part 3.4c's log. Two reads, both bounded by `since`:
#   1. every row carrying a weekend block — at most 8 a weekend-eve session,
#      so ~200 over half a year. `weekend` and `futures` only, never the
#      monitors blob.
#   2. the first market row of each ET date, which is the "next open" side of
#      the move. DISTINCT ON keeps it to one row per session (~130 a half
#      year) instead of every 5-minute row.
WEEKEND_ROWS_SQL = """
SELECT checked_at, score, regime,
       indicators->>'kind' AS kind,
       indicators->'weekend' AS weekend,
       indicators->'futures' AS futures
FROM risk.health_checks
WHERE checked_at >= $1 AND jsonb_typeof(indicators->'weekend') = 'object'
ORDER BY checked_at ASC
"""

FIRST_MARKET_ROW_SQL = """
SELECT DISTINCT ON ((checked_at AT TIME ZONE 'America/New_York')::date)
       checked_at, score, regime,
       indicators->'futures' AS futures
FROM risk.health_checks
WHERE checked_at >= $1 AND indicators->>'kind' = 'market'
ORDER BY (checked_at AT TIME ZONE 'America/New_York')::date, checked_at ASC
"""


def health_indicators(health: dict, kind: str, settle: Optional[dict],
                      paused_seconds: Optional[int] = None) -> str:
    """The indicators JSONB for one check. allow_nan=False: a NaN from a
    monitor bug raises ValueError here, before any SQL. pausedSeconds is the
    host pause before this check (3.4 follow-up addition 2), else null."""
    return json.dumps({
        "kind": kind,
        "coverage": health.get("coverage"),
        "stale": bool(health.get("stale")),
        "staleMonitors": health.get("staleMonitors") or [],
        "monitors": health.get("monitors") or {},
        "inputs": health.get("inputs") or {},
        "settleScore": settle["score"] if settle else None,
        "settleCheckedAt": settle["checkedAt"].isoformat() if settle else None,
        "pausedSeconds": paused_seconds,
        # Part 3.4b: the futures prices this check saw, and the cap it applied.
        "futures": health.get("futures") or {},
        "overlay": health.get("overlay"),
        # Part 3.4c: the weekend-exposure block, null off-window and on every
        # row written before 3.4c. It is guarded against non-finite numbers
        # before it gets here, so it can never be what raises below.
        "weekend": health.get("weekend"),
    }, allow_nan=False)


async def insert_health_check(pool, health: dict, kind: str, trend: Optional[str],
                              settle: Optional[dict], paused_seconds: Optional[int] = None) -> None:
    """One row per check, null scores included. checked_at is the snapshot's
    own checkedAt, never DEFAULT now()."""
    indicators = health_indicators(health, kind, settle, paused_seconds)
    await pool.execute(
        INSERT_HEALTH_CHECK_SQL,
        datetime.fromisoformat(health["checkedAt"]),
        health["score"],
        health["regime"],
        trend,
        indicators,
    )


async def latest_health_check(pool) -> Optional[dict]:
    row = await pool.fetchrow(LATEST_HEALTH_CHECK_SQL)
    return dict(row) if row is not None else None


async def latest_scored_health_check(pool) -> Optional[dict]:
    row = await pool.fetchrow(LATEST_SCORED_HEALTH_CHECK_SQL)
    return dict(row) if row is not None else None


async def settle_base(pool, before: datetime) -> Optional[dict]:
    """{score, checkedAt} of the latest scored settle before `before`, or None."""
    row = await pool.fetchrow(SETTLE_BASE_SQL, before)
    if row is None:
        return None
    return {"score": row["score"], "checkedAt": row["checked_at"]}


async def settle_reference(pool, before: datetime) -> Optional[dict]:
    """{score, checkedAt, regime, futures, indicators} of the latest scored
    settle before `before`, or None. `futures` is that settle's stored block and
    `indicators` its whole JSONB (the night check copies the monitors from it);
    both are {} when the row predates 3.4b or the blob has the wrong shape."""
    row = await pool.fetchrow(SETTLE_REFERENCE_SQL, before)
    if row is None:
        return None
    try:
        indicators = _json_value(row["indicators"]) if "indicators" in row else None
    except ValueError:      # a corrupt blob is no reference, never a raised read (as /market/health)
        indicators = None
    futures = indicators.get("futures") if isinstance(indicators, dict) else None
    return {"score": row["score"], "checkedAt": row["checked_at"],
            "regime": row["regime"] if "regime" in row else None,
            "futures": futures if isinstance(futures, dict) else {},
            "indicators": indicators if isinstance(indicators, dict) else {}}


def _json_bool(text: Any) -> Optional[bool]:
    return {"true": True, "false": False}.get(text)


async def health_history(pool, since: datetime) -> list[dict]:
    """Rows since `since`, ascending, without the indicators blob. A stale
    value that is not a JSON boolean reads as None, never an error."""
    rows = await pool.fetch(HEALTH_HISTORY_SQL, since)
    return [
        {
            "checked_at": row["checked_at"],
            "score": row["score"],
            "regime": row["regime"],
            "trend": row["trend"],
            "kind": row["kind"],
            "stale": _json_bool(row["stale"]),
        }
        for row in rows
    ]


# ── risk.macro_briefs (Part 3.6b, spec decision 2) ───────────────
# Columns from 005 + 006. A row exists only for a valid ai-agent answer on
# ready inputs; generate_once decides that, these helpers only store and read.

INSERT_MACRO_BRIEF_SQL = """
INSERT INTO risk.macro_briefs (generated_at, regime, health_score, brief_text, brief, inputs, trigger)
VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7)
RETURNING id
"""

LATEST_MACRO_BRIEF_SQL = """
SELECT id, generated_at, trigger, regime, health_score, brief_text, brief, inputs
FROM risk.macro_briefs
ORDER BY generated_at DESC
LIMIT 1
"""

# The debounce and the manual cooldown count stored briefs only (decision 5).
LAST_BRIEF_AT_SQL = """
SELECT max(generated_at) FROM risk.macro_briefs
WHERE $1::text IS NULL OR trigger = $1
"""


async def weekend_rows(pool, since: datetime) -> list[dict]:
    """Rows carrying a weekend block since `since`, ascending."""
    rows = await pool.fetch(WEEKEND_ROWS_SQL, since)
    return [{"checkedAt": row["checked_at"], "score": row["score"], "regime": row["regime"],
             "kind": row["kind"], "weekend": _json_value(row["weekend"]),
             "futures": _json_value(row["futures"])} for row in rows]


async def first_market_rows(pool, since: datetime) -> list[dict]:
    """The first market row of each ET date since `since`, ascending."""
    rows = await pool.fetch(FIRST_MARKET_ROW_SQL, since)
    return [{"checkedAt": row["checked_at"], "score": row["score"], "regime": row["regime"],
             "futures": _json_value(row["futures"])} for row in rows]


def _json_value(value: Any) -> Any:
    """A JSONB column as Python (asyncpg returns jsonb as text without a codec)."""
    return json.loads(value) if isinstance(value, str) else value


async def insert_macro_brief(pool, *, generated_at: datetime, regime: Optional[str], health_score: Optional[int],
                             brief_text: str, brief: dict, inputs: dict, trigger: str) -> str:
    """One row; the new id. Both JSONB bodies are dumped with allow_nan=False,
    so a NaN raises ValueError before any SQL."""
    brief_json, inputs_json = json.dumps(brief, allow_nan=False), json.dumps(inputs, allow_nan=False)
    new_id = await pool.fetchval(INSERT_MACRO_BRIEF_SQL, generated_at, regime, health_score, brief_text,
                                 brief_json, inputs_json, trigger)
    return str(new_id)


async def latest_macro_brief(pool) -> Optional[dict]:
    """The newest row with brief and inputs decoded, or None."""
    row = await pool.fetchrow(LATEST_MACRO_BRIEF_SQL)
    if row is None:
        return None
    return {**dict(row), "brief": _json_value(row["brief"]), "inputs": _json_value(row["inputs"])}


async def last_brief_at(pool, trigger: Optional[str] = None) -> Optional[datetime]:
    """max(generated_at) over every stored brief, or one trigger's; None when there is none."""
    return await pool.fetchval(LAST_BRIEF_AT_SQL, trigger)


# "This slot already has a brief" (decision 3): a `slot` row inside the slot's window.
SLOT_BRIEF_EXISTS_SQL = """
SELECT EXISTS (SELECT 1 FROM risk.macro_briefs
               WHERE trigger = 'slot' AND generated_at >= $1 AND generated_at < $2)
"""


async def slot_brief_exists(pool, window_start: datetime, window_end: datetime) -> bool:
    """Whether a `slot` brief was stored in [window_start, window_end)."""
    return bool(await pool.fetchval(SLOT_BRIEF_EXISTS_SQL, window_start, window_end))
