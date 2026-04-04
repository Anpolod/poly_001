---
name: add-analytics
description: Structured process for adding new metrics, ratios, or market filters to the Polymarket sports collector. Covers the full change path: config → analytics → schema → repository.
argument-hint: "[type: ratio | filter | metric | verdict]"
---

# Add Analytics

Use when adding a new calculation, market filter, or verdict threshold.

**Touch order matters.** Always follow: Config → Analytics → Schema → Repository.

---

## 1. Config (`config/settings.yaml` + `settings.example.yaml`)

Add the new threshold or parameter under the relevant section (`phase0` or `phase1`).  
**Always update `settings.example.yaml` too** — it's the source of truth for new installs.

```yaml
# Example: adding a new filter
phase1:
  min_liquidity_score: 0.75  # new parameter
```

Config is loaded and passed as a dict throughout the app — access it as `config["phase1"]["min_liquidity_score"]`.

---

## 2. Analytics (`analytics/cost_analyzer.py`)

Add the calculation logic here. All math lives in this module.

- Input: raw API data (orderbook dict, fee rate float, price history list)
- Output: a flat dict of computed values per market
- Keep it pure — no DB calls, no API calls in this layer

```python
# Pattern: new ratio
def calc_liquidity_score(self, orderbook: dict) -> float:
    ...
    return score
```

---

## 3. Schema (`db/schema.sql`)

Add the new column to the relevant table (`cost_analysis` for Phase 0 metrics, `price_snapshots` for Phase 1 metrics).

```sql
ALTER TABLE cost_analysis ADD COLUMN liquidity_score NUMERIC(10,4);
```

Then apply it:
```bash
psql -d <dbname> -f db/schema.sql
# or for additive changes:
psql -d <dbname> -c "ALTER TABLE cost_analysis ADD COLUMN liquidity_score NUMERIC(10,4);"
```

---

## 4. Repository (`db/repository.py`)

Update the relevant `INSERT` or `UPSERT` statement to include the new column. The pattern is:

```python
await conn.execute(
    """INSERT INTO cost_analysis (..., liquidity_score)
       VALUES (..., $N)
       ON CONFLICT (market_id) DO UPDATE SET liquidity_score = EXCLUDED.liquidity_score""",
    ..., value
)
```

Parameter indices (`$1`, `$2`, ...) must be contiguous — recount them after adding a new field.

---

## 5. Adding a new market filter

Filters live in `collector/market_discovery.py`. The pattern is a predicate applied over the market list:

```python
# Add in the filter chain
if config["phase1"].get("min_liquidity_score", 0) > 0:
    markets = [m for m in markets if m["liquidity_score"] >= config["phase1"]["min_liquidity_score"]]
```

Use `.get()` with a default so existing configs without the new key don't break.

---

## Checklist

- [ ] `settings.example.yaml` updated alongside `settings.yaml`
- [ ] New calculation is in `analytics/cost_analyzer.py`, not inline in `main.py`
- [ ] DB column added and migration applied
- [ ] Repository INSERT params reindexed correctly
- [ ] Tested Phase 0 end-to-end: `python cost_analyzer.py` produces expected CSV output
