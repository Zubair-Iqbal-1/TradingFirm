# CLAUDE.md

Guidance for Claude Code working in this repository.

## Project

TradingFirm — a day-trading system that screens the market, applies technical filters, and surfaces trade candidates. FastAPI microservices + a Next.js dashboard, with Postgres and Redis as shared infrastructure.

**Service status** (don't assume otherwise): `data-engine` and the web dashboard are functional. `risk-shield` has its regime path (Part 3.1: config, pool, cache, bounded lifespan, `risk.macro_briefs`; Part 3.2: core-quotes and FRED fetchers in `monitors/`; Part 3.3: six regime monitors, `scoring/` health score + regime; Part 3.4: an XNYS-gated scheduler writing `risk.health_checks` and publishing `tf:risk:health`, plus `GET /market/health`, `/market/indicators`, `/market/history`; Part 3.5: `GET /market/calendar` from a hand-maintained file, and a news poller sending Finnhub general news to data-engine's `POST /news/ingest`; Part 3.6a: the macro brief's inputs, `GET /macro/brief/inputs` over health rows, data-engine's `GET /news/market`, the calendar and a FRED view with last-known + cadence freshness, plus migration 006's `brief` / `trigger` columns; 3.4 follow-up: both loops wait through `wallclock.py` in sleeps of ≤ 60 s, with a host-pause WARNING and `pausedSeconds` on the payload; Part 3.6b: macro brief generation behind `MACRO_BRIEF_ENABLED`, off in prod and the twin: the ai-agent brief contract client, `GET /macro/brief`, `POST /macro/brief/generate`, a 07:30 / 12:30 / 16:30 ET slot loop and a regime trigger from `run_check`'s publish hook; Part 3.4b: night checks at :15 / :45 ET while CME futures trade, capping the latest settle's score by the ES=F / NQ=F move since it, the same cap on market checks, `futures` and `overlay` on every row and on the payload; Part 3.4c: a `weekend` block — LOW / ELEVATED / HIGH with its reasons — on the last eight rows of a weekend-eve session, `GET /market/weekend/log`, and `PUT`/`DELETE /market/weekend/situation` behind `WEEKEND_WRITE_TOKEN`). `signal-engine` and `ai-agent` are empty FastAPI scaffolds.

## Read `.agents/AGENTS.md` first

15 global rules + project rules take priority over generic habits. The ones most likely to bite:

- **G1 — Ask before building.** 3-sentence spec approved before touching files for any new feature/service.
- **G1.5 — Spec tables.** Stateful parts: writes table + failure-branch table in the spec, each branch naming its test function. Keys go through one shared normalization function.
- **G3.5 — Consult before fixing.** On an error, present the root cause and 2 options; don't auto-apply a fix.
- **G6 — Protect external APIs.** Never run untested code against live yfinance/Finviz at scale; delays and canary batches are mandatory. Getting the user's IP rate-limited is the #1 thing to avoid.
- **G7 — Tests alongside code.** Every module gets a unit test; tests never call external APIs (mock the provider).
- **G8 — Clean up memory.** `del` DataFrames + `gc.collect()` after use; never store raw DataFrames in `app.state`.
- **G13 — Verify edits landed.** Absolute paths for every edit; prove scripted edits applied; end each completion report with `git diff --stat <base>..HEAD`.
- **G14 — Never print secrets.** No `docker compose config` / `env` / `cat .env`; check keys by shape only. A mask is not a safeguard.
- **G15 — Production changes only on explicit go.** Migrations, data deletes, prod rebuilds/redeploys, secret rotation: never inside a part's verification; the report lists what is waiting for a go. From 3.6, prod `risk-shield` rebuilds run outside XNYS hours (timing rule below).

## Docs discipline

- **Active plan: `docs/plan-analyst-watcher.md`.** Read it before any part. Its §0 decisions are binding. The user says which part to build.
- **`docs/specs/<part>.md` is the part's spec** (G1): the only file written before approval, committed with the feat commit, corrected to what was approved. Approval is the word "approved" in chat for that part; a plan row is not it.
- **Plan files are read-only** once approved. If a part proves the plan wrong, record the change in `docs/decisions.md` ("supersedes D-n") and note it in `docs/progress.md`.
- **`docs/overview.md` describes what exists now**, never future state. Update it when a part changes architecture, adds a service/endpoint, or adds a third-party API or library.
- **`docs/decisions.md` is append-only.** One entry per decision: date, decision, why, supersedes.
- **`docs/progress.md`**: one row per part (status, commit, date, notes). Update at the end of every part, before the commit.
- **Never delete a plan file.** Superseded plans get a status banner and stay in `docs/`.

## Commands

### Data engine (Python)
```bash
cd services/data-engine
pip install -r requirements.txt
uvicorn main:app --reload --port 8001
```
**8001 is prod** (`tf-data-engine`, live yfinance, real keys); **8011 is the dev twin** (`tf-data-engine-dev`, fixture provider, empty Finnhub key, own database `tradingfirm_dev`, Redis DB 1). Verify parts on 8011; use 8001 for real scans.

**risk-shield: 8003 is prod** (`tf-risk-shield`, real `FRED_API_KEY` and `FINNHUB_API_KEY`); **8013 is its dev twin** (`tf-risk-shield-dev`, empty FRED and Finnhub keys, `tradingfirm_dev`, Redis DB 1, data-engine = `data-engine-dev`). Tests run there:
```bash
docker compose --profile dev up -d --build risk-shield-dev
docker exec tf-risk-shield-dev pytest tests/test_config.py tests/test_cache.py tests/test_db.py tests/test_lifespan.py tests/test_health.py tests/test_migration.py tests/test_ratelimit.py tests/test_fred_client.py tests/test_monitors_data.py tests/test_live_guard.py tests/test_monitors.py tests/test_scoring.py tests/test_scheduler_gating.py tests/test_alert_throttle.py tests/test_scheduler.py tests/test_market_endpoints.py tests/test_finnhub_client.py tests/test_calendar.py tests/test_market_news.py tests/test_fred_view.py tests/test_macro_inputs.py tests/test_wallclock.py tests/test_ai_agent_client.py tests/test_macro_brief_endpoints.py tests/test_macro_brief_flow.py tests/test_overlay.py tests/test_weekend.py tests/test_weekend_inputs.py tests/test_weekend_gate.py tests/test_weekend_row.py tests/test_weekend_log.py -v
```

Tests run inside `tf-data-engine-dev` (host pandas ≠ pinned version). It is a separate container from prod `tf-data-engine`, so prod keeps running: fixture provider, its own database `tradingfirm_dev`, Redis DB 1, pytest baked in via the Dockerfile `dev` stage. Rebuild with `--build` after changing `requirements*.txt`:
```bash
docker compose --profile dev up -d data-engine-dev
docker exec tf-data-engine-dev pytest tests/test_fixture_provider.py tests/test_provider_factory.py tests/test_scanner_pipeline.py tests/test_news_ingest.py tests/test_news_market.py -v
./scripts/dev-db.sh                  # once: create tradingfirm_dev + apply migrations (only needed to poke the dev API on :8011)
```

### Web dashboard (Next.js)
```bash
cd web
npm install
npm run dev      # localhost:3000
npm run build
npm run lint
```

### Full stack (Docker)
```bash
docker compose up -d                                              # prod-like
docker compose -f docker-compose.yml -f docker-compose.dev.yml up # hot-reload dev

curl http://localhost:8001/health
curl -X POST http://localhost:8001/scan/run -H "Content-Type: application/json" \
  -d '{"price_min": 10, "price_max": 40}'
curl http://localhost:8001/scan/status
curl http://localhost:8001/scan/results
curl "http://localhost:8001/dossier/AAPL?horizon=swing"
```
Requires `.env` with `DB_PASSWORD` set — compose fails fast without it.

## Conventions

- **Commit prefixes** `feat:`, `fix:`, `refactor:`, `docs:`; commit after every tested chunk (G12).
- **API JSON is camelCase, Python is snake_case.** `scanners/models.py` aliases every Pydantic field (`market_cap` → `marketCap`). Add new response fields the same way.
- **yfinance/Finviz limits**: ≤20 tickers per `yf.download()`, 3s between batches, 1.5–2s between `yf.Ticker` calls, `threads=False`, never pass `session=`. Rationale in `.agents/AGENTS.md` Part 2.

## Never touch / handle with care

- **Name the `test_*.py` files when running pytest** in `services/data-engine`. `pytest.ini` (`testpaths = tests`, `python_files = test_*.py`) now keeps a bare `pytest` from collecting `tests/full_scan_test.py`, which fires a real Finviz + yfinance scan on import — naming files is habit and belt-and-braces, no longer the only guard.
- **`tests/smoke_test_pipeline.py`, `tests/full_scan_test.py`, `tests/record_fixture_live.py`, `tests/record_finnhub_live.py`, `tests/record_edgar_live.py`, `tests/record_earnings_live.py`, `tests/dossier_live.py`** are live-API scripts. Run manually and deliberately, never in CI. Same for risk-shield's **`tests/fred_live.py`, `tests/quotes_live.py`, `tests/finnhub_news_live.py`**, which run only through the isolated `docker run` lines in `docs/specs/3.2.md` decision 11 (no compose network, unroutable `DATABASE_URL`/`REDIS_URL`, only the one key they need from `.env`; the Finnhub one writes the 20-item fixture, spec 3.5 decision 10); `tests/live_guard.py` exits 2 otherwise.
- **One live scan pipeline: `services/data-engine`** (proxied by `web/app/api/scanner/pro/route.js`). `scanner/` is a frozen reference — don't build on it (`scanner/README.md`). Its `results.json`/`status.json` are generated output.
- **G15 here means**: prod `./scripts/migrate.sh` and `docker compose up -d data-engine` (any prod service) wait for a go. So does `docker compose build <service>`: it moves the tag compose deploys — verify a prod stage with `docker build --target prod -t tradingfirm-<service>:verify` instead. `scripts/dev-db.sh` and the dev twin need no ask. Parts that applied prod migrations before the rule: 0.2, 1.1, 2.1.
  - **Timing (from Part 3.6):** prod `risk-shield` rebuilds happen outside XNYS hours, after the 16:20 ET settle check is recorded or before 09:30 ET. Weekends and XNYS holidays are always fine.
  - **During hours** only if the report names the slot(s) skipped and confirms the settle row was not one of them.
  - **`data-engine`** rebuilds avoid the premarket scan window.
  - **Why:** the scheduler has no catch-up, and a missed settle breaks the next day's trend, which 3.6 and Phase 6 read (`docs/decisions.md` 2026-09-10, G15 timing; 3.5 waived it).
- **Migrations must be re-runnable**: `IF NOT EXISTS` everywhere, no plain `INSERT` seeds. Why: `scripts/migrate.sh` header, `docs/decisions.md` 2026-09-05.
- **Alpha Vantage takes its key as a query parameter** (`apikey=`; no header form exists), one of two approved exceptions to "secrets never in URLs" (spec 2.3 decision 13; FRED is the other, below). Two conditions hold it in place, both in `providers/context/alphavantage_client.py`: the `httpx` logger is pinned to WARNING there (at INFO it logs the full URL), and typed errors never chain or format the httpx exception (`raise ... from None`; `HTTPStatusError.__str__` carries the URL). Never log `resp.url`. Free tier: 5 req/min, 25/day, and the daily cap arrives as HTTP 200 with an `Information` body, not a 429.
- **FRED takes its key as a query parameter too** (`api_key=`; no header form exists), the second approved exception (spec 3.2 decision 7), on the same two conditions, both in `services/risk-shield/monitors/fred_client.py`: `httpx` logger pinned to WARNING, typed errors raised `from None` with a series id + status message only (the body's `error_message` is inspected for `api_key`, never echoed). Limit 120 req/min, then 429; a 423 or ignored 429s mean blocked. Both stop the snapshot walk and start the cooldown.
- **yfinance 1.5.1 `download` never raises on a rate limit**: it catches `YFRateLimitError` per ticker and only logs it, so risk-shield's `monitors/quotes.py` watches the `yfinance` logger behind an exact version guard. A cold container also makes 2 requests per ticker (a timezone fetch first). Bumping the yfinance pin means re-checking both.
- **risk-shield's scheduler downloads live on a cadence** (Parts 3.4 and 3.4b). It runs only where `SCHEDULER_ENABLED=true` (the prod compose service).
  - **Night checks (3.4b)** add ~31 downloads a weeknight of `ES=F NQ=F` only, at :15 and :45 ET: 155 slots a week.
  - **`CMES` models neither the daily 17:00–18:00 ET halt nor the Friday 17:00 close**, so `night_slots_for_day` cuts both by hand. It also cuts the 45 minutes before an XNYS open, because a refusal's 900 s cooldown would otherwise still block the 09:30 download.
  - **`tests/futures_live.py`** is a live script like 3.2's canaries: manual, through the isolated `docker run` line in `docs/specs/3.4b.md` decision 3 only.
  - The dev twin hard-codes `SCHEDULER_ENABLED=false`, because it has no quotes fixture.
  - The twin also hard-codes `HEALTH_CHANNEL=tf:risk:dev:health`, because Redis pub/sub ignores the DB index. `test_twin_never_publishes_on_prod_channel` guards it.
  - Never drop either override. Keep `--workers 1` pinned in the Dockerfile: two workers are two schedulers.
  - **Loops wait only through `wallclock`** (3.4 follow-up): `sleep_until`, or `wait_seconds` chunks where a chunk must also answer a queue (the macro brief loop, 3.6b). A single `asyncio.sleep` or `wait_for` toward a far target runs on Docker's monotonic clock, which stops while the Mac sleeps (decisions 2026-09-11). The health payload's `PAYLOAD_KEYS` is append-only.
  - **`scheduler.on_check_published`** (3.6b) is None unless the lifespan sets it to `macro_brief.request_brief` (brief flag on), and it is reset on shutdown. `scheduler` never imports `macro_brief`, which imports `scheduler`.
- **risk-shield's news poller calls Finnhub every 15 min, around the clock** (Part 3.5). It runs only where `NEWS_POLL_ENABLED=true` (the prod compose service).
  - The dev twin hard-codes `NEWS_POLL_ENABLED=false`, `FINNHUB_API_KEY=""` and `DATA_ENGINE_URL=http://data-engine-dev:8001`, so it can never poll or write into prod's database. `test_twin_never_ingests_into_prod_data_engine` guards all three. Never drop them.
  - **The news limits exist twice**, as `NEWS_*` in data-engine's `main.py` (the route's 422) and in risk-shield's `news_poller.py` (the converter). `test_ingest_limits_pinned_to_spec` and `test_converter_limits_pinned_to_spec` pin both. Change both or neither: a drift means a 422 on every poll.
  - **risk-shield reads data-engine's Redis key `tf:cache:finnhub`** (read-only, fail-open), because a Finnhub 429 is account-level. Renaming data-engine's Finnhub cooldown means changing `DATA_ENGINE_FINNHUB_COOLDOWN_KEY` too; tests on both sides pin the name.
- **risk-shield's weekend block (Part 3.4c) runs on the live check path.** It is the only place a health check makes an HTTP call: `GET /news/market` on data-engine, 24 h, behind a 300 s Redis reuse, so a Friday afternoon costs about two calls for eight rows. Nothing in it may raise: every section degrades to a status, and a raise is caught in `scheduler.weekend_block`.
  - **The block never changes a score, a trend or a publish decision**, and it can never fail a publish: `weekend.drop_if_nonfinite` walks it before `json.dumps(allow_nan=False)` can see it. `PAYLOAD_KEYS` stays append-only.
  - **Night rows carry no block** (decisions 2026-09-14): a night check has no quotes view, so its block would be the settle's numbers under a newer timestamp. `test_night_check_has_no_weekend_key` guards it.
  - **`WEEKEND_WRITE_TOKEN` is the service's only write-route secret** (`X-TF-Token`, `hmac.compare_digest`). Empty = the route answers 503, never open; the twin hard-codes it empty and `test_situation_route_disabled_without_secret` guards that. Check it by shape only (G14).
  - **The econ calendar's fourth type, `event`**, is free text on any date including weekends, with no `sources` entry — the renewal step below is unchanged, and `event` rows are yours to add and prune.
  - **The level numbers are provisional** like 3.3's and 3.4b's; `GET /market/weekend/log` is what retunes them.
- **The macro brief stays off until ai-agent's `POST /brief/macro` exists** (Part 3.6a; 3.6b generates; Part 4.6 builds the route).
  - Prod compose sets `MACRO_BRIEF_ENABLED: "false"`. The dev twin hard-codes `MACRO_BRIEF_ENABLED=false` and `AI_AGENT_URL=http://ai-agent.invalid:8004`, so it can never reach an LLM. `test_twin_never_calls_prod_ai_agent` guards both. Never drop them.
  - **The news-read bounds exist twice:** data-engine's `NEWS_MARKET_*` (168 h / 100) and risk-shield's `macro_inputs.DATA_ENGINE_NEWS_MAX_*`. `test_news_market_bounds_pinned_for_risk_shield` and `test_inputs_news_request_within_route_bounds` pin both. Change both or neither.
  - `GET /macro/brief/inputs` is unauthenticated and its first call after 6 h can make 8 FRED requests; the 60 s reuse and the lock bound it.
  - **The brief contract exists twice:** risk-shield's `ai_agent_client.BRIEF_LIMITS` (pinned by `test_brief_limits_pinned_to_spec`) and the copy ai-agent keeps from 4.6. camelCase, rejected never truncated, 16,000 bytes measured like `macro_inputs.encoded_size` (decisions 2026-09-11, "Part 4.6's brief contract is camelCase"). Change both or neither.
  - **Flag on before 4.6 ships:** every slot, regime request and manual call assembles the inputs and gets one 404 from the scaffold: one WARNING each, no row, `lastBriefError = "ai-agent: HTTP 404 (no /brief/macro)"`, and the manual POST answers 502. Nothing backs off, so the first slot after 4.6 stores a row. Cost while it lasts: one 404 per call, plus the FRED reads the inputs make (6 h cache).
  - `POST /macro/brief/generate` is unauthenticated, blocks up to 180 s, and allows one call per 10 min after any stored brief; it answers 503 while the flag is off.
- **The econ calendar is a hand-maintained file, `services/risk-shield/data/econ_calendar.json`, covering through 2026-12-31.** Renew it by 14 days before `coversThrough` (2026-12-17). From then on `/health`'s `calendarCoverageShort` turns true, and the news poller logs a daily WARNING. The renewal step:
  1. Fetch the Fed and BLS schedules: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm, https://www.bls.gov/schedule/news_release/cpi.htm, https://www.bls.gov/schedule/news_release/empsit.htm
  2. Append the next quarter's FOMC decisions (14:00 ET, second meeting day), CPI releases and jobs reports (08:30 ET). Move `coversThrough` and `retrieved`.
  3. Update `SPEC_TABLE` and the coverage asserts in `tests/test_calendar.py` to match, and run it in the twin.
  4. The file ships in the prod image, so the prod rebuild waits for a go (G15).
- **SEC EDGAR needs a declared `User-Agent`** (`EDGAR_USER_AGENT="<app> <email>"`, header only, never a URL or a fixture); one process-wide limiter of 10 req/s (the SEC cap), and a 403 means blocked: stop, never retry.
- **Never write into another service's Postgres schema** (`data_engine`, `signals`, `risk`, `users`, `ai`). Cross-service communication is HTTP + Redis pub/sub only.
