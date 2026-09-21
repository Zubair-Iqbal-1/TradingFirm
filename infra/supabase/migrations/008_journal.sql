-- ============================================================
-- 008 — Journal: what 007's ai.verdict_outcomes lacks (Part 4.5)
--
-- 007 is applied on prod and is never edited (tests pin its SHA-256), so
-- the columns the journal scorer needs arrive here:
--   session_date      the XNYS session N the row was scored on
--   first_hit         which the window touched first: stop, target, or both
--                     on one bar (daily bars cannot order them; stop first)
--   r_multiple        R at the horizon, the stop always honoured
--   ask_session_bars  day 0's hourly bars used after the ask; 0 is valid
--                     (asked after the last hourly start), NULL = asked
--                     outside a session
-- and it widens 007's horizon CHECK from (1, 5, 20) to (1, 5, 20, 30, 60).
--
-- All nullable: the table holds no rows when this applies (nothing wrote it
-- before 4.5), and a plan-less verdict leaves first_hit / r_multiple NULL.
-- Re-runnable: ADD COLUMN IF NOT EXISTS skips a column, and its CHECK with
-- it, when the column is already there (scripts/migrate.sh header). The
-- horizon CHECK is dropped and re-added in ONE ALTER TABLE statement:
-- migrate.sh runs a file without a transaction, so two statements could
-- leave the table with no CHECK if the second failed. The name is 007's
-- generated one, read from prod on 2026-09-22.
-- ============================================================

ALTER TABLE ai.verdict_outcomes ADD COLUMN IF NOT EXISTS session_date DATE;

ALTER TABLE ai.verdict_outcomes ADD COLUMN IF NOT EXISTS first_hit TEXT
    CHECK (first_hit IN ('stop', 'target', 'same_bar'));

ALTER TABLE ai.verdict_outcomes ADD COLUMN IF NOT EXISTS r_multiple NUMERIC(8,3);

ALTER TABLE ai.verdict_outcomes ADD COLUMN IF NOT EXISTS ask_session_bars SMALLINT
    CHECK (ask_session_bars >= 0);

ALTER TABLE ai.verdict_outcomes
    DROP CONSTRAINT IF EXISTS verdict_outcomes_horizon_days_check,
    ADD CONSTRAINT verdict_outcomes_horizon_days_check
        CHECK (horizon_days IN (1, 5, 20, 30, 60));
