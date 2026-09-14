"""
TradingFirm — when a health change is published (Part 3.4, spec decision 5).

decide() is pure, first match wins:
  no score                                        → no (no regime is not a change)
  nothing published before                        → yes, initial
  CRITICAL, entering it or moving ≥ 10 inside it  → yes, critical (ignores the interval)
  < 15 min since the last publish                 → no, held
  regime changed                                  → yes, regime_change
  score moved ≥ 10 since the last publish         → yes, score_move

Not symmetric: *leaving* CRITICAL gets no bypass and is held like any other
regime change. State moves only on a publish, so a held change is compared
against the same last publish next check: delayed, never lost.

publish_health() does PUBLISH, then SET state. A SET that fails after a
successful PUBLISH means the next check may publish again: at-least-once.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from cache import (
    STATE_HEALTH_PUBLISHED,
    TTL_HEALTH_PUBLISHED,
    get_cached_json,
    set_cached_json,
    state_key,
)
from config import settings
from scoring.regime_classifier import CRITICAL, HEALTHY, REGIMES

logger = logging.getLogger(__name__)

SCORE_MOVE_POINTS = 10                  # Part 5: "≥ 10 points from last alert"
MIN_INTERVAL = timedelta(minutes=15)    # Part 5: "no more than 1 alert per 15 minutes"

REASON_INITIAL = "initial"
REASON_CRITICAL = "critical"
REASON_REGIME_CHANGE = "regime_change"
REASON_SCORE_MOVE = "score_move"

PAYLOAD_KEYS = ("score", "regime", "reason", "recovery", "previousScore", "previousRegime",
                "trend", "stale", "coverage", "checkedAt", "monitors",
                # Part 3.5 addition 8: the news feed's state (news_poller.stale_view)
                "newsPollStale", "lastNewsPollAt", "newsLastError",
                # Part 3.4 follow-up addition 1: host pause before this check. Append-only.
                "pausedSeconds",
                # Part 3.4b: which cadence ran, and the futures cap it applied.
                "kind", "overlay",
                # Part 3.4c: the weekend-exposure block, non-null only on a
                # weekend-eve session's last eight rows. Append-only.
                "weekend")
NEWS_KEYS = PAYLOAD_KEYS[11:14]


def _aware(value: Any) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else None


def parse_state(raw: Any) -> Optional[dict]:
    """{score, regime, publishedAt: datetime} from the stored state, or None
    when it has the wrong shape (treated as never published)."""
    if not isinstance(raw, dict):
        return None
    score, regime, published_at = raw.get("score"), raw.get("regime"), _aware(raw.get("publishedAt"))
    if type(score) is not int or regime not in REGIMES or published_at is None:
        return None
    return {"score": score, "regime": regime, "publishedAt": published_at}


def decide(last: Optional[dict], health: dict, now: datetime) -> tuple[bool, Optional[str]]:
    """(publish, reason). `last` is parse_state's result."""
    score, regime = health.get("score"), health.get("regime")
    if score is None or regime is None:
        return False, None
    if last is None:
        return True, REASON_INITIAL
    moved = abs(score - last["score"]) >= SCORE_MOVE_POINTS
    if regime == CRITICAL and (last["regime"] != CRITICAL or moved):
        return True, REASON_CRITICAL
    if now - last["publishedAt"] < MIN_INTERVAL:
        return False, None
    if regime != last["regime"]:
        return True, REASON_REGIME_CHANGE
    if moved:
        return True, REASON_SCORE_MOVE
    return False, None


def build_payload(health: dict, last: Optional[dict], reason: str, trend: Optional[str],
                  news: Optional[dict] = None, paused_seconds: Optional[int] = None,
                  kind: Optional[str] = None) -> dict:
    """previousScore / previousRegime are the last *published* values (Part
    5: "from last alert"); the trend base is a different thing (settle*).
    The news keys, pausedSeconds and the weekend block ride along on a
    publish; none of them causes one."""
    news = news or {}
    return {
        "score": health["score"],
        "regime": health["regime"],
        "reason": reason,
        "recovery": health["regime"] == HEALTHY and last is not None and last["regime"] != HEALTHY,
        "previousScore": last["score"] if last else None,
        "previousRegime": last["regime"] if last else None,
        "trend": trend,
        "stale": bool(health.get("stale")),
        "coverage": health.get("coverage"),
        "checkedAt": health.get("checkedAt"),
        "monitors": {name: m.get("score") for name, m in (health.get("monitors") or {}).items()},
        **{key: news.get(key) for key in NEWS_KEYS},
        "pausedSeconds": paused_seconds,
        "kind": kind,
        "overlay": health.get("overlay"),
        "weekend": health.get("weekend"),
    }


async def read_state(r) -> Optional[dict]:
    key = state_key(STATE_HEALTH_PUBLISHED)
    try:
        raw = await get_cached_json(r, key)
    except Exception as e:
        logger.warning(f"Health publish state read failed, treating as never published: {e!r}")
        return None
    if raw is None:
        return None
    state = parse_state(raw)
    if state is None:
        logger.warning(f"State at {key} has the wrong shape, treating as never published")
    return state


async def publish_health(r, health: dict, trend: Optional[str], *, now: datetime,
                         news: Optional[dict] = None, paused_seconds: Optional[int] = None,
                         kind: Optional[str] = None) -> dict:
    """Publish on settings.health_channel if decide() says so. Returns
    {published, reason}. Never raises for a Redis state; a payload that is
    not strict JSON (a NaN from a monitor bug) raises ValueError. `news` is
    news_poller.stale_view(); absent, the news keys are null. `paused_seconds`
    is the host pause before this check (3.4 follow-up addition 1), else null."""
    if r is None:
        logger.warning("Health publish skipped: Redis unavailable")
        return {"published": False, "reason": None}

    last = await read_state(r)
    publish, reason = decide(last, health, now)
    if not publish:
        return {"published": False, "reason": None}

    message = json.dumps(build_payload(health, last, reason, trend, news, paused_seconds, kind),
                         allow_nan=False)
    channel = settings.health_channel
    try:
        await r.publish(channel, message)
    except Exception as e:
        logger.warning(f"Health publish failed ({reason}), retried next check: {e!r}")
        return {"published": False, "reason": reason}

    state = {"score": health["score"], "regime": health["regime"], "publishedAt": now.isoformat()}
    try:
        await set_cached_json(r, state_key(STATE_HEALTH_PUBLISHED), state, TTL_HEALTH_PUBLISHED)
    except Exception as e:
        logger.warning(f"Health publish state write failed, the next check may publish again: {e!r}")
    logger.info(f"Published health on {channel}: {reason}, {health['regime']} {health['score']}")
    return {"published": True, "reason": reason}
