"""
TradingFirm — the four read blocks (Part 4.8b-de, spec docs/specs/4.8b.md
decisions 2-4). Pure functions on synthetic series with hand-derived values,
plus the eleven stored verdicts' bars as fixtures (tests/fixtures/reads/,
the last 300 daily bars <= asOf from the 2026-09-23 read-only pull).
"""

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from indicators.levels import Zone
from indicators.reads import (
    momentum_read,
    range_read,
    rvol_series,
    trend_read,
    volume_read,
)
from indicators.snapshot import swing_snapshot

FIXTURES = Path(__file__).parent / "fixtures" / "reads"


def series(values):
    return pd.Series([float(v) for v in values],
                     index=pd.bdate_range("2026-01-05", periods=len(values)))


def zigzag(n, start=50.0):
    """Closes alternating up 1 / down 0.5 (bar 0 flat), so every odd bar is
    an up close and every even bar after 0 a down close."""
    closes, c = [start], start
    for k in range(1, n):
        c += 1.0 if k % 2 else -0.5
        closes.append(c)
    return closes


def zone(low, high, held_below=0, broke_below=0):
    return Zone(low=low, high=high, price=(low + high) / 2, score=0, methods=(), tests=1,
                recent=False, volume_node=False, held_below=held_below, broke_below=broke_below)


# ── volumeRead ───────────────────────────────────────────────────

def test_rvol_series_is_calc_rvol_per_bar():
    rv = rvol_series(series([100] * 20 + [150]))
    assert rv.iloc[-1] == 1.5
    assert math.isnan(rv.iloc[-2]), "under 21 bars there is no 20-bar mean"


def test_volume_read_up_down_days():
    """70 zigzag bars at volume 100 (every rvol 1.0), except the last bar, an
    up close, at 600: its prior 20 are all 100, so its rvol is 6.0. The last
    5 up closes -> (6 + 1 + 1 + 1 + 1) / 5 = 2.0; the last 5 down closes 1.0."""
    closes = zigzag(70)
    vol = [100] * 69 + [600]
    out = volume_read(series(closes), series(vol), [], series(closes).index)
    assert out["up_days5_rvol"] == pytest.approx(2.0)
    assert out["down_days5_rvol"] == pytest.approx(1.0)
    # Fewer than 5 down closes inside the last 60 bars: that mean is null.
    rising = list(np.arange(50.0, 120.0))
    out = volume_read(series(rising), series([100] * 70), [], series(rising).index)
    assert out["up_days5_rvol"] == pytest.approx(1.0) and out["down_days5_rvol"] is None


def test_volume_read_breakout_newest_in_last_five():
    """Closes ... 49, 50, 51, 52, 53, 54. Zone A 50.4-50.5 (held 3 / broke 1
    from below) is cleared by the 51 bar; zone B 52.2-52.6 (3 / 2) by the 53
    bar, which is newer, so B wins. Zone C 53.5-53.8 (2 / 2, a tie) is
    cleared by the last bar but a tie is not eligible: held > broke only."""
    closes = [49.0] * 30 + [50.0, 51.0, 52.0, 53.0, 54.0]
    vol = [100] * 33 + [300, 100]
    idx = series(closes).index
    out = volume_read(series(closes), series(vol), [
        zone(50.4, 50.5, 3, 1), zone(52.2, 52.6, 3, 2), zone(53.5, 53.8, 2, 2)], idx)
    assert out["breakout"] == {"date": idx[-2].date().isoformat(), "low": 52.2, "high": 52.6,
                               "bar_rvol": pytest.approx(3.0)}


def test_volume_read_no_breakout():
    # The 50 close at bar 29 clears 49.2-49.5: six bars back, outside the
    # last five (bars 30-34). The last bar clears 50.42-50.45, but that zone
    # has broken more than it held.
    closes = [49.0] * 29 + [50.0, 50.1, 50.2, 50.3, 50.4, 50.5]
    idx = series(closes).index
    out = volume_read(series(closes), series([100] * 35), [
        zone(50.42, 50.45, 0, 1), zone(49.2, 49.5, 5, 0)], idx)
    assert out["breakout"] is None


def test_volume_read_pullback_run():
    """Three down closes end the series at volume 100 (rvol 1.0 each): 3
    days, 1.0. One down close at 50 after 100s: 1 day, rvol 0.5."""
    closes = list(np.arange(50.0, 80.0)) + [78.5, 77.5, 76.5]
    out = volume_read(series(closes), series([100] * 33), [], series(closes).index)
    assert (out["pullback_days"], out["pullback_rvol"]) == (3, pytest.approx(1.0))
    closes = list(np.arange(50.0, 80.0)) + [78.5]
    out = volume_read(series(closes), series([100] * 30 + [50]), [], series(closes).index)
    assert (out["pullback_days"], out["pullback_rvol"]) == (1, pytest.approx(0.5))
    out = volume_read(series(np.arange(50.0, 80.0)), series([100] * 30), [], series(range(30)).index)
    assert (out["pullback_days"], out["pullback_rvol"]) == (0, None)


def test_volume_read_short_history_nulls():
    closes = zigzag(15)
    out = volume_read(series(closes), series([100] * 15), [zone(10, 11)], series(closes).index)
    assert out["up_days5_rvol"] is None and out["down_days5_rvol"] is None
    assert volume_read(series([50.0]), series([100]), [], series([50.0]).index) == {
        "up_days5_rvol": None, "down_days5_rvol": None, "breakout": None,
        "pullback_days": 0, "pullback_rvol": None}


# ── trendRead and momentumRead ───────────────────────────────────

def _bars_from_closes(closes, spread=0.5):
    c = series(closes)
    return c + spread, c - spread, c


def test_trend_read_checks():
    """A straight rise of 1 a bar with a 0.5 half-range: close > EMA20 > EMA50,
    EMA20 up, its 10-bar slope 10 over an ATR of 1.5 (true range = the 1.5
    gap from the previous close to the high) -> 6.667 ATR once EMA20 has
    converged. No fractal lows on a straight line."""
    high, low, close = _bars_from_closes(np.arange(10.0, 310.0))
    out = trend_read(high, low, close)
    assert out["stack_up"] is True and out["ema20_rising10"] is True
    assert out["ema20_slope10_atr"] == pytest.approx(10 / 1.5, rel=1e-6)
    assert out["swing_lows"] == [] and out["higher_lows"] is False
    # Two dips: lows 40 (bar 5) then 42 (bar 12) -> higher lows.
    lows = [50, 49, 48, 47, 46, 40, 46, 47, 48, 49, 48, 47, 42, 47, 48, 49]
    low = series(lows)
    out = trend_read(low + 1, low, low + 0.5)
    assert out["swing_lows"] == [40.0, 42.0] and out["higher_lows"] is True
    out = trend_read(series([l + 1 for l in lows[::-1]]), series(lows[::-1]),
                     series([l + 0.5 for l in lows[::-1]]))
    assert out["swing_lows"] == [42.0, 40.0] and out["higher_lows"] is False


def test_trend_read_short_history():
    high, low, close = _bars_from_closes([10.0, 11.0, 12.0])
    out = trend_read(high, low, close)
    assert out["ema20_rising10"] is None and out["ema20_slope10_atr"] is None
    assert out["stack_up"] is True
    empty = pd.Series([], dtype=float)
    assert trend_read(empty, empty, empty)["stack_up"] is None


def test_momentum_read_short_and_empty():
    high, low, close = _bars_from_closes(np.arange(10.0, 30.0))
    out = momentum_read(high, low, close)
    assert out["move30_atr"] is None and out["range30_atr"] is None
    assert out["closes_below_ema20"] == 0 and out["lower_highs"] is False
    empty = pd.Series([], dtype=float)
    assert momentum_read(empty, empty, empty)["closes_below_ema20"] is None


# ── rangeRead ────────────────────────────────────────────────────

def test_range_read_position_and_crosses():
    """61 bars: the 60 before the last span lows 90 .. highs 110 (closes
    oscillate 95 / 105, so close - EMA20 changes sign every bar once the
    EMA sits near 100); the last close 100 sits at 0.5."""
    closes = [95.0 if k % 2 else 105.0 for k in range(60)] + [100.0]
    close = series(closes)
    high, low = close + 5, close - 5
    out = range_read(high, low, close)
    assert (out["low"], out["high"]) == (90.0, 110.0)
    assert out["pos_frac"] == pytest.approx(0.5) and out["closed_outside"] is False
    diff = (close - close.ewm(span=20, adjust=False).mean()).iloc[-41:].to_numpy()
    expected = sum(1 for a, b in zip(diff[:-1], diff[1:]) if (a > 0) != (b > 0))
    assert out["ema20_crosses40"] == expected and expected >= 39


def test_range_read_last_bar_outside():
    closes = [100.0] * 60 + [120.0]
    close = series(closes)
    out = range_read(close + 1, close - 1, close)
    # The 60 before span 99 .. 101: (120 - 99) / (101 - 99) = 10.5.
    assert out["closed_outside"] is True and out["pos_frac"] == pytest.approx(10.5)
    assert out["ema20_crosses40"] == 0, "a flat run is never a cross"


def test_range_read_short_history():
    close = series([100.0] * 60)
    assert range_read(close + 1, close - 1, close) is None


# ── The snapshot carries them; NaN and empty inputs ─────────────

def test_reads_empty_series():
    snap = swing_snapshot(None)
    assert all(snap[k] is None for k in ("volume_read", "trend_read", "momentum_read", "range_read"))


def test_reads_nan_bars_ignored():
    """NaN never raises and nulls only what it touches: a NaN bar 20 back
    sits outside the 14-bar ATR, so move30 / range30 still compute; one
    inside the ATR window nulls both (the snapshot's atr14 rule)."""
    closes = list(np.arange(50.0, 120.0))
    closes[-20] = float("nan")
    close = series(closes)
    high, low = close + 0.5, close - 0.5
    m = momentum_read(high, low, close)
    assert m["move30_atr"] is not None and m["range30_atr"] is not None
    assert range_read(high, low, close)["closed_outside"] is True
    out = volume_read(close, series([100] * 70), [zone(117.4, 117.6, 1, 0)], close.index)
    assert out["pullback_days"] == 0 and out["up_days5_rvol"] == pytest.approx(1.0)
    closes[-3] = float("nan")
    close = series(closes)
    m = momentum_read(close + 0.5, close - 0.5, close)
    assert m["move30_atr"] is None and m["range30_atr"] is None


# ── The eleven stored verdicts (spec 4.8b decision 3) ────────────

# (verdict, ticker, asOf, move30Atr, range30Atr, closesBelowEma20, lowerHighs)
# — the 09-24 rerun's table, reproduced to the digit. Then the other reads
# the same bars give. The breakout reads zones built on the full ~500-bar
# history, which a 300-bar fixture cannot rebuild, so each fixture carries
# that history's zones per asOf (low / high / heldBelow / brokeBelow).
ELEVEN = [
    ("3c31ff2c", "AAPL", "2026-09-21", 3.42, 5.16, 4, False),
    ("becd874d", "AAPL", "2026-09-21", 3.42, 5.16, 4, False),
    ("8dee4d5b", "GOOGL", "2026-09-21", 0.11, 3.91, 12, False),
    ("6c1da5ac", "OUST", "2026-09-21", -2.72, 10.12, 19, False),
    ("20824f75", "AAL", "2026-09-21", -5.60, 7.59, 19, True),
    ("2baca035", "IAG", "2026-09-21", 2.17, 5.81, 4, True),
    ("3d4ebf19", "OPCH", "2026-09-21", 0.24, 2.81, 5, False),
    ("f70e5368", "CNK", "2026-09-21", -0.87, 4.84, 14, False),
    ("e62eff60", "MSFT", "2026-09-21", 0.25, 4.11, 1, True),
    ("96bea108", "RIOT", "2026-09-21", 2.47, 5.19, 7, False),
    ("35ddee39", "OUST", "2026-09-22", -0.93, 9.69, 18, False),
]

# ticker/asOf -> (stackUp, ema20Rising10, higherLows, up5, down5, pullbackDays,
#                 range low, range high, posFrac, ema20Crosses40)
# ticker/asOf -> the newest breakout on held > broke zones: (date, low, high,
# bar RVOL), or None. 5 of the 11 verdicts; bar RVOL < 1 only on AAL.
BREAKOUTS = {
    ("AAPL", "2026-09-21"): None,
    ("GOOGL", "2026-09-21"): ("2026-09-18", 344.46, 348.32, 1.99),
    ("OUST", "2026-09-21"): None,
    ("AAL", "2026-09-21"): ("2026-09-18", 12.79, 12.95, 0.74),
    ("IAG", "2026-09-21"): None,
    ("OPCH", "2026-09-21"): ("2026-09-16", 23.83, 24.16, 2.23),
    ("CNK", "2026-09-21"): None,
    ("MSFT", "2026-09-21"): ("2026-09-21", 489.20, 493.81, 1.33),
    ("RIOT", "2026-09-21"): ("2026-09-21", 23.49, 23.93, 1.40),
    ("OUST", "2026-09-22"): None,
}

OTHER_READS = {
    ("AAPL", "2026-09-21"): (True, True, True, 0.93, 1.28, 0, 273.51, 344.27, 0.93, 6),
    ("GOOGL", "2026-09-21"): (False, True, True, 1.33, 1.05, 0, 314.70, 384.23, 0.58, 7),
    ("OUST", "2026-09-21"): (False, False, False, 1.02, 0.82, 0, 30.82, 63.79, 0.22, 3),
    ("AAL", "2026-09-21"): (False, False, False, 1.08, 1.27, 0, 12.55, 18.79, 0.16, 3),
    ("IAG", "2026-09-21"): (True, True, False, 1.46, 1.04, 1, 13.62, 22.32, 0.75, 5),
    ("OPCH", "2026-09-21"): (True, True, False, 1.37, 1.63, 2, 20.68, 24.96, 0.76, 6),
    ("CNK", "2026-09-21"): (True, False, False, 1.15, 0.84, 0, 28.43, 38.89, 0.75, 4),
    ("MSFT", "2026-09-21"): (True, True, True, 0.95, 1.00, 0, 348.54, 517.78, 0.90, 3),
    ("RIOT", "2026-09-21"): (True, True, True, 1.15, 0.69, 0, 17.39, 29.11, 0.58, 10),
    ("OUST", "2026-09-22"): (False, False, False, 1.18, 0.82, 0, 30.82, 63.79, 0.29, 3),
}


def _fixture_zones(ticker, as_of):
    doc = json.loads((FIXTURES / f"{ticker}.json").read_text())
    return [zone(z["low"], z["high"], z["heldBelow"], z["brokeBelow"]) for z in doc["zones"][as_of]]


def _fixture_frame(ticker, as_of):
    doc = json.loads((FIXTURES / f"{ticker}.json").read_text())
    rows = [r for r in doc["bars"] if r[0] <= as_of]
    index = pd.DatetimeIndex([r[0] for r in rows])
    return pd.DataFrame({"Open": [r[1] for r in rows], "High": [r[2] for r in rows],
                         "Low": [r[3] for r in rows], "Close": [r[4] for r in rows],
                         "Volume": [r[5] for r in rows]}, index=index)


@pytest.mark.parametrize("verdict,ticker,as_of,move,span,below,lower", ELEVEN)
def test_momentum_read_reproduces_the_eleven(verdict, ticker, as_of, move, span, below, lower):
    """Also the breakout on each fixture's own zones: 5 / 11 (spec 4.8b
    decision 2 as fixed 2026-09-25)."""
    df = _fixture_frame(ticker, as_of)
    assert df.index[-1].date().isoformat() == as_of
    m = momentum_read(df["High"], df["Low"], df["Close"])
    assert (round(m["move30_atr"], 2), round(m["range30_atr"], 2)) == (move, span), verdict
    assert (m["closes_below_ema20"], m["lower_highs"]) == (below, lower), verdict

    stack, rising, hl, up5, down5, pb, lo, hi, pos, crosses = OTHER_READS[(ticker, as_of)]
    t = trend_read(df["High"], df["Low"], df["Close"])
    assert (t["stack_up"], t["ema20_rising10"], t["higher_lows"]) == (stack, rising, hl), verdict
    v = volume_read(df["Close"], df["Volume"].astype(float), _fixture_zones(ticker, as_of), df.index)
    assert (round(v["up_days5_rvol"], 2), round(v["down_days5_rvol"], 2), v["pullback_days"]) == (
        up5, down5, pb), verdict
    b = v["breakout"]
    got = None if b is None else (b["date"], round(b["low"], 2), round(b["high"], 2), round(b["bar_rvol"], 2))
    assert got == BREAKOUTS[(ticker, as_of)], verdict
    r = range_read(df["High"], df["Low"], df["Close"])
    assert (round(r["low"], 2), round(r["high"], 2), round(r["pos_frac"], 2),
            r["ema20_crosses40"], r["closed_outside"]) == (lo, hi, pos, crosses, False), verdict
