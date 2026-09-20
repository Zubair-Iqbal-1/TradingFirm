"""
TradingFirm — AI Agent Redis Layer (Part 4.1)

The `tf:ai:` namespace, the daily LLM call counter and the provider
cooldown, plus the in-process fallbacks used when Redis is absent or
raising.

The pattern is risk-shield's `cache.py`, which copied data-engine's (separate
images, no shared package), with one difference that matters: every key here
lives under `tf:ai:`. `tf:cache:` is data-engine's and `tf:risk:` is
risk-shield's; all three share Redis DB 0 in prod.

4.1 has no read-through cache — there is nothing to cache yet. This module is
state only.
"""

import logging
import time as _time
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import redis.asyncio as aioredis

from config import settings

logger = logging.getLogger(__name__)

# ── Namespace ────────────────────────────────────────────────────

AI_PREFIX = "tf:ai:"
STATE_PREFIX = f"{AI_PREFIX}state:"
COOLDOWN_PREFIX = f"{AI_PREFIX}cooldown:"

# The one source name this service cools down. Source-wide, never per model:
# a 429 or a 402 is account-level, exactly like Finnhub's in risk-shield.
SOURCE_LLM = "openrouter"

# State names.
STATE_LLM_CALLS = "llm_calls"

# The day counter's TTL: 36 h, long enough that a DST day cannot expire its
# own key early, short enough that yesterday's keys disappear on their own.
TTL_DAY_COUNTER = 129600

# Every daily boundary in this repo is ET (risk-shield's sessions, 3.6b's
# brief slots). A UTC day would reset the cap at 20:00 ET, mid after-hours.
ET = ZoneInfo("America/New_York")


# ── Keys ─────────────────────────────────────────────────────────

def canonical(name: str) -> str:
    """The one normalization every `tf:ai:` key goes through (G1.5), the same
    function risk-shield's cache.py exposes: strip + upper."""
    if not isinstance(name, str):
        raise ValueError(f"key name must be a string, got {type(name).__name__}")
    return name.strip().upper()


def et_day(now: Optional[datetime] = None) -> str:
    """The ET calendar date as YYYY-MM-DD. The one place the day boundary is
    decided; nothing else calls date() on a timestamp.

    A naive `now` is read as UTC, which is what datetime.utcnow()-shaped
    callers mean, and what the tests pin.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(ET).strftime("%Y-%m-%d")


def state_key(name: str) -> str:
    """Key for one piece of ai-agent state: `tf:ai:state:{name}`. The name
    goes through canonical() and is lower-cased, so " Llm_Calls " and
    "llm_calls" are one key (G1.5)."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("state key name must be a non-empty string")
    return f"{STATE_PREFIX}{canonical(name).lower()}"


def day_counter_key(now: Optional[datetime] = None) -> str:
    """`tf:ai:state:llm_calls:{YYYY-MM-DD}` on the ET day. Built from
    state_key + et_day so there is exactly one spelling of it."""
    return f"{state_key(STATE_LLM_CALLS)}:{et_day(now)}"


def cooldown_key(name: str) -> str:
    """`tf:ai:cooldown:{NAME}`, the name through canonical() (G1.5)."""
    return f"{COOLDOWN_PREFIX}{canonical(name)}"


# ── Connection ───────────────────────────────────────────────────

async def create_redis(url: str = None, timeout: float = None) -> aioredis.Redis:
    """Create and return an async Redis connection, verified with a PING.

    `timeout` bounds the socket connect and each socket read (redis-py's
    defaults are unbounded). risk-shield's create_redis, unchanged.
    """
    import config

    if timeout is None:
        timeout = config.STARTUP_TIMEOUT
    url = url or settings.redis_url
    logger.info(f"Connecting to Redis: {url}")
    client = aioredis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=timeout,
        socket_timeout=timeout,
    )
    await client.ping()
    logger.info("Redis connection established")
    return client


# ── In-process fallbacks ─────────────────────────────────────────

class MemoryCooldowns:
    """In-memory cooldown clock used when Redis is absent or raising.

    risk-shield's class plus the *cause*: an hour of silence because the
    account is out of credits must not read as a rate limit. Lost on restart.
    """

    def __init__(self):
        self._started: dict[str, tuple[float, str]] = {}

    def remaining(self, name: str, ttl: int) -> tuple[Optional[int], Optional[str]]:
        entry = self._started.get(name)
        if entry is None:
            return None, None
        started, cause = entry
        elapsed = _time.time() - started
        if elapsed < ttl:
            return int(ttl - elapsed), cause
        return None, None

    def start(self, name: str, cause: str) -> None:
        self._started[name] = (_time.time(), cause)


class MemoryCap:
    """In-process daily call counter, the shape of MemoryCooldowns and used
    for the same reason: a Redis blip must not take the analyst down.

    Keyed by the ET day, so it rolls over like the Redis key does. It bounds a
    runaway loop inside *this* process only, and a restart clears it — which
    is the fail-open half of the bargain, stated in spec 4.1 decision 4.
    """

    def __init__(self):
        self._counts: dict[str, int] = {}

    def incr(self, day: str) -> int:
        self._counts[day] = self._counts.get(day, 0) + 1
        return self._counts[day]

    def decr(self, day: str) -> int:
        self._counts[day] = max(0, self._counts.get(day, 0) - 1)
        return self._counts[day]

    def count(self, day: str) -> int:
        return self._counts.get(day, 0)


# ── The daily call cap ───────────────────────────────────────────

async def reserve_call(
    r: Optional[aioredis.Redis],
    memory: MemoryCap,
    now: Optional[datetime] = None,
) -> int:
    """INCR today's counter and return the new count — the reservation.

    `EXPIRE key 129600 NX` sets the TTL only when the key has none, so a later
    call in the same day cannot push the expiry forward (Redis >= 7.0; prod
    runs 7.4). Redis absent or raising falls back to the in-process counter
    with one WARNING, and the caller sees the same integer either way.
    """
    day = et_day(now)
    if r is not None:
        try:
            key = day_counter_key(now)
            count = await r.incr(key)
            await r.expire(key, TTL_DAY_COUNTER, nx=True)
            return int(count)
        except Exception as e:
            logger.warning(f"LLM call counter INCR failed, using in-process count: {e}")
    return memory.incr(day)


async def release_call(
    r: Optional[aioredis.Redis],
    memory: MemoryCap,
    now: Optional[datetime] = None,
) -> None:
    """DECR today's counter. Called from exactly one place: the over-cap
    refusal, which sent no HTTP request.

    It mirrors reserve_call exactly, including the fallback: a raise here
    means the matching INCR almost certainly went to the in-process counter
    too, so that is what gets decremented. If instead Redis took the INCR and
    then failed the DECR, the Redis key reads one *higher* than the number of
    calls actually made — the cap bites one call early, which is logged and
    clears at the next ET midnight. Decrementing memory in that case is a
    no-op, because MemoryCap never goes below zero."""
    day = et_day(now)
    if r is not None:
        try:
            await r.decr(day_counter_key(now))
            return
        except Exception as e:
            logger.warning(f"LLM call counter DECR failed, day may read one call high: {e}")
    memory.decr(day)


# ── Cooldowns ────────────────────────────────────────────────────

async def cooldown_remaining(
    r: Optional[aioredis.Redis],
    memory: Optional[MemoryCooldowns],
    name: str,
    ttl: int,
) -> tuple[Optional[int], Optional[str]]:
    """(seconds left, cause) for `name`'s cooldown, or (None, None) if clear.

    The cause is the HTTP status that started it ("429", "401", "402", "403"),
    stored as the key's value. Redis is the source of truth; if it is absent
    or the read raises, the in-memory clock answers (fail-open to a shorter
    memory of refusals, never to a hard failure).
    """
    if r is not None:
        try:
            key = cooldown_key(name)
            left = await r.ttl(key)
            if left is None or left < 0:
                return None, None
            cause = await r.get(key)
        except Exception as e:
            logger.warning(f"Cooldown check failed for {name}: {e}")
        else:
            return left, (cause or None)
    if memory is None:
        return None, None
    return memory.remaining(canonical(name), ttl)


async def start_cooldown(
    r: Optional[aioredis.Redis],
    memory: Optional[MemoryCooldowns],
    name: str,
    ttl: int,
    cause: str,
) -> None:
    """Start `name`'s cooldown window, recording the status that caused it.
    Call only after the source actually refused us."""
    if r is not None:
        try:
            await r.set(cooldown_key(name), cause, ex=ttl)
            return
        except Exception as e:
            logger.warning(f"Cooldown write failed for {name}: {e}")
    if memory is not None:
        memory.start(canonical(name), cause)
