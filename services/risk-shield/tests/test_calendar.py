"""Part 3.5 — econ_calendar.py and GET /market/calendar. No network, no
Postgres, no Redis: the loader reads one JSON file, and the endpoint runs
over stubbed app.state without the lifespan (as in test_health.py)."""

import copy
import json
import logging
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

import econ_calendar
import main
from econ_calendar import CalendarUnavailable

ET = ZoneInfo("America/New_York")

# Spec 3.5 decision 8's table, copied (docs/ is not mounted in the twin).
SPEC_TABLE = [
    ("2026-07-02", "08:30", "jobs", "June 2026"),
    ("2026-07-14", "08:30", "cpi", "June 2026"),
    ("2026-07-29", "14:00", "fomc", "meeting Jul 28–29"),
    ("2026-08-07", "08:30", "jobs", "July 2026"),
    ("2026-08-12", "08:30", "cpi", "July 2026"),
    ("2026-09-04", "08:30", "jobs", "August 2026"),
    ("2026-09-11", "08:30", "cpi", "August 2026"),
    ("2026-09-16", "14:00", "fomc", "meeting Sep 15–16, with projections"),
    ("2026-10-02", "08:30", "jobs", "September 2026"),
    ("2026-10-14", "08:30", "cpi", "September 2026"),
    ("2026-10-28", "14:00", "fomc", "meeting Oct 27–28"),
    ("2026-11-06", "08:30", "jobs", "October 2026"),
    ("2026-11-10", "08:30", "cpi", "October 2026"),
    ("2026-12-04", "08:30", "jobs", "November 2026"),
    ("2026-12-09", "14:00", "fomc", "meeting Dec 8–9, with projections"),
    ("2026-12-10", "08:30", "cpi", "November 2026"),
]
SOURCES = {
    "fomc": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
    "cpi": "https://www.bls.gov/schedule/news_release/cpi.htm",
    "jobs": "https://www.bls.gov/schedule/news_release/empsit.htm",
}
VALID = {
    "coversFrom": "2026-07-01",
    "coversThrough": "2026-12-31",
    "timezone": "America/New_York",
    "retrieved": "2026-09-10",
    "sources": SOURCES,
    "events": [
        {"date": "2026-09-16", "time": "14:00", "type": "fomc", "title": "FOMC rate decision", "detail": "d"},
        {"date": "2026-09-11", "time": "08:30", "type": "cpi", "title": "Consumer Price Index", "detail": "d"},
    ],
}
TODAY = date(2026, 9, 10)


@pytest.fixture(autouse=True)
def fresh_cache():
    econ_calendar._cache.clear()
    econ_calendar._last_error.clear()
    yield
    econ_calendar._cache.clear()
    econ_calendar._last_error.clear()


def _et(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


@pytest.fixture
def client(monkeypatch):
    """TestClient over stubbed state, the clock frozen at `now` (ET wall time)."""
    def _make(now, *, path=None):
        monkeypatch.setattr(main, "_now", lambda: now.astimezone(timezone.utc))
        if path is not None:
            monkeypatch.setattr(econ_calendar, "CALENDAR_PATH", path)
        main.app.state.db_pool = None
        main.app.state.redis = None
        return TestClient(main.app)
    yield _make
    main.app.state.db_pool = None
    main.app.state.redis = None


def _write(tmp_path, body, name="cal.json"):
    path = tmp_path / name
    path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    return path


def _mutated(change):
    body = copy.deepcopy(VALID)
    change(body)
    return body


def test_shipped_calendar_matches_spec_table():
    calendar = econ_calendar.load(today=TODAY)
    assert calendar["coversFrom"] == date(2026, 7, 1)
    assert calendar["coversThrough"] == date(2026, 12, 31)
    assert calendar["retrieved"] == date(2026, 9, 10)
    assert calendar["sources"] == SOURCES
    rows = [(e["date"].isoformat(), e["time"], e["type"], e["detail"]) for e in calendar["events"]]
    assert rows == SPEC_TABLE
    titles = {e["type"]: e["title"] for e in calendar["events"]}
    assert titles == {"jobs": "Employment Situation", "cpi": "Consumer Price Index",
                      "fomc": "FOMC rate decision"}


# ── Part 3.4c decision 7: the `event` type ───────────────────────

def _with_events(*rows):
    body = copy.deepcopy(VALID)
    body["events"].extend(rows)
    return body


def _event(day="2026-09-20", time="14:00", title="EU tariff deadline"):
    return {"date": day, "time": time, "type": "event", "title": title, "detail": "hand-added"}


def test_event_type_is_accepted_on_a_weekend(tmp_path):
    """A Sunday row is the point: it is when the market cannot react."""
    calendar = econ_calendar.load(_write(tmp_path, _with_events(_event())), today=TODAY)
    row = [e for e in calendar["events"] if e["type"] == "event"]
    assert len(row) == 1 and row[0]["date"] == date(2026, 9, 20)
    assert date(2026, 9, 20).weekday() == 6


def test_two_events_can_share_a_date_at_different_times(tmp_path):
    """The duplicate rule is (date, time, type): two situations, one Sunday."""
    body = _with_events(_event(time="09:00", title="OPEC meeting"),
                        _event(time="21:00", title="Ceasefire deadline"))
    calendar = econ_calendar.load(_write(tmp_path, body), today=TODAY)
    events = [e["title"] for e in calendar["events"] if e["type"] == "event"]
    assert events == ["OPEC meeting", "Ceasefire deadline"]      # sorted by time


def test_two_events_at_the_same_time_are_still_a_duplicate(tmp_path):
    body = _with_events(_event(title="A"), _event(title="B"))
    with pytest.raises(CalendarUnavailable, match="duplicate event on 2026-09-20 at 14:00"):
        econ_calendar.load(_write(tmp_path, body), today=TODAY)


def test_sources_still_cover_only_the_three_scheduled_types(tmp_path):
    """`event` rows have no upstream page, so `sources` must not want one."""
    assert econ_calendar.SOURCE_TYPES == ("fomc", "cpi", "jobs")
    assert econ_calendar.EVENT_TYPES == ("fomc", "cpi", "jobs", "event")
    body = _with_events(_event())
    body["sources"]["event"] = "https://example.com"
    with pytest.raises(CalendarUnavailable, match=r"sources: missing \[\], unexpected \['event'\]"):
        econ_calendar.load(_write(tmp_path, body), today=TODAY)


def test_renewal_message_names_only_the_scheduled_sources(tmp_path):
    calendar = econ_calendar.load(_write(tmp_path, _with_events(_event())), today=TODAY)
    message = econ_calendar.renewal_message(calendar)
    assert "federalreserve.gov" in message and "bls.gov" in message
    assert message.count("https://") == 3


def test_event_rows_reach_the_window_unchanged(tmp_path):
    """window() is untouched by decision 7: an event row flows through it
    like any other, in time order, with the same keys."""
    calendar = econ_calendar.load(_write(tmp_path, _with_events(_event())), today=TODAY)
    body = econ_calendar.window(calendar, _et(2026, 9, 18, 15, 30).astimezone(timezone.utc), 7)
    row = [e for e in body["events"] if e["type"] == "event"]
    assert len(row) == 1
    assert set(row[0]) == {"date", "time", "datetimeUtc", "type", "title", "detail", "released"}
    assert row[0]["released"] is False and row[0]["datetimeUtc"] == "2026-09-20T18:00:00+00:00"


BAD = [
    ("not-json", "{nope", "not valid JSON"),
    ("not-an-object", [], "calendar: expected an object"),
    ("missing-top-key", _mutated(lambda b: b.pop("coversThrough")), "missing ['coversThrough']"),
    ("extra-top-key", _mutated(lambda b: b.update(note="x")), "unexpected ['note']"),
    ("timezone", _mutated(lambda b: b.update(timezone="UTC")), "timezone"),
    ("covers-inverted", _mutated(lambda b: b.update(coversFrom="2027-01-01")), "coversFrom: after"),
    ("sources-missing-cpi", _mutated(lambda b: b["sources"].pop("cpi")), "sources: missing ['cpi']"),
    ("sources-http", _mutated(lambda b: b["sources"].update(cpi="http://bls.gov")), "sources.cpi"),
    ("events-empty", _mutated(lambda b: b.update(events=[])), "events: expected a non-empty list"),
    ("bad-date", _mutated(lambda b: b["events"][0].update(date="2026-13-01")), "events[0].date: not a real date"),
    ("compact-date", _mutated(lambda b: b["events"][0].update(date="20260916")), "events[0].date: expected YYYY-MM-DD"),
    ("bad-time", _mutated(lambda b: b["events"][0].update(time="8:30")), "events[0].time"),
    ("time-24", _mutated(lambda b: b["events"][0].update(time="24:00")), "events[0].time"),
    ("unknown-type", _mutated(lambda b: b["events"][0].update(type="gdp")), "events[0].type"),
    ("blank-title", _mutated(lambda b: b["events"][0].update(title="  ")), "events[0].title"),
    ("missing-detail", _mutated(lambda b: b["events"][0].pop("detail")), "events[0]: missing ['detail']"),
    ("outside-coverage", _mutated(lambda b: b["events"][0].update(date="2027-01-05")), "outside coverage"),
    # Part 3.4c decision 7 moved the duplicate rule to (date, time, type),
    # so the clash has to share the time now; the 15:00 copy is legal below.
    ("duplicate", _mutated(lambda b: b["events"].append(dict(b["events"][0]))),
     "duplicate fomc on 2026-09-16 at 14:00"),
]


@pytest.mark.parametrize("body, fragment", [(b, f) for _, b, f in BAD], ids=[i for i, _, _ in BAD])
def test_calendar_validation_rejects_bad_events(tmp_path, caplog, body, fragment):
    caplog.set_level(logging.ERROR, logger="econ_calendar")
    path = _write(tmp_path, body)
    with pytest.raises(CalendarUnavailable) as exc:
        econ_calendar.load(path, today=TODAY)
    assert fragment in str(exc.value)
    assert "Econ calendar unavailable" in caplog.text
    assert str(path) not in econ_calendar._cache


def test_calendar_missing_file_raises(tmp_path):
    with pytest.raises(CalendarUnavailable, match="cannot read"):
        econ_calendar.load(tmp_path / "absent.json", today=TODAY)


def test_calendar_loaded_once_per_process(tmp_path):
    path = _write(tmp_path, VALID)
    first = econ_calendar.load(path, today=TODAY)
    assert [e["date"] for e in first["events"]] == [date(2026, 9, 11), date(2026, 9, 16)]  # sorted

    path.write_text("{garbage", encoding="utf-8")        # never re-read once valid
    assert econ_calendar.load(path, today=TODAY) is first

    broken = _write(tmp_path, "{garbage", name="broken.json")
    with pytest.raises(CalendarUnavailable):
        econ_calendar.load(broken, today=TODAY)
    broken.write_text(json.dumps(VALID), encoding="utf-8")   # a failure is not cached
    assert econ_calendar.load(broken, today=TODAY)["coversThrough"] == date(2026, 12, 31)


def test_calendar_renewal_warning_within_14_days(tmp_path, caplog, client):
    caplog.set_level(logging.WARNING, logger="econ_calendar")
    path = _write(tmp_path, VALID)

    calendar = econ_calendar.load(path, today=date(2026, 12, 17))    # 14 days left
    assert "renew by" not in caplog.text
    assert econ_calendar.coverage_short(calendar, date(2026, 12, 17)) is False

    econ_calendar._cache.clear()
    calendar = econ_calendar.load(path, today=date(2026, 12, 18))    # 13 days left
    warning = caplog.text
    assert "covers through 2026-12-31: renew by 2026-12-17" in warning
    assert all(url in warning for url in SOURCES.values())
    assert econ_calendar.coverage_short(calendar, date(2026, 12, 18)) is True

    # Recomputed against a later day, no reload: a process loaded in
    # September still reads short in December.
    econ_calendar._cache.clear()
    caplog.clear()
    calendar = econ_calendar.load(path, today=TODAY)
    assert "renew by" not in caplog.text
    assert econ_calendar.coverage_short(calendar, date(2026, 12, 18)) is True

    # /health recomputes it too, from the same cached load (no reload).
    assert client(_et(2026, 9, 10, 12, 0), path=path).get("/health").json()["calendarCoverageShort"] is False
    assert client(_et(2026, 12, 18, 12, 0), path=path).get("/health").json()["calendarCoverageShort"] is True
    assert econ_calendar._cache[str(path)] is calendar


# ── GET /market/calendar ─────────────────────────────────────────

EVENT_KEYS = {"date", "time", "datetimeUtc", "type", "title", "detail", "released"}


def _types(body):
    return [(e["date"], e["type"]) for e in body["events"]]


def test_calendar_window_seven_days(client):
    resp = client(_et(2026, 9, 10, 12, 0)).get("/market/calendar")
    assert resp.status_code == 200
    body = resp.json()
    assert (body["from"], body["to"], body["coversThrough"], body["coverageShort"]) == (
        "2026-09-10", "2026-09-16", "2026-12-31", False)
    assert _types(body) == [("2026-09-11", "cpi"), ("2026-09-16", "fomc")]
    cpi = body["events"][0]
    assert set(cpi) == EVENT_KEYS
    assert (cpi["time"], cpi["title"], cpi["detail"], cpi["released"]) == (
        "08:30", "Consumer Price Index", "August 2026", False)


def test_calendar_today_events_flag_released(client):
    for now, released in ((_et(2026, 9, 11, 8, 29), False),
                          (_et(2026, 9, 11, 8, 30), True),
                          (_et(2026, 9, 11, 16, 0), True)):
        events = client(now).get("/market/calendar?days=1").json()["events"]
        assert [(e["type"], e["released"]) for e in events] == [("cpi", released)]


def test_calendar_window_uses_eastern_date(client):
    late = _et(2026, 9, 10, 23, 30)
    assert late.astimezone(timezone.utc).date() == date(2026, 9, 11)
    body = client(late).get("/market/calendar?days=1").json()
    assert (body["from"], body["to"], body["events"]) == ("2026-09-10", "2026-09-10", [])

    body = client(_et(2026, 9, 11, 0, 30)).get("/market/calendar?days=1").json()
    assert _types(body) == [("2026-09-11", "cpi")]


def test_calendar_datetime_utc_across_dst(client):
    body = client(_et(2026, 9, 10, 12, 0)).get("/market/calendar?days=31").json()
    assert [(e["type"], e["datetimeUtc"]) for e in body["events"]] == [
        ("cpi", "2026-09-11T12:30:00+00:00"),     # EDT, UTC−4
        ("fomc", "2026-09-16T18:00:00+00:00"),
        ("jobs", "2026-10-02T12:30:00+00:00"),
    ]
    body = client(_et(2026, 11, 9, 12, 0)).get("/market/calendar?days=2").json()
    assert [(e["type"], e["datetimeUtc"]) for e in body["events"]] == [
        ("cpi", "2026-11-10T13:30:00+00:00"),     # EST after 2026-11-01, UTC−5
    ]


def test_calendar_empty_window_is_empty_list(client):
    resp = client(_et(2026, 9, 17, 9, 0)).get("/market/calendar")
    assert resp.status_code == 200
    body = resp.json()
    assert (body["from"], body["to"], body["events"], body["coverageShort"]) == (
        "2026-09-17", "2026-09-23", [], False)


def test_calendar_coverage_short_flagged(client, caplog):
    caplog.set_level(logging.WARNING, logger="econ_calendar")
    resp = client(_et(2026, 12, 8, 9, 0)).get("/market/calendar?days=31")
    assert resp.status_code == 200
    body = resp.json()
    assert (body["to"], body["coverageShort"]) == ("2027-01-07", True)
    assert _types(body) == [("2026-12-09", "fomc"), ("2026-12-10", "cpi")]
    assert "runs past coverage (2026-12-31)" in caplog.text

    resp = client(_et(2027, 1, 10, 9, 0)).get("/market/calendar")   # wholly past coverage
    assert resp.status_code == 200
    assert (resp.json()["events"], resp.json()["coverageShort"]) == ([], True)


def test_calendar_days_validation(client):
    c = client(_et(2026, 9, 10, 12, 0))
    for bad in ("0", "32", "x", "-1"):
        assert c.get(f"/market/calendar?days={bad}").status_code == 422
    assert c.get("/market/calendar?days=1").json()["to"] == "2026-09-10"
    assert c.get("/market/calendar?days=31").json()["to"] == "2026-10-10"
    assert c.get("/market/calendar").json()["to"] == "2026-09-16"


def test_calendar_missing_or_invalid_file_503(client, tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="econ_calendar")
    now = _et(2026, 9, 10, 12, 0)
    c = client(now, path=tmp_path / "absent.json")
    resp = c.get("/market/calendar")
    assert resp.status_code == 503
    assert resp.json() == {"detail": "calendar unavailable"}
    health = c.get("/health")
    assert health.status_code == 200
    assert (health.json()["calendarCoversThrough"], health.json()["calendarCoverageShort"]) == (None, None)
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1          # a broken file read on every healthcheck logs once

    broken = tmp_path / "broken.json"
    broken.write_text("{nope", encoding="utf-8")
    assert client(now, path=broken).get("/market/calendar").status_code == 503
    broken.write_text(json.dumps(VALID), encoding="utf-8")        # fixed: picked up, no restart
    assert client(now, path=broken).get("/market/calendar").status_code == 200


def test_calendar_needs_no_dependencies(client):
    class Exploding:
        def __getattr__(self, name):
            raise AssertionError(f"/market/calendar touched a dependency: {name}")

    c = client(_et(2026, 9, 10, 12, 0))
    main.app.state.db_pool = Exploding()
    main.app.state.redis = Exploding()
    resp = c.get("/market/calendar")
    assert resp.status_code == 200
    assert _types(resp.json()) == [("2026-09-11", "cpi"), ("2026-09-16", "fomc")]


def test_health_reports_calendar_coverage(client):
    c = client(_et(2026, 9, 10, 12, 0))
    body = c.get("/health").json()
    assert (body["calendarCoversThrough"], body["calendarCoverageShort"]) == ("2026-12-31", False)
    assert client(_et(2026, 12, 18, 12, 0)).get("/health").json()["calendarCoverageShort"] is True
    assert "GET  /market/calendar?days=7" in c.get("/").json()["endpoints"]
