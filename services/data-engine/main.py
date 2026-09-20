"""
TradingFirm — Data Engine (Service 1)

FastAPI application with endpoints:
  POST /scan/run       — trigger a new market scan (background)
  GET  /scan/results   — latest scan results (cached or in-memory)
  GET  /scan/history   — past scan metadata
  GET  /scan/status    — current scan status (idle/running/completed)
  GET  /stocks/{ticker} — enriched data for one stock
  POST /stock/{ticker}/refresh — download + persist bars for one stock
  GET  /stock/{ticker}/bars — stored bars for one stock (DB only)
  GET  /indicators/{ticker} — swing indicator set + zones from stored bars (Redis 15 min)
  GET  /dossier/{ticker}   — one document per ticker: indicators, news, events,
                             recommendations, filings, earnings reactions, profile
  GET  /market/status  — current market session
  POST /news/ingest    — store market news under _MARKET (risk-shield's poller, Part 3.5)
  GET  /news/market    — stored _MARKET news, newest first (risk-shield's macro inputs, Part 3.6a)
  POST /news/{id}/sentiment — store one headline classification (ai-agent, Part 4.2)
  GET  /health         — health check
  GET  /               — service info

Port: 8001
"""

import gc
import json
import logging
import time as _time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from providers import get_provider
from scanners.market_scanner import MarketScanner
from scanners.market_status import get_market_status
from indicators import IndicatorsResponse
from dossier import HORIZON_PROFILES, HORIZON_SWING
from dossier.models import Budget, DossierResponse
from scanners.models import ScanRequest, ScanResult
from tickers import normalize_ticker, validate_ticker

# ── Logging ──────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s | %(name)-20s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("data-engine")


# ── In-Memory Fallback (when Redis/Postgres unavailable) ─────────

class InMemoryStore:
    """Simple in-memory store for scan results when Redis/DB unavailable."""

    def __init__(self):
        from cache import MemoryCooldowns
        self.scan_status = {"status": "idle", "message": "No scan running"}
        self.scan_result = None
        self.last_scan_time = 0.0
        # Cooldown clock (refresh per ticker, sources per name). Lives in
        # cache.py since Part 2.4 so Redis-backed and in-memory cooldowns
        # go through one pair of helpers.
        self.cooldowns = MemoryCooldowns()

    def set_status(self, status: str, message: str):
        self.scan_status = {"status": status, "message": message}

    def get_status(self) -> dict:
        return self.scan_status

    def set_result(self, result: dict):
        self.scan_result = result

    def get_result(self) -> dict | None:
        return self.scan_result


# ── Lifespan (startup/shutdown) ──────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize DB pool and Redis on startup, close on shutdown."""
    logger.info("Starting Data Engine...")

    # In-memory store (always available)
    app.state.memory = InMemoryStore()

    # Redis (optional)
    try:
        from cache import create_redis
        app.state.redis = await create_redis()
        logger.info("✅ Redis connection ready")
    except Exception as e:
        logger.warning(f"⚠️  Redis unavailable (using in-memory): {e}")
        app.state.redis = None

    # Database (optional)
    try:
        from db import create_db_pool
        app.state.db_pool = await create_db_pool()
        logger.info("✅ Database pool ready")
    except Exception as e:
        logger.warning(f"⚠️  Database unavailable (results not persisted): {e}")
        app.state.db_pool = None

    app.state.provider = get_provider(settings.data_provider)
    logger.info(f"✅ Data provider: {app.state.provider.provider_name}")

    # Alpha Vantage: fallback source for past earnings dates (Part 2.3).
    # An empty key is normal (dev twin): the client raises before any HTTP
    # and the earnings step reports the source as unavailable.
    from providers.context.alphavantage_client import AlphaVantageClient
    app.state.av_client = AlphaVantageClient(settings.alphavantage_api_key)
    logger.info(f"✅ Alpha Vantage configured: {app.state.av_client.configured}")

    # Context sources (Parts 2.1/2.2), built once and shared: each client
    # owns its rate limiter, so one per process is the point. An empty key or
    # User-Agent is normal (the dev twin) — the client raises before any HTTP
    # and the dossier reports that section as `unconfigured`.
    from providers.context.edgar_client import EdgarClient
    from providers.context.finnhub_client import FinnhubClient
    app.state.finnhub = FinnhubClient(settings.finnhub_api_key)
    app.state.edgar = EdgarClient(settings.edgar_user_agent)
    logger.info(
        f"✅ Finnhub configured: {app.state.finnhub.configured}, "
        f"EDGAR configured: {app.state.edgar.configured}"
    )

    app.state.scanner = MarketScanner(app.state.provider, app.state.db_pool)
    logger.info("✅ Scanner initialized")

    logger.info(f"Data Engine ready on port {settings.service_port}")
    yield

    # Shutdown
    logger.info("Shutting down Data Engine...")
    if app.state.db_pool:
        await app.state.db_pool.close()
        logger.info("Database pool closed")
    if app.state.redis:
        await app.state.redis.close()

    for name in ("av_client", "finnhub", "edgar"):
        client = getattr(app.state, name, None)
        if client is not None:
            await client.aclose()
    logger.info("Context clients closed")


# ── App ──────────────────────────────────────────────────────────

app = FastAPI(
    title="TradingFirm — Data Engine",
    description="Market data scanning, indicators, and fundamentals",
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


# ── Background Scan Task ─────────────────────────────────────────

async def _run_scan_background(
    app_state,
    price_min: float,
    price_max: float,
    advanced: bool,
):
    """Background task: run full scan, save to DB/cache/memory."""
    try:
        # Mark scan as running
        app_state.memory.set_status("running", "Scan in progress...")
        if app_state.redis:
            from cache import set_scan_status
            await set_scan_status(app_state.redis, "running", "Scan in progress...")

        # Run the scanner
        result: ScanResult = await app_state.scanner.run_scan(
            price_min=price_min,
            price_max=price_max,
            advanced=advanced,
        )

        result_dict = result.model_dump(mode="json", by_alias=True)

        # Always save to in-memory store
        app_state.memory.set_result(result_dict)
        app_state.memory.set_status(
            "completed", f"Scan complete: {result.passed_count} stocks found"
        )

        # Save to database (optional)
        if app_state.db_pool:
            try:
                from db import save_scan_result, upsert_stocks
                await save_scan_result(
                    pool=app_state.db_pool,
                    scanned_at=result.timestamp,
                    market_status=result.market_status,
                    total_screened=result.total_scanned,
                    total_passed=result.passed_count,
                    duration_seconds=result.duration_seconds,
                    stocks=result_dict["stocks"],
                )
                await upsert_stocks(app_state.db_pool, result_dict["stocks"])
            except Exception as e:
                logger.error(f"DB save failed: {e}")

        # Cache in Redis (optional)
        if app_state.redis:
            try:
                from cache import cache_scan_result, publish_scan_complete, set_scan_status
                await cache_scan_result(app_state.redis, result_dict)
                await publish_scan_complete(app_state.redis, result_dict)
                await set_scan_status(
                    app_state.redis,
                    "completed",
                    f"Scan complete: {result.passed_count} stocks found",
                )
            except Exception as e:
                logger.error(f"Redis cache/publish failed: {e}")

        logger.info(f"Background scan complete: {result.passed_count} stocks")

    except Exception as e:
        logger.error(f"Background scan failed: {e}", exc_info=True)
        app_state.memory.set_status("failed", str(e))
        if app_state.redis:
            try:
                from cache import set_scan_status
                await set_scan_status(app_state.redis, "failed", str(e))
            except Exception:
                pass


# ── Endpoints ────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "service": settings.service_name,
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "0.2.0",
        "provider": settings.data_provider,
        "db_connected": app.state.db_pool is not None,
        "redis_connected": app.state.redis is not None,
    }


@app.get("/")
async def root():
    """Root endpoint with service info."""
    return {
        "service": settings.service_name,
        "description": "Market data scanning, indicators, and fundamentals",
        "docs": "/docs",
        "endpoints": [
            "POST /scan/run",
            "GET  /scan/results",
            "GET  /scan/history",
            "GET  /scan/status",
            "GET  /stocks/{ticker}",
            "POST /stock/{ticker}/refresh",
            "GET  /stock/{ticker}/bars",
            "GET  /indicators/{ticker}",
            "GET  /dossier/{ticker}",
            "GET  /market/status",
            "POST /news/ingest",
            "GET  /news/market?hours=24&limit=50",
            "POST /news/{id}/sentiment",
            "GET  /health",
        ],
    }


@app.post("/scan/run", status_code=202)
async def scan_run(
    request: ScanRequest,
    background_tasks: BackgroundTasks,
):
    """
    Trigger a new market scan in the background.

    A full scan takes ~6 minutes. This endpoint returns immediately
    with status 202. Poll GET /scan/status to check progress,
    then GET /scan/results for data.
    """
    # Check if scan is already running (memory or Redis)
    mem_status = app.state.memory.get_status()
    if mem_status.get("status") == "running":
        return {
            "status": "already_running",
            "message": "A scan is already in progress. Check GET /scan/status.",
        }

    # ⚠️ RATE LIMIT: 10-minute cooldown between scans
    now = _time.time()
    elapsed = now - app.state.memory.last_scan_time
    if app.state.memory.last_scan_time > 0 and elapsed < 600:
        remaining = int(600 - elapsed)
        return {
            "status": "cooldown",
            "message": f"Scan cooldown: {remaining}s remaining. Min 10 minutes between scans.",
            "retry_after_seconds": remaining,
        }

    # Record scan start time
    app.state.memory.last_scan_time = now

    # Launch background scan
    background_tasks.add_task(
        _run_scan_background,
        app.state,
        request.price_min,
        request.price_max,
        request.advanced,
    )

    return {
        "status": "scan_started",
        "message": f"Scan started (${request.price_min}-${request.price_max}, advanced={request.advanced}). "
                   f"Poll GET /scan/status for progress.",
    }


@app.get("/scan/results")
async def scan_results(
    use_cache: bool = Query(True, description="Try Redis cache first"),
):
    """
    Get the latest scan results.

    Checks Redis cache first → in-memory → database fallback.
    """
    # Try Redis cache first
    if use_cache and app.state.redis:
        try:
            from cache import get_cached_scan
            cached = await get_cached_scan(app.state.redis)
            if cached:
                cached["source"] = "cache"
                return cached
        except Exception as e:
            logger.warning(f"Cache read failed: {e}")

    # Try in-memory store
    mem_result = app.state.memory.get_result()
    if mem_result:
        mem_result["source"] = "memory"
        return mem_result

    # Fall back to database
    if app.state.db_pool:
        try:
            from db import get_latest_scan
            db_result = await get_latest_scan(app.state.db_pool)
            if db_result:
                db_result["source"] = "database"
                return db_result
        except Exception as e:
            logger.error(f"DB read failed: {e}")

    # No results anywhere
    return {
        "source": "none",
        "message": "No scan results available. Run POST /scan/run first.",
        "stocks": [],
    }


@app.get("/scan/history")
async def scan_history(
    limit: int = Query(10, ge=1, le=100, description="Number of scans to return"),
):
    """List past scan runs (metadata only, no stocks array)."""
    if not app.state.db_pool:
        return {"scans": [], "count": 0, "message": "Database not connected"}

    try:
        from db import get_scan_history
        history = await get_scan_history(app.state.db_pool, limit=limit)
        return {"scans": history, "count": len(history)}
    except Exception as e:
        logger.error(f"History query failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


@app.get("/scan/status")
async def scan_status():
    """Check if a scan is currently running."""
    # Try Redis first
    if app.state.redis:
        try:
            from cache import get_scan_status
            status = await get_scan_status(app.state.redis)
            return status
        except Exception:
            pass

    # Fall back to in-memory
    return app.state.memory.get_status()


@app.get("/stocks/{ticker}")
async def get_stock(ticker: str):
    """Get enriched data for a specific stock ticker."""
    # Validate ticker format
    ticker = normalize_ticker(ticker)
    if not ticker.isalpha() or len(ticker) > 5:
        raise HTTPException(status_code=400, detail="Invalid ticker format")

    try:
        info = await app.state.provider.get_stock_info(ticker)
        if not info.get("name"):
            raise HTTPException(
                status_code=404,
                detail=f"Ticker '{ticker}' not found or returned no data",
            )
        return {
            "ticker": ticker,
            **info,
        }
    except HTTPException:
        raise
    except Exception as e:
        error_msg = str(e).lower()
        logger.error(f"Stock info failed for {ticker}: {e}")
        if "rate" in error_msg or "too many" in error_msg or "429" in error_msg:
            raise HTTPException(
                status_code=429,
                detail=f"Rate limited by data provider. Try again in 30 seconds.",
            )
        raise HTTPException(
            status_code=502,
            detail=f"Data provider error for {ticker}: {e}",
        )


async def refresh_ticker_bars(ticker: str) -> dict:
    """
    Download daily (2y) + hourly (3mo) bars for one ticker and persist them.

    The body of POST /stock/{ticker}/refresh, extracted in Part 2.4 so the
    dossier can refresh stale bars without going through HTTP. Raises the
    same HTTPExceptions the endpoint returns (429 on cooldown, 502/429 on a
    provider problem, 503 with no database); the dossier catches them and
    serves what is stored.

    Returns `(response body, {source: calls})`. The counts are the second
    half because they are the dossier's business, not the endpoint's: only
    this helper knows what it spent on yfinance and Alpha Vantage, and the
    HTTP response keeps exactly the shape Part 1.2 defined.
    """
    ticker = normalize_ticker(ticker)
    if not ticker.isalpha() or len(ticker) > 5:
        raise HTTPException(status_code=400, detail="Invalid ticker format")

    if not app.state.db_pool:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable; refresh would not be persisted.",
        )

    from cache import (
        TTL_REFRESH_COOLDOWN,
        cooldown_remaining,
        refresh_cooldown_name,
        start_cooldown,
    )

    cooldown = refresh_cooldown_name(ticker)
    remaining = await cooldown_remaining(
        app.state.redis, app.state.memory.cooldowns, cooldown, TTL_REFRESH_COOLDOWN
    )

    if remaining is not None:
        raise HTTPException(
            status_code=429,
            detail=f"'{ticker}' was refreshed recently. Try again in {remaining}s.",
            headers={"Retry-After": str(remaining)},
        )

    try:
        bulk_daily = await app.state.provider.download_daily([ticker], period="2y")
        bulk_hourly = await app.state.provider.download_hourly([ticker], period="3mo")
    except Exception as e:
        error_msg = str(e).lower()
        logger.error(f"Refresh fetch failed for {ticker}: {e}")
        if "rate" in error_msg or "too many" in error_msg or "429" in error_msg:
            raise HTTPException(
                status_code=429,
                detail="Rate limited by data provider. Try again later.",
            )
        raise HTTPException(status_code=502, detail=f"Data provider error for {ticker}: {e}")

    from db import bar_records_from_df, upsert_bars

    df_daily = app.state.provider.extract_ticker_df(bulk_daily, ticker)
    df_hourly = app.state.provider.extract_ticker_df(bulk_hourly, ticker)
    daily_bars = bar_records_from_df(df_daily)
    hourly_bars = bar_records_from_df(df_hourly)

    daily_count = await upsert_bars(app.state.db_pool, ticker, "1d", daily_bars)
    hourly_count = await upsert_bars(app.state.db_pool, ticker, "1h", hourly_bars)

    del bulk_daily, bulk_hourly, df_daily, df_hourly, daily_bars, hourly_bars
    gc.collect()

    # Earnings report dates (Part 2.3): runs after the bars are stored, so
    # it validates report dates against a store that already includes this
    # refresh. Bars are the product — any failure here is logged and the
    # refresh still returns 200 with a reason the caller can read.
    provider_calls = {"yfinance": 2}   # the two downloads above
    av_before = getattr(getattr(app.state, "av_client", None), "calls_made", 0)

    earnings = {"source": None, "stored": 0, "dropped": 0, "reason": "error"}
    try:
        from providers.context.earnings import sync_earnings_dates
        earnings = await sync_earnings_dates(
            app.state.provider,
            getattr(app.state, "av_client", None),
            ticker,
            app.state.db_pool,
            redis=app.state.redis,
            cooldowns=app.state.memory.cooldowns,
        )
    except Exception as e:
        logger.warning(f"Earnings dates step failed for {ticker}: {type(e).__name__}: {e}")

    # One `Ticker` call unless the step stopped before it (an empty bar store).
    if earnings.get("reason") != "no_bars":
        provider_calls["yfinance"] += 1
    av_spent = getattr(getattr(app.state, "av_client", None), "calls_made", 0) - av_before
    if av_spent:
        provider_calls["alphavantage"] = av_spent

    # New bars make any cached indicator snapshot stale: drop it now so a
    # refresh is never followed by up to 15 min of old numbers. Best-effort.
    if app.state.redis:
        try:
            from cache import delete_cached_indicators
            await delete_cached_indicators(app.state.redis, ticker)
        except Exception as e:
            logger.warning(f"Indicators cache invalidation failed for {ticker}: {e}")

    # Cooldown starts only now — after a successful fetch + persist.
    await start_cooldown(
        app.state.redis, app.state.memory.cooldowns, cooldown, TTL_REFRESH_COOLDOWN
    )

    return {
        "ticker": ticker,
        "dailyBars": daily_count,
        "hourlyBars": hourly_count,
        "earningsDates": earnings,
    }, provider_calls

@app.post("/stock/{ticker}/refresh")
async def refresh_stock(ticker: str):
    """
    Download daily (2y) + hourly (3mo) bars for one ticker and persist them.

    Rejects with 429 if this ticker was refreshed in the last 15 minutes
    (Redis-backed cooldown, falling back to an in-memory one if Redis is
    down). Returns 503 if the database is unavailable — bars are never
    fetched and silently discarded.
    """
    body, _provider_calls = await refresh_ticker_bars(ticker)
    return body


@app.get("/stock/{ticker}/bars")
async def get_bars_endpoint(ticker: str, interval: str, since: Optional[str] = None):
    """
    Fetch stored OHLCV bars for one ticker/interval. DB only — never
    falls through to the live provider.

    404 if the ticker/interval has no stored bars at all. 200 with an
    empty `bars` list if bars exist but `since` filters all of them out
    (those are different states: absent vs. filtered-empty).
    """
    ticker = normalize_ticker(ticker)
    if not ticker.isalpha() or len(ticker) > 5:
        raise HTTPException(status_code=400, detail="Invalid ticker format")

    if interval not in ("1d", "1h"):
        raise HTTPException(status_code=400, detail="interval must be '1d' or '1h'")

    since_dt = None
    if since is not None:
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid 'since' value: {since}")
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=timezone.utc)

    if not app.state.db_pool:
        raise HTTPException(status_code=503, detail="Database unavailable.")

    from db import bar_record_to_json, get_bars

    try:
        bars_all = await get_bars(app.state.db_pool, ticker, interval)
    except Exception as e:
        logger.error(f"Bars query failed for {ticker}/{interval}: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable.")

    if not bars_all:
        raise HTTPException(
            status_code=404,
            detail=f"No stored '{interval}' bars for '{ticker}'.",
        )

    if since_dt is not None:
        try:
            bars = await get_bars(app.state.db_pool, ticker, interval, since=since_dt)
        except Exception as e:
            logger.error(f"Bars query failed for {ticker}/{interval}: {e}")
            raise HTTPException(status_code=503, detail="Database unavailable.")
    else:
        bars = bars_all

    return {
        "ticker": ticker,
        "interval": interval,
        "bars": [bar_record_to_json(b) for b in bars],
    }


@app.get("/indicators/{ticker}", response_model=IndicatorsResponse)
async def get_indicators(ticker: str):
    """
    Plan §3 swing indicator set + support/resistance zones for one ticker,
    computed from stored daily bars only — never calls the provider.

    Benchmarks (SPY and the sector ETF from `data_engine.stocks.sector`)
    are read from the same bar store; a missing one nulls its RS fields
    and shows `bars: 0` under `benchmarks`. Cached in Redis for 15 min;
    `cached` is set on the way out, not stored. 404 if the ticker has no
    daily bars; 503 if the database is unavailable or any read fails.

    The body lives in `dossier/sections.py` since Part 2.4 — the dossier
    assembles the same snapshot and must not carry a second copy of it.
    """
    ticker = normalize_ticker(ticker)
    if not ticker.isalpha() or len(ticker) > 5:
        raise HTTPException(status_code=400, detail="Invalid ticker format")

    if not app.state.db_pool:
        raise HTTPException(status_code=503, detail="Database unavailable.")

    from dossier.sections import NoBarsStored, indicators_body

    try:
        return await indicators_body(app.state.db_pool, app.state.redis, ticker)
    except NoBarsStored:
        raise HTTPException(status_code=404, detail=f"No stored '1d' bars for '{ticker}'.")
    except Exception as e:
        logger.error(f"Indicators DB read failed for {ticker}: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable.")


@app.get("/dossier/{ticker}", response_model=DossierResponse)
async def get_dossier(ticker: str, horizon: str = Query(HORIZON_SWING)):
    """
    One document per ticker: indicators + zones, news, events,
    recommendations, filings, earnings reactions and profile (spec
    docs/specs/2.4.md).

    Every section carries its own `status`, so a source that is down or
    unconfigured degrades one section and the rest still returns 200 — there
    is no 502 here. Stale bars (> 1 weekday behind the last close) trigger
    one refresh first; if it fails, the stored bars are served with
    `bars.status: stale`. Cached in Redis for 15 min in market hours, 60 min
    outside, and 2 min when any section failed. 400 on a bad ticker or an
    unknown horizon, 404 when there are no bars to describe, 503 when the
    database is unavailable or a read fails.
    """
    from cache import cached_json, dossier_key, dossier_ttl, valid_dossier
    from db import DB_ERRORS
    from dossier.assemble import DossierContext, assemble
    from dossier.sections import NoBarsStored

    try:
        ticker = validate_ticker(ticker)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid ticker format")
    if horizon not in HORIZON_PROFILES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown horizon '{horizon}'. Supported: {', '.join(HORIZON_PROFILES)}.",
        )

    if not app.state.db_pool:
        raise HTTPException(status_code=503, detail="Database unavailable.")

    ctx = DossierContext(
        pool=app.state.db_pool,
        redis=app.state.redis,
        cooldowns=app.state.memory.cooldowns,
        finnhub=app.state.finnhub,
        edgar=app.state.edgar,
        av_client=getattr(app.state, "av_client", None),
        provider=app.state.provider,
        refresh=refresh_ticker_bars,
    )

    built: dict = {}

    async def build() -> dict:
        response = await assemble(ctx, ticker, horizon)
        built["budget"] = response.budget
        # Store the body without `cached` *or* `budget`: both describe the
        # retrieval, not the data (the 1.7 convention). A cached hit that
        # replayed the build's budget would claim upstream calls it never
        # made — the Part 2.5 live check caught exactly that.
        return response.model_dump(mode="json", by_alias=True, exclude={"cached", "budget"})

    market_open = get_market_status()[0] == "market_open"
    started = _time.perf_counter()
    try:
        body, from_cache = await cached_json(
            app.state.redis,
            dossier_key(ticker, horizon),
            None,
            build,
            valid=valid_dossier,
            ttl_for=lambda b: dossier_ttl(b, market_open),
        )
    except NoBarsStored:
        raise HTTPException(
            status_code=404,
            detail=f"No stored '1d' bars for '{ticker}' and the refresh produced none.",
        )
    except DB_ERRORS as e:
        logger.error(f"Dossier DB read failed for {ticker}: {type(e).__name__}: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable.")

    elapsed_ms = int((_time.perf_counter() - started) * 1000)
    budget = built.get("budget")
    if from_cache or budget is None:
        # Served from Redis: no upstream call was made, and the only time
        # spent was the retrieval.
        budget = Budget(upstream_calls=0, elapsed_ms=elapsed_ms, by_source={})

    logger.info(
        f"Dossier {ticker}/{horizon}: cached={from_cache} "
        f"calls={budget.upstream_calls} bySource={budget.by_source} "
        f"elapsed={budget.elapsed_ms}ms"
    )
    return DossierResponse.model_validate({
        **body,
        "cached": from_cache,
        "budget": budget.model_dump(by_alias=True),
    })


@app.get("/market/status")
async def market_status():
    """Get current US market session status."""
    status, et = get_market_status()
    return {
        "status": status,
        "timestamp": et.isoformat(),
        "display": et.strftime("%A %I:%M %p ET"),
    }


# ── News ingest (Part 3.5, spec decision 5) ──────────────────────
# Market news only, sent by risk-shield's poller; stored under _MARKET by the
# existing upsert_news (dedup on (ticker, url)). The limits are spec 3.5
# decision 5's table. risk-shield's converter keeps a copy of the same
# numbers and makes a violation impossible before it sends; each copy is
# pinned by a test (test_ingest_limits_pinned_to_spec here,
# test_converter_limits_pinned_to_spec there). After that, a 422 here means
# the two copies drifted. NUL is refused because Postgres TEXT rejects it:
# it would otherwise surface as a 503 that the poller resends forever.

NEWS_INGEST_MAX_ITEMS = 200
NEWS_URL_MAX = 2048
NEWS_TITLE_MAX = 1000
NEWS_SUMMARY_MAX = 10000
NEWS_SOURCE_MAX = 100
NEWS_DB_UNAVAILABLE_DETAIL = "database unavailable"


class NewsIngestItem(BaseModel):
    model_config = ConfigDict(extra="forbid")   # a `ticker` field is a 422: market news only

    publishedAt: AwareDatetime
    title: str = Field(max_length=NEWS_TITLE_MAX)
    url: str = Field(max_length=NEWS_URL_MAX)
    source: str = Field("", max_length=NEWS_SOURCE_MAX)
    summary: str = Field("", max_length=NEWS_SUMMARY_MAX)

    @field_validator("title", "url", "source", "summary")
    @classmethod
    def _no_nul(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("NUL character not allowed")
        return value

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title is blank")
        return value

    @field_validator("url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        return value


class NewsIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[NewsIngestItem] = Field(min_length=1, max_length=NEWS_INGEST_MAX_ITEMS)


@app.post("/news/ingest")
async def ingest_news(body: NewsIngestRequest):
    """
    Store a batch of market news. One bad item fails the whole request with
    a 422 (the sender is our own code). 200 {received, sent}: `sent` counts
    rows sent after in-batch dedup, not rows inserted (executemany reports no
    count). No pool, or a database error or timeout, is a 503; a raise
    mid-batch can leave earlier rows stored (Part 2.1), and a resend dedups.
    """
    pool = getattr(app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail=NEWS_DB_UNAVAILABLE_DETAIL)

    from db import DB_ERRORS, MARKET_TICKER, upsert_news

    rows = [
        {"ticker": None, "published_at": item.publishedAt, "source": item.source,
         "title": item.title, "url": item.url, "summary": item.summary}
        for item in body.items
    ]
    try:
        sent = await upsert_news(pool, rows)
    except (*DB_ERRORS, TimeoutError) as e:
        logger.warning(f"/news/ingest: database unavailable ({type(e).__name__})")
        raise HTTPException(status_code=503, detail=NEWS_DB_UNAVAILABLE_DETAIL) from None
    logger.info(f"/news/ingest: {len(rows)} received, {sent} sent under {MARKET_TICKER}")
    return {"received": len(rows), "sent": sent}


# ── Market news read (Part 3.6a, spec decision 2) ────────────────
# Postgres only: no cache, no provider, no write. risk-shield's macro inputs
# call it with hours=24&limit=50 and keep a copy of these bounds
# (test_inputs_news_request_within_route_bounds there,
# test_news_market_bounds_pinned_for_risk_shield here). A drift would be a
# 422 on every brief.

NEWS_MARKET_DEFAULT_HOURS = 24
NEWS_MARKET_MAX_HOURS = 168
NEWS_MARKET_DEFAULT_LIMIT = 50
NEWS_MARKET_MAX_LIMIT = 100


@app.get("/news/market")
async def market_news(
    hours: int = Query(NEWS_MARKET_DEFAULT_HOURS, ge=1, le=NEWS_MARKET_MAX_HOURS),
    limit: int = Query(NEWS_MARKET_DEFAULT_LIMIT, ge=1, le=NEWS_MARKET_MAX_LIMIT),
):
    """
    Market news published in the last `hours`, newest first, at most `limit`
    items: [{publishedAt, source, title, summary, url}]. No rows is 200 [],
    never a 404. No pool, or a database error or timeout, is a 503.
    """
    pool = getattr(app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail=NEWS_DB_UNAVAILABLE_DETAIL)

    from db import DB_ERRORS, get_market_news

    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    try:
        rows = await get_market_news(pool, since, limit)
    except (*DB_ERRORS, TimeoutError) as e:
        logger.warning(f"/news/market: database unavailable ({type(e).__name__})")
        raise HTTPException(status_code=503, detail=NEWS_DB_UNAVAILABLE_DETAIL) from None
    return [
        {"id": row["id"],
         "publishedAt": row["published_at"].isoformat(), "source": row["source"],
         "title": row["title"], "summary": row["summary"], "url": row["url"],
         "sentiment": _decode_sentiment(row["sentiment"])}
        for row in rows
    ]


# ── Headline sentiment write-back (Part 4.2, spec decisions 8-10) ─
# ai-agent's classifier owns the values; this route owns the contract. The
# limits exist twice — NewsSentimentRequest here and classifier.ITEM_LIMITS
# there — pinned by test_sentiment_contract_pinned_to_spec and
# test_item_limits_pinned_to_spec. Change both or neither: a drift is a 422
# on every write-back, and write-back is fail-open, so it would fail quietly.
#
# The column is replaced, not merged (decision 9): a re-classification is the
# newer truth, and a merge would leave half an older verdict behind.

SENTIMENT_RELEVANCE = ("high", "medium", "low")
SENTIMENT_CATEGORIES = ("guidance", "analyst", "legal", "product", "macro", "insider", "other")
SENTIMENT_ONE_LINE_MAX = 300
SENTIMENT_MODEL_MAX = 100


def _decode_sentiment(raw):
    """jsonb arrives as text (no codec is registered). A row whose sentiment
    will not parse reads as null rather than failing the whole list — the
    same rule get_events uses for `meta`."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("/news/market: unparseable sentiment on a row, returning null")
        return None
    return value if isinstance(value, dict) else None


class NewsSentimentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relevance: str
    sentiment: float = Field(ge=-1.0, le=1.0)
    category: str
    oneLine: str = Field(max_length=SENTIMENT_ONE_LINE_MAX)
    model: str = Field(max_length=SENTIMENT_MODEL_MAX)
    classifiedAt: AwareDatetime

    @field_validator("relevance")
    @classmethod
    def _known_relevance(cls, value: str) -> str:
        if value not in SENTIMENT_RELEVANCE:
            raise ValueError(f"relevance must be one of {SENTIMENT_RELEVANCE}")
        return value

    @field_validator("category")
    @classmethod
    def _known_category(cls, value: str) -> str:
        if value not in SENTIMENT_CATEGORIES:
            raise ValueError(f"category must be one of {SENTIMENT_CATEGORIES}")
        return value

    @field_validator("oneLine", "model")
    @classmethod
    def _no_nul_not_blank(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("NUL character not allowed")
        if not value.strip():
            raise ValueError("must not be blank")
        return value


@app.post("/news/{news_id}/sentiment")
async def set_news_sentiment_route(news_id: int, body: NewsSentimentRequest):
    """
    Store one headline classification on news_items.sentiment (Part 4.2).

    200 {id, updated: true}. 404 when no row has that id — the caller counts
    it and carries on, because write-back is fail-open on its side. No pool,
    or a database error or timeout, is a 503. Idempotent: a repeat write
    replaces the value.
    """
    pool = getattr(app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail=NEWS_DB_UNAVAILABLE_DETAIL)

    from db import DB_ERRORS, set_news_sentiment

    value = {
        "relevance": body.relevance,
        "sentiment": body.sentiment,
        "category": body.category,
        "oneLine": body.oneLine,
        "model": body.model,
        "classifiedAt": body.classifiedAt.isoformat(),
    }
    try:
        updated = await set_news_sentiment(pool, news_id, value)
    except (*DB_ERRORS, TimeoutError) as e:
        logger.warning(f"/news/{news_id}/sentiment: database unavailable ({type(e).__name__})")
        raise HTTPException(status_code=503, detail=NEWS_DB_UNAVAILABLE_DETAIL) from None
    if not updated:
        raise HTTPException(status_code=404, detail="news item not found")
    logger.info(f"/news/{news_id}/sentiment: stored {body.relevance}/{body.category}")
    return {"id": news_id, "updated": True}
