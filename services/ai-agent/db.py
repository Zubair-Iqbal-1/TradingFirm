"""
TradingFirm — AI Agent Database Layer (Part 4.4)

Async PostgreSQL access using asyncpg. This service writes only the `ai`
schema and reads `users.settings`; no service owns `users` yet, and nothing
writes it until Part 5.3. Cross-service communication is HTTP + Redis, never
another service's tables.
"""

import json
import logging
from datetime import date
from typing import Any, Optional

import asyncpg

from config import settings

logger = logging.getLogger(__name__)

# risk-shield's tuples, copied (separate images, no shared package). OSError
# is absent on purpose: asyncio.TimeoutError is the builtin TimeoutError,
# which subclasses it, so an OSError-based tuple would report every timed-out
# call as a dead database (Part 2.4).
DB_ERRORS = (asyncpg.PostgresError, asyncpg.InterfaceError, ConnectionError)
DB_FAILURES = (*DB_ERRORS, TimeoutError)

# The single fixed development user (D18), seeded by 007_ai.sql.
DEV_USER_ID = "00000000-0000-4000-8000-000000000001"


def _safe_dsn(dsn: str) -> str:
    """The DSN with the credentials half removed, for logging (G14)."""
    return dsn.split("@")[1] if "@" in dsn else dsn


async def create_db_pool(timeout: float = None) -> asyncpg.Pool:
    """An asyncpg pool. `timeout` bounds each connection attempt; the
    lifespan's asyncio.wait_for is the hard bound (risk-shield 3.1)."""
    import config

    if timeout is None:
        timeout = config.STARTUP_TIMEOUT
    dsn = settings.asyncpg_url
    logger.info(f"Connecting to database: {_safe_dsn(dsn)}")
    pool = await asyncpg.create_pool(
        dsn=dsn, min_size=1, max_size=5, command_timeout=30, timeout=timeout,
    )
    logger.info("Database connection pool created")
    return pool


# ── users.settings ───────────────────────────────────────────────

GET_SETTINGS_SQL = """
SELECT account_size, risk_per_trade_pct
FROM users.settings
WHERE user_id = $1::uuid
"""


async def get_settings(pool: asyncpg.Pool, user_id: str) -> Optional[dict]:
    """{accountSize, riskPct} as the NUMERICs came back (Decimal), or None
    when the user has no row. accountSize is None until it is set by hand
    (docs/runbook.md) — the caller turns both into a 409."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(GET_SETTINGS_SQL, user_id)
    if row is None:
        return None
    return {"accountSize": row["account_size"], "riskPct": row["risk_per_trade_pct"]}


# ── ai.llm_calls ─────────────────────────────────────────────────

LLM_CALL_COLUMNS = (
    "called_at", "et_day", "user_id", "ticker", "route", "label", "model", "host",
    "tokens_in", "tokens_out", "tokens_reasoning", "cache_read_tokens",
    "cache_write_tokens", "cost_usd", "outcome", "counters", "verdict_id",
)

INSERT_LLM_CALL_SQL = """
INSERT INTO ai.llm_calls
    (called_at, et_day, user_id, ticker, route, label, model, host,
     tokens_in, tokens_out, tokens_reasoning, cache_read_tokens,
     cache_write_tokens, cost_usd, outcome, counters, verdict_id)
VALUES ($1, $2, $3::uuid, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16::text[], $17::uuid)
"""


def _call_args(call: dict) -> tuple:
    return tuple(call.get(column) for column in LLM_CALL_COLUMNS)


async def insert_llm_call(pool: asyncpg.Pool, call: dict) -> None:
    async with pool.acquire() as conn:
        await conn.execute(INSERT_LLM_CALL_SQL, *_call_args(call))


# The seed for the daily caps and the cost totals (spec 4.4 decision 6).
# Every row is a request that reached the wire, so there is nothing to filter.
LEDGER_DAY_SQL = """
SELECT
    count(*) FILTER (WHERE 'llm_calls' = ANY(counters))        AS llm_calls,
    count(*) FILTER (WHERE 'classifier_calls' = ANY(counters)) AS classifier_calls,
    COALESCE(sum(cost_usd), 0)                                 AS cost_day
FROM ai.llm_calls
WHERE et_day = $1
"""

LEDGER_MONTH_SQL = """
SELECT COALESCE(sum(cost_usd), 0) AS cost_month
FROM ai.llm_calls
WHERE et_day >= $1 AND et_day <= $2
"""


async def ledger_totals(pool: asyncpg.Pool, day: date) -> dict:
    """Today's counts per counter, and today's and the month-to-date cost,
    from the ledger."""
    async with pool.acquire() as conn:
        today = await conn.fetchrow(LEDGER_DAY_SQL, day)
        month = await conn.fetchrow(LEDGER_MONTH_SQL, day.replace(day=1), day)
    return {
        "llm_calls": int(today["llm_calls"] or 0),
        "classifier_calls": int(today["classifier_calls"] or 0),
        "cost_day": float(today["cost_day"] or 0),
        "cost_month": float(month["cost_month"] or 0),
    }


# ── ai.verdicts ──────────────────────────────────────────────────

VERDICT_COLUMNS = (
    "user_id", "ticker", "horizon", "asked_at", "entry", "entry_source",
    "dossier", "prompt_inputs", "prompt_sha", "fingerprint", "macro_brief_id",
    "regime", "verdict", "confidence", "reasoning", "thesis", "thesis_breakers",
    "risk_flags", "plan_proposed", "plan_rejection", "model", "tokens_in",
    "tokens_out",
)
_JSON_COLUMNS = frozenset({
    "dossier", "prompt_inputs", "thesis", "thesis_breakers", "risk_flags",
    "plan_proposed", "plan_rejection",
})

INSERT_VERDICT_SQL = """
INSERT INTO ai.verdicts
    (user_id, ticker, horizon, asked_at, entry, entry_source,
     dossier, prompt_inputs, prompt_sha, fingerprint, macro_brief_id,
     regime, verdict, confidence, reasoning, thesis, thesis_breakers,
     risk_flags, plan_proposed, plan_rejection, model, tokens_in, tokens_out)
VALUES ($1::uuid, $2, $3, $4, $5, $6,
        $7::jsonb, $8::jsonb, $9, $10, $11::uuid,
        $12, $13, $14, $15, $16::jsonb, $17::jsonb,
        $18::jsonb, $19::jsonb, $20::jsonb, $21, $22, $23)
RETURNING id
"""


def _verdict_args(row: dict) -> tuple:
    out = []
    for column in VERDICT_COLUMNS:
        value = row.get(column)
        if column in _JSON_COLUMNS and value is not None:
            value = json.dumps(value, default=str)
        out.append(value)
    return tuple(out)


async def insert_verdict_with_call(pool: asyncpg.Pool, verdict: dict, call: dict) -> str:
    """The verdict row and its ledger row in ONE transaction (spec 4.4 write
    7): either both exist or neither does. Returns the verdict id."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(INSERT_VERDICT_SQL, *_verdict_args(verdict))
            verdict_id = str(row["id"])
            await conn.execute(
                INSERT_LLM_CALL_SQL, *_call_args({**call, "verdict_id": verdict_id})
            )
    return verdict_id


GET_VERDICT_SQL = """
SELECT id, ticker, horizon, asked_at, entry, entry_source, fingerprint,
       macro_brief_id, regime, verdict, confidence, reasoning, thesis,
       thesis_breakers, risk_flags, plan_proposed, plan_rejection, model,
       served_count
FROM ai.verdicts
WHERE id = $1::uuid AND user_id = $2::uuid
"""

# A cache hit is visible here, not in the ledger (the ledger is wire calls
# only): one UPDATE per served hit.
BUMP_SERVED_SQL = """
UPDATE ai.verdicts
SET served_count = served_count + 1, last_served_at = $2
WHERE id = $1::uuid
"""


def _decode(value: Any) -> Any:
    """jsonb arrives as text (no codec registered)."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


async def get_verdict(pool: asyncpg.Pool, verdict_id: str, user_id: str) -> Optional[dict]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(GET_VERDICT_SQL, verdict_id, user_id)
    if row is None:
        return None
    out = dict(row)
    for column in ("thesis", "thesis_breakers", "risk_flags", "plan_proposed", "plan_rejection"):
        out[column] = _decode(out[column])
    return out


async def bump_served(pool: asyncpg.Pool, verdict_id: str, now) -> None:
    async with pool.acquire() as conn:
        await conn.execute(BUMP_SERVED_SQL, verdict_id, now)
