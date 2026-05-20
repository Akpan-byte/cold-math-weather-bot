#!/usr/bin/env python3
"""Cold Math — Cron runner for price scraper."""
import sys
sys.path.insert(0, "/config/coldmath")
from scraper.price_scraper import init_db, scrape, get_db_stats
import json

init_db()
stats = scrape(active_only=True, fetch_depth=True)
db = get_db_stats()
print(json.dumps({"scrape": stats, "db": db}, indent=2))
