"""
TradingFirm — headlines into events (Part 4.4).

The classifier labels each headline with an `eventKey`; this module groups a
ticker's labelled headlines by that key, so the verdict prompt reads one line
per *event* with a source count, instead of reading the same story from
Reuters, CNBC and Bloomberg as three separate reasons (decisions 2026-09-20,
"the headline digest does not detect cross-source duplicates").

Pure: no I/O. A label without a key, or a headline nobody could label, is its
own event, keyed by its digest — never dropped, never merged by guesswork.
"""

from typing import Optional

import cache
import classifier

MAX_EVENTS = 15
TEXT_MAX = 300

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


def group(items: list[dict]) -> list[dict]:
    """One event per eventKey, high relevance first, then newest, at most
    MAX_EVENTS. `items` are dossier news items whose `sentiment` is the
    stored or freshly made label, or None."""
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
        else:
            event.update(
                classified=False, relevance=None, category=None, sentiment=None,
                text=sanitize_untrusted(members[0][0].get("headline")),
            )
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
