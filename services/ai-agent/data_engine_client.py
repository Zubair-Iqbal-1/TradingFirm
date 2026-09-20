"""
TradingFirm — the data-engine write-back client (Part 4.2, spec decisions 6,
6b and 10).

One POST {DATA_ENGINE_URL}/news/{id}/sentiment per classified headline. No
retries, no cooldown, no backoff: the caller's cadence bounds the calls, and
data-engine has no auth.

Write-back is **fail-open and never raises at the route**: the classification
is already paid for and already cached, so a failure here is counted and
logged, and the answer still goes back to the caller. What stops fail-open
from becoming a permanent hole is that every item with an id is written back
on *every* call, cached or fresh (decision 6b) — so the next call rewrites
whatever this one failed to write.

  answer                          outcome        log
  200                             written        DEBUG
  404 (no such row)               counted        WARNING — the id is stale
  422 (our contract drifted)      counted        ERROR — the two copies differ
  other 4xx / 5xx / 3xx           counted        WARNING, the status
  transport error, timeout        counted        WARNING, the exception type

Messages carry a status, an id or an exception TYPE — never a body, never a
URL, never a key (G14).
"""

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

SENTIMENT_PATH = "/news/{news_id}/sentiment"


class WriteBackResult:
    """What happened to one write. `ok` is the only thing the route counts;
    `reason` is for the log and the test."""

    __slots__ = ("news_id", "ok", "reason")

    def __init__(self, news_id: int, ok: bool, reason: Optional[str] = None):
        self.news_id = news_id
        self.ok = ok
        self.reason = reason

    def __repr__(self) -> str:
        return f"WriteBackResult(id={self.news_id}, ok={self.ok}, reason={self.reason!r})"


def sentiment_url(base_url: str, news_id: int) -> str:
    """The one place the write-back URL is spelled."""
    return f"{base_url.rstrip('/')}{SENTIMENT_PATH.format(news_id=news_id)}"


async def write_sentiment(
    http: Optional[httpx.AsyncClient],
    base_url: str,
    news_id: int,
    classification: dict[str, Any],
) -> WriteBackResult:
    """Store one classification on data-engine. Never raises."""
    if http is None:
        logger.warning(f"write-back skipped for news {news_id}: no HTTP client")
        return WriteBackResult(news_id, False, "no client")

    url = sentiment_url(base_url, news_id)
    try:
        resp = await http.post(url, json=classification)
    except httpx.TimeoutException:
        logger.warning(f"write-back for news {news_id} timed out")
        return WriteBackResult(news_id, False, "timeout")
    except httpx.HTTPError as e:
        logger.warning(f"write-back for news {news_id} unreachable ({type(e).__name__})")
        return WriteBackResult(news_id, False, type(e).__name__)
    except OSError as e:
        logger.warning(f"write-back for news {news_id} failed ({type(e).__name__})")
        return WriteBackResult(news_id, False, type(e).__name__)

    if resp.status_code == 200:
        logger.debug(f"write-back stored for news {news_id}")
        return WriteBackResult(news_id, True)
    if resp.status_code == 404:
        logger.warning(f"write-back for news {news_id}: no such row (404)")
        return WriteBackResult(news_id, False, "404")
    if resp.status_code == 422:
        logger.error(
            f"write-back for news {news_id}: data-engine refused the body (422). "
            "classifier.ITEM_LIMITS and data-engine's NewsSentimentRequest have "
            "drifted (spec 4.2 decision 10)."
        )
        return WriteBackResult(news_id, False, "422")
    logger.warning(f"write-back for news {news_id}: HTTP {resp.status_code}")
    return WriteBackResult(news_id, False, f"HTTP {resp.status_code}")
