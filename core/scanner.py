"""
Cold Math Weather Bot — Polymarket Scanner
Discovers, filters, and enriches weather/near-certain markets from Polymarket APIs.
"""
import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional
from functools import lru_cache

import aiohttp

from core.config import ColdMathConfig
from core.engine import MarketCandidate

logger = logging.getLogger("coldmath.scanner")


class PolymarketScanner:
    """
    Scans Polymarket Gamma + CLOB APIs for qualifying weather/near-certain markets.
    Reads orderbook depth for liquidity assessment.
    """
    
    def __init__(self, config: ColdMathConfig):
        self.cfg = config
        self._session: Optional[aiohttp.ClientSession] = None
    
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            )
        return self._session
    
    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
    
    async def _get(self, url: str, params: Optional[dict] = None) -> Optional[dict]:
        session = await self._get_session()
        try:
            async with session.get(url, params=params) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.warning(f"Polymarket API {resp.status}: {url}")
                return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error(f"Polymarket API error: {e}")
            return None
    
    async def scan_weather_markets(self) -> list[MarketCandidate]:
        """Find all weather-related markets on Polymarket."""
        all_markets = []
        
        # Search Gamma API for weather markets
        for keyword in self.cfg.weather_keywords:
            markets = await self._search_gamma(keyword, limit=50)
            if markets:
                all_markets.extend(markets)
            await asyncio.sleep(0.5)  # Rate limit
        
        # Search for near-certain markets
        for keyword in self.cfg.near_certain_keywords:
            markets = await self._search_gamma(keyword, limit=20)
            if markets:
                all_markets.extend(markets)
            await asyncio.sleep(0.5)
        
        # Deduplicate by market ID
        seen = set()
        unique = []
        for m in all_markets:
            if m.market_id not in seen:
                seen.add(m.market_id)
                unique.append(m)
        
        logger.info(f"Found {len(unique)} unique candidate markets "
                    f"(from {len(all_markets)} total)")
        
        # Enrich with orderbook data
        enriched = []
        for market in unique:
            ob = await self._get_orderbook_depth(market.token_id)
            if ob:
                market.liquidity = ob
            enriched.append(market)
            await asyncio.sleep(0.3)
        
        return enriched
    
    async def _search_gamma(self, keyword: str, limit: int = 50) -> list[MarketCandidate]:
        """Search Polymarket Gamma API for markets matching a keyword."""
        url = f"{self.cfg.gamma_api_base}/markets"
        params = {
            "closed": "false",
            "limit": str(limit),
            "order": "liquidity",
            "ascending": "false",
        }
        
        data = await self._get(url, params)
        if not data:
            return []
        
        candidates = []
        items = data if isinstance(data, list) else data.get("data", [])
        
        for item in items:
            try:
                question = item.get("question", "")
                if not self._is_weather_related(question, keyword):
                    continue
                
                # Parse outcomes/tokens
                tokens = item.get("clobTokenIds", [])
                outcomes = item.get("outcomes", [])
                outcomes_prices = item.get("outcomePrices", [])
                
                if not tokens or not outcomes:
                    continue
                
                # We want to buy the side that our model says will win
                # Typically for weather: buy YES if we're confident, or NO if contrarian
                for i, outcome in enumerate(outcomes):
                    price_str = outcomes_prices[i] if i < len(outcomes_prices) else "0.5"
                    try:
                        price = float(price_str)
                    except (ValueError, TypeError):
                        price = 0.5
                    
                    # We're interested in outcomes priced 88-96¢ (our sweet spot)
                    if not (self.cfg.min_entry_price <= price <= self.cfg.max_entry_price):
                        continue
                    
                    token_id = tokens[i] if i < len(tokens) else ""
                    condition_id = item.get("conditionId", "")
                    
                    end_date = item.get("endDate", item.get("end_date_iso", ""))
                    if not end_date:
                        # Try to infer from question or skip
                        end_date = (datetime.now(timezone.utc) + 
                                   __import__('datetime').timedelta(hours=24)).isoformat()
                    
                    candidate = MarketCandidate(
                        market_id=item.get("id", ""),
                        question=question,
                        outcome=outcome,
                        price=price,
                        volume=float(item.get("volume", 0) or 0),
                        liquidity=0.0,  # Will be enriched later
                        end_date=end_date,
                        condition_id=condition_id,
                        token_id=token_id,
                        category="weather" if any(kw in question.lower() 
                                  for kw in self.cfg.weather_keywords) else "near_certain"
                    )
                    candidates.append(candidate)
                    
            except Exception as e:
                logger.debug(f"Error parsing market: {e}")
                continue
        
        return candidates
    
    def _is_weather_related(self, question: str, keyword: str) -> bool:
        """Check if a market question is related to our target keywords."""
        q_lower = question.lower()
        return keyword.lower() in q_lower
    
    async def _get_orderbook_depth(self, token_id: str) -> float:
        """Get available liquidity at best price from CLOB orderbook."""
        if not token_id:
            return 0.0
        
        url = f"{self.cfg.clob_api_base}/book"
        params = {"token_id": token_id}
        
        data = await self._get(url, params)
        if not data:
            return 0.0
        
        # Sum up the best bids/asks to find available liquidity
        # We're buying, so we look at the ask side (what we can buy)
        asks = data.get("asks", [])
        total_liquidity = 0.0
        
        for ask in asks[:10]:  # Top 10 levels
            try:
                price = float(ask.get("price", 1.0))
                size = float(ask.get("size", 0.0))
                total_liquidity += price * size
            except (ValueError, TypeError):
                continue
        
        return total_liquidity
    
    async def get_market_details(self, market_id: str) -> Optional[dict]:
        """Get full market details from Gamma API."""
        url = f"{self.cfg.gamma_api_base}/markets/{market_id}"
        return await self._get(url)
    
    async def get_event_markets(self, event_slug: str) -> list[dict]:
        """Get all markets under an event (e.g., 'will-weather-nyc')."""
        url = f"{self.cfg.gamma_api_base}/events/slug/{event_slug}"
        data = await self._get(url)
        if data:
            return data.get("markets", [])
        return []


class PolymarketScannerSync:
    """Synchronous wrapper for the scanner."""
    
    def __init__(self, config: ColdMathConfig):
        self.cfg = config
    
    def scan(self) -> list[MarketCandidate]:
        """Scan for markets synchronously."""
        async def _inner():
            scanner = PolymarketScanner(self.cfg)
            try:
                return await scanner.scan_weather_markets()
            finally:
                await scanner.close()
        
        try:
            return asyncio.run(_inner())
        except RuntimeError:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, _inner())
                return future.result(timeout=120)


# ─── Market Data from Archive ───

class ArchiveDataLoader:
    """
    Loads our existing Polymarket archive data for backtesting.
    Reads from /config/wiki/polymarket_archive/
    """
    
    def __init__(self, archive_dir: str = "/config/wiki/polymarket_archive"):
        self.archive_dir = archive_dir
        self.weather_keywords = ColdMathConfig().weather_keywords
    
    def load_price_snapshots(self) -> list[dict]:
        """Load all price snapshots from gzipped JSON files."""
        import gzip
        import json
        from pathlib import Path
        
        snapshots = []
        archive_path = Path(self.archive_dir)
        
        if not archive_path.exists():
            logger.warning(f"Archive directory not found: {self.archive_dir}")
            return snapshots
        
        for gz_file in sorted(archive_path.glob("prices_*.json.gz")):
            try:
                with gzip.open(gz_file, "rt") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        snapshots.extend(data)
                    elif isinstance(data, dict):
                        # Single snapshot format
                        ts = gz_file.stem.replace("prices_", "").replace(".json", "")
                        data["_snapshot_timestamp"] = ts
                        snapshots.append(data)
            except Exception as e:
                logger.debug(f"Error loading {gz_file}: {e}")
        
        logger.info(f"Loaded {len(snapshots)} price snapshots from archive")
        return snapshots
    
    def load_updown_data(self) -> list[dict]:
        """Load up/down market data from archive."""
        import gzip
        import json
        from pathlib import Path
        
        snapshots = []
        archive_path = Path(self.archive_dir)
        
        updown_dir = archive_path / "updown"
        if not updown_dir.exists():
            # Check flat files
            for gz_file in sorted(archive_path.glob("updown_*.json.gz")):
                try:
                    with gzip.open(gz_file, "rt") as f:
                        data = json.load(f)
                        ts = gz_file.stem.replace("updown_", "").replace(".json", "")
                        data["_snapshot_timestamp"] = ts
                        snapshots.append(data)
                except Exception as e:
                    logger.debug(f"Error loading {gz_file}: {e}")
        else:
            for gz_file in sorted(updown_dir.glob("*.json.gz")):
                try:
                    with gzip.open(gz_file, "rt") as f:
                        data = json.load(f)
                        ts = gz_file.stem.replace("updown_", "").replace(".json", "")
                        data["_snapshot_timestamp"] = ts
                        snapshots.append(data)
                except Exception as e:
                    logger.debug(f"Error loading {gz_file}: {e}")
        
        logger.info(f"Loaded {len(snapshots)} up/down snapshots from archive")
        return snapshots
    
    def convert_to_candidates(self, snapshots: list[dict]) -> list[MarketCandidate]:
        """Convert archive snapshot data into MarketCandidate objects."""
        candidates = []
        
        for snap in snapshots:
            markets = snap.get("markets", snap.get("data", []))
            if isinstance(markets, list):
                for m in markets:
                    try:
                        question = m.get("question", "")
                        outcomes = m.get("outcomes", [])
                        tokens = m.get("clobTokenIds", m.get("tokens", []))
                        prices = m.get("outcomePrices", [])
                        
                        for i, outcome in enumerate(outcomes):
                            price = float(prices[i]) if i < len(prices) else 0.5
                            token_id = tokens[i] if i < len(tokens) else ""
                            if isinstance(token_id, dict):
                                token_id = token_id.get("token_id", "")
                            
                            candidates.append(MarketCandidate(
                                market_id=m.get("id", m.get("conditionId", "")),
                                question=question,
                                outcome=outcome,
                                price=price,
                                volume=float(m.get("volume", 0) or 0),
                                liquidity=float(m.get("liquidity", 0) or 0),
                                end_date=m.get("endDate", m.get("end_date_iso", 
                                           snap.get("_snapshot_timestamp", ""))),
                                condition_id=m.get("conditionId", ""),
                                token_id=token_id,
                                category="weather" if any(kw in question.lower() 
                                          for kw in self.weather_keywords) else "other"
                            ))
                    except Exception as e:
                        logger.debug(f"Error converting market: {e}")
                        continue
        
        return candidates
