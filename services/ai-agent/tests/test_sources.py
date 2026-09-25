"""Part 4.8b-ai — the source type: the one map every caller uses. Pure."""

import pytest

import sources


@pytest.mark.parametrize("source, expected", [
    ("SeekingAlpha", "analyst"), ("ChartMill", "analyst"), (" SeekingAlpha ", "analyst"),
    ("Yahoo", "news"), ("Benzinga", "news"), ("CNBC", "news"), ("Reuters", "news"),
])
def test_source_type_map(source, expected):
    assert sources.source_type(source, "Shares rise after the print", "A plain summary.") == expected
    assert sources.ANALYST_SOURCES == {"SeekingAlpha", "ChartMill"}


@pytest.mark.parametrize("title, summary", [
    ("Zacks: is AAL a buy?", None),
    ("Airline outlook", "Zacks Investment Research rates the stock a hold."),
    ("3 Stocks The Motley Fool Likes", ""),
    ("Weekly roundup", "Read more at fool.com today"),
    ("MOTLEY FOOL: what to watch", None),
])
def test_source_type_catches_syndicated_zacks(title, summary):
    """Zacks and Motley Fool arrive under `Yahoo` (spec 4.8b X7): the text
    says so, the source name never does."""
    assert sources.source_type("Yahoo", title, summary) == "analyst"


@pytest.mark.parametrize("source, title, summary", [
    (None, "Airline raises guidance", None),
    ("", "Airline raises guidance", ""),
    (123, "Airline raises guidance", None),
    ("Yahoo", "Zacksville plant opens", "A foolish idea, said the CEO"),   # no whole-word match
    ("Yahoo", None, None),
])
def test_source_type_unknown_is_news(source, title, summary):
    assert sources.source_type(source, title, summary) == "news"
