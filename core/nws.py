"""
Cold Math Weather Bot — NWS Forecast Integration
Fetches and parses National Weather Service forecasts to compute confidence scores.
"""
import asyncio
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
from functools import lru_cache

import aiohttp

from core.engine import NWSForecast
from core.config import ColdMathConfig

logger = logging.getLogger("coldmath.nws")


class NWSClient:
    """
    Async client for the National Weather Service API.
    Free, no API key required. Rate limit: polite use (cache aggressively).
    """
    
    def __init__(self, config: ColdMathConfig):
        self.cfg = config
        self.base_url = config.nws_api_base
        self.user_agent = config.nws_user_agent
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: dict = {}
        self._cache_timestamps: dict = {}
    
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": self.user_agent},
                timeout=aiohttp.ClientTimeout(total=10)
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
                    logger.warning(f"NWS API {resp.status} for {url}")
                    return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error(f"NWS API error: {e}")
            return None
    
    async def get_points(self, lat: float, lon: float) -> Optional[dict]:
        """Get grid point metadata for a location."""
        url = f"{self.base_url}/points/{lat:.4f},{lon:.4f}"
        return await self._get(url)
    
    async def get_forecast(self, grid_id: str, grid_x: int, grid_y: int) -> Optional[dict]:
        """Get hourly forecast for a grid point."""
        url = f"{self.base_url}/gridpoints/{grid_id}/{grid_x},{grid_y}/forecast/hourly"
        return await self._get(url)
    
    async def get_forecast_for_location(self, lat: float, lon: float) -> Optional[dict]:
        """Get hourly forecast for a lat/lon (two-step: points → forecast)."""
        points = await self.get_points(lat, lon)
        if not points or "properties" not in points:
            return None
        
        props = points["properties"]
        grid_id = props.get("gridId", "")
        grid_x = props.get("gridX", 0)
        grid_y = props.get("gridY", 0)
        forecast_url = props.get("forecastHourly", "")
        
        if forecast_url:
            return await self._get(forecast_url)
        
        return await self.get_forecast(grid_id, grid_x, grid_y)
    
    async def get_observation(self, station_id: str) -> Optional[dict]:
        """Get latest observation from a weather station."""
        url = f"{self.base_url}/stations/{station_id}/observations/latest"
        return await self._get(url)
    
    async def get_stations_near(self, lat: float, lon: float, limit: int = 5) -> Optional[dict]:
        """Find weather stations near a location."""
        url = f"{self.base_url}/stations?location={lat:.4f},{lon:.4f}&limit={limit}"
        return await self._get(url)


class NWSConfidenceScorer:
    """
    Computes a confidence score (0-1) for a weather forecast matching
    a Polymarket weather market's resolution criteria.
    
    The key insight: NWS forecasts are highly accurate for near-term
    temperature ranges. If NWS says "high of 75°F" with a range of 
    ±3°F, and the market asks "Will high temp exceed 80°F?", our 
    confidence that it WON'T exceed 80°F is extremely high.
    """
    
    # NWS forecast accuracy by lead time (empirical from NWS verification data)
    ACCURACY_BY_HOURS = {
        1: 0.999,    # 1 hour ahead: near-perfect
        3: 0.998,    # 3 hours: very high
        6: 0.995,    # 6 hours: high
        12: 0.99,    # 12 hours: still very good
        24: 0.97,    # 24 hours: good
        48: 0.93,    # 48 hours: decent
        72: 0.88,    # 3 days: moderate
        96: 0.82,    # 4 days: lowering
        120: 0.75,   # 5 days: significant uncertainty
        168: 0.65,   # 7 days: weak
    }
    
    # Temperature forecast RMSE by lead time (°F)
    TEMP_RMSE_BY_HOURS = {
        1: 1.0, 3: 1.5, 6: 2.0, 12: 2.5, 24: 3.0,
        48: 4.0, 72: 5.0, 96: 6.0, 120: 7.0, 168: 8.0
    }
    
    @classmethod
    def score_temperature_market(cls, forecast_high: Optional[float],
                                  forecast_low: Optional[float],
                                  threshold_temp: float,
                                  comparison: str,  # "above" or "below"
                                  hours_to_resolution: float,
                                  current_temp: Optional[float] = None) -> float:
        """
        Score confidence for a temperature threshold market.
        
        E.g. "Will NYC high temp exceed 90°F today?"
        - forecast_high = 82, threshold = 90, comparison = "above"
        - The forecast says NO with high confidence
        """
        # Get base accuracy from lead time
        base_accuracy = cls._interpolate_accuracy(hours_to_resolution)
        temp_rmse = cls._interpolate_rmse(hours_to_resolution)
        
        # Determine which forecast value to use
        if comparison == "above":
            # Market asks "will temp exceed X?"
            forecast_val = forecast_high if forecast_high is not None else current_temp
            if forecast_val is None:
                return 0.5
            
            margin = threshold_temp - forecast_val  # How far below threshold?
            if margin > 0:
                # Forecast says it WON'T exceed — confidence depends on margin vs RMSE
                # If margin is 10°F and RMSE is 3°F, very confident
                z_score = margin / temp_rmse
                from math import erf, sqrt
                confidence = 0.5 * (1 + erf(z_score / sqrt(2)))
                # Blend with base accuracy
                return min(0.9999, confidence * base_accuracy + (1 - base_accuracy) * 0.5)
            else:
                # Forecast says it WILL exceed — we'd buy the other side
                margin_above = forecast_val - threshold_temp
                z_score = margin_above / temp_rmse
                from math import erf, sqrt
                confidence = 0.5 * (1 + erf(z_score / sqrt(2)))
                return min(0.9999, confidence * base_accuracy + (1 - base_accuracy) * 0.5)
        
        elif comparison == "below":
            forecast_val = forecast_low if forecast_low is not None else current_temp
            if forecast_val is None:
                return 0.5
            
            margin = forecast_val - threshold_temp  # How far above threshold?
            if margin > 0:
                # Forecast says it WON'T go below — confidence depends on margin
                z_score = margin / temp_rmse
                from math import erf, sqrt
                confidence = 0.5 * (1 + erf(z_score / sqrt(2)))
                return min(0.9999, confidence * base_accuracy + (1 - base_accuracy) * 0.5)
            else:
                margin_below = threshold_temp - forecast_val
                z_score = margin_below / temp_rmse
                from math import erf, sqrt
                confidence = 0.5 * (1 + erf(z_score / sqrt(2)))
                return min(0.9999, confidence * base_accuracy + (1 - base_accuracy) * 0.5)
        
        return 0.5  # Unknown comparison type
    
    @classmethod
    def score_precipitation_market(cls, precip_probability: float,
                                    threshold: str,  # "rain" or "no_rain"
                                    hours_to_resolution: float) -> float:
        """Score confidence for a precipitation market."""
        base_accuracy = cls._interpolate_accuracy(hours_to_resolution)
        
        if threshold == "no_rain":
            # We want confidence it WON'T rain
            no_rain_prob = 1.0 - precip_probability
            # NWS precip forecasts are well-calibrated
            return min(0.9999, no_rain_prob * base_accuracy + (1 - base_accuracy) * 0.5)
        elif threshold == "rain":
            return min(0.9999, precip_probability * base_accuracy + (1 - base_accuracy) * 0.5)
        
        return 0.5
    
    @classmethod
    def score_generic_market(cls, nws_text: str, market_question: str,
                              hours_to_resolution: float) -> float:
        """
        Score confidence for generic weather markets by parsing NWS text.
        Uses keyword matching and certainty indicators.
        """
        base_accuracy = cls._interpolate_accuracy(hours_to_resolution)
        
        # Certainty indicators in NWS forecasts
        high_certainty_phrases = ["certain", "definitely", "will", "confirmed",
                                   "expected", "forecast", "guaranteed"]
        moderate_certainty = ["likely", "probable", "chance", "possible"]
        low_certainty = ["uncertain", "may", "might", "could", "unlikely"]
        
        text_lower = nws_text.lower() + " " + market_question.lower()
        
        high_count = sum(1 for phrase in high_certainty_phrases if phrase in text_lower)
        low_count = sum(1 for phrase in low_certainty if phrase in text_lower)
        
        if high_count > low_count:
            return min(0.9999, 0.95 * base_accuracy)
        elif low_count > high_count:
            return min(0.9999, 0.60 * base_accuracy)
        else:
            return min(0.9999, 0.80 * base_accuracy)
    
    @classmethod
    def _interpolate_accuracy(cls, hours: float) -> float:
        """Interpolate NWS accuracy from lead time table."""
        hours_key = min(cls.ACCURACY_BY_HOURS.keys(), 
                       key=lambda x: abs(x - hours))
        return cls.ACCURACY_BY_HOURS[hours_key]
    
    @classmethod
    def _interpolate_rmse(cls, hours: float) -> float:
        """Interpolate temperature RMSE from lead time table."""
        hours_key = min(cls.TEMP_RMSE_BY_HOURS.keys(), 
                       key=lambda x: abs(x - hours))
        return cls.TEMP_RMSE_BY_HOURS[hours_key]


class NWSForecastMatcher:
    """
    Matches Polymarket weather questions to NWS forecast data.
    Parses the market question to extract: location, weather metric, threshold, time.
    """
    
    # Major US cities with NWS station coordinates
    CITY_COORDS = {
        "new york": (40.7128, -74.0060),
        "nyc": (40.7128, -74.0060),
        "los angeles": (34.0522, -118.2437),
        "la": (34.0522, -118.2437),
        "chicago": (41.8781, -87.6298),
        "houston": (29.7604, -95.3698),
        "phoenix": (33.4484, -112.0740),
        "philly": (39.9526, -75.1652),
        "philadelphia": (39.9526, -75.1652),
        "san antonio": (29.4241, -98.4936),
        "san diego": (32.7157, -117.1611),
        "dallas": (32.7767, -96.7970),
        "san jose": (37.3382, -121.8863),
        "austin": (30.2672, -97.7431),
        "jacksonville": (30.3322, -81.6557),
        "fort worth": (32.7555, -97.3308),
        "columbus": (39.9612, -82.9988),
        "charlotte": (35.2271, -80.8431),
        "san francisco": (37.7749, -122.4194),
        "sf": (37.7749, -122.4194),
        "indianapolis": (39.7684, -86.1581),
        "seattle": (47.6062, -122.3321),
        "denver": (39.7392, -104.9903),
        "washington dc": (38.9072, -77.0369),
        "dc": (38.9072, -77.0369),
        "boston": (42.3601, -71.0589),
        "el paso": (31.7619, -106.4850),
        "nashville": (36.1627, -86.7816),
        "detroit": (42.3314, -83.0458),
        "oklahoma city": (35.4676, -97.5164),
        "portland": (45.5152, -122.6784),
        "las vegas": (36.1699, -115.1398),
        "memphis": (35.1495, -90.0490),
        "louisville": (38.2527, -85.7585),
        "baltimore": (39.2904, -76.6122),
        "milwaukee": (43.0389, -87.9065),
        "albuquerque": (35.0844, -106.6504),
        "tucson": (32.2226, -110.9747),
        "fresno": (36.7378, -119.7839),
        "sacramento": (38.5816, -121.4944),
        "kansas city": (39.0997, -94.5786),
        "atlanta": (33.7490, -84.3880),
        "miami": (25.7617, -80.1918),
        "orlando": (28.5383, -81.3792),
        "tampa": (27.9506, -82.4572),
        "minneapolis": (44.9778, -93.2650),
        "honolulu": (21.3069, -157.8583),
        "omaha": (41.2565, -95.9345),
        "cincinnati": (39.1031, -84.5120),
        "pittsburgh": (40.4406, -79.9959),
        "raleigh": (35.7796, -78.6382),
        "salt lake city": (40.7608, -111.8910),
    }
    
    # Temperature pattern: "Will [city] high temp exceed 90°F?"
    TEMP_PATTERN = re.compile(
        r"(?:will\s+)?(?:the\s+)?(\w[\w\s]{1,25})\s+"
        r"(high|low)\s+(?:temp(?:erature)?\s+)?"
        r"(exceed|reach|hit|go\s+above|go\s+below|drop\s+below|stay\s+above|stay\s+below)\s+"
        r"(\d+)\s*°?\s*F?",
        re.IGNORECASE
    )
    
    # Precipitation pattern
    PRECIP_PATTERN = re.compile(
        r"(?:will\s+)?(?:the\s+)?(\w[\w\s]{1,25})\s+"
        r"(?:see|get|have|experience)\s+"
        r"(rain|snow|precipitation|thunderstorm)",
        re.IGNORECASE
    )
    
    @classmethod
    def parse_market_question(cls, question: str) -> Optional[dict]:
        """
        Parse a Polymarket weather question into structured components.
        
        Returns dict with: city, metric, comparison, threshold, hours_to_resolution
        or None if it can't parse the question.
        """
        question = question.strip()
        
        # Try temperature pattern
        temp_match = cls.TEMP_PATTERN.search(question)
        if temp_match:
            city_raw = temp_match.group(1).strip().lower()
            high_low = temp_match.group(2).lower()
            comparison_raw = temp_match.group(3).lower()
            threshold = float(temp_match.group(4))
            
            coords = cls.CITY_COORDS.get(city_raw)
            if not coords:
                # Try partial match
                for city, c in cls.CITY_COORDS.items():
                    if city in city_raw or city_raw in city:
                        coords = c
                        break
            
            comparison = "above" if comparison_raw in ("exceed", "reach", "hit", 
                                                         "go above", "stay above") else "below"
            
            return {
                "city": city_raw,
                "lat": coords[0] if coords else None,
                "lon": coords[1] if coords else None,
                "metric": "temperature",
                "high_low": high_low,
                "comparison": comparison,
                "threshold_temp": threshold,
                "found_coords": coords is not None,
            }
        
        # Try precipitation pattern
        precip_match = cls.PRECIP_PATTERN.search(question)
        if precip_match:
            city_raw = precip_match.group(1).strip().lower()
            precip_type = precip_match.group(2).lower()
            
            coords = cls.CITY_COORDS.get(city_raw)
            if not coords:
                for city, c in cls.CITY_COORDS.items():
                    if city in city_raw or city_raw in city:
                        coords = c
                        break
            
            return {
                "city": city_raw,
                "lat": coords[0] if coords else None,
                "lon": coords[1] if coords else None,
                "metric": "precipitation",
                "precip_type": precip_type,
                "threshold": "rain" if precip_type in ("rain", "thunderstorm") else "no_rain",
                "found_coords": coords is not None,
            }
        
        return None  # Can't parse this question
    
    @classmethod
    async def compute_confidence(cls, market_question: str, 
                                  end_date: str,
                                  nws_client: NWSClient) -> Optional[NWSForecast]:
        """
        Full pipeline: parse question → get NWS forecast → compute confidence.
        """
        parsed = cls.parse_market_question(market_question)
        if not parsed:
            logger.debug(f"Cannot parse weather question: {market_question}")
            return None
        
        if not parsed.get("found_coords"):
            logger.debug(f"City not in database: {parsed.get('city')}")
            return None
        
        lat = parsed["lat"]
        lon = parsed["lon"]
        
        # Get forecast
        forecast_data = await nws_client.get_forecast_for_location(lat, lon)
        if not forecast_data or "properties" not in forecast_data:
            return None
        
        # Parse hours to resolution
        try:
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            hours_to_resolution = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
            if hours_to_resolution < 0:
                return None
        except (ValueError, AttributeError):
            hours_to_resolution = 12  # Default
        
        # Extract forecast values
        periods = forecast_data["properties"].get("periods", [])
        forecast_high = None
        forecast_low = None
        precip_prob = None
        
        for period in periods[:4]:  # Next 4 periods
            if period.get("isDaytime"):
                temp = period.get("temperature")
                if temp:
                    forecast_high = float(temp)
            else:
                temp = period.get("temperature")
                if temp:
                    forecast_low = float(temp)
            
            pp = period.get("probabilityOfPrecipitation", {})
            val = pp.get("value") if isinstance(pp, dict) else pp
            if val is not None:
                precip_prob = float(val) / 100.0
        
        forecast_text = periods[0].get("detailedForecast", "") if periods else ""
        
        # Compute confidence based on market type
        confidence = 0.5
        if parsed["metric"] == "temperature":
            confidence = NWSConfidenceScorer.score_temperature_market(
                forecast_high=forecast_high,
                forecast_low=forecast_low,
                threshold_temp=parsed["threshold_temp"],
                comparison=parsed["comparison"],
                hours_to_resolution=hours_to_resolution
            )
        elif parsed["metric"] == "precipitation":
            confidence = NWSConfidenceScorer.score_precipitation_market(
                precip_probability=precip_prob or 0.5,
                threshold=parsed["threshold"],
                hours_to_resolution=hours_to_resolution
            )
        
        # Determine station_id from city
        station_id = f"K{parsed['city'][:3].upper()}"
        
        return NWSForecast(
            station_id=station_id,
            latitude=lat,
            longitude=lon,
            forecast_text=forecast_text,
            high_temp_f=int(forecast_high) if forecast_high else None,
            low_temp_f=int(forecast_low) if forecast_low else None,
            precipitation_prob=precip_prob,
            confidence=confidence,
            issued_at=datetime.now(timezone.utc).isoformat(),
            expires_at=end_date
        )


# ─── Sync wrapper for non-async contexts ───

def get_nws_forecast_sync(market_question: str, end_date: str,
                           config: ColdMathConfig) -> Optional[NWSForecast]:
    """Synchronous wrapper for NWS forecast computation."""
    async def _inner():
        client = NWSClient(config)
        try:
            return await NWSForecastMatcher.compute_confidence(
                market_question, end_date, client
            )
        finally:
            await client.close()
    
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # We're inside an existing event loop — create a new one
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, _inner())
                return future.result(timeout=30)
        else:
            return loop.run_until_complete(_inner())
    except RuntimeError:
        return asyncio.run(_inner())
