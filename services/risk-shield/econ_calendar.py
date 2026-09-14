"""
TradingFirm — the hand-maintained economic calendar (Part 3.5 decision 8).

data/econ_calendar.json lists FOMC decisions, CPI releases and jobs reports
for a stated coverage window, copied by hand from the Fed and BLS pages,
plus free-text `event` rows (Part 3.4c decision 7) for anything else that
lands while the market is shut: a tariff deadline, a summit, a vote. It
is the only calendar source: nothing here calls an API (Finnhub's
/calendar/economic is likely premium, plan §2).

load() validates the file once per process and caches it only when it is
valid. A missing or invalid file raises CalendarUnavailable every time it is
asked for, so a fixed file is picked up without a restart. The ERROR is
logged once per distinct problem, then DEBUG: /health reads the calendar on
every Docker healthcheck (every 10 s).

Renewal: the file is short when coversThrough − today (ET) < 14 days. load()
warns about that once, at load. coverage_short(calendar, today) recomputes
it for /health and the news poller, because a long-lived process loaded the
file weeks before it runs short.
"""

import json
import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
CALENDAR_PATH = Path(__file__).parent / "data" / "econ_calendar.json"
TIMEZONE = "America/New_York"
# fomc / cpi / jobs are the scheduled releases the file is renewed from.
# `event` (Part 3.4c decision 7) is a hand-added free-text row — a tariff
# deadline, a summit, a vote — allowed on any date, weekends included,
# because that is exactly when a weekend-exposure read needs one. It has
# no upstream page, so `sources` still covers the three scheduled types.
EVENT_TYPES = ("fomc", "cpi", "jobs", "event")
SOURCE_TYPES = ("fomc", "cpi", "jobs")
RENEWAL_DAYS = 14

_TOP_KEYS = {"coversFrom", "coversThrough", "timezone", "retrieved", "sources", "events"}
_EVENT_KEYS = {"date", "time", "type", "title", "detail"}
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class CalendarUnavailable(RuntimeError):
    """The calendar file is missing, unreadable or fails validation."""


# Valid calendars only, keyed by path. A failure is never cached.
_cache: dict[str, dict] = {}
# The last problem logged at ERROR per path, so a broken file read on every
# healthcheck logs once, not every 10 s.
_last_error: dict[str, str] = {}


def et_today(now: Optional[datetime] = None) -> date:
    return (now or datetime.now(ET)).astimezone(ET).date()


def _date(value: Any, where: str) -> date:
    if not isinstance(value, str) or not _DATE.match(value):
        raise CalendarUnavailable(f"{where}: expected YYYY-MM-DD, got {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise CalendarUnavailable(f"{where}: not a real date: {value!r}") from None


def _keys(obj: Any, expected: set, where: str) -> None:
    if not isinstance(obj, dict):
        raise CalendarUnavailable(f"{where}: expected an object")
    missing, extra = expected - obj.keys(), obj.keys() - expected
    if missing or extra:
        raise CalendarUnavailable(f"{where}: missing {sorted(missing)}, unexpected {sorted(extra)}")


def validate(raw: Any) -> dict:
    """The parsed file → a calendar with real dates and events sorted by
    (date, time, type). Raises CalendarUnavailable naming the problem."""
    _keys(raw, _TOP_KEYS, "calendar")
    if raw["timezone"] != TIMEZONE:
        raise CalendarUnavailable(f"timezone: only {TIMEZONE} is supported, got {raw['timezone']!r}")
    covers_from = _date(raw["coversFrom"], "coversFrom")
    covers_through = _date(raw["coversThrough"], "coversThrough")
    if covers_from > covers_through:
        raise CalendarUnavailable("coversFrom: after coversThrough")
    retrieved = _date(raw["retrieved"], "retrieved")

    sources = raw["sources"]
    _keys(sources, set(SOURCE_TYPES), "sources")
    for kind, url in sources.items():
        if not isinstance(url, str) or not url.startswith("https://"):
            raise CalendarUnavailable(f"sources.{kind}: expected an https URL")

    if not isinstance(raw["events"], list) or not raw["events"]:
        raise CalendarUnavailable("events: expected a non-empty list")
    events, seen = [], set()
    for i, item in enumerate(raw["events"]):
        where = f"events[{i}]"
        _keys(item, _EVENT_KEYS, where)
        day = _date(item["date"], f"{where}.date")
        if not isinstance(item["time"], str) or not _TIME.match(item["time"]):
            raise CalendarUnavailable(f"{where}.time: expected HH:MM, got {item['time']!r}")
        if item["type"] not in EVENT_TYPES:
            raise CalendarUnavailable(f"{where}.type: expected one of {EVENT_TYPES}, got {item['type']!r}")
        if not isinstance(item["title"], str) or not item["title"].strip():
            raise CalendarUnavailable(f"{where}.title: expected a non-blank string")
        if not isinstance(item["detail"], str):
            raise CalendarUnavailable(f"{where}.detail: expected a string")
        if not covers_from <= day <= covers_through:
            raise CalendarUnavailable(f"{where}.date: {day} is outside coverage {covers_from}…{covers_through}")
        # (date, time, type), not (date, type): two `event` rows can share a
        # Sunday (3.4c decision 7), while a second 14:00 FOMC stays a mistake.
        if (day, item["time"], item["type"]) in seen:
            raise CalendarUnavailable(
                f"{where}: duplicate {item['type']} on {day} at {item['time']}")
        seen.add((day, item["time"], item["type"]))
        events.append({"date": day, "time": item["time"], "type": item["type"],
                       "title": item["title"].strip(), "detail": item["detail"]})

    events.sort(key=lambda e: (e["date"], e["time"], e["type"]))
    return {"coversFrom": covers_from, "coversThrough": covers_through, "retrieved": retrieved,
            "sources": dict(sources), "events": events}


def coverage_short(calendar: dict, today: date) -> bool:
    """True when fewer than RENEWAL_DAYS days of coverage remain."""
    return (calendar["coversThrough"] - today).days < RENEWAL_DAYS


def renewal_message(calendar: dict) -> str:
    renew_by = calendar["coversThrough"] - timedelta(days=RENEWAL_DAYS)
    urls = ", ".join(calendar["sources"][kind] for kind in SOURCE_TYPES)
    return (f"Econ calendar covers through {calendar['coversThrough']}: renew by {renew_by} "
            f"from {urls} (CLAUDE.md, calendar renewal)")


def load(path: Optional[Path] = None, *, today: Optional[date] = None) -> dict:
    """The validated calendar at `path` (default data/econ_calendar.json),
    cached per process once valid. Raises CalendarUnavailable (ERROR)."""
    path = Path(path or CALENDAR_PATH)
    cached = _cache.get(str(path))
    if cached is not None:
        return cached
    try:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            raise CalendarUnavailable(f"{path.name}: cannot read ({type(e).__name__})") from None
        try:
            raw = json.loads(text)
        except ValueError:
            raise CalendarUnavailable(f"{path.name}: not valid JSON") from None
        calendar = validate(raw)
    except CalendarUnavailable as e:
        if _last_error.get(str(path)) != str(e):
            logger.error(f"Econ calendar unavailable: {e}")
            _last_error[str(path)] = str(e)
        else:
            logger.debug(f"Econ calendar still unavailable: {e}")
        raise

    _last_error.pop(str(path), None)
    _cache[str(path)] = calendar
    logger.info(
        f"Econ calendar loaded: {len(calendar['events'])} events, "
        f"{calendar['coversFrom']} … {calendar['coversThrough']}"
    )
    if coverage_short(calendar, today or et_today()):
        logger.warning(renewal_message(calendar))
    return calendar


# ── GET /market/calendar (decision 9) ────────────────────────────

def event_at(event: dict) -> datetime:
    """An event's ET date + HH:MM as an aware UTC datetime (DST from zoneinfo)."""
    hours, minutes = (int(part) for part in event["time"].split(":"))
    return datetime.combine(event["date"], time(hours, minutes), tzinfo=ET).astimezone(timezone.utc)


def window(calendar: dict, now: datetime, days: int) -> dict:
    """
    The endpoint body: events on ET dates today … today + days − 1, in time
    order. Today's events stay after release, flagged `released`. A window
    past the file's coverage answers what the file has, `coverageShort: true`
    and a WARNING — never an error.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    start = et_today(now)
    end = start + timedelta(days=days - 1)
    events = []
    for event in calendar["events"]:          # already in time order (validate)
        if start <= event["date"] <= end:
            at = event_at(event)
            events.append({
                "date": event["date"].isoformat(),
                "time": event["time"],
                "datetimeUtc": at.isoformat(),
                "type": event["type"],
                "title": event["title"],
                "detail": event["detail"],
                "released": at <= now,
            })
    short = end > calendar["coversThrough"]
    if short:
        logger.warning(
            f"Econ calendar window {start} … {end} runs past coverage ({calendar['coversThrough']})"
        )
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "coversThrough": calendar["coversThrough"].isoformat(),
        "coverageShort": short,
        "events": events,
    }
