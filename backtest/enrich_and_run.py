#!/usr/bin/env python3
"""
Cold Math — Build enriched historical dataset from closed Polymarket markets
+ Open-Meteo actual observations. Then run V2+V3 backtests.
"""
import json
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/config/coldmath")
from backtest.v3_engine import (
    init_backtest_db, run_v2_backtest, run_v3_backtest, run_sensitivity,
    save_backtest_run, save_market_details, list_backtest_runs,
    parse_market, geocode_city, fetch_forecast_vs_actual, _gaussian_confidence,
)

DATA_DIR = Path("/config/coldmath/data")


def _get(url: str, timeout: int = 30) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ColdMath/3.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"  GET failed: {e}")
        return None


def parse_date_from_question(question: str) -> str | None:
    """Extract date from question like '...on May 21?' or '...on February 25?'"""
    # "on Month Day?" (with optional year)
    m = re.search(r"on (\w+ \d+)(?:,?\s*(\d{4}))?\??", question)
    if m:
        date_str = m.group(1).strip()
        year_str = m.group(2)
        if year_str:
            try:
                dt = datetime.strptime(f"{date_str} {year_str}", "%B %d %Y")
                return dt.strftime("%Y-%m-%d")
            except:
                pass
        # No year — try current and recent years
        for year in [2026, 2025, 2024]:
            try:
                dt = datetime.strptime(f"{date_str} {year}", "%B %d %Y")
                return dt.strftime("%Y-%m-%d")
            except:
                continue
    # "in Month Year"
    m = re.search(r"(?:in|on) (\w+ \d{4})", question)
    if m:
        try:
            dt = datetime.strptime(m.group(1), "%B %Y")
            return dt.strftime("%Y-%m-%d")
        except:
            pass
    return None


def enrich_markets(markets: list[dict]) -> list[dict]:
    """Add actual temperatures and forecast data to closed markets."""
    enriched = []
    city_cache = {}

    for m in markets:
        question = m.get("question", "")
        parsed = parse_market(question)

        # Get resolution from outcome prices
        prices_str = m.get("outcomePrices", "[0.5,0.5]")
        try:
            prices = json.loads(prices_str) if isinstance(prices_str, str) else prices_str
        except:
            prices = [0.5, 0.5]

        yes_price = float(prices[0]) if prices else 0.5
        no_price = float(prices[1]) if len(prices) > 1 else 0.5
        actual_won_yes = yes_price > 0.5

        entry = {
            **m,
            "actual_won_yes": actual_won_yes,
            "yes_price_final": yes_price,
            "no_price_final": no_price,
        }

        if not parsed:
            enriched.append(entry)
            continue

        city = parsed["city"]
        threshold_c = parsed["threshold_c"]
        direction = parsed["direction"]
        coords = geocode_city(city)

        # Parse date
        date_str = parse_date_from_question(question)
        entry["city"] = city
        entry["threshold_c"] = threshold_c
        entry["direction"] = direction
        entry["is_exact_temp"] = parsed.get("is_exact_temp", False)
        entry["date_str"] = date_str

        if coords and date_str:
            lat, lon = coords
            entry["lat"] = lat
            entry["lon"] = lon

            # Fetch actual observation for that date
            cache_key = f"{city.lower()}_{date_str[:7]}"  # city+month
            if cache_key not in city_cache:
                # Get actual temp for that date
                url = f"https://archive-api.open-meteo.com/v1/archive?latitude={lat}&longitude={lon}&start_date={date_str}&end_date={date_str}&daily=temperature_2m_max&temperature_unit=celsius&format=json"
                data = _get(url)
                city_cache[cache_key] = data
                from time import sleep
                sleep(0.3)

            obs_data = city_cache.get(cache_key)
            if obs_data and obs_data.get("daily", {}).get("temperature_2m_max"):
                actual_max = obs_data["daily"]["temperature_2m_max"][0]
                entry["actual_temp"] = actual_max
                # margin_c = actual - threshold (used for confidence signal)
                entry["margin_c"] = actual_max - threshold_c if actual_max is not None else None

            # Also get forecast vs actual for the month (for empirical confidence)
            start = date_str[:8] + "01"  # First of month
            fc_data = fetch_forecast_vs_actual(lat, lon, start, date_str)
            if fc_data:
                errors = fc_data.get("forecast_errors_ecmwf", []) or fc_data.get("forecast_errors_gfs", [])
                entry["forecast_errors_sample"] = errors[-30:] if errors else []
                # Get forecast for target date
                dates = fc_data.get("dates", [])
                if date_str in dates:
                    idx = dates.index(date_str)
                    gfs_temps = fc_data.get("forecast_max_gfs", [])
                    if gfs_temps and idx < len(gfs_temps):
                        entry["forecast_temp"] = gfs_temps[idx]
                from time import sleep
                sleep(0.3)

        enriched.append(entry)
        print(f" ✓ {question[:60]} | city={city} | date={date_str} | actual_temp={entry.get('actual_temp','?')} | margin={entry.get('margin_c','?')}")

    return enriched


if __name__ == "__main__":
    init_backtest_db()

    # Load closed markets
    with open(DATA_DIR / "closed_temp_markets.json") as f:
        closed = json.load(f)
    print(f"Loaded {len(closed)} closed temperature markets")

    # Also load current live markets
    try:
        with open(DATA_DIR / "enhanced_markets.json") as f:
            live = json.load(f)
        print(f"Loaded {len(live)} live enhanced markets")
    except:
        live = []

    # Enrich closed markets with actual temperature data
    print("\nEnriching closed markets with Open-Meteo data...")
    enriched_closed = enrich_markets(closed)

    # Combine all
    all_markets = enriched_closed + live

    # Dedup
    seen = set()
    unique = []
    for m in all_markets:
        q = m.get("question", "")
        if q and q not in seen:
            seen.add(q)
            unique.append(m)
    markets = unique
    print(f"\nTotal unique markets for backtest: {len(markets)}")

    # Save enriched data
    with open(DATA_DIR / "enriched_all_markets.json", "w") as f:
        json.dump(markets, f, indent=2, default=str)

    # Run V2 backtest (modeled confidence)
    print("\n" + "="*60)
    print("RUNNING V2 BACKTEST (Modeled NWS Confidence)")
    print("="*60)
    v2_result = run_v2_backtest(markets, confidence_threshold=0.99)
    v2_sens = run_sensitivity(markets, engine="v2")
    v2_result["sensitivity"] = v2_sens
    v2_id = save_backtest_run(v2_result, "2.0", "Modeled NWS (Gaussian CDF)", "v2_gaussian", "polymarket+open-meteo",
                               notes="Enriched with actual observations from Open-Meteo archive")

    print(f"V2: {v2_result['qualifying_markets']} trades | WR: {v2_result['win_rate']:.1%} | P&L: ${v2_result['total_pnl']:.2f}")

    # Run V3 backtest (empirical confidence)
    print("\n" + "="*60)
    print("RUNNING V3 BACKTEST (Empirical Forecast Confidence)")
    print("="*60)
    v3_result = run_v3_backtest(markets, confidence_threshold=0.99)
    v3_sens = run_sensitivity(markets, engine="v3")
    v3_result["sensitivity"] = v3_sens
    v3_id = save_backtest_run(v3_result, "3.0", "Empirical NWS (Open-Meteo Errors)", "v3_empirical", "polymarket+open-meteo",
                               notes="Uses actual forecast-vs-observation error distributions for confidence")

    print(f"V3: {v3_result['qualifying_markets']} trades | WR: {v3_result['win_rate']:.1%} | P&L: ${v3_result['total_pnl']:.2f}")

    # Save market details for V3
    if v3_result.get("trades"):
        save_market_details(v3_id, v3_result["trades"])

    # Comparison
    print("\n" + "="*60)
    print("COMPARISON")
    print("="*60)
    for key in ["qualifying_markets", "winning_trades", "losing_trades", "win_rate", "total_pnl", "return_pct", "final_bankroll", "max_drawdown", "sharpe_ratio", "false_positives", "false_negatives"]:
        v2 = v2_result.get(key, 0)
        v3 = v3_result.get(key, 0)
        print(f"  {key:<25} V2={v2:>10}  V3={v3:>10}")

    # Show V3 trades with empirical vs modeled confidence comparison
    if v3_result.get("trades"):
        print("\nV3 Trade Details (empirical vs modeled confidence):")
        for t in v3_result["trades"]:
            modeled = t.get("nws_conf_modeled", "?")
            empirical = t.get("nws_conf_empirical", "?")
            print(f"  {t['question'][:50]} | modeled={modeled} | empirical={empirical} | used={t['confidence_used']} | {'WIN' if t['won'] else 'LOSS'}")

    # Export
    export = {
        "v2": {**v2_result, "run_id": v2_id},
        "v3": {**v3_result, "run_id": v3_id},
        "all_runs": list_backtest_runs(),
    }
    with open(DATA_DIR / "backtest_v2_v3_comparison.json", "w") as f:
        json.dump(export, f, indent=2, default=str)
    print(f"\nExported to {DATA_DIR}/backtest_v2_v3_comparison.json")
    print(f"\nAll logged runs:")
    for r in list_backtest_runs():
        print(f"  [#{r['id']}] v{r['version']} — {r['label'][:50]} | P&L: ${r.get('total_pnl',0):.2f} | WR: {r.get('win_rate',0):.1%}")
