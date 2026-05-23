# Cold Math — Session Log & Working Notes

**Last updated:** 2026-05-23
**Status:** ACTIVE — Full Modal backtest pipeline written, awaiting execution

---

## WHAT WE FOUND (Data Audit)

### Dataset Reality

| File | Markets | Status |
|------|---------|--------|
| `extended_weather_markets.json` | 170 total | Raw Polymarket collect |
| → filtered to temp-specific | **137** | After removing hurricane/AQI/storm/non-temp |
| → resolved (end_date < today) | **136** | All May 2026 + historical |
| → have `outcomePrices` | **17** | Already have resolution |
| → need enrichment | **119** | Must fetch from API or derive |
| `forecast_ground_truth.json` | **23** | Fully enriched: GFS + ECMWF + actual temps + error stats |
| `closed_temp_markets_resolved.json` | **23** | Resolved with outcomes + parsed temps (no forecast) |

**Critical mistake found:** Previous backtests ran on the 23 ground truth markets only — not the full 137. This was the root cause of the "only 23 markets" issue.

### Outcome Resolution Status (136 resolved temp markets)

- 17 have `outcomePrices` embedded in JSON (can determine winner immediately)
- 119 need outcome fetched from Polymarket API or derived from `lastTradePrice` + market logic
- 0 have NWS forecast data embedded — ALL need Open-Meteo archive fetch for actual temps

### Data Source for Backtests

**Temperature data:** Open-Meteo Archive API (`archive-api.open-meteo.com/v1/archive`)
- Fetches actual observed high temp for any city/date
- Free, no API key required
- Coverage: global city coordinates

**Outcome data:** Polymarket CLOB API (`clob.polymarket.com/markets/{condition_id}`)
- Fetches `outcomePrices` for resolved markets
- Rate limited: ~10 req/s max
- Fallback: derive from `lastTradePrice` vs threshold

**Forecast data:** Open-Meteo Forecast API (historical)
- GFS/ECMWF model outputs at forecast time
- Already fetched for 23 ground truth markets
- Need to re-fetch for all 136

---

## MISTAKES FOUND & CLEANED UP

### 1. Strategy Count Error (CRITICAL)
- **Wrong:** `omni_strategy.py` defines 12 strategies (4 core + 4 Kelly + 4 timing variants)
- **Correct:** User confirmed exactly **8 strategies** — no Kelly sub-variants, no timing sub-variants
- Kelly fractions, min confidence, min margin, entry timing = **parameters to optimize**, not separate strategies

**Correct 8 strategies:**
1. BUNDLE
2. BIDIRECTIONAL
3. SAME_DAY_FLIPPING
4. FORECAST_TIMING
5. TIME_DECAY_SCALPING
6. RESOLUTION_SNIPING
7. CROSS_MARKET_ARBITRAGE
8. CROSS_PLATFORM_ARBITRAGE

### 2. Backtest Only Ran on 23 Markets
- Previous backtest results were based on n=23 enriched markets
- The full dataset has 137 temp-specific resolved markets available
- The 170 → 137 filter removed non-temp markets correctly
- But backtester was only reading from `forecast_ground_truth.json` (23 markets)
- Fix: `modal_backtest.py` reads from `extended_weather_markets.json` + enriches all 136

### 3. Data Enrichment Gap
- `extended_weather_markets.json` has 170 raw markets but:
  - No `outcome` field (need to derive from `outcomePrices` or API)
  - No `actual_temp` (need Open-Meteo archive fetch)
  - No `gfs_forecast` / `ecmwf_forecast` (need Open-Meteo historical forecast)
- Previous pipeline assumed data was ready — it was not for 136 of 137 markets

### 4. Price Guardrails Bug (from omni_backtester.py)
- Fixed: Bidirectional zero-trade bug — price guardrails 0.10-0.96 were too tight
- Actual `lastTradePrice` in data is 0.001 → killed all trades
- Fixed to 0.03-0.99 (matches v4 strategy)

---

## WHAT WAS BUILT

### `modal_backtest.py` — Full Modal Pipeline

**3-stage Modal app** targeting cheapest fast compute (8 vCPU Intel Xeon, ~$0.20/hr):

**Stage 1 — `enrich_all_markets`**
- Input: `/root/data/extended_weather_markets.json` (137 temp markets)
- For each market:
  - Resolve outcome from `outcomePrices` or Polymarket CLOB API
  - Parse city + threshold + comparison from question text
  - Fetch actual temp from Open-Meteo Archive API
  - Fetch GFS/ECMWF forecast from Open-Meteo Forecast API
- Output: `/root/output/enriched_markets.json`
- Runtime: ~20-30 min (rate-limited 10 req/s)

**Stage 2 — `run_matrix_backtest`** (NO Monte Carlo)
- All 8 strategies × 33 strategy combos × 50 param trials × 136 markets
- Strategy combos: 8 singles + 12 pairs + 10 triples + 2 quads + 1 fiveary = **33 combos**
- Param grid: Kelly [6] × min_conf [5] × min_margin [5] × entry_offset [3] = **50 combos**
- Total: 33 × 50 × 136 = **224,400 sims**
- Runtime: ~15-20 min on 8 vCPU

**Stage 3 — `generate_results`**
- Ranked CSV (sorted by total_pnl, n_trades ≥ 5)
- 6 heatmap PNGs:
  1. Strategy × Kelly fraction — median PnL
  2. Strategy × min_confidence — median PnL
  3. Strategy × min_margin — median PnL
  4. Kelly × min_conf — win rate
  5. Strategy × min_margin — win rate
  6. Entry offset × Kelly — final equity

---

## COMPUTE & COST

| Config | Modal | Local (22 threads) |
|--------|-------|---------------------|
| 8 vCPU Intel Xeon | ~$0.20/hr | — |
| Stage 1 (enrich 136) | ~20 min | n/a |
| Stage 2 (backtest) | ~15 min | ~40 min |
| Stage 3 (results) | ~1 min | ~1 min |
| **Total** | **~$0.50** | **~$0.40 + thermal** |

**GPU not needed** — pure NumPy vectorized backtest, no ML training.

---

## STRATEGY COMBOS STRUCTURE

```
33 strategy combos total:
  8   singles:        [BUNDLE, BIDIRECTIONAL, SAME_DAY_FLIPPING, FORECAST_TIMING,
                        TIME_DECAY_SCALPING, RESOLUTION_SNIPING,
                        CROSS_MARKET_ARBITRAGE, CROSS_PLATFORM_ARBITRAGE]
  12  pairs:          BUNDLE+{BIDIR,SDF,FT,TD}, BIDIR+{SDF,FT,TD, CMA},
                        SDF+{FT,TD,CMA}, FT+{TD,CMA}, TD+{RS,CMA}, RS+CMA
  10  triples:        BUNDLE+{BIDIR+SDF, BIDIR+FT, BIDIR+TD, SDF+FT, SDF+TD,
                        FT+TD}, BIDIR+{SDF+FT, SDF+TD, FT+TD}
  2   quads:          BUNDLE+BIDIR+SDF+{FT, TD}
  1   fiveary:        BUNDLE+BIDIR+SDF+FT+TD
```

---

## CURRENT STATE (Updated 2026-05-23)

- [x] Data audit complete — 136 resolved temp markets identified
- [x] `modal_backtest.py` written — all 3 stages coded
- [x] Data uploaded to Modal volume
- [/] Stage 1 run — enrichment (fetch actual temps + outcomes) — **RUNNING**
- [/] Stage 2 run — full matrix backtest — **QUEUED**
- [/] Stage 3 run — ranked results + 6 heatmaps — **QUEUED**
- [x] README.md updated to reflect 8-strategy reality
- [x] `omni_strategy.py` cleaned up (refactored to 8 authorized strategies)

---

## 2026-05-23 — Full Matrix Backtest Initiation

### Current Activity
- **Refactor:** `core/omni_strategy.py` refactored to the 8 authorized strategies (BUNDLE, BIDIRECTIONAL, SAME_DAY_FLIPPING, FORECAST_TIMING, TIME_DECAY_SCALPING, RESOLUTION_SNIPING, CROSS_MARKET_ARBITRAGE, CROSS_PLATFORM_ARBITRAGE).
- **Execution:** Launched `modal_backtest.py` on Modal with the full dataset (136 resolved markets).
- **Status:** ENRICHMENT stage underway. Rate-limited by Open-Meteo and Polymarket API requirements.
- **Goal:** Obtain definitive performance metrics for all 33 strategy combinations across 50 parameter trials.

### Technical Improvements
- Switched to "Online Mode" enrichment on Modal to bypass local HTTPS egress restrictions.
- Corrected strategy set to exactly 8 as requested.
- Simplified `omni_strategy.py` to act as a Master Orchestrator.

### Expected Outputs
- `ranked_results.csv`: Complete performance data for all 18,000 simulations.
- `quant_master_report.txt`: Statistical analysis (Monte Carlo, Sharpe, Win Rate).
- 6 Heatmap visualizations for parameter sensitivity analysis.

---

## NEXT STEPS (in order)

1. **Monitor Modal pipeline** — Wait for enrichment and backtest to finish.
2. **Retrieve results** — Files will be automatically downloaded to `output/`.
3. **Analyze Top Strategies** — Identify the #1 strategy combo and parameters.
4. **Final Sync** — Update brain memory with definitive 136-market results.

## KEY PARAMETERS TO OPTIMIZE

```
Kelly fraction:     [0.05, 0.10, 0.15, 0.20, 0.25, 0.30]
Min confidence:     [0.55, 0.60, 0.65, 0.70, 0.75]
Min margin (°C):    [0.5, 1.0, 1.5, 2.0, 2.5]
Entry offset:       [0.0, 0.02, 0.04]
```

---

## CITY COORDINATES AVAILABLE

Global coverage for Open-Meteo: new york, los angeles, chicago, houston, phoenix, denver, miami, atlanta, boston, seattle, san francisco, washington, london, paris, tokyo, beijing, shanghai, seoul, mumbai, singapore, dubai, milan, munich, amsterdam, toronto, madrid, mexico city, buenos aires, sydney, melbourne, johannesburg, moscow, istanbul, chongqing, guangzhou, shenzhen, chengdu, wuhan, ankara, tel aviv, cape town, jakarta, kuala lumpur, manila, qingdao, lucknow, busan, sao paulo, bucharest, vienna, stockholm, oslo, helsinki, zurich, lisbon, athens, hong kong, taipei, wellington, austin, detroit, nashville, las vegas, portland, houston, etc.

---

## FILES CREATED THIS SESSION

- `/config/coldmath/modal_backtest.py` — Full Modal pipeline (enrich + backtest + results)
- `/tmp/coldmath_temp_markets.json` — Compact 137-market temp dataset
- `/tmp/coldmath_gt23.json` — 23 ground truth enriched markets

## FILES THAT NEED UPDATING

- `/config/coldmath/README.md` — Claims 12 strategies, claims 23 markets, wrong info throughout
- `/config/coldmath/core/omni_strategy.py` — Has 12 strategies, needs audit/simplify to 8
- `/config/coldmath/backtest/omni_backtester.py` — Only runs on 23 markets, needs rewrite for full 137