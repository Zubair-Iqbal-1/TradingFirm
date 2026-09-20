"""
Part 4.2 — the data-engine write-back client.

httpx.MockTransport, never respx: our own client is httpx==0.28.1, so we mock
the transport of the client actually in use (spec decision 15). The provider
tests mock httpx2's transport for exactly the same reason — the openai SDK
runs on httpx2. respx patches httpx and cannot see the SDK, which is why it is
absent from requirements-dev.txt.
"""

import httpx
import pytest

import data_engine_client as dec

BASE = "http://data-engine:8001"
CLASSIFICATION = {
    "relevance": "high", "sentiment": -0.4, "category": "guidance",
    "oneLine": "Guidance cut.", "model": "anthropic/claude-sonnet-5",
    "classifiedAt": "2026-09-20T18:00:00+00:00",
}


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_sentiment_url_is_spelled_once():
    assert dec.sentiment_url(BASE, 41) == "http://data-engine:8001/news/41/sentiment"
    assert dec.sentiment_url(BASE + "/", 41) == "http://data-engine:8001/news/41/sentiment"


@pytest.mark.asyncio
async def test_write_sentiment_posts_the_classification():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(200, json={"id": 41, "updated": True})

    async with _client(handler) as http:
        out = await dec.write_sentiment(http, BASE, 41, CLASSIFICATION)

    assert out.ok is True and out.reason is None and out.news_id == 41
    assert seen["method"] == "POST"
    assert seen["url"] == "http://data-engine:8001/news/41/sentiment"
    import json as _json
    assert _json.loads(seen["body"]) == CLASSIFICATION


@pytest.mark.asyncio
async def test_writeback_404_is_counted_not_raised():
    async with _client(lambda r: httpx.Response(404, json={"detail": "not found"})) as http:
        out = await dec.write_sentiment(http, BASE, 999, CLASSIFICATION)
    assert out.ok is False and out.reason == "404"


@pytest.mark.asyncio
async def test_writeback_422_logs_error_and_continues(caplog):
    async with _client(lambda r: httpx.Response(422, json={"detail": "bad"})) as http:
        with caplog.at_level("ERROR"):
            out = await dec.write_sentiment(http, BASE, 41, CLASSIFICATION)

    assert out.ok is False and out.reason == "422"
    assert "drifted" in caplog.text, "a 422 means the two contract copies differ"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 409, 429, 500, 502, 503, 301])
async def test_writeback_other_statuses_are_counted(status):
    async with _client(lambda r: httpx.Response(status, json={})) as http:
        out = await dec.write_sentiment(http, BASE, 41, CLASSIFICATION)
    assert out.ok is False and out.reason == f"HTTP {status}"


@pytest.mark.asyncio
async def test_writeback_unreachable_is_counted_not_raised():
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    def connect(request):
        raise httpx.ConnectError("refused", request=request)

    async with _client(timeout) as http:
        out = await dec.write_sentiment(http, BASE, 41, CLASSIFICATION)
    assert out.ok is False and out.reason == "timeout"

    async with _client(connect) as http:
        out = await dec.write_sentiment(http, BASE, 41, CLASSIFICATION)
    assert out.ok is False and out.reason == "ConnectError"


@pytest.mark.asyncio
async def test_writeback_without_a_client_is_counted_not_raised():
    out = await dec.write_sentiment(None, BASE, 41, CLASSIFICATION)
    assert out.ok is False and out.reason == "no client"


@pytest.mark.asyncio
async def test_write_sentiment_never_retries():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, json={})

    async with _client(handler) as http:
        await dec.write_sentiment(http, BASE, 41, CLASSIFICATION)

    assert len(calls) == 1, "no retries anywhere in this layer"


@pytest.mark.asyncio
async def test_messages_never_carry_a_body_or_a_url(caplog):
    """G14: OUR messages carry a status, an id or an exception type.

    Scoped to this module's own records on purpose. httpx's own logger logs
    the request line at INFO, which is not a leak here — unlike Alpha Vantage
    and FRED, this URL carries no key, only an internal host and a row id.
    That is exactly why those two clients pin the httpx logger to WARNING and
    this one does not need to.
    """
    async with _client(lambda r: httpx.Response(500, text="SECRET BODY sk-or-v1-xyz")) as http:
        with caplog.at_level("DEBUG"):
            out = await dec.write_sentiment(http, BASE, 41, CLASSIFICATION)

    ours = [r.getMessage() for r in caplog.records if r.name == "data_engine_client"]
    assert ours, "the failure must be logged"
    joined = " ".join(ours)
    assert "SECRET" not in joined and "sk-or" not in joined
    assert "http://" not in joined
    assert "SECRET" not in str(out.reason)


@pytest.mark.asyncio
async def test_writeback_oserror_is_counted_not_raised():
    """A socket error that is not an httpx.HTTPError still must not raise at
    the route — write-back is fail-open on every path."""
    def broken(request):
        raise OSError("socket died")

    async with _client(broken) as http:
        out = await dec.write_sentiment(http, BASE, 41, CLASSIFICATION)

    assert out.ok is False and out.reason == "OSError"


def test_result_repr_is_debuggable():
    out = dec.WriteBackResult(41, False, "404")
    assert "41" in repr(out) and "404" in repr(out)
