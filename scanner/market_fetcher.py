#!/usr/bin/env python3
"""
Cold Math — Market Fetcher
Fetches live weather markets from Polymarket API.
Extracts city, threshold, date from market questions.
"""
import json, re, os, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
import urllib.request

POLYMARKET_API = "https://gamma-api.polymarket.com/markets"

def _parse_price(prices_str):
    """Parse outcome prices from API response (handles JSON strings inside JSON)."""
    try:
        prices = json.loads(prices_str) if isinstance(prices_str, str) else prices_str
        if isinstance(prices, list) and len(prices) > 0:
            return float(prices[0])
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    # Fallback: strip brackets and quotes
    cleaned = prices_str.strip("[]\"' ")
    return float(cleaned.split(",")[0].strip().strip('"'))

def fetch_weather_markets():
    """Fetch all weather-related markets from Polymarket."""
    params = {
        "tag": "weather",
        "active": "true",
        "closed": "false",
        "limit": 100,
        "order": "volume",
        "ascending": "false",
    }
    
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{POLYMARKET_API}?{query}"
    
    print(f"Fetching: {url}")
    
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ColdMath/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"Error fetching markets: {e}")
        return []
    
    weather_markets = []
    for m in data:
        q = m.get("question", "")
        if any(w in q.lower() for w in ["temperature", "temp", "°c", "°f", "fahrenheit", "celsius"]):
            parsed = parse_weather_question(q)
            if parsed:
                entry = {
                    "question": q,
                    "condition_id": m.get("conditionId", ""),
                    "market_id": m.get("id", ""),
                    **parsed,
                    "yes_price": _parse_price(m.get("outcomePrices", "[0.5,0.5]")),
                    "volume": float(m.get("volume", 0)),
                    "end_date": m.get("endDate", ""),
                    "active": m.get("active", False),
                }
                weather_markets.append(entry)
    
    return weather_markets

def parse_weather_question(question):
    """Parse city, threshold, date from a weather market question.
    
    Examples:
      "Will the highest temperature in London be 18°C on February 25?"
      "Will the highest temperature in Denver be 78°F or higher on May 14?"
    """
    # Pattern: temperature in CITY be THRESHOLD on DATE
    patterns = [
        r"(?:highest|maximum|high) temperature in (.+?) be (\d+)(?:°|°)([CF])(?: or higher)? on (.+?)[\?\.]?$",
        r"(?:lowest|minimum|low) temperature in (.+?) be (\d+)(?:°|°)([CF])(?: or lower)? on (.+?)[\?\.]?$",
        r"temperature in (.+?) (?:reach|exceed|hit) (\d+)(?:°|°)([CF]) on (.+?)[\?\.]?$",
    ]
    
    for pat in patterns:
        match = re.search(pat, question, re.IGNORECASE)
        if match:
            city = match.group(1).strip()
            threshold = int(match.group(2))
            unit = match.group(3).upper()
            date_str = match.group(4).strip()
            
            # Convert to Celsius if Fahrenheit
            if unit == "F":
                threshold_c = round((threshold - 32) * 5/9, 1)
            else:
                threshold_c = threshold
            
            return {
                "city": city,
                "threshold": threshold,
                "threshold_c": threshold_c,
                "unit": unit,
                "date_raw": date_str,
            }
    
    return None

def save_markets(markets, path="/config/coldmath/data/live_markets.json"):
    """Save fetched markets to disk."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(markets, f, indent=2)
    print(f"Saved {len(markets)} markets to {path}")

if __name__ == "__main__":
    markets = fetch_weather_markets()
    print(f"\nFound {len(markets)} weather markets")
    for m in markets[:10]:
        print(f"  {m['city']:15s} | {m['threshold']}{m['unit']} | vol=${m['volume']:,.0f} | yes={m['yes_price']:.2f}")
    if markets:
        save_markets(markets)
    else:
        print("No qualifying weather markets found right now.")
        # Write empty list so scanner knows we checked
        save_markets([], "/config/coldmath/data/live_markets.json")
