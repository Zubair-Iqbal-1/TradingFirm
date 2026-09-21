"""
Part 4.3 — plan math. Every number below is hand-checked in spec 4.3's
worked examples; they are asserted exactly, never approximately.
"""

import ast
import copy
import math
from decimal import Decimal
from pathlib import Path

import pytest

from grading import plan_math
from grading.plan_math import PlanMath, PlanRejected, Target, compute_plan, r_multiple, to_cents


def zone(low, high=None):
    """The dossier's zone shape; plan math reads low and high only."""
    high = low if high is None else high
    return {"low": low, "high": high, "price": (low + high) / 2, "score": 50,
            "methods": ["swing_low"], "tests": 2, "recent": True, "volumeNode": False}


ZONES_A = [zone(45.00, 45.40), zone(47.80, 48.10), zone(53.90, 54.30), zone(57.50, 58.00)]


def plan(**over):
    args = {"entry": 50.00, "atr": 1.20, "zones": ZONES_A, "account": 25_000, "risk_pct": 1.0}
    args.update(over)
    return compute_plan(**args)


# ── Worked examples ───────────────────────────────────────────────────────


def test_example_a_accepted():
    p = plan()
    assert isinstance(p, PlanMath)
    assert p.entry == 50.00
    assert p.stop == 46.60
    assert p.disaster_line == 45.40
    assert p.risk_per_share == 3.40
    assert p.targets == (Target(53.90, 1.15), Target(57.50, 2.21))
    assert p.best_r == 2.21
    assert p.risk_budget == 250.00
    assert p.size_shares == 73
    assert p.size_bound == "risk"
    assert p.size_basis.startswith("risk: 73 shares (risk 73, max position 125, cash cap 500;")
    assert p.stop_basis == "support zone 47.8-48.1, low 47.8 - 1xATR 1.2"


def test_example_b_rejected_low_r():
    r = plan(zones=ZONES_A[:3])
    assert r == PlanRejected("low_r", "best R 1.15 < 1.5")


def test_example_c_max_position_binds():
    p = plan(atr=0.05, zones=[zone(49.90, 49.95), zone(58.00, 58.40)], account=10_000, risk_pct=2.0)
    assert p.stop == 49.85
    assert p.disaster_line == 49.80
    assert p.size_shares == 50
    assert p.size_bound == "max position"
    assert "risk 1333, max position 50, cash cap 200" in p.size_basis


def test_example_d_rounding():
    p = plan(atr=1.2047, zones=[zone(47.803, 48.10), zone(57.50, 58.00)])
    assert p.stop == 46.59
    assert p.disaster_line == 45.38


def test_float_inputs_never_reach_arithmetic():
    # the trap this rule exists for: float subtraction then a floor
    assert 47.80 - 1.20 == 46.599999999999994
    assert math.floor((47.80 - 1.20) * 100) / 100 == 46.59
    p = plan(zones=[zone(47.80), zone(57.50)])
    assert p.stop == 46.60
    assert p.disaster_line == 45.40


def test_cash_cap_binds_when_position_limit_raised(monkeypatch):
    monkeypatch.setattr(plan_math, "MAX_POSITION_PCT", Decimal("150"))
    p = plan(atr=0.05, zones=[zone(49.90), zone(58.00)], account=10_000, risk_pct=2.0)
    assert p.size_shares == 200
    assert p.size_bound == "cash cap"


def test_risk_bound_wins_a_tie():
    # risk sizing 125 == max position 125: the first bound in the order names it
    p = plan(atr=0.5, zones=[zone(48.50), zone(60.00)], account=25_000, risk_pct=1.0)
    assert p.risk_per_share == 2.00
    assert p.size_shares == 125
    assert p.size_bound == "risk"


# ── Zone selection ────────────────────────────────────────────────────────


def test_zones_resplit_around_entry_not_last_close():
    # entry 52 sits above the 50.50 zone data-engine may have called resistance
    p = plan(entry=52.00, zones=[zone(57.50), zone(47.80), zone(50.50, 50.90), zone(60.00)])
    assert p.stop == 49.30  # 50.50 - 1.20
    assert [t.price for t in p.targets] == [57.50, 60.00]


def test_zone_straddling_entry_is_support():
    p = plan(zones=[zone(49.60, 50.40), zone(57.50)])
    assert p.stop == 48.40


def test_zone_low_equal_to_entry_is_support():
    p = plan(zones=[zone(50.00, 50.30), zone(57.50)])
    assert p.stop == 48.80


def test_targets_capped_at_three_nearest_ascending():
    p = plan(zones=[zone(47.80), zone(70.00), zone(53.90), zone(60.00), zone(57.50)])
    assert [t.price for t in p.targets] == [53.90, 57.50, 60.00]


def test_target_floored_to_entry_is_dropped():
    p = plan(zones=[zone(47.80), zone(50.004), zone(57.50)])
    assert [t.price for t in p.targets] == [57.50]


def test_targets_flooring_to_same_cent_collapse():
    p = plan(zones=[zone(47.80), zone(57.501), zone(57.509)])
    assert p.targets == (Target(57.50, 2.21),)


def test_only_targets_that_floor_onto_entry_is_no_target():
    assert plan(zones=[zone(47.80), zone(50.004)]).reason == "no_target"


def test_extra_zone_keys_ignored():
    assert plan(zones=[{"low": 47.80, "high": 48.10}, {"low": 57.50, "high": 58.00}]).stop == 46.60


# ── Failure branches (spec 4.3 table) ─────────────────────────────────────


@pytest.mark.parametrize("atr", [None, math.nan, math.inf, -math.inf, 0, 0.0, -1.2, "1.2", True])
def test_rejects_missing_atr(atr):
    r = plan(atr=atr)
    assert isinstance(r, PlanRejected)
    assert r.reason == "no_atr"


def test_rejects_empty_zones():
    assert plan(zones=[]).reason == "no_support"


def test_rejects_no_support_below_entry():
    r = plan(zones=[zone(53.90), zone(57.50)])
    assert r == PlanRejected("no_support", "no zone with low <= entry 50.0 among 2")


def test_rejects_no_resistance_above_entry():
    r = plan(zones=[zone(45.00), zone(47.80)])
    assert r == PlanRejected("no_target", "no resistance above entry 50.0")


def test_rejects_best_r_below_threshold():
    # 1.49: (target - 50) / 3.40 → 55.06 gives 1.4882 → 1.49
    r = plan(zones=[zone(47.80), zone(55.06)])
    assert r == PlanRejected("low_r", "best R 1.49 < 1.5")


def test_accepts_best_r_at_threshold():
    # 55.10 → 5.10 / 3.40 = exactly 1.50
    p = plan(zones=[zone(47.80), zone(55.10)])
    assert p.best_r == 1.50


def test_threshold_uses_rounded_r():
    # 55.083 floors to 55.08 → 5.08 / 3.40 = 1.4941 → 1.49: rejected
    assert plan(zones=[zone(47.80), zone(55.083)]).reason == "low_r"
    # 55.09 → 5.09 / 3.40 = 1.4970… → 1.50: accepted, as printed
    assert plan(zones=[zone(47.80), zone(55.09)]).best_r == 1.50


def test_rejects_non_positive_stop():
    r = plan(entry=2.00, atr=1.50, zones=[zone(1.20), zone(9.00)])
    assert r.reason == "stop_non_positive"
    assert r.detail == "stop -0.30 from support low 1.2 - ATR 1.5"


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
        plan(zones=[zone(45.00), bad, zone(57.50)])


def test_rejection_order():
    # no ATR and no support: ATR is checked first
    assert plan(atr=None, zones=[zone(57.50)]).reason == "no_atr"
    # no support and no target (empty zones): support first
    assert plan(zones=[]).reason == "no_support"
    # bad risk_pct and no ATR: arguments first, as a ValueError
    with pytest.raises(ValueError):
        plan(atr=None, risk_pct=0)
    # malformed zone and no ATR: arguments first
    with pytest.raises(ValueError):
        plan(atr=None, zones=[{"low": 1.0}])
    # stop <= 0 and no target: stop first
    assert plan(entry=2.00, atr=1.50, zones=[zone(1.20)]).reason == "stop_non_positive"
    # no target and size zero: targets first
    assert plan(zones=[zone(47.80)], account=100).reason == "no_target"
    # low R and size zero: best R first
    assert plan(zones=ZONES_A[:3], account=100).reason == "low_r"


def test_deterministic_and_does_not_mutate_inputs():
    zones = [zone(57.50), zone(47.80), zone(53.90)]
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
    assert roots <= {"dataclasses", "decimal", "math", "typing", "collections"}
