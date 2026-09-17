# Spec — dossier fetchers take the context clock

Status: **approved 2026-09-17**, as drafted. Base: `0b2c067`. Follows the clock audit in `docs/specs/ci-coverage.md` (deferred item, option 1 chosen 2026-09-17).

## Spec (G1)

`dossier/assemble.py` passes `today=ctx.now_utc().date()` to the three context fetchers it calls (`company_news`, `earnings_calendar`, `recent_filings`), so an injected `ctx.now` reaches every date window in the dossier, not just staleness, the events read and `generated_at`. In prod `ctx.now` is never set, so `ctx.now_utc().date()` is the same `datetime.now(timezone.utc).date()` each fetcher computes today when `today` is None. A new regression test pins the three `today` arguments, and the two deferred tests must pass with the clock moved to every quarter through 2027-09.

## Root cause

`test_caps_applied_and_flagged` and `test_filings_block_flag_flags_truncated` build `_ctx(now=NOW)` (2026-09-09) with fixture filings dated `TODAY − i`. `build_filings` calls `recent_filings` without `today`, which falls back to `datetime.now(timezone.utc).date()` (`providers/context/edgar.py:297`). The 30-day `filing_days` window then drops the fixture rows once the real date passes ~2026-10-01. Time travel showed the first failure on 2026-10-01 and both on every date after.

## The three call sites

| # | `dossier/assemble.py` | Before | After |
|---|---|---|---|
| 1 | `build_news`, line 304 | `company_news(ctx.finnhub, ticker, redis=ctx.redis, days=profile["news_days"])` | `…, days=profile["news_days"], today=ctx.now_utc().date())` |
| 2 | `build_events`, line 349 | `earnings_calendar(ctx.finnhub, ticker, redis=ctx.redis)` | `earnings_calendar(ctx.finnhub, ticker, redis=ctx.redis, today=ctx.now_utc().date())` |
| 3 | `build_filings`, line 418 | `recent_filings(ctx.edgar, ticker, forms=…, days=…, redis=ctx.redis)` | `…, redis=ctx.redis, today=ctx.now_utc().date())` |

What each `today` controls:
1. The `/company-news` `from`/`to` query window.
2. The `/calendar/earnings` `from`/`to` window.
3. The 30-day `filter_filings` cut, applied after the cache.

Nothing else changes. `earnings_surprises`, `recommendations` and `profile` take no date. `sync_context` and `sync_filings` already pass `today`.

## Why prod behaviour is unchanged (checked against the code)

- **Prod never sets `now`.** `main.py:713` builds `DossierContext(pool=…, redis=…, cooldowns=…, finnhub=…, edgar=…, av_client=…, provider=…, refresh=…)` with no `now`. `DossierContext.now_utc` (`assemble.py:159`) is `return self.now or datetime.now(timezone.utc)`, so in prod the new argument is `datetime.now(timezone.utc).date()`.
- **The fallback is the same expression.** Each fetcher's `today = today or datetime.now(timezone.utc).date()` (`finnhub.py:80`, `finnhub.py:111`, `edgar.py:297`) takes the same value, read a few microseconds earlier.
- **The only observable edge is a request that straddles 00:00 UTC.** Before, each fetcher read the date as it started; after, each reads it at its own `ctx.now_utc()` call, which is still per call site and still just before the fetch. Both variants pick "today" at essentially the same instant, and neither is more correct.
- **Cache keys don't include dates.** `finnhub_key(kind, ticker)` (`finnhub.py:65`) and `edgar_key(KIND_FILINGS, t)` (`edgar.py:317`) are unchanged, so no cache entries are orphaned or split.
- The report will show this with `git diff` of `assemble.py` (three argument additions, nothing else) next to the lines above.

## Tests

| Test | Proves |
|---|---|
| new `test_dossier.py::test_assemble_passes_ctx_today_to_fetchers` | With `_ctx(now=datetime(2027, 3, 17, 22, tzinfo=utc))` (a Wednesday after the close, like `NOW`, so the bars are not stale), the mocked `/company-news` `to` and `/calendar/earnings` `from`/`to` are derived from 2027-03-17, and a filing dated 2027-03-12 is kept by `build_filings`. This fails before the change. |
| `test_caps_applied_and_flagged`, `test_filings_block_flag_flags_truncated` | Unchanged. Must pass under time travel at 2026-09-17, 2026-10-01, 2027-01-01, 2027-04-01, 2027-07-01 and 2027-09-30. |
| full data-engine suite (17 files) | Twin run with `--cov` passes 453 (452 + the new test), 0 skipped. The same time-travel dates give 453 passed at each. |

Time travel uses the same throwaway `time-machine` image as the clock audit (`--network none`). It is not added to `requirements-dev.txt`.

## G1.5

Not applicable: nothing new is written. The fetchers' cache writes and keys are unchanged.

## Commits

1. `fix: pass the dossier context clock to the news, calendar and filings fetchers` (`assemble.py`, the new test, this spec, `docs/progress.md`).
2. Pushed to `main` (a push needs no go, 2026-09-17). The CI run link is recorded in `docs/progress.md`.

## Verified (2026-09-17)

- **The regression test fails without the fix**, with `assemble.py` stashed: `assert '2026-09-17' == '2027-03-17'` (the news `to` came from the real clock). It passes with the fix.
- **Twin, full suite (17 files) with `--cov`:** collected 453, 453 passed, 0 skipped. TOTAL 83 % (2912 stmts, 487 missed); `dossier/assemble.py` 96 %.
- **Time travel** (full suite at 15:00 UTC, `--network none`):

| Clock | Full suite | `caps_applied_and_flagged` | `filings_block_flag_flags_truncated` | `assemble_passes_ctx_today_to_fetchers` |
|---|---|---|---|---|
| 2026-09-17 | 453 passed | PASSED | PASSED | PASSED |
| 2026-10-01 | 453 passed | PASSED | PASSED | PASSED |
| 2027-01-01 | 453 passed | PASSED | PASSED | PASSED |
| 2027-04-01 | 453 passed | PASSED | PASSED | PASSED |
| 2027-07-01 | 453 passed | PASSED | PASSED | PASSED |
| 2027-09-30 | 453 passed | PASSED | PASSED | PASSED |

Before the fix, the first two tests failed from 2026-10-01 (`docs/specs/ci-coverage.md`, clock audit).
