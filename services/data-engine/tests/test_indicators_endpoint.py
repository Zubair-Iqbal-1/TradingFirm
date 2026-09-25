"""
TradingFirm — Indicators Endpoint Tests (Part 1.7)

Verifies GET /indicators/{ticker} against a mocked asyncpg pool that
serves the recorded daily fixtures (AAPL, MSFT, SPY) as stored bars, and
the in-process FakeRedis from tests/fake_redis.py. Zero network, zero
real database.

The math itself is tested in test_indicators.py / test_levels.py; here
the response is compared to `swing_snapshot()` on the same frames to
prove the endpoint wires the right series to the right fields. Two tests
go through TestClient so the camelCase aliases are exercised through real
serialization; the rest call the endpoint function directly.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import HTTPException
from fastapi.testclient import TestClient

import main
from cache import indicators_key
from db import bar_records_from_df
from indicators import IndicatorsResponse, swing_snapshot
from providers.fixture_provider import FixtureProvider
from tests.fake_redis import FakeRedis

_PROVIDER = FixtureProvider()


async def _fixture_df(ticker: str):
    return _PROVIDER.extract_ticker_df(await _PROVIDER.download_daily([ticker]), ticker)


async def _fixture_rows(ticker: str) -> list[dict]:
    """Daily fixture as the row dicts get_bars() returns."""
    return bar_records_from_df(await _fixture_df(ticker))


def _make_pool(bars: dict[tuple[str, str], list[dict]], stocks: dict[str, dict] | None = None,
               fetch_side_effect=None, fetchrow_side_effect=None):
    """MagicMock asyncpg pool whose connection serves `bars[(ticker, interval)]`
    for get_bars() queries and `stocks[ticker]` for get_stock()."""
    stocks = stocks or {}

    async def fetch(query, *args):
        ticker, interval = args[0], args[1]
        return list(bars.get((ticker, interval), []))

    async def fetchrow(query, *args):
        return stocks.get(args[0])

    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=fetch_side_effect or fetch)
    conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect or fetchrow)
    conn.executemany = AsyncMock()

    pool = MagicMock()
    acquire_cm = AsyncMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=acquire_cm)
    return pool, conn


@pytest.fixture(autouse=True)
def reset_app_state(monkeypatch):
    # 4.8b-de: /indicators fills in sessionSoFar during market hours. Pin the
    # clock to a Saturday so these tests never depend on when they run.
    from datetime import datetime, timezone

    import bar_session
    monkeypatch.setattr(bar_session, "utc_now", lambda: datetime(2026, 9, 26, 15, 0, tzinfo=timezone.utc))
    main.app.state.provider = FixtureProvider()
    main.app.state.db_pool = None
    main.app.state.redis = None
    main.app.state.memory = main.InMemoryStore()
    yield


@pytest_asyncio.fixture
async def full_pool():
    """AAPL with SPY and an XLK benchmark (MSFT's fixture stands in for XLK
    bars) and a stocks row giving AAPL the Technology sector."""
    aapl, spy, msft = await _fixture_rows("AAPL"), await _fixture_rows("SPY"), await _fixture_rows("MSFT")
    bars = {("AAPL", "1d"): aapl, ("SPY", "1d"): spy, ("XLK", "1d"): msft}
    stocks = {"AAPL": {"ticker": "AAPL", "name": "Apple Inc.", "sector": "Technology",
                       "industry": "Consumer Electronics", "market_cap": 1, "float_shares": 1,
                       "updated_at": datetime(2026, 9, 6, tzinfo=timezone.utc)}}
    pool, conn = _make_pool(bars, stocks)
    return pool, conn, bars


# ── Happy path (through TestClient: real serialization) ───────────────────

@pytest.mark.asyncio
async def test_indicators_returns_full_set(full_pool):
    pool, _conn, _bars = full_pool
    main.app.state.db_pool = pool

    expected = swing_snapshot(
        await _fixture_df("AAPL"),
        (await _fixture_df("SPY"))["Close"],
        (await _fixture_df("MSFT"))["Close"],
    )
    expected_json = IndicatorsResponse(
        ticker="AAPL", computed_at=datetime.now(timezone.utc), **expected,
    ).model_dump(mode="json", by_alias=True)

    # No `with`: the context-manager form runs the lifespan, which would
    # replace the mocked pool/redis on app.state with real connections.
    resp = TestClient(main.app).get("/indicators/aapl")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ticker"] == "AAPL"
    assert body["bars"] == 252
    assert body["cached"] is False
    assert body["sector"] == "Technology"
    assert body["benchmarks"] == {"spy": {"ticker": "SPY", "bars": 252},
                                  "sector": {"ticker": "XLK", "bars": 252}}
    assert body["asOf"].startswith("2026-09-04")
    for key in ("ema20", "ema50", "ema200", "atr14", "rvol", "rsi14", "macd", "macdSignal",
                "macdHist", "pos52w", "ext20", "ext50", "rsSpy5", "rsSpy20", "rsSector5",
                "rsSector20", "avgDollarVolume20", "gapPct", "gaps20", "zones", "close"):
        assert body[key] == expected_json[key], key
        if isinstance(body[key], float):
            assert body[key] is not None
    assert len(body["gaps20"]) == 20
    assert body["zones"]["support"] and body["zones"]["resistance"]
    assert set(body["zones"]["support"][0]) == {"low", "high", "price", "score", "methods",
                                                "tests", "recent", "volumeNode",
                                                "touches", "held", "broke", "lastTouch",
                                                "heldBelow", "brokeBelow", "heldAbove", "brokeAbove"}
    assert body["lastSwingLow"] == expected_json["lastSwingLow"]
    assert set(body["lastSwingLow"]) == {"price", "date"}


@pytest.mark.asyncio
async def test_indicators_camelcase_keys(full_pool):
    pool, _conn, _bars = full_pool
    main.app.state.db_pool = pool

    body = TestClient(main.app).get("/indicators/AAPL").json()

    assert set(body) == {
        "ticker", "asOf", "bars", "close", "sector",
        "ema20", "ema50", "ema200", "atr14", "rvol", "rsi14",
        "macd", "macdSignal", "macdHist", "pos52w", "ext20", "ext50",
        "rsSpy5", "rsSpy20", "rsSector5", "rsSector20", "avgDollarVolume20",
        "gapPct", "gaps20", "zones", "lastSwingLow", "sessionSoFar", "volumeRead", "trendRead",
        "momentumRead", "rangeRead", "benchmarks", "computedAt", "cached",
    }
    assert not any("_" in k for k in body)


@pytest.mark.asyncio
async def test_last_swing_low_in_response(full_pool):
    """4.8a-de decision 3: the newest fractal swing low of the fixture, its
    date being the bar's own date (midnight UTC storage, no conversion)."""
    pool, _conn, bars = full_pool
    main.app.state.db_pool = pool
    body = TestClient(main.app).get("/indicators/AAPL").json()
    df = await _fixture_df("AAPL")
    from indicators import last_swing_low
    pivot = last_swing_low(df["High"], df["Low"])
    assert body["lastSwingLow"] == {"price": pivot.price, "date": df.index[pivot.index].date().isoformat()}
    assert body["lastSwingLow"]["date"] <= body["asOf"][:10]
    assert any(z["touches"] > 0 for z in body["zones"]["support"] + body["zones"]["resistance"])


@pytest.mark.asyncio
async def test_cached_pre_part_body_still_validates(full_pool):
    """A body cached before 4.8a-de (no lastSwingLow, no zone history) is
    still served, with the defaults, until its TTL: fail-open, no recompute."""
    pool, conn, _bars = full_pool
    main.app.state.db_pool = pool
    redis = FakeRedis()
    main.app.state.redis = redis
    fresh = await main.get_indicators("AAPL")
    old_body = fresh.model_dump(mode="json", by_alias=True, exclude={
        "cached", "last_swing_low", "session_so_far",
        # 4.8b-de: a body cached before the four read blocks existed
        "volume_read", "trend_read", "momentum_read", "range_read"})
    for side in ("support", "resistance"):
        for z in old_body["zones"][side]:
            for key in ("touches", "held", "broke", "lastTouch"):
                del z[key]
    import json
    await redis.set(indicators_key("AAPL"), json.dumps(old_body), ex=900)
    reads = conn.fetch.await_count
    served = await main.get_indicators("AAPL")
    assert served.cached is True and conn.fetch.await_count == reads
    assert served.last_swing_low is None
    assert served.zones.support[0].touches == 0 and served.zones.support[0].last_touch is None
    assert served.volume_read is None and served.momentum_read is None and served.range_read is None


def test_snapshot_from_bars_script():
    """scripts/snapshot_from_bars.py (4.8a-de decision 8): stored bar rows in,
    the camelCase snapshot out, bars after `asOf` dropped, the stored close
    checked; a malformed row is marked, never a raise."""
    import io
    import json
    from scripts import snapshot_from_bars as script

    n = 30
    bars = [{"ts": f"2026-01-{i + 1:02d}T00:00:00+00:00", "open": 100.0 + i, "high": 101.0 + i,
             "low": 99.0 + i, "close": 100.5 + i, "volume": 1000} for i in range(n)]
    rows = [
        {"ticker": "AAA", "asOf": "2026-01-20", "storedClose": 119.5, "bars": bars},
        {"ticker": "BBB", "asOf": "2026-01-20", "storedClose": 100.0, "bars": bars},   # mismatch
        {"ticker": "CCC", "bars": "junk"},
    ]
    out = io.StringIO()
    assert script.main(io.StringIO(json.dumps(rows)), out) == 0
    result = json.loads(out.getvalue())
    assert [r["ticker"] for r in result] == ["AAA", "BBB", "CCC"]
    aaa = result[0]
    assert aaa["error"] is None and aaa["closeMismatch"] is False
    assert aaa["indicators"]["bars"] == 20 and aaa["indicators"]["close"] == 119.5
    assert aaa["indicators"]["asOf"].startswith("2026-01-20")
    assert "lastSwingLow" in aaa["indicators"] and "zones" in aaa["indicators"]
    assert not any("_" in k for k in aaa["indicators"])
    assert result[1]["closeMismatch"] is True
    assert result[2]["error"] and result[2]["indicators"] is None
    # never a fixture, never a provider: pure arithmetic on the rows given
    import inspect
    src = inspect.getsource(script)
    assert "from providers" not in src and "import providers" not in src and "asyncpg" not in src


# ── Failure branches: closed ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_indicators_bad_ticker_400():
    main.app.state.db_pool = MagicMock()
    with pytest.raises(HTTPException) as exc_info:
        await main.get_indicators("AAPL1")
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_indicators_db_pool_absent_503():
    main.app.state.db_pool = None
    with pytest.raises(HTTPException) as exc_info:
        await main.get_indicators("AAPL")
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_indicators_db_raise_503():
    pool, _conn = _make_pool({}, fetch_side_effect=RuntimeError("connection reset"))
    main.app.state.db_pool = pool
    with pytest.raises(HTTPException) as exc_info:
        await main.get_indicators("AAPL")
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_indicators_benchmark_read_raise_503():
    """Ticker bars read fine, SPY read raises → 503 via the same DB path."""
    aapl = await _fixture_rows("AAPL")

    async def fetch(query, *args):
        if args[0] == "SPY":
            raise RuntimeError("connection reset")
        return aapl

    pool, _conn = _make_pool({}, fetch_side_effect=fetch)
    main.app.state.db_pool = pool
    with pytest.raises(HTTPException) as exc_info:
        await main.get_indicators("AAPL")
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_indicators_get_stock_raise_503():
    aapl = await _fixture_rows("AAPL")
    pool, _conn = _make_pool({("AAPL", "1d"): aapl}, fetchrow_side_effect=RuntimeError("boom"))
    main.app.state.db_pool = pool
    with pytest.raises(HTTPException) as exc_info:
        await main.get_indicators("AAPL")
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_indicators_no_bars_404():
    pool, conn = _make_pool({})
    main.app.state.db_pool = pool
    with pytest.raises(HTTPException) as exc_info:
        await main.get_indicators("AAPL")
    assert exc_info.value.status_code == 404
    assert conn.fetch.await_count == 1  # stops at the first empty read


# ── Failure branches: open ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_indicators_missing_spy_nulls_rs():
    aapl = await _fixture_rows("AAPL")
    pool, _conn = _make_pool({("AAPL", "1d"): aapl})
    main.app.state.db_pool = pool

    result = await main.get_indicators("AAPL")

    assert result.rs_spy_5 is None and result.rs_spy_20 is None
    assert result.benchmarks.spy.ticker == "SPY" and result.benchmarks.spy.bars == 0
    assert result.ema20 is not None  # everything else still computed


@pytest.mark.asyncio
async def test_indicators_missing_sector_nulls_sector_rs():
    """No stocks row → no ETF → sector RS null; SPY RS still present."""
    aapl, spy = await _fixture_rows("AAPL"), await _fixture_rows("SPY")
    pool, _conn = _make_pool({("AAPL", "1d"): aapl, ("SPY", "1d"): spy})
    main.app.state.db_pool = pool

    result = await main.get_indicators("AAPL")

    assert result.sector is None
    assert result.rs_sector_5 is None and result.rs_sector_20 is None
    assert result.benchmarks.sector.ticker is None and result.benchmarks.sector.bars == 0
    assert result.rs_spy_20 is not None


@pytest.mark.asyncio
async def test_indicators_sector_etf_bars_absent_nulls_sector_rs():
    """Stocks row says Technology, but no XLK bars are stored."""
    aapl = await _fixture_rows("AAPL")
    stocks = {"AAPL": {"ticker": "AAPL", "name": "Apple", "sector": "Technology", "industry": "x",
                       "market_cap": 0, "float_shares": 0, "updated_at": None}}
    pool, _conn = _make_pool({("AAPL", "1d"): aapl}, stocks)
    main.app.state.db_pool = pool

    result = await main.get_indicators("AAPL")

    assert result.sector == "Technology"
    assert result.benchmarks.sector.ticker == "XLK" and result.benchmarks.sector.bars == 0
    assert result.rs_sector_20 is None


@pytest.mark.asyncio
async def test_indicators_short_history_nulls():
    aapl = (await _fixture_rows("AAPL"))[:5]
    pool, _conn = _make_pool({("AAPL", "1d"): aapl})
    main.app.state.db_pool = pool

    result = await main.get_indicators("AAPL")

    assert result.bars == 5
    assert result.atr14 is None and result.rsi14 is None and result.ext20 is None
    assert result.avg_dollar_volume_20 is None
    assert result.rvol == 0.0  # 1.5 convention, not null
    assert result.ema20 is not None and result.macd is not None
    assert len(result.gaps20) == 5 and result.gaps20[0] is None


@pytest.mark.asyncio
async def test_indicators_redis_down_computes(full_pool):
    pool, _conn, _bars = full_pool
    main.app.state.db_pool = pool
    main.app.state.redis = None

    result = await main.get_indicators("AAPL")
    assert result.cached is False and result.ema20 is not None


@pytest.mark.asyncio
async def test_indicators_redis_get_raises_computes(full_pool):
    pool, _conn, _bars = full_pool
    main.app.state.db_pool = pool
    main.app.state.redis = FakeRedis(fail_on={"get"})

    result = await main.get_indicators("AAPL")
    assert result.cached is False and result.ema20 is not None


@pytest.mark.asyncio
async def test_indicators_redis_set_raises_still_200(full_pool):
    pool, _conn, _bars = full_pool
    main.app.state.db_pool = pool
    redis = FakeRedis(fail_on={"set"})
    main.app.state.redis = redis

    result = await main.get_indicators("AAPL")
    assert result.cached is False and result.ema20 is not None
    assert redis.keys() == []


@pytest.mark.asyncio
async def test_indicators_corrupt_cache_recomputes(full_pool):
    pool, conn, _bars = full_pool
    main.app.state.db_pool = pool
    redis = FakeRedis()
    main.app.state.redis = redis
    await redis.set(indicators_key("AAPL"), "{not json", ex=900)

    result = await main.get_indicators("AAPL")

    assert result.cached is False and result.ema20 is not None
    assert conn.fetch.await_count > 0
    # ...and the good body has replaced the corrupt one.
    second = await main.get_indicators("AAPL")
    assert second.cached is True


@pytest.mark.asyncio
async def test_indicators_schema_mismatch_cache_recomputes(full_pool):
    """Valid JSON that no longer matches the model (e.g. after a deploy)
    is also a miss, not a 500."""
    pool, _conn, _bars = full_pool
    main.app.state.db_pool = pool
    redis = FakeRedis()
    main.app.state.redis = redis
    await redis.set(indicators_key("AAPL"), '{"ticker": "AAPL"}', ex=900)

    result = await main.get_indicators("AAPL")
    assert result.cached is False and result.ema20 is not None


@pytest.mark.asyncio
async def test_indicators_repeat_hits_cache(full_pool):
    pool, conn, _bars = full_pool
    main.app.state.db_pool = pool
    redis = FakeRedis()
    main.app.state.redis = redis

    first = await main.get_indicators("AAPL")
    reads_after_first = conn.fetch.await_count
    second = await main.get_indicators("aapl")  # case must hit the same key

    assert first.cached is False
    assert second.cached is True
    assert conn.fetch.await_count == reads_after_first  # no DB query on the hit
    assert second.computed_at == first.computed_at
    assert second.model_dump(exclude={"cached"}) == first.model_dump(exclude={"cached"})
    assert redis.keys() == ["tf:cache:indicators:AAPL"]
    assert 0 < await redis.ttl("tf:cache:indicators:AAPL") <= 900


@pytest.mark.asyncio
async def test_refresh_invalidates_indicators_cache(full_pool):
    pool, _conn, _bars = full_pool
    main.app.state.db_pool = pool
    redis = FakeRedis()
    main.app.state.redis = redis

    await main.get_indicators("AAPL")
    assert "tf:cache:indicators:AAPL" in redis.keys()

    await main.refresh_stock("aapl")

    assert "tf:cache:indicators:AAPL" not in redis.keys()
    assert "tf:cache:refresh:AAPL" in redis.keys()  # cooldown still set
    after = await main.get_indicators("AAPL")
    assert after.cached is False


# ── The contract ai-agent's verdict projection reads (spec verdict-units) ──

# The other side is ai-agent's test_indicator_keys_pinned_to_data_engine,
# which holds this list too and maps each key to a projected name with its
# unit. Change both or neither: a field added here reaches the model only
# once ai-agent names it with a unit.
AI_AGENT_INDICATOR_FIELDS = [
    "ticker", "asOf", "bars", "close", "sector", "ema20", "ema50", "ema200", "atr14", "rvol",
    "rsi14", "macd", "macdSignal", "macdHist", "pos52w", "ext20", "ext50", "rsSpy5", "rsSpy20",
    "rsSector5", "rsSector20", "avgDollarVolume20", "gapPct", "gaps20", "zones", "lastSwingLow",
    # 4.8b-de: today so far (spec 4.8b decision 16); ai-agent projects it in 4.8b-ai.
    "sessionSoFar",
    # 4.8b-de: the four read blocks (spec 4.8b decisions 2-4).
    "volumeRead", "trendRead", "momentumRead", "rangeRead",
    "benchmarks", "computedAt", "cached",
]


def test_indicator_fields_pinned_for_ai_agent():
    import inspect

    from dossier import assemble
    from dossier.models import ProfileSection
    from providers.context import finnhub

    aliases = [f.alias or name for name, f in IndicatorsResponse.model_fields.items()]
    assert aliases == AI_AGENT_INDICATOR_FIELDS
    # 4.8b-de: sessionSoFar's own keys, each with its unit (Zubair's rename of
    # 2026-09-25: volumeSoFarShares, scaledRvol). ai-agent projects them in 4.8b-ai.
    from indicators.models import SessionSoFarOut
    inner = [f.alias or name for name, f in SessionSoFarOut.model_fields.items()]
    assert inner == ["open", "high", "low", "last", "volumeSoFarShares", "sessionElapsedFrac",
                     "changeVsPriorClosePct", "scaledRvol", "inProgress"]

    # profile.marketCap: ai-agent projects it as `marketCapUsdM`, millions of
    # USD, which is only true while it comes from Finnhub profile2 unchanged.
    field = ProfileSection.model_fields["market_cap"]
    assert field.alias == "marketCap" and "millions of USD" in (field.description or "")
    assert 'raw.get("marketCapitalization")' in inspect.getsource(assemble.build_profile)
    assert "/stock/profile2" in inspect.getsource(finnhub.profile)
