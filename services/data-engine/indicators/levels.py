"""
TradingFirm — Support / resistance zones (Part 1.6).

Pure functions on daily bars. No I/O. Pipeline:

    fractal_swings  ─┐
                     ├─► merge_levels ─► score_zones ─► support_resistance
    volume_nodes    ─┘

A *level* is a plain tuple ``(price, method, bar_index)`` where ``method`` is
one of ``"swing_high"``, ``"swing_low"``, ``"volume"`` and ``bar_index`` is
the positional index of the bar for swings, ``None`` for volume nodes.

Conventions (docs/decisions.md, Part 1.6):
  - Fractals are strict: a bar tied with a neighbour is not a fractal.
    A bar with NaN high or low is never a fractal and disqualifies every
    bar within `wing` of it — checked explicitly, not left to NaN
    comparison semantics.
  - Volume nodes bin each bar's volume by its close over [min low, max high];
    bars with NaN close or NaN volume are skipped.
  - Merge anchor is the running mean of the group, inclusive of merge_pct.
  - "methods" are swing_high / swing_low only; a volume node is reported
    separately (Zone.volume_node) and scores +25 but is not a method.
  - "tests" = number of swing members in a zone; "recent" = newest swing
    member within the last `recent_bars` bars of the full series (NaN tail
    included).
  - Support / resistance split on zone price vs last valid close;
    zone price == close is resistance.
  - Zone history (Part 4.8a-de, spec decision 2): `touches`, `held`,
    `broke`, `last_touch` per returned zone, over the full series, counted
    across both approach sides (a rejection from above counts as held
    like one from below; docs/decisions.md 2026-09-23).
"""

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

Level = tuple[float, str, int | None]

SWING_HIGH = "swing_high"
SWING_LOW = "swing_low"
VOLUME = "volume"
HOLD_BARS = 3


@dataclass(frozen=True)
class Zone:
    low: float
    high: float
    price: float
    score: int
    methods: tuple[str, ...]
    tests: int
    recent: bool
    volume_node: bool
    # Part 4.8a-de: how the level behaved over the full stored history.
    # Defaults keep the 1.6 constructor and a cached pre-part body valid.
    touches: int = 0
    held: int = 0
    broke: int = 0
    last_touch: Optional[str] = None


@dataclass(frozen=True)
class ZoneHistory:
    touches: int
    held: int
    broke: int
    last_index: Optional[int]      # positional index of the newest episode's last bar


@dataclass(frozen=True)
class SwingLow:
    price: float
    index: int                     # positional index of the pivot bar


# ── Stage 1a: fractal swings ──────────────────────────────────────────────


def fractal_swings(
    high: pd.Series,
    low: pd.Series,
    wing: int = 2,
) -> tuple[list[int], list[int]]:
    """
    Fractal swing highs and lows with `wing` bars on each side.

    Bar i is a swing high if high[i] is strictly greater than every high in
    the `wing` bars before and after it; swing low likewise on lows. Bars
    without a full window (the first and last `wing` bars) are never
    fractals.

    Any bar whose high or low is NaN is a "bad" bar: it is never a fractal
    and no bar whose window contains it can be one either.

    Returns:
        (swing_high_indices, swing_low_indices), positional, ascending.
        Both empty when fewer than 2 * wing + 1 bars are given.
    """
    if len(high) != len(low):
        raise ValueError("high and low must have the same length")

    h = high.to_numpy(dtype=float)
    lo = low.to_numpy(dtype=float)
    n = len(h)
    bad = np.isnan(h) | np.isnan(lo)

    highs: list[int] = []
    lows: list[int] = []
    for i in range(wing, n - wing):
        if bad[i - wing : i + wing + 1].any():
            continue
        left = slice(i - wing, i)
        right = slice(i + 1, i + wing + 1)
        if h[i] > h[left].max() and h[i] > h[right].max():
            highs.append(i)
        if lo[i] < lo[left].min() and lo[i] < lo[right].min():
            lows.append(i)
    return highs, lows


def last_swing_low(high: pd.Series, low: pd.Series, wing: int = 2) -> Optional[SwingLow]:
    """
    The newest fractal swing low (Part 4.8a-de, spec decision 3): the last
    index `fractal_swings` returns for lows, with its price. Confirmed by
    construction (a fractal needs `wing` bars after it). None with fewer
    than 2 * wing + 1 bars or no pivot.
    """
    _, lows = fractal_swings(high, low, wing=wing)
    if not lows:
        return None
    i = lows[-1]
    return SwingLow(float(low.to_numpy(dtype=float)[i]), i)


# ── Stage 1c: zone history ────────────────────────────────────────────────


def zone_history(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    zone_low: float,
    zone_high: float,
    hold_bars: int = HOLD_BARS,
) -> ZoneHistory:
    """
    How the band [zone_low, zone_high] behaved over the series (Part
    4.8a-de, spec decision 2, as approved):

      reach    a bar's range overlaps the band (high >= zone_low and
               low <= zone_high)
      jump     a bar that does not reach the band, whose close is on the
               opposite side of it from the last valid close outside the
               band, and that does not directly follow an episode (that
               bar belongs to the episode's outcome window instead)
      episode  a maximal run of consecutive reaching bars, or one jump bar;
               one episode is one touch
      approach the side (below / above the band) of the last valid close
               outside the band before the episode; an episode with none
               (the series starts inside the band) is skipped
      outcome  judged on the closes from the episode's first bar e0 through
               the first valid close after its last bar:
                 broke      any close in that window on the far side
                 held       no far close, the window complete, and an
                            approach-side close within e0 .. e0+hold_bars
                 undecided  otherwise (a slow rejection, or a window the
                            series ends before completing)

    A bar with a NaN high, low or close never reaches or jumps, ends an
    episode, and its close is not a valid close. Counts are side-agnostic:
    held includes rejections from above as well as from below.

    Returns ZoneHistory(touches, held, broke, last_index) with last_index
    the positional index of the newest episode's last bar (None if none).
    """
    if not len(high) == len(low) == len(close):
        raise ValueError("high, low and close must have the same length")
    if zone_high < zone_low:
        raise ValueError("zone_high must be >= zone_low")

    h = high.to_numpy(dtype=float)
    lo = low.to_numpy(dtype=float)
    c = close.to_numpy(dtype=float)
    n = len(c)
    valid = ~(np.isnan(h) | np.isnan(lo) | np.isnan(c))
    reach = valid & (h >= zone_low) & (lo <= zone_high)

    def side(i: int) -> int:
        """-1 below the band, +1 above, 0 inside or invalid."""
        if not valid[i]:
            return 0
        if c[i] < zone_low:
            return -1
        if c[i] > zone_high:
            return 1
        return 0

    touches = held = broke = 0
    last_index: Optional[int] = None
    last_outside = 0          # side of the last valid close outside the band
    last_episode_end = -2     # index of the last episode's final bar

    def outcome(start: int, end: int, approach: int) -> str:
        far = -approach
        for i in range(start, n):
            s = side(i)
            if s == far:
                return "broke"
            if i > end and s != 0:
                # the first valid close after the last bar: the window is complete
                break
        else:
            return "undecided"
        for i in range(start, min(start + hold_bars, n - 1) + 1):
            if side(i) == approach:
                return "held"
        return "undecided"

    i = 0
    while i < n:
        if reach[i]:
            start = i
            while i + 1 < n and reach[i + 1]:
                i += 1
            end = i
            if last_outside != 0:
                touches += 1
                last_index = end
                result = outcome(start, end, last_outside)
                held += result == "held"
                broke += result == "broke"
            for j in range(start, end + 1):
                last_outside = side(j) or last_outside
            last_episode_end = end
        else:
            s = side(i)
            if (s != 0 and last_outside != 0 and s == -last_outside
                    and i != last_episode_end + 1):
                touches += 1
                broke += 1
                last_index = i
                last_episode_end = i
            last_outside = s or last_outside
        i += 1
    return ZoneHistory(touches, held, broke, last_index)


# ── Stage 1b: volume nodes ────────────────────────────────────────────────


def volume_nodes(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    n_bins: int = 50,
    top_nodes: int = 5,
) -> list[float]:
    """
    Price levels where volume concentrated.

    The range [min low, max high] is split into `n_bins` equal bins. Each
    bar's volume is added to the bin holding its close. The `top_nodes`
    bins with the most volume are returned as their midpoints, highest
    volume first (ties: lower bin first). Bins with zero volume are never
    returned.

    Bars with NaN close or NaN volume are skipped. A zero-width range
    (flat series) yields a single node at that price.

    Returns:
        List of prices; empty if there are no valid bars or no volume.
    """
    lengths = {len(high), len(low), len(close), len(volume)}
    if len(lengths) != 1:
        raise ValueError("high, low, close and volume must have the same length")

    c = close.to_numpy(dtype=float)
    v = volume.to_numpy(dtype=float)
    valid = ~(np.isnan(c) | np.isnan(v))
    c, v = c[valid], v[valid]
    if len(c) == 0 or v.sum() <= 0:
        return []

    lo_edge = float(np.nanmin(low.to_numpy(dtype=float)))
    hi_edge = float(np.nanmax(high.to_numpy(dtype=float)))
    if np.isnan(lo_edge) or np.isnan(hi_edge):
        return []
    if hi_edge <= lo_edge:
        return [lo_edge]

    edges = np.linspace(lo_edge, hi_edge, n_bins + 1)
    bins = np.clip(np.digitize(c, edges) - 1, 0, n_bins - 1)
    per_bin = np.bincount(bins, weights=v, minlength=n_bins)

    # Stable sort by descending volume: ties resolve to the lower bin.
    order = np.argsort(-per_bin, kind="stable")
    top = [int(b) for b in order[:top_nodes] if per_bin[b] > 0]
    return [float((edges[b] + edges[b + 1]) / 2) for b in top]


# ── Stage 2: merge ────────────────────────────────────────────────────────


def merge_levels(levels: list[Level], merge_pct: float = 0.5) -> list[list[Level]]:
    """
    Group levels whose price sits within `merge_pct` percent of the group's
    running mean. Levels are visited in ascending price order; a level that
    does not fit the current group starts a new one.

    Returns:
        Groups in ascending price order; each group in ascending price order.
    """
    groups: list[list[Level]] = []
    current: list[Level] = []
    for level in sorted(levels, key=lambda lv: lv[0]):
        price = level[0]
        if current:
            mean = sum(lv[0] for lv in current) / len(current)
            if mean == 0:
                fits = price == 0
            else:
                fits = abs(price - mean) / abs(mean) * 100 <= merge_pct
            if fits:
                current.append(level)
                continue
            groups.append(current)
        current = [level]
    if current:
        groups.append(current)
    return groups


# ── Stage 3: score ────────────────────────────────────────────────────────


def score_zones(
    groups: list[list[Level]],
    n_bars: int,
    recent_bars: int = 20,
) -> list[Zone]:
    """
    Score each merged group and turn it into a Zone.

    Rubric (plan §6, Part 1.6; methods rule per docs/decisions.md
    2026-09-06 "volume is not a method"):
      +30  two methods: swing_high and swing_low both present
      +25  contains a volume node (not a method — a swing plus a node is 25)
      +20  tested two or more times (two or more swing members)
      +15  recent (newest swing member within the last `recent_bars` bars
           of the full series, NaN tail included)

    `n_bars` is the length of the full series the levels came from.
    """
    zones: list[Zone] = []
    for group in groups:
        prices = [lv[0] for lv in group]
        methods = tuple(sorted({lv[1] for lv in group if lv[1] != VOLUME}))
        volume_node = any(lv[1] == VOLUME for lv in group)
        swing_idx = [lv[2] for lv in group if lv[1] != VOLUME and lv[2] is not None]
        tests = len(swing_idx)
        recent = any(i >= n_bars - recent_bars for i in swing_idx)

        score = 0
        if len(methods) >= 2:
            score += 30
        if volume_node:
            score += 25
        if tests >= 2:
            score += 20
        if recent:
            score += 15

        zones.append(Zone(
            low=float(min(prices)),
            high=float(max(prices)),
            price=float(sum(prices) / len(prices)),
            score=score,
            methods=methods,
            tests=tests,
            recent=recent,
            volume_node=volume_node,
        ))
    return zones


# ── Stage 4: assemble ─────────────────────────────────────────────────────


def support_resistance(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    *,
    wing: int = 2,
    merge_pct: float = 0.5,
    n_bins: int = 50,
    top_nodes: int = 5,
    recent_bars: int = 20,
    top_n: int = 3,
) -> dict[str, list[Zone]]:
    """
    Top `top_n` support and resistance zones relative to the last valid close.

    Zones with price below the last close are support, all others (including
    a zone price equal to the close) are resistance. Each side is sorted by
    score descending, then by distance to the close ascending.

    Returns:
        {"support": [Zone, ...], "resistance": [Zone, ...]} — either list may
        be shorter than `top_n` or empty. Empty input or no valid close gives
        two empty lists.
    """
    lengths = {len(high), len(low), len(close), len(volume)}
    if len(lengths) != 1:
        raise ValueError("high, low, close and volume must have the same length")

    n = len(close)
    valid_close = close.dropna()
    if n == 0 or len(valid_close) == 0:
        return {"support": [], "resistance": []}
    last_close = float(valid_close.iloc[-1])

    swing_highs, swing_lows = fractal_swings(high, low, wing=wing)
    h = high.to_numpy(dtype=float)
    lo = low.to_numpy(dtype=float)
    levels: list[Level] = [(float(h[i]), SWING_HIGH, i) for i in swing_highs]
    levels += [(float(lo[i]), SWING_LOW, i) for i in swing_lows]
    levels += [
        (p, VOLUME, None)
        for p in volume_nodes(high, low, close, volume, n_bins=n_bins, top_nodes=top_nodes)
    ]

    zones = score_zones(merge_levels(levels, merge_pct=merge_pct), n_bars=n, recent_bars=recent_bars)

    def rank(z: Zone) -> tuple[int, float]:
        return (-z.score, abs(z.price - last_close))

    support = sorted((z for z in zones if z.price < last_close), key=rank)[:top_n]
    resistance = sorted((z for z in zones if z.price >= last_close), key=rank)[:top_n]
    return {"support": [_with_history(z, high, low, close) for z in support],
            "resistance": [_with_history(z, high, low, close) for z in resistance]}


def _with_history(zone: Zone, high: pd.Series, low: pd.Series, close: pd.Series) -> Zone:
    """The zone plus its history over the full series (4.8a-de). The last
    touch is an ISO date when the series carries a DatetimeIndex (stored
    daily bars: the date is `ts.date()`, no timezone conversion), else None."""
    hist = zone_history(high, low, close, zone.low, zone.high)
    last: Optional[str] = None
    if hist.last_index is not None and isinstance(close.index, pd.DatetimeIndex):
        last = close.index[hist.last_index].date().isoformat()
    return Zone(
        low=zone.low, high=zone.high, price=zone.price, score=zone.score,
        methods=zone.methods, tests=zone.tests, recent=zone.recent, volume_node=zone.volume_node,
        touches=hist.touches, held=hist.held, broke=hist.broke, last_touch=last,
    )
