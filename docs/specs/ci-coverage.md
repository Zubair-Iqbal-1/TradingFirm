# Spec — CI and coverage

Status: **approved 2026-09-17** with five changes (below), corrected to what shipped. Base: `95f377d`.

## Spec (G1)

A GitHub Actions workflow, `.github/workflows/tests.yml`, runs on every push and pull request: it builds the `dev` stage of `services/data-engine` and `services/risk-shield` and runs each suite inside that image with an explicit `tests/test_*.py` list, the source and `infra/supabase/migrations` mounted read-only as compose mounts them, no network and no secrets. Any failed **or skipped** test fails the job. `pytest-cov==7.1.0` is added to both `requirements-dev.txt` and the suites run with `--cov=. --cov-report=term-missing`, with a per-service `.coveragerc` omitting `tests/` so TOTAL is application code, and no threshold yet.

### Approval changes (2026-09-17)

1. Decision 1 → (a): CI runs **all 17** data-engine test files (452 tests); CLAUDE.md copies the list and says the workflow is the reference list from now on.
2. Decision 2 → (a): a separate `fix:` commit first, same method as `2325d49`, plus a grep of both suites for hard-coded dates compared against the real clock, every hit listed as frozen / not needed / deferred.
3. Coverage uses `.coveragerc` `omit = tests/*` per service instead of a bare `--cov=.`, so TOTAL is app code only.
4. `-rs` stays **and** the workflow fails on skips > 0 (grep the pytest summary line for `skipped`, exit 1).
5. Push to `main` once both twins are green with `--cov`; that push is not a prod action.

## Verified before writing this spec

On 2026-09-17, in throwaway `docker run --rm --network none` containers from the local dev images (`/app` read-only, no compose env):

| Run | data-engine | risk-shield |
|---|---|---|
| bare (no env, no `/migrations`) | 447 passed, **5 failed** | 849 passed, **8 skipped** |
| + `/migrations` mount (+ twin's non-secret env for risk-shield) | 449 passed, **3 failed** | **857 passed, 0 skipped** |
| inside the running `tf-data-engine-dev` twin, `test_dossier.py` only | **the same 3 failed** | — |

- **No test opens a Postgres or Redis socket.** Both suites pass with `--network none`: `db.asyncpg.create_pool` is patched, Redis is `tests/fake_redis.py`, HTTP is `respx`.
- `pytest-cov` latest on PyPI is **7.1.0** (`requires_python >=3.9`, `pytest>=7`, `coverage[toml]>=7.10.6`), compatible with `pytest==9.1.1`.

## What CI needs that the twins get from compose

| Twin gets from compose | Needed by tests? | CI handling |
|---|---|---|
| Postgres (`tradingfirm_dev`) | **No** — every pool is faked | none; no service container |
| Redis DB 1 | **No** — `FakeRedis` everywhere | none; no service container |
| source mount `./services/<svc>:/app` | **Yes** — `dev` stage has no `COPY . .`, `.dockerignore` drops `tests/` | `-v "$GITHUB_WORKSPACE/services/<svc>:/app:ro"` |
| `./infra/supabase/migrations:/migrations:ro` | **Yes** (tests below) | same read-only mount |
| twin env (`SERVICE_NAME`, empty keys, `HEALTH_CHANNEL`, …) | **Yes** — three risk-shield guards skip without it | the twin's **non-secret** values with `-e`, copied from `docker-compose.yml`; keys set empty; no `secrets.*` |

### Tests that depend on the twin (mount or env, never a running DB)

- **`/migrations` mount** — without it these fail (data-engine) or skip (risk-shield):
  - data-engine: `test_edgar.py::test_migration_004_shape`, `test_finnhub_fetchers.py::test_migration_003_news_ticker_not_null_unique`
  - risk-shield: the 5 tests in `test_migration.py` (005 ×4, 006 ×1)
- **`SERVICE_NAME=risk-shield-dev` + overrides** — skip otherwise:
  - `test_config.py::test_twin_never_calls_prod_ai_agent`
  - `test_alert_throttle.py::test_twin_never_publishes_on_prod_channel`
  - `test_market_news.py::test_twin_never_ingests_into_prod_data_engine`

## Workflow as shipped

- `on: push, pull_request`; `permissions: contents: read`; one matrix job over `data-engine` / `risk-shield`, `fail-fast: false`, 20 min timeout.
- Steps: checkout → `docker build --target dev` → `docker run --rm --network none` with `PYTHONDONTWRITEBYTECODE=1`, `COVERAGE_FILE=/tmp/.coverage`, the twin env, both read-only mounts, `pytest <list> -v -rs -p no:cacheprovider --cov=. --cov-report=term-missing | tee pytest.log` under `pipefail`; the step prints the suite wall-clock and exits with pytest's code → **skip check** (the last summary line must exist and must not contain `skipped`) → job summary: the 4 lines (collected, summary line, `TOTAL`, wall-clock) go to `$GITHUB_STEP_SUMMARY` and to a `::notice` annotation, because job logs need a GitHub login and annotations show on the run page without one. `actions/checkout@v5` (v4 drew a Node 20 deprecation warning on run #1).
- Not in the workflow: API keys or `secrets.*`, any compose file, `*_live.py`, `full_scan_test.py`, `record_*`, `smoke_test_*`, `dry_run_*`.
- **Verified locally before push** by replaying the workflow's parsed steps (fresh `:ci` images): data-engine 452 passed / 83 %, risk-shield 857 passed / 97 %; removing the migrations mount → 5 skipped → skip step exit 1; forcing the prod `HEALTH_CHANNEL` → 1 failed → run step exit 1, wall-clock still printed.

## Clock audit (approval change 2)

Method: grep both suites for `date.today`, `datetime.now`, `utcnow`, `time.time()` and `TODAY` / `NOW` / `T0` constants, then run both suites under `time-machine` travel (throwaway image, `--network none`) at 2026-09-09, 2026-09-17, 2026-10-01…10-12, 2026-12-23 and 2027-09-15 to prove which hits compare against the real clock. At 2026-09-09 both suites are fully green, so the clock was the only cause of the 3 failures.

| Hit | Verdict | Why |
|---|---|---|
| data-engine `test_dossier.py` `NOW` / `TODAY` in the endpoint tests (all 16 `_get()` callers via `app_state`) | **frozen** (`610cd67`) | `app_state` patches `DossierContext.now_utc` to `NOW`; fixed the 3 failures, and `test_dossier_camelcase_shape`'s own patch moved into the fixture |
| data-engine `test_dossier.py::test_caps_applied_and_flagged`, `::test_filings_block_flag_flags_truncated` | **deferred** (G3.5), then fixed by `f75f16a` (`docs/specs/dossier-clock.md`) | pass today, **fail from ~2026-10-01**. Root cause is in app code: `dossier/assemble.py` calls `recent_filings` (and `company_news`, `earnings_calendar`) without `today=`, so they read the real clock even when `ctx.now` is injected; the fixture filings dated `TODAY − i` leave the 30-day window. Not fixable by a test-only freeze |
| data-engine `TODAY` in `test_earnings_dates.py`, `test_edgar.py`, `test_finnhub_fetchers.py`, `test_earnings_reaction.py` | not needed | passed explicitly as `today=`; green under every travel date |
| data-engine `datetime.now` in `test_news_market.py`, `test_scanner_pipeline.py`, `test_indicators_endpoint.py` | not needed | relative to the real clock on both sides, no fixed date |
| risk-shield `NOW` / `T0` in `test_scoring`, `test_alert_throttle`, `test_fred_view`, `test_monitors_data`, `test_macro_brief_flow`, `test_macro_brief_endpoints`, `test_weekend_inputs`, `test_market_news`, `test_wallclock` | not needed | injected into the code under test; green under every travel date |
| risk-shield `test_calendar.py` `TODAY = 2026-09-10` | not needed | injected; green at 2026-12-23, past the 2026-12-17 coverage-short date |
| risk-shield `datetime.now` in `test_market_endpoints.py`, `test_weekend_row.py`, `test_wallclock.py:126` | not needed | relative to the real clock, no fixed date |

## Writes / failure branches (G1.5)

Not applicable: no tables, keys or caches.

## Commits

1. `fix: freeze the dossier endpoint clock in the app_state fixture` (`610cd67`)
2. `feat: CI workflow and pytest-cov`: the workflow, `.coveragerc` ×2, both `requirements-dev.txt`, `.gitignore` (`.coverage`), CLAUDE.md lists + reference line + `--cov`, `docs/overview.md`, `docs/progress.md`, this spec.
3. `docs:` commit recording run [#1](https://github.com/Zubair-Iqbal-1/TradingFirm/actions/runs/35200317643) (green): data-engine 452 passed / 0 skipped / 83 % / 20 s, risk-shield 857 passed / 0 skipped / 97 % / 19 s, plus the annotation step and `actions/checkout@v5`.
