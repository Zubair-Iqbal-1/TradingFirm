"""
TradingFirm — the indicator snapshot from stored bar rows (Part 4.8a-de,
spec decision 8). No database, no provider, no network: bar rows in on
stdin, the camelCase snapshot out on stdout.

    docker exec -i tf-postgres bash -c '... SELECT json_agg(...) ...' > /tmp/bars.json
    docker exec -i tf-data-engine-dev python -m scripts.snapshot_from_bars \
        < /tmp/bars.json > /tmp/fresh.json

Each stdin row: {"ticker", "asOf", "bars": [{ts, open, high, low, close,
volume}, ...], "storedClose"?}. Bars with a date after `asOf` are dropped,
so the snapshot is the one the dossier of that day would have carried (with
today's zone rules). `storedClose` is the close the stored dossier carried:
the row reports `closeMismatch` when the recomputed close differs by a cent
or more. A malformed row is an `error` string on the row, never a raise.

Benchmarks are not given, so the RS fields are null; plan math reads none
of them. The account never appears here: this is data-engine.
"""

import json
import os
import sys
from datetime import date, datetime, timezone
from typing import Optional, TextIO

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import bars_to_df  # noqa: E402
from indicators import IndicatorsResponse, swing_snapshot  # noqa: E402


def _bar_date(ts: str) -> date:
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).date()


def snapshot_row(row: dict) -> dict:
    out = {"ticker": row.get("ticker"), "asOf": row.get("asOf"), "error": None,
           "closeMismatch": None, "indicators": None}
    bars = row.get("bars")
    if not isinstance(bars, list) or not bars:
        out["error"] = "bars missing or empty"
        return out
    try:
        as_of: Optional[date] = date.fromisoformat(str(row["asOf"])[:10]) if row.get("asOf") else None
        kept = [b for b in bars if as_of is None or _bar_date(b["ts"]) <= as_of]
        if not kept:
            out["error"] = f"no bar on or before asOf {as_of}"
            return out
        rows = [{**b, "ts": datetime.fromisoformat(str(b["ts"]).replace("Z", "+00:00"))} for b in kept]
        df = bars_to_df(rows)
        snap = swing_snapshot(df)
        body = IndicatorsResponse(
            ticker=str(row.get("ticker") or ""), as_of=rows[-1]["ts"],
            computed_at=datetime.now(timezone.utc), **snap,
        ).model_dump(mode="json", by_alias=True)
        del df, rows
        stored = row.get("storedClose")
        if stored is not None and body["close"] is not None:
            out["closeMismatch"] = abs(float(stored) - float(body["close"])) >= 0.01
        else:
            out["closeMismatch"] = False
        out["indicators"] = body
    except (KeyError, TypeError, ValueError) as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def main(stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> int:
    rows = json.load(stdin)
    if not isinstance(rows, list):
        print("stdin must be a JSON list of rows", file=sys.stderr)
        return 2
    json.dump([snapshot_row(r) for r in rows], stdout)
    stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
