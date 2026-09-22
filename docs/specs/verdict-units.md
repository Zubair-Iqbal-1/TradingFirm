# Spec — Fix: verdict prompt inputs carry their units

Status: **approved** 2026-09-22 with two edits, marked *(approval)*: decision 5 keeps the `truncated` clause; decision 4 also pins `profile.marketCap`'s source and unit on the data-engine side. Base `1a00fb2`. No migration. Written 2026-09-22 after a read-only pass over what `/analyze` gives the model.

**Read for this part:** `CLAUDE.md`, `.agents/AGENTS.md`, spec 4.4 (decisions 9, 12, D5), ai-agent `analyze.py` (`project`, `fingerprint`, `prompt_sha`), `analyst.py` steps 9–13, `prompts/verdict.md`, `tests/test_verdict_prompt.py`, `tests/test_analyze_cache.py`; data-engine `indicators/snapshot.py`, `indicators/models.py` (`IndicatorsResponse`), `dossier/assemble.py:405–420`.

## The defect

Prod, 2026-09-22 04:58 ET, `POST /analyze/GOOGL` → verdict `8dee4d5b-1fe4-4cf8-9f59-ad9aa683e08c`, `wait` 52, entry 354.97, plan intact. The stored answer says, in `reasoning` and again in `risk_flags`:

> price is extended 6.9% above the 50EMA and 33% above 20EMA

The stored `prompt_inputs->'indicators'` it read: `ext20: 1.3349`, `ext50: 1.069`. Those are ATR multiples, `(close − EMA) ÷ ATR14` (`indicators/volatility.py:69`), not percentages. The real distances are 3.14 % above EMA20 (344.16) and 2.50 % above EMA50 (346.31). The model was never told the unit: `verdict.md`'s "What you receive" describes `indicators` in one line with no units, and the projection passes data-engine's key names through unchanged. Text only: no level came from the model (`analyze.llm_schema` has no numeric field). The row stays and will be journal-scored.

## Verified before writing this spec

Read on 2026-09-22. No network call, no LLM call, one read-only query on prod Postgres (`ai.verdicts` row `8dee4d5b`), nothing written.

| # | claim | evidence |
|---|---|---|
| X1 | `prompt_sha` = sha256 of the system text only; the current `verdict.md` hashes to `ea3d5f5e2c2f583d`, GOOGL's stored `prompt_sha` | `analyze.prompt_sha`, `analyst.py:204`, `python3 -c` over the file |
| X2 | The fingerprint has no projection version: a change to `project()` alone leaves every cached verdict servable for the cache's life | `analyze.fingerprint` basis keys |
| X3 | The projection is pass-through: `dict(sections["indicators"])` minus 7 noisy keys. A new data-engine field reaches the model unlabelled | `analyze.py:project` |
| X4 | Finnhub `profile2.marketCapitalization` is in **millions USD**: GOOGL stored `4341283.1`, AAPL `4939254.2452` | `assemble.py:416`, the two rows |
| X5 | The projection test's fixture gives `marketCap: 3.1e12` (raw dollars): it encodes the wrong unit | `tests/test_verdict_prompt.py:41` |
| X6 | `rsSpy*`, `rsSector*`, `sector` null because `benchmarks.spy.bars = 0` and `benchmarks.sector.ticker = null` | the row's `indicators.benchmarks` |
| X7 | The sector name comes from `data_engine.stocks.sector`, which only the scanner writes (from yfinance `Ticker.info`); the ETF map covers the 11 yfinance names. A hand-picked ticker with no `stocks` row has no sector, whatever ETF bars exist | `dossier/sections.py:57–60`, `indicators/sectors.py` |
| X8 | A cache hit is served from Redis + Postgres and makes no LLM call, so `cacheRead` is observable only on a call that reaches the wire: a second ticker inside 5 minutes, or `fresh=true` | `analyst.py:211–231`, spec 4.4 live table |
| X9 | The journal reads `entry` and `plan_proposed`, never `prompt_inputs`; renaming projection keys does not touch scoring of the three stored rows | `journal/`, spec 4.5 decisions 6–7 |
| X10 | 4.7's row groups past verdicts by "category flags + regime + extension bucket", i.e. it will read `prompt_inputs` keys. Key names are a contract from this fix on | plan row 4.7 |

## What the model receives, field by field (GOOGL, row `8dee4d5b`)

Every key in the projected document, with its real unit and whether the model can tell. **U** = unlabelled and misreadable, **A** = ambiguous or noisy, **N** = null for GOOGL, ok = the name carries the unit or it is a plain price level.

| section | key | value | real unit / meaning | status |
|---|---|---|---|---|
| indicators | `ext20`, `ext50` | 1.3349, 1.069 | ATR14 multiples | **U** — the defect |
| indicators | `pos52w` | 0.7162 | 0–1 fraction of the 52-week range | **U** |
| indicators | `atr14` | 8.0976 | USD, a daily range not a level | **U** |
| indicators | `avgDollarVolume20` | 8870150814.2 | USD per session | **U** |
| indicators | `rvol` | 1.196 | ratio, today ÷ 20-day mean | **U** (conventional name, still unstated) |
| indicators | `rsSpy5`, `rsSpy20`, `rsSector5`, `rsSector20` | null | percentage points, stock return − benchmark return | **U** and **N** |
| indicators | `macd`, `macdSignal`, `macdHist` | 0.6979, −1.3108, 2.0088 | USD (EMA differences) | **A** |
| indicators | `rsi14` | 58.897 | 0–100 | **A** (conventional) |
| indicators | `gapPct` | 0.3147 | percent | ok |
| indicators | `close`, `ema20`, `ema50`, `ema200` | 354.97, 344.16, 346.31, 327.46 | USD price levels | ok |
| indicators | `zones[].low/high/price` | prices | USD; `price` is the midpoint | ok |
| indicators | `zones[].score` | 75, 65, 20 | additive 0–100 (30/25/20/15 per method) | **A** |
| indicators | `zones[].tests`, `recent`, `methods`, `volumeNode` | 7, true, […], false | touch count, bool, method names, bool | ok |
| indicators | `benchmarks` | spy bars 0, sector ticker null | why the RS fields are null | ok, and the evidence for X6 |
| indicators | `ticker`, `asOf` | GOOGL, 2026-09-21 | duplicate the top-level keys | **A** (noise) |
| indicators | `sector` | null | yfinance sector name | **N** (X7) |
| profile | `marketCap` | 4341283.1 | **millions** USD (X4); reads as $4.3 M | **U** |
| profile | `name`, `industry` | Alphabet Inc, Media | Finnhub strings | ok |
| macro | `score` | 74 | health score 0–100 | **A** |
| macro | `overlay.movePct` | −0.2916 | percent, futures move since settle | ok |
| macro | `overlay.cap`, `brief`, `briefId`, `weekend` | null | not capped; no 4.6 yet; weekday | **N**, expected |
| earnings | `inDays` | 35 | calendar days | ok |
| earnings | `reactions[].gapPct`, `closeToClosePct`, `surprisePct` | −6.13, −7.13, 214.23 | percent | ok |
| earnings | `reactions[].epsEstimate`, `epsReported` | 2.9, 9.11 | USD per share | **A** |
| plan | `entry`, `stop`, `disasterLine`, `targets[].price`, `riskPerShare` | prices | USD | ok |
| plan | `targets[].r`, `bestR` | 1.14 … 1.78 | R multiples | ok |
| plan | `stopBasis` | "support zone 348.322417578686-351.37…, low … - 1xATR 8.097606317434838" | free text from plan math, the only place the model sees more than 4 dp | **A** |
| events | `sentiment`, `relevance`, `sources` | −1..1, high/medium/low, count | described in the prompt | ok |
| events | (content) | 15 events, 8 not about Google; a Meta price-target story is `relevance: high` for GOOGL | classifier relevance, not a unit | **A**, out of scope |
| filings | `form`, `filedOn` | ten Form 4 rows, all 2026-09-16 | the 10-row cap ate anything else | ok (cap), noted |
| dataQuality | `news`, `filings` | "truncated" | means capped at 30 / 10, not missing; the model flagged it as "limiting full picture" | **A** — the prompt lists "truncated" beside "missing" |
| recommendations | `strongBuy` … `period`, `symbol` | counts | counts; `symbol` duplicates the ticker | ok (noise) |
| top level | `entry`, `entrySource`, `today`, `asOf`, `horizon` | | | ok |

## Spec

This fix labels every unitless number the verdict model reads: the projection renames the misreadable indicator keys with a unit suffix and adds two code-computed percent distances from the EMAs, `verdict.md`'s "What you receive" section states the unit convention in one legend line, and the cache fingerprint gains a projection version so a projection-only change can never serve a verdict built on an older document. It must not change any price level, the LLM schema, the plan, the ledger, the Redis keys or any stored row; it must not convert or compute anything the model could later take as a level; it must not add a migration; and it must not touch the null-benchmark data gap (X6, X7), which is a live action. Acceptance: (1) `test_every_projected_number_carries_its_unit` walks the projected `indicators` and `profile` and proves every numeric key ends in a unit suffix or is on the declared price-level / conventional list; (2) `test_projection_version_bump_changes_the_fingerprint` proves a projection version bump changes the fingerprint and `test_projection_version_bump_invalidates` proves the route makes a new call; (3) after deploy, the first of the ten held analyses quotes extension as an ATR multiple or as the correct percent, checked against its own stored `prompt_inputs.indicators`.

## Decisions

### 1. Where the unit label lives

**Option A — key suffixes, plus a one-line legend in `verdict.md` (recommended).** The renamed keys, all in the projection (`analyze.project`), nothing in data-engine:

| data-engine key | projected key | unit |
|---|---|---|
| `ext20`, `ext50` | `ext20Atr`, `ext50Atr` | ATR14 multiples |
| `pos52w` | `pos52wFrac` | 0–1 |
| `atr14` | `atr14Usd` | USD |
| `avgDollarVolume20` | `avgDollarVolume20Usd` | USD |
| `rsSpy5`, `rsSpy20`, `rsSector5`, `rsSector20` | `rsSpy5Pct`, `rsSpy20Pct`, `rsSector5Pct`, `rsSector20Pct` | percentage points |
| `profile.marketCap` | `profile.marketCapUsdM` | millions USD (X4), no arithmetic |
| (new, decision 2) | `aboveEma20Pct`, `aboveEma50Pct` | percent |

Unchanged, as price levels: `close`, `ema20`, `ema50`, `ema200`, `zones[].low/high/price`, every `plan` level. Unchanged, conventional and covered by the legend: `rsi14` (0–100), `rvol` (ratio), `macd` / `macdSignal` / `macdHist` (USD), `gapPct`, `zones[].score` (0–100), `macro.score` (0–100), `reactions[].epsEstimate` / `epsReported` (USD per share). The legend line in "What you receive": *"Units: a key ending `Atr` is a multiple of ATR14, `Pct` is percent, `Frac` is 0–1, `Usd` is dollars, `UsdM` is millions of dollars; `rsi14`, `score` fields are 0–100, `rvol` is a ratio, `macd*` are in dollars, and every other bare number is a price in dollars."*
- Why A: the label sits next to the number where the model reads it, not 2,000 tokens away in the system text. The stored `prompt_inputs` row becomes self-describing, which is what 4.7 needs (X10): a reader sees `ext20Atr` and needs no prompt version to interpret it. Suffixes follow the repo's existing `gapPct` / `movePct` convention.
- Cost: one rename ripple, now, before 4.7 exists. Three stored rows keep the old keys; `prompt_inputs.projectionVersion` (decision 3) tells them apart.

**Option B — legend in `verdict.md` only, keys unchanged.** Same legend line but written per key (`ext20` and `ext50` are ATR multiples, `pos52w` is 0–1, …). Keys stay identical to data-engine's, so nothing to rename and 4.7 reads data-engine names.
- Why not: the label is separated from the number, exactly the arrangement that failed; stored rows stay unlabelled and depend on which prompt version they were made under; every future data-engine field needs a legend edit or arrives unlabelled.

### 2. Code-computed percent distances: **in**

`aboveEma20Pct` = `(close − ema20) ÷ ema20 × 100`, `aboveEma50Pct` likewise, rounded to 2 dp, computed in `project()` from the projected `close` and EMAs; `null` when either is missing or the EMA is not > 0. The model reached for a percent and did the arithmetic wrong; the number from code removes the conversion. It is a projection field only: no prompt rule, no schema field, not a level (a percent, never a price), and it is not in the fingerprint (the fingerprint already has `asOf` and the ATR bucket). ATR extension stays: it is the strategy's own measure (plan §3) and what 4.7 buckets on.

### 3. Projection version in the fingerprint and in the document

`analyze.PROJECTION_VERSION = 2` (the shipped projection is 1). It joins the fingerprint basis as `"projection"`, and the projected document carries `"projectionVersion": 2` at the top level, so a stored `prompt_inputs` row states which key set it follows (absent = 1). No column, no migration: `prompt_sha` covers the system text, the version covers the document shape, and both are inside `fingerprint`. Bump it on every future change to `project()`'s keys; `test_projection_version_is_pinned` fails on a key change without a bump (it hashes the sorted key paths of the fixture projection).

### 4. The projection becomes an allowlist

`project()` selects indicator keys from one ordered map `INDICATOR_KEYS = {source: target}` (the table in decision 1 plus the unchanged names) instead of copying everything minus seven noisy keys (X3). A key data-engine adds later is dropped, not passed through unlabelled; `zones` and `benchmarks` are copied whole (their inner keys are pinned by the walk test). `indicators.ticker` and `indicators.asOf`, duplicates of the top-level keys, and `recommendations[].symbol` are dropped as noise. The contract is pinned on both sides like the other cross-service contracts: ai-agent's `test_indicator_keys_pinned_to_data_engine` holds the literal list of `IndicatorsResponse` aliases, and data-engine's `test_indicator_fields_pinned_for_ai_agent` (in `tests/test_indicators_endpoint.py`) holds the same list and names ai-agent's test. Change both or neither. *(approval)* The same data-engine test also pins `profile.marketCap`'s unit, which the suffix walk cannot see: `ProfileSection.market_cap` is documented as millions of USD, `build_profile` reads Finnhub's `marketCapitalization`, and the fetcher hits `/stock/profile2`, all named for ai-agent's `marketCapUsdM`. A later source that reports raw dollars (yfinance) fails this test until it converts.

### 5. `verdict.md` edits, and what they do to the cache

Two edits, both in "What you receive": the legend line (decision 1) and, on the `dataQuality` line, *"`truncated` means a section was cut at its cap (30 headlines, 10 filings), not that data is missing"*. *(approval)* The second stays: the same defect class, one clause in the section already being edited. The edit changes `prompt_sha`: the three cached verdicts stop being served, and the first live verdict writes a new cache prefix. Re-check `cacheWrite` on the first of the ten and `cacheRead` on the second ticker inside 5 minutes (X8); the new prefix is expected slightly above 2,374 tokens (+ the legend).

### 6. `stopBasis` precision: **out of scope**

The unrounded floats in `plan.stopBasis` come from `grading/plan_math.py`, which 4.8's target-distance cap (decisions 2026-09-22) will already edit; rounding the basis text to cents belongs in that change. Listed, not fixed here.

### 7. Out of scope, listed

- **Null `rsSpy*` / `rsSector*` / `sector` (X6, X7): a data gap, no code.** Live action before the ten run, on its own go, G6 pacing (one `POST /stock/{t}/refresh` at a time on 8001, ≥ 5 s apart, one ticker first): refresh `SPY`, then the sector ETFs of the ten. Note X7: `rsSector` also needs a `data_engine.stocks` row with a yfinance sector name for the ticker; hand picks that no scan has written (GOOGL) get `rsSpy` back but stay `sector: null` until one exists. Which ETFs: read each of the ten's `stocks.sector` first; ETFs with no mapped ticker are not fetched.
- **Event relevance** (a Meta story `high` for GOOGL, 8 of 15 events not about the company): the classifier's judgement and Finnhub's company-news breadth, not a unit. For the 4.7 / classifier review.
- **Filings cap** showing ten identical Form 4 rows: the 10-row cap is spec 4.4's; a per-form grouping is a later change.
- **`stopBasis` precision** (decision 6).

### 8. What is unchanged

The LLM schema (`test_llm_schema_has_no_number_the_model_could_set` still passes as is), `merge`, plan math, every level, the ledger, `cache.verdict_suffix` and the Redis keys, `db.insert_verdict_with_call`, `007_ai.sql` (frozen) and `008_journal.sql`. The three stored rows are not rewritten.

## G1.5 tables

**Writes.** No new write and no changed write path; the same two writes carry different content.

| write | before / after success | state left if the op fails after it |
|---|---|---|
| `ai.verdicts` INSERT + `ai.llm_calls` row, one transaction (spec 4.4 row 7) | after the answer validates; `prompt_inputs` now has the v2 keys and `projectionVersion`, `prompt_sha` the new hash, `fingerprint` includes `projection` | unchanged: neither row, 200 `stored: false`, `VERDICT NOT STORED` payload logged |
| verdict cache `SET` (`tf:ai:verdict:…`, spec 4.4 row 8) | after the INSERT; the stored fingerprint includes the projection version | unchanged: the verdict is stored and returned, the next call pays again |

**Failure branches.** Every test runs in `tf-ai-agent-dev` (or `tf-data-engine-dev` for the pin), no network.

| branch | fails open / closed | test function |
|---|---|---|
| dependency down: data-engine dossier unavailable | closed, 503 (unchanged) | `test_dossier_unavailable_is_503` (exists) |
| bad input: a renamed source key is missing from the dossier (`ext20` absent) | open: the projected `ext20Atr` is `null`, the plan is unaffected | `test_missing_indicator_projects_as_null` (new) |
| bad input: `ema20` missing, zero or non-finite | open: `aboveEma20Pct` is `null`, no division | `test_above_ema_pct_is_null_without_a_positive_ema` (new) |
| bad input: `profile.marketCap` missing | open: `marketCapUsdM: null` | `test_missing_indicator_projects_as_null` (new, parametrised) |
| bad input: a data-engine key the allowlist does not know | open: dropped, never reaches the model | `test_unknown_indicator_key_is_dropped` (new) |
| empty result: dossier indicators section missing | closed, 502 (unchanged) | `test_dossier_without_indicators_is_502` (exists) |
| repeat call: same inputs, same version | cache hit, no LLM call (unchanged) | `test_repeat_analyze_is_served_from_cache` (exists) |
| repeat call: cache entry written under projection version 1 | miss, one new verdict call | `test_projection_version_bump_invalidates` (new, `test_analyze_cache.py`) |
| repeat call: `verdict.md` edited | miss (unchanged rule) | `test_settings_or_prompt_change_invalidates` (exists) |
| contract: every number the model reads is labelled | n/a | `test_every_projected_number_carries_its_unit` (new) |
| contract: projection keys changed without a version bump | n/a, test fails | `test_projection_version_is_pinned` (new) |
| contract: the indicator key list on both sides | n/a | `test_indicator_keys_pinned_to_data_engine` (new, ai-agent), `test_indicator_fields_pinned_for_ai_agent` (new, data-engine) |
| contract: the prompt ships the legend | n/a | `test_verdict_prompt_ships_and_states_the_rules` (exists, gains the legend phrase) |
| fingerprint: `projection` in the basis | n/a | `test_projection_version_bump_changes_the_fingerprint` (new; the constant is module state, so it has its own test rather than a `test_fingerprint_change_invalidates` case) |
| contract: `profile.marketCap` is millions from profile2 *(approval)* | n/a | `test_indicator_fields_pinned_for_ai_agent` (data-engine, same test) |

Existing tests that change: `test_projection_is_trimmed_rounded_and_carries_no_account` (asserts the new keys; the fixture's `marketCap` becomes `3100000.0`, X5), `test_injected_headline_cannot_change_levels` (reads `ext20Atr` if it reads the key at all).

## Commits

1. `fix: verdict inputs carry their units; projection version in the fingerprint` — `services/ai-agent/analyze.py`, `services/ai-agent/prompts/verdict.md`, `services/ai-agent/tests/test_verdict_prompt.py`, `services/ai-agent/tests/test_analyze_cache.py`, this spec. No new test file, so `.github/workflows/tests.yml` and CLAUDE.md's test line are unchanged.
2. `test: data-engine pins its indicator fields for ai-agent's projection` — `services/data-engine/tests/test_indicators_endpoint.py` only. No data-engine deploy.
3. After the deploy and the first live check: one `docs:` commit (overview's 4.4 paragraph, progress row, a decisions entry "Verdict prompt inputs carry their units; key names are a contract from 2026-09-22", CLAUDE.md's measured-prefix line).

Both code commits are measured with `git diff --numstat` before the first push; a split is not expected.

## Verification (in the twin, before any go)

1. The full ai-agent list from CLAUDE.md in `tf-ai-agent-dev`, 0 failed, 0 skipped; the data-engine list in `tf-data-engine-dev`.
2. One `POST /analyze/AAPL` on the twin (8014; empty key, so the verdict call refuses before the wire): the log shows the new prompt loaded and the call refused with `LLMNotConfigured`; no cost. The twin's `users.settings.account_size` was NULL (409 before the projection), so it was set to a dummy 10000 in `tradingfirm_dev` for this; a dev-twin write, no ask (G15).
3. `git diff --stat 1a00fb2..HEAD`.

## Waiting for a go (G15), one line each

- `docker compose build ai-agent` then `docker compose up -d ai-agent`, outside 17:25–18:15 ET (the journal slot), after the 17:30 ET slot report if it lands first. Tag `tradingfirm-ai-agent:rollback-4.5-base` stays as the rollback.
- Live action, separate go, before the ten: refresh `SPY`, then the mapped sector ETFs of the ten, one at a time, ≥ 5 s apart (decision 7).
- The first of the ten through `scripts/analyze_live.sh`, on the ticker Zubair names: acceptance (3), plus `cacheWrite` on it and `cacheRead` on the second.
