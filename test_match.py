import json
import re
import sys
from pathlib import Path

# Add PolyWeather to sys.path
sys.path.append("/config/PolyWeather")
try:
    from src.data_collection.city_registry import CITY_REGISTRY
except ImportError:
    print("Could not import CITY_REGISTRY from PolyWeather")
    CITY_REGISTRY = {}

INPUT_FILE = "/config/coldmath/data/extended_weather_markets.json"

def clean_city(city):
    city = city.lower().strip()
    city = city.replace(" central park", "").replace(" airport", "")
    city = city.replace("'s", "")
    city = re.sub(r"\s+station.*", "", city)
    city = re.sub(r"\b(be|on|at|in)\b.*", "", city)
    return city.strip()

def parse_question_robust(q):
    q_clean = q.lower()
    m = re.search(r"temperature\s+in\s+([a-z'\s]+?)\s+(?:be|on|at|station|recorded)", q_clean)
    if m:
        return clean_city(m.group(1))
    m = re.search(r"recorded\s+at\s+(?:the\s+)?([a-z'\s]+?)\s+(?:be|on|at|station)", q_clean)
    if m:
        return clean_city(m.group(1))
    m = re.search(r"in\s+([a-z'\s]+?)\s+(?:be|on|at)", q_clean)
    if m:
        return clean_city(m.group(1))
    return "unknown"

def main():
    with open(INPUT_FILE) as f:
        raw_markets = json.load(f)
    
    matched = []
    unmatched = []
    
    registry_cities = {k.lower(): k for k in CITY_REGISTRY.keys()}
    # Also add aliases if any (registry seems to have names as keys)
    
    for m in raw_markets:
        q = m.get("question", "")
        if any(k in q.lower() for k in ["temp", "temperature", "degree", "celsius", "fahrenheit", "°c", "°f"]):
            city = parse_question_robust(q)
            if city == "unknown":
                unmatched.append(q)
                continue
            
            found = False
            for reg_city in registry_cities:
                if city == reg_city or reg_city in city or city in reg_city:
                    matched.append((q, reg_city))
                    found = True
                    break
            if not found:
                unmatched.append(q)
                
    print(f"Total matched: {len(matched)}")
    print(f"Total unmatched: {len(unmatched)}")
    
    if unmatched:
        print("\nSome unmatched questions:")
        for q in unmatched[:10]:
            print(f"  - {q}")

if __name__ == "__main__":
    main()
