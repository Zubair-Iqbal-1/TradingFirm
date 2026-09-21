"""Part 4.4 — ai-agent's database layer, against a fake pool. The literal
SQL and its parameters are asserted; nothing opens a socket."""

import json
from datetime import date, datetime, timezone
from decimal import Decimal

import asyncpg
import pytest

import db
from tests.fake_pool import FakePool

NOW = datetime(2026, 9, 21, 16, 0, tzinfo=timezone.utc)

VERDICT = {
    "user_id": db.DEV_USER_ID, "ticker": "AAPL", "horizon": "swing", "asked_at": NOW,
    "entry": Decimal("50.00"), "entry_source": "last_close",
    "dossier": {"ticker": "AAPL"}, "prompt_inputs": {"events": []},
    "prompt_sha": "abc", "fingerprint": "f" * 64, "macro_brief_id": None,
    "regime": "CAUTIOUS", "verdict": "wait", "confidence": 55, "reasoning": "r",
    "thesis": ["a", "b", "c"], "thesis_breakers": ["x"], "risk_flags": [],
    "plan_proposed": None, "plan_rejection": {"reason": "no_target", "detail": ""},
    "model": "m", "tokens_in": 10, "tokens_out": 5,
}
CALL = {
    "called_at": NOW, "et_day": date(2026, 9, 21), "user_id": db.DEV_USER_ID,
    "ticker": "AAPL", "route": "analyze", "label": "verdict", "model": "m",
    "host": "Anthropic", "tokens_in": 10, "tokens_out": 5, "tokens_reasoning": None,
    "cache_read_tokens": None, "cache_write_tokens": None,
    "cost_usd": Decimal("0.04"), "outcome": "ok", "counters": ["llm_calls"],
    "verdict_id": None,
}


def test_dsn_is_logged_without_credentials():
    assert db._safe_dsn("postgresql://u:secret@postgres:5432/x") == "postgres:5432/x"
    assert "OSError" not in [e.__name__ for e in db.DB_ERRORS]
    assert TimeoutError in db.DB_FAILURES and TimeoutError not in db.DB_ERRORS


@pytest.mark.asyncio
async def test_get_settings_row_none_and_null_account():
    pool = FakePool({"users.settings": {"account_size": Decimal("25000"),
                                        "risk_per_trade_pct": Decimal("1.0")}})
    got = await db.get_settings(pool, db.DEV_USER_ID)
    assert got == {"accountSize": Decimal("25000"), "riskPct": Decimal("1.0")}
    kind, sql, args = pool.calls[0]
    assert sql == db.GET_SETTINGS_SQL and args == (db.DEV_USER_ID,)
    assert "INSERT" not in sql and "UPDATE" not in sql, "users is read-only here"

    assert await db.get_settings(FakePool(), db.DEV_USER_ID) is None
    seeded = FakePool({"users.settings": {"account_size": None,
                                          "risk_per_trade_pct": Decimal("1.0")}})
    assert (await db.get_settings(seeded, db.DEV_USER_ID))["accountSize"] is None


@pytest.mark.asyncio
async def test_insert_llm_call_parameter_order():
    pool = FakePool()
    await db.insert_llm_call(pool, CALL)
    kind, sql, args = pool.calls[0]
    assert sql == db.INSERT_LLM_CALL_SQL
    assert sql.count("$") == len(db.LLM_CALL_COLUMNS) == len(args) == 17
    assert dict(zip(db.LLM_CALL_COLUMNS, args)) == CALL


@pytest.mark.asyncio
async def test_verdict_and_ledger_row_share_one_transaction():
    pool = FakePool({"INSERT INTO ai.verdicts": {"id": "11111111-1111-4111-8111-111111111111"}})
    verdict_id = await db.insert_verdict_with_call(pool, VERDICT, CALL)

    assert verdict_id == "11111111-1111-4111-8111-111111111111"
    assert pool.tx_open == 1
    assert [c[1] for c in pool.calls] == [db.INSERT_VERDICT_SQL, db.INSERT_LLM_CALL_SQL]
    v_args = dict(zip(db.VERDICT_COLUMNS, pool.calls[0][2]))
    assert db.INSERT_VERDICT_SQL.count("$") == len(db.VERDICT_COLUMNS) == 23
    assert json.loads(v_args["dossier"]) == {"ticker": "AAPL"}
    assert json.loads(v_args["thesis"]) == ["a", "b", "c"]
    assert v_args["plan_proposed"] is None and v_args["entry"] == Decimal("50.00")
    assert dict(zip(db.LLM_CALL_COLUMNS, pool.calls[1][2]))["verdict_id"] == verdict_id


@pytest.mark.asyncio
async def test_ledger_failure_rolls_the_verdict_back():
    pool = FakePool({"INSERT INTO ai.verdicts": {"id": "x"}}, raise_on="ai.llm_calls")
    with pytest.raises(asyncpg.PostgresError):
        await db.insert_verdict_with_call(pool, VERDICT, CALL)
    assert pool.calls == [] and len(pool.rolled_back) == 2


@pytest.mark.asyncio
async def test_ledger_totals_reads_the_day_and_the_month():
    pool = FakePool({
        "cost_day": {"llm_calls": 7, "classifier_calls": 3, "cost_day": Decimal("0.21")},
        "cost_month": {"cost_month": Decimal("1.5")},
    })
    got = await db.ledger_totals(pool, date(2026, 9, 21))
    assert got == {"llm_calls": 7, "classifier_calls": 3, "cost_day": 0.21, "cost_month": 1.5}
    assert pool.calls[0][2] == (date(2026, 9, 21),)
    assert pool.calls[1][2] == (date(2026, 9, 1), date(2026, 9, 21))
    assert "cached" not in db.LEDGER_DAY_SQL, "every row is a wire call: nothing to filter"


@pytest.mark.asyncio
async def test_get_verdict_decodes_json_and_bump_served():
    row = {"id": "v", "ticker": "AAPL", "thesis": '["a","b","c"]', "thesis_breakers": '["x"]',
           "risk_flags": "[]", "plan_proposed": None, "plan_rejection": '{"reason":"low_r"}',
           "served_count": 2}
    pool = FakePool({"FROM ai.verdicts": row})
    got = await db.get_verdict(pool, "v", db.DEV_USER_ID)
    assert got["thesis"] == ["a", "b", "c"] and got["plan_rejection"] == {"reason": "low_r"}
    assert pool.calls[0][2] == ("v", db.DEV_USER_ID)
    assert await db.get_verdict(FakePool(), "v", db.DEV_USER_ID) is None

    await db.bump_served(pool, "v", NOW)
    kind, sql, args = pool.calls[-1]
    assert "served_count = served_count + 1" in sql and "last_served_at = $2" in sql
    assert args == ("v", NOW)


@pytest.mark.asyncio
async def test_create_db_pool_is_bounded_and_never_logs_credentials(monkeypatch, caplog):
    seen = {}

    async def fake_create_pool(**kw):
        seen.update(kw)
        return "pool"

    monkeypatch.setattr(asyncpg, "create_pool", fake_create_pool)
    monkeypatch.setattr(db.settings, "database_url",
                        "postgresql+asyncpg://tf_user:hunter2@postgres:5432/tradingfirm_dev")
    with caplog.at_level("INFO"):
        assert await db.create_db_pool(timeout=1.5) == "pool"
    assert seen["dsn"] == "postgresql://tf_user:hunter2@postgres:5432/tradingfirm_dev"
    assert seen["timeout"] == 1.5 and seen["command_timeout"] == 30
    assert "hunter2" not in caplog.text and "postgres:5432/tradingfirm_dev" in caplog.text


def test_get_verdict_unparseable_json_reads_as_none():
    assert db._decode("{not json") is None and db._decode(None) is None


# ── Part 4.5: the journal's queries ──────────────────────────────

@pytest.mark.asyncio
async def test_due_verdicts_query_and_decoding():
    since = datetime(2026, 7, 23, tzinfo=timezone.utc)
    row = {"id": "11111111-1111-4111-8111-111111111111", "ticker": "AAPL", "asked_at": NOW,
           "entry": Decimal("338.95"), "plan_proposed": json.dumps({"stop": 1}), "scored": [5, 1]}
    pool = FakePool({"FROM ai.verdicts v": [row]})
    got = await db.due_verdicts(pool, db.DEV_USER_ID, since)
    assert got == [{**row, "plan_proposed": {"stop": 1}, "scored": [1, 5]}]
    (_, sql, args), = pool.calls
    assert args == (db.DEV_USER_ID, since)
    assert "HAVING count(o.verdict_id) < 3" in sql and "ORDER BY v.asked_at ASC" in sql


def _outcome(h):
    return {"verdict_id": "11111111-1111-4111-8111-111111111111", "horizon_days": h,
            "return_pct": Decimal("1.000"), "mae_pct": Decimal("-2.000"), "mfe_pct": Decimal("3.000"),
            "stop_hit": None, "target_hit": None, "session_date": date(2026, 9, 22),
            "first_hit": None, "r_multiple": None, "ask_session_bars": 1}


@pytest.mark.asyncio
async def test_insert_on_conflict_does_nothing():
    """The statement itself carries DO NOTHING, and a conflict counts 0."""
    answers = iter(["INSERT 0 1", "INSERT 0 0"])
    pool = FakePool({"INSERT INTO ai.verdict_outcomes": lambda args: next(answers)})
    assert await db.insert_outcomes(pool, [_outcome(1), _outcome(5)]) == 1
    sql = pool.calls[0][1]
    assert "ON CONFLICT (verdict_id, horizon_days) DO NOTHING" in sql
    assert "UPDATE" not in sql, "an outcome row is never updated"
    assert pool.calls[1][2] == tuple(_outcome(5)[c] for c in db.OUTCOME_COLUMNS)
    assert pool.tx_open == 1, "one ticker, one transaction"
    assert await db.insert_outcomes(pool, []) == 0


@pytest.mark.asyncio
async def test_store_failure_rolls_back_every_row_of_the_ticker():
    pool = FakePool(raise_on="INSERT INTO ai.verdict_outcomes")
    with pytest.raises(asyncpg.PostgresError):
        await db.insert_outcomes(pool, [_outcome(1), _outcome(5)])
    assert pool.calls == []
