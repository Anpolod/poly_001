# Phase 0 Analysis Report

> **Kill / Proceed Condition:** Proceed to Phase 1 if `ratio_24h > 1.5` on the clean GO subset. Recommend STOP if `clean_go_pct < 15%` of all markets with data.

---

## Verdict Distribution

| Verdict | Count | % |
|---------|-------|---|
| GO | 9 | 8.11% |
| MARGINAL | 5 | 4.5% |
| NO_GO | 52 | 46.85% |
| NO_DATA | 45 | 40.54% |
| **TOTAL** | **111** | 100% |

---

## Cost Structure (non-NO_DATA markets)

| Metric | Avg | Min | Max |
|--------|-----|-----|-----|
| taker_rt_cost | 5.680795 | 2.6557 | 17.3846 |
| spread_pct | 3.680795 | 0.6557 | 15.3846 |
| maker_rt_cost | 5.333692 | 0.796 | 22.8894 |

---

## Ratio Stats (ratio_24h)

| Subset | Mean | Median | % > 2.0 | % > 1.5 |
|--------|------|--------|---------|---------|
| All data markets | 1.889091 | 0.0 | 13.64% | 21.21% |
| Clean GO only | 2.93 | 2.86 | 100.0% | 100.0% |

---

## GO Flag Summary

Total GO markets: **9**  
Clean GO (zero flags): **4** (44.44% of GO, 6.06% of data markets)

| Flag | Count | % of GO |
|------|-------|---------|
| LOW_VOL | 4 | 44.44% |
| EXTREME_ODDS | 1 | 11.11% |
| THIN_DEPTH | 0 | 0.0% |
| MOVE_EXCEEDS_HALF_PRICE | 3 | 33.33% |
| RATIO_OUTLIER | 1 | 11.11% |

---

## Segments

### By Sport

| Sport | Markets | GO | Clean GO | Ratio Median | % >1.5 |
|-------|---------|----|---------:|-----------:|--------|
| baseball | 18 | 0 | 0 | 0.33 | 33.33% |
| basketball | 31 | 3 | 2 | 1.38 | 37.5% |
| football | 51 | 5 | 2 | 0.0 | 18.0% |
| hockey | 4 | 0 | 0 | 0.0 | 0.0% |
| tennis | 7 | 1 | 0 | 11.62 | 100.0% |

### By Volume Tier

| Vol Tier | Markets | GO | Clean GO | Ratio Median | % >1.5 |
|----------|---------|----|---------:|------------:|--------|
| <10K | 44 | 4 | 0 | 0.0 | 19.05% |
| 10K-50K | 47 | 3 | 3 | 0.165 | 23.33% |
| >50K | 20 | 2 | 1 | 0.27 | 20.0% |

### By Price Regime

| Price Regime | Markets | GO | Clean GO | Ratio Median | % >1.5 |
|-------------|---------|----|---------:|------------:|--------|
| mid-range | 97 | 8 | 4 | 0.0 | 21.43% |
| extreme | 14 | 1 | 0 | 1.27 | 20.0% |

---

## Potential Artifacts (ratio_24h > 20) — 1 market(s)

> These markets have unusually high ratio values and may reflect data errors.
> They are NOT automatically discarded — investigate before excluding.

| market_id | slug | sport | verdict | ratio_24h | volume_24h | mid_price |
|-----------|------|-------|---------|-----------|------------|-----------|
| 1520678 | elc-sot-ips-2026-04-03-ips | football | GO | 68.52 | 5815.005145000001 | 0.1525 |

---

## Recommendation

**STOP**

**Reason:** clean_go_pct (6.1%) < 15% threshold

*Proceed if ratio_24h > 1.5 on clean GO subset; Stop if clean_go_pct < 15% of all markets with data.*
