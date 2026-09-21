"""
TradingFirm — Plan math (Part 4.3).

One pure function, `compute_plan`: entry + ATR + the dossier's zones +
account + risk percent → a long swing plan, or a named rejection.

    stop      = nearest support zone low − 1×ATR          (D11)
    disaster  = stop − 1×ATR                              (D8)
    targets   = next resistance zone lows above entry, R = (target − entry) / (entry − stop)
    reject    if best R < 1.5
    size      = min(risk sizing, cash cap, max position)

Conventions (spec 4.3):
  - Every input becomes Decimal(str(x)) BEFORE any arithmetic, and every
    subtraction, division and floor runs in Decimal. In float,
    47.80 − 1.20 = 46.599999999999994, which floors to 46.59; here it is
    46.60. Only the final numbers go back to float.
  - Zones are pooled (support + resistance, as data-engine sends them) and
    re-split around the entry, because data-engine split them around the
    last close. Support: low ≤ entry. Target: low > entry.
  - Prices floor to the cent; R rounds half-up to 2 dp, and the 1.5 test
    uses the rounded R, so a printed "R 1.50" is never rejected.
  - Check order, first failure wins: arguments → ATR → support → stop > 0
    → disaster > 0 → targets → best R → size.
  - Missing data (no ATR, no zones) is a PlanRejected; a caller bug or a
    broken zone contract is a ValueError.

Pure: standard library only (`test_plan_math_is_pure`). No config, no I/O.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Literal, Optional, Union

STOP_ATR_MULT = Decimal("1")
DISASTER_ATR_MULT = Decimal("1")
MIN_BEST_R = Decimal("1.5")
MAX_TARGETS = 3
MAX_POSITION_PCT = Decimal("25")
RISK_PCT_MAX = Decimal("10")

CENT = Decimal("0.01")

Reason = Literal[
    "no_atr",
    "no_support",
    "stop_non_positive",
    "disaster_non_positive",
    "no_target",
    "low_r",
    "size_zero",
]
SizeBound = Literal["risk", "max position", "cash cap"]


@dataclass(frozen=True)
class Target:
    price: float
    r: float


@dataclass(frozen=True)
class PlanMath:
    entry: float
    stop: float
    stop_basis: str
    disaster_line: float
    targets: tuple[Target, ...]
    best_r: float
    risk_per_share: float
    risk_budget: float
    size_shares: int
    size_bound: SizeBound
    size_basis: str


@dataclass(frozen=True)
class PlanRejected:
    reason: Reason
    detail: str


# ── Normalization ─────────────────────────────────────────────────────────


def to_decimal(value: object, name: str) -> Decimal:
    """The one entry point for numbers: finite int/float → Decimal(str(x))."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return Decimal(str(value))


def to_cents(value: Decimal) -> Decimal:
    """Floor to the cent — every stop, disaster line and target."""
    return value.quantize(CENT, rounding=ROUND_FLOOR)


def r_multiple(target: Decimal, entry: Decimal, stop: Decimal) -> Decimal:
    """(target − entry) / (entry − stop), half-up to 2 dp."""
    return ((target - entry) / (entry - stop)).quantize(CENT, rounding=ROUND_HALF_UP)


# ── Checks ────────────────────────────────────────────────────────────────


def _positive(value: object, name: str) -> Decimal:
    d = to_decimal(value, name)
    if d <= 0:
        raise ValueError(f"{name} must be > 0")
    return d


def _zones(zones: Sequence[Mapping]) -> list[tuple[Decimal, Decimal]]:
    """(low, high) per zone; a broken zone is a data-engine contract break."""
    out = []
    for i, zone in enumerate(zones):
        if not isinstance(zone, Mapping) or "low" not in zone or "high" not in zone:
            raise ValueError(f"zone {i} needs low and high")
        low = _positive(zone["low"], f"zone {i} low")
        high = _positive(zone["high"], f"zone {i} high")
        if high < low:
            raise ValueError(f"zone {i} high < low")
        out.append((low, high))
    return out


def _atr(atr: object) -> Optional[Decimal]:
    """None when the dossier has no usable ATR (too few bars) — not a bug."""
    if atr is None or isinstance(atr, bool) or not isinstance(atr, (int, float)):
        return None
    if not math.isfinite(atr) or atr <= 0:
        return None
    return Decimal(str(atr))


# ── Plan ──────────────────────────────────────────────────────────────────


def compute_plan(
    *,
    entry: float,
    atr: Optional[float],
    zones: Sequence[Mapping],
    account: float,
    risk_pct: float,
) -> Union[PlanMath, PlanRejected]:
    """
    A long plan from entry, ATR and zones, or the first check that fails.

    `zones` is data-engine's support + resistance lists pooled; each item
    needs `low` and `high` (other keys are ignored). `risk_pct` is a
    percent: 1.0 means 1 % of `account`.
    """
    # 1. arguments
    e = _positive(entry, "entry")
    acct = _positive(account, "account")
    pct = _positive(risk_pct, "risk_pct")
    if pct > RISK_PCT_MAX:
        raise ValueError(f"risk_pct must be <= {RISK_PCT_MAX}")
    pool = _zones(zones)

    # 2. ATR
    a = _atr(atr)
    if a is None:
        return PlanRejected("no_atr", f"atr {atr!r} is not a positive number")

    # 3. support: the candidate with the highest low
    support = [z for z in pool if z[0] <= e]
    if not support:
        return PlanRejected("no_support", f"no zone with low <= entry {e} among {len(pool)}")
    s_low, s_high = max(support, key=lambda z: z[0])

    # 4. stop
    stop = to_cents(s_low - STOP_ATR_MULT * a)
    if stop <= 0:
        return PlanRejected("stop_non_positive", f"stop {stop} from support low {s_low} - ATR {a}")

    # 5. disaster line, from the rounded stop so the printed numbers subtract
    disaster = to_cents(stop - DISASTER_ATR_MULT * a)
    if disaster <= 0:
        return PlanRejected("disaster_non_positive", f"disaster {disaster} from stop {stop} - ATR {a}")

    # 6. targets: nearest resistance lows above entry, floored, ascending
    prices: list[Decimal] = []
    for low in sorted(z[0] for z in pool if z[0] > e):
        price = to_cents(low)
        # the floor can land on the entry (low 50.004, entry 50.00), and two
        # lows can floor to one cent; neither is a distinct target
        if price > e and (not prices or price > prices[-1]):
            prices.append(price)
    prices = prices[:MAX_TARGETS]
    if not prices:
        return PlanRejected("no_target", f"no resistance above entry {e}")

    # 7. best R
    rs = [r_multiple(p, e, stop) for p in prices]
    best = max(rs)
    if best < MIN_BEST_R:
        return PlanRejected("low_r", f"best R {best} < {MIN_BEST_R}")

    # 8. size: the smallest of three bounds, ties in this order
    per_share = e - stop
    budget = acct * pct / 100
    bounds: list[tuple[SizeBound, int]] = [
        ("risk", int((budget / per_share).to_integral_value(rounding=ROUND_FLOOR))),
        ("max position", int((acct * MAX_POSITION_PCT / 100 / e).to_integral_value(rounding=ROUND_FLOOR))),
        ("cash cap", int((acct / e).to_integral_value(rounding=ROUND_FLOOR))),
    ]
    bound, size = min(bounds, key=lambda b: b[1])
    if size < 1:
        return PlanRejected("size_zero", f"size 0 ({bound}): budget {budget}, risk/share {per_share}")

    sizing = ", ".join(f"{name} {n}" for name, n in bounds)
    return PlanMath(
        entry=float(e),
        stop=float(stop),
        stop_basis=f"support zone {s_low}-{s_high}, low {s_low} - {STOP_ATR_MULT}xATR {a}",
        disaster_line=float(disaster),
        targets=tuple(Target(price=float(p), r=float(r)) for p, r in zip(prices, rs)),
        best_r=float(best),
        risk_per_share=float(per_share),
        risk_budget=float(budget),
        size_shares=size,
        size_bound=bound,
        size_basis=f"{bound}: {size} shares ({sizing}; risk {pct}% of {acct} = {budget}, {per_share}/share)",
    )
