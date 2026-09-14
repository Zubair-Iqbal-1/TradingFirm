"""
TradingFirm — the weekend-exposure signal (Part 3.4c).

A weekend headline gap cannot be traded out of: stops fill at the gapped
price, and by Sunday 18:00 ET the damage is priced in. The only useful
moment is the Friday afternoon, while the market is still open. So every
check from 30 minutes before a Friday close through that day's 16:20
settle carries a `weekend` block: a level — LOW / ELEVATED / HIGH — and
the reasons that produced it.

    reason                fires when                              weight
    regime_weak           the published (capped) score ≤ 39            2
    regime_soft           the published score 40-59                    1
    vix_high              VIX ≥ 25                                     2
    vix_elevated          VIX 20-25                                    1
    vix_rising            VIX up ≥ 15 % over 5 complete sessions       1
    scheduled_event       an event between Friday close and Monday open 2
    pending_decision      pending-decision language in the news        1-2
    active_situation      the operator's flag is set and unexpired     2

    HIGH ≥ 4, ELEVATED 2-3, LOW 0-1, and active_situation alone is never
    HIGH: a level is HIGH only when two independent things agree.

The regime reason reads the **capped** score, not `overlay.base`: a cap is
never less cautious than the base, and a Friday intraday futures drop is
exactly what a weekend read must not ignore (3.4b's asymmetry). Both
scores are recorded, so the log can be re-scored on the base later.

The numbers are provisional, like 3.3's and 3.4b's: the weekend log
(`GET /market/weekend/log`) is what retunes them.

**This is a risk report, never a trade action.** It says how exposed a
weekend looks and why; what to do about it is the reader's.

Pure: no clock, no Redis, no Postgres, no HTTP, nothing mutated. The one
exception is the ERROR that `drop_if_nonfinite` logs once per process.
"""

import logging
import math
import re
from typing import Any, Iterable, Optional

from monitors.series import is_partial

logger = logging.getLogger(__name__)

VERSION = 1

LEVEL_LOW = "LOW"
LEVEL_ELEVATED = "ELEVATED"
LEVEL_HIGH = "HIGH"
LEVELS = (LEVEL_LOW, LEVEL_ELEVATED, LEVEL_HIGH)

# Points at or above which a level is reached (first match from the top).
HIGH_POINTS = 4
ELEVATED_POINTS = 2

REASON_REGIME_WEAK = "regime_weak"
REASON_REGIME_SOFT = "regime_soft"
REASON_VIX_HIGH = "vix_high"
REASON_VIX_ELEVATED = "vix_elevated"
REASON_VIX_RISING = "vix_rising"
REASON_SCHEDULED_EVENT = "scheduled_event"
REASON_PENDING_DECISION = "pending_decision"
REASON_ACTIVE_SITUATION = "active_situation"
REASON_CALENDAR_UNAVAILABLE = "calendar_unavailable"   # weight 0: a note, not a reason

WEIGHTS = {
    REASON_REGIME_WEAK: 2,
    REASON_REGIME_SOFT: 1,
    REASON_VIX_HIGH: 2,
    REASON_VIX_ELEVATED: 1,
    REASON_VIX_RISING: 1,
    REASON_SCHEDULED_EVENT: 2,      # 2 each, capped at 2: one reason, however many events
    REASON_PENDING_DECISION: 1,     # 2 when ≥ PENDING_MANY distinct items match
    REASON_ACTIVE_SITUATION: 2,
    REASON_CALENDAR_UNAVAILABLE: 0,
}

# Score bands for the regime reason (the published, capped score).
REGIME_WEAK_MAX = 39
REGIME_SOFT_MAX = 59
# VIX bands, provisional like 3.3's.
VIX_HIGH_LEVEL = 25.0
VIX_ELEVATED_LEVEL = 20.0
VIX_RISING_PCT = 15.0           # over 5 complete sessions → the reason
VIX_DIRECTION_BAND = 5.0        # ± this much is "flat"
VIX_SESSIONS = 5                # complete sessions back, so 6 closes are needed
VIX_TICKER = "^VIX"

PENDING_MANY = 3                # distinct matched items that make the reason weight 2

# Caps, so a row's JSONB stays ~1.5 KB (spec D8).
MAX_REASONS = 8
MAX_EVENTS = 5
MAX_NEWS = 5
NEWS_TITLE_MAX = 120
SITUATION_TEXT_MAX = 200
DETAIL_MAX = 200

DIRECTION_RISING = "rising"
DIRECTION_FALLING = "falling"
DIRECTION_FLAT = "flat"
DIRECTION_UNKNOWN = "unknown"

EVENTS_OK = "ok"
EVENTS_UNAVAILABLE = "unavailable"

# Pending-decision language (spec D6). Matched case-insensitively over
# "title summary", as regexes so word boundaries hold. Pinned by
# test_phrases_pinned_to_spec: this tuple is the spec.
PHRASES = (
    r"expected to announce",
    r"deadline (?:sunday|saturday|this weekend)",
    r"talks (?:this |over the )?weekend",
    r"emergency (?:meeting|session|summit)",
    r"decision (?:is )?expected",
    r"vote (?:on )?(?:sunday|saturday)",
    r"ceasefire (?:deadline|talks)",
    r"tariff deadline",
    r"summit",
    r"ahead of monday",
)
_PATTERNS = tuple((phrase, re.compile(rf"\b{phrase}\b", re.IGNORECASE)) for phrase in PHRASES)


def _number(value: Any) -> Optional[float]:
    """A finite float, or None. Bools and strings are wrong shapes."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _text(value: Any, limit: int) -> Optional[str]:
    return value[:limit] if isinstance(value, str) else None


# ── The VIX snapshot (spec D10) ──────────────────────────────────

def vix5d(view: dict) -> Optional[dict]:
    """
    {level, prevClose, date, partial, asOf, source, closes, dates} from a
    quotes view, or None when ^VIX is missing or stale. `level` is the last
    bar — **partial included**, which is what makes a Friday-afternoon read
    a live one — while `closes` holds the last VIX_SESSIONS + 1 *complete*
    closes, so the 5-day direction is complete-bar arithmetic either way.

    Stored by compute_health beside 3.4b's `futures`, exactly as pure and
    exactly as cheap: no monitor changes, no scoring change.
    """
    entry = (view.get("tickers") or {}).get(VIX_TICKER)
    stale = VIX_TICKER in set(view.get("staleTickers") or [])
    if not isinstance(entry, dict) or stale or entry.get("stale"):
        return None
    dates = entry.get("date") or []
    closes = entry.get("close") or []
    if not dates or len(dates) != len(closes):
        return None
    level = _number(closes[-1])
    if level is None or level <= 0:
        return None

    as_of = entry.get("asOf") or view.get("asOf")
    # 3.3's own rule, on the body's own asOf: a bar dated today in New York
    # and downloaded before 16:15 ET is partial. No clock is read here.
    try:
        partial = is_partial(dates[-1], as_of)
    except (TypeError, ValueError):
        return None
    complete = list(zip(dates, closes))
    if partial:
        complete = complete[:-1]
    complete = [(d, _number(c)) for d, c in complete]
    complete = [(d, c) for d, c in complete if c is not None and c > 0]
    window = complete[-(VIX_SESSIONS + 1):]
    return {
        "level": level,
        "prevClose": window[-1][1] if window and partial else (window[-2][1] if len(window) > 1 else None),
        "date": dates[-1],
        "partial": partial,
        "asOf": as_of,
        "source": view.get("source"),
        "dates": [d for d, _ in window],
        "closes": [c for _, c in window],
    }


def vix_direction(snapshot: Optional[dict]) -> tuple[Optional[float], str]:
    """(% change over VIX_SESSIONS complete sessions, direction). Needs
    VIX_SESSIONS + 1 complete closes; fewer is "unknown", never 0."""
    closes = (snapshot or {}).get("closes") or []
    if len(closes) < VIX_SESSIONS + 1:
        return None, DIRECTION_UNKNOWN
    first, last = closes[-(VIX_SESSIONS + 1)], closes[-1]
    if not first:
        return None, DIRECTION_UNKNOWN
    change = (last - first) * 100 / first
    if change >= VIX_DIRECTION_BAND:
        return change, DIRECTION_RISING
    if change <= -VIX_DIRECTION_BAND:
        return change, DIRECTION_FALLING
    return change, DIRECTION_FLAT


# ── Pending-decision language (spec D6) ──────────────────────────

def match_phrases(items: Optional[Iterable[dict]]) -> list[dict]:
    """The news items whose title + summary carry a pending-decision phrase,
    newest first as they arrive, ≤ MAX_NEWS. Each keeps the phrase that fired
    it, so a reason can always be traced back to a headline."""
    matched = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or ""
        haystack = f"{title} {item.get('summary') or ''}"
        for phrase, pattern in _PATTERNS:
            if pattern.search(haystack):
                matched.append({"publishedAt": item.get("publishedAt"),
                                "source": _text(item.get("source"), 60),
                                "title": _text(title, NEWS_TITLE_MAX),
                                "phrase": phrase})
                break
        if len(matched) >= MAX_NEWS:
            break
    return matched


# ── Reasons and the level ────────────────────────────────────────

def _reason(code: str, detail: str, weight: Optional[int] = None) -> dict:
    return {"code": code, "detail": detail[:DETAIL_MAX],
            "weight": WEIGHTS[code] if weight is None else weight}


def situation_active(situation: Any, now_iso: Optional[str]) -> Optional[dict]:
    """The operator's flag if it is set and unexpired, else None. Expiry is a
    string comparison of ISO-8601 UTC timestamps, which the route writes."""
    if not isinstance(situation, dict):
        return None
    text = _text(situation.get("text"), SITUATION_TEXT_MAX)
    if not text or not text.strip():
        return None
    expires = situation.get("expiresAt")
    if isinstance(expires, str) and isinstance(now_iso, str) and expires <= now_iso:
        return None
    return {"text": text.strip(), "setAt": situation.get("setAt"), "expiresAt": expires}


def reasons_for(*, capped_score: Optional[int], vix: Optional[dict], direction: str,
                change_pct: Optional[float], events: dict, matched: list, situation: Optional[dict]) -> list[dict]:
    """Every reason that fires, in weight order then code order, ≤ MAX_REASONS."""
    out: list[dict] = []

    if capped_score is not None:
        if capped_score <= REGIME_WEAK_MAX:
            out.append(_reason(REASON_REGIME_WEAK, f"market health {capped_score} going into the weekend"))
        elif capped_score <= REGIME_SOFT_MAX:
            out.append(_reason(REASON_REGIME_SOFT, f"market health {capped_score} going into the weekend"))

    level = (vix or {}).get("level")
    if level is not None:
        if level >= VIX_HIGH_LEVEL:
            out.append(_reason(REASON_VIX_HIGH, f"VIX at {level:.1f}"))
        elif level >= VIX_ELEVATED_LEVEL:
            out.append(_reason(REASON_VIX_ELEVATED, f"VIX at {level:.1f}"))
    if change_pct is not None and change_pct >= VIX_RISING_PCT:
        out.append(_reason(REASON_VIX_RISING,
                           f"VIX up {change_pct:.1f}% over {VIX_SESSIONS} sessions ({direction})"))

    listed = (events or {}).get("events") or []
    if listed:
        titles = ", ".join(str(e.get("title")) for e in listed[:MAX_EVENTS])
        out.append(_reason(REASON_SCHEDULED_EVENT,
                           f"{len(listed)} event(s) while the market is shut: {titles}"))
    elif (events or {}).get("status") == EVENTS_UNAVAILABLE:
        out.append(_reason(REASON_CALENDAR_UNAVAILABLE,
                           "the econ calendar could not be read: events unknown"))

    if matched:
        weight = 2 if len(matched) >= PENDING_MANY else WEIGHTS[REASON_PENDING_DECISION]
        out.append(_reason(REASON_PENDING_DECISION,
                           f"{len(matched)} headline(s) with pending-decision language: "
                           f"\"{matched[0]['phrase']}\"", weight))

    if situation:
        out.append(_reason(REASON_ACTIVE_SITUATION, f"active situation: {situation['text']}"))

    out.sort(key=lambda r: (-r["weight"], r["code"]))
    return out[:MAX_REASONS]


def level_for(reasons: list[dict]) -> tuple[str, int]:
    """(level, points). active_situation on its own is never HIGH: a level is
    HIGH only when two independent things point the same way."""
    scoring = [r for r in reasons if r["weight"] > 0]
    points = sum(r["weight"] for r in scoring)
    if points >= HIGH_POINTS:
        level = LEVEL_HIGH
    elif points >= ELEVATED_POINTS:
        level = LEVEL_ELEVATED
    else:
        level = LEVEL_LOW
    if level == LEVEL_HIGH and len(scoring) == 1 and scoring[0]["code"] == REASON_ACTIVE_SITUATION:
        level = LEVEL_ELEVATED
    return level, points


# ── Non-finite guard (spec F12) ──────────────────────────────────

_nan_logged = False


def nonfinite_path(value: Any, path: str = "weekend") -> Optional[str]:
    """The path of the first NaN/Inf inside a block, or None. A block is
    small and shallow; this walks all of it."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return path
    if isinstance(value, dict):
        for key, item in value.items():
            found = nonfinite_path(item, f"{path}.{key}")
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            found = nonfinite_path(item, f"{path}[{i}]")
            if found:
                return found
    return None


def drop_if_nonfinite(block: Optional[dict]) -> tuple[Optional[dict], Optional[str]]:
    """
    (block, dropped). A block holding a NaN or an Inf is dropped to None
    with dropped "nan" and one ERROR per process naming the path.

    This runs before the block is attached to anything, so a non-finite
    number can never reach `json.dumps(allow_nan=False)` in publish_health:
    the block must never be able to fail a publish (spec Change 4).
    """
    global _nan_logged
    if block is None:
        return None, None
    path = nonfinite_path(block)
    if path is None:
        return block, None
    if not _nan_logged:
        _nan_logged = True
        logger.error(f"Weekend block dropped: non-finite value at {path}")
    else:
        logger.debug(f"Weekend block dropped again: non-finite value at {path}")
    return None, "nan"


# ── The block ────────────────────────────────────────────────────

def assess(*, capped_score: Optional[int], base_score: Optional[int], regime: Optional[str],
           vix: Optional[dict], events: Optional[dict], news: Optional[dict],
           situation: Any, window: Optional[dict], assessed_at: str) -> Optional[dict]:
    """
    The `weekend` block, or None when there is no score to stand on (spec
    F6: a level with no score would be a guess). Every argument is already
    assembled; nothing here reads a clock, a socket or a database.
    """
    if capped_score is None:
        return None

    events = events or {"status": EVENTS_UNAVAILABLE, "coverageShort": False, "events": []}
    news = news or {"status": "unavailable", "hours": None, "items": []}
    window = window or {}
    change_pct, direction = vix_direction(vix)
    active = situation_active(situation, assessed_at)
    matched = match_phrases(news.get("items"))

    reasons = reasons_for(capped_score=capped_score, vix=vix, direction=direction,
                          change_pct=change_pct, events=events, matched=matched, situation=active)
    level, points = level_for(reasons)

    return {
        "version": VERSION,
        "level": level,
        "points": points,
        "reasons": reasons,
        "inputs": {
            "regime": regime,
            "cappedScore": capped_score,
            "baseScore": base_score,
            "regimeSource": "capped",
            # What the five complete-bar monitors actually read (spec Change 1).
            "baseScoreAsOf": window.get("baseScoreAsOf"),
            "vixLevel": (vix or {}).get("level"),
            "vixPrevClose": (vix or {}).get("prevClose"),
            "vix5dChangePct": change_pct,
            "vixDirection": direction,
            "vixAsOf": (vix or {}).get("asOf"),
            "vixPartial": (vix or {}).get("partial"),
            "quotesSource": (vix or {}).get("source"),
            "gapHours": window.get("gapHours"),
            "closeAt": window.get("closeAt"),
            "nextOpenAt": window.get("nextOpenAt"),
            "events": {"status": events.get("status"),
                       "coverageShort": bool(events.get("coverageShort")),
                       "items": (events.get("events") or [])[:MAX_EVENTS]},
            "news": {"status": news.get("status"), "hours": news.get("hours"), "matched": matched},
            "activeSituation": active,
        },
        "assessedAt": assessed_at,
    }
