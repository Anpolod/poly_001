# Streamlit Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local Streamlit dashboard with four pages — Overview, Markets Browser, NBA Tanking Scanner, System Health — reading from the existing PostgreSQL/TimescaleDB DB synchronously.

**Architecture:** Isolated `dashboard/` package using `psycopg2` (sync, not asyncpg) via a cached ThreadedConnectionPool. Plotly for charts. `st.navigation` API (Streamlit 1.30+) for multi-page routing.

**Tech Stack:** Streamlit ≥1.30, psycopg2-binary, plotly, pandas (already pinned), PyYAML (already pinned).

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `requirements.txt` | Modify | Add streamlit, psycopg2-binary, plotly |
| `.streamlit/config.toml` | Create | Dark theme, wide layout |
| `dashboard/__init__.py` | Create | Empty package marker |
| `dashboard/db.py` | Create | psycopg2 pool, query_df(), query_one() |
| `dashboard/charts.py` | Create | Plotly chart builders (price history, spreads, counts) |
| `dashboard/pages/overview.py` | Create | Market counts, top movers, snapshot stats |
| `dashboard/pages/markets.py` | Create | Filterable market table + price history drilldown |
| `dashboard/pages/tanking.py` | Create | NBA tanking signals from DB + live re-scan button |
| `dashboard/pages/health.py` | Create | Data gaps, snapshot counts, collector heartbeat |
| `dashboard/main_dashboard.py` | Create | st.navigation entry point |

---

### Task 1: requirements.txt + .streamlit/config.toml + package init

**Files:**
- Modify: `requirements.txt`
- Create: `.streamlit/config.toml`
- Create: `dashboard/__init__.py`

- [ ] **Step 1: Add dashboard deps to requirements.txt**

Append these lines:
```
streamlit>=1.30.0
psycopg2-binary>=2.9
plotly>=5.20
```

- [ ] **Step 2: Create .streamlit/config.toml**

```toml
[theme]
base = "dark"
primaryColor = "#4CAF50"
backgroundColor = "#0E1117"
secondaryBackgroundColor = "#1E2330"
textColor = "#FAFAFA"

[server]
headless = true
port = 8501

[browser]
gatherUsageStats = false
```

- [ ] **Step 3: Create dashboard/__init__.py**

Empty file — package marker only.

- [ ] **Step 4: Install new deps**
```bash
venv/bin/pip install streamlit>=1.30.0 psycopg2-binary>=2.9 plotly>=5.20
```

---

### Task 2: dashboard/db.py

**Files:**
- Create: `dashboard/db.py`

- [ ] **Step 1: Write db.py**

Synchronous psycopg2 connection pool, reads credentials from `config/settings.yaml`.
Key functions: `_pool()`, `query_df(sql, params)`, `query_one(sql, params)`.

- [ ] **Step 2: Verify connection**
```bash
cd /path/to/project && venv/bin/python -c "from dashboard.db import query_one; print(query_one('SELECT count(*) FROM markets'))"
```

---

### Task 3: dashboard/charts.py

**Files:**
- Create: `dashboard/charts.py`

Plotly chart builders:
- `price_history_chart(rows)` — line chart of mid_price over ts
- `spread_dist_chart(rows)` — histogram of spread_pct
- `snapshot_count_bar(rows)` — bar chart of per-market snapshot counts

---

### Task 4: dashboard/pages/overview.py

Overview of the full market universe: counts by sport/verdict, top movers (largest 24h price change), and a snapshot rate indicator.

---

### Task 5: dashboard/pages/markets.py

Filterable table by sport, league, verdict. Click a market row to see its price_snapshots history chart.

---

### Task 6: dashboard/pages/tanking.py

Display latest tanking_signals (last 48h) from DB. "Re-scan now" button that calls `analytics.tanking_scanner.run()` asynchronously.

---

### Task 7: dashboard/pages/health.py

System health: last snapshot timestamp, snapshot counts per market (24h), recent data_gaps list, collector running indicator.

---

### Task 8: dashboard/main_dashboard.py

Entry point with `st.navigation` wiring all four pages.

---

## Verification
```bash
# Run dashboard
streamlit run dashboard/main_dashboard.py

# Check all pages load without error
# Overview: shows market counts
# Markets: filterable table renders
# NBA Tanking: table from tanking_signals
# Health: snapshot count, data_gaps
```
