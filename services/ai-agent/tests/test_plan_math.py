"""
Part 4.3 / 4.8a — plan math v2. Every number below is hand-checked in spec
4.8a's worked examples; they are asserted exactly, never approximately.

Fixture: entry 50.00, ATR 1.20, account 25,000, risk 1 %.
  stop zone 47.80-48.10 → stop 46.60 (risk 3.40 = 2.83 ATR: far, but no
  alternative is given, so the zone stop stays), disaster 45.40.
  Target cap 6 × 1.20 = 7.20 above entry → 57.20.
  53.90 pays 3.90 / 3.40 = 1.15R → overhead; 56.90 pays 6.90 / 3.40 = 2.03R → T1.
  Size: risk 250 / 3.40 = 73, max position 6,250 / 50 = 125, cash 500,
  disaster loss 625 / 4.60 = 135 → 73 (risk). Loss at disaster
  73 × 4.60 / 25,000 = 1.34 %.
"""

import ast
import copy
import math
from decimal import Decimal
from pathlib import Path

import pytest

from grading import plan_math
from grading.plan_math import PlanMath, PlanRejected, Target, compute_plan, r_multiple, to_cents


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
    assert p.size_basis == ("risk: 73 shares (risk 73, max position 125, cash cap 500, disaster loss 135; "
                            "risk 1.0% of 25000.00 = 250.00, 3.40/share; "
                            "loss at disaster 1.34% of account, cap 2.5%)")
    assert p.stop_basis == "stop 46.60: support 47.80-48.10, 2 tests, score 50, low 47.80 - 1xATR 1.20"
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
    assert "risk 1333, max position 50, cash cap 200, disaster loss 1250" in p.size_basis
    assert p.loss_at_disaster_pct == 0.10


def test_example_d_rounding():
    p = plan(atr=1.2047, zones=[zone(47.803, 48.10), zone(56.90, 57.00)])
    assert p.stop == 46.59
    assert p.disaster_line == 45.38
    assert rs(p.targets) == [2.02]   # 6.90 / 3.41


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
    assert "disaster loss 100" in p.size_basis and "loss at disaster 2.48% of account" in p.size_basis


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
    assert r == PlanRejected("no_target", "every resistance above entry 50.0 is beyond 6xATR 7.20: 60.00, 70.00")


def test_target_cap_boundary_inclusive():
    p = plan(zones=[zone(47.80), zone(57.20)])
    assert prices(p.targets) == [57.20] and rs(p.targets) == [2.12]
    assert plan(zones=[zone(47.80), zone(57.21)]).reason == "no_target"


# ── The far-support stop (decision 3) ─────────────────────────────────────

FAR = [zone(48.20), zone(56.00)]   # stop zone 48.20 → 47.00, risk 3.00 = 2.50 ATR; T 56.00 within the 7.20 cap


def test_far_support_takes_nearer_of_swing_low_and_ema20():
    p = plan(zones=FAR, ema20=49.00, swing_low=48.50, swing_low_date="2026-09-15")
    assert p.stop == 47.80
    assert p.stop_basis == ("stop 47.80: EMA20 49.00 - 1xATR 1.20; "
                            "support 48.20-48.20 gives risk 3.00 = 2.50 ATR (> 2 ATR)")
    assert rs(p.targets) == [2.73]   # 6.00 / 2.20
    p = plan(zones=FAR, ema20=49.00, swing_low=49.20, swing_low_date="2026-09-15")
    assert p.stop == 48.00
    assert p.stop_basis.startswith("stop 48.00: swing low (2026-09-15) 49.20 - 1xATR 1.20; support 48.20-48.20")


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
    assert p.stop_basis == "stop 46.60: support 47.80-48.10, 4 tests, score 65, low 47.80 - 1xATR 1.20"
    # a zone with low and high only still has a basis
    p = plan(zones=[{"low": 47.80, "high": 48.10}, {"low": 56.90, "high": 57.30}])
    assert p.targets[0].basis == "T1 56.90: resistance 56.90-57.30"
    assert p.stop_basis == "stop 46.60: support 47.80-48.10, low 47.80 - 1xATR 1.20"


def test_basis_text_is_in_cents():
    p = plan(atr=1.2047, zones=[zone(47.803, 48.10), zone(56.90, 57.00)])
    assert p.stop_basis == "stop 46.59: support 47.80-48.10, 2 tests, score 50, low 47.80 - 1xATR 1.20"
    assert p.size_basis.endswith("3.41/share; loss at disaster 1.35% of account, cap 2.5%)")


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
    assert r.detail == "disaster -0.60 from stop 0.30 - ATR 0.9"


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


def test_plan_math_version_is_two():
    """Rows before 4.8a are version 1 (NULL / absent). Bump on any rule change."""
    assert plan_math.PLAN_MATH_VERSION == 2
    assert plan_math.TARGET_MAX_ATR == Decimal("6")
    assert plan_math.FAR_SUPPORT_ATR == Decimal("2")
    assert plan_math.MAX_DISASTER_LOSS_PCT == Decimal("2.5")


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
    err_row = rerun.rerun_row(bad, v1=None, account=100_000, risk_pct=1.0)
    assert err_row["ticker"] == "X" and err_row["error"].startswith("indicators")
    good = {"verdictId": "abc", "ticker": "Y", "entry": 50.0,
            "indicators": {"atr14": 1.2, "ema20": 47.5, "zones": {"support": ZONES_A[:2], "resistance": ZONES_A[2:]}}}
    row = rerun.rerun_row(good, v1=None, account=100_000, risk_pct=1.0)
    assert row["error"] is None
    assert row["v2"]["valid"] and row["v2"]["t1"] == "56.90 (2.03R)" and row["v2"]["overhead"] == 1
    assert row["v2"]["stop"] == 46.60 and row["v2"]["riskAtr"] == 2.83 and row["v2"]["stopRule"] == "support"
    text = rerun.render([row, err_row])
    assert "| Y | 50.00 | v2 | support | 46.60 | 2.83 | 1 | 56.90 (2.03R) | yes | - |" in text
    assert "error: indicators" in text and text.rstrip().endswith("valid plans: v1 0 / 1, v2 1 / 1")
    for secret in ("size", "shares", "budget", "100000", "lossAtDisaster"):
        assert secret not in text, secret
    # the pre-part module loads from a path and answers the same row (v1: 53.90 was T1 at 1.15R)
    v1_src = Path(__file__).resolve().parent.parent / "grading" / "plan_math.py"
    same = rerun.load_v1(str(v1_src))
    row = rerun.rerun_row(good, v1=same, account=100_000, risk_pct=1.0)
    assert row["v1"] == row["v2"]
