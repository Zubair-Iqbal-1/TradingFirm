"""Part 3.4c — assembling the weekend block's inputs, and the situation
route (spec D2, D6, D7, F1-F5, F14, F16, F17). data-engine over an httpx
MockTransport, Redis via FakeRedis, the calendar over a temp file. No socket."""

import copy
import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

import cache
import econ_calendar
import macro_inputs
import main
import weekend_inputs
from config import settings
from scoring import weekend
from tests.fake_redis import FakeRedis

ET = ZoneInfo("America/New_York")
TOKEN = "twin-token-for-tests"


def et(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=ET).astimezone(timezone.utc)


# Friday 2026-09-18: close 16:00 ET, next open Monday 2026-09-21 09:30 ET.
NOW = et(2026, 9, 18, 15, 30)
CLOSE = et(2026, 9, 18, 16, 0)
NEXT_OPEN = et(2026, 9, 21, 9, 30)

CALENDAR = {
    "coversFrom": "2026-09-01", "coversThrough": "2026-12-31",
    "timezone": "America/New_York", "retrieved": "2026-09-10",
    "sources": {"fomc": "https://www.federalreserve.gov/x", "cpi": "https://www.bls.gov/c",
                "jobs": "https://www.bls.gov/j"},
    "events": [
        {"date": "2026-09-18", "time": "10:00", "type": "event",
         "title": "Before the close", "detail": ""},
        {"date": "2026-09-20", "time": "14:00", "type": "event",
         "title": "EU tariff deadline", "detail": "Sunday"},
        {"date": "2026-09-21", "time": "08:30", "type": "cpi",
         "title": "Consumer Price Index", "detail": "August 2026"},
        {"date": "2026-09-21", "time": "14:00", "type": "event",
         "title": "After the open", "detail": ""},
    ],
}


@pytest.fixture(autouse=True)
def fresh_calendar_cache():
    econ_calendar._cache.clear()
    econ_calendar._last_error.clear()
    yield
    econ_calendar._cache.clear()
    econ_calendar._last_error.clear()


@pytest.fixture
def calendar_file(tmp_path, monkeypatch):
    def _write(body=CALENDAR):
        path = tmp_path / "cal.json"
        path.write_text(json.dumps(body), encoding="utf-8")
        monkeypatch.setattr(econ_calendar, "CALENDAR_PATH", path)
        return path
    return _write


def news_http(items=None, status=200, body=None):
    """data-engine's GET /news/market over a MockTransport, recording calls."""
    calls = []

    def handler(request):
        calls.append(str(request.url))
        payload = body if body is not None else [
            {"publishedAt": "2026-09-18T17:00:00+00:00", "source": "Reuters",
             "title": title, "summary": ""} for title in (items or ["Quiet session"])]
        return httpx.Response(status, json=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    client.calls = calls
    return client


# ── events_between (D7, F1, F2) ──────────────────────────────────

@pytest.mark.asyncio
async def test_events_between_close_and_open_only(calendar_file):
    calendar_file()
    section = weekend_inputs.events_between(NOW, CLOSE, NEXT_OPEN)
    assert section["status"] == weekend.EVENTS_OK
    titles = [e["title"] for e in section["events"]]
    # The Sunday event and the Monday 08:30 CPI are in: both land before
    # anyone can react. The 10:00 Friday event and the Monday 14:00 are out.
    assert titles == ["EU tariff deadline", "Consumer Price Index"]


@pytest.mark.asyncio
async def test_event_exactly_at_the_open_counts(calendar_file):
    body = copy.deepcopy(CALENDAR)
    body["events"].append({"date": "2026-09-21", "time": "09:30", "type": "event",
                           "title": "At the bell", "detail": ""})
    calendar_file(body)
    titles = [e["title"] for e in weekend_inputs.events_between(NOW, CLOSE, NEXT_OPEN)["events"]]
    assert "At the bell" in titles


def test_calendar_unavailable_degrades_block(tmp_path, monkeypatch, caplog):
    """F1: an unreadable calendar is a status, not a raise."""
    monkeypatch.setattr(econ_calendar, "CALENDAR_PATH", tmp_path / "missing.json")
    with caplog.at_level(logging.ERROR):
        section = weekend_inputs.events_between(NOW, CLOSE, NEXT_OPEN)
    assert section == {"status": weekend.EVENTS_UNAVAILABLE, "coverageShort": False, "events": []}
    block = weekend.assess(capped_score=80, base_score=80, regime="HEALTHY", vix=None,
                           events=section, news=None, situation=None, window={},
                           assessed_at=NOW.isoformat())
    assert weekend.REASON_CALENDAR_UNAVAILABLE in [r["code"] for r in block["reasons"]]
    assert block["level"] == weekend.LEVEL_LOW


def test_calendar_coverage_short_flagged_in_block(calendar_file):
    """F2: a window past coversThrough keeps what the file has and says so."""
    body = copy.deepcopy(CALENDAR)
    body["coversThrough"] = "2026-09-19"
    body["events"] = [e for e in body["events"] if e["date"] <= "2026-09-19"]
    calendar_file(body)
    section = weekend_inputs.events_between(NOW, CLOSE, NEXT_OPEN)
    assert section["status"] == weekend.EVENTS_OK and section["coverageShort"] is True
    block = weekend.assess(capped_score=80, base_score=80, regime="HEALTHY", vix=None,
                           events=section, news=None, situation=None, window={},
                           assessed_at=NOW.isoformat())
    assert block["inputs"]["events"]["coverageShort"] is True


def test_events_without_a_window_are_unavailable(calendar_file):
    calendar_file()
    assert weekend_inputs.events_between(NOW, None, None)["status"] == weekend.EVENTS_UNAVAILABLE


def test_window_days_are_bounded(calendar_file, monkeypatch):
    """A far next open cannot ask the calendar for an unbounded window."""
    calendar_file()
    seen = {}
    real = econ_calendar.window

    def spy(cal, now, days):
        seen["days"] = days
        return real(cal, now, days)

    monkeypatch.setattr(econ_calendar, "window", spy)
    weekend_inputs.events_between(NOW, CLOSE, NEXT_OPEN + timedelta(days=90))
    assert seen["days"] == weekend_inputs.MAX_WINDOW_DAYS


# ── news_view (D6, F3, F4, F5) ───────────────────────────────────

@pytest.mark.asyncio
async def test_news_is_fetched_once_and_reused():
    """D6: eight Friday rows cost about two calls to data-engine."""
    r, http = FakeRedis(), news_http(["Talks this weekend"])
    first = await weekend_inputs.news_view(r, http)
    second = await weekend_inputs.news_view(r, http)
    assert first["cached"] is False and second["cached"] is True
    assert len(http.calls) == 1
    assert second["items"] == first["items"]
    key, _, ttl = r.set_calls[0]
    assert key == cache.risk_key(cache.KIND_WEEKEND_NEWS) and ttl == cache.TTL_WEEKEND_NEWS


@pytest.mark.asyncio
async def test_news_request_is_the_spec_window():
    r, http = FakeRedis(), news_http()
    await weekend_inputs.news_view(r, http)
    assert weekend_inputs.WEEKEND_NEWS_HOURS == 24
    assert "hours=24" in http.calls[0] and "limit=50" in http.calls[0]


@pytest.mark.asyncio
async def test_weekend_news_window_within_data_engine_bounds():
    """The pinned copy of data-engine's route bounds, like 3.6a's."""
    assert weekend_inputs.WEEKEND_NEWS_HOURS <= macro_inputs.DATA_ENGINE_NEWS_MAX_HOURS
    assert macro_inputs.NEWS_LIMIT <= macro_inputs.DATA_ENGINE_NEWS_MAX_LIMIT


@pytest.mark.asyncio
async def test_news_unavailable_degrades_block(caplog):
    """F3: a non-200 costs the news reason and nothing else."""
    r, http = FakeRedis(), news_http(status=503)
    with caplog.at_level(logging.WARNING):
        view = await weekend_inputs.news_view(r, http)
    assert view["status"] == "unavailable" and view["items"] == []
    assert r.set_calls == []                      # a blip is never cached for 5 minutes
    block = weekend.assess(capped_score=80, base_score=80, regime="HEALTHY", vix=None,
                           events=None, news=view, situation=None, window={},
                           assessed_at=NOW.isoformat())
    assert not any(r_["code"] == weekend.REASON_PENDING_DECISION for r_ in block["reasons"])


@pytest.mark.asyncio
async def test_news_bad_body_logged_once(caplog):
    """F4: our own route drifting is an ERROR once per distinct problem."""
    macro_inputs._bad_body_logged.clear()
    r, http = FakeRedis(), news_http(body={"not": "a list"})
    with caplog.at_level(logging.DEBUG):
        first = await weekend_inputs.news_view(r, http)
        second = await weekend_inputs.news_view(r, http)
    assert first["status"] == second["status"] == "unavailable"
    assert sum(rec.levelname == "ERROR" for rec in caplog.records) == 1
    macro_inputs._bad_body_logged.clear()


@pytest.mark.asyncio
async def test_redis_down_block_still_built(caplog):
    """F5: a Redis outage costs a fetch, never the block."""
    http = news_http(["Summit Sunday"])
    with caplog.at_level(logging.WARNING):
        view = await weekend_inputs.news_view(FakeRedis(fail_get=True, fail_set=True), http)
    assert view["status"] == "ok" and view["cached"] is False
    assert len(http.calls) == 1
    assert await weekend_inputs.read_situation(FakeRedis(fail_get=True)) is None


@pytest.mark.asyncio
async def test_news_view_without_redis_fetches_every_time():
    http = news_http()
    await weekend_inputs.news_view(None, http)
    await weekend_inputs.news_view(None, http)
    assert len(http.calls) == 2


# ── The situation store (D2, F14) ────────────────────────────────

@pytest.mark.asyncio
async def test_situation_round_trip_carries_a_bounded_ttl():
    r = FakeRedis()
    record = weekend_inputs.build_situation("port strike", 72, NOW)
    await weekend_inputs.write_situation(r, record, 72)
    key, _, ttl = r.set_calls[0]
    assert key == cache.state_key(cache.STATE_WEEKEND_SITUATION) and ttl == 72 * 3600
    assert await weekend_inputs.read_situation(r) == record
    assert record["expiresAt"] == (NOW + timedelta(hours=72)).isoformat()
    assert record["setBy"] == "operator"


@pytest.mark.asyncio
async def test_situation_ttl_is_capped_at_the_maximum():
    r = FakeRedis()
    await weekend_inputs.write_situation(r, weekend_inputs.build_situation("x", 999, NOW), 999)
    assert r.set_calls[0][2] == cache.SITUATION_MAX_HOURS * 3600


@pytest.mark.asyncio
async def test_expired_situation_ignored():
    """F14: the stored record is past its expiry, so the block reads none —
    and the key's own TTL means it cannot linger either."""
    r = FakeRedis()
    stale = weekend_inputs.build_situation("old thing", 1, NOW - timedelta(hours=5))
    await weekend_inputs.write_situation(r, stale, 1)
    raw = await weekend_inputs.read_situation(r)
    assert raw is not None
    assert weekend.situation_active(raw, NOW.isoformat()) is None


@pytest.mark.asyncio
async def test_situation_wrong_shape_reads_as_absent(caplog):
    r = FakeRedis()
    await r.set(cache.state_key(cache.STATE_WEEKEND_SITUATION), json.dumps(["a list"]), ex=60)
    with caplog.at_level(logging.WARNING):
        assert await weekend_inputs.read_situation(r) is None


@pytest.mark.asyncio
async def test_clear_situation_reports_whether_one_was_set():
    r = FakeRedis()
    assert await weekend_inputs.clear_situation(r) is False
    await weekend_inputs.write_situation(r, weekend_inputs.build_situation("x", 2, NOW), 2)
    assert await weekend_inputs.clear_situation(r) is True
    assert await weekend_inputs.read_situation(r) is None


# ── One assembly ─────────────────────────────────────────────────

class State:
    def __init__(self, r):
        self.redis = r


@pytest.mark.asyncio
async def test_assemble_returns_every_section(calendar_file):
    calendar_file()
    r = FakeRedis()
    await weekend_inputs.write_situation(r, weekend_inputs.build_situation("port strike", 48, NOW), 48)
    inputs = await weekend_inputs.assemble(State(r), news_http(["Talks this weekend"]),
                                           now=NOW, close_at=CLOSE, next_open_at=NEXT_OPEN)
    assert set(inputs) == {"events", "news", "situation"}
    block = weekend.assess(capped_score=80, base_score=80, regime="HEALTHY", vix=None,
                           window={"gapHours": 65}, assessed_at=NOW.isoformat(), **inputs)
    codes = {r_["code"] for r_ in block["reasons"]}
    assert codes == {weekend.REASON_SCHEDULED_EVENT, weekend.REASON_PENDING_DECISION,
                     weekend.REASON_ACTIVE_SITUATION}
    assert block["level"] == weekend.LEVEL_HIGH


@pytest.mark.asyncio
async def test_assemble_never_raises_with_everything_broken(tmp_path, monkeypatch):
    monkeypatch.setattr(econ_calendar, "CALENDAR_PATH", tmp_path / "gone.json")
    inputs = await weekend_inputs.assemble(State(FakeRedis(fail_get=True, fail_set=True)),
                                           news_http(status=500),
                                           now=NOW, close_at=CLOSE, next_open_at=NEXT_OPEN)
    assert inputs["events"]["status"] == weekend.EVENTS_UNAVAILABLE
    assert inputs["news"]["status"] == "unavailable" and inputs["situation"] is None


# ── The route (D2, F16, F17) ─────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    """TestClient over stubbed state, no lifespan (as in test_health.py)."""
    def _make(token=TOKEN, redis=None):
        monkeypatch.setattr(settings, "weekend_write_token",
                            type(settings.weekend_write_token)(token))
        monkeypatch.setattr(main, "_now", lambda: NOW)
        main.app.state.redis = redis
        main.app.state.db_pool = None
        return TestClient(main.app)
    yield _make
    main.app.state.redis = None


def test_situation_route_requires_secret(client):
    """F16: no token and a wrong token are both 401, and nothing is stored."""
    r = FakeRedis()
    c = client(redis=r)
    assert c.put("/market/weekend/situation", json={"text": "x", "hours": 2}).status_code == 401
    wrong = c.put("/market/weekend/situation", json={"text": "x", "hours": 2},
                  headers={"X-TF-Token": "nope"})
    assert wrong.status_code == 401 and TOKEN not in wrong.text
    assert c.delete("/market/weekend/situation").status_code == 401
    assert r.set_calls == [] and r.store == {}


def test_situation_route_disabled_without_secret(client):
    """F17: an empty token disables the route; it never falls open."""
    c = client(token="", redis=FakeRedis())
    for call in (c.put("/market/weekend/situation", json={"text": "x"},
                       headers={"X-TF-Token": ""}),
                 c.delete("/market/weekend/situation", headers={"X-TF-Token": "anything"})):
        assert call.status_code == 503 and "disabled" in call.json()["detail"]


def test_situation_put_and_delete_round_trip(client):
    r = FakeRedis()
    c = client(redis=r)
    put = c.put("/market/weekend/situation", json={"text": "port strike", "hours": 48},
                headers={"X-TF-Token": TOKEN})
    assert put.status_code == 200
    body = put.json()
    assert body["text"] == "port strike" and body["setBy"] == "operator"
    assert body["expiresAt"] == (NOW + timedelta(hours=48)).isoformat()
    assert TOKEN not in put.text
    assert json.loads(r.store[cache.state_key(cache.STATE_WEEKEND_SITUATION)])["text"] == "port strike"

    gone = c.delete("/market/weekend/situation", headers={"X-TF-Token": TOKEN})
    assert gone.status_code == 200 and gone.json() == {"cleared": True}
    assert c.delete("/market/weekend/situation",
                    headers={"X-TF-Token": TOKEN}).json() == {"cleared": False}


@pytest.mark.parametrize("body, why", [
    ({"hours": 2}, "no text"),
    ({"text": "", "hours": 2}, "blank text"),
    ({"text": "x", "hours": 0}, "hours below 1"),
    ({"text": "x", "hours": 169}, "hours past the maximum"),
    ({"text": "x" * 500, "hours": 2}, "text past the cap"),
])
def test_situation_put_validates_the_body(client, body, why):
    c = client(redis=FakeRedis())
    assert c.put("/market/weekend/situation", json=body,
                 headers={"X-TF-Token": TOKEN}).status_code == 422, why


def test_situation_default_hours_applied(client):
    c = client(redis=FakeRedis())
    body = c.put("/market/weekend/situation", json={"text": "x"},
                 headers={"X-TF-Token": TOKEN}).json()
    assert body["expiresAt"] == (NOW + timedelta(hours=main.SITUATION_DEFAULT_HOURS)).isoformat()


def test_situation_route_needs_redis(client):
    c = client(redis=None)
    assert c.put("/market/weekend/situation", json={"text": "x"},
                 headers={"X-TF-Token": TOKEN}).status_code == 503


def test_situation_route_503_when_the_write_fails(client, caplog):
    c = client(redis=FakeRedis(fail_set=True))
    with caplog.at_level(logging.WARNING):
        assert c.put("/market/weekend/situation", json={"text": "x"},
                     headers={"X-TF-Token": TOKEN}).status_code == 503
