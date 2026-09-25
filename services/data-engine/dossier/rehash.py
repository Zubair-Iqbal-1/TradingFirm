"""
TradingFirm — rehash layer 1: a headline retelling a stored one (Part
4.8b-de, spec docs/specs/4.8b.md decision 10).

A dossier headline whose title shares most of its words with a headline this
ticker already had more than 14 days ago is marked `rehashOf`: the story may
be old news, retold (RIOT's "$9.1 Billion AI Deal", 2026-09-16, is the case
that raised it). Layers 2 (source type) and 3 (the classifier's `eventDate`)
live in ai-agent; this layer needs the stored history, so it lives here.

`news_tokens` is the one normalization function (G1.5): every comparison
goes through it. Pure; the database read is `db.get_news_titles`.
The two thresholds are starting lines, provisional like every 4.8b line.
"""

import re
from typing import Optional

REHASH_MIN_AGE_DAYS = 14        # a stored title must be older than this
REHASH_LOOKBACK_DAYS = 180      # and no older than this
REHASH_MIN_JACCARD = 0.5
REHASH_MIN_SHARED = 4

_WORD = re.compile(r"[a-z0-9]+")

# Function words and headline filler that say nothing about the story.
STOPWORDS = frozenset("""
a about after again against all also an and any are as at be been before being
but by can could did do does doing down during each for from further had has
have having he her here his how i if in into is it its just may more most my
new no nor not now of off on once only or other our out over own same she
should so some such than that the their them then there these they this those
through to too under until up very was we were what when where which while who
whom why will with would you your inc corp co ltd plc stock stocks shares share
says said report reports today week year
""".split())


def news_tokens(text: Optional[str], ticker: Optional[str] = None) -> frozenset:
    """Lowercase alphanumeric words of `text`, without stopwords, without
    words of one or two characters, and without the ticker itself."""
    if not isinstance(text, str):
        return frozenset()
    own = (ticker or "").lower()
    return frozenset(w for w in _WORD.findall(text.lower())
                     if len(w) > 2 and w not in STOPWORDS and w != own)


def best_match(title: str, ticker: str, stored: list[dict]) -> Optional[dict]:
    """The stored row (`{id, published_at, title}`) this title retells best,
    as `{id, published_at, overlap_frac}`, or None under the thresholds."""
    words = news_tokens(title, ticker)
    if not words:
        return None
    best = None
    for row in stored:
        other = news_tokens(row.get("title"), ticker)
        shared = len(words & other)
        if shared < REHASH_MIN_SHARED:
            continue
        jaccard = shared / len(words | other)
        if jaccard >= REHASH_MIN_JACCARD and (best is None or jaccard > best["overlap_frac"]):
            best = {"id": row["id"], "published_at": row["published_at"], "overlap_frac": jaccard}
    return best
