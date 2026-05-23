#!/usr/bin/env python3
"""
Cold Math — Modal Backtest App (Ultimate Global Sniper v7)
=========================================================
Strategy: Sniper Logic (BUNDLE + FORECAST_TIMING)
Vision: 50+ Cities + All Categories (Temp, AQI, Rain)
Goal: Daily Frequency + 85%+ Win Rate
"""

import json
import math
import re
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# ─── Modal setup ───────────────────────────────────────────
import modal

app = modal.App("coldmath-ultimate-sniper-v1")

image = (modal.Image.debian_slim()
         .pip_install("numpy", "pandas", "matplotlib", "seaborn")
         .add_local_dir("/config/coldmath/data", "/root/data"))

# ─── Constants ─────────────────────────────────────────────
DATA_DIR   = Path("/root/data")
OUT_DIR    = Path("/root/output")
POLYMARKET = "https://clob.polymarket.com"
OM_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"

CITY_COORDS = {
    "new york": (40.71, -74.01), "los angeles": (34.05, -118.24),
    "chicago": (41.88, -87.63), "houston": (29.76, -95.37),
    "phoenix": (33.45, -112.07), "denver": (39.74, -104.99),
    "miami": (25.76, -80.19), "atlanta": (33.75, -84.39),
    "boston": (42.36, -71.06), "seattle": (47.61, -122.33),
    "san francisco": (37.77, -122.42), "washington": (38.91, -77.04),
    "london": (51.51, -0.13), "paris": (48.86, 2.35),
    "tokyo": (35.68, 139.69), "beijing": (39.90, 116.41),
    "shanghai": (31.23, 121.47), "seoul": (37.57, 126.98),
    "mumbai": (19.08, 72.88), "singapore": (1.35, 103.82),
    "dubai": (25.20, 55.27), "milan": (45.46, 9.19),
    "munich": (48.14, 11.58), "amsterdam": (52.37, 4.90),
    "toronto": (43.65, -79.38), "madrid": (40.42, -3.70),
    "mexico city": (19.43, -99.13), "buenos aires": (-34.60, -58.38),
    "sydney": (-33.87, 151.21), "melbourne": (-37.81, 144.96),
    "johannesburg": (-26.20, 28.05), "moscow": (55.76, 37.62),
    "istanbul": (41.01, 28.98), "chongqing": (29.56, 106.55),
    "guangzhou": (23.13, 113.26), "shenzhen": (22.54, 114.06),
    "chengdu": (30.57, 104.07), "wuhan": (30.59, 114.29),
    "ankara": (39.93, 32.86), "tel aviv": (32.08, 34.78),
    "helsinki": (60.17, 24.94), "cape town": (-33.92, 18.42),
    "jakarta": (-6.21, 106.85), "kuala lumpur": (3.14, 101.69),
    "manila": (14.60, 120.98), "qingdao": (36.07, 120.38),
    "lucknow": (26.85, 80.95), "busan": (35.18, 129.08),
    "sao paulo": (-23.55, -46.63), "bucharest": (44.43, 26.10),
    "austin": (30.27, -97.74), "detroit": (42.33, -83.05),
    "nashville": (36.16, -86.77), "las vegas": (36.17, -115.14),
    "portland": (45.52, -122.68), "vienna": (48.21, 16.37),
    "stockholm": (59.33, 18.07), "oslo": (59.91, 10.75),
    "zurich": (47.38, 8.54), "lisbon": (38.72, -9.14),
    "athens": (37.98, 23.73), "hong kong": (22.32, 114.17),
    "taipei": (25.03, 121.57), "wellington": (-41.29, 174.78),
    "warsaw": (52.23, 21.01), "jeddah": (21.54, 39.17),
    "panama city": (8.98, -79.52), "atlanta": (33.75, -84.39),
}

# ─── Utilities ─────────────────────────────────────────────

def _get(url: str, timeout: int = 20) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ColdMath/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 429: time.sleep(1)
        return None
    except Exception: return None

def parse_question(q: str):
    q_clean = q.lower()
    unit = "F" if any(x in q_clean for x in ["°f", "fahrenheit", "farenheit"]) else "C"
    city = "unknown"
    for known_city in CITY_COORDS.keys():
        if known_city in q_clean:
            city = known_city
            break
    nums = re.findall(r"(\d+(?:\.\d+)?)", q_clean)
    thresh = float(nums[-1]) if nums else None
    comparison = "exact"
    if "or higher" in q_clean: comparison = "or_higher"
    elif "or below" in q_clean: comparison = "or_below"
    elif "between" in q_clean: comparison = "range"
    thresh_c = thresh
    if unit == "F" and thresh is not None:
        thresh_c = (thresh - 32) * 5 / 9
    return {"city": city, "target_temp": thresh_c, "unit": unit, "comparison": comparison}

def fetch_aqi(city: str, date_str: str) -> float | None:
    coords = CITY_COORDS.get(city.lower())
    if not coords: return None
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        url = (f"https://air-quality-api.open-meteo.com/v1/air-quality?latitude={coords[0]}&longitude={coords[1]}&start_date={dt:%Y-%m-%d}&end_date={dt:%Y-%m-%d}&hourly=pm2_5&timezone=auto")
        d = _get(url)
        if d and "hourly" in d:
            vals = [v for v in d["hourly"].get("pm2_5", []) if v is not None]
            return max(vals) if vals else None
    except: return None
    return None

def fetch_precipitation(city: str, date_str: str) -> float | None:
    coords = CITY_COORDS.get(city.lower())
    if not coords: return None
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        url = (f"{OM_ARCHIVE}?latitude={coords[0]}&longitude={coords[1]}&start_date={dt:%Y-%m-%d}&end_date={dt:%Y-%m-%d}&daily=precipitation_sum&timezone=auto")
        d = _get(url)
        if d and "daily" in d:
            vals = [v for v in d["daily"].get("precipitation_sum", []) if v is not None]
            return vals[0] if vals else None
    except: return None
    return None

def fetch_actual_temp(city: str, date_str: str) -> float | None:
    coords = CITY_COORDS.get(city.lower())
    if not coords: return None
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        url = (f"{OM_ARCHIVE}?latitude={coords[0]}&longitude={coords[1]}&start_date={dt:%Y-%m-%d}&end_date={dt:%Y-%m-%d}&daily=temperature_2m_max&timezone=auto&temperature_unit=celsius")
        d = _get(url)
        if d and "daily" in d:
            temps = [t for t in d["daily"]["temperature_2m_max"] if t is not None]
            return temps[0] if temps else None
    except: return None
    return None

def fetch_forecast_temp(city: str, date_str: str) -> tuple[float | None, float | None]:
    coords = CITY_COORDS.get(city.lower())
    if not coords: return None, None
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        url = (f"https://historical-forecast-api.open-meteo.com/v1/forecast?latitude={coords[0]}&longitude={coords[1]}&start_date={dt:%Y-%m-%d}&end_date={dt:%Y-%m-%d}&daily=temperature_2m_max&timezone=auto&temperature_unit=celsius&models=gfs_seamless,ecmwf_ifs025&format=json")
        d = _get(url)
        if d and "daily" in d:
            gfs = d["daily"].get("temperature_2m_max_gfs_seamless", [None])[0]
            ecm = d["daily"].get("temperature_2m_max_ecmwf_ifs025", [None])[0]
            return gfs, ecm
    except: pass
    return None, None

@app.function(image=image, timeout=600, max_containers=5)
def enrich_single_market(m: dict) -> dict | None:
    q = m.get("question", "")
    q_info = parse_question(q)
    city = q_info["city"]
    end_date = m.get("end_date")
    if city == "unknown" or not end_date: return None
    op = m.get("outcomePrices", [])
    if isinstance(op, str): op = json.loads(op)
    outcome = "YES" if op and float(op[0]) > 0.5 else "NO"
    m["outcome"] = outcome
    m.update(q_info)
    if "Air Quality" in q or "AQI" in q:
        m["market_type"] = "AQI"
        m["actual_val"] = fetch_aqi(city, end_date)
    elif any(x in q.lower() for x in ["precipitation", "rainfall", "flood", "snow"]):
        m["market_type"] = "RAIN"
        m["actual_val"] = fetch_precipitation(city, end_date)
    else:
        m["market_type"] = "TEMP"
        gfs, ecmwf = fetch_forecast_temp(city, end_date)
        actual = fetch_actual_temp(city, end_date)
        m["gfs_forecast"] = gfs
        m["ecmwf_forecast"] = ecmwf
        m["actual_val"] = actual
    if m.get("actual_val") is None: return None
    print(f"  [success] Enriched {city} ({m['market_type']})")
    return m

# ─── Backtest Engine ───────────────────────────────────────

def _entry_price(market: dict, side: str, confidence: float) -> float:
    outcome = market.get("outcome", "NO")
    if outcome == "YES": yes_price = 0.50 + (confidence * 0.45)
    else: yes_price = 0.05 + (confidence * 0.40)
    side_price = yes_price if side == "YES" else (1.0 - yes_price)
    return max(side_price, 0.10)

def _simulate_pnl(market: dict, side: str, kelly: float, offset: float, price: float) -> float:
    eff_entry = min(price + offset, 0.99)
    win = (side == market.get("outcome"))
    if win: return kelly * ((1.0 / eff_entry) - 1.0)
    return -kelly

def run_backtest(markets: list, strategy_combo: tuple, params: tuple) -> dict:
    strat_name, opts = strategy_combo
    kelly, min_conf, min_margin, offset = params
    pnls = []
    for m in markets:
        fc = m.get("gfs_forecast") or m.get("ecmwf_forecast")
        act = m.get("actual_val")
        thr = m.get("target_temp")
        cmp = m.get("comparison")
        if fc is None or act is None or thr is None: continue
        margin = abs(fc - thr) if cmp == "exact" else (fc - thr if cmp == "or_higher" else thr - fc)
        if abs(margin) < min_margin: continue
        sigma = 1.15
        conf = 0.5 * (1 + math.erf(abs(margin) / (sigma * math.sqrt(2))))
        active = [strat_name] + list(opts.values())
        if "FORECAST_TIMING" in active: conf *= 0.90
        if conf < min_conf: continue
        side = "YES" if margin > 0 else "NO"
        price = _entry_price(m, side, conf)
        pnl = _simulate_pnl(m, side, kelly, offset, price)
        pnls.append(pnl)
    if not pnls: return {"total_pnl": 0, "n_trades": 0, "win_rate": 0}
    return {
        "total_pnl": sum(pnls),
        "n_trades": len(pnls),
        "win_rate": sum(1 for p in pnls if p > 0) / len(pnls),
        "kelly": kelly, "min_conf": min_conf, "min_margin": min_margin, "offset": offset,
        "strategy": strat_name, "opts": opts
    }

@app.function(image=image, timeout=3600, cpu=8)
def run_matrix_backtest(markets: list):
    import itertools
    import pandas as pd
    combos = [("BUNDLE", {}), ("BUNDLE", {"secondary": "FORECAST_TIMING"}), ("BIDIRECTIONAL", {})]
    params = list(itertools.product([0.1, 0.2, 0.3], [0.51, 0.60, 0.70], [0.0, 0.5, 1.0], [0.02]))
    results = []
    for c in combos:
        for p in params:
            results.append(run_backtest(markets, c, p))
    return pd.DataFrame(results).sort_values("total_pnl", ascending=False).to_dict(orient="records")

@app.local_entrypoint()
def main():
    print("Starting ULTIMATE GLOBAL SNIPER Backtest...")
    with open("/config/coldmath/data/extended_weather_markets.json") as f:
        data = json.load(f)
    enriched = []
    for result in enrich_single_market.map(data, order_outputs=False):
        if result: enriched.append(result)
    print(f"Enriched {len(enriched)} markets. Running strategy matrix...")
    results = run_matrix_backtest.remote(enriched)
    top = results[0]
    print("\n" + "="*50)
    print("ULTIMATE GLOBAL SNIPER RESULTS")
    print("="*50)
    print(f"Best Strategy: {top['strategy']} + {top['opts']}")
    print(f"Total Trades:  {top['n_trades']}")
    print(f"Trades/Week:   {top['n_trades'] / 260:.2f}")
    print(f"Win Rate:      {top['win_rate']:.1%}")
    print(f"Total PnL:     {top['total_pnl']:+.2f}")
    print("="*50)
