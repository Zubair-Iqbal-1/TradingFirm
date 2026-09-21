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

risk-shield's night checks (Part 3.4b) have **155 slots in a full week**. A week runs from Sunday evening to Friday: Sun 18:15–23:45 ET (12 slots), Mon–Thu (31 each), Fri 00:15–16:45 (19). A slot is **missed** if it has no `risk.health_checks` row, or if its row's `overlay.status` is `unavailable` (the check ran but had no futures quotes). Add one row per week. A week with 0 missed still gets a row.

| week | expected | missed | cause |
|---|---|---|---|
| 2026-09-14 → 09-18 | 125 (night checks went live Mon 09-14 16:24 ET, so the first slot was 16:45; a full week is 155) | 0 | — |
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
