"""Part 3.6a — the macro brief inputs (spec decisions 5–7).

Postgres through db.py's real read helpers over a fake pool, data-engine over
an httpx MockTransport, FRED over a fake client, Redis via FakeRedis. No
socket. Frozen times are ET wall clocks on the XNYS calendar."""

import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import asyncpg
import pytest

import db
import macro_inputs
import scheduler

ET = ZoneInfo("America/New_York")


def et(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=ET).astimezone(timezone.utc)


MONITORS = {
    name: {"score": score, "raw": {"series": [1, 2, 3]}, "detail": f"{name} detail", "stale": False,
           "weight": weight}
    for name, score, weight in (("vix", 60, 25), ("breadth", 60, 20), ("spy_trend", 70, 20),
                                ("sector_rotation", 65, 15), ("volume", 85, 10), ("cross_asset", 80, 10))
}


def _row(at, *, score=68, regime="CAUTIOUS", trend="stable", kind="market", stale=False, monitors=MONITORS):
    return {"checked_at": at, "score": score, "regime": regime, "trend": trend,
            "indicators": json.dumps({"kind": kind, "coverage": 100, "stale": stale,
                                      "staleMonitors": ["vix"] if stale else [], "monitors": monitors,
                                      "inputs": {}, "settleScore": 63, "settleCheckedAt": None})}


class HealthPool:
    """Answers db.py's three health reads by their literal SQL; any write or
    any other statement fails the test."""

    def __init__(self, *, latest=None, scored=None, settle=None, fail=None):
        self.latest, self.scored, self.settle, self.fail = latest, scored, settle, fail
        self.calls = []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if self.fail:
            raise self.fail
        if sql == db.LATEST_HEALTH_CHECK_SQL:
            return self.latest
        if sql == db.LATEST_SCORED_HEALTH_CHECK_SQL:
            return self.scored
        if sql == db.SETTLE_BASE_SQL:
            return self.settle
        raise AssertionError(f"unexpected read: {sql}")

    async def fetch(self, *args):
        raise AssertionError("health inputs use fetchrow only")

    async def execute(self, *args):
        raise AssertionError("the inputs never write")


# ── scheduler.last_slot_before ───────────────────────────────────

@pytest.mark.parametrize("now, expected", [
    (et(2026, 9, 10, 14, 7), ("market", et(2026, 9, 10, 14, 5))),     # mid-session
    (et(2026, 9, 10, 14, 5), ("market", et(2026, 9, 10, 14, 5))),     # exactly on a slot
    (et(2026, 9, 10, 16, 21), ("settle", et(2026, 9, 10, 16, 20))),   # just after settle
    (et(2026, 9, 10, 16, 26), ("settle", et(2026, 9, 10, 16, 20))),
    (et(2026, 9, 10, 16, 10), ("market", et(2026, 9, 10, 16, 0))),    # between close and settle
    (et(2026, 9, 11, 7, 25), ("night", et(2026, 9, 11, 7, 15))),      # pre-market: that morning's night slot
    (et(2026, 9, 12, 10, 0), ("night", et(2026, 9, 11, 16, 45))),     # Saturday: Friday's last night slot
    (et(2026, 11, 26, 10, 0), ("night", et(2026, 11, 26, 9, 45))),    # Thanksgiving: CME trades, XNYS does not
    (et(2026, 11, 27, 13, 10), ("market", et(2026, 11, 27, 13, 0))),  # early close, close slot inclusive
    (et(2026, 11, 27, 15, 0), ("market", et(2026, 11, 27, 13, 0))),
    (et(2026, 11, 27, 16, 25), ("settle", et(2026, 11, 27, 16, 20))),
], ids=["mid-session", "on-slot", "16:21", "16:26", "16:10", "pre-market", "saturday",
        "holiday", "early-close-13:10", "early-close-15:00", "early-close-settle"])
def test_last_slot_before(now, expected):
    assert scheduler.last_slot_before(now) == expected


# ── Health section ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_inputs_health_from_latest_row():
    now = et(2026, 9, 10, 14, 7)
    pool = HealthPool(latest=_row(et(2026, 9, 10, 14, 5, 2)),
                      settle={"checked_at": et(2026, 9, 9, 16, 20, 2), "score": 63})
    health, settle = await macro_inputs.health_section(pool, now)

    assert health == {
        "status": "ok", "checkedAt": et(2026, 9, 10, 14, 5, 2).isoformat(), "kind": "market",
        "score": 68, "regime": "CAUTIOUS", "trend": "stable", "coverage": 100, "stale": False,
        "staleMonitors": [],
        "monitors": {name: {"score": m["score"], "weight": m["weight"], "stale": False,
                            "detail": f"{name} detail"} for name, m in MONITORS.items()},
        "ageMinutes": 1, "lastExpectedSlotAt": et(2026, 9, 10, 14, 0).isoformat(),
    }
    assert "lastScored" not in health
    assert macro_inputs.health_ready(health) is True
    assert macro_inputs.health_freshness(health) == {
        "healthStatus": "ok", "healthStale": False, "healthMonitorsStale": False, "healthAgeMinutes": 1}
    # Only reads, the latest-scored read skipped for a scored row.
    assert [sql for sql, _ in pool.calls] == [db.LATEST_HEALTH_CHECK_SQL, db.SETTLE_BASE_SQL]


@pytest.mark.asyncio
@pytest.mark.parametrize("now, row_at, kind, stale", [
    (et(2026, 9, 10, 14, 7), et(2026, 9, 10, 14, 5, 2), "market", False),   # in session, on time
    (et(2026, 9, 10, 14, 12), et(2026, 9, 10, 14, 0, 2), "market", True),   # one slot late
    (et(2026, 9, 11, 7, 30), et(2026, 9, 11, 7, 15, 2), "night", False),    # 07:30 with the 07:15 night row
    (et(2026, 9, 11, 7, 30), et(2026, 9, 10, 16, 20, 2), "settle", True),   # 07:30, the night rows are missing
    (et(2026, 9, 12, 10, 0), et(2026, 9, 11, 16, 45, 3), "night", False),   # Saturday: Friday's last night row
], ids=["on-time", "one-slot-late", "0730-with-night", "0730-without-night", "saturday"])
async def test_inputs_health_stale_against_last_expected_slot(now, row_at, kind, stale):
    health, _ = await macro_inputs.health_section(HealthPool(latest=_row(row_at, kind=kind)), now)
    fresh = macro_inputs.health_freshness(health)
    assert fresh["healthStale"] is stale
    assert health["kind"] == kind
    if now == et(2026, 9, 12, 10, 0):
        # Amendment B: a fresh verdict never hides the age. With night rows the
        # 07:30 brief is 15 minutes behind, so the weekend carries the old case:
        # fresh against Friday's last night slot, and still ~17 hours old.
        assert fresh["healthAgeMinutes"] > 600
        assert fresh["healthAgeMinutes"] == health["ageMinutes"] == 1034
        assert health["lastExpectedSlotAt"] == et(2026, 9, 11, 16, 45).isoformat()


@pytest.mark.asyncio
async def test_inputs_health_monitors_stale_flag():
    now = et(2026, 9, 10, 14, 7)
    health, _ = await macro_inputs.health_section(HealthPool(latest=_row(et(2026, 9, 10, 14, 5), stale=True)), now)
    fresh = macro_inputs.health_freshness(health)
    assert (fresh["healthStale"], fresh["healthMonitorsStale"]) == (False, True)
    # A stored monitors blob of the wrong shape answers null, never raises.
    health, _ = await macro_inputs.health_section(
        HealthPool(latest=_row(et(2026, 9, 10, 14, 5), monitors={"vix": 5})), now)
    assert health["monitors"] is None


@pytest.mark.asyncio
async def test_inputs_health_null_score_uses_last_scored():
    now = et(2026, 9, 10, 14, 7)
    scored_at = et(2026, 9, 10, 13, 55, 1)
    pool = HealthPool(latest=_row(et(2026, 9, 10, 14, 5), score=None, regime=None, trend=None),
                      scored={"checked_at": scored_at, "score": 66, "regime": "CAUTIOUS"})
    health, _ = await macro_inputs.health_section(pool, now)
    assert health["score"] is None
    assert health["lastScored"] == {"score": 66, "regime": "CAUTIOUS", "checkedAt": scored_at.isoformat()}
    assert macro_inputs.health_ready(health) is True

    none_scored = HealthPool(latest=_row(et(2026, 9, 10, 14, 5), score=None, regime=None, trend=None))
    health, _ = await macro_inputs.health_section(none_scored, now)
    assert health["lastScored"] is None
    assert macro_inputs.health_ready(health) is False


@pytest.mark.asyncio
async def test_inputs_health_no_checks():
    now = et(2026, 9, 10, 14, 7)
    health, settle = await macro_inputs.health_section(HealthPool(), now)
    assert health == {"status": "no_checks", "lastExpectedSlotAt": et(2026, 9, 10, 14, 0).isoformat()}
    assert settle == {"present": False, "checkedAt": None, "score": None}
    assert macro_inputs.health_ready(health) is False
    assert macro_inputs.health_freshness(health) == {
        "healthStatus": "no_checks", "healthStale": True, "healthMonitorsStale": False, "healthAgeMinutes": None}


@pytest.mark.asyncio
@pytest.mark.parametrize("pool", [
    None,
    HealthPool(fail=asyncpg.PostgresError("boom")),
    HealthPool(fail=asyncpg.InterfaceError("closed")),
    HealthPool(fail=ConnectionError("reset")),
    HealthPool(fail=TimeoutError()),
], ids=["no-pool", "postgres-error", "interface-error", "connection-error", "timeout"])
async def test_inputs_health_db_unavailable(pool, caplog):
    """Never raises: the other sections are still built (the assembly and the
    all-dependencies-down endpoint rows prove that half)."""
    now = et(2026, 9, 10, 14, 7)
    with caplog.at_level(logging.WARNING, logger="macro_inputs"):
        health, settle = await macro_inputs.health_section(pool, now)
    assert health["status"] == "unavailable"
    assert settle["present"] is False
    assert macro_inputs.health_ready(health) is False
    assert macro_inputs.health_freshness(health)["healthStale"] is True
    if pool is not None:
        assert any("health read failed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_inputs_settle_present_or_absent():
    now = et(2026, 9, 11, 7, 30)
    settle_at = et(2026, 9, 10, 16, 20, 2)
    pool = HealthPool(latest=_row(settle_at, kind="settle", score=63),
                      settle={"checked_at": settle_at, "score": 63})
    _, settle = await macro_inputs.health_section(pool, now)
    assert settle == {"present": True, "checkedAt": settle_at.isoformat(), "score": 63}
    # The settle cutoff is now itself: the latest scored settle before this call.
    (sql, args), = [c for c in pool.calls if c[0] == db.SETTLE_BASE_SQL]
    assert args == (now,)

    _, settle = await macro_inputs.health_section(HealthPool(latest=_row(et(2026, 9, 10, 16, 0))), now)
    assert settle == {"present": False, "checkedAt": None, "score": None}


# ── News section (commit 4b) ─────────────────────────────────────

import asyncio
from types import SimpleNamespace

import httpx

import news_poller

DE_URL = "http://data-engine-dev:8001"


@pytest.fixture(autouse=True)
def _news_env(monkeypatch):
    monkeypatch.setattr(macro_inputs.settings, "data_engine_url", DE_URL)
    macro_inputs._bad_body_logged.clear()


def _item(i, **over):
    body = {"publishedAt": f"2026-09-10T17:{i:02d}:00+00:00", "source": "CNBC", "title": f"Headline {i}",
            "summary": f"Summary {i}", "url": f"https://example.com/{i}"}
    body.update(over)
    return body


def _http(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


def _answer(status=200, json_body=None, content=None, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        if content is not None:
            return httpx.Response(status, content=content)
        return httpx.Response(status, json=json_body)
    return handler


@pytest.mark.asyncio
async def test_inputs_news_from_data_engine():
    body = [_item(30), _item(20, title="T" * 400, source=None, summary=None), _item(10, summary="S" * 500)]
    async with _http(_answer(json_body=body)) as http:
        news = await macro_inputs.news_section(http, 24)
    assert news == {
        "status": "ok", "cause": None, "hours": 24, "limit": 50, "count": 3, "truncated": 2,
        "trimmedForSize": 0,
        "items": [
            {"publishedAt": "2026-09-10T17:30:00+00:00", "source": "CNBC", "title": "Headline 30",
             "summary": "Summary 30"},
            {"publishedAt": "2026-09-10T17:20:00+00:00", "source": None, "title": "T" * 300, "summary": ""},
            {"publishedAt": "2026-09-10T17:10:00+00:00", "source": "CNBC", "title": "Headline 10",
             "summary": "S" * 300},
        ],
    }
    assert all("url" not in item for item in news["items"])


@pytest.mark.asyncio
@pytest.mark.parametrize("now, hours", [
    (et(2026, 9, 15, 7, 30), 24),         # Tue: Mon 16:00, 15.5 h → the floor
    (et(2026, 9, 11, 16, 30), 25),        # Fri 16:30: Thu 16:00, 24.5 h
    (et(2026, 9, 14, 7, 30), 64),         # Mon: Fri 16:00, 63.5 h
    (et(2026, 9, 8, 7, 30), 88),          # Tue after Labor Day: Fri 09-04 16:00, 87.5 h
    (et(2026, 11, 27, 7, 30), 40),        # Fri after Thanksgiving: Wed 16:00, 39.5 h
    (et(2026, 12, 28, 7, 30), 91),        # Mon after Christmas Fri: Thu 12-24's 13:00 early close, 90.5 h
    (et(2026, 12, 28, 16, 30), 96),       # the same Monday's 16:30 slot: 99.5 h → the ceiling, never 100
], ids=["tue", "fri-1630", "mon", "after-labor-day", "after-thanksgiving", "after-christmas",
        "after-christmas-1630-ceiling"])
async def test_inputs_news_request_within_route_bounds(monkeypatch, now, hours):
    """Spec 3.6b decision 4: every assembly asks for the hours since the latest
    XNYS close before today, and the document stores the window it used."""
    monkeypatch.setattr(news_poller.settings, "news_poll_enabled", False)
    seen = []
    async with _http(_answer(json_body=[_item(1)], seen=seen)) as http:
        doc = await macro_inputs.assemble_inputs(_state(pool=None), FakeFred(), http, now=now)
    (request,) = seen
    assert request.method == "GET"
    assert str(request.url).split("?")[0] == f"{DE_URL}/news/market"
    assert dict(request.url.params) == {"hours": str(hours), "limit": "50"}
    assert (doc["news"]["hours"], doc["news"]["limit"], macro_inputs.news_hours(now)) == (hours, 50, hours)
    # The window's bounds, beside the pinned copy of data-engine's (spec 3.6a decision 2).
    assert (macro_inputs.NEWS_HOURS_MIN, macro_inputs.NEWS_HOURS_MAX, macro_inputs.DATA_ENGINE_NEWS_MAX_HOURS,
            macro_inputs.DATA_ENGINE_NEWS_MAX_LIMIT) == (24, 96, 168, 100)
    assert macro_inputs.NEWS_HOURS_MAX <= macro_inputs.DATA_ENGINE_NEWS_MAX_HOURS, (
        "data-engine's GET /news/market bounds are pinned there by "
        "test_news_market_bounds_pinned_for_risk_shield. Change both.")
    assert 1 <= macro_inputs.NEWS_LIMIT <= macro_inputs.DATA_ENGINE_NEWS_MAX_LIMIT


@pytest.mark.asyncio
async def test_inputs_news_empty_is_flagged():
    """An empty 24 h is a failure of the feed, not a quiet day: the section
    says so, and the anyStale truth table (commit 4c) counts it."""
    async with _http(_answer(json_body=[])) as http:
        news = await macro_inputs.news_section(http, 24)
    assert (news["status"], news["count"], news["items"], news["cause"]) == ("empty", 0, [], None)
    state = SimpleNamespace(news_status=news_poller.initial_news_status())
    assert macro_inputs.news_freshness(news, state, et(2026, 9, 10, 14, 7))["newsStatus"] == "empty"


def _raise(exc):
    def handler(request):
        raise exc
    return handler


async def _slow(request):
    await asyncio.sleep(1)
    return httpx.Response(200, json=[])


@pytest.mark.asyncio
@pytest.mark.parametrize("handler, cause", [
    (_raise(httpx.ConnectError("refused")), "ConnectError"),
    (_raise(httpx.ReadTimeout("slow")), "timeout"),
    (_slow, "timeout"),
    (_answer(307), "HTTP 307"),
    (_answer(404, json_body={"detail": "Not Found"}), "HTTP 404"),
    (_answer(422, json_body={"detail": []}), "HTTP 422"),
    (_answer(500, content=b"boom"), "HTTP 500"),
    (_answer(503, json_body={"detail": "database unavailable"}), "HTTP 503"),
], ids=["transport", "httpx-timeout", "hard-timeout", "307", "404", "422", "500", "503"])
async def test_inputs_news_unavailable(handler, cause, monkeypatch, caplog):
    monkeypatch.setattr(macro_inputs, "NEWS_TIMEOUT", 0.05)
    calls = []

    async def counting(request):
        calls.append(request)
        result = handler(request)
        return await result if asyncio.iscoroutine(result) else result

    with caplog.at_level(logging.WARNING, logger="macro_inputs"):
        async with _http(counting) as http:
            news = await macro_inputs.news_section(http, 24)
    assert (news["status"], news["cause"], news["items"]) == ("unavailable", cause, [])
    assert len(calls) == 1                     # no retry, no redirect followed
    assert [r.levelname for r in caplog.records] == ["WARNING"]


@pytest.mark.asyncio
async def test_inputs_news_bad_body(caplog):
    bodies = [
        ({"items": []}, "not a list"),
        ([_item(1), {k: v for k, v in _item(2).items() if k != "title"}], "item missing title"),
    ]
    with caplog.at_level(logging.DEBUG, logger="macro_inputs"):
        for body, _ in bodies + bodies:          # each problem twice
            async with _http(_answer(json_body=body)) as http:
                news = await macro_inputs.news_section(http, 24)
            assert (news["status"], news["cause"], news["items"]) == ("unavailable", "bad body", [])
        async with _http(_answer(content=b"<html>")) as http:
            assert (await macro_inputs.news_section(http, 24))["cause"] == "bad body"
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 3                      # one per distinct problem, never repeated
    assert any("not a list" in m for m in errors) and any("item missing title" in m for m in errors)
    assert any("not JSON" in m for m in errors)


def test_inputs_freshness_carries_news_poll_state(monkeypatch):
    now = et(2026, 9, 10, 14, 7)
    news = {"status": "ok"}
    status = news_poller.initial_news_status()
    status.update(startedAt=(now - timedelta(hours=5)).isoformat(),
                  lastSuccessAt=(now - timedelta(minutes=61)).isoformat(), lastError="ingest: HTTP 503")
    state = SimpleNamespace(news_status=status)

    monkeypatch.setattr(news_poller.settings, "news_poll_enabled", True)
    fresh = macro_inputs.news_freshness(news, state, now)
    assert fresh == {"newsStatus": "ok", "newsPollStale": True, "lastNewsPollAt": status["lastSuccessAt"],
                     "newsLastError": "ingest: HTTP 503"}
    assert {k: fresh[k] for k in news_poller.stale_view(state, now)} == news_poller.stale_view(state, now)

    status["lastSuccessAt"] = (now - timedelta(minutes=60)).isoformat()
    assert macro_inputs.news_freshness(news, state, now)["newsPollStale"] is False

    monkeypatch.setattr(news_poller.settings, "news_poll_enabled", False)
    assert macro_inputs.news_freshness(news, state, now)["newsPollStale"] is None


# ── The document (commit 4c) ─────────────────────────────────────

import econ_calendar
from cache import MemoryCooldowns
from monitors import fred
from monitors.errors import FredError
from tests.fake_redis import FakeRedis


class FakeFred:
    """A FRED client: one daily observation per series ending `last`; raises per series."""

    def __init__(self, last="2026-09-09", raises=None):
        self.last, self.raises, self.calls = last, raises or {}, []

    def observation_start(self):
        return "2024-07-02"

    async def observations(self, sid):
        self.calls.append(sid)
        if sid in self.raises:
            raise self.raises[sid]
        return {"observations": [{"date": "2026-08-01", "value": "1.0"}, {"date": self.last, "value": "2.0"}]}


def test_inputs_calendar_section(monkeypatch):
    section = macro_inputs.calendar_section(et(2026, 9, 10, 14, 7))
    assert section["status"] == "ok"
    assert (section["from"], section["to"]) == ("2026-09-10", "2026-09-16")
    assert [(e["date"], e["type"]) for e in section["events"]] == [("2026-09-11", "cpi"), ("2026-09-16", "fomc")]
    assert macro_inputs.calendar_freshness(section) == {
        "calendarStatus": "ok", "calendarWindowShort": False, "calendarRenewalDue": False}

    late = macro_inputs.calendar_section(et(2026, 12, 28, 9, 0))
    assert late["coverageShort"] is True and late["renewalDue"] is True
    assert macro_inputs.calendar_freshness(late)["calendarWindowShort"] is True

    def unavailable():
        raise econ_calendar.CalendarUnavailable("econ_calendar.json: cannot read (FileNotFoundError)")
    monkeypatch.setattr(econ_calendar, "load", unavailable)
    gone = macro_inputs.calendar_section(et(2026, 9, 10, 14, 7))
    assert gone == {"status": "unavailable", "from": None, "to": None, "coversThrough": None,
                    "coverageShort": None, "renewalDue": None, "events": []}
    assert macro_inputs.calendar_freshness(gone) == {
        "calendarStatus": "unavailable", "calendarWindowShort": False, "calendarRenewalDue": None}


@pytest.mark.asyncio
async def test_inputs_fred_section():
    now = et(2026, 9, 10, 14, 7)
    client = FakeFred(raises={"DGS2": FredError("DGS2: HTTP 500")})
    state = _state(pool=None)
    doc = await macro_inputs.assemble_inputs(state, client, _http(_answer(json_body=[])), now=now)
    assert doc["fred"] == await fred.get_fred_view(FakeRedis(), MemoryCooldowns(),
                                                   FakeFred(raises={"DGS2": FredError("x")}), now=lambda: now)
    assert doc["fred"]["DGS2"]["staleReason"] == "no_data"
    assert doc["freshness"]["fredStaleSeries"] == ["DGS2"]
    # An age-stale series is listed too; nothing carries observation arrays.
    old = await macro_inputs.assemble_inputs(_state(pool=None), FakeFred(last="2026-08-20"),
                                             _http(_answer(json_body=[])), now=now)
    assert old["freshness"]["fredStaleSeries"] == [s for s in fred.FRED_SERIES if fred.FRED_CADENCE[s][1] < 21]
    assert all("observations" not in entry for entry in old["fred"].values())


GOOD = {"healthStatus": "ok", "healthStale": False, "healthMonitorsStale": False, "healthAgeMinutes": 2,
        "settlePresent": True, "newsStatus": "ok", "newsPollStale": False, "lastNewsPollAt": None,
        "newsLastError": None, "calendarStatus": "ok", "calendarWindowShort": False,
        "calendarRenewalDue": False, "fredStaleSeries": []}


@pytest.mark.parametrize("flip, stale", [
    ({}, False),
    ({"healthStatus": "no_checks"}, True), ({"healthStatus": "unavailable"}, True),
    ({"healthStale": True}, True), ({"healthMonitorsStale": True}, True),
    ({"settlePresent": False}, True),
    ({"newsStatus": "empty"}, True), ({"newsStatus": "unavailable"}, True),
    ({"newsPollStale": True}, True),
    ({"calendarStatus": "unavailable"}, True), ({"calendarWindowShort": True}, True),
    ({"fredStaleSeries": ["CPIAUCSL"]}, True),
    ({"calendarRenewalDue": True}, False),     # maintenance, not stale input
    ({"newsPollStale": None}, False),          # poller off: no evidence
    ({"healthAgeMinutes": 909}, False),        # the age alone is not a verdict
], ids=lambda v: str(v))
def test_inputs_any_stale_truth_table(flip, stale):
    assert macro_inputs.any_stale({**GOOD, **flip}) is stale


def _doc(n_items, text="x"):
    items = [{"publishedAt": f"2026-09-10T17:{i % 60:02d}:00+00:00", "source": "S",
              "title": f"{i}" + text, "summary": text} for i in range(n_items)]
    return {"schemaVersion": 1, "health": {}, "fred": {}, "freshness": {},
            "news": {"status": "ok", "count": n_items, "trimmedForSize": 0, "items": items}}


def test_inputs_bounded_and_strict_json(monkeypatch, caplog):
    # The real limit: 50 items of 300 four-byte characters are ~180 KB.
    worst = _doc(50, text="\U0001F4C8" * 300)
    assert macro_inputs.encoded_size(worst) > macro_inputs.INPUTS_MAX_BYTES
    with caplog.at_level(logging.WARNING, logger="macro_inputs"):
        bounded = macro_inputs.bound(worst)
    assert macro_inputs.encoded_size(bounded) <= macro_inputs.INPUTS_MAX_BYTES
    kept = bounded["news"]["count"]
    assert 0 < kept < 50 and bounded["news"]["trimmedForSize"] == 50 - kept
    assert [i["title"][:2] for i in bounded["news"]["items"]] == [f"{i}\U0001F4C8"[:2] for i in range(kept)]
    assert any("dropped for size" in r.getMessage() for r in caplog.records)

    # Under the limit: untouched.
    small = _doc(3)
    assert macro_inputs.bound(small)["news"]["trimmedForSize"] == 0

    # Still over with no news left: a bug.
    monkeypatch.setattr(macro_inputs, "INPUTS_MAX_BYTES", 10)
    with pytest.raises(macro_inputs.InputsTooLarge):
        macro_inputs.bound(_doc(2))

    # A NaN anywhere is refused (allow_nan=False).
    monkeypatch.setattr(macro_inputs, "INPUTS_MAX_BYTES", 64_000)
    nan = _doc(1)
    nan["health"]["score"] = float("nan")
    with pytest.raises(ValueError):
        macro_inputs.bound(nan)


def _state(pool, redis=None):
    return SimpleNamespace(db_pool=pool, redis=redis if redis is not None else FakeRedis(),
                           cooldowns=MemoryCooldowns(), news_status=news_poller.initial_news_status())


@pytest.mark.asyncio
async def test_inputs_repeat_call_is_read_only(monkeypatch):
    monkeypatch.setattr(news_poller.settings, "news_poll_enabled", False)
    now = et(2026, 9, 10, 14, 7)
    redis = FakeRedis()
    pool = HealthPool(latest=_row(et(2026, 9, 10, 14, 5, 2)),
                      settle={"checked_at": et(2026, 9, 9, 16, 20, 2), "score": 63})
    state = _state(pool, redis)
    client = FakeFred()
    async with _http(_answer(json_body=[_item(30), _item(20)])) as http:
        first = await macro_inputs.assemble_inputs(state, client, http, now=now)
        sets_after_first = list(redis.set_calls)
        second = await macro_inputs.assemble_inputs(state, client, http, now=now + timedelta(seconds=1))

    assert list(first) == ["schemaVersion", "assembledAt", "ready", "health", "settle", "news",
                           "calendar", "fred", "freshness"]
    assert list(first["freshness"]) == list(GOOD) + ["anyStale"]
    assert first["ready"] is True and first["freshness"]["anyStale"] is False
    assert first["freshness"]["newsPollStale"] is None
    # Equal except assembledAt and FRED's source, which is fresh on the first
    # read and cache on the repeat (6 h cache; test_fred_view_repeat_call_is_cached).
    def without_volatile(doc):
        body = {k: v for k, v in doc.items() if k != "assembledAt"}
        body["fred"] = {sid: {k: v for k, v in e.items() if k != "source"} for sid, e in doc["fred"].items()}
        return body
    assert without_volatile(first) == without_volatile(second)
    assert {e["source"] for e in first["fred"].values()} == {"fresh"}
    assert {e["source"] for e in second["fred"].values()} == {"cache"}
    assert second["assembledAt"] == (now + timedelta(seconds=1)).isoformat()

    # Writes: only FRED's own cache and last-known keys, and none on the repeat.
    assert sets_after_first and all(k.startswith(("tf:risk:cache:fred:", "tf:risk:cache:fred_last:"))
                                    for k, _, _ in sets_after_first)
    assert redis.set_calls == sets_after_first
    assert len(client.calls) == 8
    assert {sql for sql, _ in pool.calls} <= {db.LATEST_HEALTH_CHECK_SQL, db.SETTLE_BASE_SQL,
                                              db.LATEST_SCORED_HEALTH_CHECK_SQL}
    json.dumps(first, allow_nan=False)


# ── GET /macro/brief/inputs and the lifespan (commit 4d) ─────────

from fastapi.testclient import TestClient

import cache
import main
from monitors.errors import FredNotConfigured
from monitors.fred_client import FredClient

ENDPOINT_NOW = et(2026, 9, 10, 14, 7)


class SlowFred(FakeFred):
    async def observations(self, sid):
        await asyncio.sleep(0.01)          # lets a second request reach the lock mid-walk
        return await super().observations(sid)


@pytest.fixture
def app_inputs(monkeypatch):
    """app.state stubbed without the lifespan, as test_health.py does."""
    clock = [1000.0]
    monkeypatch.setattr(main, "_now", lambda: ENDPOINT_NOW)
    monkeypatch.setattr(main, "_monotonic", lambda: clock[0])
    monkeypatch.setattr(news_poller.settings, "news_poll_enabled", False)
    http_calls = []

    def _set(*, pool=None, redis=None, fred_client=None, handler=None):
        st = main.app.state
        st.db_pool, st.redis = pool, redis
        st.cooldowns = MemoryCooldowns()
        st.news_status = news_poller.initial_news_status()
        st.fred_client = fred_client or FakeFred()
        st.inputs_http = _http(_answer(json_body=[_item(30)], seen=http_calls) if handler is None else handler)
        st.inputs_lock, st.inputs_last = None, None
        return st

    yield SimpleNamespace(set=_set, clock=clock, http_calls=http_calls)
    st = main.app.state
    st.db_pool = st.redis = st.inputs_last = st.inputs_lock = None


def _healthy_pool():
    return HealthPool(latest=_row(et(2026, 9, 10, 14, 5, 2)),
                      settle={"checked_at": et(2026, 9, 9, 16, 20, 2), "score": 63})


def test_macro_inputs_endpoint_returns_document(app_inputs):
    app_inputs.set(pool=_healthy_pool(), redis=FakeRedis())
    client = TestClient(main.app)
    resp = client.get("/macro/brief/inputs")
    assert resp.status_code == 200
    body = resp.json()
    assert list(body) == ["schemaVersion", "assembledAt", "ready", "health", "settle", "news",
                          "calendar", "fred", "freshness", "cached"]
    assert body["cached"] is False
    assert body["assembledAt"] == ENDPOINT_NOW.isoformat()
    assert (body["ready"], body["health"]["status"], body["settle"]["present"], body["news"]["status"],
            body["calendar"]["status"]) == (True, "ok", True, "ok", "ok")
    assert "cached" not in main.app.state.inputs_last[1]
    assert "GET  /macro/brief/inputs" in client.get("/").json()["endpoints"]


def test_macro_inputs_endpoint_all_dependencies_down(app_inputs):
    """The twin's shape with nothing reachable: still 200, every section says why."""
    app_inputs.set(pool=None, redis=None,
                   fred_client=FakeFred(raises={"VIXCLS": FredNotConfigured("VIXCLS: FRED_API_KEY is not set")}),
                   handler=_raise(httpx.ConnectError("refused")))
    resp = TestClient(main.app).get("/macro/brief/inputs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is False
    assert body["health"]["status"] == "unavailable" and body["settle"]["present"] is False
    assert (body["news"]["status"], body["news"]["cause"]) == ("unavailable", "ConnectError")
    assert body["calendar"]["status"] == "ok"
    assert {e["staleReason"] for e in body["fred"].values()} == {"no_data"}
    assert body["freshness"]["fredStaleSeries"] == list(fred.FRED_SERIES)
    assert body["freshness"]["anyStale"] is True and body["freshness"]["newsPollStale"] is None


@pytest.mark.asyncio
async def test_macro_inputs_concurrent_calls_share_one_fred_walk(app_inputs):
    client = SlowFred()
    app_inputs.set(pool=_healthy_pool(), redis=FakeRedis(), fred_client=client)
    first, second = await asyncio.gather(main.macro_brief_inputs(), main.macro_brief_inputs())
    assert len(client.calls) == 8                  # one walk, not two
    assert sorted([first["cached"], second["cached"]]) == [False, True]
    strip = lambda d: {k: v for k, v in d.items() if k != "cached"}
    assert strip(first) == strip(second)


@pytest.mark.asyncio
async def test_macro_inputs_endpoint_reuses_document_within_60s(app_inputs, monkeypatch):
    pool = _healthy_pool()
    client = FakeFred()
    app_inputs.set(pool=pool, redis=FakeRedis(), fred_client=client)
    assembled = []
    real = macro_inputs.assemble_inputs

    async def spy(*a, **k):
        doc = await real(*a, **k)
        assembled.append(doc)
        return doc
    monkeypatch.setattr(macro_inputs, "assemble_inputs", spy)

    first = await main.macro_brief_inputs()
    reads, requests = len(pool.calls), len(app_inputs.http_calls)
    app_inputs.clock[0] += 59
    again = await main.macro_brief_inputs()
    assert (first["cached"], again["cached"]) == (False, True)
    assert {k: v for k, v in again.items() if k != "cached"} == {k: v for k, v in first.items() if k != "cached"}
    assert (len(pool.calls), len(app_inputs.http_calls), len(assembled)) == (reads, requests, 1)

    app_inputs.clock[0] += 2                        # 61 s after the assembly
    fresh = await main.macro_brief_inputs()
    assert fresh["cached"] is False
    assert len(assembled) == 2 and len(pool.calls) > reads
    assert all("cached" not in doc for doc in assembled)


def test_macro_inputs_failed_assembly_is_not_reused(app_inputs, monkeypatch):
    app_inputs.set(pool=_healthy_pool(), redis=FakeRedis())
    real = macro_inputs.assemble_inputs
    calls = []

    async def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("bug in assembly")
        return await real(*a, **k)
    monkeypatch.setattr(macro_inputs, "assemble_inputs", flaky)

    client = TestClient(main.app, raise_server_exceptions=False)
    assert client.get("/macro/brief/inputs").status_code == 500
    assert main.app.state.inputs_last is None
    app_inputs.clock[0] += 5
    resp = client.get("/macro/brief/inputs")
    assert resp.status_code == 200 and resp.json()["cached"] is False
    assert len(calls) == 2


def test_macro_inputs_endpoint_ignores_brief_flag(app_inputs, monkeypatch):
    bodies = []
    for flag in (False, True):
        monkeypatch.setattr(main.settings, "macro_brief_enabled", flag)
        app_inputs.set(pool=_healthy_pool(), redis=FakeRedis())
        resp = TestClient(main.app).get("/macro/brief/inputs")
        assert resp.status_code == 200
        bodies.append(resp.json())
    assert bodies[0] == bodies[1]


def test_lifespan_closes_inputs_clients(monkeypatch):
    events = []

    class Pool:
        async def close(self):
            events.append("db closed")

    class Redis:
        async def close(self):
            events.append("redis closed")

    async def ok_redis(*a, **k):
        return Redis()

    async def ok_pool(*a, **k):
        return Pool()

    monkeypatch.setattr(cache, "create_redis", ok_redis)
    monkeypatch.setattr(db, "create_db_pool", ok_pool)
    monkeypatch.setattr(main.settings, "scheduler_enabled", False)
    monkeypatch.setattr(main.settings, "news_poll_enabled", False)
    real_fred_close = FredClient.aclose
    real_http_close = httpx.AsyncClient.aclose

    async def fred_close(self):
        events.append("fred closed")
        await real_fred_close(self)

    async def http_close(self):
        events.append("http closed")
        await real_http_close(self)
    monkeypatch.setattr(FredClient, "aclose", fred_close)
    monkeypatch.setattr(httpx.AsyncClient, "aclose", http_close)

    with TestClient(main.app) as client:
        client.get("/health")
        state = main.app.state
        assert isinstance(state.fred_client, FredClient) and state.fred_client.calls_made == 0
        assert isinstance(state.inputs_http, httpx.AsyncClient) and not state.inputs_http.is_closed
        assert state.inputs_last is None and state.inputs_lock is not None
        http = state.inputs_http
    assert http.is_closed
    assert "fred closed" in events and "http closed" in events
    assert max(events.index("fred closed"), events.index("http closed")) < events.index("db closed")
    assert events.index("db closed") < events.index("redis closed")
