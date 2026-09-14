"""
TradingFirm — the futures overlay (Part 3.4b decision 4).

A check's score is capped by how far ES=F / NQ=F have moved since the settle
whose bars the monitors read:

    move ≤ −1.5 %  → at most CAUTIOUS (69)
    move ≤ −3.0 %  → at most DANGER   (39)
    move ≤ −5.0 %  → CRITICAL         (19)

score = min(base, cap), so the overlay can only ever lower a score. The move is
the worse of the two contracts, measured against prices this service stored
itself (every check writes the futures prices it saw), never against a bar
label — how yfinance dates an evening bar is not part of the arithmetic.

The numbers are provisional, like 3.3's: Phase 6's journal retunes them.

Pure: no clock, no Redis, no Postgres, nothing mutated.
"""

import math
from typing import Any, Optional

FUTURES_TICKERS = ("ES=F", "NQ=F")

# (move %, cap), first match on a move at or below the edge. Each cap is the top
# of a 3.3 regime, so a band maps to exactly one regime.
CAP_BANDS = ((-5.0, 19), (-3.0, 39), (-1.5, 69))

STATUS_APPLIED = "applied"              # a cap is in force
STATUS_WITHIN = "within"                # the move is above the first edge
STATUS_NO_REFERENCE = "no_reference"    # the settle base carries no usable futures price
STATUS_UNAVAILABLE = "unavailable"      # no fresh price to compare with


def _number(value: Any) -> Optional[float]:
    """A finite float, or None. Strings and bools are wrong shapes, not prices."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _price(block: Any) -> Optional[float]:
    """A usable price out of one {price, date, asOf, stale} block."""
    if not isinstance(block, dict):
        return None
    price = _number(block.get("price"))
    return price if price is not None and price > 0 else None


def futures_prices(view: dict) -> dict:
    """{ticker: {price, date, asOf, stale} or None} from a quotes view, for the
    row's `futures` block and for the cap. A stale (last-known) or missing
    ticker is None: an hours-old price would be a wrong cap, not a stale one."""
    stale = set(view.get("staleTickers") or [])
    out: dict[str, Optional[dict]] = {}
    for ticker in FUTURES_TICKERS:
        entry = (view.get("tickers") or {}).get(ticker)
        out[ticker] = None
        if not isinstance(entry, dict) or ticker in stale or entry.get("stale"):
            continue
        closes = entry.get("close") or []
        price = _number(closes[-1]) if closes else None
        if price is None or price <= 0:
            continue
        dates = entry.get("date") or []
        out[ticker] = {"price": price, "date": dates[-1] if dates else None,
                       "asOf": entry.get("asOf"), "stale": False}
    return out


def move_pct(reference: Any, current: Any) -> Optional[float]:
    """(price − reference) × 100 / reference for one ticker, multiplied before
    dividing so the band edges are exact. None when either side is unusable."""
    ref, now = _price(reference), _price(current)
    if ref is None or now is None:
        return None
    return (now - ref) * 100 / ref


def cap_for(move: Optional[float]) -> Optional[int]:
    """The cap for a move, or None when it is above the first edge."""
    if move is None:
        return None
    for edge, cap in CAP_BANDS:
        if move <= edge:
            return cap
    return None


def apply_overlay(base: Optional[int], reference: Any, current: dict) -> tuple[Optional[int], dict]:
    """
    (score, record) for one check. `base` is the monitors' score (a market
    check) or the settle's score (a night check); `reference` is the settle
    base's stored `futures` block; `current` is futures_prices() of the view
    just downloaded. The record is what the row, the payload and the endpoints
    carry: {status, movePct, esPct, nqPct, base, cap, capped}.
    """
    per = {ticker: move_pct((reference or {}).get(ticker), current.get(ticker))
           for ticker in FUTURES_TICKERS}
    record = {"status": STATUS_UNAVAILABLE, "movePct": None,
              "esPct": per["ES=F"], "nqPct": per["NQ=F"],
              "base": base, "cap": None, "capped": False}

    if not any(_price((reference or {}).get(ticker)) for ticker in FUTURES_TICKERS):
        record["status"] = STATUS_NO_REFERENCE
        return base, record
    moves = [move for move in per.values() if move is not None]
    if not moves:
        return base, record

    move = min(moves)                      # the worse of the two contracts
    cap = cap_for(move)
    score = base if cap is None or base is None else min(base, cap)
    record.update(status=STATUS_APPLIED if cap is not None else STATUS_WITHIN,
                  movePct=move, cap=cap, capped=score != base)
    return score, record
