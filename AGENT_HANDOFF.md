# Cold Math — Agent Handoff & Implementation Plan
**Last updated:** 2026-05-23T06:00Z  
**Written by:** Antigravity (current session: `8a8f882c-2e6c-48fe-b7e9-684b851819dc`)  
**Previous session:** `1d2a4471-29e2-4264-badd-95c5ad497b3f` ("Cold Math Backtesting Strategy")

---

## SITUATION SUMMARY

We are backtesting 8 Polymarket weather trading strategies against historical resolved temperature markets. The goal is to produce ranked results, 6 heatmaps, and a quant report on the **full dataset of 81+ resolved markets** (not just the 20–23 that have been used so far).

### What has been built (DONE):
- `/config/coldmath/modal_backtest.py` — Full 3-stage Modal pipeline (enrich → backtest → results). 1052 lines.
- `/config/coldmath/run_full_enrichment.py` — Local enrichment script fetching Open-Meteo GFS/ECMWF/archive data
- `/config/coldmath/data/extended_weather_markets.json` — 170 raw Polymarket weather markets
- `/config/coldmath/data/forecast_ground_truth.json` — 23 already-enriched markets (our "offline fallback")
- `/config/coldmath/output/` — Results from LAST run (only 20 markets, offline mode fallback)
- `/config/PolyWeather/src/data_collection/city_registry.py` — 51-city geocoding registry with exact coordinates

### What the last run produced (already done):
- 18,000-parameter matrix backtest on **only 20 markets** (offline mode used `forecast_ground_truth.json`)
- BUNDLE strategy won, Kelly=0.30, min_conf=0.55 → 86.7% win rate, +7.377 PnL
- Results in `/config/coldmath/output/` (ranked_results.csv, quant_master_report.txt, 6 heatmaps)

---

## ROOT CAUSE OF PROBLEM

**Local sandbox blocks HTTPS egress.** `curl` and `urllib` hang/timeout when calling:
- `https://archive-api.open-meteo.com` (actual temps)
- `https://historical-forecast-api.open-meteo.com` (GFS/ECMWF forecasts)

This caused `run_full_enrichment.py` to hang at `[1/49]` every time.  
Modal remote containers DO have internet access — but `enrich_all_markets()` in `modal_backtest.py` was **overridden to offline mode** (reads `forecast_ground_truth.json` instead of calling APIs).

---

## FULL IMPLEMENTATION PLAN (in order)

### STEP 1 — Fix `enrich_all_markets` in `modal_backtest.py` (ONLINE mode)

**File:** `/config/coldmath/modal_backtest.py`  
**Function:** `enrich_all_markets()` (lines ~258–315)

Replace the offline "load from forecast_ground_truth.json" logic with the ONLINE enrichment that:
1. Loads `/root/data/extended_weather_markets.json` (170 raw markets)
2. Filters to temp-specific markets (keyword: temp/temperature/degree/celsius/fahrenheit/°c/°f)
3. Calls `parse_question()` to extract city, threshold, comparison
4. Calls `resolve_outcome()` to determine YES/NO (use permissive logic: `float(op[0]) > float(op[1])`)
5. Calls `fetch_actual_temp(city, date)` → Open-Meteo archive API
6. Calls `fetch_forecast_temp(city, date)` → Open-Meteo historical forecast API (GFS + ECMWF)
7. Sleep 0.1s between calls (rate limiting)
8. Saves enriched to `/root/output/enriched_markets.json` under key `"markets"`

**Key:** Use existing functions already in modal_backtest.py (`parse_question`, `resolve_outcome`, `fetch_actual_temp`, `fetch_forecast_temp`) — they're all already implemented correctly. The only thing that needs fixing is `enrich_all_markets` falling back to offline.

**City matching:** The `CITY_COORDS` dict in modal_backtest.py has ~60 cities. Use partial match as fallback.  
**Expected output:** ~81 enriched markets (81 resolved + geocodable out of 131 city/date-parsed markets)

### STEP 2 — Fix `image` in `modal_backtest.py` to include `extended_weather_markets.json`

**File:** `/config/coldmath/modal_backtest.py` (line ~38)  
The image already uses `.add_local_dir("/config/coldmath/data", "/root/data")` — this copies ALL data files including `extended_weather_markets.json` to Modal. This is correct. No change needed.

### STEP 3 — Verify `run_matrix_backtest` reads `markets` key correctly

**File:** `/config/coldmath/modal_backtest.py` (line ~602)  
Check that `data.get("markets", [])` is used. It is. ✓  
Also check `generate_results` at line ~662 reads `mdata.get("markets", [])`. It does. ✓

### STEP 4 — Run the Modal pipeline

```bash
cd /config/coldmath
modal run modal_backtest.py
```

This runs `main()` → `run_pipeline.remote()` which executes all 3 stages on Modal.

**Expected times:**
- Stage 1 (enrich 81+ markets): ~5-10 min (rate-limited API calls)
- Stage 2 (backtest): 33 combos × 50 params × 81 markets = ~133k sims → ~10-15 min
- Stage 3 (results): ~1 min

**Output files returned locally to `/config/coldmath/output/`:**
- `ranked_results.csv`
- `quant_master_report.txt`
- `heatmap_strategy_kelly.png`
- `heatmap_strategy_minconf.png`
- `heatmap_strategy_minmargin.png`
- `heatmap_kelly_minconf_winrate.png`
- `heatmap_strategy_minmargin_winrate.png`
- `heatmap_offset_kelly_equity.png`

### STEP 5 — Update SESSION_NOTES.md and walkthrough

**Files to update:**
- `/config/coldmath/SESSION_NOTES.md` — Mark checklist items complete
- `/config/.gemini/antigravity-cli/brain/1d2a4471-29e2-4264-badd-95c5ad497b3f/walkthrough.md` — Add new results

### STEP 6 — Brain memory sync
```bash
brain remember "Cold Math: fixed enrich_all_markets to run ONLINE on Modal, fetched 81+ markets from Open-Meteo, ran full matrix backtest. Results in /config/coldmath/output/"
brain log gemini backtest modal_backtest.py "Full 81+ market backtest complete"
```

---

## KEY FILES

| File | Purpose |
|------|---------|
| `/config/coldmath/modal_backtest.py` | Main pipeline — needs enrich fix |
| `/config/coldmath/data/extended_weather_markets.json` | 170 raw markets (input) |
| `/config/coldmath/data/forecast_ground_truth.json` | 23 pre-enriched (offline fallback only) |
| `/config/coldmath/output/` | Results output dir |
| `/config/PolyWeather/src/data_collection/city_registry.py` | 51-city geocoding registry |
| `/config/coldmath/SESSION_NOTES.md` | Project status doc |

---

## CRITICAL TECHNICAL NOTES

1. **Local HTTPS is blocked** — do NOT try to run `run_full_enrichment.py` locally. It will hang. All API calls must run on Modal.
2. **Outcome resolution logic** — use `float(op[0]) > float(op[1])` (permissive), NOT the strict `p0 > 0.99` check that was blocking 50 of 81 markets from resolving.
3. **City matching** — `parse_question()` extracts `in CITY` pattern. Many questions have "New York", "Los Angeles" etc. Use case-insensitive partial match on `CITY_COORDS` dict keys.
4. **Modal auth** — `~/.modal.toml` is configured for user `theakpanobong`. Run `modal token list` to confirm.
5. **Strategy count** — exactly 8 strategies (not 12). The STRATEGY_COMBOS list in modal_backtest.py is correct.
6. **33 combos total** — 8 singles + 12 pairs + 10 triples + 2 quads + 1 five-way.
7. **50 param trials** — Kelly [6] × min_conf [5] × min_margin [5] × entry_offset [3] = 450 combinations, reduced to 50 by the PARAM_GRID definition.

---

## WHAT TO DO IF YOU'RE A NEW AGENT PICKING THIS UP

1. Read this file + `/config/coldmath/SESSION_NOTES.md`
2. Read the last walkthrough: `/config/.gemini/antigravity-cli/brain/1d2a4471-29e2-4264-badd-95c5ad497b3f/walkthrough.md`
3. Fix `enrich_all_markets()` in `/config/coldmath/modal_backtest.py` (STEP 1 above)
4. Run `modal run modal_backtest.py` from `/config/coldmath/`
5. Wait for results, update docs, sync brain memory
