"""
TradingFirm — journal stats (Part 4.5). No I/O: rows in, one answer out.

Every group carries the model (spec 4.5 decision 13) and, from 4.8a, the
plan-math version: one `models[]` entry per model × planMathVersion, with
verdict × horizon groups and calibration per horizon inside it. No rate or
mean is ever computed across models or across versions — 4.8c puts two
models' verdicts on the same tickers, and v2's T1 / target-cap / far-support
rules are never averaged with v1's plans. `stopHitByRiskAtr` (4.8a-9) buckets
each plan by its risk in ATRs (<1.5 / 1.5-2 / >2) and gives the stop-hit
rate per horizon and bucket.

    hit   go → return > 0; avoid → return < 0; exactly 0 is a miss;
          wait → null (a wait has no direction to be right about)
    rates 0–1 at 3 dp; null on a zero denominator, never 0; n beside each
    pending  unscored and not expired (not yet due, or due and waiting)
    expired  unscored, target more than 10 sessions old (decision 5)
"""

import statistics
from decimal import Decimal
from typing import Optional

from journal import sessions

VERDICTS = ("go", "wait", "avoid")
HORIZONS = sessions.HORIZONS
BUCKETS = ((0, 49), (50, 59), (60, 69), (70, 79), (80, 89), (90, 100))
RISK_BUCKETS = ("<1.5", "1.5-2", ">2")
DEFAULT_PLAN_MATH_VERSION = 1


def risk_atr(entry, stop, atr) -> Optional[Decimal]:
    """(entry − stop) ÷ ATR14 for a row with a plan; None without a stop, a
    positive ATR, or a positive risk."""
    try:
        e, s, a = (Decimal(str(v)) for v in (entry, stop, atr))
    except Exception:
        return None
    if not (e.is_finite() and s.is_finite() and a.is_finite()) or a <= 0 or e - s <= 0:
        return None
    return (e - s) / a


def risk_bucket(value: Optional[Decimal]) -> Optional[str]:
    if value is None:
        return None
    if value < Decimal("1.5"):
        return RISK_BUCKETS[0]
    if value <= Decimal("2"):
        return RISK_BUCKETS[1]
    return RISK_BUCKETS[2]


def _rate(hits: int, n: int) -> Optional[float]:
    return round(hits / n, 3) if n else None


def _num(value) -> Optional[float]:
    return None if value is None else round(float(value), 3)


def is_hit(verdict: str, return_pct) -> Optional[bool]:
    if verdict == "go":
        return return_pct > 0
    if verdict == "avoid":
        return return_pct < 0
    return None


def _group(verdicts: list[dict], horizon: int, latest, today) -> dict:
    scored, pending, expired = [], 0, 0
    for v in verdicts:
        outcome = v["outcomes"].get(horizon)
        if outcome is not None:
            scored.append((v, outcome))
            continue
        day0, _ = sessions.entry_session(v["asked_at"])
        state, _ = sessions.horizon_state(day0, horizon, latest, today)
        if state == sessions.EXPIRED:
            expired += 1
        else:
            pending += 1
    returns = [Decimal(o["return_pct"]) for _, o in scored if o["return_pct"] is not None]
    kind = verdicts[0]["verdict"] if verdicts else None
    hits = [is_hit(kind, r) for r in returns]
    planned = [o for v, o in scored if v["has_plan"] and o["stop_hit"] is not None]
    rs = [Decimal(o["r_multiple"]) for o in planned if o["r_multiple"] is not None]
    return {
        "asked": len(verdicts), "scored": len(scored), "pending": pending, "expired": expired,
        "meanReturnPct": _num(sum(returns) / len(returns)) if returns else None,
        "medianReturnPct": _num(statistics.median(returns)) if returns else None,
        "hitRate": None if kind == "wait" else _rate(sum(1 for h in hits if h), len(hits)),
        "withPlan": len(planned),
        "stopHitRate": _rate(sum(1 for o in planned if o["stop_hit"]), len(planned)),
        "targetHitRate": _rate(sum(1 for o in planned if o["target_hit"]), len(planned)),
        "targetFirstRate": _rate(sum(1 for o in planned if o["first_hit"] == "target"), len(planned)),
        "avgR": _num(sum(rs) / len(rs)) if rs else None,
    }


def _calibration(verdicts: list[dict], horizon: int) -> list[dict]:
    out = []
    for lo, hi in BUCKETS:
        n, hits, conf = 0, 0, 0
        for v in verdicts:
            outcome = v["outcomes"].get(horizon)
            if v["verdict"] not in ("go", "avoid") or outcome is None or outcome["return_pct"] is None:
                continue
            if not lo <= v["confidence"] <= hi:
                continue
            n += 1
            conf += v["confidence"]
            hits += bool(is_hit(v["verdict"], Decimal(outcome["return_pct"])))
        out.append({"bucket": f"{lo}-{hi}", "n": n,
                    "meanConfidence": round(conf / n, 1) if n else None,
                    "hitRate": _rate(hits, n)})
    return out


def fold(rows: list[dict]) -> list[dict]:
    """The LEFT JOIN's rows (one per verdict × outcome, or one per verdict
    with none) → one dict per verdict with its outcomes by horizon."""
    by_id: dict = {}
    for r in rows:
        v = by_id.setdefault(r["id"], {
            "id": r["id"], "model": r["model"], "verdict": r["verdict"],
            "confidence": r["confidence"], "asked_at": r["asked_at"],
            "has_plan": bool(r["has_plan"]), "outcomes": {},
            "plan_math_version": int(r.get("plan_math_version") or DEFAULT_PLAN_MATH_VERSION),
            "risk_bucket": risk_bucket(risk_atr(r.get("entry"), r.get("plan_stop"), r.get("atr14")))
            if r["has_plan"] else None,
        })
        if r.get("horizon_days") is not None:
            v["outcomes"][int(r["horizon_days"])] = r
    return list(by_id.values())


def _stop_hit_by_risk(verdicts: list[dict], horizon: int) -> dict:
    out = {}
    for bucket in RISK_BUCKETS:
        scored = [v["outcomes"][horizon] for v in verdicts
                  if v["risk_bucket"] == bucket and v["outcomes"].get(horizon) is not None
                  and v["outcomes"][horizon]["stop_hit"] is not None]
        out[bucket] = {"n": len(scored),
                       "stopHitRate": _rate(sum(1 for o in scored if o["stop_hit"]), len(scored))}
    return out


def compute(rows: list[dict], now, days: int) -> dict:
    verdicts = fold(rows)
    latest, today = sessions.latest_closed_session(now), sessions.et_date(now)
    models = []
    for model, version in sorted({(v["model"], v["plan_math_version"]) for v in verdicts}):
        mine = [v for v in verdicts if v["model"] == model and v["plan_math_version"] == version]
        models.append({
            "model": model,
            "planMathVersion": version,
            "byVerdict": {
                kind: {str(h): _group([v for v in mine if v["verdict"] == kind], h, latest, today)
                       for h in HORIZONS}
                for kind in VERDICTS
            },
            "calibration": {str(h): _calibration(mine, h) for h in HORIZONS},
            "stopHitByRiskAtr": {str(h): _stop_hit_by_risk(mine, h) for h in HORIZONS},
        })
    dates = [o["session_date"] for v in verdicts for o in v["outcomes"].values()
             if o.get("session_date") is not None]
    return {"days": days, "asOf": now.isoformat(),
            "scoredThrough": max(dates).isoformat() if dates else None,
            "models": models}
