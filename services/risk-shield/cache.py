"""
TradingFirm — Risk Shield Redis Cache Layer

Key namespace, TTLs, the fail-open read-through cache and the health
pub/sub channel for Phase 3.

The pattern is data-engine's `cache.py` (Part 3.1 copies it deliberately —
separate images, no shared package), with one difference that matters: every
key here lives under `tf:risk:`, never `tf:cache:`, which data-engine owns.
Both services share Redis DB 0 in prod.
"""

import json
import logging
import time as _time
from typing import Any, Awaitable, Callable, Optional

import redis.asyncio as aioredis

from config import settings

logger = logging.getLogger(__name__)

# ── Namespace ────────────────────────────────────────────────────

# Everything this service writes starts here. data-engine owns tf:cache:*.
RISK_PREFIX = "tf:risk:"
CACHE_PREFIX = f"{RISK_PREFIX}cache:"

# State that is not a cache (Part 3.4): it must outlive any cached body.
STATE_PREFIX = f"{RISK_PREFIX}state:"
# The health pub/sub channel lives in config.settings.health_channel (3.4):
# the dev twin overrides it, because pub/sub ignores the Redis DB index.

# Cache kinds, shipped now and used by their part.
KIND_QUOTES = "quotes"   # 3.2: the batched core-ticker download
KIND_FRED = "fred"       # 3.2: one FRED series
# 3.3: the last *full* quotes body, served with stale: true when the source
# refuses, cools down or comes back degraded (spec 3.3 decision 2).
KIND_QUOTES_LAST = "quotes_last"
KIND_NIGHT_QUOTES = "night_quotes"   # 3.4b: ES=F / NQ=F for a night check

# TTLs (seconds)
TTL_QUOTES = 300     # 5 min (plan 3.2)
TTL_FRED = 21600     # 6 hours (plan 3.2)
# A body that came back degraded (envelope `reason` not null: "empty",
# "partial") is cached briefly, never for the source's full window: one
# transient empty FRED answer must not blank a series for six hours (Part
# 3.2 decision 3). Same number as data-engine's TTL_DOSSIER_ERROR.
TTL_DEGRADED = 120
TTL_NIGHT_QUOTES = 600   # under the 30-min night slot, so every slot downloads fresh (3.4b)
TTL_LAST_KNOWN = 86400   # 24 h: how long a last-known quotes body may stand in
# 3.6a: the last *full* envelope per FRED series, served with stale: true when
# the source refuses, cools down, errors or answers empty (spec 3.6a decision
# 3). 7 days, provisional: freshness is judged on observation dates, never on
# this key's age, and 24 h would erase a monthly series after one bad day.
KIND_FRED_LAST = "fred_last"
TTL_FRED_LAST_KNOWN = 7 * 86400

# 3.4: the last published health {score, regime, publishedAt}, which the
# throttle compares against. 7 days so a long-dead state cannot linger.
STATE_HEALTH_PUBLISHED = "health_published"
TTL_HEALTH_PUBLISHED = 7 * 86400

# Source cooldowns (Part 3.2 decision 4, copied from data-engine 2.4): set
# after a source refuses us, checked before any request. Source-wide — one
# name per source, never per ticker or per series.
COOLDOWN_PREFIX = f"{RISK_PREFIX}cooldown:"
SOURCE_YFINANCE = "yfinance"
SOURCE_FRED = "fred"
TTL_COOLDOWN_YFINANCE = 900      # rate limit, or a whole download empty
TTL_COOLDOWN_FRED = 900          # 429 / 423
TTL_COOLDOWN_FRED_AUTH = 3600    # 400 naming api_key
SOURCE_FINNHUB = "finnhub"       # 3.5: market news
TTL_COOLDOWN_FINNHUB = 900       # 429
TTL_COOLDOWN_FINNHUB_AUTH = 3600 # 401 / 403

# data-engine's own Finnhub cooldown (its cache.cooldown_key(SOURCE_FINNHUB),
# set for 60 s after a dossier 429). A 429 is account-level, so the news
# poller reads this key before calling Finnhub: read-only, never written,
# absent or unreadable = clear (spec 3.5 decision 4). data-engine's
# test_finnhub_cooldown_key_is_pinned_for_risk_shield pins the other end.
DATA_ENGINE_FINNHUB_COOLDOWN_KEY = "tf:cache:finnhub"


# ── Keys ─────────────────────────────────────────────────────────

def canonical(name: str) -> str:
    """
    The one normalization every Phase 3 key goes through (G1.5): tickers
    (`spy` → `SPY`) and FRED series ids (` dgs10 ` → `DGS10`) are the same
    kind of name, so they get the same treatment and cannot produce two
    keys for one thing.
    """
    if not isinstance(name, str):
        raise ValueError(f"key name must be a string, got {type(name).__name__}")
    return name.strip().upper()


def risk_key(kind: str, name: str = "") -> str:
    """
    Cache key for one cached thing: `tf:risk:cache:{kind}`, or
    `tf:risk:cache:{kind}:{name}` when the thing is per-name. `kind` is a
    module constant, `name` goes through canonical().
    """
    if not isinstance(kind, str) or not kind.strip():
        raise ValueError("cache key kind must be a non-empty string")
    kind = kind.strip().lower()
    if not name:
        return f"{CACHE_PREFIX}{kind}"
    return f"{CACHE_PREFIX}{kind}:{canonical(name)}"


def state_key(name: str) -> str:
    """
    Key for one piece of risk-shield state: `tf:risk:state:{name}`. The
    name goes through canonical() and is lower-cased, like risk_key's kind,
    so " Health_Published " and "health_published" are one key (G1.5).
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("state key name must be a non-empty string")
    return f"{STATE_PREFIX}{canonical(name).lower()}"


# ── Connection ───────────────────────────────────────────────────

async def create_redis(url: str = None, timeout: float = None) -> aioredis.Redis:
    """
    Create and return an async Redis connection, verified with a PING.

    `timeout` bounds the socket connect and each socket read (redis-py's
    defaults are unbounded). The lifespan additionally wraps this whole
    call in asyncio.wait_for — the hard bound (Part 3.1 decision 6).
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


# ── Cooldowns (sources per name) ─────────────────────────────────

class MemoryCooldowns:
    """In-memory cooldown clock used when Redis is absent or raising.

    Copied from data-engine's cache.py (Part 2.4), same semantics. Names
    are source names ("yfinance", "fred"). Lost on restart.
    """

    def __init__(self):
        self._started: dict[str, float] = {}

    def remaining(self, name: str, ttl: int) -> Optional[int]:
        started = self._started.get(name)
        if started is None:
            return None
        elapsed = _time.time() - started
        if elapsed < ttl:
            return int(ttl - elapsed)
        return None

    def start(self, name: str) -> None:
        self._started[name] = _time.time()


def cooldown_key(name: str) -> str:
    """Redis key for one source cooldown: `tf:risk:cooldown:{NAME}`. The
    name goes through canonical(), the one normalizer (G1.5)."""
    return f"{COOLDOWN_PREFIX}{canonical(name)}"


async def cooldown_remaining(
    r: Optional[aioredis.Redis],
    memory: Optional[MemoryCooldowns],
    name: str,
    ttl: int,
) -> Optional[int]:
    """
    Seconds left on `name`'s cooldown, or None if it is clear now.

    Redis is the source of truth; if it is absent or the read raises, the
    in-memory clock answers (fail-open to a shorter memory of refusals,
    never to a hard failure).
    """
    if r is not None:
        try:
            left = await r.ttl(cooldown_key(name))
        except Exception as e:
            logger.warning(f"Cooldown check failed for {name}: {e}")
        else:
            if left is None or left < 0:
                return None
            return left
    if memory is None:
        return None
    return memory.remaining(canonical(name), ttl)


async def start_cooldown(
    r: Optional[aioredis.Redis],
    memory: Optional[MemoryCooldowns],
    name: str,
    ttl: int,
) -> None:
    """
    Start `name`'s cooldown window. Call only after the source actually
    refused us. A Redis write that raises falls back to the in-memory clock.
    """
    if r is not None:
        try:
            await r.set(cooldown_key(name), "1", ex=ttl)
            return
        except Exception as e:
            logger.warning(f"Cooldown write failed for {name}: {e}")
    if memory is not None:
        memory.start(canonical(name))


# ── Generic JSON cache ───────────────────────────────────────────

async def get_cached_json(r: aioredis.Redis, key: str) -> Optional[Any]:
    """Decoded JSON at `key`, or None on a miss. A stored value that is not
    valid JSON is treated as a miss and logged (the caller recomputes and
    overwrites it)."""
    data = await r.get(key)
    if data is None:
        return None
    try:
        return json.loads(data)
    except (TypeError, ValueError) as e:
        logger.warning(f"Cache at {key} is not valid JSON, ignoring: {e}")
        return None


async def set_cached_json(r: aioredis.Redis, key: str, body: Any, ttl: int) -> None:
    """Cache any JSON-serializable body at `key` with TTL."""
    await r.set(key, json.dumps(body, default=str), ex=ttl)


async def cached_json(
    r: Optional[aioredis.Redis],
    key: str,
    ttl: Optional[int],
    fetch: Callable[[], Awaitable[Any]],
    *,
    valid: Optional[Callable[[Any], bool]] = None,
    ttl_for: Optional[Callable[[Any], int]] = None,
) -> tuple[Any, bool]:
    """
    Read-through cache: return (body, from_cache). On a miss, `fetch()`
    runs and its result is cached for `ttl` seconds. A cached body that
    fails `valid` (wrong shape) is a miss too: logged, refetched,
    overwritten. Fail-open on Redis — `r` may be None, and a raise on get
    or set is logged and ignored so the caller still gets a body. A raise
    inside `fetch()` propagates and nothing is cached.

    An empty body ([] or {}) is an answer, not a miss: it is cached and
    returned as-is. Only a literal absence is a miss — which is why a
    stored JSON `null` reads as a miss (refetched once, overwritten) and
    why `fetch()` returning None raises TypeError instead of being cached
    (Part 3.2 decision 2): a None written here would be refetched on every
    call and the cache would silently stop protecting the source.

    `ttl_for` (restored from data-engine, Part 3.2 decision 3) decides the
    TTL from the computed body and wins over `ttl`; `ttl` may then be None.
    """
    if r is not None:
        try:
            body = await get_cached_json(r, key)
        except Exception as e:
            logger.warning(f"Cache read failed for {key}: {e}")
            body = None
        if body is not None and valid is not None and not valid(body):
            logger.warning(f"Cache at {key} has the wrong shape, ignoring")
            body = None
        if body is not None:
            return body, True

    body = await fetch()
    if body is None:
        raise TypeError(f"fetch for {key} returned None; fetchers return an envelope")

    if r is not None:
        write_ttl = ttl_for(body) if ttl_for is not None else ttl
        try:
            await set_cached_json(r, key, body, write_ttl)
        except Exception as e:
            logger.warning(f"Cache write failed for {key}: {e}")
    return body, False
