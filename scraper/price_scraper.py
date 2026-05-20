#!/usr/bin/env python3
"""
Cold Math — Price History Scraper
Snaps Polymarket weather market prices + orderbooks every 5min into SQLite.
Also fetches closed markets for historical baseline.
"""
import json
import logging
import re
import sqlite3
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# ─── Config ───
GAMMA_API = "https://gamma-api.polymarket.com/markets"
CLOB_API = "https://clob.polymarket.com"
DB_PATH = Path("/config/coldmath/data/price_history.db")
LOG_PATH = Path("/config/coldmath/data/scraper.log")
RATE_LIMIT_SEC = 0.5  # 2 req/sec to CLOB
MAX_RETRIES = 3
RETRY_BACKOFF = 2  # seconds, doubles each retry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, mode="a"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("coldmath.scraper")


# ─── HTTP helper ───
def _get(url: str, timeout: int = 30) -> dict | list | None:
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ColdMath/2.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = RETRY_BACKOFF * (2 ** attempt)
                log.warning(f"429 rate-limited on {url[:80]}, waiting {wait}s")
                time.sleep(wait)
            else:
                log.error(f"HTTP {e.code} on {url[:80]}")
                return None
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF * (2 ** attempt))
            else:
                log.error(f"Failed {url[:80]}: {e}")
                return None
    return None


# ─── DB Schema ───
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    market_id TEXT NOT NULL,
    condition_id TEXT,
    question TEXT,
    yes_price REAL,
    no_price REAL,
    yes_liquidity REAL DEFAULT 0,
    no_liquidity REAL DEFAULT 0,
    spread REAL DEFAULT 0,
    volume REAL DEFAULT 0,
    yes_token_id TEXT,
    no_token_id TEXT,
    end_date TEXT,
    is_active INTEGER DEFAULT 1,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS orderbooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER,
    token_id TEXT,
    side TEXT,
    price REAL,
    size REAL,
    timestamp TEXT,
    FOREIGN KEY (snapshot_id) REFERENCES snapshots(id)
);

CREATE TABLE IF NOT EXISTS market_meta (
    market_id TEXT PRIMARY KEY,
    condition_id TEXT,
    question TEXT,
    city TEXT,
    threshold_c REAL,
    threshold_f REAL,
    threshold_direction TEXT,
    end_date TEXT,
    first_seen TEXT,
    last_seen TEXT,
    is_active INTEGER DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_snap_ts ON snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_snap_market ON snapshots(market_id);
CREATE INDEX IF NOT EXISTS idx_snap_active ON snapshots(is_active);
CREATE INDEX IF NOT EXISTS idx_ob_snapshot ON orderbooks(snapshot_id);
"""

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript(SCHEMA_SQL)
    conn.close()
    log.info(f"DB initialized at {DB_PATH}")


# ─── Parse market question ───
def parse_weather_question(question: str) -> dict:
    """Extract city, threshold, direction from weather market question."""
    result = {"city": None, "threshold_c": None, "threshold_f": None, "threshold_direction": None}

    # Pattern: highest/lowest temperature in CITY be N°C on DATE
    high_match = re.search(
        r"(?:highest|maximum|high) temperature in (.+?) be (\d+)(?:°|º|°)([Cc])(?: or (higher|lower))? on",
        question, re.IGNORECASE
    )
    low_match = re.search(
        r"(?:lowest|minimum|low) temperature in (.+?) be (\d+)(?:°|º|°)([Cc])(?: or (higher|lower))? on",
        question, re.IGNORECASE
    )
    f_match = re.search(
        r"(?:highest|maximum|high|lowest|minimum|low) temperature in (.+?) be (\d+)(?:°|º|°)([Ff])(?: or (higher|lower))? on",
        question, re.IGNORECASE
    )

    if high_match:
        result["city"] = high_match.group(1).strip()
        val = float(high_match.group(2))
        result["threshold_c"] = val
        result["threshold_f"] = round(val * 9/5 + 32, 1)
        result["threshold_direction"] = "above"
    elif low_match:
        result["city"] = low_match.group(1).strip()
        val = float(low_match.group(2))
        result["threshold_c"] = val
        result["threshold_f"] = round(val * 9/5 + 32, 1)
        result["threshold_direction"] = "below"
    elif f_match:
        result["city"] = f_match.group(1).strip()
        val = float(f_match.group(2))
        result["threshold_f"] = val
        result["threshold_c"] = round((val - 32) * 5/9, 1)
        result["threshold_direction"] = "above" if "highest" in question.lower() or "high" in question.lower() else "below"

    return result


# ─── Parse outcome prices ───
def _parse_prices(prices_str) -> tuple[float, float]:
    try:
        prices = json.loads(prices_str) if isinstance(prices_str, str) else prices_str
        if isinstance(prices, list) and len(prices) >= 2:
            return float(prices[0]), float(prices[1])
    except:
        pass
    return 0.5, 0.5


# ─── Parse token IDs ───
def _parse_token_ids(tokens_str) -> tuple[str, str]:
    try:
        tokens = json.loads(tokens_str) if isinstance(tokens_str, str) else tokens_str
        if isinstance(tokens, list) and len(tokens) >= 2:
            return str(tokens[0]), str(tokens[1])
    except:
        pass
    return "", ""


# ─── Fetch markets from gamma API ───
def fetch_weather_markets(active_only: bool = True, max_pages: int = 10) -> list[dict]:
    """Fetch all weather-tagged markets from Polymarket."""
    all_markets = []
    base_params = {
        "tag": "weather",
        "limit": "500",
        "order": "volume",
        "ascending": "false",
    }
    if active_only:
        base_params["active"] = "true"
        base_params["closed"] = "false"
    else:
        base_params["closed"] = "true"

    for page in range(max_pages):
        offset = page * 500
        params = {**base_params, "offset": str(offset)}
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{GAMMA_API}?{query}"

        log.info(f"Fetching markets page {page+1} (offset={offset})")
        data = _get(url)
        if not data or not isinstance(data, list) or len(data) == 0:
            break

        # Filter for temperature markets
        temp_kw = ["temperature", "°c", "°f", "ºc", "ºf", "celsius", "fahrenheit"]
        for m in data:
            q = (m.get("question") or "").lower()
            if any(kw in q for kw in temp_kw):
                all_markets.append(m)

        if len(data) < 500:
            break
        time.sleep(RATE_LIMIT_SEC)

    log.info(f"Found {len(all_markets)} temperature markets")
    return all_markets


# ─── Fetch CLOB price for a token ───
def fetch_clob_price(token_id: str) -> float | None:
    """Get current price from CLOB API."""
    if not token_id:
        return None
    url = f"{CLOB_API}/price?token_id={token_id}&side=buy"
    data = _get(url)
    if isinstance(data, dict) and "price" in data:
        return float(data["price"])
    return None


# ─── Fetch CLOB orderbook ───
def fetch_orderbook(token_id: str) -> dict | None:
    """Get current orderbook from CLOB API."""
    if not token_id:
        return None
    url = f"{CLOB_API}/book?token_id={token_id}"
    data = _get(url)
    return data


# ─── Main scrape function ───
def scrape(active_only: bool = True, fetch_depth: bool = True) -> dict:
    """Run one full scrape cycle. Returns stats dict."""
    ts = datetime.now(timezone.utc).isoformat()
    log.info(f"=== Scrape started at {ts} (active_only={active_only}, depth={fetch_depth}) ===")

    markets = fetch_weather_markets(active_only=active_only, max_pages=5)
    if not markets:
        log.warning("No temperature markets found")
        return {"timestamp": ts, "markets_found": 0, "snapshots_saved": 0, "errors": 0}

    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()

    snapshots_saved = 0
    ob_saved = 0
    errors = 0

    for m in markets:
        market_id = str(m.get("id", ""))
        question = m.get("question", "")
        condition_id = m.get("conditionId", "")
        yes_price, no_price = _parse_prices(m.get("outcomePrices", "[0.5,0.5]"))
        yes_token, no_token = _parse_token_ids(m.get("clobTokenIds", "[]"))
        volume = float(m.get("volume", 0) or 0)
        end_date = m.get("endDate", "")
        is_active = 1 if m.get("active", True) else 0

        # Try to get live CLOB prices (more accurate than gamma's cached ones)
        clob_yes = fetch_clob_price(yes_token) if yes_token else None
        clob_no = fetch_clob_price(no_token) if no_token else None

        if clob_yes is not None:
            yes_price = clob_yes
        if clob_no is not None:
            no_price = clob_no

        spread = round(abs(yes_price - (1 - no_price)), 4) if yes_price and no_price else 0

        time.sleep(RATE_LIMIT_SEC)

        # Insert snapshot
        try:
            cur.execute("""
                INSERT INTO snapshots (timestamp, market_id, condition_id, question,
                    yes_price, no_price, spread, volume, yes_token_id, no_token_id,
                    end_date, is_active, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (ts, market_id, condition_id, question,
                  yes_price, no_price, spread, volume, yes_token, no_token,
                  end_date, is_active, json.dumps(m)[:5000]))
            snap_id = cur.lastrowid
            snapshots_saved += 1
        except Exception as e:
            log.error(f"Snapshot insert failed for {market_id}: {e}")
            errors += 1
            continue

        # Fetch and store orderbook depth
        if fetch_depth and yes_token:
            try:
                ob = fetch_orderbook(yes_token)
                if ob and isinstance(ob, dict):
                    for side_name in ["bids", "asks"]:
                        for level in ob.get(side_name, [])[:10]:
                            cur.execute("""
                                INSERT INTO orderbooks (snapshot_id, token_id, side, price, size, timestamp)
                                VALUES (?, ?, ?, ?, ?, ?)
                            """, (snap_id, yes_token, side_name,
                                  float(level.get("price", 0)),
                                  float(level.get("size", 0)),
                                  ts))
                            ob_saved += 1
                time.sleep(RATE_LIMIT_SEC)
            except Exception as e:
                log.error(f"Orderbook fetch failed for {yes_token[:20]}: {e}")
                errors += 1

        # Update market_meta
        parsed = parse_weather_question(question)
        try:
            cur.execute("""
                INSERT INTO market_meta (market_id, condition_id, question, city,
                    threshold_c, threshold_f, threshold_direction, end_date, first_seen, last_seen, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market_id) DO UPDATE SET
                    last_seen=excluded.last_seen,
                    is_active=excluded.is_active,
                    question=excluded.question,
                    end_date=excluded.end_date
            """, (market_id, condition_id, question, parsed["city"],
                  parsed["threshold_c"], parsed["threshold_f"], parsed["threshold_direction"],
                  end_date, ts, ts, is_active))
        except Exception as e:
            log.error(f"Meta upsert failed for {market_id}: {e}")

    conn.commit()
    conn.close()

    stats = {
        "timestamp": ts,
        "markets_found": len(markets),
        "snapshots_saved": snapshots_saved,
        "orderbook_levels_saved": ob_saved,
        "errors": errors,
    }
    log.info(f"=== Scrape complete: {stats} ===")
    return stats


# ─── Stats ───
def get_db_stats() -> dict:
    """Return quick stats about the price history DB."""
    if not DB_PATH.exists():
        return {"exists": False}
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM snapshots")
    total_snaps = cur.fetchone()[0]

    cur.execute("SELECT COUNT(DISTINCT market_id) FROM snapshots")
    unique_markets = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM snapshots WHERE is_active=1")
    active_snaps = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM orderbooks")
    total_ob = cur.fetchone()[0]

    cur.execute("SELECT MIN(timestamp), MAX(timestamp) FROM snapshots")
    row = cur.fetchone()
    first_ts, last_ts = row[0] or "N/A", row[1] or "N/A"

    cur.execute("SELECT COUNT(*) FROM market_meta WHERE is_active=1")
    active_meta = cur.fetchone()[0]

    conn.close()
    return {
        "exists": True,
        "total_snapshots": total_snaps,
        "unique_markets": unique_markets,
        "active_snapshots": active_snaps,
        "orderbook_levels": total_ob,
        "first_timestamp": first_ts,
        "last_timestamp": last_ts,
        "active_markets": active_meta,
    }


if __name__ == "__main__":
    init_db()
    stats = scrape(active_only=True, fetch_depth=True)
    print(json.dumps(stats, indent=2))
    print("\nDB Stats:", json.dumps(get_db_stats(), indent=2))
