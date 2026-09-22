"""Part 4.3 — the verdict schema. Pure pydantic; no I/O anywhere."""

import ast
import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from models.verdict import Plan, Target, Verdict


def plan_json(**over):
    """Worked example A from spec 4.3, as camelCase JSON."""
    body = {
        "entry": 50.0,
        "stop": 46.6,
        "stopBasis": "support 47.80-48.10, low 47.80 - 1xATR 1.20",
        "disasterLine": 45.4,
        "invalidation": "daily close below the 20 EMA",
        "targets": [{"price": 56.9, "r": 2.03, "basis": "T1 56.90: resistance 56.90-57.30"}],
        "overhead": [{"price": 53.9, "r": 1.15, "basis": "overhead 53.90: resistance 53.90-54.30"}],
        "lossAtDisasterPct": 1.34,
        "sizeShares": 73,
        "sizeBasis": "risk: 1% of 25000 = 250 / 3.40 per share",
        "earningsInDays": 12,
        "holdThroughEarnings": False,
        "horizonDays": 10,
    }
    body.update(over)
    return body


def verdict_json(**over):
    body = {
        "verdict": "go",
        "confidence": 64,
        "reasoning": "Trend intact above the 20 EMA; pullback into support.",
        "thesis": ["Higher lows since August", "Sector leading SPY", "Volume dries up on pullbacks"],
        "thesisBreakers": ["Daily close below 47.80", "Guidance cut"],
        "plan": plan_json(),
        "riskFlags": ["earnings in 12 days"],
    }
    body.update(over)
    return body


def test_valid_verdict_parses():
    v = Verdict.model_validate(verdict_json())
    assert v.verdict == "go"
    assert v.plan.stop == 46.6
    assert v.plan.targets[0] == Target(price=56.9, r=2.03, basis="T1 56.90: resistance 56.90-57.30")
    assert v.plan.overhead[0].price == 53.9 and v.plan.loss_at_disaster_pct == 1.34
    assert v.thesis_breakers == ["Daily close below 47.80", "Guidance cut"]


def test_camel_case_round_trip():
    body = verdict_json()
    v = Verdict.model_validate(body)
    out = v.model_dump(by_alias=True)
    assert out == body
    assert Verdict.model_validate(out) == v


def test_snake_case_names_accepted_too():
    body = plan_json()
    snake = {
        "entry": body["entry"], "stop": body["stop"], "stop_basis": body["stopBasis"],
        "disaster_line": body["disasterLine"], "invalidation": body["invalidation"],
        "targets": body["targets"], "size_shares": body["sizeShares"],
        "size_basis": body["sizeBasis"], "horizon_days": body["horizonDays"],
    }
    assert Plan.model_validate(snake).disaster_line == 45.4


def test_plan_defaults():
    body = plan_json()
    del body["earningsInDays"], body["holdThroughEarnings"]
    p = Plan.model_validate(body)
    assert p.earnings_in_days is None
    assert p.hold_through_earnings is False
    v = verdict_json(verdict="avoid")
    del v["plan"], v["riskFlags"]
    parsed = Verdict.model_validate(v)
    assert parsed.plan is None
    assert parsed.risk_flags == []


def test_go_requires_plan():
    with pytest.raises(ValidationError, match="go verdict requires a plan"):
        Verdict.model_validate(verdict_json(plan=None))


@pytest.mark.parametrize("verdict", ["wait", "avoid"])
def test_wait_and_avoid_may_omit_plan(verdict):
    assert Verdict.model_validate(verdict_json(verdict=verdict, plan=None)).plan is None


@pytest.mark.parametrize(
    "over",
    [
        pytest.param({"disasterLine": 46.6}, id="disaster_equals_stop"),
        pytest.param({"disasterLine": 47.0}, id="disaster_above_stop"),
        pytest.param({"stop": 50.0}, id="stop_equals_entry"),
        pytest.param({"stop": 51.0}, id="stop_above_entry"),
        pytest.param({"targets": [{"price": 50.0, "r": 1.0}]}, id="target_equals_entry"),
        pytest.param({"targets": [{"price": 49.0, "r": 1.0}]}, id="target_below_entry"),
        pytest.param({"targets": [{"price": 57.5, "r": 2.21}, {"price": 53.9, "r": 1.15}]}, id="targets_descending"),
        pytest.param({"targets": [{"price": 53.9, "r": 1.15}, {"price": 53.9, "r": 1.15}]}, id="targets_duplicate"),
    ],
)
def test_plan_ordering_enforced(over):
    with pytest.raises(ValidationError, match="levels must ascend"):
        Plan.model_validate(plan_json(**over))


@pytest.mark.parametrize(
    "model, body",
    [
        pytest.param(Verdict, verdict_json(extra="x"), id="verdict"),
        pytest.param(Plan, plan_json(entryPrice=50.0), id="plan"),
        pytest.param(Target, {"price": 1.0, "r": 1.0, "label": "t1"}, id="target"),
    ],
)
def test_extra_fields_forbidden(model, body):
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        model.model_validate(body)


@pytest.mark.parametrize(
    "over",
    [
        pytest.param({"verdict": "buy"}, id="bad_enum"),
        pytest.param({"confidence": -1}, id="confidence_below_0"),
        pytest.param({"confidence": 101}, id="confidence_above_100"),
        pytest.param({"thesis": ["a", "b"]}, id="thesis_two"),
        pytest.param({"thesis": ["a", "b", "c", "d"]}, id="thesis_four"),
        pytest.param({"thesis": ["a", "  ", "c"]}, id="thesis_blank_bullet"),
        pytest.param({"thesis": ["a", "b", "x" * 301]}, id="thesis_bullet_too_long"),
        pytest.param({"thesisBreakers": []}, id="no_breakers"),
        pytest.param({"thesisBreakers": ["b"] * 7}, id="seven_breakers"),
        pytest.param({"reasoning": ""}, id="reasoning_empty"),
        pytest.param({"reasoning": " \n "}, id="reasoning_blank"),
        pytest.param({"reasoning": "x" * 2001}, id="reasoning_too_long"),
        pytest.param({"riskFlags": ["f"] * 9}, id="nine_flags"),
        pytest.param({"riskFlags": ["x" * 101]}, id="flag_too_long"),
    ],
)
def test_verdict_field_bounds(over):
    with pytest.raises(ValidationError):
        Verdict.model_validate(verdict_json(**over))


@pytest.mark.parametrize(
    "over",
    [
        pytest.param({"entry": math.nan}, id="nan_entry"),
        pytest.param({"stop": math.inf}, id="inf_stop"),
        pytest.param({"disasterLine": 0.0}, id="disaster_zero"),
        pytest.param({"sizeShares": 0}, id="size_zero"),
        pytest.param({"horizonDays": 0}, id="horizon_zero"),
        pytest.param({"horizonDays": 61}, id="horizon_61"),
        pytest.param({"earningsInDays": -1}, id="earnings_negative"),
        pytest.param({"targets": []}, id="no_targets"),
        pytest.param({"targets": [{"price": p, "r": 1.0} for p in (51, 52, 53, 54)]}, id="four_targets"),
        pytest.param({"targets": [{"price": 53.9, "r": 0.0}]}, id="r_zero"),
        pytest.param({"invalidation": ""}, id="invalidation_empty"),
        pytest.param({"stopBasis": "   "}, id="stop_basis_blank"),
    ],
)
def test_plan_field_bounds(over):
    with pytest.raises(ValidationError):
        Plan.model_validate(plan_json(**over))


def test_boundaries_accepted():
    Verdict.model_validate(verdict_json(confidence=0, riskFlags=[]))
    Verdict.model_validate(verdict_json(confidence=100, thesisBreakers=["b"] * 6, riskFlags=["f"] * 8))
    Plan.model_validate(plan_json(horizonDays=1, earningsInDays=0))
    Plan.model_validate(plan_json(horizonDays=60, overhead=[], targets=[{"price": p, "r": 1.0} for p in (51, 52, 53)]))
    Plan.model_validate(plan_json(overhead=[{"price": p, "r": 0.5} for p in (51, 52, 53)], targets=[{"price": 56.9, "r": 2.03}]))


def test_verdict_model_is_pure():
    """pydantic and typing only: a later edit cannot quietly add I/O."""
    src = Path(__file__).resolve().parent.parent / "models" / "verdict.py"
    roots = set()
    for node in ast.walk(ast.parse(src.read_text())):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            roots.add((node.module or "").split(".")[0])
    assert roots == {"typing", "pydantic"}


@pytest.mark.parametrize(
    "over",
    [
        pytest.param({"overhead": [{"price": 50.0, "r": 0.1}]}, id="overhead_at_entry"),
        pytest.param({"overhead": [{"price": 56.9, "r": 2.03}]}, id="overhead_at_t1"),
        pytest.param({"overhead": [{"price": 57.0, "r": 2.1}]}, id="overhead_above_t1"),
        pytest.param({"overhead": [{"price": 52.0, "r": 0.6}, {"price": 51.0, "r": 0.3}]}, id="overhead_descending"),
        pytest.param({"overhead": [{"price": p, "r": 0.5} for p in (51, 52, 53, 54)]}, id="four_overhead"),
        pytest.param({"lossAtDisasterPct": -0.1}, id="negative_loss"),
        pytest.param({"targets": [{"price": 56.9, "r": 2.03, "basis": ""}]}, id="blank_basis"),
    ],
)
def test_plan_overhead_bounds(over):
    with pytest.raises(ValidationError):
        Plan.model_validate(plan_json(**over))


def test_plan_before_4_8a_still_parses():
    """Rows stored by v1 plan math have no overhead, basis or loss field."""
    body = plan_json()
    del body["overhead"], body["lossAtDisasterPct"]
    body["targets"] = [{"price": 53.9, "r": 1.15}, {"price": 57.5, "r": 2.21}]
    p = Plan.model_validate(body)
    assert p.overhead == [] and p.loss_at_disaster_pct is None and p.targets[0].basis is None
