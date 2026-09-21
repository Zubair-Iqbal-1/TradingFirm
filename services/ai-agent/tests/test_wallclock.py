"""Copied from services/risk-shield/tests/test_wallclock.py with wallclock.py
(Part 4.5). Part 3.4 follow-up — the wall-clock wait harness. Fake wall and process
clocks with a fake sleep; one real asyncio.sleep for cancellation. No socket."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pytest

import wallclock

T0 = datetime(2026, 9, 10, 21, 7, 15, tzinfo=timezone.utc)


class FakeClocks:
    """sleep() moves the wall and process clocks by the requested seconds.
    `extra` adds wall seconds to the chunk at that index (the host paused);
    `early` moves both clocks only that fraction of the chunk at that index."""

    def __init__(self, start=T0, *, extra=None, early=None):
        self.wall, self.process = start, 1000.0
        self.extra, self.early = extra or {}, early or {}
        self.sleeps = []

    def clock(self):
        return self.wall

    def process_clock(self):
        return self.process

    async def sleep(self, seconds):
        index = len(self.sleeps)
        self.sleeps.append(seconds)
        moved = seconds * self.early.get(index, 1)
        self.wall += timedelta(seconds=moved + self.extra.get(index, 0))
        self.process += moved

    async def until(self, target, **kw):
        return await wallclock.sleep_until(target, clock=self.clock, sleep=self.sleep,
                                           process_clock=self.process_clock, **kw)


def test_wait_seconds_caps_at_sixty():
    assert (wallclock.MAX_SLEEP_SECONDS, wallclock.PAUSE_THRESHOLD_SECONDS) == (60, 120)
    for ahead, expected in ((10, 10), (60, 60), (61, 60), (16 * 3600, 60)):
        assert wallclock.wait_seconds(T0 + timedelta(seconds=ahead), T0) == expected


@pytest.mark.asyncio
async def test_sleep_until_past_target_no_sleep():
    for behind in (0, 5):
        clocks = FakeClocks()
        assert wallclock.wait_seconds(T0 - timedelta(seconds=behind), T0) == 0
        assert await clocks.until(T0 - timedelta(seconds=behind)) == wallclock.Wake(T0, None)
        assert clocks.sleeps == []


@pytest.mark.asyncio
async def test_wallclock_rejects_naive_datetimes():
    naive = datetime(2026, 9, 10, 21, 7, 15)
    with pytest.raises(ValueError):
        wallclock.wait_seconds(naive, T0)
    with pytest.raises(ValueError):
        wallclock.wait_seconds(T0, naive)
    for clocks, target in ((FakeClocks(), naive), (FakeClocks(start=naive), T0)):
        with pytest.raises(ValueError):
            await clocks.until(target)
        assert clocks.sleeps == []


@pytest.mark.asyncio
async def test_sleep_until_rereads_clock_each_chunk():
    clocks = FakeClocks()
    wake = await clocks.until(T0 + timedelta(seconds=150))
    assert clocks.sleeps == [60, 60, 30]
    assert wake == wallclock.Wake(T0 + timedelta(seconds=150), None)


@pytest.mark.asyncio
async def test_sleep_until_early_wake_sleeps_again():
    clocks = FakeClocks(early={0: 0.5})                  # the first sleep ends 30 s into its 60
    wake = await clocks.until(T0 + timedelta(seconds=100))
    assert clocks.sleeps == [60, 60, 10]
    assert wake == wallclock.Wake(T0 + timedelta(seconds=100), None)


@pytest.mark.asyncio
async def test_sleep_until_returns_on_wake_after_frozen_chunk(caplog):
    # 2026-09-10: waiting from 21:07:15 UTC toward Fri 13:30; the chunk that
    # starts at 21:45:15 (index 38) ends when the Mac wakes, at 13:32:39.
    wake_at = datetime(2026, 9, 11, 13, 32, 39, tzinfo=timezone.utc)
    clocks = FakeClocks(extra={38: (wake_at - (T0 + timedelta(seconds=39 * 60))).total_seconds()})
    with caplog.at_level(logging.WARNING):
        wake = await clocks.until(datetime(2026, 9, 11, 13, 30, tzinfo=timezone.utc))
    assert len(clocks.sleeps) == 39 and max(clocks.sleeps) == 60           # no sleep after the wake
    assert wake == wallclock.Wake(wake_at, 56784)
    assert [r.getMessage() for r in caplog.records] == ["host paused ~15h 46m (wall +56844 s, process +60 s)"]


@pytest.mark.asyncio
async def test_sleep_until_warns_host_paused_over_120s(caplog):
    named = logging.getLogger("scheduler")
    for extra, paused, lines in ((120, None, []),
                                 (121, 121, ["host paused ~0h 2m (wall +181 s, process +60 s)"])):
        clocks = FakeClocks(extra={0: extra})
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            wake = await clocks.until(T0 + timedelta(seconds=90), log=named)
        assert wake.paused_seconds == paused
        assert [(r.name, r.getMessage()) for r in caplog.records] == [("scheduler", line) for line in lines]

    # Two pauses in one wait: a line each, the gaps summed.
    clocks = FakeClocks(extra={0: 3600, 1: 170})
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        wake = await clocks.until(T0 + timedelta(seconds=4000))
    assert wake == wallclock.Wake(T0 + timedelta(seconds=4000), 3770)
    assert [r.getMessage() for r in caplog.records] == [
        "host paused ~1h 0m (wall +3660 s, process +60 s)",
        "host paused ~0h 3m (wall +230 s, process +60 s)",
    ]


@pytest.mark.asyncio
async def test_sleep_until_cancel_propagates():
    now = lambda: datetime.now(timezone.utc)
    task = asyncio.create_task(wallclock.sleep_until(now() + timedelta(hours=16), clock=now))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
