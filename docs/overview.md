# TradingFirm — Project Overview

TradingFirm is a day-trading system that screens the US stock market, applies a
chain of technical filters, and surfaces the strongest day-trading candidates
in a live dashboard. It's built as a set of FastAPI microservices behind a
Next.js frontend, sharing Postgres and Redis as common infrastructure. Only
part of the design is actually implemented today — see [Service status](#service-status).

## Big picture

```
Finviz / Yahoo Finance
        │
        ▼
┌──────────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  Data Engine │────▶│Signal Engine │────▶│  Risk Shield │────▶│   AI Agent   │
│  (Port 8001) │     │  (Port 8002) │     │  (Port 8003) │     │  (Port 8004) │
│   ✅ built   │     │  🔲 scaffold │     │  🔲 scaffold │     │  🔲 scaffold │
└──────┬───────┘     └──────────────┘     └──────────────┘     └──────┬───────┘
       │                                                              │
       ▼                                                              ▼
┌──────────────┐                                              ┌──────────────┐
│  PostgreSQL  │◀─────────────────────────────────────────────│ Web Dashboard│
│  (Port 5432) │           HTTP proxy (Next.js API routes)     │  (Port 3000) │
└──────────────┘                                               │   ✅ built   │
       ▲                                                        └──────────────┘
       │
┌──────────────┐
│    Redis     │
│  (Port 6379) │
└──────────────┘
```

Each backend service owns its own Postgres schema (`data_engine`, `signals`,
`risk`, `users`, `ai` — see
[infra/supabase/migrations/001_initial_schema.sql](../infra/supabase/migrations/001_initial_schema.sql))
and services are meant to talk to each other only over HTTP and Redis
pub/sub, never by writing into another service's tables.

## Service status

| Service | Port | Status | What it does |
|---|---|---|---|
| `data-engine` | 8001 | **Functional** | Finviz screening → yfinance OHLCV → technical filters → enrichment. The only backend service with real logic. |
| `signal-engine` | 8002 | Empty scaffold | Intended for entry/exit signal detection (zones, patterns). Only `/health` and `/` exist. |
| `risk-shield` | 8003 | **Partial** | Market health scoring and regime detection (Phase 3). Part 3.1 gave it `config.py` / `db.py` / `cache.py`, a bounded fail-open lifespan and the `risk.macro_briefs` table; Part 3.2 added the two data fetchers (`monitors/quotes.py`, `monitors/fred.py`); Part 3.3 added the six regime monitors, the health score and the regime classifier (`scoring/`). Part 3.4 added the scheduler that runs them in market hours (prod only, `SCHEDULER_ENABLED`), writes `risk.health_checks` and publishes `tf:risk:health`, and `GET /market/health`, `/market/indicators`, `/market/history` beside `/health` and `/`. Part 3.5 added `GET /market/calendar` (a hand-maintained econ calendar file) and the market news poller (Finnhub general news every 15 min into data-engine's `POST /news/ingest`, prod only, `NEWS_POLL_ENABLED`). Part 3.6a added the macro brief's inputs: `GET /macro/brief/inputs` (health rows, data-engine's `GET /news/market`, the calendar, a FRED view with last-known and cadence freshness), the `MACRO_BRIEF_ENABLED` flag (off) and migration 006. The 3.4 follow-up moved both loops onto `wallclock.py` (sleeps of ≤ 60 s, a host-pause WARNING, `pausedSeconds` on the payload). Part 3.4b added night checks and the futures cap (`scoring/overlay.py`, `night_slots_for_day`, `run_night_check`), with `futures` and `overlay` on every `risk.health_checks` row, the payload and `/market/*`. Part 3.6b added macro brief generation behind `MACRO_BRIEF_ENABLED` (off in prod and the twin): slot, regime and manual briefs through ai-agent's `POST /brief/macro` contract, stored in `risk.macro_briefs` and served by `GET /macro/brief`; ai-agent implements the route in 4.6. Part 3.4c added the weekend-exposure signal: a `weekend` block (LOW / ELEVATED / HIGH with its reasons) on the last eight rows of a weekend-eve session, `GET /market/weekend/log` for judging the levels against the next session's opening move, and `PUT`/`DELETE /market/weekend/situation` — the service's only write routes, behind the `WEEKEND_WRITE_TOKEN` shared secret. |
| `ai-agent` | 8004 (loopback only) | **Partial** | The LLM layer and the headline classifier. Part 4.1 gave it `config.py`, `cache.py` (the `tf:ai:` namespace) and `providers/`: one OpenAI-compatible client pointed at OpenRouter through `LLM_BASE_URL`, with JSON-schema structured output, a per-model reasoning table, and a daily call cap plus a post-refusal cooldown in Redis, both checked before the request. Part 4.2 added the service's first lifespan (Redis + one `httpx` client, bounded and fail-open, no DB pool), `POST /classify/headlines` (up to 30 headlines in one structured call, unseen ones only, written back to data-engine's `news_items.sentiment`), a second smaller daily cap for the classifier, and `GET /usage` for the running USD spend. Part 4.3 added two pure modules with no caller yet: `models/verdict.py` (the verdict and plan schema, camelCase, levels in order, `go` needs a plan) and `grading/plan_math.py` (`compute_plan`: stop, disaster line, targets with R, and size from entry, ATR, zones, account and risk %, in Decimal, or a named rejection). The prod service publishes on `127.0.0.1` only, because those two routes spend and report money. Its `OPENROUTER_API_KEY` has been set since 2026-09-21. The classifier is live on `anthropic/claude-sonnet-5`, and structured output with `strict: true` held on the first live calls. Part 4.4 (deployed 2026-09-21, with `LLM_VERDICT_CACHE=true` in prod after the first live call measured a 2,237-token cached prefix) adds `POST /analyze/{ticker}?horizon=swing&entry=&fresh=`: the dossier from data-engine (fail-closed), the regime and any macro brief from risk-shield (fail-open), `users.settings`, in-process classification of the ticker's unlabelled headlines with write-back, headlines grouped into events by `eventKey` (`events.py`), `compute_plan`, one verdict call behind `prompts/verdict.md` whose schema has no price, R or size field, and the result in `ai.verdicts` with the full dossier and the exact prompt inputs. Since 2026-09-22 (spec `verdict-units`) every number in those inputs carries its unit (`ext20Atr`, `pos52wFrac`, `atr14Usd`, `marketCapUsdM`, `aboveEma20Pct` …), the projection is an allowlist pinned to data-engine's indicator fields, and `projectionVersion` sits in the document and in the cache fingerprint. A per-ticker Redis cache serves a repeat while an input fingerprint is unchanged (high-relevance events, earnings date, regime, brief, bar date, a ≥ 1 ATR price move, settings, prompt, model). It also gives the service a Postgres pool, `ai.llm_calls` — one row per LLM request that reached the wire, from both routes — and a startup step that raises the daily counters and cost totals to the ledger's numbers. `LLM_PROVIDER_ORDER` (default `anthropic`) asks OpenRouter for that host first with fallbacks always allowed; the host that served each call is stored in `ai.llm_calls.host`, and a fallback logs a WARNING. Part 4.5 (deployed 2026-09-22) adds the journal: a nightly task in the lifespan (`JOURNAL_SCORING_ENABLED`, prod only) that at 17:30 ET on XNYS sessions refreshes each due ticker through data-engine's `POST /stock/{t}/refresh`, reads the bars back and scores every verdict at +1 / +5 / +20 / +30 / +60 sessions into `ai.verdict_outcomes` (return, MAE, MFE, stop / target touched, first touch, R; migration 008, which also widens 007's horizon CHECK), counting sessions with `exchange_calendars` 4.13.2 (with pandas / numpy, new in this image) and waiting through a copy of risk-shield's `wallclock.py`; and `GET /journal/stats?days=` — hit rate, returns, avg R and confidence calibration per model × verdict × horizon. No LLM call. Part 4.8a (built 2026-09-22; migration 009 and the ai-agent rebuild wait for a go) is plan math v2 in `grading/plan_math.py`: T1 is the first resistance zone paying ≥ 1.5R and nearer zones are `overhead` the model reads; a target more than 6 ATR above the entry is dropped; when the support-zone stop's risk exceeds 2 ATR (or there is no support zone) the stop is EMA20 − 1 ATR if that is nearer (the swing-low input arrives with 4.8a-de); zones re-split around the entry by midpoint; every level carries a `basis` string in cents; size also respects a 2.5 % loss at the disaster line (`lossAtDisasterPct`). Every verdict is stamped `plan_math_version` (NULL = 1), which joins the cache fingerprint, and `GET /journal/stats` groups by model × version with a stop-hit rate by risk-in-ATR bucket. `scripts/plan_math_rerun.py` reruns v1 and v2 over stored dossiers with no LLM call. `POST /brief/macro` comes with 4.6. |
| `web` (dashboard) | 3000 | **Functional** | Next.js UI showing scan results, stock cards, market status. |

## The scanner pipeline (the core of the system)

There is now **one live scanner path**:
[`services/data-engine`](../services/data-engine), wired to the dashboard's
"Pro Scanner" tab via
[`web/app/api/scanner/pro/route.js`](../web/app/api/scanner/pro/route.js),
which proxies to it.

[`scanner/pro_scan.py`](../scanner/pro_scan.py) (+ `scan.py`,
`step1_finviz.py` … `step4_enrich.py`) is the original standalone Python
implementation `services/data-engine` was ported from. It stays in the repo
as a **frozen reference** (see [`scanner/README.md`](../scanner/README.md))
— not run by anything, not built on. The earlier JS reimplementation
(`web/lib/scanner/*.js` + `web/app/api/scanner/{discover,filter,technical}`)
and the "Scanner 1" tab/`/api/scanner/run` legacy exec path that duplicated
it have been removed.

### Pipeline steps (data-engine / `pro_scan.py`)

Implemented in
[`services/data-engine/scanners/market_scanner.py`](../services/data-engine/scanners/market_scanner.py):

1. **Pre-screen** — Finviz screens ~7,000 US stocks down to ~650 candidates
   on price, volume, and market-cap filters.
2. **Daily download** — bulk daily OHLCV (1 year) for all candidates via
   `yf.download()`, batched (≤20 tickers/call, 3s between batches,
   `threads=False`).
3. **Daily filters** — ATRP 2.5–6%, RVOL > 1.0/1.2, 52-week position 10–90%,
   IPO age > 120 days → ~20–60 pass.
4. **Hourly download** — hourly OHLCV (3 months) for daily winners only
   (~90%+ fewer API calls than downloading hourly for everything).
5. **Hourly filters** — 4H price > 50 EMA, 1H 20 EMA > 50 EMA.
6. **Enrichment** — `yf.Ticker` calls (2s delay between them) for sector,
   float, and news; gates on market cap > $500M and float 20M–1B shares.
7. **Sort** — by RVOL × ATRP, best opportunities first.

A full scan takes roughly 6 minutes and is rate-limited to one run per 10
minutes (`POST /scan/run` returns a `cooldown` status if called again too
soon, and `already_running` if a scan is mid-flight).

### Why this shape

yfinance and Finviz are unauthenticated scraping-style data sources with real
rate-limit risk — getting the user's IP blocked is treated as priority #1 to
avoid (see `.agents/AGENTS.md` G6). That drives most of the pipeline's
design: small batches, forced delays, `threads=False`, a "canary batch" that
aborts the whole scan if the first request fails, and a hard 10-minute
cooldown between scans. Both providers are explicitly dev/testing-only;
Polygon.io / FMP are the intended production upgrade path.

## Data Engine service

[`services/data-engine/main.py`](../services/data-engine/main.py) is the
FastAPI app. Key pieces:

- **Endpoints**: `POST /scan/run` (kicks off a background scan, 202
  Accepted), `GET /scan/status`, `GET /scan/results`, `GET /scan/history`,
  `GET /stocks/{ticker}`, `POST /stock/{ticker}/refresh`, `GET /stock/{ticker}/bars`,
  `GET /indicators/{ticker}`, `GET /dossier/{ticker}`, `GET /market/status`,
  `POST /news/ingest`, `GET /news/market`, `POST /news/{id}/sentiment`, `GET /health`.
- **`POST /news/{id}/sentiment`** (4.2) — stores one headline classification
  `{relevance, sentiment, category, oneLine, model, classifiedAt}` on
  `news_items.sentiment`, replacing the column rather than merging it. 404 when
  no row has that id, 422 outside the contract, 503 without a pool.
  ai-agent's classifier is the only caller. Part 4.4 adds an optional,
  validated `eventKey` slug to the contract, and the dossier's news items
  now carry their `news_items` `id` and the stored `sentiment` object.
- **`GET /news/market?hours=1..168&limit=1..100`** (3.6a, default 24 / 50) —
  stored `_MARKET` news, newest first, `[{publishedAt, source, title,
  summary, url}]`, Postgres only. `[]` when empty, 503 when the database is
  down. risk-shield's macro inputs call it; the bounds are a pinned copy there.
- **`POST /news/ingest`** (3.5) — market news from risk-shield's poller,
  stored under `_MARKET` by the existing `db.upsert_news` (dedup on
  `(ticker, url)`).
  - **Validation:** a Pydantic body with `extra="forbid"` (a `ticker` field
    is a 422) and 1–200 items: aware `publishedAt`, http(s) url ≤ 2,048,
    non-blank title ≤ 1,000, summary ≤ 10,000, source ≤ 100, no NUL.
  - **Errors:** one bad item fails the whole batch. No pool, or a database
    error or timeout, is a 503.
  - **The limits are a pinned copy** of risk-shield's converter's. There is
    no auth, like `/scan/run`.
- **`data_engine.ohlcv_bars`** (Postgres) — daily/hourly OHLCV per ticker,
  written by `db.upsert_bars()` / read by `db.get_bars()`, with bar-shaping
  logic (`db.bar_records_from_df()`) shared by every write path.
  `POST /stock/{ticker}/refresh` downloads daily (2y) + hourly (3mo) bars
  for one ticker via the provider and upserts them; a 15-minute
  Redis-backed cooldown per ticker rejects repeat calls with 429 (falls
  back to an in-memory cooldown if Redis is down), and the endpoint
  returns 503 rather than silently discarding fetched bars if Postgres is
  unavailable. The scan pipeline (`scanners/market_scanner.py`) also
  upserts daily + hourly bars — for final scan winners only, once hourly
  filtering is done — best-effort: a missing db pool or a failed upsert
  for one ticker is logged and skipped, never aborts the scan. Both write
  paths key rows on `main.normalize_ticker()` (upper-cased, stripped) so
  a ticker can't land under two different casings.
- **`providers/context/`** (Phase 2 context fetchers). `finnhub_client.py`:
  thin `httpx` client, key in the `X-Finnhub-Token` header (never the URL),
  in-process limiter of 60 calls per rolling minute plus a 1.2 s gap, typed
  errors (`FinnhubNotConfigured` before any HTTP when the key is empty,
  `FinnhubAuthError`, `FinnhubRateLimited`, `FinnhubError`), no retries.
  `finnhub.py`: `company_news`, `recommendations`, `earnings_calendar`,
  `earnings_surprises`, `profile` return raw Finnhub bodies cached in Redis
  (`tf:cache:finnhub:{kind}:{ticker}`, news 15 min, the rest 24 h, fail-open
  when Redis is down); pure converters turn them into rows and
  `sync_context()` stores news and earnings events. Tables (migration
  `003_context.sql`): `data_engine.news_items` (`ticker` NOT NULL, general
  news under `_MARKET`, unique on `(ticker, url)`) and `data_engine.events`
  (PK `(ticker, event_type, event_at)`, `meta` merged on conflict). The
  key reaches prod `data-engine` only via `FINNHUB_API_KEY`; the dev twin
  has it hard-coded empty. Tests mock HTTP with `respx` and replay
  `tests/fixtures/finnhub/AAPL_*.json`, recorded once by
  `tests/record_finnhub_live.py`. `edgar_client.py` (Part 2.2, spec
  `docs/specs/2.2.md`): SEC EDGAR over the same shape — no key, a declared
  `User-Agent` "<app> <email>" from `EDGAR_USER_AGENT` (empty →
  `EdgarNotConfigured` before any HTTP), one process-wide limiter
  `ratelimit.edgar_limiter` (rolling window, 10 per second), 403/429 →
  `EdgarRateLimited` (stop, no retry), 404 → `EdgarNotFound`. `edgar.py`:
  `cik_map()` (whole `company_tickers.json` → `{ticker: cik}`, Redis
  `tf:cache:edgar:cik_map`, 24 h), `recent_filings(ticker, forms=('8-K',
  '4'), days=30)` → `(rows, truncated)` (submissions `filings.recent`
  parsed to the rows filed within the last 90 days plus the block's oldest
  date, cached 15 min under `tf:cache:edgar:filings:{T}`; forms/days
  filtered in-process, `days` 1–90, amendments fold into the base form;
  `truncated` only when the block does not reach back to `today − days`;
  not-in-map and submissions-404 both return empty), pure
  `parse_submissions` / `filter_filings` / `filing_records`, and
  `sync_filings()` storing rows via `db.upsert_filings()` into
  `data_engine.filings` (migration `004_filings.sql`, PK `(ticker,
  accession)`, `filed_on DATE` = official filing date, `accepted_at`
  nullable, `ON CONFLICT DO NOTHING`). Fixtures
  `tests/fixtures/edgar/{company_tickers,AAPL_submissions}.json` recorded
  once by `tests/record_edgar_live.py`. Shared by both fetchers:
  `providers/context/ratelimit.py` (`RateLimiter`, injectable clock),
  `cache.cached_json()` (read-through, fail-open on Redis, wrong-shaped
  bodies are a miss) and `tickers.validate_ticker()` (1–5 letters; class
  shares deferred, `docs/decisions.md` 2026-09-09).
  `alphavantage_client.py` + `earnings.py` (Part 2.3, spec `docs/specs/2.3.md`):
  past earnings report dates, which the Finnhub free calendar does not
  carry. Primary is `DataProvider.get_earnings_dates()` (yfinance
  `Ticker.get_earnings_dates(limit=12)`; the fixture provider replays
  `tests/fixtures/earnings_dates/<T>.json`, where `null` records "this
  ticker has no earnings feed" and a missing file raises); the fallback is
  Alpha Vantage `EARNINGS`, called only when the primary yields no usable
  past date — never on a rate limit (`ProviderRateLimited`) and never when
  the bar store is empty. Report dates are validated against the stored
  daily bars (a bar date, or within one day of one) before they are written
  as `('earnings', date)` rows with `meta.earnings.{source, validated,
  hour, epsEstimate, epsReported, surprisePct}`, which merges beside 2.1's
  `meta.calendar`. `POST /stock/{ticker}/refresh` runs the step after the
  bar upserts and always answers `earningsDates: {source, stored, dropped,
  reason}`; a failure there never fails a refresh whose bars were stored.
  The Alpha Vantage key travels as the `apikey` query parameter (no header
  form exists) under the two conditions in that client module: the `httpx`
  logger pinned to WARNING and typed errors raised `from None`.
  `indicators/earnings.py` holds the pure reaction calculation (plan §3):
  `earnings_reactions(events, bars, limit=8)` pairs each confirmed report
  with the session that absorbed it (`amc` → the next session, `bmo`/`dmh`
  → the same one, at most 4 calendar days later) and returns gap % and
  close-to-close % newest first, plus `dataQuality: {source, dropped,
  disagreements}`. An unknown report hour and a cross-source date conflict
  are both settled by `calc_rvol >= 2` on the candidate session, never by
  the size of the move; when volume cannot separate them the report is
  dropped and counted. `providers/context/earnings.earnings_reaction_history()`
  is the I/O wrapper over the new generic `db.get_events(ticker,
  event_type, since, until)` and the existing `get_bars`; `reactions` is
  `null` when no confirmed report exists at all and `[]` when reports exist
  but no bars explain them.
- **Storage fallback chain**: results are always kept in an in-memory store;
  Redis and Postgres are optional — the service degrades gracefully and
  keeps working (from memory only) if either is unavailable at startup.
- **`providers/`** — a `DataProvider` abstraction so a production data
  source can be swapped in later without touching scanner logic.
  `get_provider()` knows two: `yfinance` (`yfinance_provider.py`, the live
  default) and `fixture` (`fixture_provider.py`, replays
  `tests/fixtures/{daily,hourly,info}/<TICKER>.json` with no network — for
  tests only, selectable via `DATA_PROVIDER=fixture`).
- **`requirements-dev.txt`** — `pytest` + `pytest-asyncio` + `respx` +
  `pytest-cov` (same pins in risk-shield's) on top of `requirements.txt`; baked into the Dockerfile's `dev` stage only (the
  `prod` stage never sees it). Tests run in `tf-data-engine-dev`, see
  Infrastructure below and `CLAUDE.md` Commands. `pytest.ini` restricts
  discovery to `tests/test_*.py` so a bare `pytest` cannot collect the
  live-scan script `tests/full_scan_test.py`.
- **`indicators/`** — pure, no-I/O indicator functions, imported from the
  package (`from indicators import ...`; the submodule split is an
  implementation detail): `moving_averages.py` (EMA, 4H aggregation from
  hourly bars), `volatility.py` (ATR, ATRP, extension from an MA in ATR
  units, opening gap %), `momentum.py` (RSI — SMA-seeded Wilder, MACD,
  relative strength vs a benchmark in percentage points, 52-week position),
  `volume.py` (RVOL, 20-day average dollar volume), `levels.py`
  (support/resistance zones: strict fractal swings + close-binned volume
  nodes, merged within 0.5% of the group's running mean, scored 0–90,
  top 3 per side relative to the last close, returned as `Zone`
  dataclasses), `snapshot.py` (`swing_snapshot`: the plan §3 swing set +
  zones as one dict from a daily frame and optional benchmark closes),
  `sectors.py` (11 yfinance sector names → SPDR sector ETFs), `models.py`
  (`IndicatorsResponse`, camelCase aliases). Conventions in
  `docs/decisions.md` 2026-09-06 (indicator package, zones, endpoint).
- **`GET /indicators/{ticker}`** — the swing set + zones for one ticker,
  computed from stored daily bars only (never the provider). SPY and the
  sector ETF (from `data_engine.stocks.sector`, read by `db.get_stock()`)
  come from the same bar store for relative strength; a missing one nulls
  its fields and shows `bars: 0` under `benchmarks`. Cached in Redis for
  15 min (`tf:cache:indicators:{ticker}`); `cached` is set on the way out,
  and `POST /stock/{ticker}/refresh` drops the key after writing bars.
- **`GET /dossier/{ticker}?horizon=swing`** — one document per ticker
  (`dossier/`): the indicator snapshot and zones, Finnhub news, events,
  recommendations and profile, EDGAR filings, and Part 2.3's earnings
  reactions. Every section is an object with its own `status` (`ok`,
  `truncated`, `error`, `unconfigured`), so a source that is down or
  unconfigured degrades one section while the rest returns 200 — there is
  no 502 on this path. A *database* failure is the exception: it is a 503
  for the whole document (`db.DB_ERRORS`), never a degraded section. Bars
  more than one weekday behind the last close trigger one refresh through
  `main.refresh_ticker_bars()` first; if that fails or is on cooldown the
  stored bars are served with `bars.status: stale`. Caps: 30 headlines, 10
  filings, both flagged by `truncated`. Cached in Redis
  (`tf:cache:dossier:{horizon}:{ticker}`) for 15 min in market hours, 60 min
  outside, and 2 min when any section failed; `cached` is set on the way
  out. Budgets: 8 s per section, 20 s for the refresh step. A cold dossier
  costs 5 Finnhub + 2 EDGAR calls, a warm one 2, a cached one none, and the
  response reports them under `budget`.
- **Source cooldowns** (`cache.cooldown_remaining` / `start_cooldown`) — a
  Finnhub 429 parks that source for 60 s, an EDGAR 403/429 for 15 min, an
  Alpha Vantage cap for 1 h, source-wide rather than per ticker. The next
  dossier skips the source before any HTTP (`reason: cooldown`), and
  `sync_earnings_dates` skips its Alpha Vantage fallback the same way
  (`earningsDates.reason: "cooldown"`). Redis is the store; an in-memory
  clock (`cache.MemoryCooldowns`) covers Redis being absent or failing, and
  the same pair of helpers backs the per-ticker refresh cooldown.
- **`scanners/models.py`** — Pydantic models with `by_alias` field aliases
  (e.g. `market_cap` → `marketCap`) so FastAPI's snake_case internals
  serialize as the camelCase JSON the frontend expects.

## Risk Shield service

[`services/risk-shield`](../services/risk-shield) holds the Phase 3 regime
inputs (Part 3.2, spec `docs/specs/3.2.md`), the health score built on
them (Part 3.3, spec `docs/specs/3.3.md`), the scheduler and read
endpoints that run and serve it (Part 3.4, spec `docs/specs/3.4.md`), and
the econ calendar and market news poller (Part 3.5, spec `docs/specs/3.5.md`),
the macro brief's inputs (Part 3.6a, spec `docs/specs/3.6a.md`), and its
generation (Part 3.6b, spec `docs/specs/3.6b.md`):

- **`monitors/quotes.py`** — `get_core_quotes(r, memory)`: one yfinance
  1.5.1 `download` of the 17 core tickers (`SPY QQQ RSP ^VIX TLT GLD UUP
  XLK XLU XLP XLV XLY XLF ES=F NQ=F CL=F GC=F`), daily 1y, `threads=False`,
  timeout 5 s. It returns a JSON envelope `{asOf, tickers: {T: {date[],
  open[], high[], low[], close[], volume[]}}, missing, reason}` cached under
  `tf:risk:cache:quotes`: 5 min when complete, 120 s when `partial` / `empty`.
  - **Request count:** 34 requests on a cold container (a timezone fetch
    per ticker), 17 warm.
  - **Rate limits:** yfinance 1.5.1 only logs them, so a handler on the
    `yfinance` logger detects them, behind an exact version guard.
  - **Concurrency:** a single-flight lock is held for the whole download.
- **`monitors/fred.py` + `fred_client.py`** — FRED
  `series/observations` for `VIXCLS DGS10 DGS2 T10Y2Y DFF DCOILWTICO
  CPIAUCSL UNRATE`, one request per series, 800 days back, `"."` values
  dropped. Cached per series under `tf:risk:cache:fred:{SERIES}`: 6 h, or
  120 s when empty.
  - **Key:** `FRED_API_KEY` travels in the `api_key` query parameter (FRED
    has no header form), under the Alpha Vantage conditions: the `httpx`
    logger pinned to WARNING, typed errors raised `from None`.
  - **Bounds:** 8 s per request (`httpx` timeout plus `asyncio.wait_for`);
    `ratelimit.fred_limiter` at 60/min with a 1 s gap.
  - **Snapshot:** `fred_snapshot()` walks the 8 series. It stops on a
    source-wide state and continues past a per-series error.
  - **View (3.6a):** `get_fred_view()` is what the macro inputs read.
    - Every full envelope is also kept 7 days under `tf:risk:cache:fred_last:{SERIES}`. A refusal, cooldown, error or empty answer serves that copy as stale; with none it is `no_data`, never a raise.
    - Each series is judged by cadence on the ET date: daily 6 d, DCOILWTICO 14, CPIAUCSL 80, UNRATE 70.
    - It carries `latest` / `monthAgo` / `yearAgo`, never the arrays.
- **Refusals and cooldowns** — `cache.py` carries data-engine's cooldown
  helpers under `tf:risk:cooldown:{SOURCE}`:
  - yfinance: a rate limit, or an all-empty download, parks the source 15 min.
  - FRED: a 429/423 parks it 15 min, a rejected key 1 h.
  - A refusal raises (`…RateLimited`, `…CoolingDown`) and caches nothing.
  - `cached_json` refuses a `None` from a fetcher and takes a body-derived TTL (`ttl_for`).
- **Live canaries** — `tests/fred_live.py`, `tests/quotes_live.py` and
  `tests/finnhub_news_live.py` (3.5: one request; reads out page size, span
  and limits, and records the 20-item fixture) run only through the isolated
  `docker run` line in `docs/specs/3.2.md`: default bridge network,
  unroutable `DATABASE_URL` / `REDIS_URL`, and only the one key they need
  taken from `.env`. `tests/live_guard.py` refuses a prod-looking
  environment.
- **`quotes.get_quotes_view(r, memory)`** is what the monitors read.
  - Every full quotes answer is also kept 24 h under
    `tf:risk:cache:quotes_last`.
  - On a cooldown, refusal, error or empty download, the view serves that
    copy with every ticker stale.
  - A partial download is filled per ticker.
  - With nothing to serve it is empty (`source: "none"`); it never raises.
- **`monitors/series.py`** pairs tickers only through `align()`, an inner
  join on date. A same-day bar downloaded before 16:15 ET is partial and
  dropped (`zoneinfo` America/New_York).
- **`monitors/regime.py`** holds six pure monitors, each returning `{score,
  raw, detail, stale}`, registered in `MONITORS` with Part 5's weights:
  - `vix` (25) reads `^VIX` directly, intraday level included
  - `breadth` (20) is the RSP/SPY 20-day slope; `adRatio` is null
  - `spy_trend` (20) uses EMA 20/50/200 and lower lows
  - `sector_rotation` (15) is the offensive vs defensive 5-day spread
  - `volume` (10) is SPY+QQQ against the 20-day average
  - `cross_asset` (10) is the 1-day TLT/GLD/UUP/SPY moves
- **`scoring/`**:
  - `health_calculator.compute_health(r, memory)` runs the view, then the
    monitors, then the integer-weighted, half-up score. Monitors without a
    score are left out, and a covered weight below 70 gives no score.
  - `regime_classifier.classify()` maps the score to HEALTHY ≥ 70 /
    CAUTIOUS ≥ 40 / DANGER ≥ 20 / CRITICAL.
  - The thresholds Part 5 doesn't give are provisional (`docs/decisions.md`).
- **`scheduler.py`** (3.4) is one asyncio task, started by the lifespan only
  when `SCHEDULER_ENABLED=true` (the prod compose service). The Dockerfile
  pins `uvicorn --workers 1`, so there is exactly one.
  - **When:** XNYS sessions from `exchange_calendars` 4.13.2, holidays and
    early closes included. Every 5 min from open to close inclusive (79
    slots, 43 on an early close), plus a 16:20 ET settle check. A slot more
    than 60 s late is skipped with a "missed N" WARNING, never caught up.
  - **Night checks (3.4b):** every 30 min at :15 and :45 ET whenever CME
    equity futures trade (`CMES`) and no XNYS session is under way — 16:45 and
    18:15 → 08:45 on weeknights, Sundays from 18:15, 155 a week. A night check
    re-runs no monitor: it takes the latest settle's score and caps it by the
    worse ES=F / NQ=F move since that settle (−1.5 % at most CAUTIOUS, −3 % at
    most DANGER, −5 % CRITICAL), copying the settle's monitors into the row.
    Market checks take the same cap from the futures their own download
    carries; the settle check is exempt, and stores the reference.
  - **Waiting (3.4 follow-up):** the loop waits through `wallclock.py`: sleeps
    of at most 60 s, the wall clock re-read after each, because Docker's
    monotonic clock stops while the Mac sleeps. A wake that passed slots logs
    "missed N" even between slots. A host pause over 120 s logs `host paused
    ~Xh Ym`, and the next check's publish carries `pausedSeconds`. The news
    poller waits the same way.
  - **The weekend block (3.4c):** on a **weekend-eve session** — the last XNYS
    session before a gap of ≥ 2 calendar days with no session, so Friday
    normally, Thursday before a Friday holiday, Friday before a Monday one —
    every check from **close − 30 min** and that day's 16:20 settle carries a
    `weekend` block: LOW / ELEVATED / HIGH plus the reasons behind it, from
    the published (capped) score, the VIX level and its 5-day direction, the
    econ calendar between close and next open, pending-decision language in
    the news, and the operator's situation flag. Eight rows a weekend. Night
    rows carry none: a night check has no live input to read. The block never
    changes a score, a trend or a publish decision, and a non-finite value in
    it is dropped before it can reach the payload.
  - **A check:** `compute_health` → trend base → publish → insert, each step
    isolated, so a Postgres failure never delays a publish. Trend is ±5
    against the latest scored settle before the check's session open. A
    check skips while the quotes download lock is held.
  - **Publish hook (3.6b):** after a check publishes, `run_check` calls
    `on_check_published(state, reason)` when it is set. It is None unless the
    lifespan sets it to `macro_brief.request_brief` (brief flag on), and it is
    reset on shutdown.
- **`scoring/alert_manager.py`** (3.4) publishes on `settings.health_channel`.
  - **Channel:** `tf:risk:health`; the dev twin uses `tf:risk:dev:health`,
    because Redis pub/sub ignores the DB index.
  - **When:** the regime changed, or the score moved ≥ 10 since the last
    publish, at most once per 15 min. Entering CRITICAL skips the interval.
  - **State:** `tf:risk:state:health_published` (7 d), written after the
    publish, so delivery is at-least-once.
- **`risk.health_checks`** (table from 001) gets one row per check, null
  scores included. `kind`, the monitors, the inputs and the settle base live
  in the `indicators` JSONB.
- **Endpoints** (3.4) read Postgres only and never download:
  - `GET /market/health` returns the latest check with `trend`,
    `settleScore`, Part 5's message and `ageSeconds`, plus `lastScored` when
    that check has no score. Before the first check it answers 404
    `no health checks yet`.
  - `GET /market/indicators` returns the latest check's six monitors with
    weights.
  - `GET /market/history?days=1..90` (default 30) returns rows ascending,
    null scores kept.
  - A missing pool or a database failure is a 503. `/health` also reports
    `schedulerEnabled` and `lastCheckAt`.
  - **Exception (3.5):** `GET /market/health` also carries `newsPollStale`,
    `lastNewsPollAt` and `newsLastError`, read from process memory at
    request time.
- **`econ_calendar.py` + `data/econ_calendar.json`** (3.5) — the only econ
  calendar source: FOMC decisions (14:00 ET), CPI releases and jobs reports
  (08:30 ET) for 2026-07-01 … 2026-12-31, copied by hand from the Fed and BLS
  schedule pages. The file ships in the prod image.
  - **Loading:** validated once per process, and never cached when invalid.
  - **`GET /market/calendar?days=1..31`** (default 7): events on ET dates
    today … today + days − 1, with `datetimeUtc` and `released`.
    - A window past the file's end is 200 with `coverageShort: true`.
    - A missing or invalid file is a 503.
  - **Renewal:** `/health` reports `calendarCoversThrough` and
    `calendarCoverageShort`, which turns true 14 days before the end; the
    news poller also logs a daily WARNING then. The renewal step is in
    `CLAUDE.md`.
- **`monitors/finnhub_client.py` + `news_poller.py`** (3.5) — the market news
  poller, one asyncio task started only when `NEWS_POLL_ENABLED=true` (prod).
  - **When:** every wall-clock quarter hour (UTC), around the clock. A late
    wake polls once, and missed slots are never caught up.
  - **A poll:** one Finnhub `GET /news?category=general` call (key in the
    `X-Finnhub-Token` header, 60/min limiter, 8 s bound, no retries), then
    the whole page (100 items spanning ~41 h on 2026-09-10) POSTed to
    `DATA_ENGINE_URL/news/ingest`, oldest first, in chunks of 200.
    - No `minId` and no news state in Redis: ingest dedups.
    - The converter strips NUL and truncates or drops against the route's
      limits, so a 422 means the two copies drifted: ERROR once, then WARNING.
  - **Skips:** a poll makes no request while risk-shield's Finnhub cooldown
    (429 15 min, 401/403 1 h) or data-engine's `tf:cache:finnhub` (read-only)
    is running.
  - **Success** is a non-empty page, at least one item kept and every chunk
    answering 200. An overlap WARNING fires when the page's oldest item is
    newer than the previous success.
  - **`/health`** adds `newsPollEnabled`, `lastNewsPollAt` (the last
    success), `newsPageSpanMinutes`, `newsOldestAt`, `newsLastError` and
    `finnhubConfigured`.
  - **`newsPollStale`** is true when the poller is on and there has been no
    success for more than 60 min, whatever the cause. It is on
    `/market/health` and every `tf:risk:health` publish. **`newsPollStale:
    null` means the poller is off or its loop has not started yet (for
    example a boot before its first quarter hour), not "unknown".**
- **`macro_inputs.py`** (3.6a) — `assemble_inputs()` builds the document the
  macro brief will read and 3.6b will store as `risk.macro_briefs.inputs`:
  `{schemaVersion, assembledAt, ready, health, settle, news, calendar, fred,
  freshness}`.
  - **Sections fail on their own** and say why:
    - health: the latest `risk.health_checks` row, stale when older than the last slot that should have produced one
    - settle: the latest scored settle
    - news: one `GET /news/market?hours=<window>&limit=50`, no url, text cut to 300. The window (3.6b) is the hours since the latest XNYS close before today, rounded up and clamped to 24–96, and `news.hours` stores it
    - calendar: the next 7 ET days
    - fred: the view above
  - **`freshness`** flattens each section's flags plus the news poller's three keys into `anyStale`. `ready` means a scored row exists.
  - **Bounded** at 64 KB of compact JSON by dropping the oldest news; `allow_nan=False`.
  - **`GET /macro/brief/inputs`** serves it with `cached`: one assembly at a time, the last document reused for 60 s. It works with the brief flag off.
- **`risk.macro_briefs`** (005, 006) has `brief` (JSONB object) and `trigger`
  (`slot` / `regime_change` / `critical` / `manual`), both `NOT NULL`. One row
  per generated brief (3.6b): `brief_text` is `oneParagraph`, and `inputs` is
  the document the brief was generated from.
- **`ai_agent_client.py`** (3.6b) — one `POST {AI_AGENT_URL}/brief/macro` with
  `{"inputs": <document>}`, validated against the contract in
  `docs/decisions.md` (2026-09-11, camelCase): `regimeView`, `keyRisks` 1–8,
  `upcoming` 0–10, `oneParagraph`, optional `model`, 16,000 bytes.
  - A violation is rejected, never truncated; a blank `model` becomes null.
  - 404, 429, another non-200, a transport error or a timeout is "unavailable" (a 404 logs a WARNING every time); 422 is "rejected"; a bad body logs an ERROR once per rule. No retries or backoff, a 180 s hard bound, redirects not followed.
- **`macro_brief.py`** (3.6b) — generation, running only with
  `MACRO_BRIEF_ENABLED=true` (false in prod and the twin, so nothing calls ai-agent there).
  - **One generation:** skipped while another runs, without a pool, when debounced, or when the inputs aren't `ready` (no ai-agent call then); otherwise ai-agent, then one insert. `brief_status` feeds `/health`'s `lastBriefAt` and `lastBriefTrigger` (the last stored brief) and `lastBriefError` (the last attempt).
  - **Slots:** 07:30, 12:30 and 16:30 ET on XNYS sessions (early closes keep all three), waiting in `wallclock.wait_seconds` chunks of ≤ 60 s. A wake up to 30 min late runs the slot; later ones get one WARNING and are never caught up. A slot whose window (start + 1,980 s) already holds a `slot` row is skipped, so a restart doesn't generate twice.
  - **Regime trigger:** `request_brief`, the scheduler's publish hook, queues `regime_change` / `critical` on a size-1 queue (a full queue drops at DEBUG), and the loop's wait answers it at once. The debounce reads Postgres: `regime_change` waits 30 min after any stored brief, `critical` 60 min after a critical one.
- **Endpoints (3.6b):**
  - `GET /macro/brief?includeInputs=false` — the latest row, Postgres only, whatever the flag: `{id, generatedAt, ageMinutes, trigger, regime, healthScore, briefText, brief, freshness}`, with `inputs` only when asked. 404 `no macro brief yet`; 503 without a database.
  - `POST /macro/brief/generate` — a manual brief: 503 with the flag off or no database, 409 while one runs, 429 + `Retry-After` within 600 s of any stored brief, then 201 (GET's body, one serializer), 422 `inputs not ready`, 502 an ai-agent failure, 503 `brief lost`. Unauthenticated; it blocks up to 180 s.

## Web dashboard

Next.js 15 / React 19 app in [`web/`](../web). `web/app/page.js` renders the
Pro Scanner: backed by `/api/scanner/pro`, which proxies to the
`data-engine` FastAPI service — POST triggers a scan and polls
`/scan/status` until it completes, then fetches `/scan/results`.

`ProStockCard` renders individual candidates, `SectorTabs` filters by
sector, and `Header` shows market status. There is no auth yet (D18 in
`docs/plan-analyst-watcher.md` parks it deliberately); the earlier Firebase
Google-sign-in scaffolding under `web/lib/firebase/` has been removed.

## Infrastructure

- **Postgres 16** — one schema per service (`data_engine`, `signals`,
  `risk`, `users`, `ai`); scan history, stock/fundamental data, signals,
  strategies, watchlists, and an audit trail live here once tables are
  migrated in.
- **Redis 7** — scan status, pub/sub for cross-service events (e.g.
  `tf:scan:complete`, `tf:signal:new`), and cache TTLs for scan results and
  market health (see [`shared/constants.py`](../shared/constants.py)). One
  key prefix per service, all in DB 0 in prod: `tf:cache:` is data-engine's,
  `tf:risk:` risk-shield's, and `tf:ai:` ai-agent's (Part 4.1: the daily LLM
  call counter on the ET day, and the provider cooldown; Part 4.2: the
  classifier's own day counter, the running cost totals for the ET day and
  month, and `tf:ai:classify:{digest}` — one classification per headline,
  7 days, keyed by `cache.headline_digest`; Part 4.4:
  `tf:ai:verdict:{user}:{TICKER}:{horizon}:{entry|auto}` for 4 h, the
  matching `tf:ai:lock:analyze:` key for 200 s, and
  `tf:ai:state:ledger_missed:{day}`; Part 4.5: `tf:ai:lock:journal` for
  2,700 s and the set `tf:ai:journal:blanked`, 7 days).
  **Redis is not durably persisted**: the service declares no volume, AOF is
  off, and the image's anonymous `/data` volume with default RDB rules
  survives a restart and a recreate but not a `docker compose down`. So the
  cost totals and both caps are a floor, not an audit (`docs/decisions.md`
  2026-09-20).
- **OpenRouter** (`https://openrouter.ai/api/v1`, the `openai` SDK, Part
  4.1) — the one LLM gateway. OpenAI-compatible, so the client is
  `openai==3.16.2` with a configurable `base_url`; models are env knobs
  (`LLM_MODEL`, `LLM_MODEL_CLASSIFIER`), default
  `anthropic/claude-sonnet-5`, with `z-ai/glm-5.3-flash` and `z-ai/glm-5.3`
  reachable through the same key. The key is `OPENROUTER_API_KEY`, empty
  everywhere today. Structured output is `response_format` with a JSON
  schema; reasoning effort is OpenRouter's `reasoning` object, per model.
  No retries anywhere: `max_retries=0`, a daily call cap and a cooldown.
- **Docker Compose** — [`docker-compose.yml`](../docker-compose.yml) runs
  all 7 containers (postgres, redis, 4 FastAPI services, web) prod-like;
  `docker-compose.dev.yml` adds hot-reload. Requires `DB_PASSWORD` set in
  `.env` — compose fails fast without it.
- **`tf-data-engine-dev`** — opt-in eighth container (compose profile
  `dev`, `docker compose --profile dev up -d data-engine-dev`, host port
  8011; migrations mounted read-only at `/migrations`) for running data-engine tests without touching prod
  `tf-data-engine`. Built from the data-engine Dockerfile's `dev` stage
  (dev deps, no code — the source tree is volume-mounted), provider
  hard-coded to `fixture`, its own database `tradingfirm_dev` and Redis
  DB 1, no `depends_on`. `scripts/dev-db.sh` creates that database and
  applies migrations to it (via `MIGRATE_DB=` in `scripts/migrate.sh`);
  until it runs the dev API reports `db_connected: false`. Conventions in
  `docs/decisions.md` 2026-09-06 (Part 0.7 entry).
- **`tf-risk-shield-dev` (port 8013)** is the same arrangement for
  risk-shield (Part 3.1): profile `dev`, the Dockerfile's `dev` stage,
  source volume-mounted, `tradingfirm_dev` + Redis DB 1, `FRED_API_KEY`
  hard-coded empty, `SCHEDULER_ENABLED=false` and
  `HEALTH_CHANNEL=tf:risk:dev:health` hard-coded (Part 3.4),
  `FINNHUB_API_KEY=""`, `NEWS_POLL_ENABLED=false` and
  `DATA_ENGINE_URL=http://data-engine-dev:8001` hard-coded (Part 3.5),
  `MACRO_BRIEF_ENABLED=false` and `AI_AGENT_URL=http://ai-agent.invalid:8004`
  hard-coded (Part 3.6a), and
  `infra/supabase/migrations` mounted read-only at
  `/migrations` for the tests that assert migration text. Phase 3 tests run
  there:
  `docker exec tf-risk-shield-dev pytest tests/test_config.py ... -v`.
- **`tf-ai-agent-dev` (port 8014)** is the same arrangement for ai-agent
  (Part 4.1): profile `dev`, the Dockerfile's `dev` stage, source
  volume-mounted, `tradingfirm_dev` + Redis DB 1, and three hard-coded
  locks so nothing there can reach an LLM — `OPENROUTER_API_KEY=""`,
  `LLM_DAILY_CALL_CAP=0` and `LLM_BASE_URL=http://openrouter.invalid/api/v1`.
  No `depends_on` and no `/migrations` mount: every test fakes Postgres and
  Redis, and 4.1 touches no migration. Phase 4 tests run there:
  `docker exec tf-ai-agent-dev pytest tests/test_config.py tests/test_cache.py tests/test_openai_compat_provider.py tests/test_lifespan.py tests/test_usage_endpoint.py tests/test_prompts.py tests/test_data_engine_client.py tests/test_classifier.py tests/test_classify_endpoint.py tests/test_verdict_model.py tests/test_plan_math.py tests/test_db.py tests/test_ledger.py tests/test_migration.py tests/test_events.py tests/test_clients.py tests/test_verdict_prompt.py tests/test_analyze.py tests/test_analyze_cache.py tests/test_sessions.py tests/test_scoring.py tests/test_journal_runner.py tests/test_wallclock.py tests/test_journal_loop.py tests/test_journal_stats.py -v`.
  From 4.5 it hard-codes `JOURNAL_SCORING_ENABLED=false` (`test_twin_never_scores_on_a_schedule`).
  From 4.4 it mounts `/migrations` read-only (the 007 text test) and
  hard-codes `RISK_SHIELD_URL=http://risk-shield-dev:8003` and
  `LLM_VERDICT_CACHE=false` (`test_twin_never_calls_prod_risk_shield`).
  From 4.2 it hard-codes two more locks, `LLM_CLASSIFIER_DAILY_CALL_CAP=0` and
  `DATA_ENGINE_URL=http://data-engine-dev:8001`, so a twin write-back can never
  reach prod's `news_items` (`test_twin_never_writes_prod_data_engine`).
- **CI: [`.github/workflows/tests.yml`](../.github/workflows/tests.yml)** —
  GitHub Actions on every push and pull request. One job per service
  (data-engine, risk-shield) builds the Dockerfile's `dev` stage and runs the
  explicit `tests/test_*.py` list inside it with `--network none`, the source
  and `infra/supabase/migrations` mounted read-only, and the twin's
  non-secret env (keys empty). No Postgres, Redis, compose, secrets or live
  scripts: every test fakes its dependencies. A failed **or skipped** test
  fails the job (a skip means a lost mount or twin env). Coverage via
  `pytest-cov`, `.coveragerc` omitting `tests/`, no threshold yet. The
  workflow's lists are the reference; `CLAUDE.md` copies them.
- **Startup bounds differ between the two services.** risk-shield wraps
  each dependency connection in `asyncio.wait_for(config.STARTUP_TIMEOUT)`
  (5 s, worst-case boot ~10 s); data-engine does not, and a slow-but-not-
  refusing Postgres can stall its boot for up to a minute. `docs/decisions.md`
  2026-09-09 (Part 3.1) records the reasoning and leaves data-engine to a
  later refactor.

## Where things stand

The **data-engine + web dashboard** loop is the one real, working path today:
screen the market, filter candidates, enrich, display them. Everything
downstream of that — actually generating trade signals (`signal-engine`)
— is still an empty FastAPI scaffold with no business logic. `ai-agent`
has its LLM layer as of Part 4.1 (config, the `tf:ai:` cap and cooldown,
and one OpenAI-compatible provider against OpenRouter) but no routes and
no key in prod, so nothing there talks to a model yet. `risk-shield` has its infrastructure
(config, pool, cache, migration, dev twin) as of Part 3.1 and its two
data fetchers (core quotes, FRED) as of Part 3.2, its health score and
regime as of Part 3.3, and a market-hours scheduler plus the `/market/*`
read endpoints as of Part 3.4, running in prod since 2026-09-10. Part 3.5
added the econ calendar and the market news poller (with data-engine's
`POST /news/ingest`), deployed to prod on 2026-09-10. Part 3.6a added the
macro brief's inputs and `GET /macro/brief/inputs` (with data-engine's
`GET /news/market` and migration 006), deployed to prod on 2026-09-10. Part
3.6b added brief generation behind `MACRO_BRIEF_ENABLED` (off), deployed to
prod on 2026-09-11; ai-agent's route comes with 4.6. Part 4.1 opened
Phase 4 with ai-agent's provider layer and its 8014 dev twin — no prod
deploy, since nothing calls it yet. Part 4.4 built the analyze endpoint and
migration `007_ai.sql` (`ai.verdicts`, `ai.verdict_outcomes`, `ai.judgments`,
`ai.llm_calls`, `users.settings` with a NULL account size set by hand from
`docs/runbook.md`), deployed to prod on 2026-09-21 with the first live
verdicts stored. Part 4.5 built journal scoring (migration 008, the 17:30 ET
slot, `GET /journal/stats`); its deploy waits for a go. The `scanner/`
standalone scripts predate the data-engine port and stay only as a frozen
reference — see [`.agents/AGENTS.md`](../.agents/AGENTS.md) for the full
rationale.
