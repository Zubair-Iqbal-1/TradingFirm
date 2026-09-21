"""
TradingFirm — wall-clock waits for the service's loops (Part 3.4 follow-up).

COPIED VERBATIM from services/risk-shield/wallclock.py for ai-agent's journal
loop (Part 4.5); the services share no Python package. Keep the two equal: a
fix to one is a fix to both.

asyncio.sleep runs on the Docker VM's monotonic clock, which stops while the
Mac sleeps: one sleep toward a slot 16 h away slept through the next open
(decisions 2026-09-11). Every scheduling loop waits through sleep_until
instead: sleeps of at most MAX_SLEEP_SECONDS, with the wall clock re-read after
each, so a loop sees the real time within 60 s of the host waking.

A chunk whose wall-clock elapsed beats its process-clock elapsed by more than
PAUSE_THRESHOLD_SECONDS was a host pause: one WARNING on the caller's logger,
and the gaps are summed into the returned Wake (addition 1).
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Awaitable, Callable, NamedTuple, Optional

logger = logging.getLogger(__name__)

MAX_SLEEP_SECONDS = 60
PAUSE_THRESHOLD_SECONDS = 120


class Wake(NamedTuple):
    now: datetime                   # the wall-clock reading that ended the wait
    paused_seconds: Optional[int]   # host pauses over the threshold, summed; None without one


def _require_aware(value, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def wait_seconds(target: datetime, now: datetime) -> float:
    """How long to sleep before re-reading the clock: min(target − now, 60), never below 0."""
    _require_aware(target, "target")
    _require_aware(now, "now")
    return max(0.0, min((target - now).total_seconds(), MAX_SLEEP_SECONDS))


def pause_message(gap: float, wall: float, process: float) -> str:
    hours, minutes = divmod(round(gap / 60), 60)
    return f"host paused ~{hours}h {minutes}m (wall +{wall:.0f} s, process +{process:.0f} s)"


async def sleep_until(target: datetime, *, clock: Callable[[], datetime],
                      sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                      process_clock: Callable[[], float] = time.monotonic,
                      log: logging.Logger = logger) -> Wake:
    """Sleep in chunks of at most 60 s until clock() ≥ target, then return
    that reading. A passed target returns at once, with no sleep.
    Cancellation propagates."""
    paused = 0.0
    while True:
        now = clock()
        seconds = wait_seconds(target, now)
        if seconds <= 0:
            return Wake(now, round(paused) if paused else None)
        process_start = process_clock()
        await sleep(seconds)
        wall = (clock() - now).total_seconds()
        process = process_clock() - process_start
        if wall - process > PAUSE_THRESHOLD_SECONDS:
            log.warning(pause_message(wall - process, wall, process))
            paused += wall - process
