"""
TradingFirm — plan math rerun over stored verdicts (Part 4.8a decision 6,
extended in 4.8a-de decision 8). No LLM, no network, no database: rows in
on stdin, tables out on stdout.

    docker exec tf-postgres ... psql -At -c "SET default_transaction_read_only = on; SELECT json_agg(...) ..." > rows.json
    docker exec -i tf-ai-agent-dev python -m scripts.plan_math_rerun \
        --prev /tmp/plan_math_v2.py < rows.json

Each stdin row: {"verdictId", "ticker", "entry", "indicators", "fresh"?}
where `indicators` is the stored dossier's `sections.indicators`
(data-engine's raw names: `atr14`, `ema20`, `zones`, `lastSwingLow`) and
`fresh`, optional, is the same shape recomputed from the stored bars by
data-engine's scripts/snapshot_from_bars.py (full-history zones with
touches / held / broke / lastTouch, and lastSwingLow). `--prev` is an
earlier grading/plan_math.py saved from git, labelled by its own
PLAN_MATH_VERSION; without it only the module in the image runs.

Rows per verdict (4.8a-de decision 8): the previous module on the stored
inputs (what shipped); the previous module on the fresh inputs WITH
lastSwingLow withheld (zone drift alone: approval change 4); the current
module on the fresh inputs. Plus, per ticker, the stored nearest support
and resistance against the fresh nearest (approval change 11).

The account and risk % are PLACEHOLDERS (`--account`, `--risk-pct`): they
matter only to the size_zero branch. The tables never print a size, a
budget, a share count or the loss at disaster: validity is what they report.
"""

import argparse
import importlib.util
import json
import os
import sys
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

# Runnable as `python scripts/plan_math_rerun.py` from the service root too
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from grading import plan_math as current  # noqa: E402

PLACEHOLDER_ACCOUNT = 100_000.0
PLACEHOLDER_RISK_PCT = 1.0
COLUMNS = ("ticker", "entry", "inputs", "version", "stopRule", "stop", "riskAtr", "overhead",
           "ceiling", "t1", "extended", "swingLow", "valid", "reason")
ZONE_COLUMNS = ("ticker", "storedSupport", "freshSupport", "storedResistance", "freshResistance")


def load_module(path: str):
    spec = importlib.util.spec_from_file_location("plan_math_prev", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _label(module) -> str:
    return f"v{getattr(module, 'PLAN_MATH_VERSION', '?')}"


def _zones(indicators: dict) -> list:
    """Pooled and tagged with data-engine's label, as the analyst does."""
    zones = indicators.get("zones") or {}
    return [({**z, "side": side} if isinstance(z, dict) else z)
            for side in ("support", "resistance") for z in (zones.get(side) or [])]


def _two(value) -> Optional[float]:
    if value is None:
        return None
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _stop_rule(basis: str) -> str:
    if "EMA20" in basis:
        return "EMA20"
    if "swing low" in basis:
        return "swing low"
    return "support"


def _swing(indicators: dict, withhold: bool) -> tuple[Optional[float], Optional[str]]:
    swing = None if withhold else indicators.get("lastSwingLow")
    if not isinstance(swing, dict):
        return None, None
    return swing.get("price"), swing.get("date")


def summarize(result, entry: float, atr) -> dict:
    """What the table shows of one compute_plan answer, either version."""
    if hasattr(result, "reason"):
        return {"valid": False, "reason": f"{result.reason}: {result.detail}", "stop": None,
                "riskAtr": None, "stopRule": None, "t1": None, "overhead": None, "ceiling": None,
                "extended": None}
    risk = Decimal(str(entry)) - Decimal(str(result.stop))
    t1 = result.targets[0]
    # the ceiling target's own price: it may be T2 or T3, not T1
    ceiling = next((f"{t.price:.2f}" for t in result.targets if t.basis.endswith("ceiling")), None)
    extended = getattr(result, "extended", False)
    return {
        "valid": True, "reason": None,
        "stop": result.stop,
        "riskAtr": _two(risk / Decimal(str(atr))) if atr else None,
        "stopRule": _stop_rule(result.stop_basis),
        "t1": f"{t1.price:.2f} ({t1.r:.2f}R)",
        "overhead": len(getattr(result, "overhead", ()) or ()),
        "ceiling": ceiling,
        "extended": f"yes, wait for <= {getattr(result, 'entry_for_max_risk'):.2f}" if extended else "no",
    }


def _run(module, indicators: dict, entry: float, account: float, risk_pct: float, withhold_swing: bool) -> dict:
    atr = indicators.get("atr14")
    price, when = _swing(indicators, withhold_swing)
    kwargs = dict(entry=entry, atr=atr, zones=_zones(indicators), account=account, risk_pct=risk_pct,
                  ema20=indicators.get("ema20"), swing_low=price, swing_low_date=when)
    out = summarize(module.compute_plan(**kwargs), entry, atr)
    out["swingLow"] = None if price is None else f"{price:.2f} ({when})"
    return out


def _nearest(indicators: Optional[dict], side: str, entry: float) -> Optional[str]:
    """The zone nearest the entry on that side, as `low-high`."""
    if not isinstance(indicators, dict):
        return None
    zones = [z for z in ((indicators.get("zones") or {}).get(side) or []) if isinstance(z, dict)]
    if not zones:
        return "-"
    key = (lambda z: entry - z["high"]) if side == "support" else (lambda z: z["low"] - entry)
    z = min(zones, key=lambda z: abs(key(z)))
    hist = ""
    if z.get("held") is not None:
        hist = f" (touches {z.get('touches')}, held {z.get('held')}, broke {z.get('broke')})"
    return f"{z['low']:.2f}-{z['high']:.2f}{hist}"


def rerun_row(row: dict, prev, account: float, risk_pct: float) -> dict:
    """One stored verdict → its rows. A malformed row is an `error` string
    on the row, never a raise: the run continues."""
    out = {"verdictId": row.get("verdictId"), "ticker": row.get("ticker"), "entry": row.get("entry"),
           "error": None, "runs": [], "zones": None}
    indicators = row.get("indicators")
    if not isinstance(indicators, dict):
        out["error"] = "indicators missing or not an object"
        return out
    fresh = row.get("fresh")
    try:
        entry = float(row["entry"])
        if prev is not None:
            out["runs"].append({"inputs": "stored", "version": _label(prev),
                                **_run(prev, indicators, entry, account, risk_pct, withhold_swing=False)})
        if isinstance(fresh, dict):
            if prev is not None:
                out["runs"].append({"inputs": "fresh, no swing", "version": _label(prev),
                                    **_run(prev, fresh, entry, account, risk_pct, withhold_swing=True)})
            out["runs"].append({"inputs": "fresh", "version": _label(current),
                                **_run(current, fresh, entry, account, risk_pct, withhold_swing=False)})
            out["zones"] = {"storedSupport": _nearest(indicators, "support", entry),
                            "freshSupport": _nearest(fresh, "support", entry),
                            "storedResistance": _nearest(indicators, "resistance", entry),
                            "freshResistance": _nearest(fresh, "resistance", entry)}
        else:
            out["runs"].append({"inputs": "stored", "version": _label(current),
                                **_run(current, indicators, entry, account, risk_pct, withhold_swing=False)})
    except (ValueError, KeyError, TypeError) as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def _cell(value) -> str:
    return "-" if value is None else str(value)


def render(rows: list[dict]) -> str:
    lines = ["| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    for row in rows:
        if row["error"]:
            lines.append(f"| {row['ticker']} | {row['entry']} | - | - | error: {row['error']} |"
                         + " |" * (len(COLUMNS) - 5))
            continue
        for s in row["runs"]:
            cells = [row["ticker"], f"{float(row['entry']):.2f}", s["inputs"], s["version"], _cell(s["stopRule"]),
                     "-" if s["stop"] is None else f"{s['stop']:.2f}",
                     "-" if s["riskAtr"] is None else f"{s['riskAtr']:.2f}",
                     _cell(s["overhead"]), _cell(s["ceiling"]), _cell(s["t1"]), _cell(s["extended"]),
                     _cell(s["swingLow"]), "yes" if s["valid"] else "no", _cell(s["reason"])]
            lines.append("| " + " | ".join(cells) + " |")
    totals: dict[str, list[int]] = {}
    for row in rows:
        for s in row.get("runs") or []:
            key = f"{s['version']} on {s['inputs']}"
            t = totals.setdefault(key, [0, 0])
            t[0] += s["valid"]
            t[1] += 1
    lines.append("")
    lines.append("valid plans: " + "; ".join(f"{k} {v[0]} / {v[1]}" for k, v in totals.items()))
    if any(row.get("zones") for row in rows):
        lines += ["", "| " + " | ".join(ZONE_COLUMNS) + " |", "|" + "---|" * len(ZONE_COLUMNS)]
        for row in rows:
            z = row.get("zones")
            if z:
                lines.append("| " + " | ".join([row["ticker"], _cell(z["storedSupport"]), _cell(z["freshSupport"]),
                                                _cell(z["storedResistance"]), _cell(z["freshResistance"])]) + " |")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prev", help="path to an earlier grading/plan_math.py (from git)")
    parser.add_argument("--account", type=float, default=PLACEHOLDER_ACCOUNT, help="placeholder, never printed")
    parser.add_argument("--risk-pct", type=float, default=PLACEHOLDER_RISK_PCT, help="placeholder, never printed")
    args = parser.parse_args(argv)
    rows = json.load(sys.stdin)
    if not isinstance(rows, list):
        print("stdin must be a JSON list of rows", file=sys.stderr)
        return 2
    prev = load_module(args.prev) if args.prev else None
    print(render([rerun_row(r, prev, args.account, args.risk_pct) for r in rows]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
