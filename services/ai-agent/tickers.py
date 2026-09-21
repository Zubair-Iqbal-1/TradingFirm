"""
TradingFirm — the one ticker normalization in ai-agent (Part 4.4, G1.5).

data-engine's rule (services/data-engine/tickers.py), copied because the
services share no Python: upper-cased, stripped, 1–5 letters and nothing
else. Every Redis key, every row and every outgoing URL that carries a ticker
goes through validate_ticker first, so a bad one never reaches a key or a
path. test_ticker_rule_pinned_to_data_engine holds the copy to the original.
"""


def normalize_ticker(ticker: str) -> str:
    return ticker.upper().strip()


def validate_ticker(ticker: str) -> str:
    """normalize_ticker() plus the shape check. Raises ValueError."""
    if not isinstance(ticker, str):
        raise ValueError("ticker must be a string")
    t = normalize_ticker(ticker)
    if not t.isalpha() or not t.isascii() or not 1 <= len(t) <= 5:
        raise ValueError(f"ticker must be 1-5 letters, got {ticker!r}")
    return t
