"""
TradingFirm — the journal scorer (Part 4.5).

`score_once` is one night: find every stored verdict whose +1 / +5 / +20 / +30 / +60
XNYS session has closed and is unscored and not expired, refresh each of
their tickers through data-engine's existing route (one at a time, paced),
read the stored bars back, and write one ai.verdict_outcomes row per horizon.

How it knows the bars exist and are final (spec 4.5 decision 2):
  time      the slot is 17:30 ET on a session; no refresh starts after 18:10
  written   only a refresh answering 200 with daily AND hourly bars counts.
            A cooldown 429 proves a completed refresh, not a stored bar
            (F1): requeued once at the end of the night, never fresh
  present   a daily bar dated exactly each session day0+1 … N (hard); day
            0's hourly bars from the ask are soft — yfinance omits an hour
            with no trades, so only "expected some, stored none" defers

Pacing (G6, decision 11): one refresh per ticker, ≤ 20 attempts a night
retries included, 5 s apart. A night stops on a provider refusal, a dead
data-engine or a SECOND blank answer — a delisted symbol and a yfinance rate
limit answer the same 200 + zero bars (F2), and a rate limit blanks every
ticker. Every blanked or night-stopping ticker joins a Redis set and runs
behind the healthy ones the next night, so dead tickers cannot starve the
rest; every horizon expires 10 sessions after its target anyway.

No LLM path is imported here (`test_journal_never_imports_an_llm_path`).
"""

import asyncio
import logging
import uuid
from collections import deque
from datetime import datetime, time, timedelta, timezone
from typing import Awaitable, Callable, Optional

import db
import upstream
from cache import AI_PREFIX
from journal import scoring, sessions
from tickers import validate_ticker

logger = logging.getLogger(__name__)

HORIZONS = sessions.HORIZONS
MAX_ATTEMPTS = 20               # refreshes a night, cooldown retries included
SPACING_SECONDS = 5
REFRESH_TIMEOUT = 120.0
BARS_TIMEOUT = 30.0
# A cheap pre-filter; expiry is by the calendar. A +60 horizon is last due
# 70 sessions after day 0 — 100 calendar days for a 2026-09-21 ask, across
# Thanksgiving and Christmas — so 110 keeps it in the window with a margin.
SELECTION_DAYS = 110

LOCK_KEY = f"{AI_PREFIX}lock:journal"
LOCK_TTL = 2700                 # 17:30–18:10, a last 120 s refresh, and margin
BLANKED_KEY = f"{AI_PREFIX}journal:blanked"
BLANKED_TTL = 604800            # 7 days


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_result() -> dict:
    return {"due": 0, "tickers": 0, "refreshed": 0, "scored": 0, "requeued": 0,
            "blank": 0, "expired": 0, "deferred": 0, "skipped": 0, "unscored": 0,
            "stoppedBy": None}


# ── Redis: the lock and the blanked set, both optional ──────────

async def _take_lock(redis) -> Optional[bool]:
    """True = taken, False = held by another scorer, None = no Redis to ask."""
    if redis is None:
        return None
    try:
        return bool(await redis.set(LOCK_KEY, uuid.uuid4().hex, nx=True, ex=LOCK_TTL))
    except Exception as e:
        logger.warning(f"journal lock unavailable, scoring without it: {e!r}")
        return None


async def _blanked(redis) -> set:
    if redis is None:
        return set()
    try:
        members = await redis.smembers(BLANKED_KEY)
        return {m.decode() if isinstance(m, bytes) else m for m in members or ()}
    except Exception as e:
        logger.warning(f"journal blanked set unreadable, plain order tonight: {e!r}")
        return set()


async def _add_blanked(redis, ticker: str) -> None:
    if redis is None:
        return
    try:
        await redis.sadd(BLANKED_KEY, ticker)
        await redis.expire(BLANKED_KEY, BLANKED_TTL)
    except Exception as e:
        logger.warning(f"journal could not remember {ticker} as blanked: {e!r}")


# ── Selection ────────────────────────────────────────────────────

def plan_work(rows: list[dict], now: datetime, result: dict) -> dict:
    """{ticker: [verdict work]} for every due horizon, tickers in the order of
    their oldest due verdict (rows arrive oldest first)."""
    latest = sessions.latest_closed_session(now)
    today = sessions.et_date(now)
    work: dict = {}
    for row in rows:
        day0, in_session = sessions.entry_session(row["asked_at"])
        due = []
        for h in HORIZONS:
            if h in row["scored"]:
                continue
            state, target = sessions.horizon_state(day0, h, latest, today)
            if state == sessions.EXPIRED:
                result["expired"] += 1
            elif state == sessions.DUE:
                due.append((h, target))
        if not due:
            continue
        try:
            plan = scoring.parse_plan(row["plan_proposed"])
        except scoring.UnreadablePlan as e:
            logger.error(f"journal: verdict {row['id']} ({row['ticker']}) has a stored plan "
                         f"that does not parse ({e}); left unscored")
            result["unscored"] += 1
            continue
        result["due"] += len(due)
        ticker = validate_ticker(row["ticker"])
        work.setdefault(ticker, []).append({
            "id": row["id"], "asked_at": row["asked_at"], "entry": row["entry"],
            "plan": plan, "day0": day0, "in_session": in_session, "horizons": due,
        })
    return work


def order_tickers(work: dict, blanked: set) -> list[str]:
    healthy = [t for t in work if t not in blanked]
    return healthy + [t for t in work if t in blanked]


# ── One ticker, after a good refresh ─────────────────────────────

def _midnight_utc(d) -> datetime:
    return datetime.combine(d, time(0), tzinfo=timezone.utc)


def score_ticker(ticker: str, verdicts: list[dict], daily: list[dict],
                 hourly: list[dict], result: dict) -> list[dict]:
    """The outcome rows for one ticker's verdicts. Missing bars skip a horizon
    (daily, hard) or a verdict (hourly expected but none stored); a scale
    break skips the whole verdict."""
    by_date = {sessions.bar_date(b["ts"]): b for b in daily}
    starts = [(sessions.bar_start(b["ts"]), b) for b in hourly]
    rows = []
    for v in verdicts:
        day0, asked = v["day0"], v["asked_at"]
        ask_bars, count = [], None
        if v["in_session"]:
            close0 = sessions.session_close(day0)
            window = sorted(((s, b) for s, b in starts if asked <= s < close0), key=lambda x: x[0])
            expected = sessions.hourly_starts(day0, asked)
            if expected and not window:
                logger.warning(f"journal {ticker}: verdict {v['id']} expects {len(expected)} "
                               f"hourly bar(s) on {day0} after the ask and none are stored; deferred")
                result["skipped"] += 1
                continue
            missing = sorted(set(expected) - {s for s, _ in window})
            if missing:
                logger.warning(f"journal {ticker}: {day0} hourly bar(s) missing at "
                               f"{', '.join(m.isoformat() for m in missing)}; scored with "
                               f"{len(window)} (no trades in an hour means no bar)")
            ask_bars, count = [b for _, b in window], len(window)

        verdict_rows = []
        try:
            for h, target in v["horizons"]:
                needed = sessions.sessions_after(day0, target)
                absent = [d for d in needed if d not in by_date]
                if absent:
                    logger.warning(f"journal {ticker}: +{h} for verdict {v['id']} waits for "
                                   f"daily bar(s) {', '.join(d.isoformat() for d in absent)}")
                    result["skipped"] += 1
                    continue
                out = scoring.score(v["entry"], v["plan"], ask_bars + [by_date[d] for d in needed])
                verdict_rows.append({**out, "verdict_id": v["id"], "horizon_days": h,
                                     "session_date": target, "ask_session_bars": count})
        except scoring.ScaleBreak as e:
            logger.error(f"journal {ticker}: price-scale break for verdict {v['id']} — {e}; "
                         f"left unscored (a split re-scaled the stored bars, or a ≥ 30 % gap)")
            result["unscored"] += 1
            continue
        rows.extend(verdict_rows)
    return rows


# ── The night ────────────────────────────────────────────────────

async def score_once(state, settings, *, deadline: Optional[datetime] = None,
                     clock: Callable[[], datetime] = _utc_now,
                     sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> dict:
    """One scoring pass. Never raises for a dependency; returns what it did.
    `deadline` is the slot's 18:10 ET — None for a manual pass."""
    result = new_result()
    pool = getattr(state, "db_pool", None)
    if pool is None:
        logger.warning("journal: no database pool; slot skipped")
        return {**result, "stoppedBy": "no database"}

    redis = getattr(state, "redis", None)
    lock = await _take_lock(redis)
    if lock is False:
        logger.warning("journal: another scorer holds the lock; slot skipped")
        return {**result, "stoppedBy": "locked"}
    try:
        now = clock()
        try:
            rows = await db.due_verdicts(pool, db.DEV_USER_ID, now - timedelta(days=SELECTION_DAYS),
                                         len(HORIZONS))
        except Exception as e:
            logger.warning(f"journal: due-verdict read failed ({type(e).__name__}); night skipped")
            return {**result, "stoppedBy": "database error"}

        work = plan_work(rows, now, result)
        result["tickers"] = len(work)
        if not work:
            logger.info(f"journal: nothing due ({result['expired']} expired)")
            return result

        queue = deque(order_tickers(work, await _blanked(redis)))
        http, base = state.http, settings.data_engine_url
        retried, attempts, blanks = set(), 0, 0
        while queue:
            if attempts >= MAX_ATTEMPTS:
                break
            if attempts:
                await sleep(SPACING_SECONDS)
            if deadline is not None and clock() >= deadline:
                result["stoppedBy"] = "deadline"
                break
            ticker = queue.popleft()
            attempts += 1
            answer = await upstream.refresh_ticker(http, base, ticker, REFRESH_TIMEOUT)

            if answer.kind == upstream.REFRESH_COOLDOWN:
                if ticker not in retried:
                    retried.add(ticker)
                    queue.append(ticker)
                    result["requeued"] += 1
                    logger.info(f"journal {ticker}: refresh cooldown, requeued once")
                else:
                    result["skipped"] += 1
                    logger.warning(f"journal {ticker}: still on refresh cooldown; tomorrow")
                continue
            if answer.kind == upstream.REFRESH_BLANK:
                blanks += 1
                result["blank"] += 1
                await _add_blanked(redis, ticker)
                if blanks >= 2:
                    result["stoppedBy"] = f"{ticker}: second blank refresh tonight"
                    logger.warning(f"journal: {answer.detail} for {ticker}, the second tonight — "
                                   f"a yfinance rate limit blanks every ticker; stopping")
                    break
                logger.warning(f"journal {ticker}: {answer.detail}; skipped, runs last next night")
                continue
            if answer.kind == upstream.REFRESH_STOP:
                result["stoppedBy"] = f"{ticker}: {answer.detail}"
                await _add_blanked(redis, ticker)
                logger.warning(f"journal: refresh of {ticker} refused ({answer.detail}); stopping")
                break

            result["refreshed"] += 1
            verdicts = work[ticker]
            try:
                daily = await upstream.fetch_bars(
                    http, base, ticker, "1d", _midnight_utc(min(v["day0"] for v in verdicts)),
                    BARS_TIMEOUT)
                in_session = [v["asked_at"] for v in verdicts if v["in_session"]]
                hourly = await upstream.fetch_bars(
                    http, base, ticker, "1h", min(in_session), BARS_TIMEOUT) if in_session else []
            except upstream.BarsUnavailable as e:
                logger.warning(f"journal {ticker}: bars read failed after the refresh ({e}); skipped")
                result["skipped"] += 1
                continue

            rows_out = score_ticker(ticker, verdicts, daily, hourly, result)
            del daily, hourly
            try:
                result["scored"] += await db.insert_outcomes(pool, rows_out)
            except Exception as e:
                logger.error(f"journal {ticker}: outcome insert failed ({type(e).__name__}); "
                             f"{len(rows_out)} row(s) rolled back, retried next slot")
                result["skipped"] += 1

        result["deferred"] = len(queue)
        logger.info(f"journal: {result}")
        return result
    finally:
        if lock:
            try:
                await redis.delete(LOCK_KEY)
            except Exception as e:
                logger.warning(f"journal lock release failed (expires in {LOCK_TTL} s): {e!r}")


# ── The nightly loop ─────────────────────────────────────────────

async def run_loop(state, settings, *, clock: Callable[[], datetime] = _utc_now,
                   sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
    """17:30 ET on every XNYS session, forever (spec 4.5 decision 10).

    Waits only through wallclock.sleep_until: Docker's monotonic clock stops
    while the Mac sleeps. No boot pass and no catch-up pass: "due" needs no
    memory of past slots, so a slot slept through is caught by the next one,
    and a restart spends nothing. A pass that raises is logged and the loop
    goes on; cancellation (shutdown) propagates.
    """
    import wallclock

    logger.info("journal scoring loop running: 17:30 ET on XNYS sessions")
    while True:
        session, slot = sessions.next_slot(clock())
        wake = await wallclock.sleep_until(slot, clock=clock, sleep=sleep, log=logger)
        deadline = sessions.deadline_at(session)
        if wake.now >= deadline:
            logger.warning(f"journal: the {session} slot was missed (woke {wake.now.isoformat()}); "
                           f"the next slot scores whatever it left due")
            continue
        try:
            result = await score_once(state, settings, deadline=deadline, clock=clock, sleep=sleep)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"journal: the {session} pass raised {type(e).__name__}: {e}")
            result = {**new_result(), "stoppedBy": f"error: {type(e).__name__}"}
        state.journal_last_run_at = wake.now.isoformat()
        state.journal_last_result = result
