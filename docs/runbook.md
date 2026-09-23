# Runbook — keeping the MacBook always-on

Per [`docs/plan-analyst-watcher.md`](plan-analyst-watcher.md) D14, the MacBook
stays plugged in and awake for the first 1–2 months of watcher operation
(before the VPS move in Phase 9). This doc covers how to keep it up and what
to check when it drops.

## 1. macOS energy settings

System Settings → **Energy Saver** (Battery/Power Adapter pane on Apple
Silicon):

- **Prevent automatic sleeping when the display is off** — enable while on
  power adapter. This is the setting that matters most: without it, macOS
  suspends background processes (including Docker's VM) once the display
  sleeps.
- **Wake for network access** — enable, so the machine responds to LAN/remote
  checks even if it does doze.
- Display sleep timing itself doesn't matter once the above is set — the
  screen can turn off, the machine won't.
- Under **General → Login Items**, no action needed here; Docker Desktop's
  own login-item setting (below) covers startup.

## 2. `caffeinate` as a belt-and-braces layer

Energy Saver settings can get reset by a macOS update or a careless click.
`caffeinate` is a second, explicit guarantee that doesn't depend on those
settings:

```bash
caffeinate -dims &
```

- `-d` — prevent display sleep
- `-i` — prevent idle sleep
- `-m` — prevent disk sleep
- `-s` — prevent sleep while on AC power (not while on battery — that's
  intentional, so a real power loss still lets the machine sleep instead of
  draining the battery to zero)

Run it in a dedicated terminal tab (or as a login item via a small
`launchd` plist later, if this proves annoying to restart by hand after every
reboot). Check it's still running with:

```bash
pgrep -fl caffeinate
```

## 3. Docker Desktop auto-start

Docker Desktop → Settings → **General** → enable **Start Docker Desktop when
you log in**. Also enable **Restore last used containers on start**, if not
already on — this doesn't restore the exact same containers, but it makes
Docker come back into a launchable state after a reboot without babysitting
`docker compose up -d` by hand every time.

The compose stack itself does not auto-restart at boot unless Docker Desktop
finishes starting and someone (or a `launchd` job, later) runs
`docker compose up -d`. Until that's automated, the actual bring-up after any
reboot is:

```bash
docker compose up -d
curl http://localhost:8001/health
```

## 4. What to check after a Wi-Fi drop

A dropped connection doesn't stop the containers running (they're on the
Mac's own Docker network, not routed through Wi-Fi), but it does interrupt
anything talking to the outside world — yfinance/Finviz calls, and later
Alpaca's websocket stream, Telegram, Finnhub, FRED, EDGAR. After
reconnecting, check in this order:

1. **Containers still up and healthy**:
   ```bash
   docker compose ps
   ```
   All 7 should show `(healthy)` (per the healthchecks added in Part 0.3). If
   any show `unhealthy` or are missing, that service's process likely died
   mid-request when the network dropped — check its logs:
   ```bash
   docker compose logs --tail=100 <service>
   ```

2. **Data engine reachable and DB-connected**:
   ```bash
   curl http://localhost:8001/health
   ```
   Look for `db_connected: true`.

3. **In-flight scan, if any**: a scan running when Wi-Fi dropped may have
   failed a yfinance/Finviz batch mid-run. Check:
   ```bash
   curl http://localhost:8001/scan/status
   ```
   If it's stuck (not `idle`/`complete`/`error` — genuinely wedged), it's
   safe to wait out the 10-minute cooldown and re-run rather than restart the
   container, since a restart loses the in-memory result store described in
   `docs/overview.md`.

4. **Watcher heartbeat** (once `signal-engine`'s watcher loop exists, Phase
   6): check `signals.watch_log` for a `feed_down`/`feed_up` pair bracketing
   the drop, and confirm a `heartbeat` row has been written since
   reconnecting. A gap with no matching `feed_up` means the Alpaca stream
   didn't reconnect on its own and the service needs a restart.

5. **Telegram notifier** (once `services/notifier` exists, Phase 5): send
   `/status` to the bot and confirm it replies — a stale reply or no reply at
   all means its Redis subscription needs a restart.

Until Phase 6/9's self-monitoring exists (`docs/plan-analyst-watcher.md`
Part 9.3 — Telegram alert on a missed heartbeat), this check is manual. Log
every Wi-Fi drop you notice (rough time + how long) in a note somewhere so
Part 9.3's alerting threshold can be tuned against real gaps instead of a
guess.

## Missed slots

risk-shield's night checks (Part 3.4b) run on a :15 / :45 ET grid while CME futures trade (`scheduler.night_slots_for_day`). Three windows are cut: the 17:00–18:00 halt, the Friday 17:00 close, and every slot after 08:45 up to and including the 16:20 settle on an XNYS day (open − 45 min, strictly after, through settle). That gives:

| day | pre-open | after settle | evening | slots |
|---|---|---|---|---|
| Sun | — | — | 18:15–23:45 (12) | **12** |
| Mon–Thu | 00:15–08:45 (18) | 16:45 (1) | 18:15–23:45 (12) | **31** each |
| Fri | 00:15–08:45 (18) | 16:45 (1) | — | **19** |

A full week is 12 + 4 × 31 + 19 = **155**. 09:15 and 16:15 are never slots. A slot is **missed** if it has no `risk.health_checks` row, or if its row's `overlay.status` is `unavailable` (the check ran but had no futures quotes). Add one row per week. A week with 0 missed still gets a row.

| week | expected | missed | cause |
|---|---|---|---|
| 2026-09-14 → 09-18 | 125: 155 minus the 30 slots before night checks went live on Mon 09-14 at 16:24 ET (Sun 12 + Mon pre-open 18). The first slot was Mon 16:45 | 0 | — |
| 2026-09-20 → 09-25 (open) | 155 | 1 | Mon 09-21 04:15 ET: yfinance returned nothing for `ES=F` and `NQ=F` and the Finnhub news poll timed out in the same seconds. Host network blip, no action (`docs/decisions.md` 2026-09-21). |

To count a week, run this read-only query on prod (replace the two dates):

```sql
SELECT to_char(checked_at AT TIME ZONE 'America/New_York', 'Dy YYYY-MM-DD') AS et_date,
       count(*) AS rows,
       count(*) FILTER (WHERE indicators->'overlay'->>'status' = 'unavailable') AS unavailable
FROM risk.health_checks
WHERE indicators->>'kind' = 'night'
  AND checked_at >= '2026-09-20 00:00 America/New_York'
  AND checked_at <  '2026-09-26 00:00 America/New_York'
GROUP BY 1 ORDER BY min(checked_at);
```

A full day has Sun 12, Mon–Thu 31 and Fri 19 rows. A day short of that is missing slots; list them before recording a cause.

### Market / settle slots

Market checks run every 5 min from the XNYS open to the close, plus the 16:20 ET settle. There is no catch-up: a missed settle breaks the next day's trend, which is why prod risk-shield rebuilds go outside XNYS hours (the G15 timing rule in `CLAUDE.md`). Record every missed market or settle slot here.

| date | slot (ET) | kind | cause |
|---|---|---|---|
| Mon 2026-09-14 | 16:20 | settle | risk-shield deploy restart, pre-G15. The container started at 16:24:28 ET and the scheduler logged `Missed 1 health check slot(s) up to 2026-09-14T20:20:00+00:00 (woke 268s after that slot)`. |

## Analyst settings (Part 4.4)

Migration `007_ai.sql` seeds the development user with `account_size = NULL`: this repository is public, so the number never goes into a migration, a commit or a chat. Until it is set, `POST /analyze/{ticker}` answers `409 settings missing`. Set it once, by hand, after 007 is applied (replace `<ACCOUNT_SIZE>`; `risk_per_trade_pct` is a percent, 1.0 = 1 %):

```bash
docker exec -it tf-postgres bash -c 'PGUSER="$POSTGRES_USER" PGPASSWORD="$POSTGRES_PASSWORD" psql -d "$POSTGRES_DB" -c "UPDATE users.settings SET account_size = <ACCOUNT_SIZE>, risk_per_trade_pct = 1.0, updated_at = now() WHERE user_id = '"'"'00000000-0000-4000-8000-000000000001'"'"';"'
```

A change takes effect on the next analyze: account size and risk % are part of the verdict cache's fingerprint, so a cached verdict sized for the old numbers is not served. The account size is never sent to the model and never logged; the prompt carries the plan's levels and R only.

### A verdict that was paid for but not stored

If Postgres fails after the model answered, `/analyze` still returns the verdict with `stored: false`, and `tf-ai-agent` logs one ERROR line starting `VERDICT NOT STORED`, followed by a JSON payload `{"verdict": {...}, "llmCall": {...}}`. Its keys are the columns of `ai.verdicts` and `ai.llm_calls`. To backfill (D5: every verdict is stored): insert the `verdict` object into `ai.verdicts`, take the returned `id`, and insert `llmCall` into `ai.llm_calls` with that `verdict_id`. `GET /usage` shows `ledgerMissedToday > 0` on a day this happened.

## Plan math version (Part 4.8a)

`ai.verdicts.plan_math_version` names the `grading/plan_math.py` rules a row was built with. **NULL means 1**: every row before 4.8a (migration 009 adds the column and backfills nothing; `db.journal_rows` reads `COALESCE(plan_math_version, 1)`). `GET /journal/stats` never averages two versions. Bump `PLAN_MATH_VERSION` on any rule change.

To rerun plan math over stored verdicts without an LLM call (`--prev` = an earlier module from git, labelled by its own version: v1 `0954e43`, v2 `0675990`; the account is a placeholder and no size is printed):

```bash
git show 0675990:services/ai-agent/grading/plan_math.py > /tmp/plan_math_v2.py && docker cp /tmp/plan_math_v2.py tf-ai-agent-dev:/tmp/plan_math_v2.py
```

```bash
docker exec -i tf-postgres bash -c 'PGUSER="$POSTGRES_USER" PGPASSWORD="$POSTGRES_PASSWORD" PGDATABASE="$POSTGRES_DB" exec psql -qAt -v ON_ERROR_STOP=1 -c "SET default_transaction_read_only = on; SELECT json_agg(json_build_object('"'"'verdictId'"'"', id, '"'"'ticker'"'"', ticker, '"'"'entry'"'"', entry, '"'"'indicators'"'"', dossier->'"'"'sections'"'"'->'"'"'indicators'"'"') ORDER BY asked_at) FROM ai.verdicts WHERE asked_at >= now() - interval '"'"'7 days'"'"';"' > /tmp/rows.json
```

```bash
docker exec -i tf-ai-agent-dev python -m scripts.plan_math_rerun --prev /tmp/plan_math_v2.py < /tmp/rows.json
```

Since 4.8a-de the rerun can also take **fresh inputs** recomputed from the stored bars (full-history zones with `touches` / `held` / `broke` / `lastTouch`, and `lastSwingLow`): export each verdict's ticker, `asOf` (`dossier->>'asOf'`), stored close and the ticker's daily bars read-only, run `docker exec -i tf-data-engine-dev python -m scripts.snapshot_from_bars < bars_rows.json > fresh.json` (bars after `asOf` are dropped, a `closeMismatch` is flagged), and put each result's `indicators` on the row as `fresh`. The table then shows three rows per verdict: the previous module on the stored inputs, the previous module on the fresh inputs with the swing low withheld (zone drift alone), the current module on the fresh inputs; plus the nearest stored vs fresh zone per ticker.
