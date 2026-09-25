"""
TradingFirm — Single-Ticker Refresh Endpoint Tests

Verifies POST /stock/{ticker}/refresh against a fully mocked asyncpg pool
and the offline FixtureProvider — zero network, zero real database.

The endpoint function is called directly (not via TestClient/lifespan) so
tests exercise the real route logic without needing Redis/Postgres to be
reachable, matching this repo's "test the logic, not the framework" style
(see test_bars_store.py).

The 429 cooldown tests use the tiny in-process fake Redis in
tests/fake_redis.py rather than a mocked-away client, so the real cache.py
cooldown code path actually runs — a mock would make the cooldown untestable.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

import main
from providers.fixture_provider import FixtureProvider
from tests.fake_redis import FakeRedis


def _make_pool():
    """Build a MagicMock asyncpg pool whose acquire() context manager
    yields a connection with async executemany (matches test_bars_store.py)."""
    conn = AsyncMock()
    conn.executemany = AsyncMock()

    pool = MagicMock()
    acquire_cm = AsyncMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=acquire_cm)
    return pool, conn


@pytest.fixture(autouse=True)
def reset_app_state():
    """Give every test a clean, isolated app.state (module-level app is
    shared across the whole test session)."""
    main.app.state.provider = FixtureProvider()
    main.app.state.db_pool = None
    main.app.state.redis = None
    main.app.state.memory = main.InMemoryStore()
    yield


@pytest.mark.asyncio
async def test_refresh_success_returns_counts_and_persists():
    pool, conn = _make_pool()
    main.app.state.db_pool = pool

    provider = FixtureProvider()
    daily_df = provider.extract_ticker_df(await provider.download_daily(["AAPL"]), "AAPL")
    hourly_df = provider.extract_ticker_df(await provider.download_hourly(["AAPL"]), "AAPL")

    result = await main.refresh_stock("aapl")

    assert result == {
        "ticker": "AAPL",
        "dailyBars": len(daily_df),
        "hourlyBars": len(hourly_df),
        # Part 2.3: the earnings step always reports one shape. This pool
        # returns no stored bars, so report dates cannot be validated and
        # the step stops before either source is called.
        "earningsDates": {"source": None, "stored": 0, "dropped": 0, "reason": "no_bars"},
    }
    assert conn.executemany.await_count == 2


@pytest.mark.asyncio
async def test_refresh_invalid_ticker_returns_400():
    with pytest.raises(HTTPException) as exc_info:
        await main.refresh_stock("AAPL1")

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_refresh_db_unavailable_returns_503():
    main.app.state.db_pool = None  # already the fixture default; explicit for clarity

    with pytest.raises(HTTPException) as exc_info:
        await main.refresh_stock("AAPL")

    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_refresh_within_cooldown_returns_429_via_redis():
    pool, _conn = _make_pool()
    main.app.state.db_pool = pool
    main.app.state.redis = FakeRedis()

    first = await main.refresh_stock("MSFT")
    assert first["ticker"] == "MSFT"

    with pytest.raises(HTTPException) as exc_info:
        await main.refresh_stock("msft")  # different case must hit the same key

    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_refresh_cooldown_falls_back_to_memory_when_redis_down():
    pool, _conn = _make_pool()
    main.app.state.db_pool = pool
    main.app.state.redis = None  # no Redis at all — must not fail open

    first = await main.refresh_stock("SPY")
    assert first["ticker"] == "SPY"

    with pytest.raises(HTTPException) as exc_info:
        await main.refresh_stock("spy")  # different case must hit the same in-memory entry

    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_refresh_failed_fetch_does_not_start_cooldown():
    """A provider failure must not lock the ticker out for 15 minutes."""
    pool, _conn = _make_pool()
    main.app.state.db_pool = pool
    main.app.state.redis = FakeRedis()

    failing_provider = MagicMock()
    failing_provider.download_daily = AsyncMock(side_effect=RuntimeError("boom"))
    main.app.state.provider = failing_provider

    with pytest.raises(HTTPException) as exc_info:
        await main.refresh_stock("AAPL")
    assert exc_info.value.status_code == 502

    # Cooldown must still be clear — the failed call never set it.
    main.app.state.provider = FixtureProvider()
    result = await main.refresh_stock("AAPL")
    assert result["ticker"] == "AAPL"


@pytest.mark.asyncio
async def test_refresh_429s_distinguishable_for_ai_agent():
    """Pinned for ai-agent's journal scorer (spec 4.5 decision 2), which
    reads the two 429s differently: the cooldown carries Retry-After (the
    ticker is requeued once, never treated as fresh), the provider's rate
    limit does not (the scorer stops for the night). Drop Retry-After from
    one, or add it to the other, and the scorer misreads a refusal."""
    pool, _conn = _make_pool()
    main.app.state.db_pool = pool
    main.app.state.redis = FakeRedis()

    await main.refresh_stock("NVDA")
    with pytest.raises(HTTPException) as cooldown:
        await main.refresh_stock("NVDA")
    assert cooldown.value.status_code == 429
    assert int(cooldown.value.headers["Retry-After"]) > 0

    limited = MagicMock()
    limited.download_daily = AsyncMock(side_effect=RuntimeError("429 Too Many Requests"))
    main.app.state.provider = limited
    with pytest.raises(HTTPException) as provider:
        await main.refresh_stock("AMD")
    assert provider.value.status_code == 429
    assert not (provider.value.headers or {}).get("Retry-After")


@pytest.mark.asyncio
async def test_refresh_drops_open_session_bar(monkeypatch):
    """Part 4.8b-de (spec 4.8b decision 15): refreshed while the fixture's last
    session is still trading, that session's daily row and its unfinished
    hourly rows are not stored. The dossier's stale refresh is this helper."""
    from datetime import datetime, timedelta, timezone

    import bar_session

    pool, conn = _make_pool()
    main.app.state.db_pool = pool
    provider = FixtureProvider()
    daily_df = provider.extract_ticker_df(await provider.download_daily(["AAPL"]), "AAPL")
    hourly_df = provider.extract_ticker_df(await provider.download_hourly(["AAPL"]), "AAPL")
    last_day = daily_df.index[-1].date()
    times = bar_session.session_times(last_day)
    assert times is not None, "the fixture's last daily row is a session"
    now = times[0] + timedelta(minutes=45)          # 45 min into that session
    monkeypatch.setattr(bar_session, "utc_now", lambda: now)

    result = await main.refresh_stock("AAPL")

    hourly_open = sum(1 for ts in hourly_df.index
                      if ts.to_pydatetime().astimezone(timezone.utc) + timedelta(hours=1) > now)
    assert result["dailyBars"] == len(daily_df) - 1
    assert result["hourlyBars"] == len(hourly_df) - hourly_open
    daily_rows = conn.executemany.await_args_list[0].args[1]
    assert all(row[2].date() != last_day for row in daily_rows)


@pytest.mark.asyncio
async def test_session_so_far_not_in_cached_body(monkeypatch):
    """Refreshed in session: the dropped row becomes sessionSoFar (TTL to the
    close); /indicators serves it on the way out while the cached body keeps
    it null, and a read after the close is null (spec 4.8b decision 16)."""
    import json
    from datetime import timedelta

    import bar_session
    from cache import indicators_key, session_key

    pool, conn = _make_pool()
    main.app.state.db_pool = pool
    redis = FakeRedis()
    main.app.state.redis = redis
    provider = FixtureProvider()
    daily_df = provider.extract_ticker_df(await provider.download_daily(["AAPL"]), "AAPL")
    opens, closes = bar_session.session_times(daily_df.index[-1].date())
    now = opens + timedelta(minutes=90)
    monkeypatch.setattr(bar_session, "utc_now", lambda: now)

    await main.refresh_stock("AAPL")
    stash = json.loads(await redis.get(session_key("AAPL")))
    last = daily_df.iloc[-1]
    assert stash["last"] == pytest.approx(float(last["Close"])) and stash["inProgress"] is True
    assert stash["sessionElapsedFrac"] == pytest.approx(90 / ((closes - opens).total_seconds() / 60))
    assert 0 < await redis.ttl(session_key("AAPL")) <= (closes - now).total_seconds()

    # The stored daily rows are what /indicators reads: feed them back.
    daily_rows = conn.executemany.await_args_list[0].args[1]
    rows = [{"ts": r[2], "open": r[3], "high": r[4], "low": r[5], "close": r[6], "volume": r[7]}
            for r in daily_rows]
    conn.fetch = AsyncMock(side_effect=lambda q, *a: rows if a[:2] == ("AAPL", "1d") else [])
    conn.fetchrow = AsyncMock(return_value=None)
    served = await main.get_indicators("AAPL")
    assert served.session_so_far is not None and served.session_so_far.last == stash["last"]
    assert "sessionSoFar" not in json.loads(await redis.get(indicators_key("AAPL")))
    hit = await main.get_indicators("AAPL")
    assert hit.cached is True and hit.session_so_far is not None

    monkeypatch.setattr(bar_session, "utc_now", lambda: closes)
    assert (await main.get_indicators("AAPL")).session_so_far is None
