# Plan — Analyst + Watcher + Journal (swing first)

Status: **approved shape, no code written.** This file supersedes `docs/plan-x.md` for product direction. `plan-x.md` §5 (disposable Docker environments) is kept as a reference and parked to Phase 9 here.

Read `.agents/AGENTS.md` and `CLAUDE.md` before any part. G1 (3-sentence spec → approval), G6 (protect external APIs), G7 (tests without network), G8 (memory cleanup), G12 (commit per part) apply to every part below.

---

## 0. Decisions made in discussion (do not re-litigate)

| # | Decision |
|---|---|
| D1 | Build the **deep dive first**, scanner integrates later as a feeder. |
| D2 | Three pieces: **Analyst** (ask about a ticker → verdict + exit plan), **Watcher** (holds positions, rules every minute, model only when a trigger fires, Telegram alerts), **Journal** (records and scores everything). Plus a shared **Macro brief** refreshed a few times a day. |
| D3 | **Swing entries over days** is the first mode. Holding horizon is a parameter, not an assumption. A **same-day mode** (exit before close unless thesis confirmed) is added once streaming quotes exist. Day trading is not built. |
| D4 | The analyst is **one strong model + deterministic fetchers**, not five agents. Fetchers are plain code. The model does two jobs only: turn text into structured facts, and synthesize a verdict. Multi-agent only if context outgrows one call. |
| D5 | **Learning = journal.** Every verdict and alert is stored with the exact inputs it saw and scored later against real prices. No fine-tuning. Learning mechanisms: accuracy stats fed back into prompts, retrieval of similar past cases, weekly human review of misses. |
| D6 | The model **advises, the human executes**. No auto-execution, no broker order placement. Brokers (IBKR, Trading212) are data and paper-trading inputs only. |
| D7 | Hard band = rules only, no model, immediate. Soft band = model judges with evidence. Heads-up band = model, throttled. **Asymmetry: the model may tighten or exit, never loosen a stop, move it down, delay or cancel a hard alert.** |
| D8 | Hard stop default = **hourly close below stop**, plus a **disaster line** (stop − 1 ATR) that triggers on intraday touch immediately. Per-position override allowed later. |
| D9 | Early exit **before** the stop is wanted: a bleed pattern wakes the model, the model checks market vs stock-specific, news, history, regime, and answers hold / tighten / partial / exit. Red bars alone = hold + tighten, never exit. Red bars + reason = decision. The journal measures early-exit precision against a stop-only baseline. |
| D10 | Analyst proposes the plan (stop, invalidation, targets, size, catalyst flags); user approves or edits; **both proposed and accepted versions are stored.** A plan with no stop is refused. |
| D11 | Stop is structural (support zone − ~1 ATR), invalidation is a condition (e.g. daily close < 20 EMA), targets are next resistance zones with R printed, size = risk budget ÷ stop distance. Sizing bounds gap losses; stops do not. |
| D12 | Watcher runs every minute in market hours; every 15–30 min at night looking at news and index futures only. |
| D13 | **Telegram only** for notifications and as the first mobile interface. Email/push parked. |
| D14 | MacBook stays plugged in and awake for the first 1–2 months; VPS later. Every watcher gap is logged. |
| D15 | Data: **free now** (yfinance daily bars, Alpaca IEX stream, Finnhub free tier, SEC EDGAR, FRED). Paid (FMP + Polygon tier) when going public or when blocked. Provider layer makes it a config change. |
| D16 | LLM: Anthropic `claude-opus-5` for verdicts, judgments and macro brief. Headline classification defaults to `claude-opus-5` too, with an env knob to switch to `claude-haiku-4-5` for cost. Gemini fallback parked. |
| D17 | Indicator set for swing (see §3). ADX, Fibonacci/Camarilla pivots, 5-minute tradability checks are dropped. |
| D18 | Design for public from day one without building it: `user_id` on every position/verdict/alert row, notification channel abstraction, per-ticker analysis cached with TTL. Auth itself is parked. |
| D19 | Options data is a **late phase** input to analyst and watcher (§ Phase 10). |

---

## 1. How to work this plan (for any coding model)

- **One part per session.** Each part lists exactly what to read, build, test, and commit. Do not start the next part in the same session unless told.
- **Before coding a part**: post the 3-sentence spec (what / must not / acceptance) and wait for approval (G1).
- **Never call a live external API from a test.** Tests use fixtures under `tests/fixtures/` and `respx`/mocks. One-off live checks are scripts named `*_live.py`, run manually with 1 ticker first (G2).
- **Verify libraries first** (G4): print `lib.__version__`, compare with the pinned requirement, run a 3-line probe, then build. External endpoints listed in §2 marked *verify* must be confirmed against the provider's current docs at the start of the part that uses them.
- **After every part**: run the part's test command, show the actual output, commit with the given message, tick the box in §12.
- **Keep parts small.** If a part grows past ~300 new lines, stop and split it.
- **Do not run bare `pytest`** in `services/data-engine` (see `CLAUDE.md`). Always name the test file.
- **Anthropic SDK usage**: load the `claude-api` skill in the session that writes LLM code; use `client.messages.parse()` / `output_config.format` for structured output, `cache_control` for prompt caching, adaptive thinking (default on Opus 5). Include the server-side refusal `fallbacks` parameter as the skill recommends.

---

## 2. Stack and third parties (per component)

### Languages and frameworks (all already in the repo unless marked new)

| Component | Language / framework | Location |
|---|---|---|
| Data fetchers, bar store, indicators, dossier | Python 3.12, FastAPI, pandas, numpy, asyncpg, redis | `services/data-engine` |
| Macro brief, regime | Python 3.12, FastAPI | `services/risk-shield` |
| Analyst, headline classifier, watcher judgment, journal scoring of verdicts | Python 3.12, FastAPI, `anthropic` SDK | `services/ai-agent` |
| Watcher: positions, rules, triggers, alert log | Python 3.12, FastAPI, `alpaca-py` | `services/signal-engine` |
| Notifier: Telegram bot | Python 3.12, `python-telegram-bot` (async) — **new service** | `services/notifier` |
| Dashboard | Next.js 16, React 19 | `web` |
| Infra | Docker Compose, Postgres 16, Redis 7 | root, `infra/` |

### External data sources

| Need | Source | Access | Limits / notes |
|---|---|---|---|
| Daily + hourly bars (history) | yfinance 1.5.1 (pinned) | already wired via `providers/yfinance_provider.py` | Batch ≤20, 3s delay, `threads=False`, canary. Dev only. |
| 1-minute bars + real-time quotes for the watcher | **Alpaca Market Data**, IEX feed | free Alpaca account (paper account is enough), `alpaca-py` SDK, REST `data.alpaca.markets/v2/stocks/bars` and websocket stream | IEX feed is real-time but covers only IEX exchange volume; use it for **price**, not for volume-based stats. Key in `.env` as `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`. |
| Ticker news | Finnhub `/company-news`; yfinance `Ticker.news` as fallback | free Finnhub key, 60 calls/min | Also *verify*: Alpaca `/v1beta1/news` availability on free tier. |
| General market news | Finnhub `/news?category=general` | free | |
| Earnings dates + past surprises | Finnhub `/calendar/earnings`, `/stock/earnings` | free | yfinance `Ticker.earnings_dates` as fallback (*verify on 1.5.1*). |
| Analyst recommendations | Finnhub `/stock/recommendation` | free | Price targets are premium on Finnhub — skip. |
| Company profile / sector | Finnhub `/stock/profile2`; yfinance `fast_info` / `info` | free | |
| Filings: 8-K, Form 4 insider trades | **SEC EDGAR** `https://data.sec.gov/submissions/CIK{10-digit}.json`; ticker→CIK from `https://www.sec.gov/files/company_tickers.json` | free, no key | Must send a `User-Agent: <name> <email>` header; ≤10 requests/s. |
| Fundamentals (later) | SEC XBRL `https://data.sec.gov/api/xbrl/companyfacts/CIK{10-digit}.json` | free | Parked; replaces the unreliable yfinance `.info` fundamentals. |
| Macro series | **FRED** `https://api.stlouisfed.org/fred/series/observations` | free key | Series: `VIXCLS`, `DGS10`, `DGS2`, `T10Y2Y`, `DFF`, `DCOILWTICO`, `CPIAUCSL`, `UNRATE`. |
| Index/ETF/futures quotes for regime + night mode | yfinance, one batched call | already wired | `SPY QQQ RSP ^VIX TLT GLD UUP XLK XLU XLP XLV XLY XLF ES=F NQ=F CL=F GC=F` = 17 tickers, **one** `yf.download` per check. |
| Economic calendar (Fed, CPI, jobs) | Static JSON file maintained by hand for the current quarter (`risk-shield/data/econ_calendar.json`) | none | Finnhub `/calendar/economic` is *likely premium — verify*; do not depend on it. |
| Trading calendar | `exchange_calendars` (XNYS) | pip | Holidays, early closes, session times. |
| Notifications | Telegram Bot API via `python-telegram-bot` | free, token from BotFather | |
| LLM | Anthropic API, `anthropic` SDK | `ANTHROPIC_API_KEY` | Models: `claude-opus-5` (default everywhere), `claude-haiku-4-5` (optional classifier). Batches API (50% off) for nightly scanner batches. |
| Options (Phase 10) | yfinance `Ticker.option_chain` (*verify on 1.5.1*); paid later | | |

### Call budget per day (G6 — state and respect)

| Component | Source | Calls / day (market day) |
|---|---|---|
| Analyst dossier, per ticker | yfinance 2 downloads + ~5 Finnhub + 2 EDGAR + 1 FRED read (cached) | ~10 per ticker, cached 15 min |
| Macro/regime check every 5 min in market hours | yfinance, 1 batched download | ~80 |
| Night mode every 30 min | yfinance, 1 batched download | ~30 |
| Watcher price feed | Alpaca websocket | 1 connection, no per-call cost |
| Watcher bleed check | in-process on stored bars | 0 |
| News poll for watched tickers every 5 min | Finnhub, 1 call per ticker | 20 tickers × 78 = ~1,600, under the 60/min cap |
| Journal scoring nightly | data-engine bar store | 0 external |

---

## 3. Indicator set (swing)

Computed in `services/data-engine/indicators/` from stored bars. Pure functions, no I/O.

| Keep | Purpose |
|---|---|
| EMA 20 / 50 / 200 (daily) | trend structure, invalidation levels |
| ATR 14 (daily) | stop distance, disaster line, extension unit |
| RVOL (time-adjusted, existing) | participation |
| RSI 14 | momentum / exhaustion |
| MACD 12/26/9 | momentum turn |
| 52-week position (existing) | context |
| Support / resistance zones (fractal swings + volume clusters, merged within 0.5%) | stop and target levels |

| Add | Purpose |
|---|---|
| Extension = (close − EMA20) / ATR and (close − EMA50) / ATR | "already too high" in one number |
| Relative strength vs SPY and vs sector ETF, 5d and 20d | stock vs market |
| Average dollar volume 20d | liquidity |
| Gap (today open vs yesterday close, in %) and gap history | event behaviour |
| Earnings-day reaction history (last 8 reports: gap %, close-to-close %) | what a report can do to a position |
| Intraday: 5-min bars, 20-bar average volume, day high/low (watcher only) | bleed triggers |

Dropped: ADX, Fibonacci/Camarilla pivots, 5-minute tradability checks, VWAP for entries (VWAP may return for same-day mode later).

---

## 4. Data model additions (one migration per phase; each service writes only its own schema)

```
data_engine.ohlcv_bars      (ticker, interval, ts, open, high, low, close, volume)  PK (ticker, interval, ts)
data_engine.news_items      (id, ticker NULL for market news, published_at, source, title, url, summary, sentiment JSONB NULL)
data_engine.events          (ticker, event_type 'earnings'|'exdiv', event_at, meta JSONB)
data_engine.filings         (ticker, form, filed_at, accession, url, meta JSONB)

risk.macro_briefs           (id, generated_at, regime, health_score, brief_text, inputs JSONB)

ai.verdicts                 (id, user_id, ticker, horizon, asked_at, dossier JSONB, macro_brief_id, verdict, confidence, reasoning, plan_proposed JSONB, model, tokens_in, tokens_out)
ai.verdict_outcomes         (verdict_id, horizon_days 1|5|20, return_pct, mae_pct, mfe_pct, stop_hit BOOL, target_hit BOOL, scored_at)
ai.judgments                (id, position_id, triggered_at, trigger, inputs JSONB, answer 'hold'|'tighten'|'partial'|'exit', new_stop, confidence, reasoning)

signals.positions           (id, user_id, ticker, mode 'swing'|'same_day', entry_price, size, opened_at, plan_proposed JSONB, plan_accepted JSONB, temperament, status 'proposed'|'open'|'closed', closed_at, close_price, close_reason)
signals.alerts              (id, position_id, sent_at, band 'hard'|'soft'|'headsup', kind, message, price_at, outcome JSONB NULL)
signals.watch_log           (ts, event 'heartbeat'|'gap'|'feed_down'|'feed_up', detail)

notify.channels             (user_id, channel 'telegram', address, enabled)   -- new schema `notify`
users.settings              (user_id, account_size, risk_per_trade_pct, default_temperament)
```

`user_id` is a UUID; a single fixed development user is seeded by migration until auth exists (D18).

---

## 5. Phase 0 — Make the existing stack real (small, unblocks everything)

| Part | Build | Test | Commit |
|---|---|---|---|
| 0.1 | `.env.example` with every key used by compose + services (`DB_PASSWORD`, `DATA_PROVIDER`, `ANTHROPIC_API_KEY`, `FINNHUB_API_KEY`, `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `FRED_API_KEY`, `TELEGRAM_BOT_TOKEN`, `EDGAR_USER_AGENT`). Create a real `.env`. `docker compose up -d` boots postgres, redis, data-engine, web; `curl :8001/health` shows `db_connected: true`. | manual curl output pasted | `chore: env example + first verified compose boot` |
| 0.2 | Mount `infra/supabase/migrations/` into postgres `/docker-entrypoint-initdb.d/` (fresh volume applies 001). Add `scripts/migrate.sh` (bash + `psql`, applies `NNN_*.sql` in order, tracks in `public.schema_migrations`) for existing volumes. | fresh volume → `\dn` lists 5 schemas; rerun script is a no-op | `feat: apply migrations on init + migrate.sh` |
| 0.3 | `HEALTHCHECK` in all 4 FastAPI Dockerfiles and web; `depends_on: condition: service_healthy` for web → data-engine. | `docker compose ps` shows `healthy` | `feat: container healthchecks` |
| 0.4 | Remove the dead paths: "Scanner 1" tab + `web/app/api/scanner/run/route.js`, `web/lib/scanner/*`, `web/lib/data/*`, `yahoo-finance2` and `firebase` from `package.json`. `scanner/` folder stays as frozen reference (add `scanner/README.md` saying so). Update `docs/overview.md` + `CLAUDE.md` wording. | `npm run build` green; Pro Scanner tab still works | `refactor: single scanner path; freeze scanner/ as reference` |
| 0.5 | Fixture data provider `providers/fixture_provider.py` implementing `DataProvider` from `tests/fixtures/` (parquet/JSON). One manual recording script `tests/record_fixture_live.py` run once (1 ticker first, then the set). | `tests/test_fixture_provider.py` | `feat: fixture provider for offline tests` |
| 0.6 | macOS always-on notes in `docs/runbook.md`: Energy settings, `caffeinate -dims`, Docker Desktop auto-start, what to check after a Wi-Fi drop. | n/a | `docs: runbook` |

---

## 6. Phase 1 — Bar store + indicators (data-engine)

| Part | Build | Test | Commit |
|---|---|---|---|
| 1.1 | Migration `002_bars.sql`: `data_engine.ohlcv_bars`. `db.py`: `upsert_bars()`, `get_bars(ticker, interval, since)`. | `tests/test_bars_store.py` (mocked pool: asserts SQL params) | `feat: ohlcv bar store` |
| 1.2 | `POST /stock/{ticker}/refresh`: downloads daily 2y + hourly 3mo for **one** ticker via the provider (existing delays), upserts, `del` + `gc.collect()`. Returns counts. Reject if refreshed < 15 min ago (Redis key). | `tests/test_refresh_endpoint.py` with fixture provider | `feat: single-ticker refresh` |
| 1.3 | Scan pipeline persists bars for winners at the end of a scan (same upsert), then drops frames. | extend `test_scanner_pipeline.py` | `feat: scan persists winner bars` |
| 1.4 | `GET /stock/{ticker}/bars?interval=1d|1h&since=` — DB only, 404 if absent. Never falls through to the provider. | `tests/test_bars_endpoint.py` | `feat: bars endpoint` |
| 1.5 | `indicators/`: split existing `technical.py` into `moving_averages.py`, `volatility.py`, `momentum.py` (RSI, MACD), `volume.py`; add `extension()`, `relative_strength()`, `avg_dollar_volume()`, `gap()`. Keep function names used by the scanner. | `tests/test_indicators.py` with hand-computed expectations on tiny frames | `refactor: indicator modules + swing additions` |
| 1.6 | *(merge and selection superseded 2026-09-24 — ATR-width merge, the window's nearest 6 a side; `docs/decisions.md`)* `indicators/levels.py`: fractal swing highs/lows (2 bars each side), volume clusters (price bins, top volume nodes), merge within 0.5%, score (2+ methods +30, volume node +25, tested 2+ times +20, recent +15). Returns top 3 support / top 3 resistance. | `tests/test_levels.py` on a synthetic series with known pivots | `feat: support/resistance zones` |
| 1.7 | `GET /indicators/{ticker}` → one JSON with the §3 set + zones, computed from stored bars; sector ETF map as a static dict in `indicators/sectors.py`. Cached in Redis 15 min. | `tests/test_indicators_endpoint.py` | `feat: indicators endpoint` |

---

## 7. Phase 2 — Context fetchers + dossier (data-engine)

Each fetcher is a class in `providers/context/` with one `async fetch(ticker) -> dict`, its own delay, and a Redis cache with TTL. All HTTP via `httpx.AsyncClient`; tests mock with `respx`.

| Part | Build | Test | Commit |
|---|---|---|---|
| 2.1 | `finnhub_client.py`: thin client with token, 60/min limiter, typed errors. Fetchers: `company_news(ticker, days=7)`, `recommendations(ticker)`, `earnings_calendar(ticker)`, `earnings_surprises(ticker)`, `profile(ticker)`. Migration `003_context.sql` (`news_items`, `events`). Store news + events. | `tests/test_finnhub_fetchers.py` (respx, recorded JSON fixtures) | `feat: finnhub context fetchers` |
| 2.2 | `edgar_client.py`: ticker→CIK map (cached 24h), `recent_filings(ticker, forms=['8-K','4'], days=30)`, User-Agent header, 10 req/s limiter. Migration adds `filings`. | `tests/test_edgar.py` (respx) | `feat: edgar filings fetcher` |
| 2.3 | `earnings_reaction_history(ticker)`: join stored earnings dates with stored daily bars → last 8 reactions (gap %, day move %). Pure function + one query. | `tests/test_earnings_reaction.py` | `feat: earnings reaction history` |
| 2.4 | `GET /dossier/{ticker}?horizon=swing`: assembles indicators + zones + news (raw, sentiment field empty until Phase 4) + events + recommendations + filings + earnings reactions + profile into one camelCase JSON, size-capped (max 30 headlines, 10 filings). Cached 15 min in market hours, 60 min outside. Calls `/stock/{t}/refresh` first if bars are stale > 1 trading day. | `tests/test_dossier.py` (fixture provider + respx) | `feat: dossier endpoint` |
| 2.5 | `tests/dossier_live.py`: manual, 1 ticker, prints the dossier and every call made with timing. Run once, paste output. | manual | `test: dossier live check` |

---

## 8. Phase 3 — Macro brief + regime (risk-shield)

| Part | Build | Test | Commit |
|---|---|---|---|
| 3.1 | `config.py`, `db.py`, `cache.py` mirroring data-engine's (copy pattern, not code). Migration `004_risk.sql` adds `macro_briefs` (health tables exist in 001). | `tests/test_config.py` | `feat: risk-shield service skeleton` |
| 3.2 | `monitors/quotes.py`: one batched yfinance download for the 17 core tickers (daily 1y), stored in Redis 5 min. `monitors/fred.py`: FRED series fetch, cached 6h. | `tests/test_monitors_data.py` (mocked) | `feat: core quotes + FRED` |
| 3.3 | Monitors from Part 5 with the breadth substitute: `vix`, `spy_trend`, `sector_rotation`, `volume`, `cross_asset`, `breadth` (= RSP/SPY 20d ratio slope + A/D from stored scan counts when available). Each returns `{score, raw, detail, stale}`. `scoring/health_calculator.py`, `regime_classifier.py` (HEALTHY ≥70, CAUTIOUS 40–69, DANGER 20–39, CRITICAL <20). | `tests/test_monitors.py` (table-driven: VIX 45 → 5, etc.) | `feat: health score + regime` |
| 3.4 | Scheduler: asyncio task in lifespan, every 5 min in market hours (via `exchange_calendars`), every 30 min otherwise using futures (`ES=F NQ=F`) + VIX. Writes `risk.health_checks`. Publishes `tf:risk:health` only on regime change or ≥10-point move, 15-min min interval, CRITICAL bypasses. `GET /market/health`, `GET /market/indicators`, `GET /market/history`. | `tests/test_alert_throttle.py`, `tests/test_scheduler_gating.py` (frozen time) | `feat: regime scheduler + endpoints` |
| 3.5 | Market news: Finnhub general news poll every 15 min → `data_engine.news_items` with `ticker NULL` **via data-engine HTTP** (`POST /news/ingest`), not a cross-schema write. Static `data/econ_calendar.json` (Fed meetings, CPI, jobs dates for the current quarter) + `GET /market/calendar?days=7`. | `tests/test_market_news.py`, `tests/test_calendar.py` | `feat: market news + econ calendar` |
| 3.6 | Macro brief: `POST /macro/brief/generate` calls ai-agent `POST /brief/macro` (Phase 4.6) with regime, indicators, last 24h market news, next 7 days calendar, FRED snapshot. Runs 07:30, 12:30, 16:30 ET and on regime change. Stored in `macro_briefs`. `GET /macro/brief` returns latest. | `tests/test_macro_brief_flow.py` (ai-agent mocked) | `feat: scheduled macro brief` |

---

## 9. Phase 4 — Analyst (ai-agent)

| Part | Build | Test | Commit |
|---|---|---|---|
| 4.1 | `config.py` (models, daily call cap, `LLM_MODEL_CLASSIFIER`), `providers/base.py` (`LLMProvider` with `complete_structured(system, user, schema)`), `providers/anthropic_provider.py` using the SDK with structured output, prompt caching on the system block, adaptive thinking, refusal fallbacks, usage logging. Daily call cap enforced in Redis; 429 → stop, no retry storm. | `tests/test_anthropic_provider.py` (SDK client mocked; asserts request shape, cap behaviour) | `feat: llm provider layer` |
| 4.2 | Headline classifier: `prompts/headline_classify.md`, schema `{relevance: high|medium|low, sentiment: -1..1, category: guidance|analyst|legal|product|macro|insider|other, one_line}`. `POST /classify/headlines` (batch of up to 30). Writes back sentiment to `news_items` via data-engine `POST /news/{id}/sentiment`. | `tests/test_classifier.py` (provider mocked, schema validated) | `feat: headline classifier` |
| 4.3 | Verdict schema (`models/verdict.py`): `verdict: go|wait|avoid`, `confidence 0–100`, `reasoning`, `thesis` (3 bullets), `thesis_breakers` (list of conditions/news that void it), `plan: {stop, stop_basis, disaster_line, invalidation, targets:[{price, r}], size_shares, size_basis, earnings_in_days, hold_through_earnings: false, horizon_days}`, `risk_flags`. Pure plan math in `grading/plan_math.py`: stop = nearest support zone low − 1×ATR (rounded), disaster = stop − 1×ATR, targets = next resistance zones with R = (target − entry)/(entry − stop), reject if best R < 1.5, size = floor(account × risk% ÷ (entry − stop)). | `tests/test_plan_math.py` (hand-checked numbers) | `feat: verdict schema + plan math` |
| 4.4 | `prompts/verdict.md` (system: role, rules incl. "never invent price levels; levels come from plan_math", output schema) and `POST /analyze/{ticker}?horizon=swing&entry=<optional>`: fetch dossier + macro brief + user settings, classify unclassified headlines, compute plan candidates, call the model, store `ai.verdicts` with the full dossier snapshot. Migration `005_ai.sql` (`verdicts`, `verdict_outcomes`, `judgments`, `users.settings` seed). | `tests/test_analyze.py` (all HTTP + LLM mocked) | `feat: analyze endpoint` |
| 4.5 | Journal scoring: nightly task scores every verdict at +1/+5/+20 trading days using data-engine bars: return %, MAE, MFE, would stop / target have hit. `GET /journal/stats?days=90`: hit rate by verdict, avg R, calibration (confidence bucket vs outcome). | `tests/test_scoring.py` (synthetic bars) | `feat: verdict scoring + stats` |
| 4.6 | `POST /brief/macro`: prompt `prompts/macro_brief.md`, output `{regime_view, key_risks[], upcoming[], one_paragraph}`. Cached prefix = system prompt; volatile = inputs. | `tests/test_macro_brief.py` | `feat: macro brief generation` |
| 4.7 | Similar-case retrieval (v1, no embeddings): for a ticker, pull past verdicts/judgments with same category flags + regime + extension bucket, include their outcomes in the prompt (max 6). | `tests/test_similar_cases.py` | `feat: similar past cases in prompt` |
| 4.8 | `tests/analyze_live.py`: manual, 1 ticker, prints verdict + plan + token usage. Then 5 tickers. Paste outputs. | manual | `test: analyst live check` |

---

## 10. Phase 5 — Notifier (Telegram)

| Part | Build | Test | Commit |
|---|---|---|---|
| 5.1 | New service `services/notifier` (Dockerfile, `requirements.txt` with `python-telegram-bot`, `redis`, `httpx`, `asyncpg`), compose entry. Migration `006_notify.sql` (`notify.channels`). `/start` links the chat id to the dev user. | `tests/test_link.py` | `feat: notifier service skeleton` |
| 5.2 | Outbound: subscribe Redis `tf:notify` (`{user_id, band, text, buttons[]}`) → `sendMessage`. Throttle: soft/headsup max 1 per position per 15 min, hard never throttled. | `tests/test_outbound.py` (bot mocked) | `feat: telegram outbound + throttle` |
| 5.3 | Commands: `/analyze T [entry]` → ai-agent → formatted verdict + plan; `/watch T entry P [size N]` → signal-engine proposal flow (Phase 6.3); replies `ok` / `stop X` / `size N` / `no` on a proposal; `/positions`; `/close T price P`; `/settings account A risk R`; `/status` (feed up? last heartbeat?). | `tests/test_commands.py` | `feat: telegram commands` |

Message templates live in `notifier/templates.py` and match the examples agreed in discussion (warning / EXIT / proposal).

---

## 11. Phase 6 — Watcher (signal-engine)

| Part | Build | Test | Commit |
|---|---|---|---|
| 6.1 | Service skeleton (`config.py`, `db.py`, `cache.py`), migration `007_positions.sql` (`positions`, `alerts`, `watch_log`). CRUD: `POST /positions` (status `proposed`), `GET /positions`, `POST /positions/{id}/accept` (body: edits), `POST /positions/{id}/close`. Refuse accept without a stop. Store `plan_proposed` and `plan_accepted` separately. | `tests/test_positions_api.py` | `feat: positions api` |
| 6.2 | Plan proposal: `/watch` → call ai-agent `/analyze/{t}?entry=P` → plan → position `proposed` → notify with buttons. Accept/edit path validates: stop < entry, disaster < stop, targets > entry. | `tests/test_proposal_flow.py` (mocked) | `feat: proposal flow` |
| 6.3 | Price feed: `feed/alpaca_stream.py` — websocket subscription to 1-minute bars for open positions' tickers (IEX feed), reconnect with backoff, heartbeat every minute to `watch_log`; fallback `feed/alpaca_poll.py` REST 1-min bars if the stream is down > 2 min. Maintains in-memory ring buffer of 5-min bars per ticker (built from 1-min), day high/low, 20-bar avg volume. | `tests/test_feed.py` (fake stream), `tests/test_bar_builder.py` | `feat: alpaca price feed` |
| 6.4 | Hard band rules (`rules/hard.py`), pure functions on `(position, bars, clock, regime, events)`: hourly close < stop → EXIT; touch ≤ disaster line → EXIT NOW; daily close meets invalidation → EXIT; time stop reached → EXIT; earnings within 24h and `hold_through_earnings=false` → EXIT BEFORE CLOSE; regime CRITICAL → EXIT ALL. Fixed templates, no model. | `tests/test_hard_rules.py` (table-driven, both stop modes) | `feat: hard band rules` |
| 6.5 | Wake triggers (`rules/wake.py`): 2 consecutive red 5-min bars with volume > 2× 20-bar avg; drop > 1 ATR from day high; underperforming SPY by > 1.5% within 30 min; new relevant headline (classifier relevance high); recommendation downgrade; regime downgrade. Temperament scales thresholds (patient ×1.3, normal ×1, nervous ×0.7). Debounce: one wake per trigger type per 30 min. | `tests/test_wake_triggers.py` | `feat: wake triggers` |
| 6.6 | Tier-two judgment: `POST /judge/position` in ai-agent (`prompts/judge.md`): inputs = position + plan + last 30 5-min bars + SPY/sector move + fresh headlines + earnings proximity + regime + similar past judgments; output `{answer: hold|tighten|partial|exit, new_stop?, confidence, reasoning}`. **Enforcement in signal-engine, not in the prompt:** `new_stop` accepted only if higher than current; any answer that loosens is discarded and logged. Stores `ai.judgments`. | `tests/test_judge_enforcement.py` | `feat: watcher judgment + asymmetry enforcement` |
| 6.7 | The loop: every minute in market hours: hard rules → if fired, alert + close position + log; else wake triggers → if fired, judgment → alert per answer (tighten updates `plan_accepted.stop` with history). Night mode every 30 min: news + regime only. Alerts published to `tf:notify`; rows in `signals.alerts`. | `tests/test_watch_loop.py` (frozen time, fake feed, mocked ai-agent) | `feat: watcher loop` |
| 6.8 | Same-day mode: `mode=same_day` adds rule "at 15:45 ET, if not confirmed (price ≥ entry and 20-bar volume ≥ avg) → EXIT BEFORE CLOSE". | `tests/test_same_day.py` | `feat: same-day mode` |
| 6.9 | Alert scoring nightly: for every alert, price +1 and +5 trading days vs price at alert; `right` for exit if lower, for hold if higher. `GET /journal/alerts?days=90`: early-exit precision vs stop-only baseline (simulate what the stop alone would have done on the same bars). | `tests/test_alert_scoring.py` | `feat: alert scoring + baseline` |
| 6.10 | `tests/watcher_live.py`: paper position on 1 ticker during market hours for one session; paste the alert log and heartbeat gaps. | manual | `test: watcher live session` |

---

## 12. Phase 7 — Dashboard slices (web, minimal; Telegram is the primary UI until this is done)

| Part | Build | Test | Commit |
|---|---|---|---|
| 7.1 | Next.js API proxies: `/api/positions`, `/api/verdicts`, `/api/journal`, `/api/market/health`, `/api/macro/brief` (server-side, backend URLs never in the browser). | route unit tests with fetch mocked | `feat: api proxies` |
| 7.2 | `/dashboard`: regime badge + macro brief, open positions table (plan, current price, distance to stop in ATR), last verdicts. | `npm run build` | `feat: dashboard v1` |
| 7.3 | `/journal`: stats tables from 4.5 and 6.9. | build | `feat: journal page` |
| 7.4 | `/stock/[ticker]`: candlesticks with `lightweight-charts`, zones overlay, plan levels overlay for open positions. | build | `feat: stock page with zones` |

---

## 13. Phase 8 — Scanner as feeder

| Part | Build | Test | Commit |
|---|---|---|---|
| 8.1 | Nightly: after the scan, send the top N winners (N=20) through `/analyze` using the Anthropic **Batches API** (50% cost); results tagged `source=scan`. | `tests/test_scan_batch.py` (batch client mocked) | `feat: nightly analyst batch` |
| 8.2 | Morning Telegram brief 07:45 ET: regime, macro paragraph, top 5 `go` verdicts with R, calendar for the day. | `tests/test_morning_brief.py` | `feat: morning brief` |

---

## 14. Phase 9 — Hardening and always-on

| Part | Build | Test | Commit |
|---|---|---|---|
| 9.1 | `docker-compose.prod.yml` + `scripts/env-*.sh` from `plan-x.md` §5 (project-named, disposable stacks, fixture provider default). | two stacks side by side | `feat: prod overlay + env scripts` |
| 9.2 | GitHub Actions: run every `test_*.py` and `npm run build` with fixtures, no network (`--network none` in a Docker job). | CI green | `ci: offline test workflow` |
| 9.3 | Watcher self-monitoring: if no heartbeat for 3 min in market hours → Telegram "watcher down"; nightly Postgres dump to a local folder. | `tests/test_selfcheck.py` | `feat: self-check + backups` |
| 9.4 | VPS move: one Ubuntu VM, Docker, `git pull`, copy `.env`, `env-up.sh live`. Runbook section. | smoke script | `docs: vps runbook` |

---

## 15. Phase 10 — Parked features (build later, in this order unless priorities change)

| # | Feature | Notes / sources |
|---|---|---|
| P1 | **Options data** for analyst + watcher: put/call volume ratio, IV rank, unusual volume at strikes near price. Free: yfinance `option_chain` (*verify 1.5.1*), CBOE delayed pages. Paid later: Polygon options tier. Used as extra evidence in judgments and as a risk flag ("IV crush after earnings"). | End of the plan by decision D19 |
| P2 | Fundamentals from SEC XBRL `companyfacts` (revenue growth, margins, debt) → fundamental score → dossier. Replaces yfinance `.info`. | free |
| P3 | Gemini fallback provider (`google-genai` SDK) behind the same `LLMProvider` interface; auto-switch after 3 failures. | Part 7 of Documents |
| P4 | Broker position sync, read-only: IBKR via `ib_async` + IB Gateway container; Trading212 REST API (read portfolio). Positions appear in the watcher without typing. | never order placement (D6) |
| P5 | Weekly AI performance report + pattern analysis (Part 7 learning loop) once the journal has ≥ 8 weeks. | |
| P6 | Similar-case retrieval v2 with embeddings (pgvector) when v1 keyword retrieval feels thin. | |
| P7 | Social sentiment (Reddit API, X) — low priority, noisy. | |
| P8 | Email + mobile push (FCM) through the same `notify.channels` abstraction. | |
| P9 | SSE real-time dashboard updates (replace 30s polling). | |
| P10 | Huey task queue if scheduled jobs outgrow in-process asyncio loops. | |
| P11 | Backtest harness: replay stored bars + news through analyst/watcher for a past window, score offline. | needs P2 for full dossiers |
| P12 | Per-position stop mode override (touch / close / hourly+disaster) and per-user default temperament in settings. | small |
| P13 | Multi-ticker "ask" (compare 3 tickers) and natural-language questions to the analyst. | |

---

## 16. Going public — checklist (do not start before Phase 9 is done and the journal shows ≥ 3 months of scored results)

| Area | Required before any public user |
|---|---|
| **Data licensing** | Replace yfinance/Finviz entirely with licensed feeds (FMP for fundamentals/news/calendar, Polygon or Twelve Data for bars; Alpaca data terms checked for redistribution). Remove `finvizfinance` and `yfinance` from production images. |
| **Legal** | "Not investment advice" disclaimers on every verdict/alert; terms of service and privacy policy; check with a lawyer whether an alerting/analytics product needs registration in your jurisdiction and in the users' jurisdictions (rules differ between EU/UK/US). Publish the journal methodology so the track record is auditable. |
| **Auth & tenancy** | Supabase Auth (`@supabase/ssr`), RLS on `users.*`, `signals.positions`, `notify.channels`; tier column already in `users.profiles`. Telegram linking per user. |
| **Cost control** | Per-user daily LLM call quota; global daily cap kill-switch (already in 4.1); per-ticker analysis cache shared across users; Batches API for anything not interactive. |
| **Rate limits & abuse** | Per-user and per-IP limits on `/analyze` and `/watch`; input validation `^[A-Z.]{1,6}$` on tickers. |
| **Hosting** | VPS → managed Postgres (Supabase) + containers on Cloud Run or a second VPS; Redis managed; secrets in a secret manager, not `.env`. |
| **Reliability** | Uptime monitor on `/health` of every service; watcher heartbeat alerting to an ops channel; daily backups with a tested restore; status page. |
| **Security** | Dependency pinning + audit; CORS whitelist (replace `allow_origins=["*"]`); HTTPS; no backend ports exposed (prod overlay); security review of prompts against injection from headlines (headline text is untrusted input). |
| **Payments** | Stripe subscriptions mapped to `users.profiles.tier`; feature gates enforced server-side. |
| **Notifications** | Push (FCM) for the mobile app; Telegram stays as an option. |
| **Product** | Onboarding flow, settings page, journal page public per user, mobile app (React Native/Expo consuming the same API). |

---

## 17. Status

Tick when the part's test output has been shown and the commit exists.

- [ ] 0.1 [ ] 0.2 [ ] 0.3 [ ] 0.4 [ ] 0.5 [ ] 0.6
- [x] 1.1 [x] 1.2 [x] 1.3 [x] 1.4 [x] 1.5 [x] 1.6 [x] 1.7
- [x] 2.1 [x] 2.2 [ ] 2.3 [ ] 2.4 [ ] 2.5
- [ ] 3.1 [ ] 3.2 [ ] 3.3 [ ] 3.4 [ ] 3.5 [ ] 3.6
- [ ] 4.1 [ ] 4.2 [ ] 4.3 [ ] 4.4 [ ] 4.5 [ ] 4.6 [ ] 4.7 [ ] 4.8
- [ ] 5.1 [ ] 5.2 [ ] 5.3
- [ ] 6.1 [ ] 6.2 [ ] 6.3 [ ] 6.4 [ ] 6.5 [ ] 6.6 [ ] 6.7 [ ] 6.8 [ ] 6.9 [ ] 6.10
- [ ] 7.1 [ ] 7.2 [ ] 7.3 [ ] 7.4
- [ ] 8.1 [ ] 8.2
- [ ] 9.1 [ ] 9.2 [ ] 9.3 [ ] 9.4

Order of value: Phases 0–2 give a dossier you can read. Phase 4 (skippable 3.6 dependency: use an empty macro brief until Phase 3 lands) gives verdicts. Phase 5 puts them on your phone. Phase 6 is the watcher. If time is short, do 0 → 1 → 2 → 4 → 5 → 3 → 6.

---

## 18. Items verified vs. to verify

Verified from the repo or from current tool documentation during planning: existing yfinance provider limits, compose structure, schema in `001_initial_schema.sql`, Anthropic model IDs and pricing (`claude-opus-5`, `claude-haiku-4-5`), Alpaca IEX feed free access, EDGAR submissions endpoint + User-Agent rule, FRED series IDs, Telegram bot library.

To verify at the start of the part that uses them (mark result in that part's spec): Finnhub free-tier scope for `/calendar/earnings`, `/stock/earnings`, `/calendar/economic`; Alpaca news endpoint on the free tier; yfinance 1.5.1 method names for `earnings_dates`, `recommendations`, `option_chain`; `exchange_calendars` early-close handling for the year in use.
