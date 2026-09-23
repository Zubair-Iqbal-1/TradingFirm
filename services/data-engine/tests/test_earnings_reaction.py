"""
TradingFirm — Earnings reaction history (Part 2.3, commit 2).

The pure function (indicators/earnings.py), the generic events query
(db.get_events) and the wrapper that joins them. Zero network, zero real
database: synthetic bars with hand-computed expectations, plus one
end-to-end pass over the recorded AAPL fixtures through a mocked pool.
"""

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from db import get_events
from indicators.earnings import earnings_reactions
from providers.context.earnings import earnings_reaction_history

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TODAY = date(2026, 9, 9)


# ── builders ─────────────────────────────────────────────────────────────


def _weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _bars(dates, *, opens=None, closes=None, volumes=None) -> list[dict]:
    rows = []
    for i, d in enumerate(dates):
        close = closes[i] if closes else 100.0
        open_ = opens[i] if opens else close
        rows.append({
            "ts": datetime(d.year, d.month, d.day, tzinfo=timezone.utc),
            "open": open_, "high": max(open_, close) + 1, "low": min(open_, close) - 1,
            "close": close, "volume": volumes[i] if volumes else 1_000_000,
        })
    return rows


def _event(d, *, source="yfinance", hour="amc", validated=True,
           estimate=1.0, reported=1.1, surprise=10.0, meta=None) -> dict:
    block = {
        "source": source, "validated": validated, "hour": hour,
        "epsEstimate": estimate, "epsReported": reported, "surprisePct": surprise,
    }
    return {
        "ticker": "AAPL",
        "event_type": "earnings",
        "event_at": datetime(d.year, d.month, d.day, tzinfo=timezone.utc),
        "meta": meta if meta is not None else {"earnings": block},
    }


def _make_pool(fetch_results):
    """Mocked asyncpg pool. fetch_results is a list, one per fetch() call."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=list(fetch_results))
    pool = MagicMock()
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=cm)
    return pool, conn


def _event_row(d, meta: dict) -> dict:
    return {
        "ticker": "AAPL", "event_type": "earnings",
        "event_at": datetime(d.year, d.month, d.day, tzinfo=timezone.utc),
        "meta": json.dumps(meta),
    }


# ── db.get_events ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_events_sql_and_params():
    pool, conn = _make_pool([[]])
    since = datetime(2023, 9, 9, tzinfo=timezone.utc)
    until = datetime(2026, 9, 9, tzinfo=timezone.utc)

    await get_events(pool, "AAPL", event_type="earnings", since=since, until=until)

    query, *args = conn.fetch.await_args.args
    assert "SELECT ticker, event_type, event_at, meta" in query
    assert "FROM data_engine.events" in query
    assert "ticker = $1" in query and "event_type = $2" in query
    assert "event_at >= $3" in query and "event_at <= $4" in query
    assert "ORDER BY event_at ASC" in query
    assert args == ["AAPL", "earnings", since, until]


@pytest.mark.asyncio
async def test_get_events_without_filters_only_binds_ticker():
    pool, conn = _make_pool([[]])
    await get_events(pool, "AAPL")
    query, *args = conn.fetch.await_args.args
    assert args == ["AAPL"]
    assert "event_type" not in query.split("WHERE")[1]


@pytest.mark.asyncio
async def test_get_events_decodes_meta():
    meta = {"earnings": {"source": "yfinance", "validated": True}}
    pool, _ = _make_pool([[_event_row(date(2026, 7, 30), meta)]])

    events = await get_events(pool, "AAPL", event_type="earnings")

    assert len(events) == 1
    assert events[0]["meta"] == meta
    assert events[0]["event_at"] == datetime(2026, 7, 30, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_get_events_malformed_meta_is_empty_dict(caplog):
    """A meta we cannot parse must not hide the date it belongs to."""
    row = {"ticker": "AAPL", "event_type": "earnings",
           "event_at": datetime(2026, 7, 30, tzinfo=timezone.utc), "meta": "{not json"}
    pool, _ = _make_pool([[row]])

    events = await get_events(pool, "AAPL")

    assert events[0]["meta"] == {}
    assert "unparseable meta" in caplog.text


# ── session selection ────────────────────────────────────────────────────


def test_reactions_amc_uses_next_session():
    days = _weekdays(date(2026, 7, 27), 5)          # Mon..Fri
    report = days[1]
    bars = _bars(days, closes=[100, 100, 110, 110, 110], opens=[100, 100, 105, 110, 110])

    out = earnings_reactions([_event(report, hour="amc")], bars, today=TODAY)

    assert len(out["reactions"]) == 1
    row = out["reactions"][0]
    assert row["reportDate"] == report.isoformat()
    assert row["session"] == days[2].isoformat()    # the day AFTER the report
    assert row["hourAssumed"] is False


def test_reactions_bmo_uses_same_session():
    days = _weekdays(date(2026, 7, 27), 5)
    report = days[2]
    bars = _bars(days, closes=[100, 100, 110, 110, 110], opens=[100, 100, 105, 110, 110])

    out = earnings_reactions([_event(report, hour="bmo")], bars, today=TODAY)

    assert out["reactions"][0]["session"] == report.isoformat()


def test_reactions_dmh_uses_same_session():
    days = _weekdays(date(2026, 7, 27), 5)
    report = days[2]
    bars = _bars(days)

    out = earnings_reactions([_event(report, hour="dmh")], bars, today=TODAY)

    assert out["reactions"][0]["session"] == report.isoformat()


def test_reactions_hand_computed_values():
    """prev close 100 → session opens 105, closes 110."""
    days = _weekdays(date(2026, 7, 27), 3)
    bars = _bars(days, closes=[100.0, 110.0, 110.0], opens=[100.0, 105.0, 110.0])

    row = earnings_reactions([_event(days[0], hour="amc")], bars, today=TODAY)["reactions"][0]

    assert row["gapPct"] == 5.0            # (105 - 100) / 100
    assert row["closeToClosePct"] == 10.0  # (110 - 100) / 100


def test_reactions_holiday_tolerance_within_four_days():
    """Report on the Friday before a long weekend: the Tuesday session is
    still the one that absorbed it."""
    bars = _bars([date(2026, 7, 24), date(2026, 7, 28)],
                 closes=[100.0, 110.0], opens=[100.0, 105.0])

    out = earnings_reactions([_event(date(2026, 7, 24), hour="amc")], bars, today=TODAY)

    assert out["reactions"][0]["session"] == "2026-07-28"
    assert out["dataQuality"]["dropped"] == 0


def test_reactions_skips_bar_gap():
    """Six days between the report and the next stored bar is a hole in
    the store, not a reaction."""
    bars = _bars([date(2026, 7, 24), date(2026, 7, 31)])

    out = earnings_reactions([_event(date(2026, 7, 24), hour="amc")], bars, today=TODAY)

    assert out["reactions"] == []
    assert out["dataQuality"]["dropped"] == 1


def test_reactions_surprise_from_meta():
    days = _weekdays(date(2026, 7, 27), 3)
    event = _event(days[0], estimate=1.89, reported=2.02, surprise=6.74)

    row = earnings_reactions([event], _bars(days), today=TODAY)["reactions"][0]

    assert row["epsEstimate"] == 1.89
    assert row["epsReported"] == 2.02
    assert row["surprisePct"] == 6.74
    assert row["source"] == "yfinance"


# ── skips and counts ─────────────────────────────────────────────────────


def test_reactions_no_bars_empty():
    out = earnings_reactions([_event(date(2026, 7, 30))], [], today=TODAY)
    assert out["reactions"] == []
    assert out["dataQuality"]["dropped"] == 1


def test_history_no_rows_returns_null_reactions():
    """No confirmed report at all: null, not [] — never refreshed, or both
    sources were down. 2.4 shows 'no data', not 'no reaction'."""
    out = earnings_reactions([], _bars(_weekdays(date(2026, 7, 27), 3)), today=TODAY)
    assert out["reactions"] is None
    assert out["dataQuality"] == {"source": None, "dropped": 0, "disagreements": 0}


def test_reactions_skips_unconfirmed_events():
    days = _weekdays(date(2026, 7, 27), 3)
    confirmed = _event(days[0])
    # A PAST row we could not validate is a real gap (a future projection
    # is not — test_reactions_skips_future_events covers that).
    projection = _event(days[1], validated=False)
    projection["meta"]["earnings"]["validated"] = False

    out = earnings_reactions([confirmed, projection], _bars(days), today=TODAY)

    assert len(out["reactions"]) == 1
    assert out["dataQuality"]["dropped"] == 1


def test_reactions_legacy_calendar_row_counts_as_confirmed():
    """Part 2.1 rows have no meta.earnings; a reported epsActual confirms them."""
    days = _weekdays(date(2026, 7, 27), 3)
    legacy = _event(days[1], meta={"calendar": {"epsActual": 2.02, "hour": "amc"}})

    out = earnings_reactions([legacy], _bars(days), today=TODAY)

    assert len(out["reactions"]) == 1
    assert out["reactions"][0]["session"] == days[2].isoformat()   # hour from meta.calendar
    assert out["reactions"][0]["hourAssumed"] is False
    assert out["dataQuality"]["source"] is None      # legacy rows name no source


def test_reactions_skips_future_events():
    """A future report is expected, not a data-quality problem."""
    days = _weekdays(date(2026, 7, 27), 3)
    out = earnings_reactions(
        [_event(days[0]), _event(date(2026, 10, 29), validated=False)], _bars(days), today=TODAY
    )
    assert len(out["reactions"]) == 1
    assert out["dataQuality"]["dropped"] == 0


def test_reactions_skips_before_bar_history(caplog):
    """Older than the first stored bar: skipped and logged, but NOT counted.
    `dropped` means "the source gave us something we could not use", and a
    real report we hold no bars for is a limit of our history instead. Same
    treatment as out_of_range on the write side."""
    caplog.set_level(logging.INFO, logger="indicators.earnings")
    days = _weekdays(date(2026, 7, 27), 3)

    out = earnings_reactions([_event(date(2025, 1, 15))], _bars(days), today=TODAY)

    assert out["reactions"] == []
    assert out["dataQuality"]["dropped"] == 0
    assert "older than the first stored bar" in caplog.text


def test_reactions_skips_first_bar_no_previous_close():
    """The reaction lands on the oldest stored bar: no previous close, so
    there is nothing to measure the gap against."""
    days = _weekdays(date(2026, 7, 27), 3)
    out = earnings_reactions([_event(days[0], hour="bmo")], _bars(days), today=TODAY)
    assert out["reactions"] == []
    assert out["dataQuality"]["dropped"] == 1


def test_reactions_skips_after_last_bar():
    days = _weekdays(date(2026, 7, 27), 3)
    out = earnings_reactions([_event(date(2026, 8, 20), hour="amc")], _bars(days), today=TODAY)
    assert out["reactions"] == []
    assert out["dataQuality"]["dropped"] == 1


def test_reactions_skips_bad_prices():
    days = _weekdays(date(2026, 7, 27), 3)
    bars = _bars(days)
    bars[0]["close"] = 0.0                      # previous close of zero
    out = earnings_reactions([_event(days[0], hour="amc")], bars, today=TODAY)
    assert out["reactions"] == []
    assert out["dataQuality"]["dropped"] == 1

    bars2 = _bars(days)
    bars2[1]["open"] = float("nan")
    out2 = earnings_reactions([_event(days[0], hour="amc")], bars2, today=TODAY)
    assert out2["reactions"] == [] and out2["dataQuality"]["dropped"] == 1


def test_reactions_limit_newest_first():
    days = _weekdays(date(2026, 1, 5), 90)
    bars = _bars(days)
    events = [_event(days[i]) for i in (10, 35, 60, 85)]   # ~35 calendar days apart

    out = earnings_reactions(events, bars, limit=2, today=TODAY)

    assert len(out["reactions"]) == 2
    assert out["reactions"][0]["reportDate"] == days[85].isoformat()
    assert out["reactions"][1]["reportDate"] == days[60].isoformat()


@pytest.mark.parametrize("bad", [0, -1, 3.5, True, "8", None])
def test_reactions_rejects_bad_limit(bad):
    with pytest.raises(ValueError):
        earnings_reactions([], [], limit=bad)


# ── the volume rule ──────────────────────────────────────────────────────


def _volume_series(days, spike_index, spike=5_000_000, base=1_000_000):
    vols = [base] * len(days)
    if spike_index is not None:
        vols[spike_index] = spike
    return vols


def test_reactions_unknown_hour_resolved_by_volume():
    """Hour unknown: the session printing >= 2x average volume is the one
    that absorbed the report — never 'whichever moved more'."""
    days = _weekdays(date(2026, 1, 5), 40)
    report_index = 30
    vols = _volume_series(days, report_index + 1)      # the NEXT day is the spike
    closes = [100.0] * len(days)
    closes[report_index + 1] = 90.0                   # big move on the spike day
    closes[report_index] = 101.0                      # small move on the report day
    bars = _bars(days, closes=closes, volumes=vols)

    out = earnings_reactions([_event(days[report_index], hour=None)], bars, today=TODAY)

    row = out["reactions"][0]
    assert row["session"] == days[report_index + 1].isoformat()
    assert row["hourAssumed"] is True
    assert out["dataQuality"]["dropped"] == 0


def test_reactions_unknown_hour_unresolved_dropped():
    days = _weekdays(date(2026, 1, 5), 40)
    bars = _bars(days, volumes=_volume_series(days, None))   # flat volume: no spike

    out = earnings_reactions([_event(days[30], hour=None)], bars, today=TODAY)

    assert out["reactions"] == []
    assert out["dataQuality"]["dropped"] == 1


def test_reactions_unknown_hour_both_sessions_spike_dropped():
    days = _weekdays(date(2026, 1, 5), 40)
    vols = _volume_series(days, 30)
    vols[31] = 5_000_000
    bars = _bars(days, volumes=vols)

    out = earnings_reactions([_event(days[30], hour=None)], bars, today=TODAY)

    assert out["reactions"] == []
    assert out["dataQuality"]["dropped"] == 1


def test_reactions_unknown_hour_insufficient_history_dropped():
    """calc_rvol needs 20 prior sessions; without them nothing is confirmed
    and the report is dropped rather than guessed."""
    days = _weekdays(date(2026, 7, 6), 5)
    bars = _bars(days, volumes=[1_000_000, 1_000_000, 9_000_000, 1_000_000, 1_000_000])

    out = earnings_reactions([_event(days[1], hour=None)], bars, today=TODAY)

    assert out["reactions"] == []
    assert out["dataQuality"]["dropped"] == 1


# ── duplicates and cross-source disagreement ─────────────────────────────


def test_reactions_collapses_same_source_duplicate():
    days = _weekdays(date(2026, 1, 5), 40)
    events = [_event(days[20]), _event(days[22])]      # 2 days apart, one source

    out = earnings_reactions(events, _bars(days), today=TODAY)

    assert len(out["reactions"]) == 1
    assert out["reactions"][0]["reportDate"] == days[20].isoformat()
    assert out["dataQuality"]["disagreements"] == 0
    assert out["dataQuality"]["dropped"] == 0


def test_reactions_collapses_cross_source_adjacent_dates():
    """yfinance says the 30th, Alpha Vantage says the 31st: one report, not
    a disagreement and not a drop. The primary source wins."""
    days = _weekdays(date(2026, 1, 5), 40)
    events = [
        _event(days[21], source="alphavantage"),
        _event(days[20], source="yfinance"),
    ]

    out = earnings_reactions(events, _bars(days), today=TODAY)

    assert len(out["reactions"]) == 1
    assert out["reactions"][0]["reportDate"] == days[20].isoformat()
    assert out["reactions"][0]["source"] == "yfinance"
    assert out["dataQuality"]["disagreements"] == 0
    assert out["dataQuality"]["dropped"] == 0


def test_disagreement_resolved_by_volume():
    """Dates a week apart: the one whose session printed the volume wins."""
    days = _weekdays(date(2026, 1, 5), 40)
    vols = _volume_series(days, 26)                    # spike after days[25]
    bars = _bars(days, volumes=vols)
    events = [
        _event(days[20], source="yfinance"),
        _event(days[25], source="alphavantage"),
    ]

    out = earnings_reactions(events, bars, today=TODAY)

    assert len(out["reactions"]) == 1
    assert out["reactions"][0]["reportDate"] == days[25].isoformat()
    assert out["dataQuality"]["disagreements"] == 1
    assert out["dataQuality"]["dropped"] == 0


def test_disagreement_unresolved():
    """Neither date is volume-confirmed: keep nothing, count both."""
    days = _weekdays(date(2026, 1, 5), 40)
    bars = _bars(days, volumes=_volume_series(days, None))
    events = [
        _event(days[20], source="yfinance"),
        _event(days[25], source="alphavantage"),
    ]

    out = earnings_reactions(events, bars, today=TODAY)

    assert out["reactions"] == []
    assert out["dataQuality"]["disagreements"] == 1
    assert out["dataQuality"]["dropped"] == 2


def test_reactions_source_mixed():
    days = _weekdays(date(2026, 1, 5), 60)
    events = [_event(days[10], source="yfinance"), _event(days[50], source="alphavantage")]

    out = earnings_reactions(events, _bars(days), today=TODAY)

    assert len(out["reactions"]) == 2
    assert out["dataQuality"]["source"] == "mixed"


def test_reactions_data_quality_counts():
    """One good report, one unconfirmed (counted), one before the bar
    history (not counted: our history is short, the source is fine)."""
    days = _weekdays(date(2026, 1, 5), 40)
    unconfirmed = _event(days[30], validated=False)
    unconfirmed["meta"]["earnings"]["validated"] = False
    events = [_event(days[20]), unconfirmed, _event(date(2024, 5, 1))]

    out = earnings_reactions(events, _bars(days), today=TODAY)

    assert len(out["reactions"]) == 1
    assert out["dataQuality"] == {"source": "yfinance", "dropped": 1, "disagreements": 0}


# ── the wrapper ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_history_query_shape():
    pool, conn = _make_pool([[], []])

    await earnings_reaction_history(pool, "aapl", today=TODAY)

    assert conn.fetch.await_count == 2
    events_query, *events_args = conn.fetch.await_args_list[0].args
    bars_query, *bars_args = conn.fetch.await_args_list[1].args

    assert "FROM data_engine.events" in events_query
    assert events_args[0] == "AAPL" and events_args[1] == "earnings"
    assert events_args[2] == datetime(2023, 9, 9, tzinfo=timezone.utc)   # today - 3y

    assert "FROM data_engine.ohlcv_bars" in bars_query
    assert bars_args[0] == "AAPL" and bars_args[1] == "1d"
    assert bars_args[2] == datetime(2023, 9, 9, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_history_rejects_bad_ticker():
    pool, conn = _make_pool([[], []])
    for bad in ("AAPL1", "", "TOOLONG", "A.B", "BRK-B"):
        with pytest.raises(ValueError):
            await earnings_reaction_history(pool, bad, today=TODAY)
    conn.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_history_without_pool_null():
    out = await earnings_reaction_history(None, "AAPL", today=TODAY)
    assert out == {
        "ticker": "AAPL", "reactions": None,
        "dataQuality": {"source": None, "dropped": 0, "disagreements": 0},
    }


@pytest.mark.asyncio
async def test_history_db_raise_propagates():
    pool, conn = _make_pool([RuntimeError("connection reset")])
    with pytest.raises(RuntimeError):
        await earnings_reaction_history(pool, "AAPL", today=TODAY)


@pytest.mark.asyncio
async def test_history_repeat_queries_again():
    """No cache by design: 2.4 caches the whole dossier."""
    pool, conn = _make_pool([[], [], [], []])
    await earnings_reaction_history(pool, "AAPL", today=TODAY)
    await earnings_reaction_history(pool, "AAPL", today=TODAY)
    assert conn.fetch.await_count == 4


@pytest.mark.asyncio
async def test_history_fixture_end_to_end():
    """
    The recorded AAPL daily bars and the four report dates inside their
    window, through the wrapper. Every number below was computed by hand
    from the fixture before this test was written.
    """
    frame = pd.read_json(FIXTURES / "daily" / "AAPL.json", orient="table")
    bar_rows = [
        {"ts": ts.to_pydatetime().replace(tzinfo=timezone.utc),
         "open": float(row["Open"]), "high": float(row["High"]), "low": float(row["Low"]),
         "close": float(row["Close"]), "volume": int(row["Volume"])}
        for ts, row in frame.iterrows()
    ]
    reports = [date(2025, 10, 30), date(2026, 1, 29), date(2026, 4, 30), date(2026, 7, 30)]
    meta = lambda: {"earnings": {  # noqa: E731
        "source": "yfinance", "validated": True, "hour": "amc",
        "epsEstimate": 1.89, "epsReported": 2.02, "surprisePct": 6.74}}
    event_rows = [_event_row(d, meta()) for d in reports]
    event_rows.append(_event_row(date(2026, 10, 29), {"earnings": {
        "source": "yfinance", "validated": False, "hour": "amc"}}))

    pool, _ = _make_pool([event_rows, bar_rows])
    out = await earnings_reaction_history(pool, "AAPL", today=TODAY)

    assert out["ticker"] == "AAPL"
    assert out["dataQuality"] == {"source": "yfinance", "dropped": 0, "disagreements": 0}

    got = {r["reportDate"]: (r["session"], r["gapPct"], r["closeToClosePct"])
           for r in out["reactions"]}
    assert got == {
        "2026-07-30": ("2026-07-31", -8.58, -7.35),
        "2026-04-30": ("2026-05-01", 2.77, 3.24),
        "2026-01-29": ("2026-01-30", -1.20, 0.46),
        "2025-10-30": ("2025-10-31", 2.06, -0.38),
    }
    assert [r["reportDate"] for r in out["reactions"]] == [
        "2026-07-30", "2026-04-30", "2026-01-29", "2025-10-30"]   # newest first


def test_indicators_exports_earnings_reactions():
    import indicators

    assert "earnings_reactions" in indicators.__all__
    assert len(indicators.__all__) == 27   # 4.8a-de: SwingLow, ZoneHistory, last_swing_low, zone_history
    assert indicators.earnings_reactions is earnings_reactions
