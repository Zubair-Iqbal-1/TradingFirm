# Development Rules

> These rules apply to ALL projects and conversations. Violations waste the user's time and money.

---

## PART 1: GLOBAL RULES

### G1: Ask Before Building
**When:** Starting any new feature, service, or integration — every plan part included
→ Use `/grill-me` to interview the user — resolve ALL design decisions first
→ Write the spec to `docs/specs/<part>.md`: 3 sentences (what it does, what it must NOT do, acceptance criteria), then the G1.5 tables and the decisions the plan row leaves open
→ The spec file is the only file written before approval. No other file edit for the part until the word "approved" appears in chat for that part
→ A complete plan row is not a substitute for approval. "Do part X, stop when done" means: post the spec, then stop
→ The spec file is committed with the part's feat commit, corrected to what was approved
→ NEVER assume a library, data source, or architecture — always ask
→ Why: Part 2.2 was built on "Do step 2.2 only"; its own report then listed five choices the plan row never covered — exactly what the spec review exists to catch

### G1.5: Spec Tables
**When:** The part writes state (Postgres, Redis, disk) or calls a dependency
→ After the 3-sentence spec, two tables. "n/a" cells are allowed, blank cells are not:
  Writes: `write | before/after success | state left if the op fails after it`
  Failure branches: `branch | fails open/closed (both valid) | test function` — cover at least: dependency down, bad input, empty result, repeat call
→ Keys and lookups: one named normalization function; every endpoint touching that key calls it
→ Approval check: every failure-branch row names a test function that exists. No name, no approval

### G2: Test Small First
**When:** Any new code, pipeline, or integration is ready to test
→ Test 1 item → verify output → then 5 → then full load
→ Show the user the ACTUAL output before saying "it works"
→ NEVER say "that's expected" when something fails — investigate first
→ Log sample data at every pipeline stage (first 3 items minimum)

### G3: One Strike
**When:** Something fails or returns an error
→ STOP. Do NOT retry blindly.
→ Analyze root cause with actual evidence
→ Fix it, test with 1 item, only then retry
→ Never burn tokens/time/rate-limits guessing

### G3.5: Consult Before Fixing
**When:** An error occurs and you have a proposed fix
→ Do NOT apply the fix immediately
→ Show the user: (1) what went wrong, (2) your proposed fix, (3) the best alternative approach
→ Present both options side by side — let the user choose
→ Only apply after the user says which one to use
→ This prevents wasted tokens on wrong guesses and gives the user control

### G4: Verify Library Before Use
**When:** Using any library for the first time, or resuming work after a break
→ Run `library.__version__` and compare with requirements.txt
→ If versions differ (especially major version), search for breaking changes FIRST
→ Test the library's output with 3 lines of code — print raw output, verify it's correct
→ Pin the version in requirements.txt BEFORE building anything on it
→ NEVER assume the API is the same across major versions — it never is

### G5: Blame Yourself First
**When:** API returns 429, rate-limit, timeout, or unexpected error
→ Run `library.__version__`, compare with requirements.txt
→ Verify your call parameters match the installed version's docs
→ Run a minimal 3-line isolated test
→ Only AFTER all 3 checks pass: consider external cause
→ If you suggest "wait 15 min", "use VPN", or "switch network" without completing all 3 steps, you are WRONG
→ "Wait 15 minutes" is not debugging — it's avoidance
→ External blame is the LAST resort, not the first guess

### G6: Protect External APIs
**When:** Making any call to an external API or scraper
→ Research rate limits BEFORE making the first request — document them
→ Add delays between all calls (minimum 1-2 seconds)
→ Handle 429 responses — stop immediately, back off, never retry-storm
→ NEVER run untested code against a live API at scale — test with 1 call first
→ If unsure whether a call is safe, DON'T make it — ask the user first
→ NEVER risk getting the user's IP blocked — treat this as the #1 priority

### G7: Write Tests Alongside Code
**When:** Building any module or modifying existing code
→ Every module gets at least 1 unit test before moving on
→ Test edge cases: empty input, single item, error responses
→ Tests must run without external APIs (mock the providers)

### G8: Clean Up Memory
**When:** Working with large data (DataFrames, bulk downloads, API responses)
→ `del` every large DataFrame immediately after use, then `gc.collect()`
→ Close ALL connections on shutdown — DB pools, Redis, HTTP sessions
→ Never store raw DataFrames in app.state — only lightweight results
→ Background tasks MUST clean up their own variables — no leaks

### G9: Log Smart
**When:** Building pipelines or multi-step processes
→ Log sample data at each stage (first 3 items)
→ Log rejection reasons with counts
→ Log errors with context (ticker name, step number, input data)
→ WARNING for recoverable issues, ERROR for failures

### G10: Keep It Simple
**When:** Designing or implementing anything
→ Build the simplest working version first — add complexity when asked
→ Question whether the old approach still makes sense before porting
→ If a library has issues, consider alternatives — don't stack workarounds
→ Think TWICE, verify FIRST, then code

### G11: Be Honest
**When:** Reporting results, failures, or status to the user
→ Show ACTUAL output, not summaries — if a test passes, show the response
→ If something fails, say it clearly — never minimize or dismiss
→ 0 results is ALWAYS a problem — investigate, never say "expected"
→ If you don't know, say "I don't know" — never hallucinate or cover up

### G12: Commit Often
**When:** Completing a task or meaningful chunk of work
→ Commit with clear prefixes: `feat:`, `fix:`, `refactor:`, `docs:`
→ Never let more than 1 task go uncommitted
→ Push regularly — unsaved work is lost work

### G13: Verify Edits Landed
**When:** Any file edit — especially scripted or batched ones — and every completion report
→ Edit with absolute paths. Never rely on a `cd` that persisted from an earlier command
→ After a scripted edit, show the grep or diff that proves it applied BEFORE taking the next step
→ End every completion report with `git diff --stat <base>..HEAD`, where `<base>` is the last commit before the part
→ Why: two consecutive parts each had an edit pass silently not apply. A passing test is not proof that the intended edit is what got committed

### G14: Never Print Secrets
**When:** Any command or file that could carry a key, password, token or DSN
→ Never run a command whose output can contain a secret: no `docker compose config`, `env`, `cat .env`, printing `settings.*`
→ Check a secret by shape only: set/empty (`grep -c "^NAME=.\+" .env`), length, or `bool(settings.name)` in the container
→ A mask (`sed`, `grep -v`) is not a safeguard. If the value must be in the output for the check to work, use another check
→ Secrets live in headers and container env only — never in URLs, fixtures, commits or docs
→ The user pastes keys; never type, echo or read one. If one leaks into a transcript, say so and ask for rotation
→ Why: Part 2.1 masked `docker compose config` expecting quotes; compose prints unquoted, the key leaked, rotation needed

### G15: Production Changes Only On Explicit Go
**When:** Anything that changes production state and cannot be undone by `git revert` — schema migrations, data deletes, prod rebuilds/redeploys, secret rotation
→ Runs only on the user's explicit go in chat, given for that specific action
→ Never inside a part's verification — verify on the dev twin, mocks or fixtures instead
→ The completion report lists what is ready and waiting for a go, one line per action
→ A prod deploy is `docker compose build <service>` then `up -d <service>`, in that order. `up -d` alone recreates the container from the existing image and deploys nothing; the twins mount source live and do not show this
→ Why: Parts 1.6–2.1 each recreated the prod container and applied migrations without asking. Harmless while prod holds no data; not once real scans are in it. 4.2's waiting list said `up -d data-engine` alone, which would have redeployed the old image without the new route

---

## PART 2: PROJECT RULES — TradingFirm

### Scanner Architecture (Implemented)

Reference implementation: `scanner/pro_scan.py` (523 lines)

**Current 7-step pipeline:**
1. **Pre-screen**: Finviz screens ~7,000 stocks → ~650 candidates (price, volume, cap filters)
2. **Daily download**: `yf.download()` bulk daily (1y) for all candidates (batched 10/5s)
3. **Daily filters**: ATRP 2.5-6%, RVOL >1.0/1.2, 52w 10-90%, IPO age >120 days → ~50-60 pass
4. **Hourly download**: `yf.download()` hourly (3mo) for daily winners ONLY (~91% fewer calls)
5. **Hourly filters**: 4H price > 50 EMA, 1H EMA20 > EMA50 → ~30-40 pass
6. **Enrichment**: `yf.Ticker` for sector, float, news (2s delays) + market cap >$500M + float 20M-1B
7. **Sort**: By RVOL × ATRP, best first

### Data Sources

| Source | Library | Purpose | Status |
|--------|---------|---------|--------|
| Finviz | finvizfinance 1.3.0 | Stock discovery/screening | Dev/testing only |
| Yahoo Finance | yfinance 1.5.1 | OHLCV data, enrichment | Dev/testing only |
| Polygon.io / FMP | TBD | Production data provider | Upgrade path |

### yfinance Rules (v1.x)
- **Do NOT pass `session=`** to `yf.download()` or `yf.Ticker()` — yfinance 1.x manages its own sessions/cookies. Custom sessions fight internal rate-limit handling and CAUSE blocks.
- **Use `Ticker.fast_info`** for market cap (reliable). `Ticker.info` is unstable — wrap in try/except, use only for sector/industry/float.
- **Single-ticker `yf.download()`** returns flat columns (not MultiIndex). Handle both formats in `extract_ticker_df()`.
- **Batch size**: Max 20 tickers per `yf.download()` call, 3s delay between batches.
- **Always** use `threads=False` — parallel connections trigger rate limits.
- **Enrichment**: 2s delay between `yf.Ticker` calls. Skip failed tickers, never crash the scan.
- **Canary check**: First batch must succeed or abort the entire scan — don't burn quota on a bad IP.
- **Version guardrail**: Provider `__init__` checks `yf.__version__` major version. Crashes if mismatched.

### Finviz Rules (finvizfinance 1.3.0)
- **Ticker corruption bug**: Doubles first character of every ticker (AAL → AAAL). Must strip in code.
- **Random delays**: `random.uniform(1.0, 2.5)` between screener calls (9 calls per scan).
- **Cache results**: Never re-scrape if data is < 1 hour old.
- **One scan at a time**: Never run concurrent Finviz scans.

### Production Changes (G15 in this repo)
- **Needs a go**: `./scripts/migrate.sh` against prod (`tradingfirm`), and `docker compose up -d [--build] data-engine` or any other prod service recreate/rebuild. Also `.env` secret rotation and any `DELETE`/`TRUNCATE` on prod tables.
- **Needs no ask**: `scripts/dev-db.sh`, anything on the dev twin (`tf-data-engine-dev`, `tradingfirm_dev`, Redis DB 1), tests, fixtures.
- **Prod migrations applied before this rule existed**: 0.2 (`001_initial_schema.sql`), 1.1 (`002_bars.sql`), 2.1 (`003_context.sql`). Prod container recreated without asking by 1.6, 1.6 follow-up, 1.7, 2.1.

### Operations
- **Scan cooldown**: Minimum 10 minutes between full scans. API endpoint rejects if < 10 min since last scan.
- **Concurrency**: Only one scan at a time — check `tf:cache:scan_status` before starting. Return 409 if already running.
- **Error isolation**: If one ticker fails during enrichment, skip it — never crash the whole scan. Log ticker name + error.
- **Resource cleanup**: Close DB pools, Redis, HTTP sessions on shutdown. `del` large DataFrames + `gc.collect()` after processing.
- **No raw storage**: Never store DataFrames in app.state — only lightweight dicts/results.
