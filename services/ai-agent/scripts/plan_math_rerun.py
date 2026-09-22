"""
TradingFirm — plan math v1 vs v2 rerun (Part 4.8a decision 6). No LLM, no
network, no database: rows in on stdin, a table out on stdout.

    docker exec tf-postgres psql -At -v ON_ERROR_STOP=1 tradingfirm \
        -c "SET default_transaction_read_only = on; SELECT json_agg(...) ..." > ten.json
    docker exec -i tf-ai-agent-dev python -m scripts.plan_math_rerun \
        --v1 /tmp/plan_math_v1.py < ten.json

Each stdin row: {"verdictId", "ticker", "entry", "indicators"} where
`indicators` is the stored dossier's `sections.indicators` (data-engine's
raw names: `atr14`, `ema20`, `zones`, and `lastSwingLow` once a later part
sends it). `--v1` is the pre-4.8a module saved from git; without it only v2
runs.

The account and risk % are PLACEHOLDERS (`--account`, `--risk-pct`): they
matter only to the size_zero branch. The table never prints a size, a
budget, a share count or the loss at disaster: validity is what it reports.
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

from grading import plan_math as v2  # noqa: E402

PLACEHOLDER_ACCOUNT = 100_000.0
PLACEHOLDER_RISK_PCT = 1.0
COLUMNS = ("ticker", "entry", "version", "stopRule", "stop", "riskAtr", "overhead", "t1", "valid", "reason")


def load_v1(path: str):
    spec = importlib.util.spec_from_file_location("plan_math_v1", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _zones(indicators: dict) -> list:
    zones = indicators.get("zones") or {}
    return list(zones.get("support") or []) + list(zones.get("resistance") or [])


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


def summarize(result, entry: float, atr) -> dict:
    """What the table shows of one compute_plan answer, either version."""
    if hasattr(result, "reason"):
        return {"valid": False, "reason": f"{result.reason}: {result.detail}", "stop": None,
                "riskAtr": None, "stopRule": None, "t1": None, "overhead": None}
    risk = Decimal(str(entry)) - Decimal(str(result.stop))
    t1 = result.targets[0]
    return {
        "valid": True, "reason": None,
        "stop": result.stop,
        "riskAtr": _two(risk / Decimal(str(atr))) if atr else None,
        "stopRule": _stop_rule(result.stop_basis),
        "t1": f"{t1.price:.2f} ({t1.r:.2f}R)",
        "overhead": len(getattr(result, "overhead", ()) or ()),
    }


def rerun_row(row: dict, v1, account: float, risk_pct: float) -> dict:
    """One stored verdict → its v1 and v2 answers. A malformed row is an
    `error` string on the row, never a raise: the run continues."""
    out = {"verdictId": row.get("verdictId"), "ticker": row.get("ticker"), "entry": row.get("entry"),
           "error": None, "v1": None, "v2": None}
    indicators = row.get("indicators")
    if not isinstance(indicators, dict):
        out["error"] = "indicators missing or not an object"
        return out
    try:
        entry = float(row["entry"])
        atr = indicators.get("atr14")
        zones = _zones(indicators)
        swing = indicators.get("lastSwingLow")
        swing_price = swing.get("price") if isinstance(swing, dict) else None
        swing_date = swing.get("date") if isinstance(swing, dict) else None
        out["v2"] = summarize(v2.compute_plan(
            entry=entry, atr=atr, zones=zones, account=account, risk_pct=risk_pct,
            ema20=indicators.get("ema20"), swing_low=swing_price, swing_low_date=swing_date), entry, atr)
        if v1 is not None:
            out["v1"] = summarize(v1.compute_plan(
                entry=entry, atr=atr, zones=zones, account=account, risk_pct=risk_pct), entry, atr)
    except (ValueError, KeyError, TypeError) as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def render(rows: list[dict]) -> str:
    lines = ["| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    for row in rows:
        if row["error"]:
            lines.append(f"| {row['ticker']} | {row['entry']} | - | error: {row['error']} |" + " |" * (len(COLUMNS) - 4))
            continue
        for version in ("v1", "v2"):
            s = row[version]
            if s is None:
                continue
            cells = [row["ticker"], f"{float(row['entry']):.2f}", version, s["stopRule"] or "-",
                     "-" if s["stop"] is None else f"{s['stop']:.2f}",
                     "-" if s["riskAtr"] is None else f"{s['riskAtr']:.2f}",
                     "-" if s["overhead"] is None else str(s["overhead"]),
                     s["t1"] or "-", "yes" if s["valid"] else "no", s["reason"] or "-"]
            lines.append("| " + " | ".join(cells) + " |")
    valid = {v: sum(1 for r in rows if r[v] and r[v]["valid"]) for v in ("v1", "v2")}
    n = sum(1 for r in rows if not r["error"])
    lines.append(f"\nvalid plans: v1 {valid['v1']} / {n}, v2 {valid['v2']} / {n}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--v1", help="path to the pre-4.8a grading/plan_math.py (from git)")
    parser.add_argument("--account", type=float, default=PLACEHOLDER_ACCOUNT, help="placeholder, never printed")
    parser.add_argument("--risk-pct", type=float, default=PLACEHOLDER_RISK_PCT, help="placeholder, never printed")
    args = parser.parse_args(argv)
    rows = json.load(sys.stdin)
    if not isinstance(rows, list):
        print("stdin must be a JSON list of rows", file=sys.stderr)
        return 2
    v1 = load_v1(args.v1) if args.v1 else None
    print(render([rerun_row(r, v1, args.account, args.risk_pct) for r in rows]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
