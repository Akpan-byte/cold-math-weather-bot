"""
❄️ Cold Math Weather Bot — Global Weather Integration
Universal weather client using Open-Meteo API for global coverage.
Computes confidence scores for Polymarket weather contracts (ranges and thresholds).
"""
import asyncio
import logging
import re
import time
import math
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp

from core.engine import NWSForecast
from core.config import ColdMathConfig

logger = logging.getLogger("coldmath.nws")


class NWSClient:
    """
    Async client for Open-Meteo Global Weather API.
    Provides free, public, global weather forecasts. No API key required.
    """
    
    def __init__(self, config: ColdMathConfig):
        self.cfg = config
        self.base_url = "https://api.open-meteo.com/v1/forecast"
        self.user_agent = config.nws_user_agent
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: dict = {}
        self._cache_timestamps: dict = {}
    
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": self.user_agent},
                timeout=aiohttp.ClientTimeout(total=15)
            )
        return self._session
    
    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
    
    def _is_cached(self, key: str) -> bool:
        if key not in self._cache_timestamps:
            return False
        age = time.time() - self._cache_timestamps[key]
        return age < self.cfg.nws_cache_ttl
    
    async def _get(self, url: str) -> Optional[dict]:
        """GET request with caching and error handling."""
        cache_key = url
        if self._is_cached(cache_key):
            return self._cache[cache_key]
        
        session = await self._get_session()
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self._cache[cache_key] = data
                    self._cache_timestamps[cache_key] = time.time()
                    return data
                else:
                    logger.warning(f"Open-Meteo API {resp.status} for {url}")
                    return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error(f"Open-Meteo API error: {e}")
            return None
    
    async def get_forecast_for_location(self, lat: float, lon: float) -> Optional[dict]:
        """Get 7-day daily temperature forecast from Open-Meteo in Fahrenheit."""
        url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat:.4f}&longitude={lon:.4f}"
            f"&daily=temperature_2m_max,temperature_2m_min"
            f"&temperature_unit=fahrenheit"
            f"&timezone=GMT&forecast_days=7"
        )
        return await self._get(url)
    
    async def get_observation(self, station_id: str) -> Optional[dict]:
        """Mock observation endpoint (backward compatibility)."""
        return {"properties": {"temperature": {"value": 20.0}}}


class NWSConfidenceScorer:
    """
    Computes confidence score (0-1) for a weather forecast matching Polymarket criteria.
    Uses interpolated RMSE by lead time to run Gaussian cumulative distribution scaling.
    """
    
    # Baseline weather model accuracy by lead time (lead hours → baseline probability weight)
    ACCURACY_BY_HOURS = {
        1: 0.999, 3: 0.998, 6: 0.995, 12: 0.99, 24: 0.97,
        48: 0.93, 72: 0.88, 96: 0.82, 120: 0.75, 168: 0.65
    }
    
    # Temperature forecast standard error / RMSE by lead time (°F)
    TEMP_RMSE_BY_HOURS = {
        1: 1.0, 3: 1.5, 6: 2.0, 12: 2.5, 24: 3.0,
        48: 4.0, 72: 5.0, 96: 6.0, 120: 7.0, 168: 8.0
    }
    
    @classmethod
    def _interpolate_accuracy(cls, hours: float) -> float:
        hours = max(1.0, min(168.0, hours))
        hours_key = min(cls.ACCURACY_BY_HOURS.keys(), key=lambda x: abs(x - hours))
        return cls.ACCURACY_BY_HOURS[hours_key]
    
    @classmethod
    def _interpolate_rmse(cls, hours: float) -> float:
        hours = max(1.0, min(168.0, hours))
        hours_key = min(cls.TEMP_RMSE_BY_HOURS.keys(), key=lambda x: abs(x - hours))
        return cls.TEMP_RMSE_BY_HOURS[hours_key]
    
    @classmethod
    def score_temperature_market(cls, forecast_high: Optional[float],
                                  forecast_low: Optional[float],
                                  threshold_temp: float,
                                  comparison: str,  # "above", "below", "range"
                                  hours_to_resolution: float,
                                  current_temp: Optional[float] = None,
                                  range_low: Optional[float] = None,
                                  range_high: Optional[float] = None) -> float:
        """
        Score confidence for a temperature contract using a Gaussian CDF error curve.
        Supports thresholds (above/below) and narrow range-bound options (range).
        All inputs must be in Fahrenheit.
        """
        base_accuracy = cls._interpolate_accuracy(hours_to_resolution)
        temp_rmse = cls._interpolate_rmse(hours_to_resolution)
        
        # 1. Narrow Range-Bound bet (e.g. between 46-47°F)
        if comparison == "range":
            forecast_val = forecast_high if forecast_high is not None else current_temp
            if forecast_val is None or range_low is None or range_high is None:
                return 0.5
            
            z_high = (range_high - forecast_val) / temp_rmse
            z_low = (range_low - forecast_val) / temp_rmse
            
            cdf_high = 0.5 * (1.0 + math.erf(z_high / math.sqrt(2.0)))
            cdf_low = 0.5 * (1.0 + math.erf(z_low / math.sqrt(2.0)))
            
            prob_in_range = max(0.0, min(1.0, cdf_high - cdf_low))
            
            # Polymarket range bets are highly narrow YES ranges. 
            # We buy the NO side, so the win probability is the probability that it falls OUTSIDE the range.
            win_prob = 1.0 - prob_in_range
            return min(0.9999, win_prob * base_accuracy + (1.0 - base_accuracy) * 0.5)
        
        # 2. Exceed/Above Threshold bet
        elif comparison == "above":
            forecast_val = forecast_high if forecast_high is not None else current_temp
            if forecast_val is None:
                return 0.5
            
            margin = threshold_temp - forecast_val  # distance below threshold
            z_score = margin / temp_rmse
            confidence = 0.5 * (1.0 + math.erf(z_score / math.sqrt(2.0)))
            
            # If margin is positive: NWS says it won't exceed. Confidence is the CDF prob of staying below.
            # If margin is negative: NWS says it will exceed. We buy YES, confidence is CDF of exceeding.
            if margin < 0:
                confidence = 1.0 - confidence
                
            return min(0.9999, confidence * base_accuracy + (1.0 - base_accuracy) * 0.5)
        
        # 3. Below Threshold bet
        elif comparison == "below":
            forecast_val = forecast_low if forecast_low is not None else current_temp
            if forecast_val is None:
                return 0.5
            
            margin = forecast_val - threshold_temp  # distance above threshold
            z_score = margin / temp_rmse
            confidence = 0.5 * (1.0 + math.erf(z_score / math.sqrt(2.0)))
            
            if margin < 0:
                confidence = 1.0 - confidence
                
            return min(0.9999, confidence * base_accuracy + (1.0 - base_accuracy) * 0.5)
        
        return 0.5
    
    @classmethod
    def score_precipitation_market(cls, precip_probability: float,
                                    threshold: str,  # "rain" or "no_rain"
                                    hours_to_resolution: float) -> float:
        base_accuracy = cls._interpolate_accuracy(hours_to_resolution)
        if threshold == "no_rain":
            no_rain_prob = 1.0 - precip_probability
            return min(0.9999, no_rain_prob * base_accuracy + (1 - base_accuracy) * 0.5)
        elif threshold == "rain":
            return min(0.9999, precip_probability * base_accuracy + (1 - base_accuracy) * 0.5)
        return 0.5


class NWSForecastMatcher:
    """
    Parses weather questions and matches them against the global city coordinates.
    """
    
    # Unified 70+ Global City Registry (loaded from quant backtest engine)
    CITY_COORDS = {
        # US cities
        "new york": (40.71, -74.01), "nyc": (40.71, -74.01), "new york city": (40.71, -74.01),
        "los angeles": (34.05, -118.24), "la": (34.05, -118.24), "chicago": (41.88, -87.63), 
        "houston": (29.76, -95.37), "phoenix": (33.45, -112.07), "denver": (39.74, -104.99),
        "dallas": (32.78, -96.80), "miami": (25.76, -80.19), "atlanta": (33.75, -84.39), 
        "boston": (42.36, -71.06), "seattle": (47.61, -122.33), "san francisco": (37.77, -122.42),
        "sf": (37.77, -122.42), "washington": (38.91, -77.04), "detroit": (42.33, -83.05),
        "philadelphia": (39.95, -75.17), "philly": (39.95, -75.17), "minneapolis": (44.98, -93.27),
        "nashville": (36.16, -86.77), "portland": (45.52, -122.68), "las vegas": (36.17, -115.14), 
        "austin": (30.27, -97.74), "orlando": (28.54, -81.38), "tampa": (27.95, -82.46),
        # International
        "london": (51.51, -0.13), "paris": (48.86, 2.35), "tokyo": (35.68, 139.69), 
        "beijing": (39.90, 116.41), "shanghai": (31.23, 121.47), "seoul": (37.57, 126.98),
        "mumbai": (19.08, 72.88), "delhi": (28.61, 77.21), "cairo": (30.04, 31.24), 
        "sydney": (-33.87, 151.21), "toronto": (43.65, -79.38), "moscow": (55.76, 37.62),
        "berlin": (52.52, 13.41), "rome": (41.90, 12.50), "madrid": (40.42, -3.70), 
        "istanbul": (41.01, 28.98), "bangkok": (13.76, 100.50), "singapore": (1.35, 103.82),
        "dubai": (25.20, 55.27), "mexico city": (19.43, -99.13), "são paulo": (-23.55, -46.63), 
        "buenos aires": (-34.60, -58.38), "ankara": (39.93, 32.86), "tel aviv": (32.08, 34.78),
        "chongqing": (29.56, 106.55), "melbourne": (-37.81, 144.96), "bucharest": (44.43, 26.10), 
        "warsaw": (52.23, 21.01), "budapest": (47.50, 19.04), "vienna": (48.21, 16.37),
        "amsterdam": (52.37, 4.90), "brussels": (50.85, 4.35), "zurich": (47.38, 8.54), 
        "stockholm": (59.33, 18.07), "oslo": (59.91, 10.75), "helsinki": (60.17, 24.94),
        "copenhagen": (55.68, 12.57), "dublin": (53.35, -6.26), "lisbon": (38.72, -9.14), 
        "athens": (37.98, 23.73), "jakarta": (-6.21, 106.85), "kuala lumpur": (3.14, 101.69),
        "taipei": (25.03, 121.57), "hong kong": (22.32, 114.17), "nairobi": (-1.29, 36.82), 
        "lagos": (6.52, 3.38), "capetown": (-33.92, 18.42), "johannesburg": (-26.20, 28.05),
        "lima": (-12.05, -77.04), "bogota": (4.71, -74.07), "santiago": (-33.45, -70.67), 
        "caracas": (10.49, -66.88), "lucknow": (26.85, 80.95), "wellington": (-41.29, 174.78),
        "manila": (14.60, 120.98), "qingdao": (36.07, 120.38), "shenzhen": (22.54, 114.06), 
        "jeddah": (21.49, 39.19)
    }

    @classmethod
    def parse_market_question(cls, question: str) -> Optional[dict]:
        """
        Robustly parses a Polymarket question to extract target temperatures, ranges,
        units, and matches the correct geocoded city name.
        """
        q_clean = question.lower().strip()
        
        # 1. Detect Temperature Unit
        unit = "F" if any(x in q_clean for x in ["°f", "fahrenheit", "farenheit"]) else "C"
        
        # 2. Match Target Temperature, Ranges, and Comparisons
        target_temp = None
        comparison = "above"
        range_low = None
        range_high = None
        
        # Range match: "between 46-47" or "between 46 and 47"
        m_range = re.search(r"between\s+(\d+(?:\.\d+)?)\s*[-–\s\band\b]+\s*(\d+(?:\.\d+)?)\s*(?:°|degree|c|f)?", q_clean)
        if m_range:
            low = float(m_range.group(1))
            high = float(m_range.group(2))
            range_low = low
            range_high = high
            target_temp = (low + high) / 2
            comparison = "range"
        else:
            # Specific threshold match: e.g. "22°C" or "75 degrees"
            m_thresh = re.search(r"(\d+(?:\.\d+)?)\s*(?:°|degree|celsius|fahrenheit|\bc\b|\bf\b)", q_clean)
            if m_thresh:
                thresh = float(m_thresh.group(1))
                if thresh < 150:  # Ignore years/dates accidentally matched
                    target_temp = thresh
                    if "or higher" in q_clean or "above" in q_clean or "exceed" in q_clean or "reach" in q_clean or "hit" in q_clean:
                        comparison = "above"
                    elif "or below" in q_clean or "below" in q_clean or "drop" in q_clean:
                        comparison = "below"
                    else:
                        comparison = "above"
            
            # Fallback: extract the first number in the question < 150
            if target_temp is None:
                nums = re.findall(r"(\d+(?:\.\d+)?)", q_clean)
                if nums:
                    valid_nums = [float(n) for n in nums if float(n) < 150]
                    if valid_nums:
                        target_temp = valid_nums[0]
                        if "or higher" in q_clean or "above" in q_clean or "exceed" in q_clean or "reach" in q_clean or "hit" in q_clean:
                            comparison = "above"
                        elif "or below" in q_clean or "below" in q_clean or "drop" in q_clean:
                            comparison = "below"
                        else:
                            comparison = "above"
        
        if target_temp is None:
            return None
            
        # 3. Geocode City Name
        city_found = None
        coords = None
        
        # Word-boundary key check to avoid matching "ok" for "oklahoma"
        for city, c_coords in cls.CITY_COORDS.items():
            if re.search(r'\b' + re.escape(city) + r'\b', q_clean):
                city_found = city
                coords = c_coords
                break
                
        if not city_found:
            # Fuzzy partial fallback
            for city, c_coords in cls.CITY_COORDS.items():
                if city in q_clean:
                    city_found = city
                    coords = c_coords
                    break
                    
        if not city_found or not coords:
            return None
            
        # 4. Standardize all calculations to Fahrenheit
        thresh_f = target_temp
        if unit == "C":
            thresh_f = (target_temp * 9 / 5) + 32
            if range_low is not None:
                range_low = (range_low * 9 / 5) + 32
            if range_high is not None:
                range_high = (range_high * 9 / 5) + 32
                
        high_low = "high"
        if any(x in q_clean for x in ["lowest", "minimum", "low temp", "low of", "drop below"]):
            high_low = "low"
            
        return {
            "city": city_found,
            "lat": coords[0],
            "lon": coords[1],
            "metric": "temperature",
            "high_low": high_low,
            "comparison": comparison,
            "threshold_temp": thresh_f,
            "range_low": range_low,
            "range_high": range_high,
            "unit": "F",
            "found_coords": True,
        }

    @classmethod
    async def compute_confidence(cls, market_question: str, 
                                  end_date: str,
                                  nws_client: NWSClient) -> Optional[NWSForecast]:
        """
        Parses question → Queries Open-Meteo → Matches target dates → Returns confidence.
        """
        parsed = cls.parse_market_question(market_question)
        if not parsed:
            logger.debug(f"Cannot parse weather question: {market_question}")
            return None
        
        lat = parsed["lat"]
        lon = parsed["lon"]
        
        # Get live daily forecast from Open-Meteo (global coverage)
        forecast_data = await nws_client.get_forecast_for_location(lat, lon)
        if not forecast_data or "daily" not in forecast_data:
            logger.warning(f"Could not retrieve global forecast for {parsed['city']}")
            return None
            
        # Parse hours to resolution
        try:
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            hours_to_resolution = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
            if hours_to_resolution < 0:
                hours_to_resolution = 1.0  # Fallback to current hour
        except (ValueError, AttributeError):
            hours_to_resolution = 12.0  # Default
            
        # Extract target date (YYYY-MM-DD) from the contract's resolution end_date
        try:
            target_date = end_date.split('T')[0]
        except Exception:
            target_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            
        daily = forecast_data.get("daily", {})
        times = daily.get("time", [])
        max_temps = daily.get("temperature_2m_max", [])
        min_temps = daily.get("temperature_2m_min", [])
        
        forecast_high = None
        forecast_low = None
        
        # Match exact date corresponding to resolution
        if target_date in times:
            idx = times.index(target_date)
            forecast_high = float(max_temps[idx]) if idx < len(max_temps) and max_temps[idx] is not None else None
            forecast_low = float(min_temps[idx]) if idx < len(min_temps) and min_temps[idx] is not None else None
        else:
            # Fallback to day 1
            forecast_high = float(max_temps[0]) if max_temps and max_temps[0] is not None else None
            forecast_low = float(min_temps[0]) if min_temps and min_temps[0] is not None else None
            
        forecast_text = f"Global forecast for {parsed['city']} on {target_date}: High {forecast_high}°F / Low {forecast_low}°F"
        
        # Compute confidence based on metric
        confidence = 0.5
        if parsed["metric"] == "temperature":
            confidence = NWSConfidenceScorer.score_temperature_market(
                forecast_high=forecast_high,
                forecast_low=forecast_low,
                threshold_temp=parsed["threshold_temp"],
                comparison=parsed["comparison"],
                hours_to_resolution=hours_to_resolution,
                range_low=parsed.get("range_low"),
                range_high=parsed.get("range_high")
            )
        elif parsed["metric"] == "precipitation":
            confidence = NWSConfidenceScorer.score_precipitation_market(
                precip_probability=0.2,  # default standard probability
                threshold=parsed["threshold"],
                hours_to_resolution=hours_to_resolution
            )
            
        station_id = f"OM_{parsed['city'][:3].upper()}"
        
        return NWSForecast(
            station_id=station_id,
            latitude=lat,
            longitude=lon,
            forecast_text=forecast_text,
            high_temp_f=int(forecast_high) if forecast_high else None,
            low_temp_f=int(forecast_low) if forecast_low else None,
            precipitation_prob=0.2,
            confidence=confidence,
            issued_at=datetime.now(timezone.utc).isoformat(),
            expires_at=end_date
        )


class NWSForecastMatcherSync:
    """Synchronous wrapper for computing confidence."""
    
    def __init__(self, config: ColdMathConfig):
        self.cfg = config
        
    def compute(self, market_question: str, end_date: str) -> Optional[NWSForecast]:
        async def _inner():
            client = NWSClient(self.cfg)
            try:
                return await NWSForecastMatcher.compute_confidence(market_question, end_date, client)
            finally:
                await client.close()
                
        try:
            return asyncio.run(_inner())
        except RuntimeError:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, _inner())
                return future.result(timeout=60)
