"""
TradingFirm — Plan math v3 (Part 4.3, rewritten in 4.8a, extended in 4.8a-de).

One pure function, `compute_plan`: entry + ATR + the dossier's zones (with
their history) + EMA20 + the last swing low + account + risk percent → a
long swing plan, or a named rejection.

    components  every level input is floored to the cent BEFORE the
                subtraction (ATR too), so each figure a basis string prints
                is a component and the printed subtraction lands on the
                level: stop = level_c − atr_c                     (4.8a-de-4)
    stop zone   among the support-side zones whose stop leaves ≤ 2×ATR of
                risk, the one that held most from above (`heldAbove`; ties:
                highest low); a support zone with brokeAbove ≥ heldAbove is
                looked through — never the stop zone, never eligible; a
                zone's side is data-engine's label (`side`), the midpoint
                only when a zone carries none    (4.8a-de-4, 4.8a-7, 2026-09-24)
    far         no support zone within 2×ATR: the stop is the highest of
                {the highest-low support zone's stop, EMA20 − 1×ATR,
                swing low − 1×ATR} whose raw level is ≤ entry and whose
                result is > 0                                        (4.8a-3)
    extended    the chosen stop still leaves risk > 2×ATR: the plan is built
                and flagged, with entryForMaxRisk = stop + 2×atr_c, the
                highest entry at which the risk is 2 ATR            (4.8a-de-4)
    disaster    = stop − atr_c                                            (D8)
    candidates  resistance-side zone lows above entry (a straddling zone:
                its high), floored, ascending; one whose distance in ATRs
                (2 dp, half-up, exact ATR) exceeds 8 is dropped       (4.8a-2)
    ceiling     the nearest candidate whose zone, approached from below,
                held ≥ 3 and held ≥ 3×broke (`heldBelow` / `brokeBelow`) is
                where the walk stops: it is the last eligible candidate;
                nothing above it is a target          (4.8a-de-5, 2026-09-24)
    T1          the first eligible candidate paying ≥ 1.5R; the ones before
                it are `overhead` (three listed, the walk continues); no T1
                under a ceiling → `ceiling`, none at all → `low_r` (4.8a-1)
    targets     T1 and the next two eligible candidates
    size        min(risk sizing, max position, cash cap, disaster-loss cap)
    lossAtDisasterPct = size × (entry − disaster) ÷ account, ≤ 2.5   (4.8a-8)

Conventions (spec 4.3, kept):
  - Every input becomes Decimal(str(x)) BEFORE any arithmetic, and every
    subtraction, division and floor runs in Decimal. Only the final numbers
    go back to float.
  - Prices floor to the cent; R rounds half-up to 2 dp, and the 1.5 test
    uses the rounded R, so a printed "R 1.50" is never rejected. Ratios
    (the 8-ATR cap, the 2-ATR tests) use the EXACT ATR: they are
    comparisons, not printed levels.
  - Check order, first failure wins: arguments → ATR → stop (no_support)
    → stop > 0 → disaster > 0 → targets (no_target) → ceiling → T1 (low_r)
    → size.
  - Missing data (no ATR, no zones) is a PlanRejected; a caller bug or a
    broken zone contract is a ValueError.
  - Every level carries a plain-language `basis` naming the zone or rule it
    came from, in cents. A zone with history prints touches / held / broke /
    last and not its `tests` count (4.8a-de change 7).
  - `size_basis` names bounds only: no count, no dollar figure, no percent
    of the account (4.8a-de change 3). `size_shares` is the one count.

`PLAN_MATH_VERSION` is stamped on every verdict (`ai.verdicts.plan_math_version`,
`prompt_inputs.planMathVersion`) and joins the cache fingerprint. Rows
before 4.8a (NULL / absent) are version 1, 4.8a's are 2. Bump it on any
change to a rule above.

Pure: standard library only (`test_plan_math_is_pure`). No config, no I/O.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Literal, Optional, Union

PLAN_MATH_VERSION = 3

STOP_ATR_MULT = Decimal("1")
DISASTER_ATR_MULT = Decimal("1")
MIN_BEST_R = Decimal("1.5")
MAX_TARGETS = 3
MAX_OVERHEAD = 3
TARGET_MAX_ATR = Decimal("8")
FAR_SUPPORT_ATR = Decimal("2")
CEILING_MIN_HELD = 3
CEILING_HELD_PER_BROKE = 3
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
    "ceiling",
    "low_r",
    "size_zero",
]
SizeBound = Literal["risk", "max position", "cash cap", "disaster loss"]
BOUND_LABELS: dict[str, str] = {
    "risk": "risk",
    "max position": f"max position ≤ {MAX_POSITION_PCT} %",
    "cash cap": "cash cap",
    "disaster loss": f"disaster loss ≤ {MAX_DISASTER_LOSS_PCT} %",
}


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
    extended: bool
    entry_for_max_risk: Optional[float]


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
    """Floor to the cent — every level, and every component a level is
    built from."""
    return value.quantize(CENT, rounding=ROUND_FLOOR)


def r_multiple(target: Decimal, entry: Decimal, stop: Decimal) -> Decimal:
    """(target − entry) / (entry − stop), half-up to 2 dp."""
    return ((target - entry) / (entry - stop)).quantize(CENT, rounding=ROUND_HALF_UP)


def atr_distance(price: Decimal, entry: Decimal, atr: Decimal) -> Decimal:
    """(price − entry) / ATR, half-up to 2 dp — the yardstick of the target
    cap, the 2-ATR support test and the extension test (exact ATR)."""
    return ((price - entry) / atr).quantize(CENT, rounding=ROUND_HALF_UP)


def money(value: Decimal) -> str:
    """A price, an ATR or a ratio for a basis string: floored to the cent,
    the SAME rounding as every plan level (`to_cents`). Since v3 every level
    is built from floored components, a printed `a - b` equals its level."""
    return f"{to_cents(value)}"


def _ratio(value: Decimal) -> str:
    return money(value)


# ── Checks ────────────────────────────────────────────────────────────────


def _positive(value: object, name: str) -> Decimal:
    d = to_decimal(value, name)
    if d <= 0:
        raise ValueError(f"{name} must be > 0")
    return d


SIDES = ("support", "resistance")


@dataclass(frozen=True)
class _Zone:
    low: Decimal
    high: Decimal
    mid: Decimal
    detail: str
    side: Optional[str]
    held_below: Optional[int]
    broke_below: Optional[int]
    held_above: Optional[int]
    broke_above: Optional[int]

    def is_support(self, entry: Decimal) -> bool:
        """data-engine's label when the zone carries one; else the midpoint."""
        if self.side is not None:
            return self.side == "support"
        return self.mid < entry

    def is_ceiling(self) -> bool:
        """Approached from below, held ≥ 3 and held ≥ 3 × broke (4.8a-de
        decision 5 on the side split, 2026-09-24). A zone without the split
        is never a ceiling."""
        if self.held_below is None:
            return False
        broke = self.broke_below or 0
        return self.held_below >= CEILING_MIN_HELD and self.held_below >= CEILING_HELD_PER_BROKE * broke

    def is_looked_through_support(self) -> bool:
        """A support zone that gave way from above as often as it held
        (brokeAbove ≥ heldAbove) is never the stop zone (2026-09-24). A
        zone without the split is kept."""
        if self.held_above is None and self.broke_above is None:
            return False
        return (self.broke_above or 0) >= (self.held_above or 0)


def _count(value: object) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _zone_detail(zone: Mapping) -> str:
    """The zone's own facts for a basis string. With history (4.8a-de):
    touches, held, broke, last; without it (an older dossier): the swing
    count. Then volume node and score. Every key is optional (plan math
    needs low and high only); a malformed field is simply not printed."""
    parts = []
    held, broke = _count(zone.get("held")), _count(zone.get("broke"))
    if held is not None or broke is not None:
        touches = _count(zone.get("touches"))
        if touches is not None:
            parts.append(f"touches {touches}")
        hb, ha = _count(zone.get("heldBelow")), _count(zone.get("heldAbove"))
        bb, ba = _count(zone.get("brokeBelow")), _count(zone.get("brokeAbove"))
        split = hb is not None and ha is not None and bb is not None and ba is not None
        if held is not None:
            parts.append(f"held {held} ({hb} below, {ha} above)" if split else f"held {held}")
        if broke is not None:
            parts.append(f"broke {broke} ({bb} below, {ba} above)" if split else f"broke {broke}")
        last = zone.get("lastTouch")
        if isinstance(last, str) and last.strip():
            parts.append(f"last {last.strip()}")
    else:
        tests = _count(zone.get("tests"))
        if tests:
            parts.append(f"{tests} test{'s' if tests != 1 else ''}")
    if zone.get("volumeNode") is True or zone.get("volume_node") is True:
        parts.append("volume node")
    score = zone.get("score")
    if isinstance(score, int) and not isinstance(score, bool):
        parts.append(f"score {score}")
    return ", ".join(parts)


def _zones(zones: Sequence[Mapping]) -> list[_Zone]:
    """(low, high, midpoint, detail, side, held, broke) per zone; a broken
    zone is a data-engine contract break."""
    out = []
    for i, zone in enumerate(zones):
        if not isinstance(zone, Mapping) or "low" not in zone or "high" not in zone:
            raise ValueError(f"zone {i} needs low and high")
        low = _positive(zone["low"], f"zone {i} low")
        high = _positive(zone["high"], f"zone {i} high")
        if high < low:
            raise ValueError(f"zone {i} high < low")
        side = zone.get("side")
        if side is not None and side not in SIDES:
            raise ValueError(f"zone {i} side {side!r} is not one of {SIDES}")
        out.append(_Zone(low, high, (low + high) / TWO, _zone_detail(zone), side,
                         _count(zone.get("heldBelow")), _count(zone.get("brokeBelow")),
                         _count(zone.get("heldAbove")), _count(zone.get("brokeAbove"))))
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
    needs `low` and `high`, carries `side` ("support" / "resistance", the
    list it came from; absent = classify by midpoint), the side split
    `heldBelow` / `brokeBelow` (the ceiling reads them) and `heldAbove` /
    `brokeAbove` (the stop preference and the looked-through rule read
    them; absent = no history), and `touches`, `held`, `broke`,
    `lastTouch`, `tests`, `volumeNode`, `score` feed the basis text; other
    keys are ignored. `risk_pct` is a percent: 1.0 means
    1 % of `account`. `ema20` and `swing_low` are the far-branch stop
    alternatives (None = not available); `swing_low_date` only names the
    swing low in the basis text.
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

    # 2. ATR: exact for ratios, floored as a level component
    a = _atr(atr)
    if a is None:
        return PlanRejected("no_atr", f"atr {atr!r} is not a positive number")
    a_c = to_cents(a)
    buffer = STOP_ATR_MULT * a_c

    def level_stop(level: Decimal) -> Decimal:
        """A stop from a level: both components floored first."""
        return to_cents(level) - buffer

    def risk_atr(stop_: Decimal) -> Decimal:
        return atr_distance(e, stop_, a)

    # 3. the stop
    all_support = [z for z in pool if z.is_support(e)]
    resistance = [z for z in pool if not z.is_support(e)]
    looked_through = [z for z in all_support if z.is_looked_through_support()]
    support = [z for z in all_support if z not in looked_through]
    eligible = [(z, level_stop(z.low)) for z in support if risk_atr(level_stop(z.low)) <= FAR_SUPPORT_ATR]
    candidates: list[tuple[Decimal, str]] = []
    skipped = (f"; {len(looked_through)} support zone{'s' if len(looked_through) != 1 else ''} looked through "
               f"(broke from above at least as often as held)") if looked_through else ""
    if eligible:
        z, zone_stop = max(eligible, key=lambda pair: (pair[0].held_above or 0, pair[0].low))
        rule = f"{_describe('support', z)}, low {money(z.low)} - {STOP_ATR_MULT}xATR {money(a)}"
        if len(eligible) > 1:
            rule += f"; most held from above of {len(eligible)} support zones within {FAR_SUPPORT_ATR} ATR"
        candidates.append((zone_stop, rule + skipped))
    else:
        if support:
            nearest = max(support, key=lambda z: z.low)
            zone_stop = level_stop(nearest.low)
            zone_risk = e - zone_stop
            far_note = (f"nearest support {money(nearest.low)}-{money(nearest.high)} gives risk "
                        f"{money(zone_risk)} = {_ratio(zone_risk / a)} ATR (> {FAR_SUPPORT_ATR} ATR)")
            candidates.append((zone_stop, f"{_describe('support', nearest)}, low {money(nearest.low)}"
                                          f" - {STOP_ATR_MULT}xATR {money(a)}"))
        else:
            far_note = f"no support zone below entry {money(e)}" + skipped
        if ema is not None and ema <= e:
            alt = level_stop(ema)
            if alt > 0:
                candidates.append((alt, f"EMA20 {money(ema)} - {STOP_ATR_MULT}xATR {money(a)}; {far_note}"))
        if swing is not None and swing <= e:
            alt = level_stop(swing)
            if alt > 0:
                when = f" ({swing_low_date})" if swing_low_date else ""
                candidates.append((alt, f"swing low{when} {money(swing)} - {STOP_ATR_MULT}xATR {money(a)}; {far_note}"))
    if not candidates:
        return PlanRejected("no_support", f"no zone with midpoint < entry {e} among {len(pool)}, "
                                          f"and no EMA20 / swing low at or below it")
    stop, stop_rule = max(candidates, key=lambda c: c[0])

    # 4. stop > 0 (only a support-zone stop can be ≤ 0: alternatives were filtered)
    if stop <= 0:
        return PlanRejected("stop_non_positive", f"stop {stop} from {stop_rule}")

    # 5. disaster line: the same floored buffer, so the printed numbers subtract
    disaster = stop - DISASTER_ATR_MULT * a_c
    if disaster <= 0:
        return PlanRejected("disaster_non_positive", f"disaster {disaster} from stop {stop} - ATR {a_c}")

    # 6. extension: the stop that won still leaves more than 2 ATR of risk
    per_share = e - stop
    risk_in_atr = risk_atr(stop)
    extended = risk_in_atr > FAR_SUPPORT_ATR
    entry_for_max_risk: Optional[Decimal] = None
    if extended:
        entry_for_max_risk = stop + FAR_SUPPORT_ATR * a_c
        stop_rule += (f"; extended: risk {money(per_share)} = {risk_in_atr} ATR (> {FAR_SUPPORT_ATR} ATR), "
                      f"entry for {FAR_SUPPORT_ATR} ATR risk {money(entry_for_max_risk)} = "
                      f"stop {money(stop)} + {FAR_SUPPORT_ATR}xATR {money(a)}")

    # 7. target candidates: resistance-side zone lows above entry; a zone
    #    straddling the entry offers its high, and everything up to that
    #    high is overhead whatever it pays
    raw: list[tuple[Decimal, str, _Zone]] = []
    straddle_high: Optional[Decimal] = None
    for z in resistance:
        if z.low > e:
            raw.append((to_cents(z.low), _describe("resistance", z), z))
        else:
            top = to_cents(z.high)
            raw.append((top, f"zone high, {_describe('resistance', z)} straddles entry {money(e)}", z))
            straddle_high = top if straddle_high is None else max(straddle_high, top)
    raw.sort(key=lambda c: c[0])
    if not raw:
        return PlanRejected("no_target", f"no resistance above entry {e}")

    cap = TARGET_MAX_ATR * a
    prices: list[tuple[Decimal, str, _Zone]] = []
    dropped: list[Decimal] = []
    for price, detail, z in raw:
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
        prices.append((price, detail, z))
    if not prices:
        if dropped:
            return PlanRejected("no_target", f"every resistance above entry {e} is beyond "
                                             f"{TARGET_MAX_ATR}xATR {money(cap)}: {', '.join(money(p) for p in dropped)}")
        return PlanRejected("no_target", f"no resistance above entry {e}")

    # 8. the ceiling: the nearest well-held zone ends the eligible list
    ceiling: Optional[tuple[Decimal, str, _Zone]] = None
    for i, (price, detail, z) in enumerate(prices):
        if z.is_ceiling():
            ceiling = (price, detail, z)
            prices = prices[: i + 1]
            break

    # 9. T1 = the first eligible candidate paying >= 1.5R; the ones before it are overhead
    overhead: list[Target] = []
    targets: list[Target] = []
    best_seen = Decimal("0")
    for price, detail, z in prices:
        r = r_multiple(price, e, stop)
        best_seen = max(best_seen, r)
        inside = straddle_high is not None and price <= straddle_high
        if ceiling is not None and price == ceiling[0]:
            detail = f"{detail}, ceiling"
        if not targets and (r < MIN_BEST_R or inside):
            if len(overhead) < MAX_OVERHEAD:
                overhead.append(Target(float(price), float(r), f"overhead {price}: {detail}"))
            continue
        if len(targets) < MAX_TARGETS:
            targets.append(Target(float(price), float(r), f"T{len(targets) + 1} {price}: {detail}"))
    if not targets:
        if ceiling is not None:
            return PlanRejected("ceiling", f"{_describe('resistance', ceiling[2])} caps the trade at "
                                           f"{r_multiple(ceiling[0], e, stop)}R")
        if best_seen >= MIN_BEST_R:
            return PlanRejected("low_r", f"no target outside the zone straddling entry {e} "
                                         f"(best R {best_seen} is inside it)")
        return PlanRejected("low_r", f"no resistance pays >= {MIN_BEST_R}R: best R {best_seen}")
    best = max(Decimal(str(t.r)) for t in targets)

    # 10. size: the smallest of four bounds, ties in this order
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

    others = ", ".join(BOUND_LABELS[name] for name, _ in bounds if name != bound)
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
        size_basis=f"size: {BOUND_LABELS[bound]}-bound ({others} not binding)",
        loss_at_disaster_pct=float(loss_pct),
        extended=extended,
        entry_for_max_risk=None if entry_for_max_risk is None else float(entry_for_max_risk),
    )
