"""
TradingFirm — Dossier assembly + endpoint tests (Part 2.4).
Spec and failure-branch table: docs/specs/2.4.md.

Zero network, enforced rather than promised: the autouse `_no_network`
fixture wraps every test in `respx.mock(assert_all_mocked=True)`, so a call
to a host no test mounted raises instead of leaving the machine. Bodies come
from the recorded fixtures of 2.1/2.2 plus small hand-built ones for the
boundary logic; Redis is the in-process FakeRedis; the DB pool is a fake
that answers by SQL shape.
"""

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx

import cache as cache_mod
from cache import MemoryCooldowns
from dossier import HORIZON_PROFILES, HORIZON_SWING, MAX_FILINGS, MAX_HEADLINES
from dossier.assemble import (
    SOURCE_EDGAR,
    SOURCE_FINNHUB,
    DossierContext,
    assemble,
    has_error_section,
    is_stale,
    reference_session,
    safe_detail,
    weekdays_between,
)
from dossier.models import (
    STATUS_ERROR,
    STATUS_OK,
    STATUS_STALE,
    STATUS_TRUNCATED,
    STATUS_UNCONFIGURED,
)
from dossier.sections import NoBarsStored
from providers.context.edgar_client import EdgarClient, EdgarError
from providers.context.finnhub_client import FinnhubClient
from providers.context.ratelimit import RateLimiter
from tests.fake_redis import FakeRedis

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FINNHUB = "https://finnhub.io/api/v1"
EDGAR_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_RE = r"https://data\.sec\.gov/submissions/CIK\d{10}\.json"

TICKER = "AAPL"
UA = "TradingFirm test@example.com"
# A Wednesday 18:00 ET (22:00 UTC): the market has closed, so the reference
# session is that same day.
NOW = datetime(2026, 9, 9, 22, 0, tzinfo=timezone.utc)
TODAY = date(2026, 9, 9)


@pytest.fixture(autouse=True)
def _no_network():
    """Every test in this file runs inside respx with assert_all_mocked, so
    an unmocked host raises instead of reaching the internet (G6/G7).
    `assert_all_called=False`: a test that mounts every source but exercises
    a cooldown deliberately leaves some routes unused."""
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        yield router


def _fixture(*parts):
    return json.loads((FIXTURES.joinpath(*parts)).read_text())


# ── Fakes ────────────────────────────────────────────────────────────────

def _bars(n: int, last: date, *, ticker: str = TICKER) -> list[dict]:
    """`n` daily bars ending on `last`, one per weekday, prices walking up."""
    rows, day = [], last
    while len(rows) < n:
        if day.weekday() < 5:
            i = len(rows)
            rows.append({
                "ts": datetime(day.year, day.month, day.day, tzinfo=timezone.utc),
                "open": 100.0 + i, "high": 101.0 + i, "low": 99.0 + i,
                "close": 100.5 + i, "volume": 1_000_000 + i,
            })
        day -= timedelta(days=1)
    return list(reversed(rows))


class FakePool:
    """Answers by SQL shape: bars, events, stocks. Records every write."""

    def __init__(self, *, bars=None, events=None, stock=None, raise_on=None):
        self.bars = bars if bars is not None else {}
        self.events = events or []
        self.stock = stock
        self.raise_on = raise_on          # substring of the SQL that blows up
        self.writes: list[tuple[str, int]] = []
        self.reads: list[str] = []

    def acquire(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(side_effect=self._fetch)
        conn.fetchrow = AsyncMock(side_effect=self._fetchrow)
        conn.executemany = AsyncMock(side_effect=self._executemany)
        conn.execute = AsyncMock(return_value="")
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=conn)
        cm.__aexit__ = AsyncMock(return_value=None)
        return cm

    def _guard(self, query: str):
        self.reads.append(query)
        if self.raise_on and self.raise_on in query:
            import asyncpg
            raise asyncpg.PostgresError(f"fake db failure on {self.raise_on}")

    async def _fetch(self, query, *args):
        self._guard(query)
        if "ohlcv_bars" in query:
            ticker, interval = args[0], args[1]
            rows = self.bars.get((ticker, interval), [])
            if len(args) > 2 and args[2] is not None:
                rows = [r for r in rows if r["ts"] >= args[2]]
            return rows
        if "data_engine.events" in query:
            rows = self.events
            if len(args) > 1:
                rows = [r for r in rows if r["event_type"] == args[1]] if isinstance(args[1], str) else rows
            return [
                {"ticker": r["ticker"], "event_type": r["event_type"],
                 "event_at": r["event_at"], "meta": json.dumps(r["meta"])}
                for r in rows
            ]
        return []

    async def _fetchrow(self, query, *args):
        self._guard(query)
        if "data_engine.stocks" in query:
            return self.stock
        return None

    async def _executemany(self, query, rows):
        self._guard(query)
        table = "news_items" if "news_items" in query else (
            "events" if "events" in query else "other")
        self.writes.append((table, len(rows)))


def _finnhub(key="test-key", **kw) -> FinnhubClient:
    limiter = kw.pop("limiter", RateLimiter(max_calls=100, window=1.0, min_gap=0.0))
    return FinnhubClient(key, limiter=limiter, **kw)


def _edgar(ua=UA, **kw) -> EdgarClient:
    limiter = kw.pop("limiter", RateLimiter(max_calls=100, window=1.0, min_gap=0.0))
    return EdgarClient(ua, limiter=limiter, **kw)


def _ctx(**kw) -> DossierContext:
    kw.setdefault("pool", FakePool(bars={(TICKER, "1d"): _bars(60, TODAY)}))
    kw.setdefault("redis", FakeRedis())
    kw.setdefault("cooldowns", MemoryCooldowns())
    kw.setdefault("finnhub", _finnhub())
    kw.setdefault("edgar", _edgar())
    kw.setdefault("now", NOW)
    return DossierContext(**kw)


def _mount_finnhub(router, *, news=None, calendar=None, surprises=None,
                   recommendations=None, profile=None):
    news = news if news is not None else _fixture("finnhub", "AAPL_news.json")
    calendar = calendar if calendar is not None else _fixture("finnhub", "AAPL_earnings_calendar.json")
    surprises = surprises if surprises is not None else _fixture("finnhub", "AAPL_earnings_surprises.json")
    recs = recommendations if recommendations is not None else _fixture("finnhub", "AAPL_recommendations.json")
    prof = profile if profile is not None else _fixture("finnhub", "AAPL_profile.json")
    router.get(f"{FINNHUB}/company-news").mock(return_value=httpx.Response(200, json=news))
    router.get(f"{FINNHUB}/calendar/earnings").mock(return_value=httpx.Response(200, json=calendar))
    router.get(f"{FINNHUB}/stock/earnings").mock(return_value=httpx.Response(200, json=surprises))
    router.get(f"{FINNHUB}/stock/recommendation").mock(return_value=httpx.Response(200, json=recs))
    router.get(f"{FINNHUB}/stock/profile2").mock(return_value=httpx.Response(200, json=prof))


def _mount_edgar(router, *, submissions=None):
    body = submissions if submissions is not None else _fixture("edgar", "AAPL_submissions.json")
    router.get(EDGAR_TICKERS).mock(
        return_value=httpx.Response(200, json=_fixture("edgar", "company_tickers.json")))
    router.get(url__regex=SUBMISSIONS_RE).mock(return_value=httpx.Response(200, json=body))


def _mount_all(router, **kw):
    _mount_finnhub(router, **{k: v for k, v in kw.items() if k != "submissions"})
    _mount_edgar(router, submissions=kw.get("submissions"))


# ── Staleness (pure) ─────────────────────────────────────────────────────

def test_weekdays_between_skips_weekends():
    assert weekdays_between(date(2026, 9, 9), date(2026, 9, 9)) == 0
    assert weekdays_between(date(2026, 9, 8), date(2026, 9, 9)) == 1
    # Friday → Monday is one weekday, not three days.
    assert weekdays_between(date(2026, 9, 4), date(2026, 9, 7)) == 1
    assert weekdays_between(date(2026, 8, 10), date(2026, 9, 9)) == 22


def test_reference_session_before_and_after_the_close():
    # Wednesday 18:00 ET → today; 10:00 ET → Tuesday (today has not closed).
    assert reference_session(NOW) == date(2026, 9, 9)
    assert reference_session(datetime(2026, 9, 9, 14, 0, tzinfo=timezone.utc)) == date(2026, 9, 8)
    # Sunday → Friday.
    assert reference_session(datetime(2026, 9, 13, 22, 0, tzinfo=timezone.utc)) == date(2026, 9, 11)


def test_is_stale_is_more_than_one_weekday_behind():
    assert is_stale(date(2026, 9, 9), NOW) == (False, 0)
    assert is_stale(date(2026, 9, 8), NOW) == (False, 1)   # one day behind is normal
    assert is_stale(date(2026, 9, 7), NOW) == (True, 2)
    assert is_stale(None, NOW) == (True, 0)


def test_safe_detail_drops_urls_and_keys():
    detail = safe_detail(EdgarError("https://data.sec.gov/submissions/CIK0000320193.json: 403 blocked"))
    assert "://" not in detail and "sec.gov" not in detail
    assert detail.startswith("EdgarError")
    assert "403" in detail


# ── Assembly ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_all_sources_ok(_no_network):
    _mount_all(_no_network)
    ctx = _ctx()
    d = await assemble(ctx, TICKER, HORIZON_SWING)

    assert d.ticker == TICKER and d.horizon == HORIZON_SWING
    assert d.sections.bars.status == STATUS_OK
    assert d.sections.bars.last_bar_date == TODAY
    for name in ("indicators", "news", "events", "recommendations", "filings", "earnings", "profile"):
        section = getattr(d.sections, name)
        assert section.status in (STATUS_OK, STATUS_TRUNCATED), (name, section.status, section.detail)
    assert d.sections.news.items and d.sections.news.items[0].headline
    assert d.sections.profile.name == "Apple Inc"
    assert d.sections.recommendations.items
    assert d.sections.indicators.close is not None
    # News and events were stored on the way through.
    assert ("news_items", 20) in ctx.pool.writes
    assert any(table == "events" for table, _n in ctx.pool.writes)


@pytest.mark.asyncio
async def test_one_source_error_degrades(_no_network):
    _mount_all(_no_network)
    _no_network.get(f"{FINNHUB}/stock/profile2").mock(return_value=httpx.Response(500, text="boom"))
    d = await assemble(_ctx(), TICKER, HORIZON_SWING)

    assert d.sections.profile.status == STATUS_ERROR
    assert d.sections.profile.reason == "upstream"
    assert d.sections.profile.name is None          # payload key present, empty
    assert d.sections.news.status == STATUS_OK      # the rest is untouched
    assert d.sections.filings.status in (STATUS_OK, STATUS_TRUNCATED)


@pytest.mark.asyncio
async def test_edgar_error_degrades(_no_network):
    _mount_finnhub(_no_network)
    _no_network.get(EDGAR_TICKERS).mock(
        return_value=httpx.Response(200, json=_fixture("edgar", "company_tickers.json")))
    _no_network.get(url__regex=SUBMISSIONS_RE).mock(return_value=httpx.Response(500, text="oops"))
    d = await assemble(_ctx(), TICKER, HORIZON_SWING)

    assert d.sections.filings.status == STATUS_ERROR
    assert d.sections.filings.reason == "upstream"
    assert d.sections.filings.rows == []
    assert "://" not in (d.sections.filings.detail or "")
    assert d.sections.news.status == STATUS_OK


@pytest.mark.asyncio
async def test_partial_context_news_still_served(_no_network):
    """The calendar call fails; news is still fetched *and stored*, and the
    events section still reads what the store holds."""
    _mount_all(_no_network)
    _no_network.get(f"{FINNHUB}/calendar/earnings").mock(return_value=httpx.Response(500))
    pool = FakePool(
        bars={(TICKER, "1d"): _bars(60, TODAY)},
        events=[{"ticker": TICKER, "event_type": "earnings",
                 "event_at": datetime(2026, 7, 30, tzinfo=timezone.utc),
                 "meta": {"earnings": {"validated": True, "source": "yfinance"}}}],
    )
    d = await assemble(_ctx(pool=pool), TICKER, HORIZON_SWING)

    assert d.sections.news.status == STATUS_OK
    assert ("news_items", 20) in pool.writes
    assert d.sections.events.status == STATUS_OK
    assert len(d.sections.events.items) == 1
    assert d.sections.events.items[0].type == "earnings"


@pytest.mark.asyncio
async def test_events_error_when_store_cannot_answer(_no_network):
    """The forgiveness rule has a floor: the calendar call fails *and* the
    store read comes back empty, so the section reports the source instead of
    claiming an empty `ok`. Compare test_partial_context_news_still_served,
    where the store has a row and the section stays `ok`."""
    _mount_all(_no_network)
    _no_network.get(f"{FINNHUB}/calendar/earnings").mock(return_value=httpx.Response(500))
    pool = FakePool(bars={(TICKER, "1d"): _bars(60, TODAY)}, events=[])
    d = await assemble(_ctx(pool=pool), TICKER, HORIZON_SWING)

    assert d.sections.events.status == STATUS_ERROR
    assert d.sections.events.reason == "upstream"
    assert d.sections.events.items == []
    assert d.sections.news.status == STATUS_OK      # still fetched and stored


@pytest.mark.asyncio
async def test_section_error_shape_has_empty_payload(_no_network):
    """Every error section still carries its own payload key, empty."""
    _no_network.get(f"{FINNHUB}/company-news").mock(return_value=httpx.Response(500))
    _no_network.get(f"{FINNHUB}/calendar/earnings").mock(return_value=httpx.Response(500))
    _no_network.get(f"{FINNHUB}/stock/earnings").mock(return_value=httpx.Response(500))
    _no_network.get(f"{FINNHUB}/stock/recommendation").mock(return_value=httpx.Response(500))
    _no_network.get(f"{FINNHUB}/stock/profile2").mock(return_value=httpx.Response(500))
    _no_network.get(EDGAR_TICKERS).mock(return_value=httpx.Response(500))
    d = await assemble(_ctx(pool=FakePool(bars={(TICKER, "1d"): _bars(60, TODAY)})),
                       TICKER, HORIZON_SWING)
    body = d.model_dump(mode="json", by_alias=True)

    assert body["sections"]["news"]["items"] == []
    assert body["sections"]["news"]["count"] == 0
    assert body["sections"]["events"]["items"] == []
    assert body["sections"]["recommendations"]["items"] == []
    assert body["sections"]["filings"]["rows"] == []
    for name in ("news", "events", "recommendations", "filings", "profile"):
        assert body["sections"][name]["status"] == STATUS_ERROR
        assert body["sections"][name]["reason"]
    assert has_error_section(body) is True


@pytest.mark.asyncio
async def test_section_reraises_db_errors(_no_network):
    """A DB failure inside a section is not a degraded section: it comes out
    of assemble() for the endpoint to answer 503."""
    import asyncpg
    _mount_all(_no_network)
    pool = FakePool(bars={(TICKER, "1d"): _bars(60, TODAY)}, raise_on="data_engine.events")
    with pytest.raises(asyncpg.PostgresError):
        await assemble(_ctx(pool=pool), TICKER, HORIZON_SWING)


@pytest.mark.asyncio
async def test_caps_applied_and_flagged(_no_network):
    """31 headlines and 11 filings are cut to 30 and 10, newest first, and
    both sections say `truncated`."""
    news = []
    for i in range(31):
        news.append({
            "datetime": int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp()) + i * 3600,
            "headline": f"headline {i}", "url": f"https://example.com/{i}",
            "source": "Test", "summary": "s",
        })
    rows = [(f"0000320193-26-{i:06d}", (TODAY - timedelta(days=i)).isoformat(), "8-K",
             "doc.htm", "2.02", f"2026-09-0{(i % 9) + 1}T12:00:00.000Z") for i in range(11)]
    cols = {
        "accessionNumber": [r[0] for r in rows],
        "filingDate": [r[1] for r in rows],
        "reportDate": [r[1] for r in rows],
        "acceptanceDateTime": [r[5] for r in rows],
        "form": [r[2] for r in rows],
        "primaryDocument": [r[3] for r in rows],
        "primaryDocDescription": ["" for _ in rows],
        "items": [r[4] for r in rows],
    }
    submissions = {"cik": "320193", "filings": {"recent": cols, "files": []}}

    _mount_all(_no_network, news=news, submissions=submissions)
    d = await assemble(_ctx(), TICKER, HORIZON_SWING)

    assert d.sections.news.status == STATUS_TRUNCATED
    assert d.sections.news.truncated is True
    assert d.sections.news.count == MAX_HEADLINES == len(d.sections.news.items)
    assert d.sections.news.items[0].headline == "headline 30"      # newest first
    assert d.sections.filings.status == STATUS_TRUNCATED
    assert d.sections.filings.truncated is True
    assert len(d.sections.filings.rows) == MAX_FILINGS
    assert d.sections.filings.rows[0].filed_on == TODAY            # newest first


@pytest.mark.asyncio
async def test_filings_block_flag_flags_truncated(_no_network):
    """Under the 10-cap, but EDGAR's `recent` block did not reach back 30
    days: one flag, same meaning — the list is not the complete window."""
    rows = [(f"0000320193-26-{i:06d}", (TODAY - timedelta(days=i)).isoformat(), "8-K",
             "doc.htm", "2.02", "2026-09-09T12:00:00.000Z") for i in range(3)]
    cols = {
        "accessionNumber": [r[0] for r in rows],
        "filingDate": [r[1] for r in rows],
        "reportDate": [r[1] for r in rows],
        "acceptanceDateTime": [r[5] for r in rows],
        "form": [r[2] for r in rows],
        "primaryDocument": [r[3] for r in rows],
        "primaryDocDescription": ["" for _ in rows],
        "items": [r[4] for r in rows],
    }
    _mount_all(_no_network, submissions={"cik": "320193", "filings": {"recent": cols, "files": []}})
    d = await assemble(_ctx(), TICKER, HORIZON_SWING)

    assert len(d.sections.filings.rows) == 3 < MAX_FILINGS
    assert d.sections.filings.truncated is True
    assert d.sections.filings.status == STATUS_TRUNCATED


@pytest.mark.asyncio
async def test_horizon_profile_drives_windows(_no_network):
    """The windows in the request come from HORIZON_PROFILES, not literals."""
    _mount_all(_no_network)
    await assemble(_ctx(), TICKER, HORIZON_SWING)

    profile = HORIZON_PROFILES[HORIZON_SWING]
    news_call = [c for c in _no_network.calls if "company-news" in str(c.request.url)][0]
    params = dict(httpx.URL(str(news_call.request.url)).params)
    assert (date.fromisoformat(params["to"]) - date.fromisoformat(params["from"])).days == profile["news_days"]

    with pytest.raises(ValueError):
        await assemble(_ctx(), TICKER, "intraday")


@pytest.mark.asyncio
async def test_assemble_passes_ctx_today_to_fetchers(_no_network):
    """An injected ctx.now reaches the news, earnings-calendar and filings
    windows (spec dossier-clock): without `today=` they read the real clock."""
    from providers.context.finnhub import CALENDAR_LOOKAHEAD_DAYS, CALENDAR_LOOKBACK_DAYS

    later = datetime(2027, 3, 17, 22, 0, tzinfo=timezone.utc)      # Wednesday, after the close
    day = later.date()
    filed = (day - timedelta(days=5)).isoformat()
    cols = {
        "accessionNumber": ["0000320193-27-000001"], "filingDate": [filed], "reportDate": [filed],
        "acceptanceDateTime": [f"{filed}T12:00:00.000Z"], "form": ["8-K"],
        "primaryDocument": ["doc.htm"], "primaryDocDescription": [""], "items": ["2.02"],
    }
    _mount_all(_no_network, submissions={"cik": "320193", "filings": {"recent": cols, "files": []}})
    pool = FakePool(bars={(TICKER, "1d"): _bars(60, day)})
    d = await assemble(_ctx(now=later, pool=pool), TICKER, HORIZON_SWING)

    def params(path):
        call = [c for c in _no_network.calls if path in str(c.request.url)][0]
        return dict(httpx.URL(str(call.request.url)).params)

    assert params("company-news")["to"] == day.isoformat()
    cal = params("calendar/earnings")
    assert cal["from"] == (day - timedelta(days=CALENDAR_LOOKBACK_DAYS)).isoformat()
    assert cal["to"] == (day + timedelta(days=CALENDAR_LOOKAHEAD_DAYS)).isoformat()
    assert [r.filed_on for r in d.sections.filings.rows] == [date.fromisoformat(filed)]


@pytest.mark.asyncio
async def test_earnings_null_reactions_passthrough(_no_network):
    """2.3's `reactions: null` (no confirmed report) is passed through as-is,
    with the section still `ok`."""
    _mount_all(_no_network)
    d = await assemble(_ctx(), TICKER, HORIZON_SWING)

    assert d.sections.earnings.status == STATUS_OK
    assert d.sections.earnings.reactions is None
    assert d.sections.earnings.data_quality.source is None
    assert d.sections.earnings.data_quality.dropped == 0


@pytest.mark.asyncio
async def test_nan_serializes_as_null(_no_network):
    """A NaN anywhere in the document serializes as JSON null, never NaN."""
    from dossier.models import DossierResponse, NewsSection, ProfileSection

    section = ProfileSection(status=STATUS_OK, name="X", market_cap=float("nan"))
    assert section.market_cap is None

    _mount_all(_no_network)
    _no_network.get(f"{FINNHUB}/stock/profile2").mock(return_value=httpx.Response(
        200,
        content=b'{"name":"Apple Inc","marketCapitalization":NaN,"shareOutstanding":Infinity}',
        headers={"content-type": "application/json"},
    ))
    d = await assemble(_ctx(), TICKER, HORIZON_SWING)
    body = json.dumps(d.model_dump(mode="json", by_alias=True), allow_nan=False)
    assert "NaN" not in body and "Infinity" not in body
    assert d.sections.profile.market_cap is None
    assert d.sections.profile.shares_outstanding is None


@pytest.mark.asyncio
async def test_budget_counts_upstream_calls(_no_network):
    """Spec decision 13's table is the oracle: 7 cold, 2 warm, 0 cached.

    Every route answers slowly enough that the four Finnhub sections are in
    flight at the same time, sharing one client: the count must be of calls
    actually made, not of per-section deltas on a shared counter (which the
    2.5 live check caught reporting 14 for 10 calls).
    """
    async def _slow(request):
        await asyncio.sleep(0.05)
        return None      # filled in per route below

    _mount_all(_no_network)
    for path, body in (
        ("/company-news", _fixture("finnhub", "AAPL_news.json")),
        ("/calendar/earnings", _fixture("finnhub", "AAPL_earnings_calendar.json")),
        ("/stock/earnings", _fixture("finnhub", "AAPL_earnings_surprises.json")),
        ("/stock/recommendation", _fixture("finnhub", "AAPL_recommendations.json")),
        ("/stock/profile2", _fixture("finnhub", "AAPL_profile.json")),
    ):
        async def _delayed(request, _body=body):
            await asyncio.sleep(0.05)
            return httpx.Response(200, json=_body)
        _no_network.get(f"{FINNHUB}{path}").mock(side_effect=_delayed)

    redis = FakeRedis()
    ctx = _ctx(redis=redis)
    d = await assemble(ctx, TICKER, HORIZON_SWING)

    assert len(_no_network.calls) == 7
    assert d.budget.upstream_calls == 7          # not 14: concurrent sections
    assert d.budget.by_source == {SOURCE_EDGAR: 2, SOURCE_FINNHUB: 5}
    assert d.budget.elapsed_ms >= 0

    # Warm: the 24 h keys (calendar, surprises, recommendations, profile, CIK
    # map) survive; only the 15 min keys (news, submissions) are refetched.
    for key in list(redis.keys()):
        if "news" in key or "filings" in key:
            await redis.delete(key)
    before = len(_no_network.calls)
    ctx2 = _ctx(redis=redis)
    d2 = await assemble(ctx2, TICKER, HORIZON_SWING)
    assert len(_no_network.calls) - before == 2
    assert d2.budget.upstream_calls == 2
    assert d2.budget.by_source == {SOURCE_EDGAR: 1, SOURCE_FINNHUB: 1}

    # Fully warm: nothing upstream at all.
    before = len(_no_network.calls)
    d3 = await assemble(_ctx(redis=redis), TICKER, HORIZON_SWING)
    assert len(_no_network.calls) - before == 0
    assert d3.budget.upstream_calls == 0
    assert d3.budget.by_source == {}


@pytest.mark.asyncio
async def test_budget_counts_refresh_provider_calls(_no_network):
    """The refresh helper is the only caller that knows what it spent on
    yfinance and Alpha Vantage, so it returns (body, {source: calls}) and the
    assembly folds them in. This is spec decision 13's stale-cold row: 11."""
    _mount_all(_no_network)
    pool = FakePool(bars={(TICKER, "1d"): _bars(60, date(2026, 9, 4))})

    async def _refresh(ticker):
        pool.bars[(TICKER, "1d")] = _bars(60, TODAY)
        return {"dailyBars": 60}, {"yfinance": 3, "alphavantage": 1}

    d = await assemble(_ctx(pool=pool, refresh=_refresh), TICKER, HORIZON_SWING)

    assert d.budget.by_source == {
        SOURCE_EDGAR: 2, SOURCE_FINNHUB: 5, "alphavantage": 1, "yfinance": 3}
    assert d.budget.upstream_calls == 11
    assert len(_no_network.calls) == 7          # the other 4 are not HTTP here


# ── Cooldowns and budgets ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_finnhub_429_sets_cooldown(_no_network):
    _mount_all(_no_network)
    _no_network.get(f"{FINNHUB}/company-news").mock(return_value=httpx.Response(429))
    redis = FakeRedis()
    ctx = _ctx(redis=redis)
    d = await assemble(ctx, TICKER, HORIZON_SWING)

    assert d.sections.news.status == STATUS_ERROR
    assert d.sections.news.reason == "rate_limited"
    assert await cache_mod.cooldown_remaining(redis, ctx.cooldowns, SOURCE_FINNHUB, 60) is not None


@pytest.mark.asyncio
async def test_edgar_403_sets_cooldown(_no_network):
    _mount_finnhub(_no_network)
    _no_network.get(EDGAR_TICKERS).mock(return_value=httpx.Response(403, text="blocked"))
    redis = FakeRedis()
    ctx = _ctx(redis=redis)
    d = await assemble(ctx, TICKER, HORIZON_SWING)

    assert d.sections.filings.status == STATUS_ERROR
    assert d.sections.filings.reason == "blocked"
    left = await cache_mod.cooldown_remaining(redis, ctx.cooldowns, SOURCE_EDGAR, 900)
    assert left is not None and left > 60          # the long window, not Finnhub's
    # One call, no retry.
    assert len([c for c in _no_network.calls if "company_tickers" in str(c.request.url)]) == 1


@pytest.mark.asyncio
async def test_cooldown_skips_source(_no_network):
    """A set cooldown skips the source before any HTTP."""
    _mount_finnhub(_no_network)
    redis = FakeRedis()
    ctx = _ctx(redis=redis)
    await cache_mod.start_cooldown(redis, ctx.cooldowns, SOURCE_EDGAR, 900)
    d = await assemble(ctx, TICKER, HORIZON_SWING)

    assert d.sections.filings.status == STATUS_ERROR
    assert d.sections.filings.reason == "cooldown"
    assert not [c for c in _no_network.calls if "sec.gov" in str(c.request.url)]
    assert d.budget.by_source.get(SOURCE_EDGAR) is None


@pytest.mark.asyncio
async def test_cooldown_falls_back_to_memory(_no_network):
    """Redis down: the refusal is still remembered for the next dossier."""
    _mount_finnhub(_no_network)
    _no_network.get(EDGAR_TICKERS).mock(return_value=httpx.Response(403))
    cooldowns = MemoryCooldowns()
    first = await assemble(_ctx(redis=None, cooldowns=cooldowns), TICKER, HORIZON_SWING)
    assert first.sections.filings.reason == "blocked"

    sec_calls = len([c for c in _no_network.calls if "sec.gov" in str(c.request.url)])
    second = await assemble(_ctx(redis=None, cooldowns=cooldowns), TICKER, HORIZON_SWING)
    assert second.sections.filings.reason == "cooldown"
    assert len([c for c in _no_network.calls if "sec.gov" in str(c.request.url)]) == sec_calls


@pytest.mark.asyncio
async def test_source_timeout_is_error_section(_no_network):
    """A source over its own bound is an error section; the rest still ok."""
    _mount_all(_no_network)

    async def _slow(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json=[])

    _no_network.get(f"{FINNHUB}/stock/recommendation").mock(side_effect=_slow)
    import dossier.assemble as asm
    original = asm.SECTION_TIMEOUT
    asm.SECTION_TIMEOUT = 0.05
    try:
        d = await assemble(_ctx(), TICKER, HORIZON_SWING)
    finally:
        asm.SECTION_TIMEOUT = original

    assert d.sections.recommendations.status == STATUS_ERROR
    assert d.sections.recommendations.reason == "timeout"
    assert d.sections.recommendations.items == []
    assert d.sections.news.status == STATUS_OK


@pytest.mark.asyncio
async def test_total_budget_caps_fanout(_no_network):
    """The outer guard is unreachable with the inner bound in place, so the
    test patches it below the per-section timeout to exercise it."""
    _mount_all(_no_network)

    async def _slow(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json=[])

    _no_network.get(f"{FINNHUB}/stock/recommendation").mock(side_effect=_slow)
    d = await assemble(_ctx(), TICKER, HORIZON_SWING, fanout_budget=0.05)

    assert d.sections.recommendations.status == STATUS_ERROR
    assert d.sections.recommendations.reason == "timeout"


# ── Bars and staleness through the assembly ──────────────────────────────

@pytest.mark.asyncio
async def test_fresh_bars_no_refresh(_no_network):
    _mount_all(_no_network)
    calls = []

    async def _refresh(ticker):
        calls.append(ticker)
        return {"dailyBars": 1}

    d = await assemble(_ctx(refresh=_refresh), TICKER, HORIZON_SWING)
    assert calls == []
    assert d.sections.bars.status == STATUS_OK
    assert d.sections.bars.refreshed is False


@pytest.mark.asyncio
async def test_stale_bars_triggers_refresh(_no_network):
    _mount_all(_no_network)
    pool = FakePool(bars={(TICKER, "1d"): _bars(60, date(2026, 9, 4))})   # 3 weekdays behind
    calls = []

    async def _refresh(ticker):
        calls.append(ticker)
        pool.bars[(TICKER, "1d")] = _bars(60, TODAY)
        return {"dailyBars": 60}

    d = await assemble(_ctx(pool=pool, refresh=_refresh), TICKER, HORIZON_SWING)
    assert calls == [TICKER]
    assert d.sections.bars.status == STATUS_OK
    assert d.sections.bars.refreshed is True
    assert d.sections.bars.last_bar_date == TODAY


@pytest.mark.asyncio
async def test_refresh_fails_served_stale(_no_network):
    _mount_all(_no_network)
    pool = FakePool(bars={(TICKER, "1d"): _bars(60, date(2026, 9, 4))})

    async def _refresh(ticker):
        raise RuntimeError("provider is down")

    d = await assemble(_ctx(pool=pool, refresh=_refresh), TICKER, HORIZON_SWING)
    assert d.sections.bars.status == STATUS_STALE
    assert d.sections.bars.refreshed is False
    assert d.sections.bars.stale_weekdays == 3
    assert d.sections.indicators.status == STATUS_OK      # computed from stored bars


@pytest.mark.asyncio
async def test_refresh_succeeds_no_newer_bar_stays_stale(_no_network):
    """We asked, the source had nothing newer: stale *and* refreshed."""
    _mount_all(_no_network)
    pool = FakePool(bars={(TICKER, "1d"): _bars(60, date(2026, 9, 4))})

    async def _refresh(ticker):
        return {"dailyBars": 0}          # nothing newer came back

    d = await assemble(_ctx(pool=pool, refresh=_refresh), TICKER, HORIZON_SWING)
    assert d.sections.bars.status == STATUS_STALE
    assert d.sections.bars.refreshed is True
    assert d.sections.bars.stale_weekdays == 3


@pytest.mark.asyncio
async def test_stale_bars_on_cooldown_not_refreshed(_no_network):
    """The refresh helper refuses (429 cooldown): serve what is stored."""
    from fastapi import HTTPException
    _mount_all(_no_network)
    pool = FakePool(bars={(TICKER, "1d"): _bars(60, date(2026, 9, 4))})

    async def _refresh(ticker):
        raise HTTPException(status_code=429, detail="refreshed recently")

    d = await assemble(_ctx(pool=pool, refresh=_refresh), TICKER, HORIZON_SWING)
    assert d.sections.bars.status == STATUS_STALE
    assert d.sections.bars.refreshed is False


@pytest.mark.asyncio
async def test_no_bars_raises_no_bars_stored(_no_network):
    """Nothing stored and no refresh to save it: the endpoint's 404."""
    _mount_all(_no_network)
    with pytest.raises(NoBarsStored):
        await assemble(_ctx(pool=FakePool(bars={})), TICKER, HORIZON_SWING)


@pytest.mark.asyncio
async def test_no_bars_refresh_recovers(_no_network):
    _mount_all(_no_network)
    pool = FakePool(bars={})

    async def _refresh(ticker):
        pool.bars[(TICKER, "1d")] = _bars(60, TODAY)
        return {"dailyBars": 60}

    d = await assemble(_ctx(pool=pool, refresh=_refresh), TICKER, HORIZON_SWING)
    assert d.sections.bars.status == STATUS_OK
    assert d.sections.bars.refreshed is True


@pytest.mark.asyncio
async def test_bad_ticker_rejected_before_any_call(_no_network):
    for bad in ("BRK.B", "TOOLONG", "12", ""):
        with pytest.raises(ValueError):
            await assemble(_ctx(), bad, HORIZON_SWING)
    assert not _no_network.calls


@pytest.mark.asyncio
async def test_unconfigured_sections(_no_network):
    """Empty key / empty User-Agent is `unconfigured`, not `error`, and costs
    no HTTP call — the dev twin's normal state."""
    ctx = _ctx(finnhub=_finnhub(""), edgar=_edgar(""))
    d = await assemble(ctx, TICKER, HORIZON_SWING)

    for name in ("news", "events", "recommendations", "profile", "filings"):
        section = getattr(d.sections, name)
        assert section.status == STATUS_UNCONFIGURED, (name, section.status)
        assert section.reason is None and section.detail is None
    assert not _no_network.calls
    assert d.sections.indicators.status == STATUS_OK    # stored bars still work
    assert d.sections.earnings.status == STATUS_OK


# ── The endpoint (through TestClient: real serialization) ────────────────

import main  # noqa: E402  (imported here so the assembly tests above stay
             #              independent of the FastAPI app)
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def app_state(monkeypatch):
    """Point the app at fakes and hand back the pieces a test needs to poke.
    `TestClient` is created without the lifespan (it would open real Redis
    and Postgres connections), so every dependency is set here."""
    from dossier.assemble import DossierContext
    from providers.fixture_provider import FixtureProvider

    # Freeze the endpoint's clock to NOW (the 2026-09-09 session), as 2325d49
    # did for one test. Without it staleWeekdays compares TODAY's bars with the
    # real session: after 2026-09-10 the refresh path runs and its upstream
    # calls land in the budget. Test-only: the code under test is unchanged.
    monkeypatch.setattr(DossierContext, "now_utc", lambda self: NOW)

    pool = FakePool(bars={(TICKER, "1d"): _bars(60, TODAY)})
    redis = FakeRedis()
    main.app.state.provider = FixtureProvider()
    main.app.state.db_pool = pool
    main.app.state.redis = redis
    main.app.state.memory = main.InMemoryStore()
    main.app.state.finnhub = _finnhub()
    main.app.state.edgar = _edgar()
    main.app.state.av_client = None
    yield pool, redis
    main.app.state.db_pool = None
    main.app.state.redis = None


def _get(url: str = f"/dossier/{TICKER}"):
    return TestClient(main.app).get(url)


def test_dossier_camelcase_shape(_no_network, app_state):
    # The clock is frozen to NOW by the app_state fixture.
    _mount_all(_no_network)
    resp = _get()
    assert resp.status_code == 200
    body = resp.json()

    assert set(body) == {"ticker", "horizon", "asOf", "generatedAt", "cached", "sections", "budget"}
    assert body["ticker"] == TICKER and body["horizon"] == HORIZON_SWING
    assert body["cached"] is False
    assert set(body["sections"]) == {
        "bars", "indicators", "news", "events", "recommendations",
        "filings", "earnings", "profile",
    }
    assert set(body["budget"]) == {"upstreamCalls", "elapsedMs", "bySource"}
    assert body["sections"]["bars"]["staleWeekdays"] == 0
    assert body["sections"]["bars"]["lastBarDate"] == TODAY.isoformat()
    assert body["sections"]["earnings"]["dataQuality"] == {
        "source": None, "dropped": 0, "disagreements": 0}
    assert body["sections"]["indicators"]["computedAt"]
    # No snake_case anywhere in the document.
    flat = json.dumps(body)
    for snake in ("last_bar_date", "stale_weekdays", "data_quality", "upstream_calls",
                  "published_at", "filed_on", "market_cap"):
        assert snake not in flat


def test_cache_hit_no_upstream_calls(_no_network, app_state):
    """The second request touches neither an upstream nor the database."""
    _mount_all(_no_network)
    pool, _redis = app_state

    first = _get().json()
    calls_after_first = len(_no_network.calls)
    reads_after_first = len(pool.reads)
    assert first["cached"] is False

    second = _get().json()
    assert second["cached"] is True
    assert len(_no_network.calls) == calls_after_first
    assert len(pool.reads) == reads_after_first

    # `cached` and `budget` both describe the retrieval, not the data: a hit
    # reports no upstream calls, no sources, and only the time it took to
    # read Redis. Everything else is byte-identical to the stored document.
    assert second["budget"]["upstreamCalls"] == 0
    assert second["budget"]["bySource"] == {}
    assert second["budget"]["elapsedMs"] < first["budget"]["elapsedMs"] + 1000
    assert first["budget"]["upstreamCalls"] == 7
    assert {k: v for k, v in second.items() if k not in ("cached", "budget")} == \
           {k: v for k, v in first.items() if k not in ("cached", "budget")}


def test_error_section_short_ttl(_no_network, app_state):
    """A document with a failed section lives 2 min, not 15 or 60."""
    from cache import TTL_DOSSIER_ERROR, dossier_key
    _mount_all(_no_network)
    _no_network.get(f"{FINNHUB}/stock/profile2").mock(return_value=httpx.Response(500))
    _pool, redis = app_state

    body = _get().json()
    assert body["sections"]["profile"]["status"] == STATUS_ERROR
    assert await_ttl(redis, dossier_key(TICKER, HORIZON_SWING)) <= TTL_DOSSIER_ERROR


def test_ttl_market_hours_and_outside(_no_network, app_state, monkeypatch):
    """15 min while the market is open, 60 min outside."""
    from cache import TTL_DOSSIER_CLOSED, TTL_DOSSIER_MARKET, dossier_key
    _mount_all(_no_network)
    _pool, redis = app_state
    key = dossier_key(TICKER, HORIZON_SWING)

    monkeypatch.setattr(main, "get_market_status", lambda: ("market_open", NOW))
    _get()
    open_ttl = await_ttl(redis, key)
    assert TTL_DOSSIER_MARKET - 5 <= open_ttl <= TTL_DOSSIER_MARKET

    redis._store.pop(key, None)          # FakeRedis is a plain dict underneath
    monkeypatch.setattr(main, "get_market_status", lambda: ("weekend", NOW))
    _get()
    assert await_ttl(redis, key) > TTL_DOSSIER_MARKET
    assert await_ttl(redis, key) <= TTL_DOSSIER_CLOSED


def await_ttl(redis: FakeRedis, key: str) -> int:
    """FakeRedis TTL without an event loop of its own (it is pure Python)."""
    import time as _t
    value, expires_at = redis._store[key]
    return int(expires_at - _t.time())


def test_cache_absent(_no_network, app_state):
    """No Redis at all: computed every time, still 200."""
    _mount_all(_no_network)
    main.app.state.redis = None
    first, second = _get().json(), _get().json()
    assert first["cached"] is False and second["cached"] is False


def test_cache_get_raises(_no_network, app_state):
    _mount_all(_no_network)
    main.app.state.redis = FakeRedis(fail_on={"get"})
    body = _get().json()
    assert body["cached"] is False
    assert body["sections"]["news"]["status"] == STATUS_OK


def test_cache_set_raises(_no_network, app_state):
    _mount_all(_no_network)
    main.app.state.redis = FakeRedis(fail_on={"set"})
    resp = _get()
    assert resp.status_code == 200
    assert resp.json()["cached"] is False


def test_cache_corrupt_body_recomputes(_no_network, app_state):
    """A cached value that is not a dossier is a miss: recomputed and
    overwritten, never served."""
    from cache import dossier_key
    _mount_all(_no_network)
    _pool, redis = app_state
    key = dossier_key(TICKER, HORIZON_SWING)
    redis._store[key] = ("not json at all", None)

    body = _get().json()
    assert body["cached"] is False
    assert body["sections"]["bars"]["status"] == STATUS_OK
    assert json.loads(redis._store[key][0])["sections"]["bars"]["status"] == STATUS_OK


def test_bad_horizon_400(_no_network, app_state):
    pool, _redis = app_state
    for horizon in ("intraday", "", "SWING"):
        resp = _get(f"/dossier/{TICKER}?horizon={horizon}")
        assert resp.status_code == 400, horizon
        assert "horizon" in resp.json()["detail"].lower()
    assert not _no_network.calls
    assert pool.reads == []


def test_bad_ticker_400(_no_network, app_state):
    pool, _redis = app_state
    for bad in ("BRK.B", "TOOLONG", "12"):
        assert _get(f"/dossier/{bad}").status_code == 400, bad
    assert not _no_network.calls
    assert pool.reads == []


def test_unknown_ticker_404(_no_network, app_state):
    """No stored bars and the refresh produces none."""
    _mount_all(_no_network)
    main.app.state.db_pool = FakePool(bars={})
    resp = _get("/dossier/ZZZZ")
    assert resp.status_code == 404
    assert "ZZZZ" in resp.json()["detail"]


def test_db_down_503(_no_network, app_state):
    """No pool at all: no document to build, and nothing is fetched."""
    main.app.state.db_pool = None
    resp = _get()
    assert resp.status_code == 503
    assert not _no_network.calls


def test_db_read_raise_503(_no_network, app_state):
    """The pre-fan-out bars read raises: 503, and no upstream call was made."""
    _mount_all(_no_network)
    main.app.state.db_pool = FakePool(bars={(TICKER, "1d"): _bars(60, TODAY)},
                                      raise_on="ohlcv_bars")
    resp = _get()
    assert resp.status_code == 503
    assert not _no_network.calls


def test_earnings_db_raise_503(_no_network, app_state):
    """A DB read inside a section is still a 503, not a degraded section —
    after the fan-out has spent its calls, which is accepted."""
    _mount_all(_no_network)
    main.app.state.db_pool = FakePool(bars={(TICKER, "1d"): _bars(60, TODAY)},
                                      raise_on="data_engine.events")
    resp = _get()
    assert resp.status_code == 503
    assert _no_network.calls        # the calls were already spent


def test_unconfigured_sections_in_dev(_no_network, app_state):
    """The dev twin's state: both keys empty. Every source section is
    `unconfigured`, nothing is `error`, no HTTP happens, and the document is
    still a 200 with real indicators from stored bars."""
    main.app.state.finnhub = _finnhub("")
    main.app.state.edgar = _edgar("")
    resp = _get()
    assert resp.status_code == 200
    body = resp.json()

    for name in ("news", "events", "recommendations", "profile", "filings"):
        assert body["sections"][name]["status"] == STATUS_UNCONFIGURED, name
        assert body["sections"][name]["reason"] is None
    assert body["sections"]["indicators"]["status"] == STATUS_OK
    assert body["sections"]["indicators"]["close"] is not None
    assert body["budget"]["upstreamCalls"] == 0
    assert not _no_network.calls


def test_stale_bars_triggers_refresh_through_the_endpoint(_no_network, app_state):
    """The endpoint injects the real refresh helper: stale bars call it once,
    and a refusal from it is served as stale rather than failing."""
    _mount_all(_no_network)
    pool = FakePool(bars={(TICKER, "1d"): _bars(60, date(2026, 9, 4))})
    main.app.state.db_pool = pool
    calls = []

    async def _refresh(ticker):
        calls.append(ticker)
        raise main.HTTPException(status_code=429, detail="refreshed recently")

    original = main.refresh_ticker_bars
    main.refresh_ticker_bars = _refresh
    try:
        body = _get().json()
    finally:
        main.refresh_ticker_bars = original

    assert calls == [TICKER]
    assert body["sections"]["bars"]["status"] == STATUS_STALE
    assert body["sections"]["bars"]["refreshed"] is False


@pytest.mark.asyncio
async def test_alphavantage_cooldown_skips_av(_no_network):
    """The AV cooldown acts one level down, inside the refresh's earnings
    step: the call is skipped and the refresh reports reason 'cooldown'."""
    from cache import SOURCE_ALPHAVANTAGE, TTL_COOLDOWN_ALPHAVANTAGE, start_cooldown
    from providers.context.earnings import sync_earnings_dates

    redis = FakeRedis()
    cooldowns = MemoryCooldowns()
    await start_cooldown(redis, cooldowns, SOURCE_ALPHAVANTAGE, TTL_COOLDOWN_ALPHAVANTAGE)

    av = MagicMock()
    av.get = AsyncMock(side_effect=AssertionError("Alpha Vantage must not be called"))
    provider = MagicMock()
    provider.get_earnings_dates = AsyncMock(return_value=None)   # primary empty
    pool = FakePool(bars={(TICKER, "1d"): _bars(60, TODAY)})

    result = await sync_earnings_dates(
        provider, av, TICKER, pool, today=TODAY, redis=redis, cooldowns=cooldowns
    )

    assert result == {"source": None, "stored": 0, "dropped": 0, "reason": "cooldown"}
    assert av.get.await_count == 0
