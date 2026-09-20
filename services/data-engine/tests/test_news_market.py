"""
Part 3.6a — GET /news/market (spec decision 2).

TestClient over the app without the lifespan: query validation is FastAPI's
job, so the route has to go through it. The asyncpg pool is a mock whose
connection records fetch(), so the real db.get_market_news SQL and
parameters are asserted. No network, no real database; the real-Postgres
read is the twin round trip in the part's verification.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from fastapi.testclient import TestClient

import db
import main

# `id` and `sentiment` joined the row in Part 4.2: ai-agent's classifier needs
# an addressable row to write back to, and `sentiment` is what 4.4 filters on
# (IS NULL = not yet classified). jsonb arrives as TEXT — no codec is
# registered — so the second row carries the string form on purpose.
SENTIMENT_JSON = ('{"relevance": "high", "sentiment": -0.4, "category": "guidance", '
                  '"oneLine": "Guidance cut", "model": "anthropic/claude-sonnet-5", '
                  '"classifiedAt": "2026-09-10T18:00:00+00:00"}')

ROWS = [
    {"id": 41, "published_at": datetime(2026, 9, 10, 17, 12, 29, tzinfo=timezone.utc),
     "source": "CNBC", "title": "OpenAI targets work of Wall Street junior bankers",
     "summary": "A short summary.", "sentiment": None,
     "url": "https://www.cnbc.com/2026/09/10/openai-bankers.html"},
    {"id": 42, "published_at": datetime(2026, 9, 10, 15, 55, tzinfo=timezone.utc), "source": None,
     "title": "Oil surges 5%", "summary": None, "sentiment": SENTIMENT_JSON,
     "url": "https://www.reuters.com/markets/oil"},
]


def _pool(rows=(), fail=None):
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=list(rows), side_effect=fail)
    conn.executemany = AsyncMock()
    conn.execute = AsyncMock()
    pool = MagicMock()
    acquire_cm = AsyncMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=acquire_cm)
    return pool, conn


@pytest.fixture
def client():
    saved_redis = getattr(main.app.state, "redis", None)

    def _make(pool, redis=None):
        main.app.state.db_pool = pool
        main.app.state.redis = redis
        return TestClient(main.app)
    yield _make
    main.app.state.db_pool = None
    main.app.state.redis = saved_redis


def test_news_market_query_shape(client):
    pool, conn = _pool(ROWS)
    before = datetime.now(timezone.utc)
    resp = client(pool).get("/news/market")
    after = datetime.now(timezone.utc)

    assert resp.status_code == 200
    conn.fetch.assert_awaited_once()
    sql, ticker, since, limit = conn.fetch.await_args.args
    assert sql == db.GET_MARKET_NEWS_SQL
    assert "WHERE ticker = $1 AND published_at >= $2" in sql
    assert "ORDER BY published_at DESC" in sql
    assert "LIMIT $3" in sql
    assert ticker == "_MARKET" == db.MARKET_TICKER
    assert before - timedelta(hours=24) <= since <= after - timedelta(hours=24)
    assert since.tzinfo is not None
    assert limit == 50

    client(pool).get("/news/market?hours=168&limit=100")
    _, _, since, limit = conn.fetch.await_args.args
    assert limit == 100
    assert since <= datetime.now(timezone.utc) - timedelta(hours=168)


def test_news_market_items_shape(client):
    pool, _ = _pool(ROWS)
    resp = client(pool).get("/news/market")
    assert resp.status_code == 200
    assert resp.json() == [
        {"id": 41, "publishedAt": "2026-09-10T17:12:29+00:00", "source": "CNBC",
         "title": "OpenAI targets work of Wall Street junior bankers", "summary": "A short summary.",
         "sentiment": None,
         "url": "https://www.cnbc.com/2026/09/10/openai-bankers.html"},
        {"id": 42, "publishedAt": "2026-09-10T15:55:00+00:00", "source": None,
         "title": "Oil surges 5%", "summary": None,
         "sentiment": {"relevance": "high", "sentiment": -0.4, "category": "guidance",
                       "oneLine": "Guidance cut", "model": "anthropic/claude-sonnet-5",
                       "classifiedAt": "2026-09-10T18:00:00+00:00"},
         "url": "https://www.reuters.com/markets/oil"},
    ]


def test_news_market_selects_id_and_sentiment():
    """Part 4.2: without these two columns nothing can address a row for
    write-back, and 4.4 cannot tell a classified row from an unclassified
    one."""
    assert "SELECT id, published_at, source, title, summary, url, sentiment" in db.GET_MARKET_NEWS_SQL


def test_news_market_unparseable_sentiment_reads_as_null(client):
    """A malformed jsonb must not hide the headline it belongs to — the rule
    get_events already uses for `meta`."""
    rows = [dict(ROWS[0], sentiment="not json at all")]
    pool, _ = _pool(rows)
    resp = client(pool).get("/news/market")
    assert resp.status_code == 200
    assert resp.json()[0]["sentiment"] is None
    assert resp.json()[0]["title"] == ROWS[0]["title"]


@pytest.mark.parametrize("query, status", [
    ("hours=0", 422), ("hours=169", 422), ("hours=x", 422),
    ("limit=0", 422), ("limit=101", 422), ("limit=x", 422),
    ("hours=1", 200), ("hours=168", 200), ("limit=1", 200), ("limit=100", 200), ("", 200),
])
def test_news_market_param_validation(client, query, status):
    pool, conn = _pool(ROWS)
    resp = client(pool).get(f"/news/market?{query}")
    assert resp.status_code == status
    if status == 422:
        conn.fetch.assert_not_awaited()


def test_news_market_empty_is_empty_list(client):
    pool, _ = _pool([])
    resp = client(pool).get("/news/market")
    assert resp.status_code == 200
    assert resp.json() == []


def test_news_market_db_unavailable_503(client):
    resp = client(None).get("/news/market")
    assert resp.status_code == 503
    assert resp.json() == {"detail": "database unavailable"}

    for fail in (asyncpg.PostgresError("boom"), asyncpg.InterfaceError("closed"),
                 ConnectionError("reset"), TimeoutError()):
        pool, _ = _pool(fail=fail)
        resp = client(pool).get("/news/market")
        assert resp.status_code == 503, type(fail).__name__
        assert resp.json() == {"detail": "database unavailable"}


def test_news_market_repeat_is_read_only(client):
    pool, conn = _pool(ROWS)
    redis = MagicMock()
    c = client(pool, redis=redis)
    first = c.get("/news/market?limit=2")
    second = c.get("/news/market?limit=2")

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert conn.fetch.await_count == 2
    (sql1, *args1), (sql2, *args2) = (call.args for call in conn.fetch.await_args_list)
    assert sql1 == sql2 and args1[0] == args2[0] and args1[2] == args2[2]
    conn.executemany.assert_not_awaited()
    conn.execute.assert_not_awaited()
    assert redis.mock_calls == []


def test_news_market_bounds_pinned_for_risk_shield():
    assert (main.NEWS_MARKET_DEFAULT_HOURS, main.NEWS_MARKET_MAX_HOURS,
            main.NEWS_MARKET_DEFAULT_LIMIT, main.NEWS_MARKET_MAX_LIMIT) == (24, 168, 50, 100), (
        "risk-shield's macro inputs call GET /news/market?hours=24&limit=50 and keep a copy "
        "of these bounds (services/risk-shield/macro_inputs.py, "
        "test_inputs_news_request_within_route_bounds, spec 3.6a decision 2). Change both."
    )
