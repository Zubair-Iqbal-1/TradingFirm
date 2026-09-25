"""
TradingFirm — Data Engine Redis Cache Layer

Handles scan result caching, scan status tracking,
and pub/sub event publishing for inter-service communication.
"""

import json
import logging
import time as _time
from typing import Any, Awaitable, Callable, Optional

import redis.asyncio as aioredis

from config import settings

logger = logging.getLogger(__name__)

# Cache keys (matching shared/constants.py)
CACHE_LAST_SCAN = "tf:cache:last_scan"
CACHE_SCAN_STATUS = "tf:cache:scan_status"
# One prefix for every cooldown (Part 2.4). "refresh:<T>" keeps the
# Part 1.2 key `tf:cache:refresh:<T>` unchanged; sources add "edgar",
# "finnhub", "alphavantage".
CACHE_COOLDOWN_PREFIX = "tf:cache:"
CACHE_INDICATORS_PREFIX = "tf:cache:indicators:"
CACHE_FINNHUB_PREFIX = "tf:cache:finnhub:"
CACHE_EDGAR_PREFIX = "tf:cache:edgar:"

# Pub/sub channels
CHANNEL_SCAN_COMPLETE = "tf:scan:complete"

# TTLs (seconds)
TTL_SCAN_RESULT = 3600      # 1 hour
TTL_SCAN_STATUS = 600       # 10 minutes
TTL_REFRESH_COOLDOWN = 900  # 15 minutes
TTL_INDICATORS = 900        # 15 minutes
TTL_FINNHUB_NEWS = 900      # 15 minutes
TTL_FINNHUB_CONTEXT = 86400 # 24 hours (recommendations, earnings, profile)
TTL_EDGAR_CIK_MAP = 86400   # 24 hours (ticker → CIK map, plan 2.2)
TTL_EDGAR_FILINGS = 900     # 15 minutes (recent filings per ticker, same as news)

# Source cooldowns (Part 2.4): set after a source refuses us, checked before
# any HTTP to that source. Names are also the `bySource` keys of the dossier
# budget. EDGAR's window is long because a 403 means blocked, not throttled.
SOURCE_FINNHUB = "finnhub"
SOURCE_EDGAR = "edgar"
SOURCE_ALPHAVANTAGE = "alphavantage"
TTL_COOLDOWN_FINNHUB = 60         # the width of Finnhub's per-minute window
# risk-shield's news poller reads this source's key, tf:cache:finnhub, before
# it calls Finnhub (spec 3.5 decision 4). Renaming it means changing
# risk-shield's DATA_ENGINE_FINNHUB_COOLDOWN_KEY too; tests/test_cooldowns.py
# pins it.
TTL_COOLDOWN_EDGAR = 900          # 403 = blocked: stop, do not poke it
TTL_COOLDOWN_ALPHAVANTAGE = 3600  # the free tier's daily cap, seen as HTTP 200

# Dossier documents (Part 2.4).
TTL_DOSSIER_MARKET = 900          # 15 min while the market is open
TTL_DOSSIER_CLOSED = 3600         # 60 min outside market hours
TTL_DOSSIER_ERROR = 120           # a document with a failed section: 2 min
CACHE_DOSSIER_PREFIX = "tf:cache:dossier:"
# Part 4.8b-de: the open session so far, per ticker, TTL to the close.
CACHE_SESSION_PREFIX = "tf:cache:session:"


# ── Connection ───────────────────────────────────────────────────

async def create_redis(url: str = None) -> aioredis.Redis:
    """Create and return an async Redis connection."""
    url = url or settings.redis_url
    logger.info(f"Connecting to Redis: {url}")
    client = aioredis.from_url(url, decode_responses=True)
    # Verify connection
    await client.ping()
    logger.info("Redis connection established")
    return client


# ── Scan Result Cache ────────────────────────────────────────────

async def cache_scan_result(
    r: aioredis.Redis,
    scan_result: dict,
    ttl: int = TTL_SCAN_RESULT,
) -> None:
    """Cache the latest scan result as JSON with TTL."""
    await r.set(
        CACHE_LAST_SCAN,
        json.dumps(scan_result, default=str),
        ex=ttl,
    )
    logger.info(f"Cached scan result ({len(scan_result.get('stocks', []))} stocks, TTL={ttl}s)")


async def get_cached_scan(r: aioredis.Redis) -> Optional[dict]:
    """Retrieve cached scan result. Returns None if cache miss."""
    data = await r.get(CACHE_LAST_SCAN)
    if data is None:
        logger.debug("Cache miss: no cached scan result")
        return None
    logger.debug("Cache hit: returning cached scan result")
    return json.loads(data)


# ── Scan Status Tracking ────────────────────────────────────────

async def set_scan_status(
    r: aioredis.Redis,
    status: str,
    message: str = "",
) -> None:
    """
    Track whether a scan is currently running.
    Status: 'idle' | 'running' | 'completed' | 'failed'
    """
    payload = json.dumps({"status": status, "message": message})
    await r.set(CACHE_SCAN_STATUS, payload, ex=TTL_SCAN_STATUS)
    logger.info(f"Scan status: {status} — {message}")


async def get_scan_status(r: aioredis.Redis) -> dict:
    """Get current scan status. Defaults to 'idle' if not set."""
    data = await r.get(CACHE_SCAN_STATUS)
    if data is None:
        return {"status": "idle", "message": ""}
    return json.loads(data)


# ── Cooldowns (refresh per ticker, sources per name) ─────────────

class MemoryCooldowns:
    """In-memory cooldown clock used when Redis is absent or raising.

    Moved out of main.py's InMemoryStore in Part 2.4 so the same pair of
    helpers serves the per-ticker refresh cooldown (Part 1.2) and the
    per-source cooldowns the dossier sets after a refusal (Part 2.4).
    Names are opaque: "refresh:AAPL", "edgar", "finnhub".
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
    """Redis key for one cooldown. `name` is already canonical (a
    normalized ticker for "refresh:<T>", a source name otherwise)."""
    return f"{CACHE_COOLDOWN_PREFIX}{name}"


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
    return memory.remaining(name, ttl)


async def start_cooldown(
    r: Optional[aioredis.Redis],
    memory: Optional[MemoryCooldowns],
    name: str,
    ttl: int,
) -> None:
    """
    Start `name`'s cooldown window. Call only after the thing the cooldown
    protects actually happened — a successful refresh (Part 1.2), or a
    refusal from a source (Part 2.4). A Redis write that raises falls back
    to the in-memory clock.
    """
    if r is not None:
        try:
            await r.set(cooldown_key(name), "1", ex=ttl)
            return
        except Exception as e:
            logger.warning(f"Cooldown write failed for {name}: {e}")
    if memory is not None:
        memory.start(name)


def refresh_cooldown_name(ticker: str) -> str:
    """Cooldown name for one ticker's bar refresh. Keeps the Part 1.2 key
    (`tf:cache:refresh:<T>`) byte-for-byte."""
    return f"refresh:{ticker}"


# ── Per-Ticker Indicator Snapshot (Part 1.7) ─────────────────────

def indicators_key(ticker: str) -> str:
    """Cache key for one ticker's indicator snapshot. Caller passes the
    normalized ticker (tickers.normalize_ticker)."""
    return f"{CACHE_INDICATORS_PREFIX}{ticker}"


async def get_cached_indicators(r: aioredis.Redis, ticker: str) -> Optional[dict]:
    """
    Cached indicator body for `ticker`, or None on a miss. A stored value
    that is not valid JSON (or not a JSON object) is treated as a miss and
    logged — the caller recomputes and overwrites it.
    """
    data = await r.get(indicators_key(ticker))
    if data is None:
        return None
    try:
        body = json.loads(data)
    except (TypeError, ValueError) as e:
        logger.warning(f"Indicators cache for {ticker} is not valid JSON, ignoring: {e}")
        return None
    if not isinstance(body, dict):
        logger.warning(f"Indicators cache for {ticker} is not an object, ignoring")
        return None
    return body


async def set_cached_indicators(
    r: aioredis.Redis,
    ticker: str,
    body: dict,
    ttl: int = TTL_INDICATORS,
) -> None:
    """Cache one ticker's indicator body (JSON-ready dict) with TTL."""
    await r.set(indicators_key(ticker), json.dumps(body, default=str), ex=ttl)


async def delete_cached_indicators(r: aioredis.Redis, ticker: str) -> int:
    """Drop the cached snapshot (after a refresh wrote new bars). Returns
    the number of keys removed (0 or 1)."""
    return int(await r.delete(indicators_key(ticker)))


# ── The open session so far (Part 4.8b-de) ───────────────────────

def session_key(ticker: str) -> str:
    """Cache key for one ticker's sessionSoFar block (normalized ticker)."""
    return f"{CACHE_SESSION_PREFIX}{ticker}"


async def set_session_so_far(r: aioredis.Redis, ticker: str, block: dict, ttl: int) -> None:
    await r.set(session_key(ticker), json.dumps(block), ex=ttl)


async def get_session_so_far(r: aioredis.Redis, ticker: str) -> Optional[dict]:
    """The stashed block, or None on a miss or a body that is not an object."""
    data = await r.get(session_key(ticker))
    if data is None:
        return None
    try:
        body = json.loads(data)
    except (TypeError, ValueError):
        logger.warning(f"sessionSoFar cache for {ticker} is not valid JSON, ignoring")
        return None
    return body if isinstance(body, dict) else None


# ── Dossier documents (Part 2.4) ─────────────────────────────────

def dossier_key(ticker: str, horizon: str) -> str:
    """Cache key for one ticker's dossier at one horizon. Both parts are
    already canonical: the ticker through tickers.validate_ticker, the
    horizon through the endpoint's enum check."""
    return f"{CACHE_DOSSIER_PREFIX}{horizon}:{ticker}"


def dossier_ttl(body: Any, market_open: bool) -> int:
    """How long this document may be served. A dossier assembled with a
    failed section is cached for 2 minutes, not for the full window: a
    broken source must not be pinned for an hour."""
    from dossier.assemble import has_error_section  # deferred: imports models
    if has_error_section(body):
        return TTL_DOSSIER_ERROR
    return TTL_DOSSIER_MARKET if market_open else TTL_DOSSIER_CLOSED


def valid_dossier(body: Any) -> bool:
    """A cached body that is not a dossier is a miss (recomputed and
    overwritten), the get_cached_indicators() rule."""
    return isinstance(body, dict) and isinstance(body.get("sections"), dict)


# ── Generic JSON cache (Part 2.1) ────────────────────────────────

def finnhub_key(kind: str, ticker: str) -> str:
    """Cache key for one Finnhub fetcher result. Caller passes the
    normalized ticker (tickers.normalize_ticker)."""
    return f"{CACHE_FINNHUB_PREFIX}{kind}:{ticker}"


async def get_cached_json(r: aioredis.Redis, key: str) -> Optional[Any]:
    """Decoded JSON at `key`, or None on a miss. A stored value that is
    not valid JSON is treated as a miss and logged (caller recomputes and
    overwrites). Unlike get_cached_indicators(), any JSON value is
    accepted — Finnhub bodies are lists as often as objects."""
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
    Read-through cache shared by the context fetchers: return
    (body, from_cache). On a miss, `fetch()` runs and its result is cached
    for `ttl` seconds. A cached body that fails `valid` (wrong shape) is a
    miss too: logged, refetched, overwritten — the get_cached_indicators()
    rule (Part 2.2). Fail-open on Redis: `r` may be None, and a raise on
    get or set is logged and ignored so the caller still gets a body. A
    raise inside `fetch()` propagates and nothing is cached.

    `ttl_for` (Part 2.4) decides the TTL *from the computed body*, for
    callers whose window depends on what came back — the dossier caches a
    document with a failed section for 2 minutes and a whole one for 15 or
    60. It wins over `ttl` when given; `ttl` may then be None.
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

    if r is not None:
        write_ttl = ttl_for(body) if ttl_for is not None else ttl
        try:
            await set_cached_json(r, key, body, write_ttl)
        except Exception as e:
            logger.warning(f"Cache write failed for {key}: {e}")
    return body, False


# ── EDGAR keys (Part 2.2) ─────────────────────────────────────────

EDGAR_CIK_MAP_KEY = f"{CACHE_EDGAR_PREFIX}cik_map"


def edgar_key(kind: str, ticker: str) -> str:
    """Cache key for one EDGAR per-ticker result (kind: 'filings'). Caller
    passes the normalized ticker (tickers.normalize_ticker)."""
    return f"{CACHE_EDGAR_PREFIX}{kind}:{ticker}"


# ── Pub/Sub Events ───────────────────────────────────────────────

async def publish_scan_complete(
    r: aioredis.Redis,
    scan_result: dict,
) -> int:
    """
    Publish scan completion event for other services.
    Signal Engine subscribes to this to auto-generate signals.
    Returns number of subscribers that received the message.
    """
    payload = json.dumps({
        "event": "scan_complete",
        "market_status": scan_result.get("market_status", ""),
        "passed_count": scan_result.get("passed_count", 0),
        "timestamp": scan_result.get("timestamp", ""),
    }, default=str)
    receivers = await r.publish(CHANNEL_SCAN_COMPLETE, payload)
    logger.info(f"Published scan_complete event ({receivers} subscribers)")
    return receivers
