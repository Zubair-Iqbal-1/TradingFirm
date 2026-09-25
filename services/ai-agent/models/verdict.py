"""
TradingFirm — Analyst verdict schema (Part 4.3).

The shape of one verdict and its trade plan, as ai-agent returns and stores
it. Pure: pydantic and typing only (`test_verdict_model_is_pure`).

Python fields are snake_case, JSON is camelCase (`by_alias=True` on the way
out, either name on the way in). Unknown fields, NaN and inf are refused.

The price levels in a Plan come from `grading.plan_math`, never from the
model (spec 4.3; 4.4's prompt rule "never invent price levels"). The
LLM-facing structured-output schema is 4.4's, derived from this one. The
length caps below are provisional; 4.4 tunes them against real answers.
"""

from typing import Annotated, Literal, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

REASONING_MAX = 2000
BULLET_MAX = 300
FLAG_MAX = 100
THESIS_LEN = 3
BREAKERS_MAX = 6
RISK_FLAGS_MAX = 8
TARGETS_MAX = 3
HORIZON_DAYS_MAX = 60


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("blank text")
    return value


Bullet = Annotated[str, Field(min_length=1, max_length=BULLET_MAX), AfterValidator(_not_blank)]
Flag = Annotated[str, Field(min_length=1, max_length=FLAG_MAX), AfterValidator(_not_blank)]
Basis = Annotated[str, Field(min_length=1, max_length=BULLET_MAX), AfterValidator(_not_blank)]

_CONFIG = ConfigDict(extra="forbid", populate_by_name=True, allow_inf_nan=False)


class Target(BaseModel):
    model_config = _CONFIG

    price: float = Field(gt=0)
    r: float = Field(gt=0)
    # 4.8a: which zone the level came from, in plain words. Optional so rows
    # stored before 4.8a still parse (the journal reads them).
    basis: Optional[Basis] = None


class Plan(BaseModel):
    model_config = _CONFIG

    entry: float = Field(gt=0)
    stop: float = Field(gt=0)
    stop_basis: Basis = Field(alias="stopBasis")
    disaster_line: float = Field(gt=0, alias="disasterLine")
    invalidation: Bullet
    targets: list[Target] = Field(min_length=1, max_length=TARGETS_MAX)
    # 4.8a: resistance between the entry and T1 that pays under 1.5R (or sits
    # in a zone straddling the entry). Empty on rows stored before 4.8a.
    overhead: list[Target] = Field(default_factory=list, max_length=TARGETS_MAX)
    size_shares: int = Field(ge=1, alias="sizeShares")
    size_basis: Basis = Field(alias="sizeBasis")
    # 4.8a: size × (entry − disaster) ÷ account, capped at 2.5 % by plan math.
    loss_at_disaster_pct: Optional[float] = Field(default=None, ge=0, alias="lossAtDisasterPct")
    # 4.8a-de: the stop leaves more than 2 ATR of risk at this entry;
    # entryForMaxRisk is the highest entry at which the risk is 2 ATR (plan
    # math's number). Both optional on read: rows before 4.8a-de have neither.
    extended: bool = False
    entry_for_max_risk: Optional[float] = Field(default=None, gt=0, alias="entryForMaxRisk")
    earnings_in_days: Optional[int] = Field(default=None, ge=0, alias="earningsInDays")
    hold_through_earnings: bool = Field(default=False, alias="holdThroughEarnings")
    horizon_days: int = Field(ge=1, le=HORIZON_DAYS_MAX, alias="horizonDays")
    # 4.8b-ai: on `wait`, what blocks `go` now and the level to wait for (a
    # level the plan or the zone list printed, never the model's own — checked
    # softly); the soft-check warnings the answer earned. Both optional so
    # rows stored before 4.8b-ai still parse.
    wait_for: Optional[Bullet] = Field(default=None, alias="waitFor")
    contract_warnings: list[str] = Field(default_factory=list, alias="contractWarnings")

    @model_validator(mode="after")
    def _levels_ascend(self) -> "Plan":
        """0 < disaster < stop < entry < overhead… < t1 < t2 < t3, strictly
        (D8, D11, 4.8a-3)."""
        levels = ([self.disaster_line, self.stop, self.entry] + [t.price for t in self.overhead]
                  + [t.price for t in self.targets])
        if any(lo >= hi for lo, hi in zip(levels, levels[1:])):
            raise ValueError("levels must ascend: disasterLine < stop < entry < overhead < targets")
        if self.entry_for_max_risk is not None and not self.stop < self.entry_for_max_risk < self.entry:
            raise ValueError("entryForMaxRisk must sit between the stop and the entry")
        return self


class Verdict(BaseModel):
    model_config = _CONFIG

    verdict: Literal["go", "wait", "avoid"]
    confidence: int = Field(ge=0, le=100)
    reasoning: Annotated[str, Field(min_length=1, max_length=REASONING_MAX), AfterValidator(_not_blank)]
    thesis: list[Bullet] = Field(min_length=THESIS_LEN, max_length=THESIS_LEN)
    thesis_breakers: list[Bullet] = Field(min_length=1, max_length=BREAKERS_MAX, alias="thesisBreakers")
    plan: Optional[Plan] = None
    risk_flags: list[Flag] = Field(default_factory=list, max_length=RISK_FLAGS_MAX, alias="riskFlags")

    @model_validator(mode="after")
    def _go_needs_plan(self) -> "Verdict":
        """D10: a plan with no stop is refused, so a `go` with no plan is too."""
        if self.verdict == "go" and self.plan is None:
            raise ValueError("a go verdict requires a plan")
        return self
