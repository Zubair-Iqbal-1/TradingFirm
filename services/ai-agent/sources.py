"""
TradingFirm — a headline's source type (Part 4.8b-ai, spec 4.8b decision 10
layer 2). Pure, no LLM.

`analyst` is commentary: a rating, a target, a "should you buy" piece. It is
read from the source name (SeekingAlpha, ChartMill) or, because Zacks and
Motley Fool arrive syndicated under `Yahoo` (spec 4.8b X7: Finnhub reports
only five source values, every url host is finnhub.io), from the title or
summary text. Everything else, an unknown source and a missing one, is
`news`. This is the one normalization function for the type: events.group,
the pre-filter and the knob script all call it (G1.5).
"""

import re

ANALYST = "analyst"
NEWS = "news"

ANALYST_SOURCES = frozenset({"SeekingAlpha", "ChartMill"})
_ANALYST_TEXT = re.compile(r"\bzacks\b|\bmotley fool\b|\bfool\.com\b", re.IGNORECASE)


def source_type(source, title, summary) -> str:
    if isinstance(source, str) and source.strip() in ANALYST_SOURCES:
        return ANALYST
    for text in (title, summary):
        if isinstance(text, str) and _ANALYST_TEXT.search(text):
            return ANALYST
    return NEWS
