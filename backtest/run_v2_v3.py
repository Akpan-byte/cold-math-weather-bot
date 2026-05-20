#!/usr/bin/env python3
"""
Cold Math — Run V2 + V3 backtests and log both to the backtest DB.
"""
import json
import sys
sys.path.insert(0, "/config/coldmath")

from backtest.v3_engine import (
    init_backtest_db, run_v2_backtest, run_v3_backtest, run_sensitivity,
    save_backtest_run, save_market_details, list_backtest_runs,
)
from scanner.market_fetcher import fetch_weather_markets

init_backtest_db()

# Fetch current active weather markets
print("Fetching active weather markets...")
markets = fetch_weather_markets()
print(f"Found {len(markets)} weather markets")

# Also load the existing historical market data
try:
    with open("/config/coldmath/data/enhanced_markets.json") as f:
        enhanced = json.load(f)
    print(f"Loaded {len(enhanced)} enhanced markets from file")
    markets.extend(enhanced)
except:
    pass

# Dedup
seen = set()
unique = []
for m in markets:
    q = m.get("question", "")
    if q and q not in seen:
        seen.add(q)
        unique.append(m)
markets = unique
print(f"Total unique markets: {len(markets)}")

# ─── Run V2 (Modeled NWS Confidence) ───
print("\n" + "="*60)
print("RUNNING V2 BACKTEST (Modeled NWS Confidence)")
print("="*60)
v2_result = run_v2_backtest(markets, confidence_threshold=0.99)
v2_sensitivity = run_sensitivity(markets, engine="v2")

print(f"\nV2 Results:")
print(f"  Qualifying: {v2_result['qualifying_markets']}/{v2_result['total_markets']}")
print(f"  Win rate: {v2_result['win_rate']:.1%}")
print(f"  P&L: ${v2_result['total_pnl']:.2f}")
print(f"  Return: {v2_result['return_pct']:.1%}")
print(f"  Final bankroll: ${v2_result['final_bankroll']:.2f}")
print(f"  Max drawdown: {v2_result['max_drawdown']:.2%}")
print(f"  Sharpe: {v2_result['sharpe_ratio']:.2f}")

v2_result["sensitivity"] = v2_sensitivity
v2_run_id = save_backtest_run(
    result=v2_result,
    version="2.0",
    label="Modeled NWS Confidence (Gaussian CDF)",
    engine="v2_gaussian_cdf",
    data_source="polymarket_gamma+enhanced",
    notes="Baseline: modeled NWS confidence from Gaussian CDF of margin/sigma. No real forecast data."
)

# ─── Run V3 (Empirical Forecast Confidence via Open-Meteo) ───
print("\n" + "="*60)
print("RUNNING V3 BACKTEST (Empirical Forecast Confidence)")
print("="*60)
v3_result = run_v3_backtest(markets, confidence_threshold=0.99)
v3_sensitivity = run_sensitivity(markets, engine="v3")

print(f"\nV3 Results:")
print(f"  Qualifying: {v3_result['qualifying_markets']}/{v3_result['total_markets']}")
print(f"  Win rate: {v3_result['win_rate']:.1%}")
print(f"  P&L: ${v3_result['total_pnl']:.2f}")
print(f"  Return: {v3_result['return_pct']:.1%}")
print(f"  Final bankroll: ${v3_result['final_bankroll']:.2f}")
print(f"  Max drawdown: {v3_result['max_drawdown']:.2%}")
print(f"  Sharpe: {v3_result['sharpe_ratio']:.2f}")

v3_result["sensitivity"] = v3_sensitivity
v3_run_id = save_backtest_run(
    result=v3_result,
    version="3.0",
    label="Empirical NWS Confidence (Open-Meteo Forecast Errors)",
    engine="v3_openmeteo_empirical",
    data_source="polymarket_gamma+open-meteo_historical",
    notes="Improved: uses actual Open-Meteo forecast-vs-observation error distribution for confidence instead of Gaussian CDF."
)

# Save market details for V3
if v3_result.get("trades"):
    save_market_details(v3_run_id, v3_result["trades"])

# ─── Summary ───
print("\n" + "="*60)
print("BACKTEST COMPARISON")
print("="*60)
print(f"{'Metric':<25} {'V2 (Modeled)':>15} {'V3 (Empirical)':>15}")
print("-"*60)
for key in ["qualifying_markets", "winning_trades", "losing_trades",
            "win_rate", "total_pnl", "return_pct", "final_bankroll",
            "max_drawdown", "sharpe_ratio", "false_positives"]:
    v2_val = v2_result.get(key, 0)
    v3_val = v3_result.get(key, 0)
    if isinstance(v2_val, float):
        print(f"{key:<25} {v2_val:>15.4f} {v3_val:>15.4f}")
    else:
        print(f"{key:<25} {v2_val:>15} {v3_val:>15}")

# Show all logged runs
print("\n\nAll logged backtest runs:")
runs = list_backtest_runs()
for r in runs:
    print(f"  [#{r['id']}] v{r['version']} — {r['label'][:50]} | {r['timestamp'][:19]} | P&L: ${r.get('total_pnl',0):.2f} | WR: {r.get('win_rate',0):.1%}")

# Export for dashboard
export = {
    "v2": {**v2_result, "run_id": v2_run_id},
    "v3": {**v3_result, "run_id": v3_run_id},
    "all_runs": runs,
}
with open("/config/coldmath/data/backtest_v2_v3_comparison.json", "w") as f:
    json.dump(export, f, indent=2, default=str)
print(f"\nExported comparison to /config/coldmath/data/backtest_v2_v3_comparison.json")
