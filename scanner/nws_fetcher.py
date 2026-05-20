#!/usr/bin/env python3
"""
Cold Math — NWS Forecast Fetcher
Fetches NWS API forecasts for cities with active Polymarket weather markets.
Computes margin_c and NWS confidence for each market.
"""
import json, math, urllib.request, re
from datetime import datetime, timezone
from pathlib import Path

CONFIG_DIR = "/config/coldmath/data"

# NWS API requires lat/lon for each city
CITY_COORDS = {
    "London": (51.5074, -0.1278),
    "New York City": (40.7128, -74.0060),
    "Denver": (39.7392, -104.9903),
    "Atlanta": (33.7490, -84.3880),
    "Ankara": (39.9334, 32.8597),
    "Shanghai": (31.2304, 121.4737),
    "Paris": (48.8566, 2.3522),
    "Toronto": (43.6532, -79.3832),
    "Buenos Aires": (-34.6037, -58.3816),
    "Milan": (45.4642, 9.1900),
    "Istanbul": (41.0082, 28.9784),
    "Wellington": (-41.2865, 174.7762),
    "Lucknow": (26.8467, 80.9462),
    "Madrid": (40.4168, -3.7038),
    "Mumbai": (19.0760, 72.8777),
    "Sydney": (-33.8688, 151.2093),
    "Tokyo": (35.6762, 139.6503),
    "Berlin": (52.5200, 13.4050),
    "Moscow": (55.7558, 37.6173),
    "Beijing": (39.9042, 116.4074),
    "Seoul": (37.5665, 126.9780),
    "Bangkok": (13.7563, 100.5018),
    "Cairo": (30.0444, 31.2357),
    "Lima": (-12.0464, -77.0428),
    "Mexico City": (19.4326, -99.1332),
    "São Paulo": (-23.5505, -46.6333),
    "Lagos": (6.5244, 3.3792),
    "Nairobi": (-1.2921, 36.8219),
    "Singapore": (1.3521, 103.8198),
    "Dubai": (25.2048, 55.2708),
}

NWS_RMSE = 0.8  # °C for 24h forecasts

def nws_confidence(margin_c, rmse=NWS_RMSE):
    """Calculate NWS confidence from margin distance."""
    z = abs(margin_c) / rmse
    return min(0.9999, 0.5 * (1 + math.erf(z / math.sqrt(2))))

def fetch_nws_forecast(city, lat, lon):
    """Fetch NWS forecast for a given city."""
    # NWS API: point → forecast office → grid → forecast
    point_url = f"https://api.weather.gov/points/{lat},{lon}"
    
    try:
        req = urllib.request.Request(point_url, headers={
            "User-Agent": "ColdMathBot/1.0 (research)",
            "Accept": "application/ld+json"
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            point_data = json.loads(resp.read().decode())
        
        forecast_url = point_data.get("properties", {}).get("forecast", "")
        if not forecast_url:
            return None
        
        req2 = urllib.request.Request(forecast_url, headers={
            "User-Agent": "ColdMathBot/1.0 (research)",
            "Accept": "application/ld+json"
        })
        with urllib.request.urlopen(req2, timeout=15) as resp2:
            forecast_data = json.loads(resp2.read().decode())
        
        periods = forecast_data.get("properties", {}).get("periods", [])
        
        forecasts = []
        for p in periods:
            temp_f = p.get("temperature")
            if temp_f is None:
                continue
            temp_c = round((temp_f - 32) * 5/9, 1)
            forecasts.append({
                "name": p.get("name", ""),
                "temp_f": temp_f,
                "temp_c": temp_c,
                "wind_speed": p.get("windSpeed", ""),
                "short_forecast": p.get("shortForecast", ""),
                "is_daytime": p.get("isDaytime", True),
                "start_time": p.get("startTime", ""),
            })
        
        return forecasts
    
    except Exception as e:
        print(f"  Error fetching NWS for {city}: {e}")
        return None

def fetch_open_meteo_forecast(city, lat, lon):
    """Fallback: Fetch forecast from Open-Meteo API (free, no auth, global coverage)."""
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}"
        f"&daily=temperature_2m_max,temperature_2m_min"
        f"&timezone=auto&forecast_days=3"
    )
    
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ColdMathBot/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        
        daily = data.get("daily", {})
        times = daily.get("time", [])
        max_temps = daily.get("temperature_2m_max", [])
        
        forecasts = []
        for i, t in enumerate(times):
            if i < len(max_temps) and max_temps[i] is not None:
                forecasts.append({
                    "date": t,
                    "max_temp_c": round(max_temps[i], 1),
                    "source": "open-meteo",
                })
        
        return forecasts
    
    except Exception as e:
        print(f"  Error fetching Open-Meteo for {city}: {e}")
        return None

def compute_margins(markets_file=None, output_file=None):
    """Load live markets, fetch forecasts, compute margins + confidence."""
    markets_file = markets_file or f"{CONFIG_DIR}/live_markets.json"
    output_file = output_file or f"{CONFIG_DIR}/enhanced_markets.json"
    
    with open(markets_file) as f:
        markets = json.load(f)
    
    enhanced = []
    for m in markets:
        city = m.get("city", "")
        threshold_c = m.get("threshold_c", m.get("threshold", 0))
        unit = m.get("unit", "C")
        
        # Convert threshold to Celsius if needed
        if unit == "F":
            threshold_c = round((m["threshold"] - 32) * 5/9, 1)
        
        # Get coordinates
        coords = CITY_COORDS.get(city)
        if not coords:
            print(f"  ⚠️  No coords for {city}, skipping")
            continue
        
        lat, lon = coords
        
        # Try NWS first (US cities), then Open-Meteo (global)
        forecasts = None
        if -125 <= lon <= -66 and 24 <= lat <= 50:  # Rough US bounds
            forecasts = fetch_nws_forecast(city, lat, lon)
        
        if not forecasts:
            forecasts = fetch_open_meteo_forecast(city, lat, lon)
        
        if not forecasts:
            print(f"  ❌ No forecast available for {city}")
            continue
        
        # Find the max forecast temp for the market date
        market_date = m.get("date_raw", "")
        forecast_max_c = None
        
        for f in forecasts:
            if isinstance(f, dict):
                # Open-Meteo format
                if "max_temp_c" in f:
                    fd = f.get("date", "")
                    # Try to match date
                    if market_date.lower() in fd.lower() or not market_date:
                        forecast_max_c = f["max_temp_c"]
                        break
                # NWS format — use first daytime period
                elif f.get("is_daytime") and forecast_max_c is None:
                    forecast_max_c = f.get("temp_c")
        
        if forecast_max_c is None and forecasts:
            # Fallback: use the first forecast
            f0 = forecasts[0]
            forecast_max_c = f0.get("max_temp_c") or f0.get("temp_c")
        
        if forecast_max_c is None:
            print(f"  ❌ No temp forecast for {city} on {market_date}")
            continue
        
        # Compute margin: how far is forecast from threshold
        # We're betting NO (temp won't exceed threshold)
        # Positive margin = forecast is BELOW threshold = NO likely wins
        margin_c = threshold_c - forecast_max_c
        
        conf = nws_confidence(margin_c)
        
        entry = {
            **m,
            "forecast_max_c": forecast_max_c,
            "margin_c": round(margin_c, 1),
            "nws_confidence": round(conf, 4),
            "forecast_source": "open-meteo" if "open-meteo" in str(forecasts[0]) else "nws",
        }
        enhanced.append(entry)
        
        status = "✅" if conf >= 0.97 else "⚠️" if conf >= 0.90 else "❌"
        print(f"  {status} {city:15s} | forecast={forecast_max_c}°C | threshold={threshold_c}°C | margin={margin_c:+.1f}°C | conf={conf:.1%}")
    
    # Save enhanced markets
    out_path = output_file if output_file else f"{CONFIG_DIR}/enhanced_markets.json"
    with open(out_path, "w") as f:
        json.dump(enhanced, f, indent=2)
    
    print(f"\nSaved {len(enhanced)} enhanced markets to {out_path}")
    return enhanced

if __name__ == "__main__":
    from pathlib import Path
    # Fix output path
    output = f"{CONFIG_DIR}/enhanced_markets.json"
    compute_margins(output_file=output)
