"""
TradingFirm — journal scoring math (Part 4.5). Pure: no I/O, no clock.

One verdict, one horizon, one window of bars in time order — day 0's hourly
bars that started at or after the ask, then the daily bars day0+1 … N — in,
one outcome row out (spec 4.5 decisions 6, 7, 7b).

    return_pct = (close of session N − entry) ÷ entry × 100
    mae_pct    = (lowest low − entry) ÷ entry × 100      raw and signed
    mfe_pct    = (highest high − entry) ÷ entry × 100    raw and signed

With a stored plan only:
    stop_hit   = any low ≤ stop           (a touch: stricter than D8's hourly
                                           close, so it over-counts stops)
    target_hit = any high ≥ T1
    first_hit  = the first bar touching either: stop | target | same_bar
    r_multiple = 1R = entry − stop; the stop is always honoured and the
                 target is not an exit: exit at the stop (at the open when
                 the bar opened at or below it), else at session N's close.
                 same_bar counts as stop first.

4.3's convention: every number becomes Decimal(str(x)) before arithmetic;
results are rounded half-up to 3 dp (NUMERIC(8,3)).
"""

from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from models.verdict import Plan

HUNDRED = Decimal(100)
THREE_DP = Decimal("0.001")
# The first window bar's open further than this from the entry is read as a
# price-scale break (a split re-scaled the stored bars; spec 4.5 F3), not a
# move: the verdict is left unscored.
SCALE_BREAK = Decimal("0.30")

STOP, TARGET, SAME_BAR = "stop", "target", "same_bar"


class ScaleBreak(ValueError):
    """The first window bar's open is > 30 % from the entry."""


class UnreadablePlan(ValueError):
    """A stored plan that does not parse as models.verdict.Plan."""


def dec(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError(f"not a number: {value!r}")
    out = Decimal(str(value))
    if not out.is_finite():
        raise ValueError(f"not finite: {value!r}")
    return out


def q3(value: Decimal) -> Decimal:
    return value.quantize(THREE_DP, rounding=ROUND_HALF_UP)


def parse_plan(stored) -> Optional[Plan]:
    """None when no plan was stored (plan math rejected); UnreadablePlan when
    one was stored and does not parse — the caller leaves that verdict
    unscored rather than pretending it had no plan."""
    if stored is None:
        return None
    try:
        return Plan.model_validate(stored)
    except Exception as e:
        raise UnreadablePlan(f"{type(e).__name__}") from None


def check_scale(entry: Decimal, first_open: Decimal) -> None:
    if abs(first_open - entry) / entry > SCALE_BREAK:
        raise ScaleBreak(f"first window open {first_open} vs entry {entry}")


def score(entry, plan: Optional[Plan], bars: list[dict]) -> dict:
    """The outcome for one horizon. `bars` are in time order and the last one
    is session N's daily bar. Raises ScaleBreak before computing anything."""
    if not bars:
        raise ValueError("a window has at least session N's bar")
    entry = dec(entry)
    if not entry > 0:
        raise ValueError("entry must be > 0")
    rows = [{k: dec(bar[k]) for k in ("open", "high", "low", "close")} for bar in bars]
    check_scale(entry, rows[0]["open"])

    def pct(price: Decimal) -> Decimal:
        return q3((price - entry) / entry * HUNDRED)

    out = {
        "return_pct": pct(rows[-1]["close"]),
        "mae_pct": pct(min(r["low"] for r in rows)),
        "mfe_pct": pct(max(r["high"] for r in rows)),
        "stop_hit": None, "target_hit": None, "first_hit": None, "r_multiple": None,
    }
    if plan is None:
        return out

    stop, t1 = dec(plan.stop), dec(plan.targets[0].price)
    one_r = entry - stop
    stop_hits = [r["low"] <= stop for r in rows]
    target_hits = [r["high"] >= t1 for r in rows]
    out["stop_hit"] = any(stop_hits)
    out["target_hit"] = any(target_hits)

    for s, t in zip(stop_hits, target_hits):
        if s or t:
            out["first_hit"] = SAME_BAR if (s and t) else (STOP if s else TARGET)
            break

    exit_price = rows[-1]["close"]
    for r, s in zip(rows, stop_hits):
        if s:
            exit_price = r["open"] if r["open"] <= stop else stop
            break
    out["r_multiple"] = q3((exit_price - entry) / one_r)
    return out
