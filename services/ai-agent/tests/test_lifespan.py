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
import db
import main


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    """Part 4.4 gave the lifespan a pool. No test here may open a socket to
    Postgres, so by default the factory fails the way a dead database does;
    the pool tests below replace it."""
    async def boom(*a, **kw):
        raise ConnectionRefusedError("postgres is down")

    monkeypatch.setattr(db, "create_db_pool", boom)


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


class FakePool:
    def __init__(self, close_raises=False):
        self.closed = False
        self.close_raises = close_raises

    async def close(self):
        if self.close_raises:
            raise RuntimeError("close failed")
        self.closed = True


def _redis_ok(monkeypatch):
    async def fake_create_redis(*a, **kw):
        return FakeRedisClient()
    monkeypatch.setattr(cache, "create_redis", fake_create_redis)


def test_lifespan_db_down_boots_without_pool(monkeypatch, no_http, caplog):
    """Fail-open: no pool, the service still boots and says so. /analyze is
    what refuses (503), not the process."""
    _redis_ok(monkeypatch)
    with caplog.at_level("WARNING"):
        with TestClient(main.app) as client:
            assert main.app.state.db_pool is None
            body = client.get("/health").json()
    assert body["db_connected"] is False
    assert body["capsSeeded"] is False
    assert "Database unavailable" in caplog.text
    assert "caps not seeded from the ledger: no database pool" in caplog.text


def test_lifespan_db_hang_is_bounded_by_startup_timeout(monkeypatch, no_http):
    import asyncio
    _redis_ok(monkeypatch)
    monkeypatch.setattr(config, "STARTUP_TIMEOUT", 0.05)

    async def hang(*a, **kw):
        await asyncio.sleep(30)

    monkeypatch.setattr(db, "create_db_pool", hang)
    with TestClient(main.app):
        assert main.app.state.db_pool is None


def test_lifespan_opens_pool_seeds_caps_and_closes(monkeypatch, no_http):
    import ledger
    _redis_ok(monkeypatch)
    pool = FakePool()
    seen = {}

    async def fake_pool(*a, **kw):
        return pool

    async def fake_seed(p, r, caps, cost, now=None):
        seen.update(pool=p, redis=r, caps=set(caps))
        return True

    monkeypatch.setattr(db, "create_db_pool", fake_pool)
    monkeypatch.setattr(ledger, "seed_caps", fake_seed)

    with TestClient(main.app) as client:
        assert main.app.state.db_pool is pool
        body = client.get("/health").json()
        assert body["db_connected"] is True and body["capsSeeded"] is True
    assert seen["pool"] is pool
    assert isinstance(seen["redis"], FakeRedisClient), "seeded after Redis is resolved"
    assert seen["caps"] == {cache.STATE_LLM_CALLS, cache.STATE_CLASSIFIER_CALLS}
    assert pool.closed is True


def test_lifespan_pool_close_failure_does_not_break_shutdown(monkeypatch, no_http):
    import ledger
    _redis_ok(monkeypatch)

    async def fake_pool(*a, **kw):
        return FakePool(close_raises=True)

    async def fake_seed(*a, **kw):
        return False

    monkeypatch.setattr(db, "create_db_pool", fake_pool)
    monkeypatch.setattr(ledger, "seed_caps", fake_seed)
    with TestClient(main.app):
        pass


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


# ── Part 4.5: the journal task ───────────────────────────────────

def test_scoring_loop_off_when_disabled(monkeypatch, no_http):
    from journal import runner
    started = []
    monkeypatch.setattr(runner, "run_loop", lambda *a, **kw: started.append(1))
    monkeypatch.setattr(main.settings, "journal_scoring_enabled", False)
    with TestClient(main.app) as client:
        assert main.app.state.journal_task is None
        body = client.get("/health").json()
    assert started == []
    assert body["journalScoringEnabled"] is False
    assert body["journalLastRunAt"] is None and body["journalLastResult"] is None


def test_restart_spends_nothing(monkeypatch, no_http):
    """Flag on: the task starts and waits for the next slot. No boot pass —
    score_once is never called at startup — and shutdown cancels it."""
    from journal import runner
    passes, cancelled = [], []

    async def never(*a, **kw):
        passes.append(1)

    async def fake_loop(state, settings):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(1)
            raise

    monkeypatch.setattr(runner, "score_once", never)
    monkeypatch.setattr(runner, "run_loop", fake_loop)
    monkeypatch.setattr(main.settings, "journal_scoring_enabled", True)
    with TestClient(main.app) as client:
        task = main.app.state.journal_task
        assert task is not None and not task.done()
        assert client.get("/health").json()["journalScoringEnabled"] is True
    assert passes == [] and cancelled == [1]
    assert main.app.state.journal_task is None
