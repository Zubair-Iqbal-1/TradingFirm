"""Part 4.5 — journal scoring math (journal/scoring.py), synthetic bars.

Every expected number is worked by hand in the comment beside it."""

import ast
from decimal import Decimal
from pathlib import Path

import pytest

from journal import scoring

PLAN = {
    "entry": 100.0, "stop": 95.0, "stopBasis": "support 97-98 minus 1 ATR",
    "disasterLine": 92.0, "invalidation": "daily close below the 20 EMA",
    "targets": [{"price": 110.0, "r": 2.0}], "sizeShares": 10,
    "sizeBasis": "1% of account over the stop distance", "horizonDays": 10,
}


def bar(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


@pytest.fixture
def plan():
    return scoring.parse_plan(PLAN)


def test_return_mae_mfe_hand_computed(plan):
    bars = [bar(100.5, 101, 99, 100),            # an ask-session hour
            bar(100, 104.37, 97.25, 103.1234)]   # session N
    out = scoring.score(100, plan, bars)
    assert out["return_pct"] == Decimal("3.123")   # 3.1234 %
    assert out["mae_pct"] == Decimal("-2.750")     # (97.25 − 100) / 100
    assert out["mfe_pct"] == Decimal("4.370")      # (104.37 − 100) / 100
    assert out["stop_hit"] is False and out["target_hit"] is False
    assert out["first_hit"] is None
    assert out["r_multiple"] == Decimal("0.625")   # 3.1234 / 5 = 0.62468


def test_metrics_round_half_up_to_3dp():
    # 0.0025 % rounds half-up to 0.003 (banker's rounding would give 0.002).
    out = scoring.score(100, None, [bar(100, 100.0025, 100, 100.0025)])
    assert out["return_pct"] == Decimal("0.003")
    # Decimal from the text, not the float: 47.80 − 1.20 style drift never shows.
    out = scoring.score(47.8, None, [bar(47.8, 47.8, 46.6, 46.6)])
    assert out["return_pct"] == Decimal("-2.510")  # −1.2 / 47.8 = −2.5104…


def test_mae_is_signed_not_clamped():
    """A gap up that never trades below the entry: MAE is positive."""
    out = scoring.score(100, None, [bar(103, 106, 102, 105)])
    assert out["mae_pct"] == Decimal("2.000")
    assert out["mfe_pct"] == Decimal("6.000")


def test_no_plan_scores_returns_only():
    out = scoring.score(100, scoring.parse_plan(None), [bar(100, 120, 80, 101)])
    assert out["return_pct"] == Decimal("1.000")
    assert [out[k] for k in ("stop_hit", "target_hit", "first_hit", "r_multiple")] == [None] * 4


def test_unreadable_plan_is_refused():
    with pytest.raises(scoring.UnreadablePlan):
        scoring.parse_plan({"stop": "not a plan"})
    broken = {**PLAN, "stop": 101.0}      # stop above entry: levels do not ascend
    with pytest.raises(scoring.UnreadablePlan):
        scoring.parse_plan(broken)


def test_same_bar_counts_stop_first(plan):
    out = scoring.score(100, plan, [bar(100, 111, 94, 108)])
    assert out["stop_hit"] is True and out["target_hit"] is True
    assert out["first_hit"] == scoring.SAME_BAR
    assert out["r_multiple"] == Decimal("-1.000")   # exit at the stop, 95


def test_gap_through_stop_exits_at_open(plan):
    out = scoring.score(100, plan, [bar(100, 101, 98, 99), bar(93, 94, 92, 93.5)])
    assert out["first_hit"] == scoring.STOP
    assert out["r_multiple"] == Decimal("-1.400")   # (93 − 100) / 5
    # Opening exactly on the stop exits there too.
    out = scoring.score(100, plan, [bar(95, 96, 94, 95)])
    assert out["r_multiple"] == Decimal("-1.000")


def test_stop_honoured_after_target(plan):
    out = scoring.score(100, plan, [bar(100, 111, 99, 109), bar(108, 108, 94, 96)])
    assert out["first_hit"] == scoring.TARGET
    assert out["stop_hit"] is True and out["target_hit"] is True
    assert out["r_multiple"] == Decimal("-1.000")   # the target is not an exit


def test_no_touch_r_from_horizon_close(plan):
    out = scoring.score(100, plan, [bar(100, 102, 97, 101), bar(101, 104, 99, 102.5)])
    assert out["first_hit"] is None
    assert out["r_multiple"] == Decimal("0.500")    # 2.5 / 5


def test_first_hit_walks_hourly_then_daily(plan):
    hour_stop, day_target = bar(100, 100.5, 94.9, 96), bar(97, 111, 96, 109)
    assert scoring.score(100, plan, [hour_stop, day_target])["first_hit"] == scoring.STOP
    hour_target, day_stop = bar(100, 110.2, 99, 109), bar(108, 108, 94, 96)
    assert scoring.score(100, plan, [hour_target, day_stop])["first_hit"] == scoring.TARGET


def test_price_scale_break_leaves_verdict_unscored():
    """A 4:1 split inside the window: entry 200 against bars at 50."""
    with pytest.raises(scoring.ScaleBreak):
        scoring.score(200, None, [bar(50, 51, 49, 50.5)])
    # Exactly 30 % away is still a move; past it is a break.
    scoring.score(100, None, [bar(130, 131, 129, 130)])
    scoring.score(100, None, [bar(70, 71, 69, 70)])
    with pytest.raises(scoring.ScaleBreak):
        scoring.score(100, None, [bar(130.01, 131, 129, 130)])


def test_bad_numbers_are_refused():
    with pytest.raises(ValueError):
        scoring.score(100, None, [])
    with pytest.raises(ValueError):
        scoring.score(0, None, [bar(1, 1, 1, 1)])
    with pytest.raises(ValueError):
        scoring.score(100, None, [bar(float("nan"), 1, 1, 1)])
    with pytest.raises(TypeError):
        scoring.score(100, None, [bar(None, 1, 1, 1)])


def test_scoring_is_pure():
    src = Path(__file__).resolve().parent.parent / "journal" / "scoring.py"
    roots = set()
    for node in ast.walk(ast.parse(src.read_text())):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            roots.add((node.module or "").split(".")[0])
    assert roots <= {"decimal", "typing", "models"}, roots
