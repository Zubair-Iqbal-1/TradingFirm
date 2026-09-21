"""
TradingFirm — AI Agent Redis Layer (Parts 4.1, 4.2)

The `tf:ai:` namespace, the daily LLM call counter and the provider
cooldown, plus the in-process fallbacks used when Redis is absent or
raising.

The pattern is risk-shield's `cache.py`, which copied data-engine's (separate
images, no shared package), with one difference that matters: every key here
lives under `tf:ai:`. `tf:cache:` is data-engine's and `tf:risk:` is
risk-shield's; all three share Redis DB 0 in prod.

Part 4.2 adds the first cached thing — one classification per headline
digest — plus the classifier's own day counter and the running cost totals.
"""

import hashlib
import json
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
STATE_CLASSIFIER_CALLS = "classifier_calls"       # Part 4.2's second cap
STATE_COST_DAY = "cost_day"
STATE_COST_MONTH = "cost_month"
STATE_COST_MISSING = "cost_missing"
STATE_LEDGER_MISSED = "ledger_missed"             # Part 4.4: rows ai.llm_calls lacks

# Part 4.2: the classification cache, `tf:ai:classify:{digest}`.
CLASSIFY_PREFIX = f"{AI_PREFIX}classify:"

# Part 4.4: the per-ticker verdict cache and /analyze's in-flight lock.
VERDICT_PREFIX = f"{AI_PREFIX}verdict:"
LOCK_PREFIX = f"{AI_PREFIX}lock:analyze:"

# Longer than the provider's hard 150 s per-call bound, so a lock never
# expires under a call that is still running; short enough that a crashed
# request blocks its one key for a few minutes at most.
TTL_ANALYZE_LOCK = 200

# The day counter's TTL: 36 h, long enough that a DST day cannot expire its
# own key early, short enough that yesterday's keys disappear on their own.
# This is for the two *reservation* counters, which must disappear.
TTL_DAY_COUNTER = 129600

# The cost keys' TTL: 40 days (Part 4.2). Long enough to hold a full month of
# day-by-day totals beside the month-to-date figure, which is what an admin
# panel will chart later. cost_missing shares it so a historical day's total
# is never read without its caveat.
TTL_COST = 3456000

# The classification cache's TTL: 7 days. That is data-engine's
# NEWS_MARKET_MAX_HOURS (168 h), so nothing the market-news window can return
# is ever re-sent to the model.
TTL_CLASSIFY = 604800

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


def et_month(now: Optional[datetime] = None) -> str:
    """The ET calendar month as YYYY-MM, on the same boundary as et_day so a
    month never rolls over at a different instant than its last day."""
    return et_day(now)[:7]


def day_counter_key(now: Optional[datetime] = None, *, name: str = STATE_LLM_CALLS) -> str:
    """`tf:ai:state:{name}:{YYYY-MM-DD}` on the ET day. Built from state_key +
    et_day so there is exactly one spelling of it.

    `name` is keyword-only and defaults to 4.1's single counter, so every
    existing positional caller is unchanged. Part 4.2 passes
    STATE_CLASSIFIER_CALLS to get the classifier's own cap on the very same
    code path, and the cost counters reuse it too.
    """
    return f"{state_key(name)}:{et_day(now)}"


def month_counter_key(now: Optional[datetime] = None, *, name: str = STATE_COST_MONTH) -> str:
    """`tf:ai:state:{name}:{YYYY-MM}` on the ET month (Part 4.2)."""
    return f"{state_key(name)}:{et_month(now)}"


def headline_digest(title: str, url: Optional[str] = None) -> str:
    """The one normalization every headline key goes through (G1.5).

    The url identifies the article when there is one — two feeds quoting the
    same story under different titles are one digest — and the title is the
    fallback when there is not. Both go through canonical(), so trailing
    whitespace or a case difference cannot split one headline into two paid
    classifications.

    It does NOT detect the same *event* reported by Reuters, CNBC and
    Bloomberg: three urls are three digests and three payments. That is a
    known limitation, accepted at this volume, and its fix belongs to dossier
    assembly in Part 4.4, not here (spec 4.2 decision 20).
    """
    if not isinstance(title, str) or not title.strip():
        raise ValueError("headline title must be a non-empty string")
    if url is not None and not isinstance(url, str):
        raise ValueError("headline url must be a string or None")
    basis = canonical(url) if url and url.strip() else canonical(title)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def classify_key(digest: str) -> str:
    """`tf:ai:classify:{digest}`, the digest exactly as headline_digest built
    it — the only function that may produce one."""
    if not isinstance(digest, str) or not digest.strip():
        raise ValueError("digest must be a non-empty string")
    return f"{CLASSIFY_PREFIX}{digest.strip()}"


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

    One instance per counter, never one shared instance with namespaced keys
    (Part 4.2): the provider owns the global cap's fallback and the classifier
    owns its own. Two objects cost nothing and keep the key space trivially
    correct — a namespaced key here would only re-encode what the owning
    object already says.
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

    def seed(self, day: str, count: int) -> int:
        """Raise the day's count to `count`, never lower it (Part 4.4)."""
        self._counts[day] = max(self._counts.get(day, 0), int(count))
        return self._counts[day]


# ── The daily call cap ───────────────────────────────────────────

async def reserve_call(
    r: Optional[aioredis.Redis],
    memory: MemoryCap,
    now: Optional[datetime] = None,
    *,
    name: str = STATE_LLM_CALLS,
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
            key = day_counter_key(now, name=name)
            count = await r.incr(key)
            await r.expire(key, TTL_DAY_COUNTER, nx=True)
            return int(count)
        except Exception as e:
            logger.warning(f"{name} counter INCR failed, using in-process count: {e}")
    return memory.incr(day)


async def release_call(
    r: Optional[aioredis.Redis],
    memory: MemoryCap,
    now: Optional[datetime] = None,
    *,
    name: str = STATE_LLM_CALLS,
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
            await r.decr(day_counter_key(now, name=name))
            return
        except Exception as e:
            logger.warning(f"{name} counter DECR failed, day may read one call high: {e}")
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


# ── Running cost totals (Part 4.2) ───────────────────────────────
#
# OpenRouter reports `cost` on every answer, so the only work here is adding
# it up somewhere readable without logging into their dashboard. Three keys,
# all on the ET boundary and all kept 40 days (TTL_COST): today's total, this
# month's total, and a count of answers that carried no cost at all — so a
# total is never quietly read as complete when it is not.
#
# Every write here is best-effort. A cost that fails to record must never fail
# a classification the user has already paid for, so each path logs and
# returns. Redis persistence itself is weak (spec 4.2 decision 19): these
# figures are a floor, not an audit, until the housekeeping batch gives redis
# a declared volume and AOF.


class MemoryCost:
    """In-process cost totals, the fallback shape MemoryCap and
    MemoryCooldowns already use. Lost on restart, like the rest."""

    def __init__(self):
        self._totals: dict[str, float] = {}
        self._missing: dict[str, int] = {}

    def add(self, day: str, month: str, amount: float) -> None:
        self._totals[day] = self._totals.get(day, 0.0) + amount
        self._totals[month] = self._totals.get(month, 0.0) + amount

    def add_missing(self, day: str) -> None:
        self._missing[day] = self._missing.get(day, 0) + 1

    def total(self, period: str) -> float:
        return self._totals.get(period, 0.0)

    def seed(self, period: str, amount: float) -> None:
        """Raise a period's total to `amount`, never lower it (Part 4.4)."""
        self._totals[period] = max(self._totals.get(period, 0.0), float(amount))

    def missing(self, day: str) -> int:
        return self._missing.get(day, 0)


async def record_cost(
    r: Optional[aioredis.Redis],
    memory: MemoryCost,
    cost: Optional[float],
    now: Optional[datetime] = None,
) -> None:
    """Add one answer's reported cost to today's and this month's totals.

    `cost` None or unparseable counts as *missing*, never as zero: a gateway
    that stops reporting cost must show up as a caveat on the total rather
    than as a suspiciously cheap day.
    """
    day, month = et_day(now), et_month(now)
    amount = None
    if cost is not None:
        try:
            amount = float(cost)
        except (TypeError, ValueError):
            amount = None
    if amount is None or amount != amount or amount in (float("inf"), float("-inf")):
        if r is not None:
            try:
                key = day_counter_key(now, name=STATE_COST_MISSING)
                await r.incr(key)
                await r.expire(key, TTL_COST, nx=True)
                return
            except Exception as e:
                logger.warning(f"cost_missing INCR failed: {e}")
        memory.add_missing(day)
        return

    if r is not None:
        try:
            day_key = day_counter_key(now, name=STATE_COST_DAY)
            month_key = month_counter_key(now, name=STATE_COST_MONTH)
            await r.incrbyfloat(day_key, amount)
            await r.expire(day_key, TTL_COST, nx=True)
            await r.incrbyfloat(month_key, amount)
            await r.expire(month_key, TTL_COST, nx=True)
            return
        except Exception as e:
            logger.warning(f"cost total INCRBYFLOAT failed, totals under-report: {e}")
    memory.add(day, month, amount)


# ── Seeding from the ledger (Part 4.4) ───────────────────────────
#
# Redis has no durable persistence (spec 4.2 decision 19): a `docker compose
# down` resets today's counters to zero. At startup the ledger in Postgres
# says how many calls really went out today, and each counter is raised to
# that number when it is lower. Max of the two, never an overwrite — either
# side can be the short one (the ledger misses a row when its write failed).
#
# The keys are 4.1's and 4.2's own, written by their own rule: a relative
# INCRBY / INCRBYFLOAT of the shortfall, then `EXPIRE key ttl NX`. A plain
# SET would drop the key's TTL and the EXPIRE would then push the expiry
# forward, which is exactly what NX exists to prevent.
#
#   tf:ai:state:llm_calls:{YYYY-MM-DD}          TTL_DAY_COUNTER (129600) NX
#   tf:ai:state:classifier_calls:{YYYY-MM-DD}   TTL_DAY_COUNTER (129600) NX
#   tf:ai:state:cost_day:{YYYY-MM-DD}           TTL_COST (3456000) NX
#   tf:ai:state:cost_month:{YYYY-MM}            TTL_COST (3456000) NX

async def seed_counter(
    r: Optional[aioredis.Redis],
    memory: MemoryCap,
    target: int,
    now: Optional[datetime] = None,
    *,
    name: str = STATE_LLM_CALLS,
) -> int:
    """Raise today's `name` counter to `target` if it is lower. Returns the
    shortfall that was added to Redis (0 when Redis already had it, or is
    absent). The in-process counter is seeded either way, so a Redis that
    dies later does not restart the day at zero."""
    memory.seed(et_day(now), target)
    if r is None or target <= 0:
        return 0
    key = day_counter_key(now, name=name)
    current = int(await r.get(key) or 0)
    shortfall = int(target) - current
    if shortfall <= 0:
        return 0
    await r.incrby(key, shortfall)
    await r.expire(key, TTL_DAY_COUNTER, nx=True)
    return shortfall


async def seed_cost(
    r: Optional[aioredis.Redis],
    memory: MemoryCost,
    cost_day: float,
    cost_month: float,
    now: Optional[datetime] = None,
) -> None:
    """The same rule for today's and the month's USD totals."""
    memory.seed(et_day(now), cost_day)
    memory.seed(et_month(now), cost_month)
    if r is None:
        return
    for key, target in (
        (day_counter_key(now, name=STATE_COST_DAY), cost_day),
        (month_counter_key(now, name=STATE_COST_MONTH), cost_month),
    ):
        shortfall = float(target) - float(await r.get(key) or 0.0)
        if shortfall > 1e-9:
            await r.incrbyfloat(key, shortfall)
            await r.expire(key, TTL_COST, nx=True)


async def read_usage(
    r: Optional[aioredis.Redis],
    memory_caps: dict[str, MemoryCap],
    memory_cost: MemoryCost,
    now: Optional[datetime] = None,
) -> dict:
    """The numbers behind `GET /usage`: both day counters, the cost-missing
    count, and today's and this month's totals.

    Redis is the source of truth; if it is absent or the read raises, the
    in-process state answers and `source` says "memory" so a small number is
    never mistaken for a quiet day. `memory_caps` maps a state name to the
    MemoryCap that owns it.
    """
    day, month = et_day(now), et_month(now)
    if r is not None:
        try:
            values = await r.mget([
                day_counter_key(now, name=STATE_LLM_CALLS),
                day_counter_key(now, name=STATE_CLASSIFIER_CALLS),
                day_counter_key(now, name=STATE_COST_MISSING),
                day_counter_key(now, name=STATE_COST_DAY),
                month_counter_key(now, name=STATE_COST_MONTH),
                day_counter_key(now, name=STATE_LEDGER_MISSED),
            ])
        except Exception as e:
            logger.warning(f"usage read failed, answering from in-process state: {e}")
        else:
            llm, classifier, missing, cost_day, cost_month, ledger_missed = values
            return {
                "day": day, "month": month, "source": "redis",
                "llmCalls": int(llm or 0),
                "classifierCalls": int(classifier or 0),
                "costMissing": int(missing or 0),
                "costToday": round(float(cost_day or 0.0), 6),
                "costMonth": round(float(cost_month or 0.0), 6),
                "ledgerMissed": int(ledger_missed or 0),
            }
    return {
        "day": day, "month": month, "source": "memory",
        "llmCalls": memory_caps[STATE_LLM_CALLS].count(day),
        "classifierCalls": memory_caps[STATE_CLASSIFIER_CALLS].count(day),
        "costMissing": memory_cost.missing(day),
        "costToday": round(memory_cost.total(day), 6),
        "costMonth": round(memory_cost.total(month), 6),
        "ledgerMissed": 0,
    }


# ── The classification cache (Part 4.2) ──────────────────────────

async def get_classifications(
    r: Optional[aioredis.Redis],
    digests: list[str],
) -> dict[str, dict]:
    """{digest: classification} for the digests already stored.

    One MGET, so a batch of 30 costs one round trip. A miss, a raise or an
    unparseable value all read as "not classified": the cost of being wrong
    is one extra classification, and the cost of trusting a corrupt value is
    a wrong verdict.
    """
    if r is None or not digests:
        return {}
    try:
        values = await r.mget([classify_key(d) for d in digests])
    except Exception as e:
        logger.warning(f"classification cache read failed, treating all as misses: {e}")
        return {}
    found: dict[str, dict] = {}
    for digest, raw in zip(digests, values):
        if raw is None:
            continue
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("classification cache holds an unparseable value; treating as a miss")
            continue
        if isinstance(value, dict):
            found[digest] = value
    return found


async def store_classifications(
    r: Optional[aioredis.Redis],
    items: dict[str, dict],
) -> int:
    """Store {digest: classification} for TTL_CLASSIFY. Returns how many were
    written. Best-effort: a failure means the headline is classified again on
    a later call, which costs one call and stores nothing wrong."""
    if r is None or not items:
        return 0
    written = 0
    for digest, value in items.items():
        try:
            await r.set(classify_key(digest), json.dumps(value), ex=TTL_CLASSIFY)
            written += 1
        except Exception as e:
            logger.warning(f"classification cache write failed for one item: {e}")
    return written


# ── The verdict cache and the in-flight lock (Part 4.4) ──────────
#
# `suffix` is built by verdict_suffix() and nowhere else: user id, ticker
# (already through tickers.validate_ticker), horizon, and the entry as integer
# cents or `auto`. Both keys share it, so a lock always guards exactly the
# cache entry it is about to write.

def verdict_suffix(user_id: str, ticker: str, horizon: str, entry_key: str) -> str:
    parts = (user_id, ticker, horizon, entry_key)
    if not all(isinstance(p, str) and p.strip() and ":" not in p for p in parts):
        raise ValueError("verdict key parts must be non-empty strings without ':'")
    return f"{user_id.strip().lower()}:{canonical(ticker)}:{horizon.strip().lower()}:{entry_key.strip().lower()}"


async def get_cached_verdict(r: Optional[aioredis.Redis], suffix: str) -> Optional[dict]:
    """{verdictId, fingerprint, entry} or None. A miss, a raise and an
    unparseable value are all a miss: the cost is one more verdict, never a
    wrong one."""
    if r is None:
        return None
    try:
        raw = await r.get(f"{VERDICT_PREFIX}{suffix}")
        value = json.loads(raw) if raw else None
    except Exception as e:
        logger.warning(f"verdict cache read failed, treating as a miss: {e}")
        return None
    if isinstance(value, dict) and all(isinstance(value.get(k), str)
                                       for k in ("verdictId", "fingerprint", "entry")):
        return value
    return None


async def store_cached_verdict(r: Optional[aioredis.Redis], suffix: str, value: dict, ttl: int) -> bool:
    if r is None:
        return False
    try:
        await r.set(f"{VERDICT_PREFIX}{suffix}", json.dumps(value), ex=ttl)
        return True
    except Exception as e:
        logger.warning(f"verdict cache write failed; the next call pays again: {e}")
        return False


async def acquire_analyze_lock(r: Optional[aioredis.Redis], suffix: str) -> bool:
    """True when this request may spend. False only when Redis says another
    request holds the same key; Redis absent or raising is True (fail-open:
    a Redis blip must not take the analyst down, and the caps still bound
    the spend)."""
    if r is None:
        return True
    try:
        return bool(await r.set(f"{LOCK_PREFIX}{suffix}", "1", ex=TTL_ANALYZE_LOCK, nx=True))
    except Exception as e:
        logger.warning(f"analyze lock failed, proceeding without one: {e}")
        return True


async def release_analyze_lock(r: Optional[aioredis.Redis], suffix: str) -> None:
    if r is None:
        return
    try:
        await r.delete(f"{LOCK_PREFIX}{suffix}")
    except Exception as e:
        logger.warning(f"analyze lock release failed; it expires in {TTL_ANALYZE_LOCK}s: {e}")
