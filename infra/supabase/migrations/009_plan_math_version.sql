-- ============================================================
-- 009 — Which plan-math rules built a verdict (Part 4.8a)
--
-- 007 is applied on prod and is never edited (tests pin its SHA-256), so
-- the one column 4.8a needs arrives here:
--   plan_math_version   grading.plan_math.PLAN_MATH_VERSION at the time the
--                       row was written. NULL = 1: every row before 4.8a
--                       (no backfill UPDATE; readers COALESCE to 1).
-- Journal stats split every rate by model × plan_math_version, so the
-- T1 / target-cap / far-support rules of v2 are never averaged with v1.
--
-- Nullable, re-runnable: ADD COLUMN IF NOT EXISTS is one statement
-- (scripts/migrate.sh header).
-- ============================================================

ALTER TABLE ai.verdicts ADD COLUMN IF NOT EXISTS plan_math_version SMALLINT
    CHECK (plan_math_version >= 1);
