"""
Tests for the indicators package (Part 1.5).

Every expectation is hand-computed on a tiny frame with a small period so it
can be checked on paper. Fractions are left as fractions on purpose — they
show the derivation. No network, no fixtures, no I/O.

Conventions under test (docs/decisions.md, Part 1.5):
  - rsi(): SMA-seeded Wilder, first `period` rows NaN
  - macd(): reference convention, no warm-up mask
  - divisions never return inf (extension on ATR 0, gap on prev close 0)
  - empty input: series functions return empty, scalar functions return NaN,
    calc_rvol keeps 0.0
"""

import numpy as np
import pandas as pd
import pytest

import indicators
from indicators import (
    Zone,
    aggregate_4h,
    avg_dollar_volume,
    calc_atr,
    calc_atrp,
    calc_rvol,
    check_52w_position,
    ema,
    extension,
    gap,
    macd,
    relative_strength,
    rsi,
    sector_etf,
    support_resistance,
    swing_snapshot,
    zone_to_dict,
)

# ── Shared tiny frames ────────────────────────────────────────────────────

# 5 bars used by the ATR family. True ranges by hand:
#   bar0: h-l=2, no prev close             -> TR 2
#   bar1: h-l=3, |12-9|=3, |9-9|=0         -> TR 3
#   bar2: h-l=4, |11-11|=0, |7-11|=4       -> TR 4
#   bar3: h-l=3, |13-8|=5, |10-8|=2        -> TR 5
#   bar4: h-l=3, |12-12|=0, |9-12|=3       -> TR 3
HIGH = pd.Series([10.0, 12.0, 11.0, 13.0, 12.0])
LOW = pd.Series([8.0, 9.0, 7.0, 10.0, 9.0])
CLOSE = pd.Series([9.0, 11.0, 8.0, 12.0, 10.0])

EMPTY = pd.Series([], dtype=float)
NAN5 = pd.Series([np.nan] * 5)


def _all_nan(x) -> bool:
    arr = x.to_numpy() if hasattr(x, "to_numpy") else np.asarray(x, dtype=float)
    return bool(np.isnan(arr.astype(float)).all())


# ── Happy path: moved functions ───────────────────────────────────────────


def test_ema_matches_hand_computed():
    # span=3 -> alpha=0.5, adjust=False: e_t = 0.5*x_t + 0.5*e_{t-1}
    out = ema(pd.Series([1.0, 2.0, 3.0, 4.0, 5.0]), 3)
    np.testing.assert_allclose(out.to_numpy(), [1.0, 1.5, 2.25, 3.125, 4.0625])


def test_calc_atr_matches_hand_computed():
    # rolling(3).mean() over TR = [2, 3, 4, 5, 3]
    out = calc_atr(HIGH, LOW, CLOSE, period=3)
    np.testing.assert_allclose(out.to_numpy(), [np.nan, np.nan, 3.0, 4.0, 4.0])


def test_calc_atrp_matches_hand_computed():
    # last ATR 4 / last close 10 * 100
    assert calc_atrp(HIGH, LOW, CLOSE, period=3) == pytest.approx(40.0)


def test_calc_rvol_matches_hand_computed():
    # lookback=3 averages the 3 bars *before* the last one: (200+300+400)/3 = 300.
    # The series' own last value (999) is ignored; last_volume is passed separately.
    volumes = pd.Series([100.0, 200.0, 300.0, 400.0, 999.0])
    assert calc_rvol(volumes, last_volume=600.0, lookback=3) == pytest.approx(2.0)
    assert calc_rvol(volumes, last_volume=600.0, lookback=3, scale_factor=2.0) == pytest.approx(4.0)


def test_aggregate_4h_groups_into_4h_blocks():
    # hours 9,10,11 -> block 2; hours 12,13 -> block 3 (hour // 4)
    idx = pd.to_datetime([
        "2026-01-05 09:00", "2026-01-05 10:00", "2026-01-05 11:00",
        "2026-01-05 12:00", "2026-01-05 13:00",
    ])
    hourly = pd.DataFrame({
        "Open": [1.0, 2.0, 3.0, 7.0, 8.0],
        "High": [5.0, 6.0, 4.0, 9.0, 10.0],
        "Low": [0.5, 1.0, 2.0, 6.0, 5.0],
        "Close": [2.0, 3.0, 1.0, 8.0, 9.0],
        "Volume": [10, 20, 30, 40, 50],
    }, index=idx)
    out = aggregate_4h(hourly)
    assert list(out.index) == [0, 1]
    assert out.loc[0].tolist() == [1.0, 6.0, 0.5, 1.0, 60]
    assert out.loc[1].tolist() == [7.0, 10.0, 5.0, 9.0, 90]


def test_check_52w_position_matches_hand_computed():
    # hi 40, lo 10, range 30, last 22 -> (22-10)/30
    assert check_52w_position(pd.Series([10.0, 20.0, 30.0, 40.0, 22.0])) == pytest.approx(0.4)


# ── Happy path: new functions ─────────────────────────────────────────────


def test_rsi_matches_hand_computed():
    # period=3, closes 10,11,10,12,11,13,12 -> deltas +1,-1,+2,-1,+2,-1
    # seed (rows 1..3): avg_gain=(1+0+2)/3=1, avg_loss=(0+1+0)/3=1/3 -> 100*1/(4/3)=75
    # row4: g=(1*2+0)/3=2/3, l=(1/3*2+1)/3=5/9 -> 100*(6/9)/(11/9)=600/11
    # row5: g=(2/3*2+2)/3=10/9, l=(5/9*2+0)/3=10/27 -> 100*(30/27)/(40/27)=75
    # row6: g=(10/9*2+0)/3=20/27, l=(10/27*2+1)/3=47/81 -> 100*(60/81)/(107/81)=6000/107
    out = rsi(pd.Series([10.0, 11.0, 10.0, 12.0, 11.0, 13.0, 12.0]), period=3)
    np.testing.assert_allclose(
        out.to_numpy(),
        [np.nan, np.nan, np.nan, 75.0, 600 / 11, 75.0, 6000 / 107],
    )


def test_macd_matches_hand_computed():
    # close 1..5, fast=2 (alpha 2/3), slow=3 (alpha 1/2), signal=2 (alpha 2/3)
    # ema2:  1, 5/3, 23/9, 95/27, 365/81
    # ema3:  1, 3/2, 9/4, 25/8, 65/16
    # macd:  0, 1/6, 11/36, 85/216, 575/1296
    # sig:   0, 1/9, 13/54, 37/108, 797/1944
    # hist:  0, 1/18, 7/108, 11/216, 131/3888
    out = macd(pd.Series([1.0, 2.0, 3.0, 4.0, 5.0]), fast=2, slow=3, signal=2)
    assert list(out.columns) == ["macd", "signal", "hist"]
    np.testing.assert_allclose(out["macd"].to_numpy(), [0, 1 / 6, 11 / 36, 85 / 216, 575 / 1296])
    np.testing.assert_allclose(out["signal"].to_numpy(), [0, 1 / 9, 13 / 54, 37 / 108, 797 / 1944])
    np.testing.assert_allclose(out["hist"].to_numpy(), [0, 1 / 18, 7 / 108, 11 / 216, 131 / 3888])


def test_extension_matches_hand_computed():
    out = extension(
        close=pd.Series([10.0, 12.0, 9.0]),
        ma=pd.Series([8.0, 10.0, 10.0]),
        atr=pd.Series([1.0, 2.0, 0.5]),
    )
    np.testing.assert_allclose(out.to_numpy(), [2.0, 1.0, -2.0])


def test_relative_strength_matches_hand_computed():
    # stock +21% over 2 bars, bench +10% -> +11 percentage points
    stock = pd.Series([100.0, 110.0, 121.0])
    bench = pd.Series([200.0, 210.0, 220.0])
    assert relative_strength(stock, bench, period=2) == pytest.approx(11.0)


def test_relative_strength_aligns_on_index():
    # bench is missing 01-02; the inner join must drop stock's 999 there so the
    # window is [100, 110, 121] vs [200, 210, 220] -> +11, same as above.
    stock = pd.Series(
        [100.0, 999.0, 110.0, 121.0],
        index=pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]),
    )
    bench = pd.Series(
        [200.0, 210.0, 220.0],
        index=pd.to_datetime(["2026-01-01", "2026-01-03", "2026-01-04"]),
    )
    assert relative_strength(stock, bench, period=2) == pytest.approx(11.0)


def test_avg_dollar_volume_matches_hand_computed():
    # lookback=3 -> last three: 2*10 + 3*20 + 4*30 = 200, /3. First row excluded.
    close = pd.Series([1.0, 2.0, 3.0, 4.0])
    volume = pd.Series([100.0, 10.0, 20.0, 30.0])
    assert avg_dollar_volume(close, volume, lookback=3) == pytest.approx(200 / 3)


def test_gap_matches_hand_computed():
    # (open - prev_close)/prev_close*100: NaN, (12-11)/11, (9-10)/10, (11-10)/10
    out = gap(
        open_=pd.Series([10.0, 12.0, 9.0, 11.0]),
        close=pd.Series([11.0, 10.0, 10.0, 12.0]),
    )
    np.testing.assert_allclose(out.to_numpy(), [np.nan, 100 / 11, -10.0, 10.0])


def test_package_exports_all_public_names():
    expected = [
        "IndicatorsResponse", "SwingLow", "Zone", "ZoneHistory", "aggregate_4h",
        "avg_dollar_volume", "calc_atr",
        "calc_atrp", "calc_rvol", "check_52w_position", "earnings_reactions", "ema",
        "extension",
        "fractal_swings", "gap", "last_swing_low", "macd", "merge_levels", "relative_strength", "rsi",
        "score_zones", "sector_etf", "support_resistance", "swing_snapshot",
        "volume_nodes", "zone_history", "zone_to_dict",
    ]
    assert sorted(indicators.__all__) == expected
    for name in expected:
        assert callable(getattr(indicators, name)), name


# ── Failure branches: empty input ─────────────────────────────────────────


@pytest.mark.parametrize(
    "fn",
    [
        pytest.param(lambda e: ema(e, 3), id="ema"),
        pytest.param(lambda e: calc_atr(e, e, e, period=3), id="calc_atr"),
        pytest.param(lambda e: rsi(e, period=3), id="rsi"),
        pytest.param(lambda e: macd(e), id="macd"),
        pytest.param(lambda e: extension(e, e, e), id="extension"),
        pytest.param(lambda e: gap(e, e), id="gap"),
    ],
)
def test_series_functions_empty_input_return_empty(fn):
    out = fn(EMPTY)
    assert len(out) == 0


def test_calc_atrp_empty_returns_nan():
    assert np.isnan(calc_atrp(EMPTY, EMPTY, EMPTY, period=3))


def test_check_52w_position_empty_returns_nan():
    assert np.isnan(check_52w_position(EMPTY))


def test_calc_rvol_empty_returns_zero():
    assert calc_rvol(EMPTY, last_volume=100.0, lookback=3) == 0.0


def test_relative_strength_empty_returns_nan():
    assert np.isnan(relative_strength(EMPTY, EMPTY, period=2))


def test_avg_dollar_volume_empty_returns_nan():
    assert np.isnan(avg_dollar_volume(EMPTY, EMPTY, lookback=3))


def test_aggregate_4h_empty_returns_empty():
    hourly = pd.DataFrame(
        columns=["Open", "High", "Low", "Close", "Volume"],
        index=pd.DatetimeIndex([]),
    )
    assert len(aggregate_4h(hourly)) == 0


# ── Failure branches: shorter than period / lookback ──────────────────────


def test_calc_atr_shorter_than_period_all_nan():
    out = calc_atr(HIGH.iloc[:2], LOW.iloc[:2], CLOSE.iloc[:2], period=3)
    assert len(out) == 2 and _all_nan(out)


def test_calc_atrp_shorter_than_period_returns_nan():
    assert np.isnan(calc_atrp(HIGH.iloc[:2], LOW.iloc[:2], CLOSE.iloc[:2], period=3))


def test_rsi_shorter_than_period_all_nan():
    # exactly `period` rows is still too short: RSI needs period deltas = period+1 closes
    out = rsi(pd.Series([10.0, 11.0, 12.0]), period=3)
    assert len(out) == 3 and _all_nan(out)


def test_calc_rvol_shorter_than_lookback_returns_zero():
    # lookback=3 needs 4 bars (3 prior + the current one)
    assert calc_rvol(pd.Series([100.0, 200.0, 300.0]), last_volume=600.0, lookback=3) == 0.0


def test_avg_dollar_volume_shorter_than_lookback_returns_nan():
    assert np.isnan(avg_dollar_volume(pd.Series([1.0, 2.0]), pd.Series([10.0, 10.0]), lookback=3))


def test_relative_strength_fewer_than_period_plus_one_returns_nan():
    assert np.isnan(relative_strength(pd.Series([100.0, 110.0]), pd.Series([200.0, 210.0]), period=2))


# ── Failure branches: all-NaN columns ─────────────────────────────────────


@pytest.mark.parametrize(
    "fn",
    [
        pytest.param(lambda s: ema(s, 3), id="ema"),
        pytest.param(lambda s: calc_atr(s, s, s, period=3), id="calc_atr"),
        pytest.param(lambda s: calc_atrp(s, s, s, period=3), id="calc_atrp"),
        pytest.param(lambda s: rsi(s, period=3), id="rsi"),
        pytest.param(lambda s: macd(s, fast=2, slow=3, signal=2), id="macd"),
        pytest.param(lambda s: extension(s, s, s), id="extension"),
        pytest.param(lambda s: gap(s, s), id="gap"),
        pytest.param(lambda s: check_52w_position(s), id="check_52w_position"),
    ],
)
def test_all_nan_price_column_propagates_nan(fn):
    assert _all_nan(fn(NAN5))


def test_all_nan_volume_column_returns_nan():
    assert np.isnan(calc_rvol(NAN5, last_volume=100.0, lookback=3))
    assert np.isnan(avg_dollar_volume(pd.Series([1.0, 2.0, 3.0, 4.0, 5.0]), NAN5, lookback=3))


# ── Failure branches: divide guards and alignment ─────────────────────────


def test_extension_zero_atr_returns_nan():
    out = extension(
        close=pd.Series([10.0, 12.0]),
        ma=pd.Series([8.0, 10.0]),
        atr=pd.Series([1.0, 0.0]),
    )
    assert out.iloc[0] == pytest.approx(2.0)
    assert np.isnan(out.iloc[1]) and not np.isinf(out.iloc[1])


def test_extension_nan_atr_returns_nan():
    out = extension(
        close=pd.Series([10.0, 12.0]),
        ma=pd.Series([8.0, 10.0]),
        atr=pd.Series([1.0, np.nan]),
    )
    assert out.iloc[0] == pytest.approx(2.0)
    assert np.isnan(out.iloc[1])


def test_relative_strength_no_index_overlap_returns_nan():
    stock = pd.Series([100.0, 110.0, 121.0], index=pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03"]))
    bench = pd.Series([200.0, 210.0, 220.0], index=pd.to_datetime(["2026-02-01", "2026-02-02", "2026-02-03"]))
    assert np.isnan(relative_strength(stock, bench, period=2))


def test_gap_zero_prev_close_returns_nan():
    # prev close of row 1 is 0 -> NaN, never inf; row 2 is a normal gap
    out = gap(
        open_=pd.Series([10.0, 12.0, 9.0]),
        close=pd.Series([0.0, 10.0, 10.0]),
    )
    assert np.isnan(out.iloc[1]) and not np.isinf(out.iloc[1])
    assert out.iloc[2] == pytest.approx(-10.0)


# ── Part 1.7: sector ETF map ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "sector, etf",
    [
        ("Technology", "XLK"),
        ("Healthcare", "XLV"),
        ("Financial Services", "XLF"),
        ("Consumer Cyclical", "XLY"),
        ("Consumer Defensive", "XLP"),
        ("Energy", "XLE"),
        ("Industrials", "XLI"),
        ("Utilities", "XLU"),
        ("Real Estate", "XLRE"),
        ("Basic Materials", "XLB"),
        ("Communication Services", "XLC"),
        ("Unknown Sector", None),
    ],
)
def test_sector_etf_map(sector, etf):
    assert sector_etf(sector) == etf


def test_sector_etf_normalizes_case_and_whitespace():
    assert sector_etf("  technology ") == "XLK"
    assert sector_etf("FINANCIAL SERVICES") == "XLF"
    assert sector_etf(None) is None
    assert sector_etf("") is None
    assert sector_etf("Other") is None  # the scanner's placeholder


# ── Part 1.7: swing_snapshot (the endpoint's math, minus I/O) ─────────────
#
# Only the arithmetic the snapshot adds on top of the 1.5/1.6 functions is
# hand-computed here: the 52-week window, the gap-history slice, the RS
# wiring and the null / 0.0 conventions. Fixed-period fields are checked
# for equality with the package function they wrap.


def _frame(close, open_=None, high=None, low=None, volume=None) -> pd.DataFrame:
    close = pd.Series(close, dtype=float)
    n = len(close)
    return pd.DataFrame(
        {
            "Open": pd.Series(open_, dtype=float) if open_ is not None else close,
            "High": pd.Series(high, dtype=float) if high is not None else close + 1,
            "Low": pd.Series(low, dtype=float) if low is not None else close - 1,
            "Close": close,
            "Volume": pd.Series(volume, dtype=float) if volume is not None else pd.Series([1000.0] * n),
        }
    ).set_index(pd.date_range("2026-01-01", periods=n, freq="D"))


def test_swing_snapshot_pos52w_uses_window():
    # closes 10,20,30,40,22. Full series: hi 40, lo 10 -> (22-10)/30 = 0.4.
    # window 3 -> [30, 40, 22]: hi 40, lo 22 -> (22-22)/18 = 0.
    df = _frame([10.0, 20.0, 30.0, 40.0, 22.0])
    assert swing_snapshot(df, window_52w=5)["pos_52w"] == pytest.approx(0.4)
    assert swing_snapshot(df, window_52w=3)["pos_52w"] == pytest.approx(0.0)


def test_swing_snapshot_gap_history_is_last_n_gaps():
    # gaps: NaN, (12-11)/11*100, (9-10)/10*100, (11-10)/10*100
    df = _frame(close=[11.0, 10.0, 10.0, 12.0], open_=[10.0, 12.0, 9.0, 11.0])
    snap = swing_snapshot(df, gap_history=2)
    assert snap["gap_pct"] == pytest.approx(10.0)
    assert snap["gaps20"] == [pytest.approx(-10.0), pytest.approx(10.0)]
    full = swing_snapshot(df, gap_history=4)["gaps20"]
    assert full[0] is None  # first bar has no previous close -> null, not NaN
    assert full[1:] == [pytest.approx(100 / 11), pytest.approx(-10.0), pytest.approx(10.0)]


def test_swing_snapshot_relative_strength_hand_computed():
    # 5-bar window: stock 100 -> 121 (+21%), SPY 200 -> 220 (+10%) -> +11.0
    # sector 50 -> 60 (+20%) -> +1.0. 20-bar RS needs 21 aligned rows -> None.
    df = _frame([100.0, 105.0, 103.0, 108.0, 110.0, 121.0])
    spy = pd.Series([200.0, 205.0, 210.0, 215.0, 218.0, 220.0], index=df.index)
    sector = pd.Series([50.0, 52.0, 55.0, 54.0, 58.0, 60.0], index=df.index)
    snap = swing_snapshot(df, spy, sector)
    assert snap["rs_spy_5"] == pytest.approx(11.0)
    assert snap["rs_sector_5"] == pytest.approx(1.0)
    assert snap["rs_spy_20"] is None and snap["rs_sector_20"] is None


def test_swing_snapshot_missing_benchmark_nulls_rs():
    df = _frame([100.0, 105.0, 103.0, 108.0, 110.0, 121.0])
    snap = swing_snapshot(df)
    assert snap["rs_spy_5"] is None and snap["rs_sector_5"] is None
    assert snap["ema20"] is not None


def test_swing_snapshot_last_values_match_package_functions():
    n = 30
    close = [100 + i + (i % 3) for i in range(n)]
    volume = [1000 + 10 * i for i in range(n)]
    df = _frame(close, volume=volume)
    c, h, lo, v = df["Close"], df["High"], df["Low"], df["Volume"]
    snap = swing_snapshot(df)

    ema20, ema50 = ema(c, 20), ema(c, 50)
    atr14 = calc_atr(h, lo, c, 14)
    macd_df = macd(c)
    assert snap["bars"] == n and snap["close"] == c.iloc[-1]
    assert snap["ema20"] == pytest.approx(ema20.iloc[-1])
    assert snap["ema50"] == pytest.approx(ema50.iloc[-1])
    assert snap["ema200"] == pytest.approx(ema(c, 200).iloc[-1])
    assert snap["atr14"] == pytest.approx(atr14.iloc[-1])
    assert snap["rsi14"] == pytest.approx(rsi(c, 14).iloc[-1])
    assert snap["macd"] == pytest.approx(macd_df["macd"].iloc[-1])
    assert snap["macd_signal"] == pytest.approx(macd_df["signal"].iloc[-1])
    assert snap["macd_hist"] == pytest.approx(macd_df["hist"].iloc[-1])
    assert snap["ext20"] == pytest.approx(extension(c, ema20, atr14).iloc[-1])
    assert snap["ext50"] == pytest.approx(extension(c, ema50, atr14).iloc[-1])
    assert snap["avg_dollar_volume_20"] == pytest.approx(avg_dollar_volume(c, v, 20))
    assert snap["rvol"] == pytest.approx(calc_rvol(v, float(v.iloc[-1]), lookback=20))
    assert snap["rvol"] > 0


def test_swing_snapshot_zones_use_window():
    # 8 leading bars far below, then the 17-bar test_levels series. With
    # window 17 the zones must equal support_resistance() on those 17 bars
    # alone (default 1.6 parameters); the leading bars change the volume
    # bins and must be excluded.
    lead = 8
    high = [50.0 + i for i in range(lead)] + [103, 102, 101.5, 103, 105, 110, 106, 103, 102, 103, 104, 106, 108, 107, 103, 104, 106]
    low = [48.0 + i for i in range(lead)] + [101, 100.5, 100, 100.8, 102, 106, 101, 99, 95, 98, 100, 102, 104, 100.5, 99.6, 101, 103]
    close = [49.0 + i for i in range(lead)] + [102, 101, 100.5, 102, 104, 108, 102, 100, 96, 101, 103, 104, 106, 107, 100, 103, 105]
    volume = [1.0] * lead + [1] * 13 + [100] + [1] * 3
    df = _frame(close, high=high, low=low, volume=volume)

    tail = df.tail(17)
    expected = support_resistance(tail["High"], tail["Low"], tail["Close"], tail["Volume"])
    snap = swing_snapshot(df, window_52w=17)
    for side in ("support", "resistance"):
        assert snap["zones"][side] == [zone_to_dict(z) for z in expected[side]]
    assert snap["zones"]["support"]  # the known pivots produce zones

    full = support_resistance(df["High"], df["Low"], df["Close"], df["Volume"])
    assert [z.price for z in full["support"]] != [z.price for z in expected["support"]]


def test_zone_to_dict_shape():
    z = Zone(low=1.0, high=2.0, price=1.5, score=25, methods=("swing_low",), tests=1, recent=False, volume_node=True)
    assert zone_to_dict(z) == {
        "low": 1.0, "high": 2.0, "price": 1.5, "score": 25, "methods": ["swing_low"],
        "tests": 1, "recent": False, "volume_node": True,
    }


def test_swing_snapshot_short_history_nulls():
    snap = swing_snapshot(_frame([10.0, 11.0, 12.0]))
    assert snap["bars"] == 3
    for key in ("atr14", "rsi14", "ext20", "ext50", "avg_dollar_volume_20"):
        assert snap[key] is None, key
    assert snap["rvol"] == 0.0  # 1.5 convention: 0.0 on short input, never null
    assert snap["ema20"] is not None and snap["macd"] is not None
    assert snap["pos_52w"] == pytest.approx(1.0)
    assert len(snap["gaps20"]) == 3 and snap["gaps20"][0] is None


def test_swing_snapshot_empty_returns_nulls():
    for df in (None, _frame([])):
        snap = swing_snapshot(df)
        assert snap["bars"] == 0 and snap["close"] is None
        assert snap["rvol"] == 0.0
        assert snap["gaps20"] == []
        assert snap["zones"] == {"support": [], "resistance": []}
        assert all(snap[k] is None for k in ("ema20", "atr14", "rsi14", "macd", "pos_52w", "rs_spy_5"))
