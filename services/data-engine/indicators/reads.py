"""
TradingFirm — the four read blocks (Part 4.8b-de, spec docs/specs/4.8b.md
decisions 2-4).

Measurements from stored daily bars, computed in code so the verdict model
reads them instead of estimating them. No thresholds and no flags live
here: ai-agent's reads.py turns these numbers into flags at its own
starting lines and versions them (READS_VERSION). Every number carries its
unit in its key: `Rvol` a multiple of the 20-bar average volume, `Days` a
count of trading days, `Atr` a multiple of ATR14, `Frac` 0-1; bare
`low` / `high` / `swingLows` are prices; `closesBelowEma20` and
`ema20Crosses40` are counts.

Pure: no I/O. NaN never raises: a window with too few bars nulls exactly the
fields that need it.
"""

import math
from typing import Any, Optional

import pandas as pd

from indicators.levels import fractal_swings
from indicators.moving_averages import ema
from indicators.volatility import calc_atr

RVOL_LOOKBACK = 20          # calc_rvol's convention: the 20 bars before the day
UPDOWN_DAYS = 5
UPDOWN_WINDOW = 60
BREAKOUT_BARS = 5
SLOPE_BARS = 10
MOVE_BARS = 30
BELOW_EMA_BARS = 20
RANGE_BARS = 60
CROSS_BARS = 40


def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    value = float(value)
    return None if math.isnan(value) or math.isinf(value) else value


def rvol_series(volume: pd.Series) -> pd.Series:
    """rvol(k) = volume[k] / mean(volume[k-20 .. k-1]): `calc_rvol` applied
    to every bar. NaN under 21 bars or on a zero mean."""
    avg = volume.shift(1).rolling(RVOL_LOOKBACK).mean()
    return volume / avg.where(avg > 0)


def volume_read(close: pd.Series, volume: pd.Series, zones: list, dates) -> dict:
    """upDays5Rvol / downDays5Rvol, the newest breakout of the last 5 bars,
    and the pullback run ending at the last bar (spec 4.8b decision 2).

    `zones` are levels.Zone objects (either side). A breakout counts only a
    zone that has held from below at least as often as it broke
    (`held_below >= broke_below`, approval answer 1)."""
    out = {"up_days5_rvol": None, "down_days5_rvol": None, "breakout": None,
           "pullback_days": 0, "pullback_rvol": None}
    if len(close) < 2:
        return out
    rv = rvol_series(volume)
    change = close.diff()
    window = slice(-UPDOWN_WINDOW, None)
    up = rv[window][change[window] > 0].dropna().iloc[-UPDOWN_DAYS:]
    down = rv[window][change[window] < 0].dropna().iloc[-UPDOWN_DAYS:]
    if len(up) == UPDOWN_DAYS:
        out["up_days5_rvol"] = _num(up.mean())
    if len(down) == UPDOWN_DAYS:
        out["down_days5_rvol"] = _num(down.mean())

    eligible = [z for z in zones if z.held_below >= z.broke_below]
    n = len(close)
    for k in range(n - 1, max(n - 1 - BREAKOUT_BARS, 0), -1):
        prev, cur = close.iloc[k - 1], close.iloc[k]
        if math.isnan(prev) or math.isnan(cur):
            continue
        cleared = [z for z in eligible if prev <= z.high < cur]
        if cleared:
            zone = max(cleared, key=lambda z: z.high)      # the highest band the bar cleared
            out["breakout"] = {"date": _date(dates[k]), "low": zone.low, "high": zone.high,
                               "bar_rvol": _num(rv.iloc[k])}
            break

    run = 0
    while run < n - 1 and change.iloc[n - 1 - run] < 0:
        run += 1
    out["pullback_days"] = run
    if run:
        out["pullback_rvol"] = _num(rv.iloc[n - run:].mean())
    return out


def trend_read(high: pd.Series, low: pd.Series, close: pd.Series) -> dict:
    """The price / EMA stack, the EMA20 slope over 10 bars, the last two
    fractal swing lows (spec 4.8b decision 3). The RS check reads the
    snapshot's own `rs_spy_20`; nothing here reads the 5-day RS."""
    out = {"stack_up": None, "ema20_rising10": None, "ema20_slope10_atr": None,
           "swing_lows": [], "higher_lows": False}
    if len(close) == 0:
        return out
    e20, e50 = ema(close, 20), ema(close, 50)
    c, a, b = _num(close.iloc[-1]), _num(e20.iloc[-1]), _num(e50.iloc[-1])
    if None not in (c, a, b):
        out["stack_up"] = c > a > b
    if len(close) > SLOPE_BARS:
        now, then = _num(e20.iloc[-1]), _num(e20.iloc[-1 - SLOPE_BARS])
        atr = _num(calc_atr(high, low, close, 14).iloc[-1])
        if None not in (now, then):
            out["ema20_rising10"] = now > then
            if atr:
                out["ema20_slope10_atr"] = (now - then) / atr
    _highs, lows = fractal_swings(high, low)
    prices = [float(low.iloc[i]) for i in lows[-2:]]
    out["swing_lows"] = prices
    out["higher_lows"] = len(prices) == 2 and prices[1] > prices[0]
    return out


def momentum_read(high: pd.Series, low: pd.Series, close: pd.Series) -> dict:
    """move30Atr, range30Atr, closesBelowEma20, lowerHighs (spec 4.8b
    decision 3; the 09-24 rerun's definitions, unchanged)."""
    out = {"move30_atr": None, "range30_atr": None, "closes_below_ema20": None,
           "lower_highs": False}
    if len(close) == 0:
        return out
    atr = _num(calc_atr(high, low, close, 14).iloc[-1])
    if atr and len(close) > MOVE_BARS:
        move = _num(close.iloc[-1] - close.iloc[-1 - MOVE_BARS])
        span = _num(high.iloc[-MOVE_BARS:].max() - low.iloc[-MOVE_BARS:].min())
        out["move30_atr"] = None if move is None else move / atr
        out["range30_atr"] = None if span is None else span / atr
    if len(close) >= BELOW_EMA_BARS:
        e20 = ema(close, 20)
        out["closes_below_ema20"] = int(
            (close.iloc[-BELOW_EMA_BARS:].to_numpy() < e20.iloc[-BELOW_EMA_BARS:].to_numpy()).sum())
    highs, _lows = fractal_swings(high, low)
    last3 = [float(high.iloc[i]) for i in highs[-3:]]
    out["lower_highs"] = len(last3) == 3 and last3[0] > last3[1] > last3[2]
    return out


def range_read(high: pd.Series, low: pd.Series, close: pd.Series) -> Optional[dict]:
    """The 60 bars before the last one, the last close's place in them, and
    EMA20 crosses over the last 40 transitions (spec 4.8b decision 4). None
    under 61 bars."""
    if len(close) < RANGE_BARS + 1:
        return None
    lo = _num(low.iloc[-RANGE_BARS - 1:-1].min())
    hi = _num(high.iloc[-RANGE_BARS - 1:-1].max())
    last = _num(close.iloc[-1])
    if None in (lo, hi, last):
        return None
    diff = (close - ema(close, 20)).iloc[-CROSS_BARS - 1:].to_numpy()
    crosses = sum(1 for a, b in zip(diff[:-1], diff[1:])
                  if a != 0 and b != 0 and not math.isnan(a) and not math.isnan(b) and (a > 0) != (b > 0))
    return {
        "low": lo,
        "high": hi,
        "pos_frac": (last - lo) / (hi - lo) if hi > lo else None,
        "ema20_crosses40": crosses,
        "closed_outside": last > hi or last < lo,
    }


def _date(value) -> str:
    return value.date().isoformat() if hasattr(value, "date") else str(value)
