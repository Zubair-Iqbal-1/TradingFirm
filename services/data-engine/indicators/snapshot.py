"""
TradingFirm — Swing indicator snapshot (Part 1.7).

One pure function that turns a daily OHLCV frame (plus optional benchmark
close series) into the plan §3 swing set + 1.6 zones as a snake_case dict.
No I/O. The endpoint in main.py wraps this with storage, caching and the
camelCase response model; nothing here knows about Redis or Postgres.

Field → function map (docs/decisions.md, Part 1.7). "last" = `.iloc[-1]`
of the series the named function returns, over the full stored history:

    ema20 / ema50 / ema200   ema(close, n)                       last
    atr14                    calc_atr(high, low, close, 14)      last
    rsi14                    rsi(close, 14)                      last
    macd / macd_signal /     macd(close)                         last row
      macd_hist
    ext20 / ext50            extension(close, ema20|50, atr14)   last
    gap_pct                  gap(open, close)                    last
    gaps20                   gap(open, close)                    last `gap_history`
    rvol                     calc_rvol(volume, last volume, 20, scale 1.0)
    avg_dollar_volume_20     avg_dollar_volume(close, volume, 20)
    rs_spy_5 / rs_spy_20     relative_strength(close, spy, 5|20)
    rs_sector_5 / _20        relative_strength(close, sector, 5|20)
    pos_52w                  check_52w_position(close over the last `window_52w` bars)
    zones                    support_resistance(...) over the FULL stored history
                             (Part 4.8a-de; 52 weeks before it), each zone with
                             its touches / held / broke / last_touch and the
                             side split (held_below … broke_above); ATR-width
                             merge and the windowed nearest-6 selection (2026-09-24)
    last_swing_low           last_swing_low(high, low): {price, date} or None
    volume_read / trend_read / momentum_read / range_read
                             indicators.reads (Part 4.8b-de, spec 4.8b decisions
                             2-4): measurements only, no thresholds; the
                             breakout reads the zones above

Conventions:
  - Undefined is None (every NaN becomes None). No minimum bar count; a
    short history nulls exactly the fields whose function returns NaN.
  - rvol keeps the 1.5 convention and is 0.0, not None, when fewer than
    21 bars exist. Scale factor is 1.0: stored daily bars carry no
    "minutes since open", so the last bar is taken as a full day.
  - A missing benchmark (None) nulls the RS fields that need it.
  - Empty input returns the same keys with None / [] / 0 values.
"""

import math
from typing import Any

import pandas as pd

from indicators.levels import Zone, last_swing_low, support_resistance
from indicators.momentum import check_52w_position, macd, relative_strength, rsi
from indicators.moving_averages import ema
from indicators.reads import momentum_read, range_read, trend_read, volume_read
from indicators.volatility import calc_atr, extension, gap
from indicators.volume import avg_dollar_volume, calc_rvol

WINDOW_52W = 252
GAP_HISTORY = 20


def _num(value: Any) -> float | None:
    """float(value), or None when the value is NaN / None."""
    if value is None:
        return None
    value = float(value)
    return None if math.isnan(value) else value


def _last(series: pd.Series) -> float | None:
    if len(series) == 0:
        return None
    return _num(series.iloc[-1])


def zone_to_dict(zone: Zone) -> dict:
    """Plain-dict form of a Zone (methods as a list) for JSON responses."""
    return {
        "low": zone.low,
        "high": zone.high,
        "price": zone.price,
        "score": zone.score,
        "methods": list(zone.methods),
        "tests": zone.tests,
        "recent": zone.recent,
        "volume_node": zone.volume_node,
        "touches": zone.touches,
        "held": zone.held,
        "broke": zone.broke,
        "last_touch": zone.last_touch,
        "held_below": zone.held_below,
        "broke_below": zone.broke_below,
        "held_above": zone.held_above,
        "broke_above": zone.broke_above,
    }


def _empty_snapshot() -> dict:
    return {
        "bars": 0,
        "close": None,
        "ema20": None, "ema50": None, "ema200": None,
        "atr14": None,
        "rvol": 0.0,
        "rsi14": None,
        "macd": None, "macd_signal": None, "macd_hist": None,
        "pos_52w": None,
        "ext20": None, "ext50": None,
        "rs_spy_5": None, "rs_spy_20": None,
        "rs_sector_5": None, "rs_sector_20": None,
        "avg_dollar_volume_20": None,
        "gap_pct": None,
        "gaps20": [],
        "zones": {"support": [], "resistance": []},
        "last_swing_low": None,
        "volume_read": None,
        "trend_read": None,
        "momentum_read": None,
        "range_read": None,
    }


def _rs(close: pd.Series, bench: pd.Series | None, period: int) -> float | None:
    if bench is None or len(bench) == 0:
        return None
    return _num(relative_strength(close, bench, period))


def swing_snapshot(
    daily: pd.DataFrame | None,
    spy_close: pd.Series | None = None,
    sector_close: pd.Series | None = None,
    *,
    window_52w: int = WINDOW_52W,
    gap_history: int = GAP_HISTORY,
) -> dict:
    """
    Plan §3 swing set + zones for one ticker, as a snake_case dict.

    `daily` has Open/High/Low/Close/Volume columns and a DatetimeIndex,
    oldest first (the shape `db.bars_to_df()` returns). Benchmarks are
    close Series on the same kind of index; relative strength aligns the
    two on index before computing.
    """
    if daily is None or daily.empty:
        return _empty_snapshot()

    open_ = daily["Open"].astype(float)
    high = daily["High"].astype(float)
    low = daily["Low"].astype(float)
    close = daily["Close"].astype(float)
    volume = daily["Volume"].astype(float)

    ema20 = ema(close, 20)
    ema50 = ema(close, 50)
    ema200 = ema(close, 200)
    atr14 = calc_atr(high, low, close, 14)
    macd_df = macd(close)
    gaps = gap(open_, close)

    window = daily.tail(window_52w)
    # Zones and the swing low read the full stored history (4.8a-de decision
    # 1): a level from 18 months ago is still a level. pos_52w keeps its window.
    zones = support_resistance(high, low, close, volume)
    swing = last_swing_low(high, low)
    swing_out = None
    if swing is not None:
        when = daily.index[swing.index]
        swing_out = {"price": swing.price,
                     "date": when.date().isoformat() if hasattr(when, "date") else str(when)}

    return {
        "bars": int(len(daily)),
        "close": _last(close),
        "ema20": _last(ema20),
        "ema50": _last(ema50),
        "ema200": _last(ema200),
        "atr14": _last(atr14),
        "rvol": float(calc_rvol(volume, float(volume.iloc[-1]), lookback=20)),
        "rsi14": _last(rsi(close, 14)),
        "macd": _last(macd_df["macd"]),
        "macd_signal": _last(macd_df["signal"]),
        "macd_hist": _last(macd_df["hist"]),
        "pos_52w": _num(check_52w_position(window["Close"].astype(float))),
        "ext20": _last(extension(close, ema20, atr14)),
        "ext50": _last(extension(close, ema50, atr14)),
        "rs_spy_5": _rs(close, spy_close, 5),
        "rs_spy_20": _rs(close, spy_close, 20),
        "rs_sector_5": _rs(close, sector_close, 5),
        "rs_sector_20": _rs(close, sector_close, 20),
        "avg_dollar_volume_20": _num(avg_dollar_volume(close, volume, 20)),
        "gap_pct": _last(gaps),
        "gaps20": [_num(v) for v in gaps.tail(gap_history)],
        "zones": {
            "support": [zone_to_dict(z) for z in zones["support"]],
            "resistance": [zone_to_dict(z) for z in zones["resistance"]],
        },
        "last_swing_low": swing_out,
        "volume_read": volume_read(close, volume, zones["support"] + zones["resistance"], daily.index),
        "trend_read": trend_read(high, low, close),
        "momentum_read": momentum_read(high, low, close),
        "range_read": range_read(high, low, close),
    }
