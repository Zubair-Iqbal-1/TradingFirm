"""
TradingFirm — Market Scanner Pipeline

Orchestrates the full scan flow (optimized: daily first, filter, then hourly):
  1. Pre-screen candidates via Finviz (~650 tickers)
  2. Download DAILY ONLY for all candidates
  3. Apply daily-only filters (ATRP, RVOL, 52w, IPO) → ~20-40 winners
  4. Download HOURLY ONLY for daily winners (~95% fewer API calls)
  5. Apply hourly filters (4H price > 50 EMA, 1H EMA20 > EMA50)
     — persist daily+hourly bars for the final winners (best-effort)
  6. Enrich final winners (name, sector, float, news) + fundamental gates
  7. Sort by quality score (RVOL × ATRP)

Ported from scanner/pro_scan.py into an async, provider-agnostic class.

RATE LIMIT SAFETY:
  - Canary batch: first download batch must succeed or entire scan aborts
  - 10 tickers per batch, 5s delay between batches
  - 2s delay between enrichment calls (yf.Ticker.info)
  - Large DataFrames explicitly deleted after use + gc.collect()
"""

import asyncio
import gc
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import numpy as np

from tickers import normalize_ticker
from indicators import (
    aggregate_4h,
    calc_atr,
    calc_atrp,
    calc_rvol,
    check_52w_position,
    ema,
)
from providers.base import DataProvider
from scanners.market_status import get_market_status, get_minutes_since_open
from scanners.models import ScanResult, StockResult, NewsItem

logger = logging.getLogger(__name__)

# ── Safety Constants ─────────────────────────────────────────────
ENRICHMENT_DELAY = 2.0  # Seconds between yf.Ticker.info calls


class MarketScanner:
    """
    Professional day trading scanner.

    Filters:
      1. Price $10–$40 (configurable)
      2. Market Cap > $500M
      3. Daily Volume > 1M (Finviz pre-filter)
      4. RVOL > 1.2 (live) or > 1.0 (weekend/premarket)
      5. ATRP 2.5%–6.0%
      6. 4H: Price > 50 EMA
      7. 1H: 20 EMA > 50 EMA
      8. Not at 52-week top/bottom 10%
      9. IPO > 6 months (120+ trading days of data)
     10. Float 20M–1B (enrichment gate)
    """

    def __init__(self, provider: DataProvider, db_pool=None, redis=None):
        self.provider = provider
        self.db_pool = db_pool
        # Part 4.8b-de: where a winner's open-session row is kept as
        # sessionSoFar (bar_session.stash_session_so_far). None = not kept.
        self.redis = redis

    async def run_scan(
        self,
        price_min: float = 10.0,
        price_max: float = 40.0,
        advanced: bool = False,
    ) -> ScanResult:
        """
        Execute the full scan pipeline.

        Optimized flow — downloads daily first, filters, then hourly only for winners:
          Step 1: Pre-screen candidates via Finviz (~650 tickers)
          Step 2: Download DAILY ONLY for all candidates
          Step 3: Apply daily-only filters (ATRP, RVOL, 52w, IPO) → ~20-40 winners
          Step 4: Download HOURLY ONLY for daily winners
          Step 5: Apply hourly filters (4H EMA50, 1H EMA20>EMA50) → ~10-20 final
          Step 6: Enrich final winners (name, sector, float, news)
          Step 7: Sort by quality score (RVOL × ATRP)

        Args:
            price_min: Minimum stock price
            price_max: Maximum stock price
            advanced: Enable 5-minute tradability filters (slower)

        Returns:
            ScanResult with all passing stocks, sorted by quality
        """
        t0 = time.time()
        logger.info(f"{'='*60}")
        logger.info(f"  SCAN STARTED — ${price_min}-${price_max} (advanced={advanced})")
        logger.info(f"{'='*60}")

        # ── Step 1: Pre-screen via provider ──
        logger.info("Step 1/7: Pre-screening candidates...")
        tickers, market_status = await self.provider.get_candidates(
            price_min=price_min,
            price_max=price_max,
        )

        if not tickers:
            logger.warning("No candidates found. Returning empty result.")
            return self._empty_result(t0, market_status, total_scanned=0)

        logger.info(f"Step 1/7 complete: {len(tickers)} candidates")

        # ── Step 2: Download DAILY ONLY for all candidates ──
        logger.info(f"Step 2/7: Downloading DAILY data for {len(tickers)} tickers...")
        daily_data = await self.provider.download_daily(tickers)

        # ⚠️ CANARY: if daily download returned nothing, abort
        if daily_data is None or (hasattr(daily_data, 'empty') and daily_data.empty):
            logger.error(
                "Step 2/7 FAILED: Daily download returned no data. "
                "Yahoo may be rate-limiting — wait 15-30 minutes. ABORTING."
            )
            return self._empty_result(t0, market_status, total_scanned=len(tickers))

        logger.info("Step 2/7 complete: Daily downloads finished")

        # ── Step 3: Apply DAILY-ONLY filters ──
        logger.info(f"Step 3/7: Applying daily filters ({market_status} mode)...")
        daily_winners = {}
        daily_frames = {}
        daily_rejections = {}

        for ticker in tickers:
            daily_df = self.provider.extract_ticker_df(daily_data, ticker)
            if daily_df is None:
                continue

            passed, result = self._apply_daily_filters(ticker, daily_df, market_status)

            if passed:
                key = normalize_ticker(ticker)
                daily_winners[key] = result
                daily_frames[key] = daily_df
                logger.debug(
                    f"  ✅ {ticker:6s}  ATRP {result['atrp']:>5.1f}%  "
                    f"RVOL {result['rvol']:>5.2f}x  52w {result['pos52w']:>3d}%"
                )
            else:
                key = result.split("(")[0].split("<")[0].strip()
                daily_rejections[key] = daily_rejections.get(key, 0) + 1

        logger.info(
            f"Step 3/7 complete: {len(daily_winners)} passed daily filters "
            f"/ {len(tickers)} scanned"
        )
        for reason, count in sorted(daily_rejections.items(), key=lambda x: -x[1])[:8]:
            logger.info(f"  {count:>4d}×  {reason}")

        if not daily_winners:
            logger.warning("No tickers passed daily filters. Returning empty result.")
            del daily_data, daily_frames
            gc.collect()
            return self._empty_result(t0, market_status, total_scanned=len(tickers))

        # ⚠️ GARBAGE: release daily data BEFORE downloading hourly
        del daily_data
        gc.collect()
        logger.debug("Released daily DataFrames from memory")

        # ── Step 4: Download HOURLY ONLY for daily winners ──
        daily_winner_tickers = list(daily_winners.keys())
        logger.info(
            f"Step 4/7: Downloading HOURLY data for {len(daily_winner_tickers)} "
            f"daily winners (NOT all {len(tickers)} candidates)"
        )
        hourly_data = await self.provider.download_hourly(daily_winner_tickers)

        hourly_failed = (
            hourly_data is None
            or (hasattr(hourly_data, 'empty') and hourly_data.empty)
        )
        if hourly_failed:
            logger.warning(
                "Step 4/7: Hourly download returned no data. "
                "Skipping hourly filters (degraded mode)."
            )
        else:
            logger.info("Step 4/7 complete: Hourly downloads finished")

        # ── Step 5: Apply HOURLY filters ──
        logger.info(f"Step 5/7: Applying hourly filters to {len(daily_winners)} daily winners...")
        winners = {}
        hourly_frames = {}
        hourly_rejections = {}

        for ticker, daily_filter_data in daily_winners.items():
            if hourly_failed:
                # Degraded mode: skip hourly filters, pass through daily winners.
                # No hourly_df exists to capture in this branch — "1h" bars for
                # these tickers simply won't be persisted this run.
                winners[ticker] = daily_filter_data
                continue

            hourly_df = self.provider.extract_ticker_df(hourly_data, ticker)
            if hourly_df is None:
                hourly_rejections["No hourly data"] = (
                    hourly_rejections.get("No hourly data", 0) + 1
                )
                continue

            passed, result = self._apply_hourly_filters(
                ticker, hourly_df, daily_filter_data
            )

            if passed:
                winners[ticker] = result
                hourly_frames[ticker] = hourly_df
                logger.debug(f"  ✅ {ticker:6s}  Passed hourly filters")
            else:
                key = result.split("(")[0].split("<")[0].strip()
                hourly_rejections[key] = hourly_rejections.get(key, 0) + 1

        logger.info(
            f"Step 5/7 complete: {len(winners)} passed hourly filters "
            f"/ {len(daily_winners)} daily winners"
        )
        for reason, count in sorted(hourly_rejections.items(), key=lambda x: -x[1])[:5]:
            logger.info(f"  {count:>4d}×  {reason}")

        # ── Persist bars for final winners only (best-effort) ──
        await self._persist_winner_bars(winners, daily_frames, hourly_frames)

        # ⚠️ GARBAGE: release hourly data + winner frames before enrichment
        if not hourly_failed:
            del hourly_data
        del daily_frames, hourly_frames
        gc.collect()
        logger.debug("Released hourly DataFrames from memory")

        # ── Step 6: Enrich winners ──
        logger.info(
            f"Step 6/7: Enriching {len(winners)} winners "
            f"(with {ENRICHMENT_DELAY}s delays)..."
        )
        results = []
        enrich_count = 0

        for ticker, filter_data in winners.items():
            # ⚠️ RATE LIMIT: delay between yf.Ticker.info calls
            if enrich_count > 0:
                await asyncio.sleep(ENRICHMENT_DELAY)
            enrich_count += 1

            try:
                enriched = await self._enrich_stock(ticker, filter_data)
                if enriched is not None:
                    results.append(enriched)
                    logger.debug(
                        f"  ✅ {ticker:6s}  {enriched.name:<30s}  "
                        f"{enriched.industry:<25s}  Float: {enriched.float_str}"
                    )
            except Exception as e:
                logger.warning(f"  ❌ {ticker}: Enrichment failed — {e}")

        # ── Step 7: Sort by quality (RVOL × ATRP) ──
        results.sort(
            key=lambda s: (s.rvol or 0) * (s.atrp or 0),
            reverse=True,
        )

        duration = time.time() - t0
        logger.info(f"{'='*60}")
        logger.info(f"  SCAN COMPLETE: {len(results)} stocks in {duration:.1f}s")
        logger.info(
            f"  Pipeline: {len(tickers)} screened → {len(daily_winners)} daily "
            f"→ {len(winners)} hourly → {len(results)} enriched"
        )
        logger.info(f"{'='*60}")

        return ScanResult(
            timestamp=datetime.now(timezone.utc),
            duration_seconds=round(duration, 2),
            market_status=market_status,
            total_scanned=len(tickers),
            passed_count=len(results),
            stocks=results,
        )

    def _empty_result(
        self, t0: float, market_status: str, total_scanned: int
    ) -> ScanResult:
        """Helper to return an empty ScanResult without repeating boilerplate."""
        return ScanResult(
            timestamp=datetime.now(timezone.utc),
            duration_seconds=time.time() - t0,
            market_status=market_status,
            total_scanned=total_scanned,
            passed_count=0,
            stocks=[],
        )

    async def _persist_winner_bars(
        self,
        winners: dict,
        daily_frames: dict,
        hourly_frames: dict,
    ) -> None:
        """
        Upsert daily + hourly bars for final scan winners into
        data_engine.ohlcv_bars. Winner-only, best-effort: a missing db_pool
        or a failed upsert for one ticker is logged and skipped, never
        aborts the scan (the scan's job is to return ScanResult; bar
        persistence is a side effect of it).

        `winners`/`daily_frames`/`hourly_frames` keys are already
        normalized (see tickers.normalize_ticker() applied when a ticker enters
        daily_winners in Step 3) — no re-normalization needed here.
        """
        if self.db_pool is None or not winners:
            return

        from bar_session import drop_open_session_bars, stash_session_so_far, utc_now
        from db import bar_records_from_df, upsert_bars

        # Part 4.8b-de: ranking above used today's partial bar in memory; the
        # save drops it (spec 4.8b decision 15).
        now = utc_now()
        for ticker in winners:
            for interval, frame in (
                ("1d", daily_frames.get(ticker)),
                ("1h", hourly_frames.get(ticker)),
            ):
                if frame is None:
                    continue
                try:
                    kept, _open = drop_open_session_bars(bar_records_from_df(frame), interval, now)
                    await upsert_bars(self.db_pool, ticker, interval, kept)
                    if interval == "1d":
                        await stash_session_so_far(self.redis, ticker, _open, kept, now)
                except Exception as e:
                    logger.warning(f"Bar persist failed for {ticker} ({interval}): {e}")

    # ── Private: Daily-Only Filters (split from old _apply_filters) ──

    def _apply_daily_filters(
        self,
        ticker: str,
        daily_df,
        market_status: str,
    ) -> tuple[bool, any]:
        """
        Apply daily-only technical filters to a single stock.
        These filters use only daily OHLCV data — no hourly data needed.

        Filters applied:
          1. IPO / history check (120+ trading days ≈ 6 months)
          2. ATRP 2.5% – 6.0%
          3. RVOL > threshold (time-adjusted for live market)
          4. 52-week position not in top/bottom 10%

        Returns:
            (True, data_dict) if passed, or (False, reason_string) if rejected.
        """
        closes = daily_df["Close"]

        # ── IPO / history check (120 trading days ≈ 6 months) ──
        if len(daily_df) < 120:
            return False, f"Too new ({len(daily_df)} days < 120)"

        # ── ATRP 2.5% – 6.0% ──
        atrp = calc_atrp(daily_df["High"], daily_df["Low"], closes)
        if np.isnan(atrp):
            return False, "ATR calc failed"
        if atrp < 2.5:
            return False, f"ATRP {atrp:.1f}% < 2.5%"
        if atrp > 6.0:
            return False, f"ATRP {atrp:.1f}% > 6%"

        # ── RVOL (time-adjusted for live market) ──
        if len(daily_df) < 22:
            return False, "Not enough days for RVOL"

        last_vol = float(daily_df["Volume"].iloc[-1])

        # Scale partial-day volume during market hours
        scale = 1.0
        if market_status == "market_open":
            mins_elapsed = get_minutes_since_open()
            mins_elapsed = max(mins_elapsed, 5)  # avoid divide-by-zero
            scale = 390.0 / mins_elapsed

        rvol = calc_rvol(daily_df["Volume"], last_vol, lookback=20, scale_factor=scale)

        rvol_floor = 1.0 if market_status in ("weekend", "pre_market") else 1.2
        if rvol < rvol_floor:
            return False, f"RVOL {rvol:.2f}"

        # ── 52-week position: not in top/bottom 10% ──
        pos = check_52w_position(closes)
        if pos > 0.90:
            return False, f"52w high zone ({pos:.0%})"
        if pos < 0.10:
            return False, f"52w low zone ({pos:.0%})"

        # ── All daily filters passed ──
        atr_series = calc_atr(daily_df["High"], daily_df["Low"], closes)
        return True, {
            "atrp": round(atrp, 2),
            "atr": round(float(atr_series.iloc[-1]), 2),
            "rvol": round(rvol, 2),
            "pos52w": round(pos * 100),
            "price": round(float(closes.iloc[-1]), 2),
            "hi52": round(float(closes.max()), 2),
            "lo52": round(float(closes.min()), 2),
        }

    # ── Private: Hourly Filters (new — split from _apply_filters) ──

    def _apply_hourly_filters(
        self,
        ticker: str,
        hourly_df,
        daily_filter_data: dict,
    ) -> tuple[bool, any]:
        """
        Apply hourly-based technical filters to a single stock.
        Only called on stocks that already passed daily filters.

        Filters applied:
          1. 4H: Price > 50 EMA (aggregate hourly → 4H first)
          2. 1H: EMA 20 > EMA 50

        Args:
            ticker: Stock symbol
            hourly_df: 1-hour OHLCV DataFrame
            daily_filter_data: Data dict from _apply_daily_filters (passed through)

        Returns:
            (True, merged_data_dict) if passed, or (False, reason_string) if rejected.
        """
        # ── 4H: Price > 50 EMA ──
        h4 = aggregate_4h(hourly_df)
        if len(h4) < 55:
            return False, f"4H data short ({len(h4)} bars)"

        ema50_4h = ema(h4["Close"], 50).iloc[-1]
        price_4h = h4["Close"].iloc[-1]
        if price_4h <= ema50_4h:
            return False, "4H Price < 50 EMA"

        # ── 1H: EMA 20 > EMA 50 ──
        if len(hourly_df) < 55:
            return False, f"1H data short ({len(hourly_df)} bars)"

        ema20_1h = ema(hourly_df["Close"], 20).iloc[-1]
        ema50_1h = ema(hourly_df["Close"], 50).iloc[-1]
        if ema20_1h <= ema50_1h:
            return False, "1H EMA20 < EMA50"

        # ── All hourly filters passed — return daily data unchanged ──
        return True, daily_filter_data

    # ── Private: Enrichment ──────────────────────────────────────

    async def _enrich_stock(
        self,
        ticker: str,
        filter_data: dict,
    ) -> Optional[StockResult]:
        """
        Enrich a stock with fundamental data and apply final gates.

        Returns StockResult if it passes market cap and float gates,
        or None if rejected.
        """
        info = await self.provider.get_stock_info(ticker)

        # ── Market cap gate: > $500M ──
        market_cap = info.get("marketCap", 0) or 0
        if market_cap < 500_000_000:
            mc_str = f"${market_cap / 1e6:.0f}M" if market_cap > 0 else "N/A"
            logger.info(f"  ❌ {ticker:6s}  Market cap {mc_str} < $500M — rejected")
            return None

        # ── Float gate: 20M – 1B ──
        float_shares = info.get("floatShares", 0) or 0
        float_str = info.get("floatStr", "N/A")
        if 0 < float_shares < 20_000_000:
            logger.info(f"  ❌ {ticker:6s}  Float {float_str} < 20M — too illiquid")
            return None
        if float_shares > 1_000_000_000:
            logger.info(f"  ❌ {ticker:6s}  Float {float_str} > 1B — too slow")
            return None

        # ── Build result ──
        news_items = [
            NewsItem(
                title=n.get("title", ""),
                url=n.get("url", ""),
                publisher=n.get("publisher", ""),
            )
            for n in info.get("news", [])
        ]

        return StockResult(
            symbol=ticker,
            name=info.get("name", ""),
            price=filter_data["price"],
            sector=info.get("sector", "Other"),
            industry=info.get("industry", "Other"),
            market_cap=market_cap,
            float_shares=float_shares,
            float_str=float_str,
            fifty_two_week_high=filter_data["hi52"],
            fifty_two_week_low=filter_data["lo52"],
            atrp=filter_data["atrp"],
            atr=filter_data["atr"],
            rvol=filter_data["rvol"],
            pos_52w=filter_data["pos52w"],
            news=news_items,
        )
