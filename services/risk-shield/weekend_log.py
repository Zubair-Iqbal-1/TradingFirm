"""
TradingFirm — the weekend log (Part 3.4c decision 11).

The point of the levels is to find out, in a few months, whether HIGH
actually predicted bad weekends. This module answers that from rows that
already exist: no new table, no Monday job, nothing that can rot.

Per weekend-eve session it pairs

    levelAtClose   the last market row starting **strictly before** the
                   close — 15:55 normally, 12:55 on a half day. This is
                   the actionable one: at 16:00:0x the bell has rung.
    levelAtSettle  the 16:20 row's level, recorded beside it.

with the move from that settle's stored ES=F / NQ=F prices to the first
row of the next session — a 16:20 → 09:30 gap proxy, which is what a
weekend headline actually does to an open. SPY's own gap is not stored
and storing it would be a new write.

The `summary` runs on **levelAtClose** (Change 3), so the question it
answers is literally "did HIGH-at-15:55 predict a bad Monday".

Pure: the rows come from db.py, the clock from the caller.
"""

import logging
from datetime import date, datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from scoring import overlay, weekend

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

STATUS_SCORED = "scored"        # a level and a move
STATUS_PENDING = "pending"      # the next session has not opened yet
STATUS_UNSCORED = "unscored"    # a level but no usable move, or no level

MAX_REASON_CODES = 8


def _et_date(at: datetime) -> date:
    return at.astimezone(ET).date()


def _move(reference: Any, current: Any, ticker: str) -> Optional[float]:
    """One contract's % move between two stored futures blocks."""
    return overlay.move_pct((reference or {}).get(ticker), (current or {}).get(ticker))


def _level_of(row: Optional[dict]) -> Optional[str]:
    block = (row or {}).get("weekend") or {}
    level = block.get("level")
    return level if level in weekend.LEVELS else None


def _close_at(row: dict) -> Optional[datetime]:
    """The session close the block itself recorded, so the log never
    re-derives a calendar it does not need."""
    raw = ((row.get("weekend") or {}).get("inputs") or {}).get("closeAt")
    try:
        return datetime.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        return None


def group_sessions(rows: list[dict]) -> dict:
    """Weekend-block rows → {ET close date: {close_row, settle_row}}. The
    close row is the last market row before that session's close; the settle
    row is the day's settle. Rows arrive ascending."""
    sessions: dict[date, dict] = {}
    for row in rows:
        if _level_of(row) is None:
            continue
        day = _et_date(row["checkedAt"])
        session = sessions.setdefault(day, {"close_row": None, "settle_row": None})
        if row.get("kind") == "settle":
            session["settle_row"] = row
            continue
        close_at = _close_at(row)
        if close_at is not None and row["checkedAt"] >= close_at:
            continue                      # the 16:00 row: the bell has rung
        session["close_row"] = row        # ascending, so the last one wins
    return sessions


def next_session_row(first_rows: list[dict], after: datetime) -> Optional[dict]:
    """The first market row of the first ET date after `after`."""
    later = [row for row in first_rows if row["checkedAt"] > after]
    return later[0] if later else None


def build_rows(weekend_rows: list[dict], first_rows: list[dict]) -> list[dict]:
    """One log row per weekend-eve session, newest first."""
    out = []
    for day, session in sorted(group_sessions(weekend_rows).items(), reverse=True):
        close_row, settle_row = session["close_row"], session["settle_row"]
        source = close_row or settle_row            # the actionable block, else the settle's
        block = (source or {}).get("weekend") or {}
        inputs = block.get("inputs") or {}
        reference = (settle_row or source or {}).get("futures")
        anchor = (settle_row or source or {})["checkedAt"]
        nxt = next_session_row(first_rows, anchor)

        es = nq = None
        if nxt is not None:
            es = _move(reference, nxt.get("futures"), "ES=F")
            nq = _move(reference, nxt.get("futures"), "NQ=F")
        if nxt is None:
            status = STATUS_PENDING
        elif es is None and nq is None:
            status = STATUS_UNSCORED
        else:
            status = STATUS_SCORED

        out.append({
            "closeDate": day.isoformat(),
            "levelAtClose": _level_of(close_row),
            "levelAtSettle": _level_of(settle_row),
            "points": block.get("points"),
            "reasons": [r.get("code") for r in (block.get("reasons") or [])][:MAX_REASON_CODES],
            "gapHours": inputs.get("gapHours"),
            "esMovePct": es,
            "nqMovePct": nq,
            "nextOpenAt": nxt["checkedAt"].isoformat() if nxt else None,
            "nextOpenScore": nxt["score"] if nxt else None,
            "nextOpenRegime": nxt["regime"] if nxt else None,
            "status": status,
        })
    return out


def summarize(rows: list[dict]) -> dict:
    """Per level, over scored rows, graded on **levelAtClose**: how many,
    the mean and the worst ES=F move. `disagreements` counts the weekends
    whose two levels differ — the first thing that will look suspicious,
    so it is counted rather than hidden."""
    levels: dict[str, dict] = {}
    for level in weekend.LEVELS:
        moves = [row["esMovePct"] for row in rows
                 if row["levelAtClose"] == level and row["status"] == STATUS_SCORED
                 and row["esMovePct"] is not None]
        levels[level] = {
            "n": len(moves),
            "meanEsMovePct": round(sum(moves) / len(moves), 3) if moves else None,
            "worstEsMovePct": round(min(moves), 3) if moves else None,
        }
    return {
        "levels": levels,
        "weekends": len(rows),
        "scored": sum(row["status"] == STATUS_SCORED for row in rows),
        "pending": sum(row["status"] == STATUS_PENDING for row in rows),
        "unscored": sum(row["status"] == STATUS_UNSCORED for row in rows),
        "disagreements": sum(row["levelAtClose"] != row["levelAtSettle"] for row in rows),
    }


def build(weekend_rows: list[dict], first_rows: list[dict], *, weeks: int, since: datetime) -> dict:
    """The endpoint body."""
    rows = build_rows(weekend_rows, first_rows)
    return {"weeks": weeks, "since": since.isoformat(), "rows": rows, "summary": summarize(rows)}
