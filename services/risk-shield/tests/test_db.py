"""Part 3.1 — db.py: the error tuple and the bounded pool factory.
asyncpg.create_pool is patched; nothing opens a socket."""

import asyncio
import json
from datetime import datetime, timezone

import asyncpg
import pytest

import db


def test_db_errors_membership():
    assert db.DB_ERRORS == (asyncpg.PostgresError, asyncpg.InterfaceError, ConnectionError)


def test_db_errors_excludes_timeout():
    """The Part 2.4 regression: asyncio.TimeoutError *is* the builtin
    TimeoutError, which subclasses OSError. With OSError in the tuple,
    every timed-out call would be reported as a dead database."""
    assert asyncio.TimeoutError is TimeoutError
    assert issubclass(TimeoutError, OSError)
    assert OSError not in db.DB_ERRORS
    assert not issubclass(TimeoutError, db.DB_ERRORS)
    assert not isinstance(asyncio.TimeoutError(), db.DB_ERRORS)


@pytest.mark.asyncio
async def test_create_db_pool_uses_stripped_dsn_and_pinned_sizes(monkeypatch):
    seen = {}

    async def fake_create_pool(**kwargs):
        seen.update(kwargs)
        return "POOL"

    monkeypatch.setattr(db.asyncpg, "create_pool", fake_create_pool)
    monkeypatch.setattr(
        db.settings, "database_url",
        "postgresql+asyncpg://tf_user:pw@postgres:5432/tradingfirm_dev",
    )
    pool = await db.create_db_pool()
    assert pool == "POOL"
    assert seen["dsn"] == "postgresql://tf_user:pw@postgres:5432/tradingfirm_dev"
    assert "+asyncpg" not in seen["dsn"]
    assert seen["min_size"] == 2
    assert seen["max_size"] == 10
    assert seen["command_timeout"] == 30
    assert seen["timeout"] == 5.0          # decision 6: bounds connecting


@pytest.mark.asyncio
async def test_create_db_pool_reads_startup_timeout_at_call_time(monkeypatch):
    """The bound comes from config at call time, so a test (and 3.4) can
    patch it — no from-import copy."""
    seen = {}

    async def fake_create_pool(**kwargs):
        seen.update(kwargs)
        return "POOL"

    monkeypatch.setattr(db.asyncpg, "create_pool", fake_create_pool)
    monkeypatch.setattr("config.STARTUP_TIMEOUT", 0.25)
    await db.create_db_pool()
    assert seen["timeout"] == 0.25


@pytest.mark.asyncio
async def test_create_db_pool_logs_dsn_without_password(monkeypatch, caplog):
    password = "p4ssw0rd-3f9ac1"

    async def fake_create_pool(**kwargs):
        return "POOL"

    monkeypatch.setattr(db.asyncpg, "create_pool", fake_create_pool)
    monkeypatch.setattr(
        db.settings, "database_url",
        f"postgresql+asyncpg://tf_user:{password}@postgres:5432/tradingfirm_dev",
    )
    with caplog.at_level("DEBUG"):
        await db.create_db_pool()
    joined = "\n".join(f"{r.getMessage()} {r.args}" for r in caplog.records)
    assert password not in joined
    assert "tf_user" not in joined
    assert "postgres:5432/tradingfirm_dev" in joined


@pytest.mark.asyncio
async def test_create_db_pool_raise_propagates(monkeypatch):
    """Closed here; the lifespan is what catches it (test_lifespan.py)."""
    async def fake_create_pool(**kwargs):
        raise asyncpg.InvalidCatalogNameError("database does not exist")

    monkeypatch.setattr(db.asyncpg, "create_pool", fake_create_pool)
    with pytest.raises(asyncpg.PostgresError):
        await db.create_db_pool()


# ── risk.health_checks (Part 3.4) ────────────────────────────────

AT = datetime(2026, 9, 10, 20, 20, tzinfo=timezone.utc)
PREV = datetime(2026, 9, 9, 20, 20, tzinfo=timezone.utc)


class _RecordingPool:
    def __init__(self, row=None, rows=(), value=None):
        self.row, self.rows, self.value, self.calls = row, list(rows), value, []

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))

    async def fetchval(self, sql, *args):
        self.calls.append(("fetchval", sql, args))
        return self.value

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        return self.row

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        return self.rows


def _flat(sql):
    return " ".join(sql.split())


@pytest.mark.asyncio
async def test_insert_health_check_sql_and_params():
    pool = _RecordingPool()
    health = {"score": 64, "regime": "CAUTIOUS", "coverage": 80, "stale": True, "staleMonitors": ["vix"],
              "checkedAt": AT.isoformat(), "monitors": {"vix": {"score": 60}}, "inputs": {"source": "last_known"}}
    await db.insert_health_check(pool, health, "settle", "declining", {"score": 70, "checkedAt": PREV})
    [(op, sql, args)] = pool.calls
    assert op == "execute"
    assert _flat(sql) == ("INSERT INTO risk.health_checks (checked_at, score, regime, trend, indicators) "
                          "VALUES ($1, $2, $3, $4, $5::jsonb)")
    assert args[:4] == (AT, 64, "CAUTIOUS", "declining")
    assert args[0].tzinfo is not None
    assert json.loads(args[4]) == {
        "kind": "settle", "coverage": 80, "stale": True, "staleMonitors": ["vix"],
        "monitors": {"vix": {"score": 60}}, "inputs": {"source": "last_known"},
        "settleScore": 70, "settleCheckedAt": "2026-09-09T20:20:00+00:00",
        "pausedSeconds": None,                                  # 3.4 follow-up addition 2, no pause
        "futures": {}, "overlay": None,                         # Part 3.4b, a settle check
    }
    # No settle base; a NaN never reaches SQL.
    health["monitors"]["vix"]["raw"] = {"level": float("nan")}
    pool.calls.clear()
    with pytest.raises(ValueError):
        await db.insert_health_check(pool, health, "market", None, None)
    assert pool.calls == []
    assert db.DB_FAILURES == (*db.DB_ERRORS, TimeoutError)


@pytest.mark.asyncio
async def test_latest_health_check_queries():
    row = {"checked_at": AT, "score": 64, "regime": "CAUTIOUS", "trend": None, "indicators": "{}"}
    pool = _RecordingPool(row=row)
    assert await db.latest_health_check(pool) == row
    assert await db.latest_scored_health_check(pool) == row
    (_, latest_sql, latest_args), (_, scored_sql, scored_args) = pool.calls
    assert _flat(latest_sql) == ("SELECT checked_at, score, regime, trend, indicators "
                                 "FROM risk.health_checks ORDER BY checked_at DESC LIMIT 1")
    assert _flat(scored_sql) == ("SELECT checked_at, score, regime FROM risk.health_checks "
                                 "WHERE score IS NOT NULL ORDER BY checked_at DESC LIMIT 1")
    assert latest_args == scored_args == ()
    empty = _RecordingPool(row=None)
    assert await db.latest_health_check(empty) is None
    assert await db.latest_scored_health_check(empty) is None


@pytest.mark.asyncio
async def test_settle_base_query():
    before = datetime(2026, 9, 10, 13, 30, tzinfo=timezone.utc)
    pool = _RecordingPool(row={"checked_at": PREV, "score": 70})
    assert await db.settle_base(pool, before) == {"score": 70, "checkedAt": PREV}
    [(op, sql, args)] = pool.calls
    assert op == "fetchrow" and args == (before,)
    assert _flat(sql) == ("SELECT checked_at, score FROM risk.health_checks "
                          "WHERE indicators->>'kind' = 'settle' AND score IS NOT NULL AND checked_at < $1 "
                          "ORDER BY checked_at DESC LIMIT 1")
    assert await db.settle_base(_RecordingPool(row=None), before) is None


@pytest.mark.asyncio
async def test_health_history_query():
    since = datetime(2026, 8, 11, 20, 0, tzinfo=timezone.utc)
    rows = [
        {"checked_at": PREV, "score": 64, "regime": "CAUTIOUS", "trend": "stable", "kind": "market", "stale": "false"},
        {"checked_at": AT, "score": None, "regime": None, "trend": None, "kind": "settle", "stale": "true"},
        {"checked_at": AT, "score": 70, "regime": "HEALTHY", "trend": None, "kind": None, "stale": "garbage"},
    ]
    pool = _RecordingPool(rows=rows)
    result = await db.health_history(pool, since)
    [(op, sql, args)] = pool.calls
    assert op == "fetch" and args == (since,)
    assert _flat(sql) == ("SELECT checked_at, score, regime, trend, indicators->>'kind' AS kind, "
                          "indicators->>'stale' AS stale FROM risk.health_checks "
                          "WHERE checked_at >= $1 ORDER BY checked_at ASC")
    assert [r["stale"] for r in result] == [False, True, None]
    assert result[1]["score"] is None
    assert set(result[0]) == {"checked_at", "score", "regime", "trend", "kind", "stale"}
    assert await db.health_history(_RecordingPool(rows=[]), since) == []


# ── risk.macro_briefs (Part 3.6b) ────────────────────────────────

@pytest.mark.asyncio
async def test_macro_brief_queries():
    body, inputs = {"oneParagraph": "p", "keyRisks": ["r"]}, {"schemaVersion": 1, "news": {"items": []}}
    fields = dict(generated_at=AT, regime="CAUTIOUS", health_score=64, brief_text="p", brief=body, inputs=inputs,
                  trigger="slot")
    pool = _RecordingPool(value="3f0c9a52-6d1e-4b7a-9c1f-2a6e8d4b5c70")
    assert await db.insert_macro_brief(pool, **fields) == "3f0c9a52-6d1e-4b7a-9c1f-2a6e8d4b5c70"
    [(op, sql, args)] = pool.calls
    assert op == "fetchval"
    assert _flat(sql) == ("INSERT INTO risk.macro_briefs (generated_at, regime, health_score, brief_text, brief, "
                          "inputs, trigger) VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7) RETURNING id")
    assert (*args[:4], json.loads(args[4]), json.loads(args[5]), args[6]) == (
        AT, "CAUTIOUS", 64, "p", body, inputs, "slot")
    pool.calls.clear()
    for field in ("brief", "inputs"):                      # a NaN in either body never reaches SQL
        with pytest.raises(ValueError):
            await db.insert_macro_brief(pool, **{**fields, field: {"x": float("nan")}})
    assert pool.calls == []

    stored = {"id": "3f0c9a52-6d1e-4b7a-9c1f-2a6e8d4b5c70", "generated_at": AT, "trigger": "slot",
              "regime": "CAUTIOUS", "health_score": 64, "brief_text": "p", "brief": json.dumps(body),
              "inputs": json.dumps(inputs)}
    pool = _RecordingPool(row=stored)
    assert await db.latest_macro_brief(pool) == {**stored, "brief": body, "inputs": inputs}
    assert _flat(pool.calls[0][1]) == ("SELECT id, generated_at, trigger, regime, health_score, brief_text, brief, "
                                       "inputs FROM risk.macro_briefs ORDER BY generated_at DESC LIMIT 1")
    assert await db.latest_macro_brief(_RecordingPool(row=None)) is None

    pool = _RecordingPool(value=PREV)
    assert (await db.last_brief_at(pool), await db.last_brief_at(pool, "critical")) == (PREV, PREV)
    assert [(op, args) for op, _, args in pool.calls] == [("fetchval", (None,)), ("fetchval", ("critical",))]
    assert _flat(pool.calls[0][1]) == ("SELECT max(generated_at) FROM risk.macro_briefs "
                                       "WHERE $1::text IS NULL OR trigger = $1")

    pool = _RecordingPool(value=True)
    end = AT.replace(minute=53)                                         # the slot window: + 1,980 s
    assert await db.slot_brief_exists(pool, AT, end) is True
    [(op, sql, args)] = pool.calls
    assert (op, args) == ("fetchval", (AT, end))
    assert _flat(sql) == ("SELECT EXISTS (SELECT 1 FROM risk.macro_briefs "
                          "WHERE trigger = 'slot' AND generated_at >= $1 AND generated_at < $2)")


@pytest.mark.asyncio
async def test_settle_reference_query():
    """Part 3.4b decision 6: the same row settle_base finds, with its futures."""
    before = datetime(2026, 9, 10, 13, 30, tzinfo=timezone.utc)
    futures = {"ES=F": {"price": 5000.0, "date": "2026-09-09", "asOf": "x", "stale": False}, "NQ=F": None}
    pool = _RecordingPool(row={"checked_at": PREV, "score": 70, "regime": "HEALTHY",
                               "indicators": json.dumps({"kind": "settle", "futures": futures})})
    assert await db.settle_reference(pool, before) == {
        "score": 70, "checkedAt": PREV, "regime": "HEALTHY", "futures": futures,
        "indicators": {"kind": "settle", "futures": futures}}
    [(op, sql, args)] = pool.calls
    assert op == "fetchrow" and args == (before,)
    assert _flat(sql) == ("SELECT checked_at, score, regime, indicators FROM risk.health_checks "
                          "WHERE indicators->>'kind' = 'settle' AND score IS NOT NULL AND checked_at < $1 "
                          "ORDER BY checked_at DESC LIMIT 1")
    # A row from before 3.4b keeps its blob; it simply has no futures block.
    pool = _RecordingPool(row={"checked_at": PREV, "score": 70, "regime": "HEALTHY",
                               "indicators": json.dumps({"kind": "settle", "coverage": 100})})
    reference = await db.settle_reference(pool, before)
    assert reference["futures"] == {} and reference["indicators"] == {"kind": "settle", "coverage": 100}
    # A corrupt or wrong-shaped blob: nothing to copy, and never a raise.
    for stored in ("not json {", json.dumps([1, 2]), None):
        pool = _RecordingPool(row={"checked_at": PREV, "score": 70, "regime": "HEALTHY",
                                   "indicators": stored})
        reference = await db.settle_reference(pool, before)
        assert reference["futures"] == {} and reference["indicators"] == {}
    assert await db.settle_reference(_RecordingPool(row=None), before) is None
