"""
Part 4.3 / 4.8a / 4.8a-de — plan math v3. Every number below is hand-checked
in the specs' worked examples; they are asserted exactly, never approximately.

Fixture: entry 50.00, ATR 1.20, account 25,000, risk 1 %.
  stop zone 47.80-48.10 → stop 46.60 (risk 3.40 = 2.83 ATR: far, but no
  alternative is given, so the zone stop stays and the plan is `extended`
  with entryForMaxRisk 46.60 + 2.40 = 49.00), disaster 45.40.
  Target cap 8 × 1.20 = 9.60 above entry → 59.60.
  53.90 pays 3.90 / 3.40 = 1.15R → overhead; 56.90 pays 6.90 / 3.40 = 2.03R → T1.
  Size: risk 250 / 3.40 = 73, max position 6,250 / 50 = 125, cash 500,
  disaster loss 625 / 4.60 = 135 → 73 (risk). Loss at disaster
  73 × 4.60 / 25,000 = 1.34 %.
"""

import ast
import copy
import math
import re
from decimal import Decimal
from pathlib import Path

import pytest

from grading import plan_math
from grading.plan_math import PlanMath, PlanRejected, Target, atr_distance, compute_plan, r_multiple, to_cents


def zone(low, high=None, **over):
    """The dossier's zone shape; plan math reads low and high, and names
    tests / volumeNode / score in the basis text."""
    high = low if high is None else high
    z = {"low": low, "high": high, "price": (low + high) / 2, "score": 50,
         "methods": ["swing_low"], "tests": 2, "recent": True, "volumeNode": False}
    z.update(over)
    return z


ZONES_A = [zone(45.00, 45.40), zone(47.80, 48.10), zone(53.90, 54.30), zone(56.90, 57.30)]


def plan(**over):
    args = {"entry": 50.00, "atr": 1.20, "zones": ZONES_A, "account": 25_000, "risk_pct": 1.0}
    args.update(over)
    return compute_plan(**args)


def prices(levels):
    return [t.price for t in levels]


def rs(levels):
    return [t.r for t in levels]


# ── Worked examples ───────────────────────────────────────────────────────


def test_example_a_accepted():
    p = plan()
    assert isinstance(p, PlanMath)
    assert p.entry == 50.00
    assert p.stop == 46.60
    assert p.disaster_line == 45.40
    assert p.risk_per_share == 3.40
    assert prices(p.targets) == [56.90] and rs(p.targets) == [2.03]
    assert prices(p.overhead) == [53.90] and rs(p.overhead) == [1.15]
    assert p.best_r == 2.03
    assert p.risk_budget == 250.00
    assert p.size_shares == 73
    assert p.size_bound == "risk"
    assert p.loss_at_disaster_pct == 1.34
    assert p.size_basis == "size: risk-bound (max position ≤ 25 %, cash cap, disaster loss ≤ 2.5 % not binding)"
    assert p.stop_basis == ("stop 46.60: support 47.80-48.10, 2 tests, score 50, low 47.80 - 1xATR 1.20; "
                            "extended: risk 3.40 = 2.83 ATR (> 2 ATR), entry for 2 ATR risk 49.00 = "
                            "stop 46.60 + 2xATR 1.20")
    assert p.extended is True and p.entry_for_max_risk == 49.00
    assert p.targets[0].basis == "T1 56.90: resistance 56.90-57.30, 2 tests, score 50"
    assert p.overhead[0].basis == "overhead 53.90: resistance 53.90-54.30, 2 tests, score 50"


def test_example_b_rejected_low_r():
    r = plan(zones=ZONES_A[:3])
    assert r == PlanRejected("low_r", "no resistance pays >= 1.5R: best R 1.15")


def test_example_c_max_position_binds():
    # ATR 0.05: cap 0.30 above entry, so the target sits at 50.28 (1.87R on a 0.15 risk)
    p = plan(atr=0.05, zones=[zone(49.90, 49.95), zone(50.28, 50.30)], account=10_000, risk_pct=2.0)
    assert p.stop == 49.85
    assert p.disaster_line == 49.80
    assert prices(p.targets) == [50.28] and rs(p.targets) == [1.87]
    assert p.size_shares == 50
    assert p.size_bound == "max position"
    assert p.size_basis == "size: max position ≤ 25 %-bound (risk, cash cap, disaster loss ≤ 2.5 % not binding)"
    assert p.loss_at_disaster_pct == 0.10


def test_example_d_rounding():
    # v3 (4.8a-de change 8): the components floor first — 47.80 - 1.20, not
    # floor(47.803 - 1.2047) = 46.59 — so the basis text subtracts to the level
    p = plan(atr=1.2047, zones=[zone(47.803, 48.10), zone(56.90, 57.00)])
    assert p.stop == 46.60
    assert p.disaster_line == 45.40
    assert rs(p.targets) == [2.03]   # 6.90 / 3.40


def test_float_inputs_never_reach_arithmetic():
    # the trap this rule exists for: float subtraction then a floor
    assert 47.80 - 1.20 == 46.599999999999994
    assert math.floor((47.80 - 1.20) * 100) / 100 == 46.59
    p = plan(zones=[zone(47.80), zone(56.90)])
    assert p.stop == 46.60
    assert p.disaster_line == 45.40


def test_cash_cap_binds_when_position_limit_raised(monkeypatch):
    monkeypatch.setattr(plan_math, "MAX_POSITION_PCT", Decimal("150"))
    p = plan(atr=0.05, zones=[zone(49.90), zone(50.28)], account=10_000, risk_pct=2.0)
    assert p.size_shares == 200
    assert p.size_bound == "cash cap"


def test_risk_bound_wins_a_tie():
    # risk sizing 125 == max position 125: the first bound in the order names it
    p = plan(atr=0.5, zones=[zone(48.50), zone(53.00)], account=25_000, risk_pct=1.0)
    assert p.risk_per_share == 2.00
    assert p.size_shares == 125
    assert p.size_bound == "risk"


def test_size_capped_by_disaster_loss():
    # ATR 2: stop 45.80, disaster 43.80 (6.20 below entry). 2.5 % of 25,000 =
    # 625 / 6.20 = 100 shares, under risk 750 / 4.20 = 178 and max position 125.
    p = plan(atr=2.0, risk_pct=3.0)
    assert (p.stop, p.disaster_line) == (45.80, 43.80)
    assert p.size_shares == 100
    assert p.size_bound == "disaster loss"
    assert p.loss_at_disaster_pct == 2.48
    assert p.loss_at_disaster_pct <= float(plan_math.MAX_DISASTER_LOSS_PCT)
    assert p.size_basis == "size: disaster loss ≤ 2.5 %-bound (risk, max position ≤ 25 %, cash cap not binding)"


# ── Zone selection ────────────────────────────────────────────────────────


def test_zones_resplit_around_entry_not_last_close():
    # entry 52 sits above the 50.50 zone data-engine may have called resistance
    p = plan(entry=52.00, zones=[zone(57.50), zone(47.80), zone(50.50, 50.90), zone(59.00)])
    assert p.stop == 49.30  # 50.50 - 1.20
    assert prices(p.targets) == [57.50, 59.00]


def test_support_zone_straddling_entry_is_stop_zone():
    # 49.50-50.40 has its midpoint (49.95) below the entry: it is the stop
    # zone, consulted before the 47.80 zone below it
    p = plan(zones=[zone(47.80), zone(49.50, 50.40), zone(56.90)])
    assert p.stop == 48.30
    assert p.stop_basis.startswith("stop 48.30: support 49.50-50.40")


def test_resistance_zone_straddling_entry_is_overhead():
    # 49.70-50.40 has its midpoint (50.05) at or above the entry: overhead
    # first, at the zone high, with its R (0.40 / 3.40 = 0.12)
    p = plan(zones=[zone(47.80), zone(49.70, 50.40), zone(56.90)])
    assert p.stop == 46.60
    assert prices(p.overhead) == [50.40] and rs(p.overhead) == [0.12]
    assert p.overhead[0].basis == "overhead 50.40: zone high, resistance 49.70-50.40, 2 tests, score 50 straddles entry 50.00"
    assert prices(p.targets) == [56.90]
    # a straddling zone whose high pays >= 1.5R is still overhead, never T1
    p = plan(atr=0.5, zones=[zone(49.00), zone(49.90, 52.60), zone(53.00)])
    assert p.stop == 48.50
    assert prices(p.overhead) == [52.60] and rs(p.overhead) == [1.73]
    assert prices(p.targets) == [53.00] and rs(p.targets) == [2.00]
    # and alone it is no plan
    r = plan(atr=0.5, zones=[zone(49.00), zone(49.90, 52.60)])
    assert r == PlanRejected("low_r", "no target outside the zone straddling entry 50.0 (best R 1.73 is inside it)")


def test_zone_side_from_label_beats_midpoint():
    """data-engine's label decides; the midpoint is only the fallback for a
    zone without one (older stored dossiers in the rerun)."""
    # midpoint 50.05 says resistance, the label says support → the stop zone
    p = plan(zones=[zone(47.80), zone(49.70, 50.40, side="support"), zone(56.90)])
    assert p.stop == 48.50 and p.overhead == ()
    # midpoint 49.95 says support, the label says resistance → overhead at the high
    p = plan(zones=[zone(47.80), zone(49.50, 50.40, side="resistance"), zone(56.90)])
    assert p.stop == 46.60 and prices(p.overhead) == [50.40]
    # a labelled zone entirely on one side keeps its label whatever the midpoint says
    p = plan(zones=[zone(47.80, side="support"), zone(56.90, side="resistance")])
    assert p.stop == 46.60 and prices(p.targets) == [56.90]
    # an unknown label is a contract break
    with pytest.raises(ValueError, match="zone 1 side"):
        plan(zones=[zone(47.80), zone(56.90, side="above")])
    with pytest.raises(ValueError, match="zone 0 side"):
        plan(zones=[zone(47.80, side=True), zone(56.90)])


def test_zone_low_equal_to_entry_is_overhead():
    p = plan(zones=[zone(47.80), zone(50.00, 50.30), zone(56.90)])
    assert p.stop == 46.60
    assert prices(p.overhead) == [50.30] and rs(p.overhead) == [0.09]


def test_t1_is_first_zone_paying_min_r_and_nearer_zones_are_overhead():
    p = plan(zones=ZONES_A + [zone(57.10, 57.40)])
    assert prices(p.overhead) == [53.90]
    assert prices(p.targets) == [56.90, 57.10] and rs(p.targets) == [2.03, 2.09]
    assert [t.basis[:3] for t in p.targets] == ["T1 ", "T2 "]


def test_targets_capped_at_three_nearest_ascending():
    # ATR 3: stop 44.80, risk 5.20, cap 18. 53.90 (0.75R) and 57.50 (1.44R)
    # are overhead; 60 / 62 / 65 are the targets, 67 is the fourth
    p = plan(atr=3.0, zones=[zone(47.80), zone(67.00), zone(53.90), zone(62.00), zone(60.00), zone(57.50), zone(65.00)])
    assert prices(p.overhead) == [53.90, 57.50] and rs(p.overhead) == [0.75, 1.44]
    assert prices(p.targets) == [60.00, 62.00, 65.00] and rs(p.targets) == [1.92, 2.31, 2.88]


def test_overhead_capped_at_three():
    # four weak zones then T1: the list keeps the nearest three, the walk
    # still reaches 58.00 (8.00 / 5.20 = 1.54R)
    p = plan(atr=3.0, zones=[zone(47.80), zone(51.00), zone(52.00), zone(53.00), zone(54.00), zone(58.00)])
    assert prices(p.overhead) == [51.00, 52.00, 53.00]
    assert prices(p.targets) == [58.00] and rs(p.targets) == [1.54]


def test_target_floored_to_entry_is_dropped():
    p = plan(zones=[zone(47.80), zone(50.004), zone(56.90)])
    assert prices(p.targets) == [56.90] and p.overhead == ()


def test_targets_flooring_to_same_cent_collapse():
    p = plan(zones=[zone(47.80), zone(56.901), zone(56.909)])
    assert prices(p.targets) == [56.90] and rs(p.targets) == [2.03]


def test_only_targets_that_floor_onto_entry_is_no_target():
    assert plan(zones=[zone(47.80), zone(50.004)]) == PlanRejected("no_target", "no resistance above entry 50.0")


def test_extra_zone_keys_ignored():
    assert plan(zones=[{"low": 47.80, "high": 48.10}, {"low": 56.90, "high": 57.00}]).stop == 46.60


# ── The target cap (decision 2) ───────────────────────────────────────────


def test_target_beyond_atr_cap_is_dropped():
    p = plan(zones=ZONES_A + [zone(60.00)])
    assert prices(p.targets) == [56.90]


def test_all_targets_beyond_cap_is_no_target():
    r = plan(zones=[zone(47.80), zone(60.00), zone(70.00)])
    assert r == PlanRejected("no_target", "every resistance above entry 50.0 is beyond 8xATR 9.60: 60.00, 70.00")


def test_target_cap_boundary_inclusive():
    # exactly 8 ATR (59.60) is kept; 59.61 is 8.01 ATR and dropped
    p = plan(zones=[zone(47.80), zone(59.60)])
    assert prices(p.targets) == [59.60] and rs(p.targets) == [2.82]
    assert plan(zones=[zone(47.80), zone(59.61)]).reason == "no_target"
    # the quantized boundary: OPCH's ATR. 29.38 sits 5.43 above 23.95, and
    # 8 × 0.6785711560930524 = 5.428569 — a raw comparison drops it, but the
    # distance is 8.0023 → 8.00 ATR, so it is kept; 29.39 is 8.02 and dropped
    opch = dict(entry=23.95, atr=0.6785711560930524, zones=[zone(22.20, 22.29), zone(29.38)], ema20=23.8845)
    p = compute_plan(account=25_000, risk_pct=1.0, **opch)
    assert prices(p.targets) == [29.38]
    opch["zones"] = [zone(22.20, 22.29), zone(29.39)]
    assert compute_plan(account=25_000, risk_pct=1.0, **opch).reason == "no_target"
    assert atr_distance(Decimal("29.38"), Decimal("23.95"), Decimal("0.6785711560930524")) == Decimal("8.00")
    assert atr_distance(Decimal("28.70"), Decimal("23.95"), Decimal("0.6785711560930524")) == Decimal("7.00")


# ── The far-support stop (decision 3) ─────────────────────────────────────

FAR = [zone(48.20), zone(56.00)]   # stop zone 48.20 → 47.00, risk 3.00 = 2.50 ATR; T 56.00 within the 7.20 cap


def test_far_support_takes_nearer_of_swing_low_and_ema20():
    p = plan(zones=FAR, ema20=49.00, swing_low=48.50, swing_low_date="2026-09-15")
    assert p.stop == 47.80
    assert p.stop_basis == ("stop 47.80: EMA20 49.00 - 1xATR 1.20; "
                            "nearest support 48.20-48.20 gives risk 3.00 = 2.50 ATR (> 2 ATR)")
    assert rs(p.targets) == [2.73]   # 6.00 / 2.20
    assert p.extended is False and p.entry_for_max_risk is None
    p = plan(zones=FAR, ema20=49.00, swing_low=49.20, swing_low_date="2026-09-15")
    assert p.stop == 48.00
    assert p.stop_basis.startswith("stop 48.00: swing low (2026-09-15) 49.20 - 1xATR 1.20; nearest support 48.20-48.20")


def test_far_support_ignores_ema20_above_entry():
    p = plan(zones=FAR, ema20=50.50, swing_low=48.50)
    assert p.stop == 47.30
    assert "swing low 48.50" in p.stop_basis


def test_far_support_without_alternative_keeps_zone_stop():
    for kw in ({}, {"ema20": 50.50, "swing_low": 51.00}, {"ema20": 44.00}, {"ema20": math.nan, "swing_low": 0.0}):
        p = plan(zones=FAR, **kw)
        assert p.stop == 47.00, kw
        assert p.stop_basis.startswith("stop 47.00: support 48.20-48.20")
        assert rs(p.targets) == [2.00]   # 6.00 / 3.00


def test_near_support_never_uses_alternative_stop():
    # stop zone 49.00 → 47.80, risk 2.20 = 1.83 ATR: not far, so an EMA20
    # that would give a tighter 48.70 is never consulted
    p = plan(zones=[zone(49.00), zone(56.90)], ema20=49.90, swing_low=49.95)
    assert p.stop == 47.80
    assert "EMA20" not in p.stop_basis and "swing" not in p.stop_basis


def test_missing_swing_low_falls_back_to_ema20():
    p = plan(zones=FAR, ema20=49.00, swing_low=None)
    assert p.stop == 47.80 and "EMA20 49.00" in p.stop_basis


def test_rejects_no_support_below_entry():
    # no zone below the entry is far support (risk = infinity): the EMA20
    # alternative is consulted; without one it is no_support as before
    r = plan(zones=[zone(53.90), zone(56.90)])
    assert r == PlanRejected("no_support", "no zone with midpoint < entry 50.0 among 2, "
                                           "and no EMA20 / swing low at or below it")
    p = plan(zones=[zone(53.90), zone(56.90)], ema20=49.00)
    assert p.stop == 47.80
    assert p.stop_basis == "stop 47.80: EMA20 49.00 - 1xATR 1.20; no support zone below entry 50.00"
    assert prices(p.targets) == [53.90, 56.90] and rs(p.targets) == [1.77, 3.14]
    assert plan(zones=[zone(53.90), zone(56.90)], ema20=50.50).reason == "no_support"


@pytest.mark.parametrize("bad", ["49", True, [49.0]])
def test_alternative_levels_must_be_numbers_or_none(bad):
    with pytest.raises(ValueError):
        plan(ema20=bad)
    with pytest.raises(ValueError):
        plan(swing_low=bad)
    with pytest.raises(ValueError):
        plan(swing_low_date=49)


# ── Basis text (change 6, verdict-units decision 6) ──────────────────────


def test_level_basis_names_zone():
    zones = [zone(47.80, 48.10, tests=4, score=65), zone(56.90, 57.30, tests=3, volumeNode=True, score=85)]
    p = plan(zones=zones)
    assert p.targets[0].basis == "T1 56.90: resistance 56.90-57.30, 3 tests, volume node, score 85"
    assert p.stop_basis.startswith("stop 46.60: support 47.80-48.10, 4 tests, score 65, low 47.80 - 1xATR 1.20; extended")
    # a zone with low and high only still has a basis
    p = plan(zones=[{"low": 47.80, "high": 48.10}, {"low": 56.90, "high": 57.30}])
    assert p.targets[0].basis == "T1 56.90: resistance 56.90-57.30"
    assert p.stop_basis.startswith("stop 46.60: support 47.80-48.10, low 47.80 - 1xATR 1.20; extended")


def test_zone_history_replaces_tests_in_basis():
    """4.8a-de change 7: a zone with history prints touches / held / broke /
    last and not its `tests` count, so a level reads one way; a zone without
    history still prints the swing count. Malformed fields are not printed."""
    zones = [zone(47.80, 48.10, tests=4, touches=6, held=4, broke=1, lastTouch="2026-09-02", score=65),
             zone(56.90, 57.30, tests=3, touches=3, held=3, broke=0, lastTouch="2026-08-12", volumeNode=True, score=85)]
    p = plan(zones=zones)
    assert p.targets[0].basis == ("T1 56.90: resistance 56.90-57.30, touches 3, held 3, broke 0, "
                                  "last 2026-08-12, volume node, score 85, ceiling")
    assert p.stop_basis.startswith("stop 46.60: support 47.80-48.10, touches 6, held 4, broke 1, last 2026-09-02, "
                                   "score 65, low 47.80 - 1xATR 1.20")
    assert "tests" not in p.stop_basis and "tests" not in p.targets[0].basis
    # malformed → not printed, never a raise; `held` alone still switches the text
    p = plan(zones=[zone(47.80), zone(56.90, held=None, broke="2", lastTouch=" ", tests=True, touches=-1)])
    assert p.targets[0].basis == "T1 56.90: resistance 56.90-56.90, score 50"
    p = plan(zones=[zone(47.80), zone(56.90, held=1, tests=3)])
    assert p.targets[0].basis == "T1 56.90: resistance 56.90-56.90, held 1, score 50"


def test_level_and_basis_share_rounding():
    """One rounding — floor to the cent — for a level and for the text that
    names its zone. OUST live 2026-09-23: the 49.389999 zone low printed as
    T1 49.38 but its basis said 49.39-49.50 under a half-up text rounding."""
    zones = [zone(32.970001220703125, 33.08000183105469), zone(40.4900016784668, 40.599998474121094),
             zone(48.25, 48.380001068115234), zone(49.38999938964844, 49.5)]
    p = plan(entry=40.25, atr=2.0208, zones=zones, ema20=36.596, account=25_000, risk_pct=1.0)
    t1 = p.targets[0]
    assert t1.price == 49.38 and t1.basis.startswith("T1 49.38: resistance 49.38-49.50")
    for level in p.targets + p.overhead:
        low = level.basis.split(": resistance ")[1].split("-")[0]
        assert f"{level.price:.2f}" == low, level.basis
    # the stop is built from the floored components (v3), so the printed
    # subtraction is the level: 36.59 - 2.02 = 34.57
    assert p.stop == 34.57
    assert p.stop_basis.startswith("stop 34.57: EMA20 36.59 - 1xATR 2.02;")
    # a zone low that floors down and rounds up prints the floored figure everywhere
    p = plan(zones=[zone(47.80), zone(56.909, 57.309)])
    assert p.targets[0].price == 56.90 and p.targets[0].basis == "T1 56.90: resistance 56.90-57.30, 2 tests, score 50"


def test_basis_text_is_in_cents():
    p = plan(atr=1.2047, zones=[zone(47.803, 48.10), zone(56.90, 57.00)])
    assert p.stop_basis.startswith("stop 46.60: support 47.80-48.10, 2 tests, score 50, low 47.80 - 1xATR 1.20; "
                                   "extended: risk 3.40 = 2.82 ATR (> 2 ATR), entry for 2 ATR risk 49.00")


# ── Failure branches (spec 4.8a table) ────────────────────────────────────


@pytest.mark.parametrize("atr", [None, math.nan, math.inf, -math.inf, 0, 0.0, -1.2, "1.2", True])
def test_rejects_missing_atr(atr):
    r = plan(atr=atr)
    assert isinstance(r, PlanRejected)
    assert r.reason == "no_atr"


def test_rejects_empty_zones():
    assert plan(zones=[]).reason == "no_support"


def test_rejects_no_resistance_above_entry():
    r = plan(zones=[zone(45.00), zone(47.80)])
    assert r == PlanRejected("no_target", "no resistance above entry 50.0")


def test_rejects_best_r_below_threshold():
    # 1.49: (target - 50) / 3.40 → 55.06 gives 1.4882 → 1.49
    r = plan(zones=[zone(47.80), zone(55.06)])
    assert r == PlanRejected("low_r", "no resistance pays >= 1.5R: best R 1.49")


def test_accepts_best_r_at_threshold():
    # 55.10 → 5.10 / 3.40 = exactly 1.50
    p = plan(zones=[zone(47.80), zone(55.10)])
    assert p.best_r == 1.50 and p.overhead == ()


def test_threshold_uses_rounded_r():
    # 55.083 floors to 55.08 → 5.08 / 3.40 = 1.4941 → 1.49: rejected
    assert plan(zones=[zone(47.80), zone(55.083)]).reason == "low_r"
    # 55.09 → 5.09 / 3.40 = 1.4970… → 1.50: accepted, as printed
    assert plan(zones=[zone(47.80), zone(55.09)]).best_r == 1.50


def test_rejects_non_positive_stop():
    r = plan(entry=2.00, atr=1.50, zones=[zone(1.20), zone(9.00)])
    assert r.reason == "stop_non_positive"
    assert r.detail == "stop -0.30 from support 1.20-1.20, 2 tests, score 50, low 1.20 - 1xATR 1.50"


def test_rejects_non_positive_disaster():
    r = plan(entry=2.00, atr=0.90, zones=[zone(1.20), zone(9.00)])
    assert r.reason == "disaster_non_positive"
    assert r.detail == "disaster -0.60 from stop 0.30 - ATR 0.90"


def test_rejects_zero_size():
    # 1% of 100 = 1.00 of risk, 3.40 per share
    r = plan(account=100)
    assert r.reason == "size_zero"
    assert r.detail.startswith("size 0 (risk):")


def test_size_zero_when_one_share_exceeds_the_position_limit():
    assert plan(account=150).reason == "size_zero"


@pytest.mark.parametrize(
    "over",
    [
        pytest.param({"entry": 0}, id="entry_zero"),
        pytest.param({"entry": -50.0}, id="entry_negative"),
        pytest.param({"entry": math.nan}, id="entry_nan"),
        pytest.param({"entry": math.inf}, id="entry_inf"),
        pytest.param({"entry": None}, id="entry_none"),
        pytest.param({"entry": "50"}, id="entry_str"),
        pytest.param({"entry": True}, id="entry_bool"),
        pytest.param({"account": 0}, id="account_zero"),
        pytest.param({"account": -1}, id="account_negative"),
        pytest.param({"account": math.nan}, id="account_nan"),
        pytest.param({"risk_pct": 0}, id="risk_zero"),
        pytest.param({"risk_pct": -1.0}, id="risk_negative"),
        pytest.param({"risk_pct": 10.01}, id="risk_above_10"),
        pytest.param({"risk_pct": math.inf}, id="risk_inf"),
    ],
)
def test_invalid_arguments_raise(over):
    with pytest.raises(ValueError):
        plan(**over)


def test_risk_pct_upper_bound_inclusive():
    assert isinstance(plan(risk_pct=10), PlanMath)


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param({"high": 48.10}, id="missing_low"),
        pytest.param({"low": 47.80}, id="missing_high"),
        pytest.param({"low": math.nan, "high": 48.10}, id="nan_low"),
        pytest.param({"low": 47.80, "high": math.inf}, id="inf_high"),
        pytest.param({"low": 0.0, "high": 48.10}, id="zero_low"),
        pytest.param({"low": -1.0, "high": 48.10}, id="negative_low"),
        pytest.param({"low": 48.10, "high": 47.80}, id="high_below_low"),
        pytest.param({"low": None, "high": 48.10}, id="null_low"),
        pytest.param([47.80, 48.10], id="not_a_mapping"),
    ],
)
def test_malformed_zone_raises(bad):
    with pytest.raises(ValueError, match="zone 1"):
        plan(zones=[zone(45.00), bad, zone(56.90)])


def test_rejection_order():
    # no ATR and no support: ATR is checked first
    assert plan(atr=None, zones=[zone(56.90)]).reason == "no_atr"
    # no support and no target (empty zones): support first
    assert plan(zones=[]).reason == "no_support"
    # no support zone but a valid EMA20: a plan; an EMA20 above the entry: no_support
    assert isinstance(plan(zones=[zone(56.90)], ema20=49.00), PlanMath)
    assert plan(zones=[zone(56.90)], ema20=51.00).reason == "no_support"
    # bad risk_pct and no ATR: arguments first, as a ValueError
    with pytest.raises(ValueError):
        plan(atr=None, risk_pct=0)
    # malformed zone and no ATR: arguments first
    with pytest.raises(ValueError):
        plan(atr=None, zones=[{"low": 1.0}])
    # bad ema20 and no ATR: arguments first
    with pytest.raises(ValueError):
        plan(atr=None, ema20="49")
    # stop <= 0 and no target: stop first
    assert plan(entry=2.00, atr=1.50, zones=[zone(1.20)]).reason == "stop_non_positive"
    # no target and size zero: targets first
    assert plan(zones=[zone(47.80)], account=100).reason == "no_target"
    # every target beyond the cap and size zero: no_target first
    assert plan(zones=[zone(47.80), zone(60.00)], account=100).reason == "no_target"
    # a ceiling under 1.5R and size zero: ceiling first; and ceiling before low_r
    capped = [zone(49.00), zone(52.80, 53.10, held=4, broke=0), zone(56.90)]
    assert plan(zones=capped, account=100).reason == "ceiling"
    assert plan(zones=capped).reason == "ceiling"
    # low R and size zero: T1 first
    assert plan(zones=ZONES_A[:3], account=100).reason == "low_r"


def test_deterministic_and_does_not_mutate_inputs():
    zones = [zone(56.90), zone(47.80), zone(53.90)]
    before = copy.deepcopy(zones)
    first = plan(zones=zones)
    second = plan(zones=zones)
    assert first == second
    assert zones == before
    assert isinstance(first, PlanMath)
    with pytest.raises(AttributeError):
        first.stop = 1.0  # frozen


def test_zones_accept_a_tuple():
    assert plan(zones=tuple(ZONES_A)).size_shares == 73


def test_plan_math_version_is_three():
    """Rows before 4.8a are version 1 (NULL / absent), 4.8a's are 2. Bump on
    any rule change."""
    assert plan_math.PLAN_MATH_VERSION == 3
    assert plan_math.TARGET_MAX_ATR == Decimal("8")
    assert plan_math.FAR_SUPPORT_ATR == Decimal("2")
    assert plan_math.MAX_DISASTER_LOSS_PCT == Decimal("2.5")
    assert (plan_math.CEILING_MIN_HELD, plan_math.CEILING_HELD_PER_BROKE) == (3, 3)


# ── Plan math v3 (4.8a-de): the stop preference, the extension, the ceiling ─

NEAR = [zone(49.00, 49.10, held=1, broke=0), zone(56.90, 57.30)]   # stop 47.80, risk 2.20 = 1.83 ATR


def test_stop_prefers_most_held_support_within_two_atr():
    # P 49.00-49.10 held 1 → 47.80 (1.83 ATR); Q 48.80-48.90 held 4 → 47.60 (2.00 ATR): Q wins
    p = plan(zones=[zone(49.00, 49.10, held=1, broke=0), zone(48.80, 48.90, held=4, broke=0), zone(56.90)])
    assert p.stop == 47.60
    assert p.stop_basis == ("stop 47.60: support 48.80-48.90, held 4, broke 0, score 50, low 48.80 - 1xATR 1.20; "
                            "most held of 2 support zones within 2 ATR")
    assert p.extended is False


def test_stop_ignores_most_held_support_beyond_two_atr():
    # Q at 48.40-48.50 → 47.20, risk 2.80 = 2.33 ATR: out, whatever it held
    p = plan(zones=[zone(49.00, 49.10, held=1, broke=0), zone(48.40, 48.50, held=9, broke=0), zone(56.90)])
    assert p.stop == 47.80 and "most held" not in p.stop_basis
    assert p.stop_basis.startswith("stop 47.80: support 49.00-49.10, held 1, broke 0")


def test_stop_tie_on_held_takes_highest_low():
    p = plan(zones=[zone(49.00, held=2, broke=0), zone(48.90, held=2, broke=0), zone(56.90)])
    assert p.stop == 47.80
    # no history at all → v2's rule, the highest low
    p = plan(zones=[zone(49.00), zone(48.90), zone(56.90)])
    assert p.stop == 47.80 and "most held" in p.stop_basis


def test_stop_two_atr_boundary_inclusive():
    # low 48.80 → stop 47.60, risk 2.40 = 2.00 ATR: eligible; 48.79 → 2.41 = 2.01 ATR: far
    assert plan(zones=[zone(48.80, held=1), zone(56.90)]).extended is False
    p = plan(zones=[zone(48.79, held=1), zone(56.90)])
    assert p.stop == 47.59 and p.extended is True and p.entry_for_max_risk == 49.99


def test_swing_low_stop_wins_when_nearer():
    # far branch: EMA20 49.00 → 47.80, swing low 49.20 → 48.00: the higher wins
    p = plan(zones=FAR, ema20=49.00, swing_low=49.20, swing_low_date="2026-09-15")
    assert p.stop == 48.00 and p.stop_basis.startswith("stop 48.00: swing low (2026-09-15) 49.20 - 1xATR 1.20")
    # and the swing low is never consulted while a support zone sits within 2 ATR
    p = plan(zones=NEAR, swing_low=49.50)
    assert p.stop == 47.80 and "swing" not in p.stop_basis


def test_extended_plan_carries_entry_for_max_risk():
    """OUST 35ddee39 (bars of 2026-09-22): EMA20 36.602 → 36.60 - 2.02 =
    34.58 (v2 printed 34.57), risk 5.67 = 2.80 ATR → extended, entry for 2 ATR
    risk 34.58 + 4.04 = 38.62 (the Next block's 38.61 was v2's stop + 4.04)."""
    zones = [zone(32.970001220703125, 33.08000183105469), zone(26.290000915527344, 26.299999237060547),
             zone(40.4900016784668, 40.599998474121094), zone(48.25, 48.380001068115234),
             zone(49.38999938964844, 49.5)]
    p = plan(entry=40.25, atr=2.0239, zones=zones, ema20=36.602)
    assert (p.stop, p.disaster_line) == (34.58, 32.56)
    assert p.extended is True and p.entry_for_max_risk == 38.62
    assert p.stop_basis == ("stop 34.58: EMA20 36.60 - 1xATR 2.02; nearest support 32.97-33.08 gives risk 9.30 = "
                            "4.59 ATR (> 2 ATR); extended: risk 5.67 = 2.80 ATR (> 2 ATR), entry for 2 ATR risk "
                            "38.62 = stop 34.58 + 2xATR 2.02")
    assert prices(p.targets) == [49.38] and rs(p.targets) == [1.61]
    assert prices(p.overhead) == [40.49, 48.25]
    # the flag never rejects and never touches the size rule
    assert p.size_shares >= 1


def test_extended_never_when_risk_within_two_atr():
    p = plan(zones=NEAR)
    assert p.extended is False and p.entry_for_max_risk is None and "extended" not in p.stop_basis


def test_extension_boundary_two_atr_inclusive():
    # no support: EMA20 48.80 → 47.60, risk 2.40 = 2.00 ATR: not extended; 48.79 → 2.01: extended
    p = plan(zones=[zone(56.90)], ema20=48.80)
    assert p.stop == 47.60 and p.extended is False
    p = plan(zones=[zone(56.90)], ema20=48.79)
    assert p.stop == 47.59 and p.extended is True and p.entry_for_max_risk == 49.99


def test_stop_built_on_cent_floored_components():
    # v2 floored the exact difference: 36.602 - 2.0239 = 34.5781 → 34.57;
    # v3 floors the components: 36.60 - 2.02 = 34.58
    p = plan(entry=40.25, atr=2.0239, zones=[zone(49.39, 49.5)], ema20=36.602)
    assert p.stop == 34.58 and p.disaster_line == 32.56
    p = plan(atr=1.2047, zones=[zone(47.803, 48.10), zone(56.90)])
    assert p.stop == 46.60 and p.disaster_line == 45.40
    p = plan(zones=[zone(56.90)], swing_low=49.209, swing_low_date="2026-09-15")
    assert p.stop == 48.00 and "swing low (2026-09-15) 49.20 - 1xATR 1.20" in p.stop_basis


def test_basis_arithmetic_reconciles():
    """Every `a - 1xATR b` in a stop basis equals the stop, and every
    `stop a + 2xATR b` equals entryForMaxRisk: floored components, one
    rounding (4.8a-de change 8)."""
    cases = [plan(), plan(zones=NEAR), plan(zones=FAR, ema20=49.00), plan(zones=FAR, ema20=50.5, swing_low=48.50),
             plan(atr=1.2047, zones=[zone(47.803, 48.10), zone(56.90)]),
             plan(entry=40.25, atr=2.0239, zones=[zone(32.97, 33.08), zone(49.39, 49.5)], ema20=36.602),
             plan(entry=40.25, atr=2.0239, zones=[zone(32.97, 33.08), zone(49.39, 49.5)], ema20=36.602, swing_low=37.111)]
    for p in cases:
        assert isinstance(p, PlanMath), p
        m = re.search(r"(\d+\.\d\d) - 1xATR (\d+\.\d\d)", p.stop_basis)
        assert m and Decimal(m.group(1)) - Decimal(m.group(2)) == Decimal(str(p.stop)), p.stop_basis
        m = re.search(r"stop (\d+\.\d\d) \+ 2xATR (\d+\.\d\d)", p.stop_basis)
        if p.extended:
            assert m and Decimal(m.group(1)) + 2 * Decimal(m.group(2)) == Decimal(str(p.entry_for_max_risk))
        else:
            assert m is None


CEIL_SUP = zone(49.00, 49.10, touches=2, held=1, broke=0, lastTouch="2026-09-01")   # stop 47.80, risk 2.20
CEIL_A = zone(52.00, 52.20, touches=3, held=1, broke=2, lastTouch="2026-08-01")     # 0.91R, looked through
CEIL_B = zone(53.50, 53.80, touches=5, held=4, broke=0, lastTouch="2026-08-12")     # 1.59R, ceiling
CEIL_C = zone(56.00, 56.30, touches=2, held=2, broke=0, lastTouch="2026-07-01")     # 2.73R, beyond the ceiling


def test_ceiling_caps_t1():
    """Spec 4.8a-de decision 5: T1 is the ceiling's low, C above it is no target."""
    p = plan(zones=[CEIL_SUP, CEIL_A, CEIL_B, CEIL_C])
    assert prices(p.overhead) == [52.00] and rs(p.overhead) == [0.91]
    assert prices(p.targets) == [53.50] and rs(p.targets) == [1.59]
    assert p.targets[0].basis == ("T1 53.50: resistance 53.50-53.80, touches 5, held 4, broke 0, last 2026-08-12, "
                                  "score 50, ceiling")
    # without the history C is T2 (v2 behaviour)
    p = plan(zones=[CEIL_SUP, zone(52.00, 52.20), zone(53.50, 53.80), zone(56.00, 56.30)])
    assert prices(p.targets) == [53.50, 56.00]


def test_ceiling_under_min_r_rejects_ceiling():
    b = zone(52.80, 53.10, touches=5, held=4, broke=0, lastTouch="2026-08-12")
    r = plan(zones=[CEIL_SUP, CEIL_A, b, CEIL_C])
    assert r == PlanRejected("ceiling", "resistance 52.80-53.10, touches 5, held 4, broke 0, last 2026-08-12, "
                                        "score 50 caps the trade at 1.27R")


def test_ceiling_ratio_rule():
    # held 3 / broke 1 → 3 >= 3: a ceiling; held 3 / broke 2 → 3 < 6: not
    yes = zone(52.80, 53.10, held=3, broke=1)
    no = zone(52.80, 53.10, held=3, broke=2)
    assert plan(zones=[CEIL_SUP, yes, CEIL_C]).reason == "ceiling"
    p = plan(zones=[CEIL_SUP, no, CEIL_C])
    assert prices(p.overhead) == [52.80] and prices(p.targets) == [56.00]
    # held 2 / broke 0: under the minimum
    assert prices(plan(zones=[CEIL_SUP, zone(52.80, 53.10, held=2, broke=0), CEIL_C]).targets) == [56.00]


def test_broken_zone_is_looked_through():
    # broke >= held is never a ceiling, however often it held
    weak = zone(52.80, 53.10, held=5, broke=5)
    p = plan(zones=[CEIL_SUP, weak, CEIL_C])
    assert prices(p.overhead) == [52.80] and prices(p.targets) == [56.00]


def test_zone_without_history_is_never_a_ceiling():
    p = plan(zones=[CEIL_SUP, zone(52.80, 53.10, tests=9), CEIL_C])
    assert prices(p.targets) == [56.00] and "ceiling" not in p.targets[0].basis
    # and a zone with held alone (no broke key) can be one
    assert plan(zones=[CEIL_SUP, zone(52.80, 53.10, held=3), CEIL_C]).reason == "ceiling"


def test_capped_zone_is_never_a_ceiling():
    # a well-held zone beyond 8 ATR (60.00 = 8.33 ATR) is dropped before the walk
    p = plan(zones=[CEIL_SUP, zone(53.50, 53.80), zone(60.00, 60.20, held=5, broke=0)])
    assert prices(p.targets) == [53.50] and rs(p.targets) == [1.59]
    # inside the cap it is the ceiling and T2
    p = plan(zones=[CEIL_SUP, zone(53.50, 53.80), zone(58.00, 58.20, held=5, broke=0)])
    assert prices(p.targets) == [53.50, 58.00] and p.targets[1].basis.endswith("ceiling")


def test_straddling_ceiling_rejects():
    # a well-held resistance zone the entry sits inside: its high is overhead
    # and the ceiling, so nothing is eligible above it
    r = plan(zones=[CEIL_SUP, zone(49.70, 50.40, side="resistance", held=4, broke=0), CEIL_C])
    assert r.reason == "ceiling" and r.detail.endswith("caps the trade at 0.18R")


def test_size_basis_has_no_dollar_figure():
    """4.8a-de change 3: bound names only. The only digits are the two
    parameters (25 %, 2.5 %); no count, no budget, no percent of the account."""
    for p in (plan(), plan(zones=NEAR), plan(atr=2.0, risk_pct=3.0),
              plan(atr=0.05, zones=[zone(49.90, 49.95), zone(50.28, 50.30)], account=10_000, risk_pct=2.0)):
        assert p.size_basis.startswith("size: ") and p.size_basis.endswith(" not binding)")
        assert set(re.findall(r"\d+(?:\.\d+)?", p.size_basis)) <= {"25", "2.5"}, p.size_basis
        assert str(p.size_shares) not in p.size_basis.replace("25", "").replace("2.5", "")


# ── Helpers ───────────────────────────────────────────────────────────────


def test_to_cents_floors():
    assert to_cents(Decimal("46.599")) == Decimal("46.59")
    assert to_cents(Decimal("46.60")) == Decimal("46.60")
    assert to_cents(Decimal("-0.301")) == Decimal("-0.31")


def test_r_multiple_rounds_half_up():
    assert r_multiple(Decimal("55.10"), Decimal("50"), Decimal("46.60")) == Decimal("1.50")
    assert r_multiple(Decimal("51.0025"), Decimal("50"), Decimal("49.50")) == Decimal("2.01")


def test_plan_math_is_pure():
    """Standard library only: a later edit cannot quietly add I/O."""
    src = Path(__file__).resolve().parent.parent / "grading" / "plan_math.py"
    roots = set()
    for node in ast.walk(ast.parse(src.read_text())):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            roots.add((node.module or "").split(".")[0])
    assert roots <= {"math", "collections", "dataclasses", "decimal", "typing"}, roots


# ── The rerun script (decision 6) ─────────────────────────────────────────


def test_rerun_row_error_is_reported_not_raised():
    from scripts import plan_math_rerun as rerun
    bad = {"verdictId": "abc", "ticker": "X", "entry": 50.0}      # no indicators
    err_row = rerun.rerun_row(bad, prev=None, account=100_000, risk_pct=1.0)
    assert err_row["ticker"] == "X" and err_row["error"].startswith("indicators")
    good = {"verdictId": "abc", "ticker": "Y", "entry": 50.0,
            "indicators": {"atr14": 1.2, "ema20": 47.5, "zones": {"support": ZONES_A[:2], "resistance": ZONES_A[2:]}}}
    row = rerun.rerun_row(good, prev=None, account=100_000, risk_pct=1.0)
    assert row["error"] is None and len(row["runs"]) == 1
    assert [z["side"] for z in rerun._zones(good["indicators"])] == ["support"] * 2 + ["resistance"] * 2
    run = row["runs"][0]
    assert (run["inputs"], run["version"]) == ("stored", "v3")
    assert run["valid"] and run["t1"] == "56.90 (2.03R)" and run["overhead"] == 1
    assert run["stop"] == 46.60 and run["riskAtr"] == 2.83 and run["stopRule"] == "support"
    assert run["extended"] == "yes, wait for <= 49.00" and run["ceiling"] is None and run["swingLow"] is None
    text = rerun.render([row, err_row])
    assert "| Y | 50.00 | stored | v3 | support | 46.60 | 2.83 | 1 | - | 56.90 (2.03R) | yes, wait for <= 49.00 | - | yes | - |" in text
    assert "error: indicators" in text and text.rstrip().endswith("valid plans: v3 on stored 1 / 1")
    for secret in ("size", "shares", "budget", "100000", "lossAtDisaster"):
        assert secret not in text, secret
    # a bad entry is an error cell too, never a raise
    assert rerun.rerun_row({**good, "entry": "x"}, prev=None, account=1, risk_pct=1.0)["error"].startswith("ValueError")


def test_rerun_reports_extension_and_ceiling_columns():
    """4.8a-de decision 8: three rows per verdict — the previous module on
    the stored inputs, the previous module on the fresh inputs with the
    swing low withheld, the current module on the fresh inputs — and the
    nearest stored vs fresh zones per ticker."""
    from scripts import plan_math_rerun as rerun
    prev = rerun.load_module(str(Path(__file__).resolve().parent.parent / "grading" / "plan_math.py"))
    stored = {"atr14": 1.2, "ema20": 47.5, "zones": {"support": ZONES_A[:2], "resistance": ZONES_A[2:]}}
    fresh = {"atr14": 1.2, "ema20": 47.5, "lastSwingLow": {"price": 49.20, "date": "2026-09-15"},
             "zones": {"support": [zone(45.00, 45.40, touches=1, held=1, broke=0),
                                   zone(47.80, 48.10, touches=3, held=2, broke=1)],
                       "resistance": [zone(53.90, 54.30, touches=5, held=4, broke=0, lastTouch="2026-08-12"),
                                      zone(56.90, 57.30, touches=2, held=2, broke=0)]}}
    row = rerun.rerun_row({"verdictId": "abc", "ticker": "Y", "entry": 50.0, "indicators": stored, "fresh": fresh},
                          prev=prev, account=100_000, risk_pct=1.0)
    assert row["error"] is None
    assert [(r["inputs"], r["version"]) for r in row["runs"]] == [("stored", "v3"), ("fresh, no swing", "v3"), ("fresh", "v3")]
    stored_run, drift_run, fresh_run = row["runs"]
    # stored: the far zone stop, extended
    assert stored_run["stop"] == 46.60 and stored_run["extended"].startswith("yes") and stored_run["swingLow"] is None
    # fresh without the swing low: the same stop (zone drift alone), but the
    # 53.90 zone is now a ceiling under 1.5R → rejected
    assert drift_run["swingLow"] is None and drift_run["valid"] is False and drift_run["reason"].startswith("ceiling:")
    # fresh with the swing low: 49.20 - 1.20 = 48.00 wins (risk 2.00 = 1.67 ATR, not extended);
    # 53.90 pays 1.95R at the ceiling → T1 = the ceiling, nothing above it
    assert fresh_run["stop"] == 48.00 and fresh_run["stopRule"] == "swing low" and fresh_run["extended"] == "no"
    assert fresh_run["swingLow"] == "49.20 (2026-09-15)" and fresh_run["riskAtr"] == 1.67
    assert fresh_run["t1"] == "53.90 (1.95R)" and fresh_run["ceiling"] == "53.90" and fresh_run["valid"]
    assert row["zones"] == {"storedSupport": "47.80-48.10", "freshSupport": "47.80-48.10 (touches 3, held 2, broke 1)",
                            "storedResistance": "53.90-54.30",
                            "freshResistance": "53.90-54.30 (touches 5, held 4, broke 0)"}
    # the ceiling column names the ceiling target itself, wherever it sits:
    # 53.90 (held 1) is T1 at 1.95R and 56.90 (held 4, broke 0) is T2 and the ceiling
    later = {**fresh, "zones": {**fresh["zones"], "resistance": [
        zone(53.90, 54.30, touches=3, held=1, broke=1), zone(56.90, 57.30, touches=5, held=4, broke=0)]}}
    t2 = rerun.rerun_row({"verdictId": "abc", "ticker": "Y", "entry": 50.0, "indicators": stored, "fresh": later},
                         prev=None, account=100_000, risk_pct=1.0)["runs"][-1]
    assert t2["t1"] == "53.90 (1.95R)" and t2["ceiling"] == "56.90"
    text = rerun.render([row])
    assert "| Y | 50.00 | fresh | v3 | swing low | 48.00 | 1.67 | 0 | 53.90 | 53.90 (1.95R) | no | 49.20 (2026-09-15) | yes | - |" in text
    assert "valid plans: v3 on stored 1 / 1; v3 on fresh, no swing 0 / 1; v3 on fresh 1 / 1" in text
    assert "| storedSupport | freshSupport | storedResistance | freshResistance |" in text
    for secret in ("size", "shares", "budget", "100000", "lossAtDisaster"):
        assert secret not in text, secret
