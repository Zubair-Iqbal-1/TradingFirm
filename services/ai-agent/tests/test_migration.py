"""Part 4.4 — 007_ai.sql, asserted as file text only (risk-shield's 3.1
pattern). The real apply-and-rerun is scripts/dev-db.sh, an acceptance step.
The file arrives through the read-only /migrations mount on the dev twin."""

import os
import re

import pytest

import db

MIGRATIONS_DIR = os.environ.get("MIGRATIONS_DIR", "/migrations")
FILENAME = "007_ai.sql"


@pytest.fixture(scope="module")
def sql():
    path = os.path.join(MIGRATIONS_DIR, FILENAME)
    if not os.path.exists(path):
        pytest.skip(f"{path} not mounted (run inside tf-ai-agent-dev)")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    # Statements only: an assertion about what the file does must not read
    # the prose explaining it.
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("--"))


def test_migration_007_is_rerunnable(sql):
    assert len(re.findall(r"CREATE TABLE IF NOT EXISTS", sql)) == \
        len(re.findall(r"CREATE TABLE", sql)) == 5
    assert len(re.findall(r"CREATE INDEX IF NOT EXISTS", sql)) == len(re.findall(r"CREATE INDEX", sql))
    assert len(re.findall(r"CREATE SCHEMA IF NOT EXISTS", sql)) == len(re.findall(r"CREATE SCHEMA", sql))
    inserts = re.findall(r"INSERT INTO[^;]+;", sql)
    assert len(inserts) == 2 and all("ON CONFLICT" in i and "DO NOTHING" in i for i in inserts)
    without_fk_actions = re.sub(r"ON DELETE (CASCADE|SET NULL)", "", sql)
    assert not re.search(r"\b(DROP|TRUNCATE|DELETE|ALTER|UPDATE)\b", without_fk_actions)


def test_migration_007_has_no_cross_schema_fk(sql):
    refs = re.findall(r"REFERENCES\s+([a-z_]+)\.([a-z_]+)", sql)
    assert refs and all(schema in ("ai", "users") for schema, _ in refs), refs
    assert re.search(r"macro_brief_id\s+UUID,", sql)
    assert re.search(r"position_id\s+UUID NOT NULL,", sql)


def test_migration_007_never_carries_an_account_size(sql):
    """The repo is public. The seed is NULL; the number is set by hand."""
    assert re.search(r"VALUES \('00000000-0000-4000-8000-000000000001', NULL, 1\.0\)", sql)
    assert db.DEV_USER_ID in sql
    assert re.search(r"account_size\s+NUMERIC\(14,2\) CHECK", sql), "nullable"


def test_migration_007_matches_the_columns_db_py_writes(sql):
    # plan_math_version arrives in 009 (test_migrations_hold_every_verdict_column_db_py_writes)
    later = {"plan_math_version"}
    for table, columns in (("ai.verdicts", db.VERDICT_COLUMNS), ("ai.llm_calls", db.LLM_CALL_COLUMNS)):
        body = re.search(rf"CREATE TABLE IF NOT EXISTS {re.escape(table)} \((.*?)\n\);", sql, re.S).group(1)
        for column in (c for c in columns if c not in later):
            assert re.search(rf"^\s+{column}\s+\S", body, re.M), f"{table}.{column}"
    verdicts = re.search(r"ai\.verdicts \((.*?)\n\);", sql, re.S).group(1)
    assert "served_count    INTEGER NOT NULL DEFAULT 0" in verdicts
    assert re.search(r"last_served_at\s+TIMESTAMPTZ", verdicts)
    calls = re.search(r"ai\.llm_calls \((.*?)\n\);", sql, re.S).group(1)
    assert not re.search(r"^\s+cached\s", calls, re.M), "the ledger is wire calls only"
    for outcome in ("ok", "rate_limited", "unavailable", "rejected", "refused",
                    "bad_response", "auth_failed"):
        assert f"'{outcome}'" in calls


# ── Part 4.5: 008_journal.sql, and 007 frozen ────────────────────

# 007 is applied on prod (2026-09-21). A change to it would never reach prod
# (migrate.sh keys schema_migrations by filename), so it must never change:
# new columns go in a new file.
MIGRATION_007_SHA256 = "73e378642ef5ac686bb67f7b3bd31646f52ef85e38ee1404ca332853bfca19dd"


def _raw(filename):
    path = os.path.join(MIGRATIONS_DIR, filename)
    if not os.path.exists(path):
        pytest.skip(f"{path} not mounted (run inside tf-ai-agent-dev)")
    with open(path, "rb") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def sql008():
    text = _raw("008_journal.sql").decode("utf-8")
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("--"))


def test_migration_007_is_frozen():
    import hashlib
    assert hashlib.sha256(_raw(FILENAME)).hexdigest() == MIGRATION_007_SHA256


def test_migration_008_is_rerunnable(sql008):
    statements = [s.strip() for s in sql008.split(";") if s.strip()]
    assert len(statements) == 5
    for statement in statements[:4]:
        assert statement.startswith("ALTER TABLE ai.verdict_outcomes ADD COLUMN IF NOT EXISTS "), statement
        assert "NOT NULL" not in statement, "nullable: a plan-less verdict leaves columns NULL"
    # The horizon CHECK: dropped IF EXISTS and re-added in ONE statement, so a
    # rerun repeats it and a failure can never leave the table without it.
    widen = statements[4]
    assert widen.startswith("ALTER TABLE ai.verdict_outcomes")
    assert "DROP CONSTRAINT IF EXISTS verdict_outcomes_horizon_days_check," in widen
    assert "ADD CONSTRAINT verdict_outcomes_horizon_days_check" in widen
    rest = sql008.replace("DROP CONSTRAINT IF EXISTS verdict_outcomes_horizon_days_check", "")
    assert not re.search(r"\b(DROP|TRUNCATE|DELETE|UPDATE|INSERT|CREATE)\b", rest)


def test_migration_008_horizons_match_the_code(sql, sql008):
    """007 allowed (1, 5, 20) and stays as it is; 008 widens the CHECK to the
    five horizons the scorer uses, and the two lists must stay one."""
    from journal import sessions
    assert re.search(r"horizon_days\s+INTEGER NOT NULL CHECK \(horizon_days IN \(1, 5, 20\)\)", sql)
    allowed = re.search(r"CHECK \(horizon_days IN \(([\d, ]+)\)\)", sql008).group(1)
    assert tuple(int(h) for h in allowed.split(",")) == sessions.HORIZONS == (1, 5, 20, 30, 60)


def test_migration_008_adds_the_four_journal_columns(sql008):
    assert re.search(r"session_date DATE;", sql008)
    assert re.search(r"first_hit TEXT\s+CHECK \(first_hit IN \('stop', 'target', 'same_bar'\)\);", sql008)
    assert re.search(r"r_multiple NUMERIC\(8,3\);", sql008)
    assert re.search(r"ask_session_bars SMALLINT\s+CHECK \(ask_session_bars >= 0\);", sql008)


def test_migrations_hold_every_outcome_column_db_py_writes(sql, sql008):
    body = re.search(r"ai\.verdict_outcomes \((.*?)\n\);", sql, re.S).group(1)
    for column in db.OUTCOME_COLUMNS:
        assert re.search(rf"^\s+{column}\s+\S", body, re.M) or \
            re.search(rf"ADD COLUMN IF NOT EXISTS {column} ", sql008), column


@pytest.fixture(scope="module")
def sql009():
    text = _raw("009_plan_math_version.sql").decode("utf-8")
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("--"))


def test_migration_009_is_rerunnable(sql009):
    statements = [s.strip() for s in sql009.split(";") if s.strip()]
    assert len(statements) == 1
    assert statements[0].startswith("ALTER TABLE ai.verdicts ADD COLUMN IF NOT EXISTS plan_math_version SMALLINT")
    assert "NOT NULL" not in statements[0], "nullable: rows before 4.8a read as version 1"
    assert not re.search(r"\b(DROP|TRUNCATE|DELETE|UPDATE|INSERT|CREATE)\b", sql009), "no backfill"


def test_migration_009_adds_plan_math_version(sql009):
    from grading import plan_math
    assert re.search(r"plan_math_version SMALLINT\s+CHECK \(plan_math_version >= 1\);", sql009)
    assert plan_math.PLAN_MATH_VERSION >= 1


def test_migrations_hold_every_verdict_column_db_py_writes(sql, sql009):
    body = re.search(r"ai\.verdicts \((.*?)\n\);", sql, re.S).group(1)
    for column in db.VERDICT_COLUMNS:
        assert re.search(rf"^\s+{column}\s+\S", body, re.M) or \
            re.search(rf"ADD COLUMN IF NOT EXISTS {column} ", sql009), column
