"""
TradingFirm — Plan math v2 (Part 4.3, rewritten in Part 4.8a).

One pure function, `compute_plan`: entry + ATR + the dossier's zones (+ EMA20
and, when a later part sends it, the last swing low) + account + risk
percent → a long swing plan, or a named rejection.

    stop zone = the support-side zone with the highest low (a zone that
                straddles the entry with its midpoint below it counts)
    stop      = stop zone low − 1×ATR                                 (D11)
    far       = no stop zone, or entry − stop > 2×ATR: then the stop is the
                highest of {stop zone stop, EMA20 − 1×ATR, swing low − 1×ATR}
                whose raw level is ≤ entry and whose result is > 0   (4.8a-3)
    disaster  = stop − 1×ATR                                          (D8)
    candidates = resistance-side zone lows above entry (a zone straddling
                the entry with its midpoint at or above it: its high),
                floored to the cent, ascending; one whose distance in ATRs
                (2 dp, half-up) exceeds 8 is dropped                 (4.8a-2)
    T1        = the first candidate paying ≥ 1.5R; every candidate before it,
                and every one inside a straddling zone, is `overhead` (the
                nearest three are listed; the walk continues)        (4.8a-1)
    targets   = T1 and the next two candidates
    size      = min(risk sizing, cash cap, max position, disaster-loss cap)
    lossAtDisasterPct = size × (entry − disaster) ÷ account, ≤ 2.5   (4.8a-8)

Conventions (spec 4.3, kept):
  - Every input becomes Decimal(str(x)) BEFORE any arithmetic, and every
    subtraction, division and floor runs in Decimal. Only the final numbers
    go back to float.
  - Zones are pooled (support + resistance, as data-engine sends them) and
    re-split around the entry by their midpoint, because data-engine split
    them around the last close by the same rule.
  - Prices floor to the cent; R rounds half-up to 2 dp, and the 1.5 test
    uses the rounded R, so a printed "R 1.50" is never rejected.
  - Check order, first failure wins: arguments → ATR → stop (no_support)
    → stop > 0 → disaster > 0 → targets (no_target) → T1 (low_r) → size.
  - Missing data (no ATR, no zones) is a PlanRejected; a caller bug or a
    broken zone contract is a ValueError.
  - Every level carries a plain-language `basis` naming the zone or rule it
    came from, all numbers in cents, ATR to 2 dp (verdict-units decision 6).

`PLAN_MATH_VERSION` is stamped on every verdict (`ai.verdicts.plan_math_version`,
`prompt_inputs.planMathVersion`) and joins the cache fingerprint. Rows
before 4.8a (NULL / absent) are version 1. Bump it on any change to a rule
above.

Pure: standard library only (`test_plan_math_is_pure`). No config, no I/O.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Literal, Optional, Union

PLAN_MATH_VERSION = 2

STOP_ATR_MULT = Decimal("1")
DISASTER_ATR_MULT = Decimal("1")
MIN_BEST_R = Decimal("1.5")
MAX_TARGETS = 3
MAX_OVERHEAD = 3
TARGET_MAX_ATR = Decimal("8")
FAR_SUPPORT_ATR = Decimal("2")
MAX_POSITION_PCT = Decimal("25")
MAX_DISASTER_LOSS_PCT = Decimal("2.5")
RISK_PCT_MAX = Decimal("10")

CENT = Decimal("0.01")
TWO = Decimal("2")
HUNDRED = Decimal("100")

Reason = Literal[
    "no_atr",
    "no_support",
    "stop_non_positive",
    "disaster_non_positive",
    "no_target",
    "low_r",
    "size_zero",
]
SizeBound = Literal["risk", "max position", "cash cap", "disaster loss"]


@dataclass(frozen=True)
class Target:
    price: float
    r: float
    basis: str


@dataclass(frozen=True)
class PlanMath:
    entry: float
    stop: float
    stop_basis: str
    disaster_line: float
    targets: tuple[Target, ...]
    overhead: tuple[Target, ...]
    best_r: float
    risk_per_share: float
    risk_budget: float
    size_shares: int
    size_bound: SizeBound
    size_basis: str
    loss_at_disaster_pct: float


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


def atr_distance(price: Decimal, entry: Decimal, atr: Decimal) -> Decimal:
    """(price − entry) / ATR, half-up to 2 dp — the target cap's yardstick."""
    return ((price - entry) / atr).quantize(CENT, rounding=ROUND_HALF_UP)


def money(value: Decimal) -> str:
    """A price or an ATR for a basis string: 2 dp, half-up (display only —
    the plan's own levels are already floored to the cent)."""
    return f"{value.quantize(CENT, rounding=ROUND_HALF_UP)}"


def _ratio(value: Decimal) -> str:
    return f"{value.quantize(CENT, rounding=ROUND_HALF_UP)}"


# ── Checks ────────────────────────────────────────────────────────────────


def _positive(value: object, name: str) -> Decimal:
    d = to_decimal(value, name)
    if d <= 0:
        raise ValueError(f"{name} must be > 0")
    return d


@dataclass(frozen=True)
class _Zone:
    low: Decimal
    high: Decimal
    mid: Decimal
    detail: str


def _zone_detail(zone: Mapping) -> str:
    """The zone's own facts for a basis string: tests, volume node, score.
    Every key is optional (plan math needs low and high only)."""
    parts = []
    tests = zone.get("tests")
    if isinstance(tests, int) and not isinstance(tests, bool) and tests > 0:
        parts.append(f"{tests} test{'s' if tests != 1 else ''}")
    if zone.get("volumeNode") is True or zone.get("volume_node") is True:
        parts.append("volume node")
    score = zone.get("score")
    if isinstance(score, int) and not isinstance(score, bool):
        parts.append(f"score {score}")
    return ", ".join(parts)


def _zones(zones: Sequence[Mapping]) -> list[_Zone]:
    """(low, high, midpoint, detail) per zone; a broken zone is a data-engine
    contract break."""
    out = []
    for i, zone in enumerate(zones):
        if not isinstance(zone, Mapping) or "low" not in zone or "high" not in zone:
            raise ValueError(f"zone {i} needs low and high")
        low = _positive(zone["low"], f"zone {i} low")
        high = _positive(zone["high"], f"zone {i} high")
        if high < low:
            raise ValueError(f"zone {i} high < low")
        out.append(_Zone(low, high, (low + high) / TWO, _zone_detail(zone)))
    return out


def _optional_level(value: object, name: str) -> Optional[Decimal]:
    """EMA20 / swing low: None when absent or unusable (not a bug: the
    dossier may lack them); a non-number that is not None is a caller bug."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number or None")
    if not math.isfinite(value) or value <= 0:
        return None
    return Decimal(str(value))


def _atr(atr: object) -> Optional[Decimal]:
    """None when the dossier has no usable ATR (too few bars) — not a bug."""
    if atr is None or isinstance(atr, bool) or not isinstance(atr, (int, float)):
        return None
    if not math.isfinite(atr) or atr <= 0:
        return None
    return Decimal(str(atr))


def _describe(kind: str, z: _Zone) -> str:
    text = f"{kind} {money(z.low)}-{money(z.high)}"
    return f"{text}, {z.detail}" if z.detail else text


# ── Plan ──────────────────────────────────────────────────────────────────


def compute_plan(
    *,
    entry: float,
    atr: Optional[float],
    zones: Sequence[Mapping],
    account: float,
    risk_pct: float,
    ema20: Optional[float] = None,
    swing_low: Optional[float] = None,
    swing_low_date: Optional[str] = None,
) -> Union[PlanMath, PlanRejected]:
    """
    A long plan from entry, ATR and zones, or the first check that fails.

    `zones` is data-engine's support + resistance lists pooled; each item
    needs `low` and `high` (`tests`, `volumeNode`, `score` feed the basis
    text; other keys are ignored). `risk_pct` is a percent: 1.0 means 1 % of
    `account`. `ema20` and `swing_low` are the stop alternatives for a name
    whose support is far below (None = not available); `swing_low_date`
    only names the swing low in the basis text.
    """
    # 1. arguments
    e = _positive(entry, "entry")
    acct = _positive(account, "account")
    pct = _positive(risk_pct, "risk_pct")
    if pct > RISK_PCT_MAX:
        raise ValueError(f"risk_pct must be <= {RISK_PCT_MAX}")
    pool = _zones(zones)
    ema = _optional_level(ema20, "ema20")
    swing = _optional_level(swing_low, "swing_low")
    if swing_low_date is not None and not isinstance(swing_low_date, str):
        raise ValueError("swing_low_date must be a string or None")

    # 2. ATR
    a = _atr(atr)
    if a is None:
        return PlanRejected("no_atr", f"atr {atr!r} is not a positive number")

    # 3. the stop: support side = midpoint below the entry (data-engine's own
    #    split rule, applied to the entry instead of the last close)
    support = [z for z in pool if z.mid < e]
    resistance = [z for z in pool if z.mid >= e]
    stop_zone = max(support, key=lambda z: z.low) if support else None
    candidates: list[tuple[Decimal, str]] = []
    far_note: str
    if stop_zone is not None:
        zone_stop = to_cents(stop_zone.low - STOP_ATR_MULT * a)
        zone_risk = e - zone_stop
        far = zone_risk > FAR_SUPPORT_ATR * a
        candidates.append((zone_stop, f"{_describe('support', stop_zone)}, low {money(stop_zone.low)}"
                                      f" - {STOP_ATR_MULT}xATR {money(a)}"))
        far_note = (f"support {money(stop_zone.low)}-{money(stop_zone.high)} gives risk "
                    f"{money(zone_risk)} = {_ratio(zone_risk / a)} ATR (> {FAR_SUPPORT_ATR} ATR)")
    else:
        far = True
        far_note = f"no support zone below entry {money(e)}"
    if far:
        if ema is not None and ema <= e:
            alt = to_cents(ema - STOP_ATR_MULT * a)
            if alt > 0:
                candidates.append((alt, f"EMA20 {money(ema)} - {STOP_ATR_MULT}xATR {money(a)}; {far_note}"))
        if swing is not None and swing <= e:
            alt = to_cents(swing - STOP_ATR_MULT * a)
            if alt > 0:
                when = f" ({swing_low_date})" if swing_low_date else ""
                candidates.append((alt, f"swing low{when} {money(swing)} - {STOP_ATR_MULT}xATR {money(a)}; {far_note}"))
    if not candidates:
        return PlanRejected("no_support", f"no zone with midpoint < entry {e} among {len(pool)}, "
                                          f"and no EMA20 / swing low at or below it")
    stop, stop_rule = max(candidates, key=lambda c: c[0])

    # 4. stop > 0 (only the support-zone stop can be ≤ 0: alternatives were filtered)
    if stop <= 0:
        return PlanRejected("stop_non_positive", f"stop {stop} from {stop_rule}")

    # 5. disaster line, from the rounded stop so the printed numbers subtract
    disaster = to_cents(stop - DISASTER_ATR_MULT * a)
    if disaster <= 0:
        return PlanRejected("disaster_non_positive", f"disaster {disaster} from stop {stop} - ATR {a}")

    # 6. target candidates: resistance-side zone lows above entry; a zone
    #    straddling the entry offers its high, and everything up to that
    #    high is overhead whatever it pays
    raw: list[tuple[Decimal, str]] = []
    straddle_high: Optional[Decimal] = None
    for z in resistance:
        if z.low > e:
            raw.append((to_cents(z.low), _describe("resistance", z)))
        else:
            top = to_cents(z.high)
            raw.append((top, f"zone high, {_describe('resistance', z)} straddles entry {money(e)}"))
            straddle_high = top if straddle_high is None else max(straddle_high, top)
    raw.sort(key=lambda c: c[0])
    if not raw:
        return PlanRejected("no_target", f"no resistance above entry {e}")

    cap = TARGET_MAX_ATR * a
    prices: list[tuple[Decimal, str]] = []
    dropped: list[Decimal] = []
    for price, detail in raw:
        # the floor can land on the entry (low 50.004, entry 50.00), and two
        # lows can floor to one cent; neither is a distinct level
        if price <= e or (prices and price <= prices[-1][0]):
            continue
        # the distance in ATRs, quantized like R: a zone printed as "8.00
        # ATR" is never dropped by a millionth (OPCH's 28.70 sat 4.7500 above
        # a 23.95 entry against 7 × ATR = 4.749998)
        if atr_distance(price, e, a) > TARGET_MAX_ATR:
            dropped.append(price)
            continue
        prices.append((price, detail))
    if not prices:
        if dropped:
            return PlanRejected("no_target", f"every resistance above entry {e} is beyond "
                                             f"{TARGET_MAX_ATR}xATR {money(cap)}: {', '.join(money(p) for p in dropped)}")
        return PlanRejected("no_target", f"no resistance above entry {e}")

    # 7. T1 = the first candidate paying >= 1.5R; the ones before it are overhead
    overhead: list[Target] = []
    targets: list[Target] = []
    best_seen = Decimal("0")
    for price, detail in prices:
        r = r_multiple(price, e, stop)
        best_seen = max(best_seen, r)
        inside = straddle_high is not None and price <= straddle_high
        if not targets and (r < MIN_BEST_R or inside):
            if len(overhead) < MAX_OVERHEAD:
                overhead.append(Target(float(price), float(r), f"overhead {price}: {detail}"))
            continue
        if len(targets) < MAX_TARGETS:
            targets.append(Target(float(price), float(r), f"T{len(targets) + 1} {price}: {detail}"))
    if not targets:
        if best_seen >= MIN_BEST_R:
            return PlanRejected("low_r", f"no target outside the zone straddling entry {e} "
                                         f"(best R {best_seen} is inside it)")
        return PlanRejected("low_r", f"no resistance pays >= {MIN_BEST_R}R: best R {best_seen}")
    best = max(Decimal(str(t.r)) for t in targets)

    # 8. size: the smallest of four bounds, ties in this order
    per_share = e - stop
    per_share_disaster = e - disaster
    budget = acct * pct / HUNDRED
    bounds: list[tuple[SizeBound, int]] = [
        ("risk", int((budget / per_share).to_integral_value(rounding=ROUND_FLOOR))),
        ("max position", int((acct * MAX_POSITION_PCT / HUNDRED / e).to_integral_value(rounding=ROUND_FLOOR))),
        ("cash cap", int((acct / e).to_integral_value(rounding=ROUND_FLOOR))),
        ("disaster loss", int((acct * MAX_DISASTER_LOSS_PCT / HUNDRED / per_share_disaster)
                              .to_integral_value(rounding=ROUND_FLOOR))),
    ]
    bound, size = min(bounds, key=lambda b: b[1])
    if size < 1:
        return PlanRejected("size_zero", f"size 0 ({bound}): budget {money(budget)}, risk/share {money(per_share)}")
    loss_pct = (Decimal(size) * per_share_disaster / acct * HUNDRED).quantize(CENT, rounding=ROUND_HALF_UP)

    sizing = ", ".join(f"{name} {n}" for name, n in bounds)
    return PlanMath(
        entry=float(e),
        stop=float(stop),
        stop_basis=f"stop {stop}: {stop_rule}",
        disaster_line=float(disaster),
        targets=tuple(targets),
        overhead=tuple(overhead),
        best_r=float(best),
        risk_per_share=float(per_share),
        risk_budget=float(budget),
        size_shares=size,
        size_bound=bound,
        size_basis=(f"{bound}: {size} shares ({sizing}; risk {pct}% of {money(acct)} = {money(budget)}, "
                    f"{money(per_share)}/share; loss at disaster {loss_pct}% of account, cap {MAX_DISASTER_LOSS_PCT}%)"),
        loss_at_disaster_pct=float(loss_pct),
    )
