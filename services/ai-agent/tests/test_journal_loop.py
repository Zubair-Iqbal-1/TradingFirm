"""Part 4.5 — the journal's nightly loop (journal/runner.run_loop).

A fake wall clock that the injected sleep moves; score_once is replaced, so
nothing is refreshed. The loop only ends by cancellation, which the fake
score_once raises when a test has seen enough."""

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from journal import runner, sessions

ET = ZoneInfo("America/New_York")


class Wall:
    """clock() reads it; sleep(s) moves it by s, or jumps it once to `jump`
    (the Mac slept through the slot)."""

    def __init__(self, start, jump=None):
        self.now, self.jump = start.astimezone(timezone.utc), jump

    def clock(self):
        return self.now

    async def sleep(self, seconds):
        if self.jump is not None:
            self.now, self.jump = self.jump.astimezone(timezone.utc), None
        else:
            self.now += timedelta(seconds=seconds)


def recording_score_once(calls, stop_after, fail_first=False):
    async def fake(state, settings, *, deadline=None, clock=None, sleep=None):
        calls.append({"at": clock(), "deadline": deadline})
        if fail_first and len(calls) == 1:
            raise RuntimeError("boom")
        if len(calls) >= stop_after:
            raise asyncio.CancelledError
        return {**runner.new_result(), "scored": len(calls)}
    return fake


@pytest.mark.asyncio
async def test_loop_runs_at_the_slot_with_its_deadline(monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "score_once", recording_score_once(calls, stop_after=2))
    wall = Wall(datetime(2026, 9, 18, 12, 0, tzinfo=ET))           # a Friday
    state = SimpleNamespace()
    with pytest.raises(asyncio.CancelledError):
        await runner.run_loop(state, None, clock=wall.clock, sleep=wall.sleep)
    assert [c["at"] for c in calls] == [datetime(2026, 9, 18, 17, 30, tzinfo=ET),
                                        datetime(2026, 9, 21, 17, 30, tzinfo=ET)]
    assert calls[0]["deadline"] == sessions.deadline_at(date(2026, 9, 18))
    assert state.journal_last_result["scored"] == 1
    assert state.journal_last_run_at == "2026-09-18T21:30:00+00:00"


@pytest.mark.asyncio
async def test_missed_slot_is_caught_by_the_next(monkeypatch, caplog):
    """The host slept from noon to 19:00: the 09-21 slot is past its 18:10
    deadline, so it is skipped with a WARNING and 09-22's slot runs."""
    calls = []
    monkeypatch.setattr(runner, "score_once", recording_score_once(calls, stop_after=1))
    wall = Wall(datetime(2026, 9, 21, 12, 0, tzinfo=ET), jump=datetime(2026, 9, 21, 19, 0, tzinfo=ET))
    with pytest.raises(asyncio.CancelledError):
        await runner.run_loop(SimpleNamespace(), None, clock=wall.clock, sleep=wall.sleep)
    assert calls[0]["at"] == datetime(2026, 9, 22, 17, 30, tzinfo=ET)
    assert "2026-09-21 slot was missed" in caplog.text


@pytest.mark.asyncio
async def test_loop_survives_a_pass_that_raises(monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(runner, "score_once", recording_score_once(calls, stop_after=3, fail_first=True))
    wall = Wall(datetime(2026, 9, 21, 12, 0, tzinfo=ET))
    state = SimpleNamespace()
    with pytest.raises(asyncio.CancelledError):
        await runner.run_loop(state, None, clock=wall.clock, sleep=wall.sleep)
    assert len(calls) == 3 and "RuntimeError: boom" in caplog.text
    assert state.journal_last_result["scored"] == 2


@pytest.mark.asyncio
async def test_loop_waits_in_wallclock_chunks(monkeypatch):
    """No single sleep toward a far slot: every chunk is ≤ 60 s."""
    calls, chunks = [], []
    monkeypatch.setattr(runner, "score_once", recording_score_once(calls, stop_after=1))
    wall = Wall(datetime(2026, 9, 21, 16, 0, tzinfo=ET))

    async def spy(seconds):
        chunks.append(seconds)
        await wall.sleep(seconds)

    with pytest.raises(asyncio.CancelledError):
        await runner.run_loop(SimpleNamespace(), None, clock=wall.clock, sleep=spy)
    assert max(chunks) <= 60 and sum(chunks) == 90 * 60
