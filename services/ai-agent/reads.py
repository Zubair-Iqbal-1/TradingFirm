"""
TradingFirm — the reads: data-engine's measurements turned into an uptrend
call and six flags (Part 4.8b-ai, spec 4.8b decision 5; spec 4.8b-ai
decisions 1–3). Pure: no I/O, no clock unless passed in.

data-engine measures (`volumeRead`, `trendRead`, `momentumRead`, `rangeRead`
on the indicator snapshot); this module judges. The starting lines below are
provisional and live here, not in data-engine, so one can move without a
data-engine rebuild. Every line is a constant, all of them sit in THRESHOLDS,
and READS_VERSION is stamped on every verdict and sits in the cache
fingerprint: **bump it on any change to a constant or a flag rule**, the
PLAN_MATH_VERSION rule. A change without a bump mixes eras in the journal
(test_reads_version_is_pinned).

**No flag is a rule.** Nothing here rejects a plan, changes a level or
removes `go`; the model reads the flags and weighs them (the prompt says so).
`sessionSoFar` and `rsSpy5` are never read here.
"""

import math
from datetime import date, datetime
from typing import Any, Optional

from journal import sessions

READS_VERSION = 1

# ── The starting lines (spec 4.8b-ai decision 1) ─────────────────
LOW_VOLUME_BREAKOUT_RVOL = 1.0     # breakout.barRvol < this
DISTRIBUTION_RATIO = 1.5           # downDays5Rvol > this × upDays5Rvol
DRY_PULLBACK_DAYS = 2              # pullbackDays >= this ...
DRY_PULLBACK_RVOL = 0.7            # ... and pullbackRvol < this
DEAD_MOVE_ATR = 1.5                # abs(move30Atr) < this ...
DEAD_RANGE_ATR = 5                 # ... and range30Atr < this
BLEEDING_MOVE_ATR = -1.5           # move30Atr <= this ...
BLEEDING_CLOSES_BELOW = 10         # ... and closesBelowEma20 >= this, and lowerHighs
RANGE_POS_LOW = 0.2                # this <= posFrac <= RANGE_POS_HIGH ...
RANGE_POS_HIGH = 0.8
RANGE_CROSSES = 5                  # ... and ema20Crosses40 >= this, and not closedOutside

THRESHOLDS = (
    LOW_VOLUME_BREAKOUT_RVOL, DISTRIBUTION_RATIO, DRY_PULLBACK_DAYS, DRY_PULLBACK_RVOL,
    DEAD_MOVE_ATR, DEAD_RANGE_ATR, BLEEDING_MOVE_ATR, BLEEDING_CLOSES_BELOW,
    RANGE_POS_LOW, RANGE_POS_HIGH, RANGE_CROSSES,
)

# The flags in the order they are listed, and which of them read the last
# bar's volume (withheld while that bar is partial).
FLAGS = ("lowVolumeBreakout", "distribution", "dryPullback", "dead", "bleeding", "rangeBound")
VOLUME_FLAGS = frozenset({"lowVolumeBreakout", "distribution", "dryPullback"})
# caution / positive, for the legend; nothing in code reads it
FLAG_READING = {"lowVolumeBreakout": "caution", "distribution": "caution", "dryPullback": "positive",
                "dead": "caution", "bleeding": "caution", "rangeBound": "caution"}

PARTIAL_REASON = "partial bar"


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _flag(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def _block(indicators: dict, name: str) -> dict:
    block = indicators.get(name)
    return block if isinstance(block, dict) else {}


def _fmt(value: Optional[float], signed: bool = False) -> str:
    if value is None:
        return "unknown"
    return f"{value:+.2f}" if signed else f"{value:.2f}"


def _mark(ok: Optional[bool]) -> str:
    return "✓" if ok else "✗"


# ── The uptrend and its four reasons ─────────────────────────────

def uptrend(indicators: dict) -> tuple[bool, list[str]]:
    """(uptrend, four reasons). True only when all four checks hold; a null
    input is a failed check, never a pass (spec 4.8b decision 5)."""
    trend = _block(indicators, "trendRead")
    stack = _flag(trend.get("stackUp"))
    rising = _flag(trend.get("ema20Rising10"))
    rs = _num(indicators.get("rsSpy20"))
    rs_ok = rs is not None and rs > 0
    higher = _flag(trend.get("higherLows"))
    lows = [x for x in (trend.get("swingLows") or []) if _num(x) is not None]

    reasons = [
        f"close {_fmt(_num(indicators.get('close')))} > EMA20 {_fmt(_num(indicators.get('ema20')))} "
        f"> EMA50 {_fmt(_num(indicators.get('ema50')))} {_mark(stack)}",
        f"EMA20 rising over 10 bars {_mark(rising)} ({_fmt(_num(trend.get('ema20Slope10Atr')), signed=True)} ATR)",
        f"RS 20d vs SPY {'unknown' if rs is None else _fmt(rs, signed=True) + ' %'} {_mark(rs_ok)}",
        f"higher swing lows {_mark(higher)}"
        + (f" ({_fmt(float(lows[0]))} → {_fmt(float(lows[1]))})" if len(lows) >= 2 else " (unknown)"),
    ]
    return bool(stack and rising and rs_ok and higher), reasons


# ── The flags ────────────────────────────────────────────────────

def _evaluate(indicators: dict) -> dict[str, Optional[bool]]:
    """Each flag → True (raised), False (not raised) or None (an input is
    missing, so the flag is absent)."""
    vol = _block(indicators, "volumeRead")
    mom = _block(indicators, "momentumRead")
    rng = _block(indicators, "rangeRead")
    out: dict[str, Optional[bool]] = {}

    breakout = vol.get("breakout")
    if "breakout" not in vol:
        out["lowVolumeBreakout"] = None
    elif not isinstance(breakout, dict):
        out["lowVolumeBreakout"] = False              # no breakout in the last 5 bars
    else:
        bar = _num(breakout.get("barRvol"))
        out["lowVolumeBreakout"] = None if bar is None else bar < LOW_VOLUME_BREAKOUT_RVOL

    up, down = _num(vol.get("upDays5Rvol")), _num(vol.get("downDays5Rvol"))
    out["distribution"] = None if up is None or down is None else down > DISTRIBUTION_RATIO * up

    days = vol.get("pullbackDays")
    days = days if isinstance(days, int) and not isinstance(days, bool) else None
    pb_rvol = _num(vol.get("pullbackRvol"))
    if days is None:
        out["dryPullback"] = None
    elif days < DRY_PULLBACK_DAYS:
        out["dryPullback"] = False                    # no run, so no dry run
    else:
        out["dryPullback"] = None if pb_rvol is None else pb_rvol < DRY_PULLBACK_RVOL

    move, span = _num(mom.get("move30Atr")), _num(mom.get("range30Atr"))
    out["dead"] = None if move is None or span is None else abs(move) < DEAD_MOVE_ATR and span < DEAD_RANGE_ATR

    below = mom.get("closesBelowEma20")
    below = below if isinstance(below, int) and not isinstance(below, bool) else None
    lower = _flag(mom.get("lowerHighs"))
    out["bleeding"] = (None if move is None or below is None or lower is None
                       else move <= BLEEDING_MOVE_ATR and below >= BLEEDING_CLOSES_BELOW and lower)

    pos, crosses, outside = _num(rng.get("posFrac")), rng.get("ema20Crosses40"), _flag(rng.get("closedOutside"))
    crosses = crosses if isinstance(crosses, int) and not isinstance(crosses, bool) else None
    out["rangeBound"] = (None if pos is None or crosses is None or outside is None
                         else RANGE_POS_LOW <= pos <= RANGE_POS_HIGH and crosses >= RANGE_CROSSES and not outside)
    return out


def last_bar_partial(as_of: Any, today: date, now: datetime) -> bool:
    """True when the dossier's last bar is today's open XNYS session (spec
    4.8b decision 5; a second guard since 4.8b-de stopped storing that bar).
    False on anything it cannot read."""
    try:
        bar_day = date.fromisoformat(str(as_of)[:10])
        if bar_day != today or not sessions.is_session(bar_day, today):
            return False
        return now < sessions.session_close(bar_day)
    except (TypeError, ValueError, KeyError):
        return False


def build(indicators: dict, *, today: date, now: datetime) -> dict:
    """The `reads` block of the verdict document. Reads data-engine's raw
    indicator section (its own key names, before projection). Never mutates
    it; the same inputs give the same output."""
    indicators = indicators if isinstance(indicators, dict) else {}
    partial = last_bar_partial(indicators.get("asOf"), today, now)
    up, reasons = uptrend(indicators)
    evaluated = _evaluate(indicators)
    flags, withheld = [], []
    for name in FLAGS:
        value = evaluated[name]
        if value is None:
            continue
        if partial and name in VOLUME_FLAGS:
            withheld.append(name)
        elif value:
            flags.append(name)
    return {
        "uptrend": up,
        "trendReasons": reasons,
        "flags": flags,
        "withheld": withheld,
        "lastBarPartial": partial,
    }
