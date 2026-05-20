#!/usr/bin/env python3
"""
Cold Math — Backtest v3 with Open-Meteo Historical Forecast Data
Replaces modeled NWS confidence (Gaussian CDF from margin) with actual forecast error rates.

Key improvement: Instead of assuming NWS confidence from margin, we:
1. Fetch actual historical forecasts from Open-Meteo for each city
2. Compare forecast vs actual observation to compute REAL forecast error
3. Use empirical error distribution to calculate TRUE confidence
4. Backtest with real confidence instead of modeled confidence
"""
import json
import logging
import math
import re
import sqlite3
import statistics
import urllib.request
import urllib.error
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import sleep

# ─── Config ───
OM_FORECAST_API = "https://historical-forecast-api.open-meteo.com/v1/forecast"
OM_ARCHIVE_API = "https://archive-api.open-meteo.com/v1/archive"
BACKTEST_DB = Path("/config/coldmath/data/backtest_log.db")
DATA_DIR = Path("/config/coldmath/data")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("coldmath.backtest_v3")


# ─── HTTP ───
def _get(url: str, timeout: int = 30) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ColdMath/3.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        log.error(f"GET failed {url[:80]}: {e}")
        return None


# ─── City geocoding ───
CITY_COORDS = {
    # US cities
    "new york": (40.71, -74.01), "los angeles": (34.05, -118.24),
    "chicago": (41.88, -87.63), "houston": (29.76, -95.37),
    "phoenix": (33.45, -112.07), "denver": (39.74, -104.99),
    "dallas": (32.78, -96.80), "miami": (25.76, -80.19),
    "atlanta": (33.75, -84.39), "boston": (42.36, -71.06),
    "seattle": (47.61, -122.33), "san francisco": (37.77, -122.42),
    "washington": (38.91, -77.04), "detroit": (42.33, -83.05),
    "philadelphia": (39.95, -75.17), "minneapolis": (44.98, -93.27),
    "nashville": (36.16, -86.77), "portland": (45.52, -122.68),
    "las vegas": (36.17, -115.14), "austin": (30.27, -97.74),
    # International
    "london": (51.51, -0.13), "paris": (48.86, 2.35),
    "tokyo": (35.68, 139.69), "beijing": (39.90, 116.41),
    "shanghai": (31.23, 121.47), "seoul": (37.57, 126.98),
    "mumbai": (19.08, 72.88), "delhi": (28.61, 77.21),
    "cairo": (30.04, 31.24), "sydney": (-33.87, 151.21),
    "toronto": (43.65, -79.38), "moscow": (55.76, 37.62),
    "berlin": (52.52, 13.41), "rome": (41.90, 12.50),
    "madrid": (40.42, -3.70), "istanbul": (41.01, 28.98),
    "bangkok": (13.76, 100.50), "singapore": (1.35, 103.82),
    "dubai": (25.20, 55.27), "mexico city": (19.43, -99.13),
    "são paulo": (-23.55, -46.63), "buenos aires": (-34.60, -58.38),
    "ankara": (39.93, 32.86), "tel aviv": (32.08, 34.78),
    "chongqing": (29.56, 106.55), "melbourne": (-37.81, 144.96),
    "bucharest": (44.43, 26.10), "warsaw": (52.23, 21.01),
    "budapest": (47.50, 19.04), "vienna": (48.21, 16.37),
    "amsterdam": (52.37, 4.90), "brussels": (50.85, 4.35),
    "zurich": (47.38, 8.54), "stockholm": (59.33, 18.07),
    "oslo": (59.91, 10.75), "helsinki": (60.17, 24.94),
    "copenhagen": (55.68, 12.57), "dublin": (53.35, -6.26),
    "lisbon": (38.72, -9.14), "athens": (37.98, 23.73),
    "jakarta": (-6.21, 106.85), "kuala lumpur": (3.14, 101.69),
    "taipei": (25.03, 121.57), "hong kong": (22.32, 114.17),
    "nairobi": (-1.29, 36.82), "lagos": (6.52, 3.38),
    "capetown": (-33.92, 18.42), "johannesburg": (-26.20, 28.05),
    "lima": (-12.05, -77.04), "bogota": (4.71, -74.07),
    "santiago": (-33.45, -70.67),    "caracas": (10.49, -66.88),
    # Additional cities from Polymarket markets
    "lucknow": (26.85, 80.95), "wellington": (-41.29, 174.78),
    "manila": (14.60, 120.98), "qingdao": (36.07, 120.38),
    "shenzhen": (22.54, 114.06), "jeddah": (21.49, 39.19),
    "new york": (40.71, -74.01), "new york city": (40.71, -74.01),
}

def geocode_city(city: str) -> tuple[float, float] | None:
    """Look up lat/lon for a city name."""
    # Clean: strip possessives like "New York's Central Park" → "new york"
    key = city.lower().strip()
    # Remove possessives and sub-locations
    key = re.sub(r"'s.*$", "", key)  # "New York's Central Park" → "new york"
    key = re.sub(r"[,].*$", "", key)  # "Paris, France" → "paris"
    key = key.strip()
    
    if key in CITY_COORDS:
        return CITY_COORDS[key]
    # Fuzzy match: check if any key contains the city or vice versa
    for k, v in CITY_COORDS.items():
        if key in k or k in key:
            return v
    return None


# ─── Parse market question ───
def parse_market(question: str) -> dict | None:
    """Extract city, threshold, direction, date from market question."""
    # "Will the highest temperature in X be between Y-Z°F on DATE"
    # → YES if temp falls in range, NO otherwise. Narrow range → always buy NO.
    m = re.search(
        r"(?:highest|maximum|high) temperature in (.+?) be between (\d+)-(\d+)(?:°|º|°)([CcFf]).*?on (\w+ \d+)",
        question, re.IGNORECASE
    )
    if m:
        city = m.group(1).strip()
        low_f = float(m.group(2))
        high_f = float(m.group(3))
        unit = m.group(4).upper()
        date_str = m.group(5).strip()
        if unit == "C":
            threshold_c = (low_f + high_f) / 2
        else:
            threshold_c = round(((low_f + high_f) / 2 - 32) * 5/9, 1)
        direction = "above"
        return {"city": city, "threshold_c": threshold_c, "direction": direction,
                "date_str": date_str, "is_exact_temp": True, "range_width_c": (high_f - low_f)}

    # "Will the highest temperature in Seoul be 20°C on May 21?"
    # → EXACT temp if no "or higher/lower", THRESHOLD if "or higher/lower"
    m = re.search(
        r"(?:highest|maximum|high) temperature in (.+?) be (\d+)(?:°|º|°)([CcFf])(?: or (higher|lower|below|above))? on (\w+ \d+)",
        question, re.IGNORECASE
    )
    if m:
        city = m.group(1).strip()
        threshold = float(m.group(2))
        unit = m.group(3).upper()
        direction_hint = (m.group(4) or "").lower()
        date_str = m.group(5).strip()
        threshold_c = threshold if unit == "C" else round((threshold - 32) * 5/9, 1)
        is_exact = not direction_hint  # No "or higher/lower" = exact temp market
        if is_exact:
            direction = "above"  # placeholder; we'll always buy NO for exact
        elif "below" in direction_hint or "lower" in direction_hint:
            direction = "below"
        else:
            direction = "above"
        return {"city": city, "threshold_c": threshold_c, "direction": direction,
                "date_str": date_str, "is_exact_temp": is_exact}

    # "Will the lowest temperature in X be Y°C or below on DATE"
    m = re.search(
        r"(?:lowest|minimum|low) temperature in (.+?) be (\d+)(?:°|º|°)([CcFf])(?: or (higher|lower|below|above))? on (\w+ \d+)",
        question, re.IGNORECASE
    )
    if m:
        city = m.group(1).strip()
        threshold = float(m.group(2))
        unit = m.group(3).upper()
        direction_hint = (m.group(4) or "").lower()
        date_str = m.group(5).strip()
        threshold_c = threshold if unit == "C" else round((threshold - 32) * 5/9, 1)
        is_exact = not direction_hint
        if is_exact:
            direction = "below"  # placeholder; we'll always buy NO for exact
        elif "above" in direction_hint or "higher" in direction_hint:
            direction = "above"
        else:
            direction = "below"
        return {"city": city, "threshold_c": threshold_c, "direction": direction,
                "date_str": date_str, "is_exact_temp": is_exact}

    return None


# ─── Fetch forecast + observation for a city/date range ───
def fetch_forecast_vs_actual(lat: float, lon: float, start: str, end: str) -> dict:
    """Fetch both forecast and actual temperatures for comparison.
    
    Returns: {
        "dates": [...],
        "forecast_max": [...],  # GFS forecast for max temp
        "ecmwf_max": [...],     # ECMWF forecast for max temp  
        "actual_max": [...],    # Observed max temp
        "forecast_errors_gfs": [...],  # GFS error per day
        "forecast_errors_ecmwf": [...],  # ECMWF error per day
    }
    """
    params_forecast = {
        "latitude": lat, "longitude": lon,
        "start_date": start, "end_date": end,
        "daily": "temperature_2m_max",
        "temperature_unit": "celsius",
        "models": "gfs_seamless,ecmwf_ifs025",
        "format": "json",
    }
    params_actual = {
        "latitude": lat, "longitude": lon,
        "start_date": start, "end_date": end,
        "daily": "temperature_2m_max",
        "temperature_unit": "celsius",
        "format": "json",
    }

    f_url = OM_FORECAST_API + "?" + "&".join(f"{k}={v}" for k, v in params_forecast.items())
    a_url = OM_ARCHIVE_API + "?" + "&".join(f"{k}={v}" for k, v in params_actual.items())

    forecast_data = _get(f_url)
    actual_data = _get(a_url)

    if not forecast_data or not actual_data:
        return None

    f_daily = forecast_data.get("daily", {})
    a_daily = actual_data.get("daily", {})

    dates = f_daily.get("time", [])
    gfs_temps = f_daily.get("temperature_2m_max_gfs_seamless", [])
    ecmwf_temps = f_daily.get("temperature_2m_max_ecmwf_ifs025", [])
    actual_temps = a_daily.get("temperature_2m_max", [])

    if not dates or not actual_temps:
        return None

    # Compute errors
    gfs_errors = []
    ecmwf_errors = []
    for i in range(min(len(dates), len(gfs_temps), len(ecmwf_temps), len(actual_temps))):
        if gfs_temps[i] is not None and actual_temps[i] is not None:
            gfs_errors.append(gfs_temps[i] - actual_temps[i])
        if ecmwf_temps[i] is not None and actual_temps[i] is not None:
            ecmwf_errors.append(ecmwf_temps[i] - actual_temps[i])

    return {
        "dates": dates,
        "forecast_max_gfs": gfs_temps,
        "forecast_max_ecmwf": ecmwf_temps,
        "actual_max": actual_temps,
        "forecast_errors_gfs": gfs_errors,
        "forecast_errors_ecmwf": ecmwf_errors,
    }


# ─── Compute empirical confidence ───
def compute_empirical_confidence(errors: list[float], threshold: float, margin: float,
                                  shrinkage: float = 0.4) -> float:
    """
    Given a list of historical forecast errors, compute the probability
    that the actual temperature will exceed (or fall below) the threshold.
    
    Uses Bayesian shrinkage: blends empirical error distribution with
    the Gaussian prior (sigma=2.5) to prevent overconfidence from small samples.
    
    shrinkage: weight toward Gaussian prior (0=pure empirical, 1=pure Gaussian)
    """
    from math import erf, sqrt
    
    # Gaussian prior (the modeled approach)
    prior_sigma = 2.5
    prior_conf = _gaussian_confidence(margin, sigma=prior_sigma)
    
    if not errors or len(errors) < 5:
        return prior_conf

    n = len(errors)
    mean_err = statistics.mean(errors)
    std_err = statistics.stdev(errors) if n > 1 else prior_sigma

    if std_err == 0:
        std_err = 1.0

    # Shrink toward prior as sample size decreases
    # n=5 → shrinkage~0.7, n=30 → shrinkage~0.4, n=90 → shrinkage~0.25
    adaptive_shrinkage = shrinkage + (1 - shrinkage) * max(0, (30 - n) / 30)
    
    # Inflate empirical std to account for estimation uncertainty
    # (t-distribution adjustment for small samples)
    t_inflation = 1.0 + (2.0 / n)  # mild inflation for small n
    adjusted_std = std_err * t_inflation
    
    # Empirical confidence from error distribution
    z = (-margin - mean_err) / adjusted_std
    cdf = 0.5 * (1 + erf(z / sqrt(2)))
    
    if margin > 0:
        empirical_conf = max(0.5, min(1.0, 1 - cdf))
    else:
        empirical_conf = max(0.5, min(1.0, cdf))
    
    # Blend: shrink empirical toward prior
    blended = adaptive_shrinkage * prior_conf + (1 - adaptive_shrinkage) * empirical_conf
    
    return max(0.5, min(1.0, blended))


def _gaussian_confidence(margin: float, sigma: float = 2.5) -> float:
    """Gaussian CDF confidence from margin/sigma. Returns P(bet direction correct)."""
    from math import erf, sqrt
    if sigma <= 0:
        sigma = 1.0
    z = margin / sigma
    cdf = 0.5 * (1 + erf(z / sqrt(2)))
    # P(actual > threshold) when margin > 0, P(actual < threshold) when margin < 0
    if margin > 0:
        return max(0.5, min(1.0, cdf))
    else:
        return max(0.5, min(1.0, 1 - cdf))


# ─── Backtest DB Schema ───
BACKTEST_SCHEMA = """
CREATE TABLE IF NOT EXISTS backtest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL,
    label TEXT,
    timestamp TEXT NOT NULL,
    engine TEXT NOT NULL,
    data_source TEXT,
    total_markets INTEGER,
    qualifying_markets INTEGER,
    winning_trades INTEGER,
    losing_trades INTEGER,
    win_rate REAL,
    total_pnl REAL,
    return_pct REAL,
    final_bankroll REAL,
    max_drawdown REAL,
    sharpe_ratio REAL,
    avg_margin_c REAL,
    min_margin_c REAL,
    false_positives INTEGER,
    false_negatives INTEGER,
    confidence_threshold REAL DEFAULT 0.99,
    kelly_fraction REAL DEFAULT 0.25,
    starting_bankroll REAL DEFAULT 25.0,
    config_json TEXT,
    equity_curve_json TEXT,
    trades_json TEXT,
    sensitivity_json TEXT,
    monte_carlo_json TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS backtest_market_details (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    market_question TEXT,
    city TEXT,
    threshold_c REAL,
    forecast_temp REAL,
    actual_temp REAL,
    margin_c REAL,
    nws_confidence_modeled REAL,
    nws_confidence_empirical REAL,
    confidence_used REAL,
    entry_price REAL,
    position_size REAL,
    pnl REAL,
    bankroll_after REAL,
    won INTEGER,
    forecast_source TEXT,
    raw_json TEXT,
    FOREIGN KEY (run_id) REFERENCES backtest_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_runs_version ON backtest_runs(version);
CREATE INDEX IF NOT EXISTS idx_runs_timestamp ON backtest_runs(timestamp);
CREATE INDEX IF NOT EXISTS idx_details_run ON backtest_market_details(run_id);
"""

def init_backtest_db():
    BACKTEST_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(BACKTEST_DB))
    conn.executescript(BACKTEST_SCHEMA)
    conn.close()
    log.info(f"Backtest DB initialized at {BACKTEST_DB}")


# ─── Save backtest run ───
def save_backtest_run(result: dict, version: str, label: str, engine: str,
                      data_source: str, notes: str = "", config: dict = None) -> int:
    """Save a backtest run to the log. Returns run_id."""
    conn = sqlite3.connect(str(BACKTEST_DB))
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO backtest_runs (
            version, label, timestamp, engine, data_source,
            total_markets, qualifying_markets, winning_trades, losing_trades,
            win_rate, total_pnl, return_pct, final_bankroll, max_drawdown,
            sharpe_ratio, avg_margin_c, min_margin_c,
            false_positives, false_negatives,
            confidence_threshold, kelly_fraction, starting_bankroll,
            config_json, equity_curve_json, trades_json,
            sensitivity_json, monte_carlo_json, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        version, label, datetime.now(timezone.utc).isoformat(), engine, data_source,
        result.get("total_markets", 0), result.get("qualifying_markets", 0),
        result.get("winning_trades", 0), result.get("losing_trades", 0),
        result.get("win_rate", 0), result.get("total_pnl", 0),
        result.get("return_pct", 0), result.get("final_bankroll", 0),
        result.get("max_drawdown", 0), result.get("sharpe_ratio", 0),
        result.get("avg_margin_c", 0), result.get("min_margin_c", 0),
        result.get("false_positives", 0), result.get("false_negatives", 0),
        result.get("confidence_threshold", 0.99), result.get("kelly_fraction", 0.25),
        result.get("starting_bankroll", 25.0),
        json.dumps(config or {}), json.dumps(result.get("equity_curve", [])),
        json.dumps(result.get("trades", [])),
        json.dumps(result.get("sensitivity", {})),
        json.dumps(result.get("monte_carlo_summary", {})), notes
    ))
    run_id = cur.lastrowid
    conn.commit()
    conn.close()
    log.info(f"Saved backtest run v{version} (id={run_id})")
    return run_id


def save_market_details(run_id: int, details: list[dict]):
    """Save per-market details for a backtest run."""
    conn = sqlite3.connect(str(BACKTEST_DB))
    cur = conn.cursor()
    for d in details:
        cur.execute("""
            INSERT INTO backtest_market_details (
                run_id, market_question, city, threshold_c,
                forecast_temp, actual_temp, margin_c,
                nws_confidence_modeled, nws_confidence_empirical,
                confidence_used, entry_price, position_size,
                pnl, bankroll_after, won, forecast_source, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run_id, d.get("question", ""), d.get("city", ""),
            d.get("threshold_c", 0), d.get("forecast_temp"),
            d.get("actual_temp"), d.get("margin_c", 0),
            d.get("nws_conf_modeled"), d.get("nws_conf_empirical"),
            d.get("confidence_used"), d.get("entry_price"),
            d.get("position_size"), d.get("pnl"),
            d.get("bankroll_after"), 1 if d.get("won") else 0,
            d.get("forecast_source", "open-meteo"),
            json.dumps(d, default=str)[:5000]
        ))
    conn.commit()
    conn.close()


def list_backtest_runs() -> list[dict]:
    """List all backtest runs."""
    conn = sqlite3.connect(str(BACKTEST_DB))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT id, version, label, timestamp, engine, data_source,
            total_markets, qualifying_markets, winning_trades, losing_trades,
            win_rate, total_pnl, return_pct, final_bankroll, max_drawdown,
            sharpe_ratio, avg_margin_c, false_positives, false_negatives,
            confidence_threshold, notes
        FROM backtest_runs ORDER BY timestamp DESC
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_backtest_run(run_id: int) -> dict | None:
    """Get full backtest run details."""
    conn = sqlite3.connect(str(BACKTEST_DB))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM backtest_runs WHERE id = ?", (run_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return None
    result = dict(row)
    # Parse JSON fields
    for key in ["equity_curve_json", "trades_json", "sensitivity_json",
                "monte_carlo_json", "config_json"]:
        if result.get(key):
            try:
                result[key.replace("_json", "")] = json.loads(result[key])
            except:
                result[key.replace("_json", "")] = None
    # Get market details
    cur.execute("SELECT * FROM backtest_market_details WHERE run_id = ?", (run_id,))
    result["market_details"] = [dict(r) for r in cur.fetchall()]
    conn.close()
    return result


# ─── Kelly criterion ───
def kelly_size(bankroll: float, win_prob: float, entry_price: float,
               kelly_fraction: float = 0.25) -> float:
    b = (1 - entry_price) / entry_price if entry_price > 0 else 0
    p = win_prob
    q = 1 - p
    f = (b * p - q) / b if b > 0 else 0
    f = max(0, f)
    return bankroll * f * kelly_fraction


# ─── V3 Backtest: Open-Meteo Forecast-Based ───
def run_v3_backtest(markets: list[dict], confidence_threshold: float = 0.99,
                    starting_bankroll: float = 25.0, kelly_fraction: float = 0.25,
                    lookback_days: int = 90) -> dict:
    """
    Run backtest with Open-Meteo actual forecast data.
    For each market:
    1. Parse city, threshold, date
    2. Fetch historical forecasts + observations for that city (lookback period)
    3. Compute empirical forecast error distribution
    4. Calculate true confidence from error distribution + margin
    5. Compare with modeled confidence (Gaussian CDF)
    6. Execute trade based on empirical confidence
    """
    bankroll = starting_bankroll
    max_bankroll = bankroll
    max_drawdown = 0.0
    equity_curve = [bankroll]
    trades = []
    market_details = []
    false_positives = 0
    false_negatives = 0

    # Cache forecast data per city
    city_cache = {}

    for i, m in enumerate(markets):
        question = m.get("question", "")
        parsed = parse_market(question)

        if not parsed:
            log.warning(f"Could not parse: {question[:60]}")
            continue

        city = parsed["city"]
        threshold_c = parsed["threshold_c"]
        direction = parsed["direction"]
        is_exact = parsed.get("is_exact_temp", False)

        # Propagate to market dict for downstream logic
        m["is_exact_temp"] = is_exact

        # Get coordinates
        coords = geocode_city(city)
        if not coords:
            log.warning(f"City not in geocode: {city}")
            continue

        lat, lon = coords

        # Fetch forecast vs actual for this city (with caching)
        cache_key = city.lower()
        if cache_key not in city_cache:
            # Use past 90 days for error distribution
            # For active markets (future dates), use recent past data
            from datetime import timedelta
            end_dt = datetime.now(timezone.utc) - timedelta(days=1)
            start_dt = end_dt - timedelta(days=lookback_days)
            start_str = start_dt.strftime("%Y-%m-%d")
            end_str = end_dt.strftime("%Y-%m-%d")

            log.info(f"Fetching forecast data for {city} ({start_str} to {end_str})")
            fc_data = fetch_forecast_vs_actual(lat, lon, start_str, end_str)
            city_cache[cache_key] = fc_data
            sleep(0.5)  # Rate limit

        fc_data = city_cache.get(cache_key)
        if not fc_data:
            log.warning(f"No forecast data for {city}")
            continue

        # Get forecast for the target date (or most recent forecast)
        # For markets about future dates, use the CURRENT forecast
        forecast_temp = m.get("forecast_temp")  # If pre-fetched
        actual_temp = m.get("actual_temp")  # If resolved

        # If not pre-fetched, use the latest forecast value
        if forecast_temp is None:
            # Use the last GFS forecast value as current forecast
            gfs_temps = fc_data.get("forecast_max_gfs", [])
            if gfs_temps:
                # Get the most recent non-None forecast
                for t in reversed(gfs_temps):
                    if t is not None:
                        forecast_temp = t
                        break
            if forecast_temp is None:
                forecast_temp = threshold_c  # Fallback

        margin_c = forecast_temp - threshold_c

        # Compute BOTH confidences
        # Modeled (old way): Gaussian CDF from margin
        nws_conf_modeled = _gaussian_confidence(margin_c)

        # Empirical (new way): from actual forecast errors
        errors_gfs = fc_data.get("forecast_errors_gfs", [])
        errors_ecmwf = fc_data.get("forecast_errors_ecmwf", [])
        # Use the better model (ECMWF if available, else GFS)
        errors = errors_ecmwf if len(errors_ecmwf) >= len(errors_gfs) else errors_gfs
        nws_conf_empirical = compute_empirical_confidence(errors, threshold_c, margin_c)

        # Use empirical confidence for trading decision
        nws_conf = nws_conf_empirical

        # Determine market type and our side
        is_exact = m.get("is_exact_temp", False)
        
        if is_exact:
            # EXACT market: YES = temp is exactly threshold, NO = temp ≠ threshold
            # We ALWAYS buy NO for exact markets (P(exact) ≈ 0)
            our_side_yes = False  # We buy NO
            if actual_temp is not None:
                actual_yes = abs(actual_temp - threshold_c) < 0.5  # within 0.5°C = exact match
            else:
                actual_yes = m.get("actual_won_yes", m.get("actual_yes", None))
        else:
            # THRESHOLD market: YES = temp ≥ threshold (above) or ≤ threshold (below)
            if direction == "above":
                if actual_temp is not None:
                    actual_yes = actual_temp >= threshold_c
                else:
                    actual_yes = m.get("actual_won_yes", m.get("actual_yes", None))
                # If confident above, buy YES; if confident below, buy NO
                our_side_yes = margin_c > 0  # forecast > threshold → buy YES
            else:
                if actual_temp is not None:
                    actual_yes = actual_temp <= threshold_c
                else:
                    actual_yes = m.get("actual_won_yes", m.get("actual_yes", None))
                our_side_yes = margin_c < 0  # forecast < threshold → buy YES
        
        if actual_yes is None:
            if nws_conf < confidence_threshold:
                false_negatives += 1
            continue
        
        our_side_won = (our_side_yes and actual_yes) or (not our_side_yes and not actual_yes)

        # Filter by confidence
        if nws_conf < confidence_threshold:
            if our_side_won:
                false_negatives += 1
            continue

        # SAFETY: Hard margin floor — don't trade if margin too thin
        # Even 99% confidence is unreliable for <2°C margin due to forecast model error
        if abs(margin_c) < 2.0:
            if our_side_won:
                false_negatives += 1
            continue

        # Entry price — market-realistic based on live Polymarket data
        # Live data shows: exact-temp NO shares trade at $0.82-0.994 (avg ~0.87)
        # Threshold market shares priced by market confidence (shrunk toward 0.5)
        if is_exact:
            abs_margin = abs(margin_c)
            if abs_margin > 5:
                no_price = 0.94   # Far from threshold → market prices NO high
            elif abs_margin > 3:
                no_price = 0.90
            else:
                no_price = 0.85   # Closer → more uncertainty → cheaper NO
            entry_price = no_price  # Always buying NO for exact-temp
        else:
            # Threshold: compute market-implied YES price from margin direction
            # Use pure-Python Gaussian CDF (scipy BLAS corrupted on this VM)
            import math as _math
            def _norm_cdf(x):
                return 0.5 * (1.0 + _math.erf(x / _math.sqrt(2.0)))
            if direction == "above":
                # YES wins if temp >= threshold → P = Φ(margin/σ)
                p_yes = _norm_cdf(margin_c / 3.0)  # σ=3°C from forecast errors
            else:
                # YES wins if temp <= threshold → P = 1 - Φ(margin/σ)
                p_yes = 1.0 - _norm_cdf(margin_c / 3.0)
            # Shrink toward 0.5 to model market inefficiency
            market_yes = 0.5 + (p_yes - 0.5) * 0.6
            if our_side_yes:
                entry_price = max(0.05, market_yes)
            else:
                entry_price = max(0.05, 1.0 - market_yes)
        entry_price = max(0.05, min(0.96, entry_price))

        # Position size
        position_size = kelly_size(bankroll, nws_conf, entry_price, kelly_fraction)
        position_size = max(0.10, min(bankroll * 0.25, position_size))

        shares = position_size / entry_price

        # P&L
        if our_side_won:
            pnl = shares * (1.0 - entry_price)
        else:
            pnl = -position_size
            false_positives += 1

        bankroll += pnl
        bankroll = max(0.01, bankroll)

        if bankroll > max_bankroll:
            max_bankroll = bankroll
        dd = (max_bankroll - bankroll) / max_bankroll if max_bankroll > 0 else 0
        max_drawdown = max(max_drawdown, dd)

        trade = {
            "day": i,
            "question": question[:80],
            "city": city,
            "threshold_c": threshold_c,
            "margin_c": round(margin_c, 2),
            "nws_conf_modeled": round(nws_conf_modeled, 4),
            "nws_conf_empirical": round(nws_conf_empirical, 4),
            "confidence_used": round(nws_conf, 4),
            "entry_price": round(entry_price, 4),
            "position_size": round(position_size, 4),
            "pnl": round(pnl, 4),
            "bankroll_after": round(bankroll, 2),
            "won": our_side_won,
            "forecast_temp": round(forecast_temp, 1),
            "actual_temp": round(actual_temp, 1) if actual_temp else None,
        }
        trades.append(trade)
        equity_curve.append(round(bankroll, 2))

        if bankroll < 0.01:
            break

    # Compile results
    wins = [t for t in trades if t["won"]]
    losses = [t for t in trades if not t["won"]]
    pnls = [t["pnl"] for t in trades]
    avg_margin = statistics.mean([t["margin_c"] for t in trades]) if trades else 0
    min_margin = min([t["margin_c"] for t in trades]) if trades else 0

    if len(pnls) > 1:
        std = statistics.stdev(pnls)
        sharpe = (statistics.mean(pnls) / std) * (252 ** 0.5) if std > 0 else 0
    else:
        sharpe = 0

    return {
        "total_markets": len(markets),
        "confidence_threshold": confidence_threshold,
        "qualifying_markets": len(trades),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate": len(wins) / len(trades) if trades else 0,
        "total_pnl": round(sum(pnls), 2),
        "return_pct": round((bankroll - starting_bankroll) / starting_bankroll, 4) if starting_bankroll > 0 else 0,
        "final_bankroll": round(bankroll, 2),
        "max_drawdown": round(max_drawdown, 4),
        "sharpe_ratio": round(sharpe, 2),
        "avg_margin_c": round(avg_margin, 2),
        "min_margin_c": round(min_margin, 2),
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "equity_curve": equity_curve,
        "trades": trades,
        "kelly_fraction": kelly_fraction,
        "starting_bankroll": starting_bankroll,
    }


# ─── V2 Backtest: Original Modeled Confidence ───
def run_v2_backtest(markets: list[dict], confidence_threshold: float = 0.99,
                    starting_bankroll: float = 25.0, kelly_fraction: float = 0.25) -> dict:
    """Run backtest with modeled NWS confidence (Gaussian CDF). Same logic as v2."""
    bankroll = starting_bankroll
    max_bankroll = bankroll
    max_drawdown = 0.0
    equity_curve = [bankroll]
    trades = []
    false_positives = 0
    false_negatives = 0

    for i, m in enumerate(markets):
        question = m.get("question", "")
        margin_c = abs(m.get("margin_c", 0))
        actual_margin = m.get("margin_c", 0)
        nws_conf = _gaussian_confidence(actual_margin)

        is_exact = m.get("is_exact_temp", False)
        actual_temp = m.get("actual_temp")
        threshold_c = m.get("threshold_c", 0)
        direction = m.get("direction", "above")

        # Determine outcome and our side (same logic as V3)
        if is_exact:
            our_side_yes = False
            if actual_temp is not None:
                actual_yes = abs(actual_temp - threshold_c) < 0.5
            else:
                actual_yes = m.get("actual_won_yes", m.get("actual_yes", None))
        else:
            if direction == "above":
                if actual_temp is not None:
                    actual_yes = actual_temp >= threshold_c
                else:
                    actual_yes = m.get("actual_won_yes", m.get("actual_yes", None))
                our_side_yes = actual_margin > 0
            else:
                if actual_temp is not None:
                    actual_yes = actual_temp <= threshold_c
                else:
                    actual_yes = m.get("actual_won_yes", m.get("actual_yes", None))
                our_side_yes = actual_margin < 0

        if actual_yes is None:
            if nws_conf < confidence_threshold:
                false_negatives += 1
            continue

        our_side_won = (our_side_yes and actual_yes) or (not our_side_yes and not actual_yes)

        if nws_conf < confidence_threshold:
            if our_side_won:
                false_negatives += 1
            continue

        # Entry price — market-realistic (same model as V3)
        is_exact_v2 = m.get("is_exact_temp", False)
        if is_exact_v2:
            abs_margin_v2 = abs(actual_margin)
            if abs_margin_v2 > 5:
                entry_price = 0.94
            elif abs_margin_v2 > 3:
                entry_price = 0.90
            else:
                entry_price = 0.85
        else:
            # Threshold: compute market-implied YES price from margin (same as V3)
            import math as _math2
            def _norm_cdf2(x):
                return 0.5 * (1.0 + _math2.erf(x / _math2.sqrt(2.0)))
            if direction == "above":
                p_yes_v2 = _norm_cdf2(actual_margin / 3.0)
            else:
                p_yes_v2 = 1.0 - _norm_cdf2(actual_margin / 3.0)
            market_yes_v2 = 0.5 + (p_yes_v2 - 0.5) * 0.6
            if our_side_yes:
                entry_price = max(0.05, market_yes_v2)
            else:
                entry_price = max(0.05, 1.0 - market_yes_v2)
        entry_price = max(0.05, min(0.96, entry_price))

        position_size = kelly_size(bankroll, nws_conf, entry_price, kelly_fraction)
        position_size = max(0.10, min(bankroll * 0.25, position_size))
        shares = position_size / entry_price

        if our_side_won:
            pnl = shares * (1.0 - entry_price)
        else:
            pnl = -position_size
            false_positives += 1

        bankroll += pnl
        bankroll = max(0.01, bankroll)

        if bankroll > max_bankroll:
            max_bankroll = bankroll
        dd = (max_bankroll - bankroll) / max_bankroll if max_bankroll > 0 else 0
        max_drawdown = max(max_drawdown, dd)

        trades.append({
            "day": i,
            "question": m.get("question", "")[:80],
            "margin_c": round(margin_c, 2),
            "nws_conf_modeled": round(nws_conf, 4),
            "nws_conf_empirical": None,
            "confidence_used": round(nws_conf, 4),
            "entry_price": round(entry_price, 4),
            "position_size": round(position_size, 4),
            "pnl": round(pnl, 4),
            "bankroll_after": round(bankroll, 2),
            "won": our_side_won,
        })
        equity_curve.append(round(bankroll, 2))

        if bankroll < 0.01:
            break

    wins = [t for t in trades if t["won"]]
    losses = [t for t in trades if not t["won"]]
    pnls = [t["pnl"] for t in trades]
    avg_margin = statistics.mean([t["margin_c"] for t in trades]) if trades else 0
    min_margin = min([t["margin_c"] for t in trades]) if trades else 0

    if len(pnls) > 1:
        std = statistics.stdev(pnls)
        sharpe = (statistics.mean(pnls) / std) * (252 ** 0.5) if std > 0 else 0
    else:
        sharpe = 0

    return {
        "total_markets": len(markets),
        "confidence_threshold": confidence_threshold,
        "qualifying_markets": len(trades),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate": len(wins) / len(trades) if trades else 0,
        "total_pnl": round(sum(pnls), 2),
        "return_pct": round((bankroll - starting_bankroll) / starting_bankroll, 4) if starting_bankroll > 0 else 0,
        "final_bankroll": round(bankroll, 2),
        "max_drawdown": round(max_drawdown, 4),
        "sharpe_ratio": round(sharpe, 2),
        "avg_margin_c": round(avg_margin, 2),
        "min_margin_c": round(min_margin, 2),
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "equity_curve": equity_curve,
        "trades": trades,
        "kelly_fraction": kelly_fraction,
        "starting_bankroll": starting_bankroll,
    }


# ─── Sensitivity analysis ───
def run_sensitivity(markets: list[dict], engine: str = "v3") -> dict:
    results = {}
    for threshold in [0.80, 0.85, 0.90, 0.95, 0.97, 0.99, 0.995, 0.999]:
        if engine == "v3":
            r = run_v3_backtest(markets, confidence_threshold=threshold)
        else:
            r = run_v2_backtest(markets, confidence_threshold=threshold)
        results[f"conf_{threshold:.3f}"] = {
            "threshold": threshold,
            "qualifying": r["qualifying_markets"],
            "wins": r["winning_trades"],
            "losses": r["losing_trades"],
            "win_rate": round(r["win_rate"], 4),
            "total_pnl": r["total_pnl"],
            "return_pct": r["return_pct"],
            "max_drawdown": r["max_drawdown"],
            "sharpe": r["sharpe_ratio"],
            "final_bankroll": r["final_bankroll"],
            "avg_margin": r["avg_margin_c"],
            "false_positives": r["false_positives"],
            "false_negatives": r["false_negatives"],
        }
    return results


if __name__ == "__main__":
    init_backtest_db()
    print("Backtest v3 engine ready. Use run_v3_backtest() or run_v2_backtest().")
    print(f"Backtest log DB at {BACKTEST_DB}")
