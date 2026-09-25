"""
TradingFirm — the open session's bar is never stored (Part 4.8b-de, spec
docs/specs/4.8b.md decision 15). Frozen clocks, the real XNYS calendar
(exchange_calendars, no network), no database.

Dates used: 2026-09-24 (Thu, a full session, close 16:00 ET = 20:00 UTC),
2026-11-26 (Thanksgiving, no session), 2026-11-27 (early close, 13:00 ET =
18:00 UTC).
"""

import subprocess
import sys
from datetime import datetime, timezone

import pytest

import bar_session
from bar_session import INTERVAL_DAILY, INTERVAL_HOURLY, drop_open_session_bars


def utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def daily(*days):
    """Daily rows the way bar_records_from_df builds them from yfinance: a
    naive midnight timestamp per session date."""
    return [{"ts": datetime(2026, m, d), "open": 1.0, "high": 1.0, "low": 1.0,
             "close": 1.0, "volume": 1} for m, d in days]


def hourly(*starts):
    return [{"ts": s, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1}
            for s in starts]


def dates(rows):
    return [r["ts"] for r in rows]


def test_open_session_bar_not_stored():
    """Daily: today's row goes while the session trades (and before it opens);
    hourly: a row goes while its hour, cut at the close, has not ended —
    on a full day and on an early-close day."""
    rows = daily((9, 22), (9, 23), (9, 24))
    # 13:00 ET on a full session: today's row is dropped, the rest kept.
    kept, dropped = drop_open_session_bars(rows, INTERVAL_DAILY, utc(2026, 9, 24, 17, 0))
    assert dates(kept) == [datetime(2026, 9, 22), datetime(2026, 9, 23)]
    assert dates(dropped) == [datetime(2026, 9, 24)]
    # 08:00 ET, pre-market: a row dated today is still the unclosed session's.
    kept, dropped = drop_open_session_bars(rows, INTERVAL_DAILY, utc(2026, 9, 24, 12, 0))
    assert dates(dropped) == [datetime(2026, 9, 24)]

    # Hourly on the full day at 13:45 ET (17:45Z): 12:30 ET ended at 13:30,
    # 13:30 ET runs to 14:30 -> dropped.
    bars = hourly(utc(2026, 9, 24, 16, 30), utc(2026, 9, 24, 17, 30))
    kept, dropped = drop_open_session_bars(bars, INTERVAL_HOURLY, utc(2026, 9, 24, 17, 45))
    assert dates(kept) == [utc(2026, 9, 24, 16, 30)]
    assert dates(dropped) == [utc(2026, 9, 24, 17, 30)]

    # Early close 2026-11-27 (13:00 ET = 18:00Z). The 12:30 ET bar (17:30Z)
    # ends at the close, not at 13:30: dropped at 12:45 ET, kept at 13:05 ET
    # although its clock hour has not ended.
    early = hourly(utc(2026, 11, 27, 16, 30), utc(2026, 11, 27, 17, 30))
    kept, dropped = drop_open_session_bars(early, INTERVAL_HOURLY, utc(2026, 11, 27, 17, 45))
    assert dates(dropped) == [utc(2026, 11, 27, 17, 30)]
    kept, dropped = drop_open_session_bars(early, INTERVAL_HOURLY, utc(2026, 11, 27, 18, 5))
    assert dropped == [] and len(kept) == 2
    # And the early-close daily row is stored from 13:00 ET, not 16:00 ET.
    rows = daily((11, 25), (11, 27))
    assert drop_open_session_bars(rows, INTERVAL_DAILY, utc(2026, 11, 27, 17, 59))[1] == rows[1:]
    assert drop_open_session_bars(rows, INTERVAL_DAILY, utc(2026, 11, 27, 18, 0))[1] == []


def test_closed_session_bar_stored_after_close():
    rows = daily((9, 23), (9, 24))
    kept, dropped = drop_open_session_bars(rows, INTERVAL_DAILY, utc(2026, 9, 24, 20, 0))
    assert dropped == [] and kept == rows
    last_hour = hourly(utc(2026, 9, 24, 19, 30))          # 15:30 ET, cut at 16:00
    assert drop_open_session_bars(last_hour, INTERVAL_HOURLY, utc(2026, 9, 24, 20, 0)) == (last_hour, [])


def test_non_session_day_keeps_every_row():
    """Thanksgiving: no session, so a row dated that day (never real) is not
    today's open session and is not dropped; weekends the same."""
    rows = daily((11, 25), (11, 26))
    assert drop_open_session_bars(rows, INTERVAL_DAILY, utc(2026, 11, 26, 17, 0)) == (rows, [])
    assert drop_open_session_bars(daily((9, 25)), INTERVAL_DAILY, utc(2026, 9, 26, 17, 0))[1] == []


def test_drop_open_session_bars_edges():
    assert drop_open_session_bars([], INTERVAL_DAILY, utc(2026, 9, 24, 17)) == ([], [])
    with pytest.raises(ValueError):
        drop_open_session_bars(daily((9, 24)), "1w", utc(2026, 9, 24, 17))
    # A naive hourly start is read as UTC.
    naive = [{"ts": datetime(2026, 9, 24, 17, 30), "volume": 1}]
    assert drop_open_session_bars(naive, INTERVAL_HOURLY, utc(2026, 9, 24, 17, 45))[1] == naive


def test_calendar_built_lazily_and_rebuilt_out_of_range():
    bar_session.reset()
    before = bar_session.builds
    assert bar_session.session_times(datetime(2026, 9, 24).date())[1] == utc(2026, 9, 24, 20)
    assert bar_session.session_times(datetime(2026, 9, 25).date()) is not None
    assert bar_session.builds == before + 1, "one build serves nearby dates"
    bar_session.session_times(datetime(2028, 3, 1).date())
    assert bar_session.builds == before + 2, "a date outside the bounds rebuilds"
    assert bar_session.open_session(utc(2026, 9, 24, 13, 0)) is None, "09:00 ET is before the open"
    assert bar_session.open_session(utc(2026, 9, 24, 14, 0))[0] == datetime(2026, 9, 24).date()


def test_importing_main_builds_no_calendar():
    """Importing the app loads no calendar (risk-shield's and ai-agent's rule)."""
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys, main, bar_session; "
         "print('exchange_calendars' in sys.modules, bar_session.builds)"],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == "False 0"


# ── sessionSoFar (spec 4.8b decision 16) ─────────────────────────

from bar_session import read_session_so_far, session_so_far, stash_session_so_far  # noqa: E402
from tests.fake_redis import FakeRedis  # noqa: E402


def _prior(n=20, volume=1_000_000, close=100.0):
    return [{"ts": datetime(2026, 8, 1 + i % 28), "open": close, "high": close, "low": close,
             "close": close, "volume": volume} for i in range(n)]


TODAY_ROW = {"ts": datetime(2026, 9, 24), "open": 101.0, "high": 103.0, "low": 99.5,
             "close": 102.0, "volume": 600_000}


def test_session_so_far_block():
    """Hand values. 2026-09-24 opens 13:30Z, closes 20:00Z (390 min); at
    16:45Z 195 min have run -> 0.5. Prior close 100 -> +2 %; 600,000 so far
    over half a session against a 1,000,000 mean -> rvolScaled 1.2."""
    block = session_so_far(TODAY_ROW, _prior(), utc(2026, 9, 24, 16, 45),
                           utc(2026, 9, 24, 13, 30), utc(2026, 9, 24, 20, 0))
    assert block == {"open": 101.0, "high": 103.0, "low": 99.5, "last": 102.0,
                     "volumeSoFar": 600_000, "sessionElapsedFrac": 0.5,
                     "changeVsPriorClosePct": 2.0, "rvolScaled": 1.2, "inProgress": True}
    # Early close 2026-11-27: 14:30Z -> 18:00Z is 210 min; at 16:15Z, 105 min = 0.5.
    early = session_so_far(TODAY_ROW, _prior(), utc(2026, 11, 27, 16, 15),
                           utc(2026, 11, 27, 14, 30), utc(2026, 11, 27, 18, 0))
    assert early["sessionElapsedFrac"] == 0.5 and early["rvolScaled"] == 1.2
    # The first 5 minutes: no scaled RVOL. Fewer than 20 prior sessions: none either.
    first = session_so_far(TODAY_ROW, _prior(), utc(2026, 9, 24, 13, 33),
                           utc(2026, 9, 24, 13, 30), utc(2026, 9, 24, 20, 0))
    assert first["rvolScaled"] is None and first["sessionElapsedFrac"] == pytest.approx(3 / 390)
    short = session_so_far(TODAY_ROW, _prior(19), utc(2026, 9, 24, 16, 45),
                           utc(2026, 9, 24, 13, 30), utc(2026, 9, 24, 20, 0))
    assert short["rvolScaled"] is None and short["changeVsPriorClosePct"] == 2.0
    assert session_so_far(TODAY_ROW, [], utc(2026, 9, 24, 16, 45), utc(2026, 9, 24, 13, 30),
                          utc(2026, 9, 24, 20, 0))["changeVsPriorClosePct"] is None


@pytest.mark.asyncio
async def test_session_so_far_null_after_close():
    """Stashed at 12:45 ET; read in the session it is served, read at or after
    the close it is null whatever Redis still holds."""
    redis = FakeRedis()
    stored = await stash_session_so_far(redis, "AAPL", [TODAY_ROW], _prior(), utc(2026, 9, 24, 16, 45))
    assert stored["rvolScaled"] == 1.2
    assert await read_session_so_far(redis, "AAPL", utc(2026, 9, 24, 17, 0)) == stored
    assert await read_session_so_far(redis, "AAPL", utc(2026, 9, 24, 20, 0)) is None
    assert await read_session_so_far(redis, "AAPL", utc(2026, 9, 26, 15, 0)) is None   # Saturday


@pytest.mark.asyncio
async def test_session_so_far_absent_without_stash():
    redis = FakeRedis()
    assert await read_session_so_far(redis, "AAPL", utc(2026, 9, 24, 17, 0)) is None
    assert await read_session_so_far(None, "AAPL", utc(2026, 9, 24, 17, 0)) is None
    # Nothing is stashed pre-market (no session trades yet), without Redis,
    # without a dropped row, or when Redis fails (fail-open, logged).
    assert await stash_session_so_far(redis, "AAPL", [TODAY_ROW], _prior(), utc(2026, 9, 24, 12, 0)) is None
    assert await stash_session_so_far(None, "AAPL", [TODAY_ROW], _prior(), utc(2026, 9, 24, 17, 0)) is None
    assert await stash_session_so_far(redis, "AAPL", [], _prior(), utc(2026, 9, 24, 17, 0)) is None
    assert redis.keys() == []
    broken = FakeRedis(fail_on={"set", "get"})
    assert await stash_session_so_far(broken, "AAPL", [TODAY_ROW], _prior(), utc(2026, 9, 24, 17, 0)) is None
    assert await read_session_so_far(broken, "AAPL", utc(2026, 9, 24, 17, 0)) is None
