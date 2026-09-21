"""
TradingFirm — the LLM call ledger (Part 4.4).

One `ai.llm_calls` row per LLM request that reached the wire, whatever it
answered, and nothing else: a pre-wire refusal (no key, cooldown, a cap) sent
nothing and writes nothing. Redis stays the live counter; this is the
permanent record, and what re-seeds the daily caps at startup.

Written by the routes, never the provider, which stays database-free.

A ledger write never fails a call that was already paid for: a failure is one
ERROR line and one increment of `tf:ai:state:ledger_missed:{day}`, which
/usage shows, so a short ledger is visible rather than silent.
"""

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

import cache
import db
from providers.base import (
    LLMAuthFailed,
    LLMBadResponse,
    LLMError,
    LLMRateLimited,
    LLMRefused,
    LLMRejected,
    LLMResult,
    LLMUnavailable,
)

logger = logging.getLogger(__name__)

ROUTE_ANALYZE = "analyze"
ROUTE_CLASSIFY = "classify"

OUTCOME_OK = "ok"

# Exactly the errors raised after a request went out. LLMAuthFailed is here
# (a 401/402/403 is an answer) although it subclasses LLMNotConfigured, which
# is pre-wire — so the lookup is by exact type, never isinstance.
_POST_WIRE = {
    LLMRateLimited: "rate_limited",
    LLMUnavailable: "unavailable",
    LLMRejected: "rejected",
    LLMRefused: "refused",
    LLMBadResponse: "bad_response",
    LLMAuthFailed: "auth_failed",
}


def outcome_of(error: BaseException) -> Optional[str]:
    """The ledger outcome for an error, or None when no request reached the
    wire and so no row is owed."""
    return _POST_WIRE.get(type(error))


def _cost(value) -> Optional[Decimal]:
    try:
        cost = Decimal(str(value))
    except Exception:
        return None
    return cost if cost.is_finite() and cost >= 0 else None


def _int(value) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def build(
    *,
    route: str,
    label: str,
    model: str,
    outcome: str,
    counters: list[str],
    result: Optional[LLMResult] = None,
    ticker: Optional[str] = None,
    user_id: Optional[str] = None,
    verdict_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """One ledger row. `result` is None for a post-wire failure, whose tokens
    and cost the gateway never reported — those columns stay NULL, never 0."""
    now = now or datetime.now(timezone.utc)
    usage = result.usage if result is not None else {}
    return {
        "called_at": now,
        "et_day": date.fromisoformat(cache.et_day(now)),
        "user_id": user_id,
        "ticker": ticker,
        "route": route,
        "label": label,
        "model": (result.model if result is not None else model) or "unknown",
        "tokens_in": _int(usage.get("input")),
        "tokens_out": _int(usage.get("output")),
        "tokens_reasoning": _int(usage.get("reasoning")),
        "cache_read_tokens": _int(usage.get("cacheRead")),
        "cache_write_tokens": _int(usage.get("cacheWrite")),
        "cost_usd": _cost(usage.get("cost")) if "cost" in usage else None,
        "outcome": outcome,
        "counters": list(counters),
        "verdict_id": verdict_id,
    }


async def missed(redis, now: Optional[datetime] = None) -> None:
    """Count one row the ledger does not have."""
    if redis is None:
        return
    try:
        key = cache.day_counter_key(now, name=cache.STATE_LEDGER_MISSED)
        await redis.incr(key)
        await redis.expire(key, cache.TTL_COST, nx=True)
    except Exception as e:
        logger.warning(f"ledger_missed INCR failed: {e}")


async def record(pool, redis, row: dict) -> bool:
    """Write one row. Never raises; False means the ledger is short by one."""
    if pool is not None:
        try:
            await db.insert_llm_call(pool, row)
            return True
        except Exception as e:
            logger.error(
                f"ledger write failed ({type(e).__name__}); row not stored: "
                f"route={row['route']} label={row['label']} outcome={row['outcome']} "
                f"model={row['model']} in={row['tokens_in']} out={row['tokens_out']} "
                f"cost={row['cost_usd']}"
            )
    else:
        logger.error(
            f"ledger write skipped, no database pool: route={row['route']} "
            f"label={row['label']} outcome={row['outcome']} cost={row['cost_usd']}"
        )
    await missed(redis, row["called_at"])
    return False


async def record_error(pool, redis, error: LLMError, **kw) -> bool:
    """A ledger row for a failed call, only if it reached the wire."""
    outcome = outcome_of(error)
    if outcome is None:
        return False
    return await record(pool, redis, build(outcome=outcome, **kw))


async def seed_caps(pool, redis, memory_caps: dict, memory_cost, now: Optional[datetime] = None) -> bool:
    """Raise today's counters and cost totals to what the ledger holds (spec
    4.4 decision 6). Runs once in the lifespan, before the app serves, so
    nothing races it. Never raises; False means nothing was seeded and
    /health says `capsSeeded: false`."""
    if pool is None:
        logger.warning("caps not seeded from the ledger: no database pool")
        return False
    now = now or datetime.now(timezone.utc)
    try:
        totals = await db.ledger_totals(pool, date.fromisoformat(cache.et_day(now)))
    except Exception as e:
        logger.warning(f"caps not seeded from the ledger: read failed ({type(e).__name__})")
        return False

    # The in-process fallbacks first: they cannot fail, and they are what
    # bounds the day if Redis is down now or dies later.
    for name in (cache.STATE_LLM_CALLS, cache.STATE_CLASSIFIER_CALLS):
        memory_caps[name].seed(cache.et_day(now), totals[name])
    try:
        added = {
            name: await cache.seed_counter(redis, memory_caps[name], totals[name], now, name=name)
            for name in (cache.STATE_LLM_CALLS, cache.STATE_CLASSIFIER_CALLS)
        }
        await cache.seed_cost(redis, memory_cost, totals["cost_day"], totals["cost_month"], now)
    except Exception as e:
        logger.warning(
            f"caps seeded in-process only, Redis write failed ({type(e).__name__}): {totals}"
        )
        return True
    logger.info(f"Caps seeded from the ledger: {totals}, added to Redis: {added}")
    return True
