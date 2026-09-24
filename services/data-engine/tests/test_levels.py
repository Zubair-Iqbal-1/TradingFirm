"""
Tests for indicators/levels.py (Part 1.6).

Every expectation is hand-computed on a tiny synthetic series with known
pivots. No network, no fixtures, no I/O.

Conventions under test (docs/decisions.md, Part 1.6):
  - strict fractals; NaN high/low disqualifies the bar and its window
  - volume nodes bin by close over [min low, max high]; NaN close/volume skipped
  - merge: a band never wider than 0.5 x ATR14 (2026-09-24); the 0.5 %
    running-mean rule when there is no ATR
  - rubric: +30 both swing methods, +25 volume node (not a method),
    +20 tested twice, +15 recent
  - split on last valid close; zone price == close is resistance; each
    side the nearest 6 inside [close - 2.5 ATR, close + 8 ATR], filled to 3
"""

import numpy as np
import pandas as pd
import pytest

from indicators import (
    Zone,
    fractal_swings,
    merge_levels,
    score_zones,
    support_resistance,
    volume_nodes,
)

# ── Shared synthetic series (17 bars, known pivots) ───────────────────────
#
# Swing highs (strict, wing 2):  idx 5 -> 110, idx 12 -> 108
# Swing lows  (strict, wing 2):  idx 2 -> 100, idx 8 -> 95, idx 14 -> 99.6
# Volume: all 1 except bar 13 (close 107) = 100, so with n_bins=15 over
# [95, 110] (width 1) the single top node is bin [107, 108) -> 107.5.
#
# ATR14 of the series = 4.9571 (bar 16), so the merge width is 2.4786:
#   95                        -> alone (99.6 is 4.6 above)
#   99.6, 100  (0.4 wide)     -> zone [99.6, 100], price 99.8
#   107.5, 108 (0.5 wide)     -> zone [107.5, 108], price 107.75
#   110                       -> alone (2.5 above 107.5 > 2.4786)
# (the same four groups the 0.5 % rule gave before 2026-09-24)
# Window on the last close 105: [92.6, 144.7] holds every zone; each side
# returns its zones nearest first.
#
# Scores with recent_bars=5 (recent = idx >= 12):
#   95      swing_low idx 8                 -> 0
#   99.8    2 swing lows, idx 14 recent     -> 20 + 15 = 35
#   107.75  swing_high idx 12 + volume      -> 25 + 15 = 40 (one method)
#   110     swing_high idx 5                -> 0
# Last close 105: support = [99.8, 95], resistance = [107.75, 110].

HIGH = pd.Series([103, 102, 101.5, 103, 105, 110, 106, 103, 102, 103, 104, 106, 108, 107, 103, 104, 106], dtype=float)
LOW = pd.Series([101, 100.5, 100, 100.8, 102, 106, 101, 99, 95, 98, 100, 102, 104, 100.5, 99.6, 101, 103], dtype=float)
CLOSE = pd.Series([102, 101, 100.5, 102, 104, 108, 102, 100, 96, 101, 103, 104, 106, 107, 100, 103, 105], dtype=float)
VOLUME = pd.Series([1] * 13 + [100] + [1] * 3, dtype=float)

EMPTY = pd.Series([], dtype=float)


def _prices(zones):
    return [z.price for z in zones]


def _run(close=CLOSE, **kw):
    kw.setdefault("n_bins", 15)
    kw.setdefault("top_nodes", 1)
    kw.setdefault("recent_bars", 5)
    return support_resistance(HIGH, LOW, close, VOLUME, **kw)


# ── Happy path: fractal_swings ────────────────────────────────────────────


def test_fractal_swings_finds_known_pivots():
    assert fractal_swings(HIGH, LOW) == ([5, 12], [2, 8, 14])


def test_fractal_swings_strict_ties_not_pivots():
    # A double top / double bottom at the exact same price is not a fractal.
    high = pd.Series([1, 2, 3, 3, 2, 1, 0], dtype=float)
    low = pd.Series([3, 2, 1, 1, 2, 3, 4], dtype=float)
    assert fractal_swings(high, low) == ([], [])


def test_fractal_swings_last_two_bars_never_pivots():
    # The extreme sits on the last bar, which has no right-hand window.
    high = pd.Series([1, 2, 3, 4, 5], dtype=float)
    low = pd.Series([5, 4, 3, 2, 1], dtype=float)
    assert fractal_swings(high, low) == ([], [])


# ── Happy path: volume_nodes ──────────────────────────────────────────────


def test_volume_nodes_picks_top_bins():
    # 4 bins of width 1 over [100, 104]; closes land one per bin.
    high = pd.Series([101, 102, 103, 104], dtype=float)
    low = pd.Series([100, 101, 102, 103], dtype=float)
    close = pd.Series([100.5, 101.5, 102.5, 103.5], dtype=float)
    volume = pd.Series([10, 40, 20, 30], dtype=float)
    out = volume_nodes(high, low, close, volume, n_bins=4, top_nodes=2)
    assert out == [101.5, 103.5]  # volume 40, then 30


# ── Happy path: merge_levels ──────────────────────────────────────────────


def test_merge_levels_groups_within_half_percent():
    # 100.4 vs 100 = 0.4% -> merged (mean 100.2).
    # 100.9 vs running mean 100.2 = 0.6986% -> new group, even though it is
    # 0.498% from 100.4 (a chain merge would have joined it).
    levels = [
        (100.0, "swing_low", 2),
        (105.0, "swing_high", 9),
        (100.9, "volume", None),
        (100.4, "swing_low", 5),
    ]
    groups = merge_levels(levels, merge_pct=0.5)
    assert [[lv[0] for lv in g] for g in groups] == [[100.0, 100.4], [100.9], [105.0]]


def test_merge_levels_does_not_merge_beyond_half_percent():
    groups = merge_levels([(100.0, "swing_low", 1), (100.6, "swing_low", 4)])
    assert len(groups) == 2


# ── Happy path: score_zones rubric ────────────────────────────────────────


@pytest.mark.parametrize(
    "group, expected",
    [
        pytest.param(
            [(100.0, "swing_low", 3)],
            Zone(100.0, 100.0, 100.0, 0, ("swing_low",), 1, False, False),
            id="swing_only_single_not_recent_0",
        ),
        pytest.param(
            [(100.0, "volume", None)],
            Zone(100.0, 100.0, 100.0, 25, (), 0, False, True),
            id="volume_only_25",
        ),
        pytest.param(
            [(100.0, "swing_low", 3), (100.3, "volume", None)],
            Zone(100.0, 100.3, 100.15, 25, ("swing_low",), 1, False, True),
            id="swing_plus_volume_is_one_method_25",
        ),
        pytest.param(
            [(100.0, "swing_high", 3), (100.3, "swing_low", 6)],
            Zone(100.0, 100.3, 100.15, 50, ("swing_high", "swing_low"), 2, False, False),
            id="two_swing_methods_tested_twice_50",
        ),
        pytest.param(
            [(100.0, "swing_high", 28), (100.3, "swing_low", 6), (100.2, "volume", None)],
            Zone(100.0, 100.3, 300.5 / 3, 90, ("swing_high", "swing_low"), 2, True, True),
            id="all_four_90",
        ),
    ],
)
def test_score_zones_rubric(group, expected):
    # n_bars=30, recent_bars=5 -> recent means index >= 25
    (zone,) = score_zones([group], n_bars=30, recent_bars=5)
    assert zone.price == pytest.approx(expected.price)
    assert zone == Zone(
        expected.low, expected.high, zone.price, expected.score,
        expected.methods, expected.tests, expected.recent, expected.volume_node,
    )


# ── Happy path: support_resistance ────────────────────────────────────────


def test_support_resistance_splits_by_last_close_and_ranks_by_score():
    out = _run()
    assert _prices(out["support"]) == pytest.approx([99.8, 95.0])
    assert [z.score for z in out["support"]] == [35, 0]
    assert _prices(out["resistance"]) == pytest.approx([107.75, 110.0])
    assert [z.score for z in out["resistance"]] == [40, 0]

    top = out["resistance"][0]
    # 4.8a-de: the zone carries its history (2 touches, both held from below; no date on a RangeIndex)
    assert top == Zone(107.5, 108.0, 107.75, 40, ("swing_high",), 1, True, True, 2, 2, 0, None, 2, 0, 0, 0)

    # Zone price == last close -> resistance, not support. Moving the last
    # close onto the 107.75 zone changes only that bar's bin (still bin
    # [107,108), still the top node); swings use high/low only.
    close = CLOSE.copy()
    close.iloc[-1] = 107.75
    out = _run(close=close)
    assert _prices(out["support"]) == pytest.approx([99.8, 95.0])
    assert _prices(out["resistance"]) == pytest.approx([107.75, 110.0])


def test_support_resistance_nearest_six_inside_the_window_filled_to_three():
    """2026-09-24 selection on a ladder: 40 bars closing at 100 with one
    swing low per dip (90..98) and one swing high per spike (102..110), no
    volume node (top_nodes=0), merge_atr=0 so every level is its own zone.
    The window is set in ATR units from the frame's own ATR14 so that its
    floor sits between 97 and 98 and its top between 108 and 109: support
    inside = 98 only, filled to 3 with 97 and 96 (nearest first); resistance
    inside = 102..108, the nearest 6 → 102..107."""
    from indicators.volatility import calc_atr
    close = pd.Series([100.0] * 40)
    high = pd.Series([101.0] * 40)
    low = pd.Series([99.0] * 40)
    for k, price in enumerate(range(90, 99)):          # swing lows at bars 2, 6, ...
        low.iloc[2 + 4 * k] = float(price)
    for k, price in enumerate(range(102, 111)):        # swing highs at bars 4, 8, ...
        high.iloc[4 + 4 * k] = float(price)
    atr = float(calc_atr(high, low, close, 14).iloc[-1])
    kw = dict(n_bins=1, top_nodes=0, merge_atr=0.0, window_below=2.5 / atr, window_above=8.5 / atr)
    out = support_resistance(high, low, close, pd.Series([1.0] * 40), **kw)
    assert _prices(out["support"]) == [98.0, 97.0, 96.0]
    assert _prices(out["resistance"]) == [102.0, 103.0, 104.0, 105.0, 106.0, 107.0]
    # a wider window lets 97 and 96 in on their own, and per_side 9 keeps 108 too
    out = support_resistance(high, low, close, pd.Series([1.0] * 40), **{**kw, "window_below": 4.5 / atr, "per_side": 9})
    assert _prices(out["support"]) == [98.0, 97.0, 96.0]
    assert _prices(out["resistance"]) == [102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0]
    # the defaults: never more than 6 a side, never fewer than 3 while zones exist
    out = support_resistance(high, low, close, pd.Series([1.0] * 40), n_bins=1, top_nodes=0)
    assert 3 <= len(out["support"]) <= 6 and 3 <= len(out["resistance"]) <= 6


def test_support_resistance_without_atr_sends_the_nearest_six():
    # 9 bars: no ATR14, so no window and the 0.5 % merge. 10 bins of width 1
    # over [100, 110]; eight volume-only zones below the close 110; the
    # nearest six are kept, nearest first.
    px = pd.Series([100, 101, 102, 103, 104, 105, 106, 107, 110], dtype=float)
    volume = pd.Series([8, 7, 6, 5, 4, 3, 2, 1, 0], dtype=float)
    out = support_resistance(px, px, px, volume, n_bins=10, top_nodes=8)
    assert len(out["support"]) == 6 and out["resistance"] == []
    assert _prices(out["support"]) == sorted(_prices(out["support"]), reverse=True)


def test_merge_levels_width_cap():
    """A band never grows wider than max_width: 100, 100.4, 100.8, 101.2 with
    max_width 1.0 → [100 .. 100.8] (0.8 wide) and [101.2] (1.2 > 1.0 from
    100), where the running-mean rule would have chained all four."""
    levels = [(100.0, "swing_low", 1), (100.4, "swing_low", 5), (100.8, "swing_high", 9), (101.2, "volume", None)]
    groups = merge_levels(levels, max_width=1.0)
    assert [[lv[0] for lv in g] for g in groups] == [[100.0, 100.4, 100.8], [101.2]]
    # the 0.5 % running-mean rule (no ATR) splits the same ladder in two:
    # 100.8 sits 0.6 % above mean(100, 100.4), 101.2 then joins 100.8
    assert [[lv[0] for lv in g] for g in merge_levels(levels, merge_pct=0.5)] == [[100.0, 100.4], [100.8, 101.2]]
    assert [[lv[0] for lv in g] for g in merge_levels(levels, max_width=0.3)] == [[100.0], [100.4], [100.8], [101.2]]


def test_support_resistance_empty_returns_empty_lists():
    assert support_resistance(EMPTY, EMPTY, EMPTY, EMPTY) == {"support": [], "resistance": []}


def test_fractal_swings_fewer_than_five_bars_returns_empty():
    high = pd.Series([1, 5, 1, 0], dtype=float)
    low = pd.Series([1, 0, 1, 5], dtype=float)
    assert fractal_swings(high, low) == ([], [])


def test_support_resistance_all_nan_returns_empty_lists():
    nan5 = pd.Series([np.nan] * 5)
    assert support_resistance(nan5, nan5, nan5, nan5) == {"support": [], "resistance": []}


def test_volume_nodes_zero_volume_returns_empty():
    zero = pd.Series([0, 0, 0, 0, 0], dtype=float)
    assert volume_nodes(HIGH.iloc[:5], LOW.iloc[:5], CLOSE.iloc[:5], zero) == []


def test_volume_nodes_flat_price_single_node():
    flat = pd.Series([50, 50, 50], dtype=float)
    assert volume_nodes(flat, flat, flat, pd.Series([1, 2, 3], dtype=float)) == [50.0]


def test_support_resistance_mismatched_lengths_raises():
    with pytest.raises(ValueError):
        support_resistance(HIGH, LOW.iloc[:-1], CLOSE, VOLUME)


def test_support_resistance_returns_fewer_than_three_when_scarce():
    # One zone in total (flat series -> one volume node at 50). Its price
    # equals the last close, so it lands on the resistance side.
    flat = pd.Series([50, 50, 50], dtype=float)
    out = support_resistance(flat, flat, flat, pd.Series([1, 2, 3], dtype=float))
    assert out["support"] == []
    assert out["resistance"] == [Zone(50.0, 50.0, 50.0, 25, (), 0, False, True)]


def test_support_resistance_is_deterministic():
    assert _run() == _run()


def test_fractal_swings_nan_bar_disqualifies_neighbours():
    # Without NaN: swing highs at 2 and 6, swing low at 4.
    high = pd.Series([1, 2, 5, 2, 1, 3, 9, 3, 1], dtype=float)
    low = pd.Series([0, 1, 4, 1, 0, 2, 8, 2, 0], dtype=float)
    assert fractal_swings(high, low) == ([2, 6], [4])

    # NaN high on bar 3 disqualifies bars 1-5: the swing high at 2 and the
    # swing low at 4 vanish; bar 6's window (4-8) is clean and survives.
    high.iloc[3] = np.nan
    assert fractal_swings(high, low) == ([6], [])


def test_volume_nodes_skips_nan_volume():
    high = pd.Series([101, 102, 103, 104], dtype=float)
    low = pd.Series([100, 101, 102, 103], dtype=float)
    close = pd.Series([100.5, 101.5, 102.5, 103.5], dtype=float)
    volume = pd.Series([10, np.nan, 20, 30], dtype=float)
    # Bin 1 (the 40 in the happy-path test) is now NaN and dropped.
    assert volume_nodes(high, low, close, volume, n_bins=4, top_nodes=2) == [103.5, 102.5]


# ── Part 4.8a-de: zone history and the last swing low ─────────────────────
#
# On the shared 17-bar series, hand-walked (spec 4.8a-de decision 2):
#   band [99.6, 100]  reach at 2 (approach from above: bar 1 close 101);
#                     close 100.5 above at e0 → held. Reach 7-10 (approach
#                     above, bar 6 close 102): close 7 = 100 inside, close
#                     8 = 96 below = far → broke. Reach 14 (approach above,
#                     bar 13 close 107): close 14 = 100 inside, close 15 =
#                     103 above at e0+1 → held.        → 3 / 2 / 1, last 14
#   band [107.5, 108] reach 5 (approach below): close 108 inside, close 6
#                     = 102 below at e0+1 → held. Reach 12 (approach below,
#                     bar 11 close 104): close 106 below at e0 → held.
#                                                        → 2 / 2 / 0, last 12
#   band [95, 95]     reach 8 only (approach above): close 96 → held → 1/1/0
#   band [110, 110]   reach 5 only (approach below): close 108 → held → 1/1/0

from indicators import SwingLow, ZoneHistory, last_swing_low, zone_history  # noqa: E402


def _hist(band, high=HIGH, low=LOW, close=CLOSE, **kw):
    return zone_history(high, low, close, *band, **kw)


def _bars(rows):
    """rows of (high, low, close) → three Series."""
    h, lo, c = zip(*rows)
    return (pd.Series(h, dtype=float), pd.Series(lo, dtype=float), pd.Series(c, dtype=float))


def test_zone_history_counts_touch_held_broke():
    # the split: (99.6, 100) was approached from above three times — held,
    # broke, held — so held_above 2 / broke_above 1 and nothing from below
    assert _hist((99.6, 100.0)) == ZoneHistory(3, 2, 1, 14, 0, 0, 2, 1)
    assert _hist((107.5, 108.0)) == ZoneHistory(2, 2, 0, 12, 2, 0, 0, 0)
    assert _hist((95.0, 95.0)) == ZoneHistory(1, 1, 0, 8, 0, 0, 1, 0)
    assert _hist((110.0, 110.0)) == ZoneHistory(1, 1, 0, 5, 1, 0, 0, 0)
    # a band the series never reaches
    assert _hist((120.0, 121.0)) == ZoneHistory(0, 0, 0, None)


def test_zone_history_far_close_in_episode_beats_earlier_hold():
    """Approval change 1: resistance 50.00-50.20 from below; day 1 high 50.10
    close 49.80 (an approach-side close), day 2 low 49.90 high 50.60 close
    50.50 (a far close in the same episode) → broke, not held."""
    h, lo, c = _bars([(49.8, 49.0, 49.5), (50.1, 49.5, 49.8), (50.6, 49.9, 50.5), (51.0, 50.4, 50.8)])
    assert zone_history(h, lo, c, 50.0, 50.2) == ZoneHistory(1, 0, 1, 2, 0, 1, 0, 0)


def test_zone_history_touch_that_holds_on_its_own_close():
    h, lo, c = _bars([(49.8, 49.0, 49.5), (50.1, 49.5, 49.8), (49.9, 49.2, 49.6)])
    assert zone_history(h, lo, c, 50.0, 50.2) == ZoneHistory(1, 1, 0, 1, 1, 0, 0, 0)
    # the last bar of an episode also starts its window: no close after it → undecided
    assert zone_history(h[:2], lo[:2], c[:2], 50.0, 50.2) == ZoneHistory(1, 0, 0, 1)


def test_zone_history_slow_rejection_is_undecided():
    """Five closes inside the band, then a close back on the approach side at
    e0+5: no far close, but the hold came too late → undecided."""
    rows = [(49.8, 49.0, 49.5)] + [(50.3, 49.9, 50.1)] * 5 + [(49.8, 49.0, 49.5)]
    h, lo, c = _bars(rows)
    assert zone_history(h, lo, c, 50.0, 50.2) == ZoneHistory(1, 0, 0, 5)
    # the same shape rejected at e0+3 is held
    rows = [(49.8, 49.0, 49.5)] + [(50.3, 49.9, 50.1)] * 3 + [(49.8, 49.0, 49.5)]
    h, lo, c = _bars(rows)
    assert zone_history(h, lo, c, 50.0, 50.2) == ZoneHistory(1, 1, 0, 3, 1, 0, 0, 0)
    # hold_bars is the knob
    assert zone_history(h, lo, c, 50.0, 50.2, hold_bars=2) == ZoneHistory(1, 0, 0, 3)


def test_zone_history_far_close_after_long_inside_run_is_broke():
    """Approval change 1, the long case: six closes inside the band, then a
    far-side close at e0+6 → broke (a far close anywhere in the window
    wins, however late), whether that bar has left the band or still
    reaches it."""
    inside = (50.3, 49.9, 50.1)
    h, lo, c = _bars([(49.8, 49.0, 49.5)] + [inside] * 6 + [(51.5, 50.5, 51.0)])
    assert zone_history(h, lo, c, 50.0, 50.2) == ZoneHistory(1, 0, 1, 6, 0, 1, 0, 0)
    h, lo, c = _bars([(49.8, 49.0, 49.5)] + [inside] * 6 + [(51.5, 50.1, 51.0)])
    assert zone_history(h, lo, c, 50.0, 50.2) == ZoneHistory(1, 0, 1, 7, 0, 1, 0, 0)


def test_zone_history_gap_through_band_is_a_break():
    # day 1 opens and closes above the band without a bar inside it
    h, lo, c = _bars([(49.8, 49.0, 49.5), (51.5, 50.5, 51.0), (51.8, 51.0, 51.3)])
    assert zone_history(h, lo, c, 50.0, 50.2) == ZoneHistory(1, 0, 1, 1, 0, 1, 0, 0)
    # the bar after an episode is judged by the outcome rule, never as a jump
    h, lo, c = _bars([(49.8, 49.0, 49.5), (50.1, 49.5, 49.8), (51.5, 50.5, 51.0)])
    assert zone_history(h, lo, c, 50.0, 50.2) == ZoneHistory(1, 0, 1, 1, 0, 1, 0, 0)


def test_zone_history_approach_side_flips():
    """Held from below, a gap through it, then held from above: counts are
    side-agnostic (decisions 2026-09-23)."""
    h, lo, c = _bars([
        (49.8, 49.0, 49.5), (50.1, 49.6, 49.7), (49.9, 49.0, 49.6),   # held from below
        (52.0, 51.0, 51.5), (51.8, 51.0, 51.3),                        # jump: broke
        (51.2, 50.1, 50.9), (51.5, 50.6, 51.0),                        # held from above
    ])
    assert zone_history(h, lo, c, 50.0, 50.2) == ZoneHistory(3, 2, 1, 5, 1, 1, 1, 0)   # held from below, broke from below (the jump), held from above


def test_zone_history_skips_until_a_close_outside():
    # the series opens inside the band: no approach side, so no touch until
    # a close outside exists
    h, lo, c = _bars([(50.15, 49.95, 50.10), (50.15, 49.95, 50.05), (49.8, 49.0, 49.5),
                      (50.1, 49.5, 49.8), (49.9, 49.2, 49.6)])
    assert zone_history(h, lo, c, 50.0, 50.2) == ZoneHistory(1, 1, 0, 3, 1, 0, 0, 0)


def test_zone_history_nan_bars_ignored():
    # a NaN bar ends the episode and is not a valid close; the next valid
    # close (e0+2, approach side) completes the window → held
    h, lo, c = _bars([(49.8, 49.0, 49.5), (50.1, 49.5, 49.8), (np.nan, np.nan, np.nan),
                      (49.9, 49.2, 49.6), (50.1, 49.5, 49.9), (49.9, 49.2, 49.6)])
    assert zone_history(h, lo, c, 50.0, 50.2) == ZoneHistory(2, 2, 0, 4, 2, 0, 0, 0)
    # a NaN close alone disqualifies the bar too
    h, lo, c = _bars([(49.8, 49.0, 49.5), (50.1, 49.5, np.nan), (49.9, 49.2, 49.6)])
    assert zone_history(h, lo, c, 50.0, 50.2) == ZoneHistory(0, 0, 0, None)


def test_zone_history_open_episode_is_undecided():
    # the series ends while price sits at the level: not yet held
    h, lo, c = _bars([(49.8, 49.0, 49.5), (50.1, 49.5, 49.8), (50.3, 49.9, 50.1)])
    assert zone_history(h, lo, c, 50.0, 50.2) == ZoneHistory(1, 0, 0, 2)
    # but a far close already seen is a break whatever follows
    h, lo, c = _bars([(49.8, 49.0, 49.5), (50.6, 49.9, 50.5), (50.3, 49.9, 50.1)])
    assert zone_history(h, lo, c, 50.0, 50.2) == ZoneHistory(1, 0, 1, 2, 0, 1, 0, 0)


def test_zone_history_empty_series():
    assert zone_history(EMPTY, EMPTY, EMPTY, 50.0, 50.2) == ZoneHistory(0, 0, 0, None)
    with pytest.raises(ValueError):
        zone_history(HIGH, LOW, CLOSE[:3], 50.0, 50.2)
    with pytest.raises(ValueError):
        zone_history(HIGH, LOW, CLOSE, 50.2, 50.0)


def test_support_resistance_zones_carry_history():
    out = _run()
    by_price = {round(z.price, 2): z for z in out["support"] + out["resistance"]}
    assert (by_price[99.8].touches, by_price[99.8].held, by_price[99.8].broke) == (3, 2, 1)
    assert (by_price[99.8].held_below, by_price[99.8].broke_below, by_price[99.8].held_above, by_price[99.8].broke_above) == (0, 0, 2, 1)
    assert (by_price[107.75].touches, by_price[107.75].held, by_price[107.75].broke) == (2, 2, 0)
    assert (by_price[107.75].held_below, by_price[107.75].broke_below) == (2, 0)
    # a RangeIndex has no date to name
    assert by_price[99.8].last_touch is None
    # a DatetimeIndex names the last touch by its bar date, no tz conversion
    idx = pd.date_range("2026-01-01", periods=len(CLOSE), freq="D", tz="UTC")
    dated = support_resistance(HIGH.set_axis(idx), LOW.set_axis(idx), CLOSE.set_axis(idx),
                               VOLUME.set_axis(idx), n_bins=15, top_nodes=1, recent_bars=5)
    dated_by_price = {round(z.price, 2): z for z in dated["support"] + dated["resistance"]}
    assert dated_by_price[99.8].last_touch == "2026-01-15"
    assert dated_by_price[107.75].last_touch == "2026-01-13"


def test_last_swing_low_is_newest_pivot():
    # swing lows at 2 (100), 8 (95), 14 (99.6): the newest wins, not the lowest
    assert last_swing_low(HIGH, LOW) == SwingLow(99.6, 14)


def test_last_swing_low_none_without_pivot():
    assert last_swing_low(HIGH[:4], LOW[:4]) is None
    assert last_swing_low(EMPTY, EMPTY) is None
    flat = pd.Series([100.0] * 9)
    assert last_swing_low(flat, flat) is None


def test_zone_history_side_split_sums_to_the_totals():
    """The four split counts partition held and broke by approach side; the
    totals stay what they were (2026-09-24)."""
    for band in ((99.6, 100.0), (107.5, 108.0), (95.0, 95.0), (110.0, 110.0)):
        h = _hist(band)
        assert h.held == h.held_below + h.held_above and h.broke == h.broke_below + h.broke_above
        assert h.touches >= h.held + h.broke
