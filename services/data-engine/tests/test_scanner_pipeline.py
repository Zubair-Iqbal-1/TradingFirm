"""
TradingFirm — Scanner Pipeline Tests

Tests for the restructured daily-first pipeline:
  - Daily filter pass/reject cases
  - Hourly filter pass/reject cases
  - Pipeline integration: download_hourly called with only daily winners

All tests use synthetic OHLCV data and a mocked provider.
Zero live API calls.
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest

# ── Synthetic Data Generators ────────────────────────────────────


def make_daily_df(
    rows: int = 150,
    base_price: float = 25.0,
    daily_range: float = 1.0,
    volume_avg: float = 1_500_000,
    volume_last: float = 2_000_000,
    trend: float = 0.01,
) -> pd.DataFrame:
    """
    Generate synthetic daily OHLCV data.

    Args:
        rows: Number of trading days
        base_price: Starting price
        daily_range: Avg High-Low range (controls ATRP)
        volume_avg: Average volume for days 1..N-1
        volume_last: Volume for the last day (controls RVOL)
        trend: Per-bar price increment (controls 52w position)
    """
    dates = pd.bdate_range(end=datetime.now(), periods=rows)
    # Create a price path that peaks mid-series and pulls back,
    # so the 52-week position ends around 50% (not at extreme)
    np.random.seed(42)
    peak_idx = rows * 2 // 3  # peak at 2/3 through the series
    up = np.linspace(0, trend * rows, peak_idx)
    down = np.linspace(trend * rows, trend * rows * 0.5, rows - peak_idx)
    closes = base_price + np.concatenate([up, down])
    closes = closes + np.random.normal(0, 0.05, rows)

    highs = closes + daily_range / 2
    lows = closes - daily_range / 2

    volumes = np.full(rows, volume_avg, dtype=float)
    volumes[-1] = volume_last

    return pd.DataFrame(
        {"Open": closes - 0.1, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=dates,
    )


def make_hourly_df(
    rows: int = 300,
    base_price: float = 25.0,
    trend: float = 0.005,
) -> pd.DataFrame:
    """
    Generate synthetic hourly OHLCV data.

    For the EMA filters to pass, we need a consistent uptrend.
    For them to fail, use a downtrend (negative trend).
    """
    dates = pd.date_range(end=datetime.now(), periods=rows, freq="h")
    closes = base_price + np.arange(rows) * trend
    np.random.seed(42)
    closes = closes + np.random.normal(0, 0.02, rows)

    highs = closes + 0.15
    lows = closes - 0.15

    volumes = np.full(rows, 50_000, dtype=float)

    return pd.DataFrame(
        {"Open": closes - 0.05, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=dates,
    )


# ── Test: Daily Filters ─────────────────────────────────────────


class TestDailyFilters:
    """Tests for _apply_daily_filters() in isolation."""

    def _make_scanner(self):
        """Create a MarketScanner with a dummy provider."""
        from scanners.market_scanner import MarketScanner

        mock_provider = MagicMock()
        return MarketScanner(mock_provider)

    def test_daily_filters_pass(self):
        """Ticker with valid ATRP, RVOL, 52w position passes daily filters."""
        scanner = self._make_scanner()

        # daily_range=1.0 on $25 stock -> ATRP ~ 4% (within 2.5-6%)
        # volume_last=2M vs avg=1.5M -> RVOL ~ 1.33 (> 1.0 weekend)
        # trend=0.01 over 150 days -> price moves ~$1.50, 52w pos ~ 50%
        daily_df = make_daily_df(
            rows=150,
            base_price=25.0,
            daily_range=1.0,
            volume_avg=1_500_000,
            volume_last=2_000_000,
            trend=0.01,
        )

        passed, result = scanner._apply_daily_filters("TEST", daily_df, "weekend")

        assert passed is True, f"Expected pass but got rejection: {result}"
        assert isinstance(result, dict)
        assert "atrp" in result
        assert "rvol" in result
        assert "pos52w" in result
        assert "price" in result
        assert 2.5 <= result["atrp"] <= 6.0, f"ATRP {result['atrp']} out of range"
        assert result["rvol"] > 1.0, f"RVOL {result['rvol']} should be > 1.0"
        print(f"  PASS: ATRP={result['atrp']}%, RVOL={result['rvol']}x, 52w={result['pos52w']}%")

    def test_daily_filters_reject_atrp_too_low(self):
        """Ticker with ATRP < 2.5% gets rejected."""
        scanner = self._make_scanner()

        # daily_range=0.2 on $25 stock -> ATRP ~ 0.8% (< 2.5%)
        daily_df = make_daily_df(
            rows=150,
            base_price=25.0,
            daily_range=0.2,
            volume_avg=1_500_000,
            volume_last=2_000_000,
        )

        passed, result = scanner._apply_daily_filters("SLOW", daily_df, "weekend")

        assert passed is False, f"Expected rejection but passed with: {result}"
        assert "ATRP" in result, f"Rejection reason should mention ATRP: {result}"
        print(f"  REJECTED: {result}")

    def test_daily_filters_reject_rvol_too_low(self):
        """Ticker with low RVOL gets rejected."""
        scanner = self._make_scanner()

        # volume_last=500K vs avg=1.5M -> RVOL ~ 0.33 (< 1.0)
        daily_df = make_daily_df(
            rows=150,
            base_price=25.0,
            daily_range=1.0,
            volume_avg=1_500_000,
            volume_last=500_000,
        )

        passed, result = scanner._apply_daily_filters("DEAD", daily_df, "weekend")

        assert passed is False, f"Expected rejection but passed with: {result}"
        assert "RVOL" in result, f"Rejection reason should mention RVOL: {result}"
        print(f"  REJECTED: {result}")

    def test_daily_filters_reject_too_new(self):
        """Ticker with < 120 trading days gets rejected (IPO check)."""
        scanner = self._make_scanner()

        daily_df = make_daily_df(rows=60, base_price=25.0, daily_range=1.0)

        passed, result = scanner._apply_daily_filters("IPO", daily_df, "weekend")

        assert passed is False, f"Expected rejection but passed with: {result}"
        assert "Too new" in result, f"Rejection reason should mention 'Too new': {result}"
        print(f"  REJECTED: {result}")


# ── Test: Hourly Filters ────────────────────────────────────────


class TestHourlyFilters:
    """Tests for _apply_hourly_filters() in isolation."""

    def _make_scanner(self):
        from scanners.market_scanner import MarketScanner

        mock_provider = MagicMock()
        return MarketScanner(mock_provider)

    def test_hourly_filters_pass(self):
        """Ticker with uptrending hourly data passes 4H/1H EMA filters."""
        scanner = self._make_scanner()

        # Uptrend -> EMA20 > EMA50, price > 50 EMA
        hourly_df = make_hourly_df(rows=300, base_price=25.0, trend=0.005)
        daily_data = {"atrp": 4.0, "rvol": 1.5, "price": 25.0}

        passed, result = scanner._apply_hourly_filters("UP", hourly_df, daily_data)

        assert passed is True, f"Expected pass but got rejection: {result}"
        assert isinstance(result, dict)
        # Should return the daily_data passed through
        assert result["atrp"] == 4.0
        assert result["rvol"] == 1.5
        print(f"  PASS: Hourly filters passed, data preserved")

    def test_hourly_filters_reject_downtrend(self):
        """Ticker with downtrending hourly data fails EMA filters."""
        scanner = self._make_scanner()

        # Strong downtrend -> EMA20 < EMA50
        hourly_df = make_hourly_df(rows=300, base_price=30.0, trend=-0.01)
        daily_data = {"atrp": 4.0, "rvol": 1.5, "price": 25.0}

        passed, result = scanner._apply_hourly_filters("DOWN", hourly_df, daily_data)

        assert passed is False, f"Expected rejection but passed with: {result}"
        assert isinstance(result, str)
        print(f"  REJECTED: {result}")


# ── Test: Pipeline Integration ──────────────────────────────────


class TestPipelineIntegration:
    """Test that download_hourly is called with only daily winners."""

    @pytest.mark.asyncio
    async def test_pipeline_downloads_hourly_for_daily_winners_only(self):
        """
        Core pipeline test: verify that download_hourly() receives only
        the tickers that passed daily filters, NOT all candidates.
        """
        from scanners.market_scanner import MarketScanner

        # Set up 3 tickers -- only GOOD will pass daily filters
        all_tickers = ["GOOD", "BAD_ATRP", "BAD_RVOL"]

        # Build daily data: GOOD passes, others fail
        good_daily = make_daily_df(
            rows=150, base_price=25.0, daily_range=1.0,
            volume_avg=1_500_000, volume_last=2_000_000, trend=0.01,
        )
        bad_atrp_daily = make_daily_df(
            rows=150, base_price=25.0, daily_range=0.2,  # ATRP too low
            volume_avg=1_500_000, volume_last=2_000_000,
        )
        bad_rvol_daily = make_daily_df(
            rows=150, base_price=25.0, daily_range=1.0,
            volume_avg=1_500_000, volume_last=500_000,  # RVOL too low
        )

        # Build hourly data for GOOD (uptrend -> passes hourly filters)
        good_hourly = make_hourly_df(rows=300, base_price=25.0, trend=0.005)

        # Mock provider
        mock_provider = AsyncMock()
        mock_provider.provider_name = "mock"

        # get_candidates returns all 3 tickers
        mock_provider.get_candidates = AsyncMock(
            return_value=(all_tickers, "weekend")
        )

        # download_daily returns a fake bulk object
        # Must set empty=False so canary check doesn't abort
        daily_bulk = MagicMock()
        daily_bulk.empty = False
        mock_provider.download_daily = AsyncMock(return_value=daily_bulk)

        # download_hourly returns a different fake bulk object
        hourly_bulk = MagicMock()
        hourly_bulk.empty = False
        mock_provider.download_hourly = AsyncMock(return_value=hourly_bulk)

        # extract_ticker_df returns the appropriate data per ticker and bulk
        def extract_fn(bulk, ticker):
            if bulk is daily_bulk:
                return {
                    "GOOD": good_daily,
                    "BAD_ATRP": bad_atrp_daily,
                    "BAD_RVOL": bad_rvol_daily,
                }.get(ticker)
            if bulk is hourly_bulk:
                return {"GOOD": good_hourly}.get(ticker)
            return None

        mock_provider.extract_ticker_df = MagicMock(side_effect=extract_fn)

        # get_stock_info for enrichment (GOOD passes gates)
        mock_provider.get_stock_info = AsyncMock(return_value={
            "name": "Good Corp",
            "sector": "Technology",
            "industry": "Software",
            "marketCap": 5_000_000_000,
            "floatShares": 100_000_000,
            "floatStr": "100M",
            "news": [],
        })

        # Run the scanner
        scanner = MarketScanner(mock_provider)
        result = await scanner.run_scan(price_min=10.0, price_max=40.0)

        # ── KEY ASSERTION: download_hourly was called with ONLY ["GOOD"] ──
        mock_provider.download_hourly.assert_called_once()
        hourly_call_tickers = mock_provider.download_hourly.call_args[0][0]
        assert "GOOD" in hourly_call_tickers, (
            f"GOOD should be in hourly download list: {hourly_call_tickers}"
        )
        assert "BAD_ATRP" not in hourly_call_tickers, (
            f"BAD_ATRP should NOT be in hourly download list: {hourly_call_tickers}"
        )
        assert "BAD_RVOL" not in hourly_call_tickers, (
            f"BAD_RVOL should NOT be in hourly download list: {hourly_call_tickers}"
        )
        assert len(hourly_call_tickers) == 1, (
            f"Expected 1 ticker for hourly download, got {len(hourly_call_tickers)}: "
            f"{hourly_call_tickers}"
        )

        print(f"  download_hourly called with {hourly_call_tickers} (not all {len(all_tickers)})")
        print(f"  Scan result: {result.passed_count} stocks passed")


# ── Test: Part 1.3 — scan persists bars for winners ─────────────


def _make_mock_provider(all_tickers, daily_bulk, hourly_bulk, daily_map, hourly_map):
    """Mock provider whose extract_ticker_df dispatches by upper-cased
    ticker, so callers can exercise both raw-case candidates and the
    normalized keys the scanner produces downstream."""
    mock_provider = AsyncMock()
    mock_provider.provider_name = "mock"
    mock_provider.get_candidates = AsyncMock(return_value=(all_tickers, "weekend"))
    mock_provider.download_daily = AsyncMock(return_value=daily_bulk)
    mock_provider.download_hourly = AsyncMock(return_value=hourly_bulk)

    def extract_fn(bulk, ticker):
        key = ticker.upper()
        if bulk is daily_bulk:
            return daily_map.get(key)
        if bulk is hourly_bulk:
            return hourly_map.get(key)
        return None

    mock_provider.extract_ticker_df = MagicMock(side_effect=extract_fn)
    mock_provider.get_stock_info = AsyncMock(return_value={
        "name": "Good Corp",
        "sector": "Technology",
        "industry": "Software",
        "marketCap": 5_000_000_000,
        "floatShares": 100_000_000,
        "floatStr": "100M",
        "news": [],
    })
    return mock_provider


class TestPipelinePersistsWinnerBars:
    """Part 1.3: scan pipeline persists daily + hourly bars for final
    winners only, via db.upsert_bars, with normalized ticker keys."""

    @pytest.mark.asyncio
    async def test_persists_bars_for_final_winners_only_with_normalized_keys(self):
        from scanners.market_scanner import MarketScanner

        # "good" (lowercase candidate) passes both daily + hourly filters.
        good_daily = make_daily_df(
            rows=150, base_price=25.0, daily_range=1.0,
            volume_avg=1_500_000, volume_last=2_000_000, trend=0.01,
        )
        good_hourly = make_hourly_df(rows=300, base_price=25.0, trend=0.005)

        # Fails daily filters (ATRP too low) — never reaches winners.
        bad_atrp_daily = make_daily_df(
            rows=150, base_price=25.0, daily_range=0.2,
            volume_avg=1_500_000, volume_last=2_000_000,
        )

        # Passes daily filters but fails hourly (downtrend) — reaches
        # daily_winners/daily_frames but must NOT be persisted at all.
        good_but_down_daily = make_daily_df(
            rows=150, base_price=25.0, daily_range=1.0,
            volume_avg=1_500_000, volume_last=2_000_000, trend=0.01,
        )
        good_but_down_hourly = make_hourly_df(rows=300, base_price=30.0, trend=-0.01)

        daily_bulk = MagicMock()
        daily_bulk.empty = False
        hourly_bulk = MagicMock()
        hourly_bulk.empty = False

        daily_map = {
            "GOOD": good_daily,
            "BAD_ATRP": bad_atrp_daily,
            "GOOD_BUT_DOWN": good_but_down_daily,
        }
        hourly_map = {
            "GOOD": good_hourly,
            "GOOD_BUT_DOWN": good_but_down_hourly,
        }

        mock_provider = _make_mock_provider(
            ["good", "BAD_ATRP", "GOOD_BUT_DOWN"], daily_bulk, hourly_bulk, daily_map, hourly_map,
        )

        scanner = MarketScanner(mock_provider, db_pool=MagicMock())

        with patch("db.upsert_bars", new=AsyncMock(return_value=1)) as mock_upsert:
            result = await scanner.run_scan(price_min=10.0, price_max=40.0)

        calls = mock_upsert.call_args_list
        # upsert_bars(pool, ticker, interval, bars) — positional call.
        call_keys = {(c.args[1], c.args[2]) for c in calls}

        assert call_keys == {("GOOD", "1d"), ("GOOD", "1h")}, (
            f"Expected exactly the normalized-key GOOD 1d/1h upserts, got {call_keys}"
        )
        assert not any(c.args[1] == "BAD_ATRP" for c in calls), "non-daily-winner was persisted"
        assert not any(c.args[1] == "GOOD_BUT_DOWN" for c in calls), (
            "daily winner that failed hourly filters was persisted"
        )
        assert not any(c.args[1] == "good" for c in calls), "persisted under raw (non-normalized) key"

        assert result.passed_count == 1
        assert result.stocks[0].symbol == "GOOD", "winner symbol should be the normalized ticker"
        print(f"  upsert_bars calls: {call_keys}")

    @pytest.mark.asyncio
    async def test_skips_persist_when_pool_none(self):
        from scanners.market_scanner import MarketScanner

        good_daily = make_daily_df(
            rows=150, base_price=25.0, daily_range=1.0,
            volume_avg=1_500_000, volume_last=2_000_000, trend=0.01,
        )
        good_hourly = make_hourly_df(rows=300, base_price=25.0, trend=0.005)
        daily_bulk = MagicMock()
        daily_bulk.empty = False
        hourly_bulk = MagicMock()
        hourly_bulk.empty = False

        mock_provider = _make_mock_provider(
            ["GOOD"], daily_bulk, hourly_bulk, {"GOOD": good_daily}, {"GOOD": good_hourly},
        )

        scanner = MarketScanner(mock_provider)  # db_pool defaults to None

        with patch("db.upsert_bars", new=AsyncMock()) as mock_upsert:
            result = await scanner.run_scan(price_min=10.0, price_max=40.0)

        mock_upsert.assert_not_called()
        assert result.passed_count == 1
        print("  no db_pool -> persist skipped, scan still returned results")

    @pytest.mark.asyncio
    async def test_continues_on_upsert_error(self):
        from scanners.market_scanner import MarketScanner

        good_daily = make_daily_df(
            rows=150, base_price=25.0, daily_range=1.0,
            volume_avg=1_500_000, volume_last=2_000_000, trend=0.01,
        )
        good_hourly = make_hourly_df(rows=300, base_price=25.0, trend=0.005)
        daily_bulk = MagicMock()
        daily_bulk.empty = False
        hourly_bulk = MagicMock()
        hourly_bulk.empty = False

        mock_provider = _make_mock_provider(
            ["GOOD"], daily_bulk, hourly_bulk, {"GOOD": good_daily}, {"GOOD": good_hourly},
        )

        scanner = MarketScanner(mock_provider, db_pool=MagicMock())

        with patch(
            "db.upsert_bars", new=AsyncMock(side_effect=Exception("db exploded"))
        ) as mock_upsert:
            result = await scanner.run_scan(price_min=10.0, price_max=40.0)

        # Both the 1d and 1h attempts still happen despite each raising.
        assert mock_upsert.call_count == 2
        assert result.passed_count == 1
        assert result.stocks[0].symbol == "GOOD"
        print("  upsert_bars raised on every call, scan still completed normally")

    @pytest.mark.asyncio
    async def test_no_upsert_when_no_final_winners(self):
        """daily_winners is non-empty (GOOD_BUT_DOWN passes daily filters)
        but every one fails hourly filters, so winners ends up empty —
        exercises _persist_winner_bars' own empty-winners guard."""
        from scanners.market_scanner import MarketScanner

        good_but_down_daily = make_daily_df(
            rows=150, base_price=25.0, daily_range=1.0,
            volume_avg=1_500_000, volume_last=2_000_000, trend=0.01,
        )
        good_but_down_hourly = make_hourly_df(rows=300, base_price=30.0, trend=-0.01)
        daily_bulk = MagicMock()
        daily_bulk.empty = False
        hourly_bulk = MagicMock()
        hourly_bulk.empty = False

        mock_provider = _make_mock_provider(
            ["GOOD_BUT_DOWN"], daily_bulk, hourly_bulk,
            {"GOOD_BUT_DOWN": good_but_down_daily}, {"GOOD_BUT_DOWN": good_but_down_hourly},
        )

        scanner = MarketScanner(mock_provider, db_pool=MagicMock())

        with patch("db.upsert_bars", new=AsyncMock()) as mock_upsert:
            result = await scanner.run_scan(price_min=10.0, price_max=40.0)

        mock_upsert.assert_not_called()
        assert result.passed_count == 0
        print("  no final winners -> zero upsert_bars calls")

    @pytest.mark.asyncio
    async def test_repeat_persist_calls_upsert_again_each_time(self):
        """Consecutive persist calls for the same winner (as back-to-back
        scheduled scans would produce) re-upsert every time — idempotency
        is delegated to upsert_bars' ON CONFLICT DO UPDATE, not to any
        client-side dedup/skip logic here."""
        from scanners.market_scanner import MarketScanner

        good_daily = make_daily_df(
            rows=150, base_price=25.0, daily_range=1.0,
            volume_avg=1_500_000, volume_last=2_000_000, trend=0.01,
        )
        good_hourly = make_hourly_df(rows=300, base_price=25.0, trend=0.005)

        scanner = MarketScanner(MagicMock(), db_pool=MagicMock())
        winners = {"GOOD": {"price": 25.0}}
        daily_frames = {"GOOD": good_daily}
        hourly_frames = {"GOOD": good_hourly}

        with patch("db.upsert_bars", new=AsyncMock(return_value=1)) as mock_upsert:
            await scanner._persist_winner_bars(winners, daily_frames, hourly_frames)
            await scanner._persist_winner_bars(winners, daily_frames, hourly_frames)

        assert mock_upsert.call_count == 4  # 2 intervals x 2 runs, no dedup
        print("  repeat persist calls re-upsert every time (no client-side cache)")


@pytest.mark.asyncio
async def test_scanner_persist_drops_open_session_bar(monkeypatch):
    """Part 4.8b-de (spec 4.8b decision 15): the scan ranks on today's partial
    bar in memory; the save drops it. Frozen at 13:00 ET on 2026-09-24."""
    import bar_session
    from scanners.market_scanner import MarketScanner

    monkeypatch.setattr(bar_session, "utc_now", lambda: datetime(2026, 9, 24, 17, 0, tzinfo=timezone.utc))
    day_index = pd.DatetimeIndex([datetime(2026, 9, 22), datetime(2026, 9, 23), datetime(2026, 9, 24)])
    hour_index = pd.DatetimeIndex([datetime(2026, 9, 24, 15, 30, tzinfo=timezone.utc),
                                   datetime(2026, 9, 24, 16, 30, tzinfo=timezone.utc)])
    cols = {"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 1}
    daily_frames = {"GOOD": pd.DataFrame(cols, index=day_index)}
    hourly_frames = {"GOOD": pd.DataFrame(cols, index=hour_index)}
    scanner = MarketScanner(MagicMock(), db_pool=MagicMock())

    with patch("db.upsert_bars", new=AsyncMock(return_value=1)) as mock_upsert:
        await scanner._persist_winner_bars({"GOOD": {}}, daily_frames, hourly_frames)

    stored = {c.args[2]: [r["ts"] for r in c.args[3]] for c in mock_upsert.call_args_list}
    assert stored["1d"] == [datetime(2026, 9, 22), datetime(2026, 9, 23)]
    assert stored["1h"] == [datetime(2026, 9, 24, 15, 30, tzinfo=timezone.utc)]
