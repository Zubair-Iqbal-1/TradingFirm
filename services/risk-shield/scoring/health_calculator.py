"""
TradingFirm — the market health score (Part 3.3, spec decision 6).

score = round-half-up of Σ scoreᵢ·weightᵢ / W over the monitors that could
score, in integer arithmetic (weights are integer percents), where W is
their total weight. A monitor with no score is left out and the rest
renormalize — never counted as 0. Below COVERAGE_FLOOR there is no score
and no regime.

compute_health() is the one call 3.4's scheduler makes; nothing calls it
in 3.3. It reads quotes only through get_quotes_view, so it never raises
for a source state.
"""

import logging
from datetime import datetime, timezone
from typing import Callable

from monitors.quotes import get_quotes_view
from monitors.regime import MONITORS
from scoring.overlay import futures_prices
from scoring.regime_classifier import classify
from scoring.weekend import vix5d

logger = logging.getLogger(__name__)

WEIGHTS = {name: m.weight for name, m in MONITORS.items()}
COVERAGE_FLOOR = 70    # provisional (spec 3.3 decision 6)


def run_monitors(view: dict) -> dict:
    """Every monitor on one view. A monitor that raises is a bug, not a
    data state: it is logged and scores null, and the others continue."""
    results = {}
    for name, m in MONITORS.items():
        try:
            results[name] = m.fn(view)
        except Exception as e:
            logger.error(f"Monitor {name} raised {type(e).__name__}: {e}")
            results[name] = {"score": None, "raw": {}, "detail": f"monitor error: {type(e).__name__}",
                             "stale": False}
    return results


def health_from_monitors(results: dict) -> dict:
    """{score, regime, coverage, stale, staleMonitors, monitors} from one
    result per monitor in MONITORS. Pure."""
    contributing = [n for n in MONITORS if results[n]["score"] is not None]
    coverage = sum(WEIGHTS[n] for n in contributing)
    if coverage >= COVERAGE_FLOOR:
        total = sum(results[n]["score"] * WEIGHTS[n] for n in contributing)
        score = (2 * total + coverage) // (2 * coverage)    # round half up, no float
    else:
        score = None
    stale_monitors = [n for n in contributing if results[n]["stale"]]
    return {
        "score": score,
        "regime": classify(score),
        "coverage": coverage,
        "stale": bool(stale_monitors),
        "staleMonitors": stale_monitors,
        "monitors": {n: {**results[n], "weight": WEIGHTS[n]} for n in MONITORS},
    }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def compute_health(r, memory, *, now: Callable[[], datetime] = _utc_now) -> dict:
    """The health snapshot: the quotes view → the six monitors → the score,
    plus checkedAt and where the inputs came from."""
    view = await get_quotes_view(r, memory, now=now)
    health = health_from_monitors(run_monitors(view))
    health["checkedAt"] = now().isoformat()
    # The futures prices this check saw, for the row and for 3.4b's cap. No
    # score depends on them here: the cap is applied by the scheduler.
    health["futures"] = futures_prices(view)
    # 3.4c: the VIX snapshot the weekend block reads — the live (partial)
    # level and the last complete closes behind it. No monitor scores it.
    health["vix5d"] = vix5d(view)
    health["inputs"] = {
        "asOf": view["asOf"],
        "source": view["source"],
        "reason": view["reason"],
        "staleTickers": view["staleTickers"],
    }
    if health["score"] is None:
        logger.warning(f"Health: no score, coverage {health['coverage']} < {COVERAGE_FLOOR}")
    return health
