"""
TradingFirm — headlines into events (Part 4.4).

The classifier labels each headline with an `eventKey`; this module groups a
ticker's labelled headlines by that key, so the verdict prompt reads one line
per *event* with a source count, instead of reading the same story from
Reuters, CNBC and Bloomberg as three separate reasons (decisions 2026-09-20,
"the headline digest does not detect cross-source duplicates").

Pure: no I/O. A label without a key, or a headline nobody could label, is its
own event, keyed by its digest — never dropped, never merged by guesswork.

Part 4.8b-ai (spec 4.8b decisions 10–11): every event carries its age
(`ageDays`, from the classifier's `eventDate` when a headline stated one,
else from `firstSeen`), `stale` (older than 14 days), `rehash` (a retelling:
data-engine's `rehashOf`, or an event date more than 14 days before the
story was first seen), `sourceType` (commentary vs news) and `eventDate`. A
stale or rehashed event is capped at `low` relevance, an analyst piece
without a new fact at `medium`; a capped event never enters the fingerprint's
high keys. Nothing is dropped: the model reads "old news, retold" as
information. Before all that, `prefilter` cuts the dossier's headlines to 15
in code, so the classifier and the verdict see the fresh ones.
"""

from datetime import date, datetime, timedelta
from typing import Optional

import cache
import classifier
import sources

MAX_EVENTS = 15
TEXT_MAX = 300

# 4.8b-ai
HEADLINES_MAX = 15          # the pre-filter's cut (spec 4.8b decision 11)
STALE_DAYS = 14             # ageDays above this → stale
REHASH_DAYS = 14            # eventDate more than this before firstSeen → rehash
NEW_FACT_GRACE_DAYS = 3     # an eventDate on or after firstSeen − this is a new fact
RELEVANCE_CAP_OLD = "low"
RELEVANCE_CAP_ANALYST = "medium"

_RELEVANCE_RANK = {"high": 0, "medium": 1, "low": 2, None: 3}


def sanitize_untrusted(text, limit: int = TEXT_MAX) -> str:
    """Headline-derived text on its way into a prompt: control characters
    out, whitespace collapsed, bounded. It stays untrusted after this — the
    prompt's data block is what contains it (analyze.user_prompt)."""
    if not isinstance(text, str):
        return ""
    cleaned = "".join(ch if ch.isprintable() else " " for ch in text)
    return " ".join(cleaned.split())[:limit]


def _label(item: dict) -> Optional[dict]:
    label = item.get("sentiment")
    if not isinstance(label, dict):
        return None
    if label.get("relevance") not in classifier.RELEVANCE:
        return None
    return label


def _iso_date(value) -> Optional[date]:
    """A date from `YYYY-MM-DD` or an ISO datetime string (`Z` allowed);
    None for anything else. Tolerant on purpose: stored labels are data."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def event_age_days(event_date, first_seen, today: Optional[date]) -> Optional[int]:
    """The one age rule (spec 4.8b-ai decision 11): today minus the event's
    own date when the classifier found one, else minus the day the story
    was first seen; negative for a scheduled event; None without `today`
    or without either date."""
    if today is None:
        return None
    anchor = _iso_date(event_date) or _iso_date(first_seen)
    return None if anchor is None else (today - anchor).days


def _cap(relevance: Optional[str], cap: str) -> Optional[str]:
    if relevance is None:
        return None
    return cap if _RELEVANCE_RANK[relevance] < _RELEVANCE_RANK[cap] else relevance


def prefilter(items: list[dict]) -> tuple[list[dict], int]:
    """The dossier's headlines cut to HEADLINES_MAX before the classifier
    (spec 4.8b decision 11): every item carrying `rehashOf` goes first, then
    the rest are ordered by (not a question, not analyst commentary, newest
    first) and the first 15 kept. Returns (kept, how many were cut)."""
    dicts = [i for i in items if isinstance(i, dict)]
    fresh = [i for i in dicts if not isinstance(i.get("rehashOf"), dict)]
    fresh.sort(key=lambda i: str(i.get("publishedAt") or ""), reverse=True)
    fresh.sort(key=lambda i: (
        str(i.get("headline") or "").rstrip().endswith("?"),
        sources.source_type(i.get("source"), i.get("headline"), i.get("summary")) == sources.ANALYST,
    ))
    kept = fresh[:HEADLINES_MAX]
    return kept, len(dicts) - len(kept)


def group(items: list[dict], today: Optional[date] = None) -> list[dict]:
    """One event per eventKey, high relevance first, then newest, at most
    MAX_EVENTS. `items` are dossier news items whose `sentiment` is the
    stored or freshly made label, or None. `today` (4.8b-ai) is what the
    age reads; without it `ageDays` is null and nothing is stale."""
    buckets: dict[str, list[tuple[dict, Optional[dict]]]] = {}
    for item in items:
        title = item.get("headline") or ""
        if not title.strip():
            continue
        label = _label(item)
        key = label.get("eventKey") if label else None
        if not classifier.valid_event_key(key):
            key = "unkeyed-" + cache.headline_digest(title, item.get("url"))[:12]
        buckets.setdefault(key, []).append((item, label))

    events = []
    for key, members in buckets.items():
        labelled = [(i, l) for i, l in members if l is not None]
        dates = sorted(str(i.get("publishedAt")) for i, _ in members if i.get("publishedAt"))
        event = {
            "eventKey": key,
            "firstSeen": dates[0] if dates else None,
            "lastSeen": dates[-1] if dates else None,
            "sources": len(members),
        }
        event_date = None
        if labelled:
            lead_item, lead = min(labelled, key=lambda m: _RELEVANCE_RANK[m[1]["relevance"]])
            scores = [l["sentiment"] for _, l in labelled
                      if isinstance(l.get("sentiment"), (int, float))]
            event.update(
                classified=True,
                relevance=lead["relevance"],
                category=lead.get("category"),
                sentiment=round(sum(scores) / len(scores), 2) if scores else None,
                text=sanitize_untrusted(lead.get("oneLine")),
            )
            # the lead label's date, else the first stated one by publication
            dated = [l.get("eventDate") for _, l in sorted(labelled, key=lambda m: str(m[0].get("publishedAt") or ""))
                     if _iso_date(l.get("eventDate")) is not None]
            picked = lead.get("eventDate") if _iso_date(lead.get("eventDate")) is not None else (dated[0] if dated else None)
            event_date = _iso_date(picked).isoformat() if picked is not None else None
        else:
            event.update(
                classified=False, relevance=None, category=None, sentiment=None,
                text=sanitize_untrusted(members[0][0].get("headline")),
            )

        # 4.8b-ai: age, staleness, retelling, source type, and the caps
        first_seen = _iso_date(event["firstSeen"])
        event_day = _iso_date(event_date)
        age = event_age_days(event_date, event["firstSeen"], today)
        rehash = any(isinstance(i.get("rehashOf"), dict) for i, _ in members) or (
            event_day is not None and first_seen is not None
            and (first_seen - event_day).days > REHASH_DAYS)
        stale = age is not None and age > STALE_DAYS
        types = {sources.source_type(i.get("source"), i.get("headline"), i.get("summary")) for i, _ in members}
        source_type = sources.ANALYST if types == {sources.ANALYST} else sources.NEWS
        new_fact = (event_day is not None and first_seen is not None
                    and event_day >= first_seen - timedelta(days=NEW_FACT_GRACE_DAYS))
        relevance = event["relevance"]
        if rehash or stale:
            relevance = _cap(relevance, RELEVANCE_CAP_OLD)
        elif source_type == sources.ANALYST and not new_fact:
            relevance = _cap(relevance, RELEVANCE_CAP_ANALYST)
        event.update(relevance=relevance, eventDate=event_date, ageDays=age, stale=stale,
                     rehash=rehash, sourceType=source_type)
        events.append(event)

    events.sort(key=lambda e: e["lastSeen"] or "", reverse=True)
    events.sort(key=lambda e: _RELEVANCE_RANK[e["relevance"]])
    return events[:MAX_EVENTS]


def high_relevance_keys(events: list[dict]) -> list[str]:
    """What the verdict cache's fingerprint reads: a new one of these forces
    a new verdict, a medium or low headline does not."""
    return sorted(e["eventKey"] for e in events if e["relevance"] == "high")


def known_keys(items: list[dict]) -> list[str]:
    """Keys already on the ticker's labelled headlines, offered to the
    classifier for reuse."""
    seen: list[str] = []
    for item in items:
        label = _label(item)
        key = label.get("eventKey") if label else None
        if classifier.valid_event_key(key) and key not in seen:
            seen.append(key)
    return seen[:classifier.KNOWN_KEYS_MAX]
