-- ============================================================
-- 007 — AI: verdicts, outcomes, judgments, the LLM call ledger, and
--       users.settings (Part 4.4)
--
-- Numbered 007, not the plan's 005: 005_risk.sql and
-- 006_macro_brief_output.sql shipped with Parts 3.1 and 3.6a, and
-- migrate.sh keys public.schema_migrations by filename.
--
-- Re-runnable: IF NOT EXISTS on every object, and the one seed is
-- INSERT ... ON CONFLICT DO NOTHING (scripts/migrate.sh header;
-- docs/decisions.md 2026-09-05). Depends on 001 for users.profiles.
--
-- No foreign key leaves the ai / users schemas: macro_brief_id names a
-- risk.macro_briefs row and position_id a signals.positions row (which
-- does not exist yet), both as plain UUIDs. Services share no tables.
--
-- 001's four unused ai.* tables (grades, accuracy_logs, reports,
-- patterns) are left alone.
-- ============================================================

CREATE SCHEMA IF NOT EXISTS ai;
CREATE SCHEMA IF NOT EXISTS users;

-- ── users.settings ──────────────────────────────────────────
-- account_size is NULL on purpose: this repository is public and an
-- account size never goes into a migration. It is set by hand after this
-- file is applied (docs/runbook.md); until then POST /analyze answers 409.
CREATE TABLE IF NOT EXISTS users.settings (
    user_id             UUID PRIMARY KEY REFERENCES users.profiles(id) ON DELETE CASCADE,
    account_size        NUMERIC(14,2) CHECK (account_size > 0),
    risk_per_trade_pct  NUMERIC(5,2) NOT NULL DEFAULT 1.0
                        CHECK (risk_per_trade_pct > 0 AND risk_per_trade_pct <= 10),
    default_temperament TEXT,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The single fixed development user (D18), until auth exists.
INSERT INTO users.profiles (id, display_name)
VALUES ('00000000-0000-4000-8000-000000000001', 'dev')
ON CONFLICT (id) DO NOTHING;

INSERT INTO users.settings (user_id, account_size, risk_per_trade_pct)
VALUES ('00000000-0000-4000-8000-000000000001', NULL, 1.0)
ON CONFLICT (user_id) DO NOTHING;

-- ── ai.verdicts ─────────────────────────────────────────────
-- One row per verdict the model gave. `dossier` is the full document
-- data-engine answered with; `prompt_inputs` is the exact projection the
-- model saw (D5: scored later against the inputs it actually had).
-- `entry` is always the resolved number, given or the last daily close.
-- A cache-served analyze writes no row: it bumps served_count instead.
CREATE TABLE IF NOT EXISTS ai.verdicts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL,
    ticker          TEXT NOT NULL,
    horizon         TEXT NOT NULL,
    asked_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    entry           NUMERIC(12,2) NOT NULL CHECK (entry > 0),
    entry_source    TEXT NOT NULL CHECK (entry_source IN ('given', 'last_close')),
    dossier         JSONB NOT NULL,
    prompt_inputs   JSONB NOT NULL,
    prompt_sha      TEXT NOT NULL,
    fingerprint     TEXT NOT NULL,
    macro_brief_id  UUID,
    regime          TEXT,
    verdict         TEXT NOT NULL CHECK (verdict IN ('go', 'wait', 'avoid')),
    confidence      INTEGER NOT NULL CHECK (confidence >= 0 AND confidence <= 100),
    reasoning       TEXT NOT NULL,
    thesis          JSONB NOT NULL,
    thesis_breakers JSONB NOT NULL,
    risk_flags      JSONB NOT NULL DEFAULT '[]'::jsonb,
    plan_proposed   JSONB,
    plan_rejection  JSONB,
    model           TEXT NOT NULL,
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    served_count    INTEGER NOT NULL DEFAULT 0,
    last_served_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_verdicts_user_ticker_asked
    ON ai.verdicts(user_id, ticker, asked_at DESC);

-- ── ai.verdict_outcomes (written by Part 4.5) ───────────────
CREATE TABLE IF NOT EXISTS ai.verdict_outcomes (
    verdict_id      UUID NOT NULL REFERENCES ai.verdicts(id) ON DELETE CASCADE,
    horizon_days    INTEGER NOT NULL CHECK (horizon_days IN (1, 5, 20)),
    return_pct      NUMERIC(8,3),
    mae_pct         NUMERIC(8,3),
    mfe_pct         NUMERIC(8,3),
    stop_hit        BOOLEAN,
    target_hit      BOOLEAN,
    scored_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (verdict_id, horizon_days)
);

-- ── ai.judgments (written by Phase 6) ───────────────────────
CREATE TABLE IF NOT EXISTS ai.judgments (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    position_id     UUID NOT NULL,
    triggered_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    trigger         TEXT NOT NULL,
    inputs          JSONB NOT NULL DEFAULT '{}'::jsonb,
    answer          TEXT NOT NULL CHECK (answer IN ('hold', 'tighten', 'partial', 'exit')),
    new_stop        NUMERIC(12,2),
    confidence      INTEGER CHECK (confidence >= 0 AND confidence <= 100),
    reasoning       TEXT
);

CREATE INDEX IF NOT EXISTS idx_judgments_position ON ai.judgments(position_id, triggered_at DESC);

-- ── ai.llm_calls — the permanent cost ledger ────────────────
-- One row per LLM request that reached the wire, whatever it answered,
-- and nothing else. Redis stays the live counter; this is the record a
-- `docker compose down` cannot erase, and what re-seeds the daily caps at
-- startup. `counters` names the day counters the call counted against.
-- cost_usd NULL = the gateway reported none, never 0.
CREATE TABLE IF NOT EXISTS ai.llm_calls (
    id                  BIGSERIAL PRIMARY KEY,
    called_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    et_day              DATE NOT NULL,
    user_id             UUID,
    ticker              TEXT,
    route               TEXT NOT NULL CHECK (route IN ('analyze', 'classify')),
    label               TEXT NOT NULL,
    model               TEXT NOT NULL,
    host                TEXT,       -- the OpenRouter host that served it; NULL = not reported
    tokens_in           INTEGER,
    tokens_out          INTEGER,
    tokens_reasoning    INTEGER,
    cache_read_tokens   INTEGER,
    cache_write_tokens  INTEGER,
    cost_usd            NUMERIC(12,6),
    outcome             TEXT NOT NULL CHECK (outcome IN
                        ('ok', 'rate_limited', 'unavailable', 'rejected',
                         'refused', 'bad_response', 'auth_failed')),
    counters            TEXT[] NOT NULL DEFAULT '{}',
    verdict_id          UUID REFERENCES ai.verdicts(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_llm_calls_et_day ON ai.llm_calls(et_day);
