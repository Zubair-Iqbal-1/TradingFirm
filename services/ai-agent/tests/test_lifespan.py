"""
Part 4.2 — the service's first lifespan (spec decision 1 and the
"Redis down at startup" failure branch).

risk-shield's shape: bounded and fail-open. Redis gets config.STARTUP_TIMEOUT
seconds, a failure or a timeout leaves it None, and the service still boots.
This is also the first caller cache.create_redis has ever had — 4.1 shipped it
uncovered because nothing built a client.

Nothing here opens a socket: create_redis is monkeypatched in every test.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

import cache
import config
import main


@pytest.fixture
def no_http(monkeypatch):
    """The lifespan builds one httpx.AsyncClient. It opens no connection
    until a request is made, so it is left real — only its aclose() is
    watched, to prove shutdown closes it."""
    closed = []
    import httpx
    real_aclose = httpx.AsyncClient.aclose

    async def spy(self):
        closed.append(self)
        return await real_aclose(self)

    monkeypatch.setattr(httpx.AsyncClient, "aclose", spy)
    return closed


class FakeRedisClient:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


def test_lifespan_opens_redis_provider_and_http(monkeypatch, no_http):
    built = FakeRedisClient()

    async def fake_create_redis(*a, **kw):
        return built

    monkeypatch.setattr(cache, "create_redis", fake_create_redis)

    with TestClient(main.app) as client:
        assert main.app.state.redis is built
        assert main.app.state.provider is not None
        assert main.app.state.http is not None
        assert set(main.app.state.memory_caps) == {
            cache.STATE_LLM_CALLS, cache.STATE_CLASSIFIER_CALLS}
        # one MemoryCap per counter, never one shared instance
        assert (main.app.state.memory_caps[cache.STATE_LLM_CALLS]
                is not main.app.state.memory_caps[cache.STATE_CLASSIFIER_CALLS])
        assert isinstance(main.app.state.memory_cost, cache.MemoryCost)
        assert client.get("/health").json()["redis"] is True

    assert built.closed is True, "shutdown must close Redis"
    assert no_http, "shutdown must close the HTTP client"
    assert main.app.state.provider is None


def test_lifespan_redis_down_boots_without_redis(monkeypatch, no_http):
    """Fail-open: the classifier still works, in-process, with nothing
    cached."""
    async def boom(*a, **kw):
        raise ConnectionError("no redis here")

    monkeypatch.setattr(cache, "create_redis", boom)

    with TestClient(main.app) as client:
        assert main.app.state.redis is None
        assert main.app.state.provider is not None
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["redis"] is False


def test_lifespan_redis_hang_is_bounded_by_startup_timeout(monkeypatch, no_http):
    """A Redis that accepts the socket and never answers must not hold boot
    open. STARTUP_TIMEOUT is read at call time, so patching config works."""
    monkeypatch.setattr(config, "STARTUP_TIMEOUT", 0.05)

    async def hang(*a, **kw):
        await asyncio.sleep(30)

    monkeypatch.setattr(cache, "create_redis", hang)

    with TestClient(main.app) as client:
        assert main.app.state.redis is None
        assert client.get("/health").json()["redis"] is False


def test_lifespan_redis_close_failure_does_not_break_shutdown(monkeypatch, no_http):
    class AngryRedis:
        async def aclose(self):
            raise RuntimeError("close failed")

    async def fake_create_redis(*a, **kw):
        return AngryRedis()

    monkeypatch.setattr(cache, "create_redis", fake_create_redis)

    with TestClient(main.app):
        pass   # exiting the context is the shutdown; it must not raise


def test_lifespan_builds_no_database_pool(monkeypatch, no_http):
    """Spec 4.2 decision 1: no ai.* table is written in this part, so there
    is no pool. Part 4.4 adds it with migration 005_ai.sql."""
    async def fake_create_redis(*a, **kw):
        return FakeRedisClient()

    monkeypatch.setattr(cache, "create_redis", fake_create_redis)

    with TestClient(main.app):
        assert getattr(main.app.state, "db_pool", None) is None


def test_health_reports_caps_and_a_bool_for_the_key(monkeypatch, no_http):
    async def fake_create_redis(*a, **kw):
        return FakeRedisClient()

    monkeypatch.setattr(cache, "create_redis", fake_create_redis)

    with TestClient(main.app) as client:
        body = client.get("/health").json()

    assert body["caps"] == {
        "llmDaily": main.settings.llm_daily_call_cap,
        "classifierDaily": main.settings.llm_classifier_daily_call_cap,
    }
    assert isinstance(body["llmConfigured"], bool), "G14: a bool, never the key"
    assert "key" not in str(body).lower() or body["llmConfigured"] is False
    for value in body.values():
        assert "sk-" not in str(value)
