---
name: debug-collector
description: Systematic debugging for Phase 1 data collection issues — WebSocket disconnects, missing markets, data gaps, DB write failures, API rate limiting.
argument-hint: "[symptom: ws-disconnect | missing-market | no-snapshots | db-error | rate-limit]"
---

# Debug Collector

Use when Phase 1 (`main.py`) is misbehaving: data gaps, silent failures, or unexpected stops.

## Step 1 — Locate the failure

```bash
# Find the last error in the log
grep -E "ERROR|WARNING|Exception|Traceback" logs/collector.log | tail -30

# Check if the process is running
pgrep -fl main.py
```

## Step 2 — Identify the category

| Symptom | Likely cause | Go to |
|---|---|---|
| `ConnectionClosed` or `ws_reconnect` entries | WS disconnect | § WebSocket |
| Snapshot count stalled in DB | DB write failure or market filter | § Database |
| Market missing from collection | Discovery filter too strict | § Market Discovery |
| `429` or `Too Many Requests` in logs | API rate limit | § Rate Limiting |

## § WebSocket

- Check reconnect loop in `collector/ws_client.py` — backoff goes 5→10→20→40→60s
- If stuck at max backoff, the WS endpoint may be down: verify `api.ws_url` in settings
- `data_gaps` table records every disconnect window — query it to confirm timing

```bash
psql -d <dbname> -c "SELECT * FROM data_gaps ORDER BY started_at DESC LIMIT 5;"
```

## § Database

- Pool exhaustion: default is 2–10 connections (`db/repository.py`). Check for unclosed connections in long-running queries.
- `ON CONFLICT DO NOTHING` means silent deduplication — not a bug if counts seem low after a restart
- Verify writes are landing:

```bash
psql -d <dbname> -c "SELECT MAX(ts) FROM price_snapshots;"
psql -d <dbname> -c "SELECT MAX(ts) FROM trades;"
```

## § Market Discovery

- Filters are in `config/settings.yaml` under `phase1`: `min_volume_24h`, `max_spread`, `min_depth`
- MarketDiscovery rescans every 3600s — a market that appeared recently won't be picked up until next rescan
- To force immediate rescan: restart `main.py`
- To check what markets are currently tracked:

```bash
psql -d <dbname> -c "SELECT token_id, sport, league, status FROM markets WHERE status = 'active';"
```

## § Rate Limiting

- `rest_client.py` enforces `request_delay_sec` (default 1.0s) between calls
- If hitting 429s, increase `api.request_delay_sec` in settings and restart
- Phase 0 is more aggressive than Phase 1 (bulk scanning) — prefer running it off-peak

## Step 3 — Verify fix

After any change to settings or code, restart and confirm within 10 minutes:
```bash
grep -c "snapshot saved" logs/collector.log  # should increment every 5 min
```
