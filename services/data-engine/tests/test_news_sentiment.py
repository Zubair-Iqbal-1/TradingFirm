"""
Part 4.2 — POST /news/{id}/sentiment (spec decisions 8-10).

The classifier's write-back target. TestClient over the app without the
lifespan, the asyncpg pool mocked, so the real db.set_news_sentiment SQL and
parameters are asserted. No network, no real database.

The contract this route enforces exists twice — here and as
classifier.ITEM_LIMITS in ai-agent. test_sentiment_contract_pinned_to_spec
below and test_item_limits_pinned_to_spec there are what keep them together.
"""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from fastapi.testclient import TestClient

import db
import main

BODY = {
    "relevance": "high",
    "sentiment": -0.4,
    "category": "guidance",
    "oneLine": "Q4 guidance cut on weak enterprise demand.",
    "model": "anthropic/claude-sonnet-5",
    "classifiedAt": "2026-09-20T18:00:00+00:00",
}


def _pool(returns=41, fail=None):
    """`returns` is what RETURNING id yields: a row, or None for no match."""
    conn = AsyncMock()
    row = None if returns is None else {"id": returns}
    conn.fetchrow = AsyncMock(return_value=row, side_effect=fail)
    conn.execute = AsyncMock()
    conn.executemany = AsyncMock()
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


def test_sentiment_write_shape(client):
    pool, conn = _pool()
    resp = client(pool).post("/news/41/sentiment", json=BODY)

    assert resp.status_code == 200
    assert resp.json() == {"id": 41, "updated": True}

    conn.fetchrow.assert_awaited_once()
    sql, news_id, payload = conn.fetchrow.await_args.args
    assert sql == db.SET_NEWS_SENTIMENT_SQL
    assert "UPDATE data_engine.news_items" in sql
    assert "SET sentiment = $2::jsonb" in sql
    assert "WHERE id = $1" in sql
    assert "RETURNING id" in sql
    assert news_id == 41
    assert json.loads(payload) == {
        "relevance": "high", "sentiment": -0.4, "category": "guidance",
        "oneLine": "Q4 guidance cut on weak enterprise demand.",
        "model": "anthropic/claude-sonnet-5",
        "classifiedAt": "2026-09-20T18:00:00+00:00",
    }


def test_sentiment_replaces_never_merges():
    """Decision 9: a re-classification is the newer truth. A merge (`||`)
    would leave half an older verdict behind."""
    assert "||" not in db.SET_NEWS_SENTIMENT_SQL
    assert "SET sentiment = $2::jsonb" in db.SET_NEWS_SENTIMENT_SQL


def test_sentiment_unknown_id_is_404(client):
    pool, conn = _pool(returns=None)
    resp = client(pool).post("/news/999/sentiment", json=BODY)
    assert resp.status_code == 404
    assert resp.json() == {"detail": "news item not found"}
    conn.fetchrow.assert_awaited_once()


def test_sentiment_db_unavailable_is_503(client):
    resp = client(None).post("/news/41/sentiment", json=BODY)
    assert resp.status_code == 503
    assert resp.json() == {"detail": "database unavailable"}

    for fail in (asyncpg.PostgresError("boom"), asyncpg.InterfaceError("closed"),
                 ConnectionError("reset"), TimeoutError()):
        pool, _ = _pool(fail=fail)
        resp = client(pool).post("/news/41/sentiment", json=BODY)
        assert resp.status_code == 503, type(fail).__name__
        assert resp.json() == {"detail": "database unavailable"}


@pytest.mark.parametrize("field, value", [
    ("relevance", "HIGH"),            # enum is case-sensitive
    ("relevance", "critical"),
    ("relevance", None),
    ("category", "rumour"),
    ("category", 3),
    ("sentiment", -1.01),
    ("sentiment", 1.01),
    ("sentiment", "negative"),
    ("oneLine", ""),
    ("oneLine", "   "),
    ("oneLine", "x" * 301),
    ("oneLine", "bad\x00byte"),
    ("model", ""),
    ("model", "m" * 101),
    ("classifiedAt", "2026-09-20T18:00:00"),   # naive: AwareDatetime refuses
    ("classifiedAt", "not a date"),
])
def test_sentiment_bad_body_is_422(client, field, value):
    pool, conn = _pool()
    resp = client(pool).post("/news/41/sentiment", json={**BODY, field: value})
    assert resp.status_code == 422, f"{field}={value!r}"
    conn.fetchrow.assert_not_awaited()


@pytest.mark.parametrize("body", [
    {**BODY, "ticker": "AAPL"},          # extra="forbid"
    {k: v for k, v in BODY.items() if k != "relevance"},
    {},
])
def test_sentiment_extra_or_missing_field_is_422(client, body):
    pool, conn = _pool()
    resp = client(pool).post("/news/41/sentiment", json=body)
    assert resp.status_code == 422
    conn.fetchrow.assert_not_awaited()


def test_sentiment_bad_path_id_is_422(client):
    pool, conn = _pool()
    resp = client(pool).post("/news/not-an-int/sentiment", json=BODY)
    assert resp.status_code == 422
    conn.fetchrow.assert_not_awaited()


@pytest.mark.parametrize("relevance", main.SENTIMENT_RELEVANCE)
@pytest.mark.parametrize("bound", [-1.0, 0.0, 1.0])
def test_sentiment_accepts_every_enum_and_both_bounds(client, relevance, bound):
    pool, _ = _pool()
    body = {**BODY, "relevance": relevance, "sentiment": bound}
    assert client(pool).post("/news/41/sentiment", json=body).status_code == 200

    for category in main.SENTIMENT_CATEGORIES:
        pool, _ = _pool()
        resp = client(pool).post("/news/41/sentiment", json={**BODY, "category": category})
        assert resp.status_code == 200, category


def test_sentiment_repeat_write_overwrites(client):
    """Idempotent: the second call is the same statement with the newer
    value, and nothing else on the row is touched."""
    pool, conn = _pool()
    c = client(pool)
    first = c.post("/news/41/sentiment", json=BODY)
    second = c.post("/news/41/sentiment", json={**BODY, "sentiment": 0.8, "relevance": "low"})

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"id": 41, "updated": True}
    assert conn.fetchrow.await_count == 2
    (sql1, id1, p1), (sql2, id2, p2) = (call.args for call in conn.fetchrow.await_args_list)
    assert sql1 == sql2 and id1 == id2 == 41
    assert json.loads(p1)["sentiment"] == -0.4
    assert json.loads(p2)["sentiment"] == 0.8
    conn.execute.assert_not_awaited()
    conn.executemany.assert_not_awaited()


@pytest.mark.parametrize("key, status", [
    (None, 200),                                  # absent: pre-4.4 callers
    ("nvda-q3-guidance-cut", 200),
    ("a1-b2", 200),
    ("nvda", 422),                                # one word is not a slug
    ("NVDA-guidance-cut", 422),                   # upper case
    ("nvda guidance cut", 422),
    ("nvda--cut", 422),
    ("-nvda-cut", 422),
    ("a-b-c-d-e-f-g-h-i", 422),                   # nine words
    ("a-" + "b" * 80, 422),                       # over 80 chars
    ("", 422),
])
def test_sentiment_event_key_optional_and_validated(client, key, status):
    """Part 4.4: eventKey is optional (deploy order, older labels) and
    validated when present. A stored value carries it only when sent."""
    pool, conn = _pool()
    body = dict(BODY) if key is None else {**BODY, "eventKey": key}
    resp = client(pool).post("/news/41/sentiment", json=body)
    assert resp.status_code == status
    if status == 200:
        stored = json.loads(conn.fetchrow.await_args.args[2])
        assert stored.get("eventKey") == key
        assert ("eventKey" in stored) is (key is not None)
    else:
        conn.fetchrow.assert_not_awaited()


def test_sentiment_contract_pinned_to_spec():
    assert (main.SENTIMENT_EVENT_KEY_MAX, main.SENTIMENT_EVENT_KEY_RE) == (
        80, r"^[a-z0-9]+(-[a-z0-9]+){1,7}$"), (
        "ai-agent's classifier.EVENT_KEY_MAX / EVENT_KEY_RE keep a copy "
        "(spec 4.4 decision 4). Change both or neither."
    )
    assert main.SENTIMENT_RELEVANCE == ("high", "medium", "low")
    assert main.SENTIMENT_CATEGORIES == (
        "guidance", "analyst", "legal", "product", "macro", "insider", "other")
    assert (main.SENTIMENT_ONE_LINE_MAX, main.SENTIMENT_MODEL_MAX) == (300, 100), (
        "ai-agent's classifier.ITEM_LIMITS keeps a copy of this contract "
        "(services/ai-agent/classifier.py, test_item_limits_pinned_to_spec, "
        "spec 4.2 decision 10). Change both or neither: a drift is a 422 on "
        "every write-back, and write-back is fail-open, so it fails quietly."
    )
