"""
TradingFirm — core quotes for the regime monitors (Part 3.2).

One yf.download of the plan's 17 core tickers (daily, 1y), cached as an
envelope under tf:risk:cache:quotes:
    {asOf, period, interval,
     tickers: {SPY: {date[], open[], high[], low[], close[], volume[]}, …},
     missing: [...], reason: null | "partial" | "empty"}
5 min when every ticker came back, 120 s when degraded (decision 3).

REQUEST COUNT (read from the installed 1.5.1, decision 5): threads=False
loops Ticker(t).history() once per ticker, and each first fetches the
ticker's timezone (hard-coded timeout=10) unless yfinance's on-disk tz cache
has it. Cold container: 34 requests. Warm: 17. Worst case at timeout=5:
255 s cold, 85 s warm.

NO OUTER TIMEOUT, on purpose: the download runs in a thread, which cannot be
cancelled; a wait_for would release the single-flight lock while the thread
kept sending requests. The lock is held until the thread returns.

REFUSAL DETECTION (decision 6): 1.5.1 catches YFRateLimitError per ticker
and only logs it, so a handler on the "yfinance" logger watches the
download. Version-guarded because it depends on that log format.

LAST-KNOWN (Part 3.3 decision 2): every full answer is also written to
tf:risk:cache:quotes_last for 24 h. get_quotes_view() is what the regime
monitors read: it serves that body with stale: true on a cooldown, a
refusal, an error or a degraded answer. Stale is decided here, once.
"""

import asyncio
import gc
import logging
import math
import re
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import pandas as pd
import yfinance as yf
from yfinance.exceptions import YFRateLimitError

from cache import (
    KIND_NIGHT_QUOTES,
    KIND_QUOTES,
    KIND_QUOTES_LAST,
    SOURCE_YFINANCE,
    TTL_COOLDOWN_YFINANCE,
    TTL_DEGRADED,
    TTL_LAST_KNOWN,
    TTL_NIGHT_QUOTES,
    TTL_QUOTES,
    cached_json,
    canonical,
    cooldown_remaining,
    get_cached_json,
    risk_key,
    set_cached_json,
    start_cooldown,
)
from monitors.errors import QuotesCoolingDown, QuotesError, QuotesRateLimited

logger = logging.getLogger(__name__)

EXPECTED_YF_VERSION = "1.5.1"

# Plan §2, in plan order. ≤ 20 per yf.download (CLAUDE.md).
CORE_TICKERS = tuple(canonical(t) for t in (
    "SPY", "QQQ", "RSP", "^VIX", "TLT", "GLD", "UUP", "XLK", "XLU",
    "XLP", "XLV", "XLY", "XLF", "ES=F", "NQ=F", "CL=F", "GC=F",
))
# Night mode (Part 3.4b decision 7). Step 0 (2026-09-13) showed the 1d bar
# updating live through the Globex evening session, within 0.013 % of the 1h
# close, so a night check reads the same interval the core download does.
NIGHT_TICKERS = tuple(canonical(t) for t in ("ES=F", "NQ=F"))
NIGHT_PERIOD = "5d"
NIGHT_INTERVAL = "1d"
QUOTES_PERIOD = "1y"
QUOTES_INTERVAL = "1d"
YF_REQUEST_TIMEOUT = 5
QUOTES_REASONS = (None, "partial", "empty")
RATE_LIMIT_PATTERN = re.compile(r"YFRateLimitError|Too Many Requests")

_ENVELOPE_KEYS = ("asOf", "period", "interval", "tickers", "missing", "reason")
_COLUMNS = (("Open", "open"), ("High", "high"), ("Low", "low"), ("Close", "close"))

# Single-flight: one lock per running event loop (an asyncio.Lock binds to
# the loop it first waits on; the service runs one loop, tests run many).
_lock_state: dict[str, Any] = {"loop": None, "lock": None}


def _download_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    if _lock_state["loop"] is not loop:
        _lock_state["loop"] = loop
        _lock_state["lock"] = asyncio.Lock()
    return _lock_state["lock"]


def download_in_flight() -> bool:
    """Whether a core-quotes download holds the single-flight lock in the
    running loop. A scheduled check skips rather than queues (Part 3.4)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    lock = _lock_state["lock"]
    return lock is not None and _lock_state["loop"] is loop and lock.locked()


# ── Download ─────────────────────────────────────────────────────

class _LogCapture(logging.Handler):
    """Collects every yfinance log message emitted during one download."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.messages.append(record.getMessage())
        except Exception:
            self.messages.append(str(record.msg))


def is_rate_limited(messages: list[str]) -> bool:
    return any(RATE_LIMIT_PATTERN.search(m) for m in messages)


def _download_sync(tickers: list[str], period: str = QUOTES_PERIOD,
                   interval: str = QUOTES_INTERVAL) -> pd.DataFrame:
    # No session= (yfinance 1.x manages its own); threads=False (CLAUDE.md).
    return yf.download(
        " ".join(tickers),
        period=period,
        interval=interval,
        group_by="ticker",
        threads=False,
        progress=False,
        auto_adjust=True,
        timeout=YF_REQUEST_TIMEOUT,
    )


async def download_frame(tickers, period: str = QUOTES_PERIOD,
                         interval: str = QUOTES_INTERVAL) -> tuple[Optional[pd.DataFrame], list[str]]:
    """
    Run the download in a thread with the yfinance logger watched.
    Returns (frame or None, captured log messages). A raised
    YFRateLimitError returns (None, messages + its repr) so the caller sees
    one signal; any other raise becomes QuotesError. The handler is removed
    on every path.
    """
    capture = _LogCapture()
    yf_logger = logging.getLogger("yfinance")
    yf_logger.addHandler(capture)
    try:
        df = await asyncio.to_thread(_download_sync, list(tickers), period, interval)
    except YFRateLimitError as e:
        return None, capture.messages + [repr(e)]
    except Exception as e:
        raise QuotesError(f"core quotes download failed: {type(e).__name__}: {e}") from None
    finally:
        yf_logger.removeHandler(capture)
    return df, capture.messages


# ── Frame → envelope ─────────────────────────────────────────────

def _num(value: Any, integer: bool = False) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return int(f) if integer else f


def frame_to_tickers(df: Optional[pd.DataFrame], tickers) -> tuple[dict, list[str]]:
    """Columnar series per ticker; rows with a NaN close dropped; handles
    MultiIndex (Ticker, Price) columns and a flat single-ticker frame."""
    tickers = list(tickers)
    out: dict[str, dict] = {}
    if df is not None and not df.empty:
        multi = isinstance(df.columns, pd.MultiIndex)
        for t in tickers:
            if multi:
                if t not in df.columns.get_level_values(0):
                    continue
                sub = df[t]
            elif len(tickers) == 1:
                sub = df
            else:
                break
            if "Close" not in sub.columns:
                continue
            sub = sub.dropna(subset=["Close"])
            if sub.empty:
                continue
            series = {"date": [ts.strftime("%Y-%m-%d") for ts in sub.index]}
            for src, dst in _COLUMNS:
                series[dst] = [_num(v) for v in sub[src]] if src in sub.columns else [None] * len(sub)
            series["volume"] = (
                [_num(v, integer=True) for v in sub["Volume"]]
                if "Volume" in sub.columns else [None] * len(sub)
            )
            out[t] = series
    missing = [t for t in tickers if t not in out]
    return out, missing


def build_envelope(df: Optional[pd.DataFrame], tickers, now: Callable[[], datetime]) -> dict:
    series, missing = frame_to_tickers(df, tickers)
    if not missing:
        reason = None
    elif series:
        reason = "partial"
    else:
        reason = "empty"
    return {
        "asOf": now().isoformat(),
        "period": QUOTES_PERIOD,
        "interval": QUOTES_INTERVAL,
        "tickers": series,
        "missing": missing,
        "reason": reason,
    }


def quotes_ttl(body: dict) -> int:
    return TTL_DEGRADED if body.get("reason") is not None else TTL_QUOTES


def _valid_envelope(body: Any, tickers) -> bool:
    return (
        isinstance(body, dict)
        and all(k in body for k in _ENVELOPE_KEYS)
        and body["reason"] in QUOTES_REASONS
        and isinstance(body["tickers"], dict)
        and isinstance(body["missing"], list)
        and set(body["tickers"]) | set(body["missing"]) == set(tickers)
    )


def valid_quotes(body: Any) -> bool:
    return _valid_envelope(body, CORE_TICKERS)


def valid_night_quotes(body: Any) -> bool:
    return _valid_envelope(body, NIGHT_TICKERS)


def night_quotes_ttl(body: dict) -> int:
    return TTL_DEGRADED if body.get("reason") is not None else TTL_NIGHT_QUOTES


def _require_version() -> None:
    if yf.__version__ != EXPECTED_YF_VERSION:
        raise QuotesError(
            f"yfinance {yf.__version__} installed, rate-limit detection is "
            f"written for {EXPECTED_YF_VERSION}"
        )


# ── Public ───────────────────────────────────────────────────────

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def get_core_quotes(r, memory, *, now: Callable[[], datetime] = _utc_now) -> tuple[dict, bool]:
    """
    The 17 core tickers as (envelope, from_cache). Order: cache read →
    version guard → cooldown check → download. A refusal starts the
    source-wide cooldown and raises; an all-empty download is cached for
    120 s and also starts the cooldown (1.x's silent-block pattern).
    """
    async def fetch() -> dict:
        _require_version()
        left = await cooldown_remaining(r, memory, SOURCE_YFINANCE, TTL_COOLDOWN_YFINANCE)
        if left is not None:
            raise QuotesCoolingDown(left)

        df, messages = await download_frame(CORE_TICKERS)
        try:
            if is_rate_limited(messages):
                await start_cooldown(r, memory, SOURCE_YFINANCE, TTL_COOLDOWN_YFINANCE)
                raise QuotesRateLimited("yfinance rate limited the core quotes download")
            body = build_envelope(df, CORE_TICKERS, now)
        finally:
            del df          # G8: nothing but the JSON envelope survives
            gc.collect()

        if body["reason"] is None:
            # Before cached_json writes the main key (spec 3.3 writes table).
            await write_last_known(r, body)
        elif body["reason"] == "empty":
            await start_cooldown(r, memory, SOURCE_YFINANCE, TTL_COOLDOWN_YFINANCE)
            logger.warning("Core quotes: all 17 tickers empty, yfinance cooldown started")
        else:
            logger.warning(f"Core quotes: partial, missing {body['missing']}")
        return body

    async with _download_lock():
        return await cached_json(
            r, risk_key(KIND_QUOTES), None, fetch, valid=valid_quotes, ttl_for=quotes_ttl
        )


# ── Last-known body and the monitors' view (Part 3.3) ────────────

def valid_last_known(body: Any) -> bool:
    """Only a full answer is ever stored or served as last-known."""
    return valid_quotes(body) and body["reason"] is None


async def write_last_known(r, body: dict) -> None:
    """Keep a copy of a full body for 24 h. A write failure is logged and
    never fails the download; the previous copy stands until its TTL."""
    if r is None:
        return
    try:
        await set_cached_json(r, risk_key(KIND_QUOTES_LAST), body, TTL_LAST_KNOWN)
    except Exception as e:
        logger.warning(f"Last-known quotes write failed: {e}")


async def read_last_known(r) -> Optional[dict]:
    """The last full body, or None when Redis is absent or raising, the key
    is missing or expired, or it holds unparseable JSON or the wrong shape."""
    if r is None:
        return None
    key = risk_key(KIND_QUOTES_LAST)
    try:
        body = await get_cached_json(r, key)
    except Exception as e:
        logger.warning(f"Last-known quotes read failed: {e}")
        return None
    if body is None:
        return None
    if not valid_last_known(body):
        logger.warning(f"Cache at {key} has the wrong shape, ignoring")
        return None
    return body


def _entry(body: dict, ticker: str, stale: bool) -> dict:
    return {**body["tickers"][ticker], "asOf": body["asOf"], "stale": stale}


async def get_quotes_view(r, memory, *, now: Callable[[], datetime] = _utc_now) -> dict:
    """
    What the regime monitors read: {asOf, source, reason, tickers: {T:
    {date[], open[], …, asOf, stale}}, staleTickers}. Never raises for a
    source state; each ticker carries the asOf of the body it came from, so
    the partial-bar rule reads the download time.

    source: fresh | cached (a full or partial answer), last_known (the whole
    view from the 24 h copy), none (nothing to serve). reason: null |
    partial | empty | cooldown | rate_limited | error.
    """
    body: Optional[dict] = None
    from_cache = False
    try:
        body, from_cache = await get_core_quotes(r, memory, now=now)
        reason = body["reason"]
    except QuotesCoolingDown:
        reason = "cooldown"
    except QuotesRateLimited:
        reason = "rate_limited"
    except QuotesError as e:
        # A bug must not hide for 24 h behind a quiet stale flag.
        logger.error(f"Core quotes failed, serving last-known: {e}")
        reason = "error"

    if body is not None and reason is None:
        return {
            "asOf": body["asOf"],
            "source": "cached" if from_cache else "fresh",
            "reason": None,
            "tickers": {t: _entry(body, t, False) for t in CORE_TICKERS},
            "staleTickers": [],
        }

    last = await read_last_known(r)

    if body is not None and reason == "partial":
        tickers = {}
        for t in CORE_TICKERS:
            if t in body["tickers"]:
                tickers[t] = _entry(body, t, False)
            elif last is not None:
                tickers[t] = _entry(last, t, True)
        logger.warning(
            f"Core quotes partial: {body['missing']} "
            f"{'from last-known ' + last['asOf'] if last else 'unavailable (no last-known)'}"
        )
        return {
            "asOf": body["asOf"],
            "source": "cached" if from_cache else "fresh",
            "reason": "partial",
            "tickers": tickers,
            "staleTickers": list(body["missing"]),
        }

    # empty, cooldown, rate_limited, error: the whole view is last-known.
    if last is not None:
        logger.warning(f"Core quotes {reason}: serving last-known from {last['asOf']}")
        return {
            "asOf": last["asOf"],
            "source": "last_known",
            "reason": reason,
            "tickers": {t: _entry(last, t, True) for t in CORE_TICKERS},
            "staleTickers": list(CORE_TICKERS),
        }
    logger.warning(f"Core quotes {reason}: no last-known body, monitors cannot score")
    return {
        "asOf": None,
        "source": "none",
        "reason": reason,
        "tickers": {},
        "staleTickers": list(CORE_TICKERS),
    }


# ── Night mode (Part 3.4b decision 7) ────────────────────────────

async def get_night_quotes(r, memory, *, now: Callable[[], datetime] = _utc_now) -> tuple[dict, bool]:
    """
    ES=F and NQ=F as (envelope, from_cache), cached 600 s (120 s degraded).
    Same version guard, source cooldown and single-flight lock as the core
    download. No last-known copy: an hours-old futures price would be a wrong
    cap, not a stale-but-useful body.
    """
    async def fetch() -> dict:
        _require_version()
        left = await cooldown_remaining(r, memory, SOURCE_YFINANCE, TTL_COOLDOWN_YFINANCE)
        if left is not None:
            raise QuotesCoolingDown(left)

        df, messages = await download_frame(NIGHT_TICKERS, NIGHT_PERIOD, NIGHT_INTERVAL)
        try:
            if is_rate_limited(messages):
                await start_cooldown(r, memory, SOURCE_YFINANCE, TTL_COOLDOWN_YFINANCE)
                raise QuotesRateLimited("yfinance rate limited the night quotes download")
            body = build_envelope(df, NIGHT_TICKERS, now)
        finally:
            del df          # G8
            gc.collect()

        if body["reason"] == "empty":
            await start_cooldown(r, memory, SOURCE_YFINANCE, TTL_COOLDOWN_YFINANCE)
            logger.warning("Night quotes: both contracts empty, yfinance cooldown started")
        elif body["reason"] is not None:
            logger.warning(f"Night quotes: partial, missing {body['missing']}")
        return body

    async with _download_lock():
        return await cached_json(
            r, risk_key(KIND_NIGHT_QUOTES), None, fetch,
            valid=valid_night_quotes, ttl_for=night_quotes_ttl
        )


async def get_night_view(r, memory, *, now: Callable[[], datetime] = _utc_now) -> dict:
    """
    What a night check reads, in the same shape the monitors' view has:
    {asOf, source, reason, tickers, staleTickers}. Never raises for a source
    state, and never serves a last-known body — a cooldown, refusal, error or
    empty answer is an empty view, which the overlay reports as unavailable.
    """
    try:
        body, from_cache = await get_night_quotes(r, memory, now=now)
    except QuotesCoolingDown:
        reason = "cooldown"
    except QuotesRateLimited:
        reason = "rate_limited"
    except QuotesError as e:
        logger.error(f"Night quotes failed: {e}")
        reason = "error"
    else:
        return {
            "asOf": body["asOf"],
            "source": "cached" if from_cache else "fresh",
            "reason": body["reason"],
            "tickers": {t: _entry(body, t, False) for t in body["tickers"]},
            "staleTickers": list(body["missing"]),
        }
    logger.warning(f"Night quotes {reason}: no futures price for this check")
    return {"asOf": None, "source": "none", "reason": reason,
            "tickers": {}, "staleTickers": list(NIGHT_TICKERS)}
