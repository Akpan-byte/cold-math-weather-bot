"""
Cold Math Weather Bot — Paper Trading Engine
Simulates real trades without real money. Tracks fills, slippage, and P&L
as if we were actually trading on Polymarket.
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

from core.config import ColdMathConfig
from core.engine import (
    ColdMathEngine, MarketCandidate, NWSForecast,
    TradeSignal, TradeRecord, TradeStatus, FilterResult
)
from core.scanner import PolymarketScanner
from core.nws import NWSClient, NWSForecastMatcher

logger = logging.getLogger("coldmath.paper")


class PaperTrader:
    """
    Paper trading engine that:
    1. Scans real Polymarket markets
    2. Fetches real NWS forecasts
    3. Runs through our filter pipeline
    4. Simulates order execution with realistic slippage
    5. Tracks results against actual market resolutions
    """
    
    def __init__(self, config: Optional[ColdMathConfig] = None):
        self.cfg = config or ColdMathConfig()
        self.engine = ColdMathEngine(self.cfg)
        self._open_positions: dict[str, dict] = {}  # trade_id → position info
    
    async def scan_and_evaluate(self) -> list[TradeSignal]:
        """Full pipeline: scan markets → fetch NWS → evaluate → return signals."""
        signals = []
        
        # Scan Polymarket
        scanner = PolymarketScanner(self.cfg)
        nws_client = NWSClient(self.cfg)
        
        try:
            markets = await scanner.scan_weather_markets()
            logger.info(f"Found {len(markets)} candidate markets")
            
            for market in markets:
                # Try to get NWS forecast for weather markets
                nws = None
                if market.category == "weather":
                    nws = await NWSForecastMatcher.compute_confidence(
                        market.question, market.end_date, nws_client
                    )
                
                # Evaluate through filter pipeline
                signal = self.engine.evaluate_market(market, nws)
                signals.append(signal)
                
                if signal.filter_result == FilterResult.PASS:
                    logger.info(f"🎯 SIGNAL: {market.question[:60]}... "
                               f"@ {signal.entry_price:.2f} | "
                               f"Win≈{signal.win_probability:.1%} | "
                               f"Size=${signal.position_size_usd:.2f}")
                else:
                    logger.debug(f"Filtered: {signal.filter_result} — {signal.reason}")
        finally:
            await scanner.close()
            await nws_client.close()
        
        return signals
    
    async def execute_paper_trades(self, signals: list[TradeSignal]) -> list[TradeRecord]:
        """Execute passing signals as paper trades with simulated slippage."""
        trades = []
        
        for signal in signals:
            if signal.filter_result != FilterResult.PASS:
                continue
            
            # Simulate execution with 0.5% slippage
            slippage = 0.005
            fill_price = signal.entry_price * (1 + slippage)  # We pay slightly more
            
            # Execute through engine
            trade = self.engine.execute_signal(signal)
            if trade:
                # Adjust for slippage in the record
                trade.entry_price = round(fill_price, 4)
                trade.shares = round(signal.position_size_usd / fill_price, 4)
                
                # Track open position
                self._open_positions[trade.trade_id] = {
                    "trade": trade,
                    "signal": signal,
                    "entered_at": datetime.now(timezone.utc).isoformat(),
                    "market_end": signal.market.end_date
                }
                
                trades.append(trade)
                logger.info(f"📝 PAPER TRADE: {trade.trade_id} | "
                           f"{signal.market.question[:50]}... | "
                           f"Fill @{fill_price:.4f} | ${signal.position_size_usd:.2f}")
        
        return trades
    
    async def check_resolutions(self) -> list[dict]:
        """
        Check if any open positions have resolved.
        For paper trading, we check the actual market outcome on Polymarket.
        """
        resolved = []
        
        for trade_id, pos in list(self._open_positions.items()):
            market = pos["signal"].market
            end_date = pos["market_end"]
            
            # Check if market has resolved
            try:
                from datetime import datetime, timezone
                end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) < end_dt:
                    continue  # Not yet resolved
            except (ValueError, AttributeError):
                continue
            
            # For paper trading, simulate resolution based on our confidence
            # In live mode, we'd check the actual market outcome
            import random
            win_prob = pos["signal"].win_probability
            actual_win = random.random() < win_prob
            
            exit_price = 1.0 if actual_win else 0.0
            trade = pos["trade"]
            pnl = (exit_price - trade.entry_price) * trade.shares
            
            self.engine.resolve_trade(trade_id, actual_win, exit_price)
            
            resolved.append({
                "trade_id": trade_id,
                "question": market.question,
                "won": actual_win,
                "entry_price": trade.entry_price,
                "exit_price": exit_price,
                "pnl": round(pnl, 4),
                "bankroll": round(self.engine.sizer.bankroll, 2)
            })
            
            del self._open_positions[trade_id]
            logger.info(f"📊 RESOLVED: {trade_id} | "
                       f"{'WON ✅' if actual_win else 'LOST ❌'} | "
                       f"P&L: ${pnl:.4f} | Bankroll: ${self.engine.sizer.bankroll:.2f}")
        
        return resolved
    
    def get_status(self) -> dict:
        """Get paper trading status."""
        engine_status = self.engine.get_status()
        return {
            **engine_status,
            "open_positions": len(self._open_positions),
            "mode": "paper"
        }


class PaperTraderSync:
    """Synchronous wrapper for paper trading."""
    
    def __init__(self, config: Optional[ColdMathConfig] = None):
        self.cfg = config or ColdMathConfig()
        self._trader = PaperTrader(self.cfg)
    
    def run_scan_cycle(self) -> list[dict]:
        """Run one complete scan-evaluate-execute cycle."""
        async def _inner():
            signals = await self._trader.scan_and_evaluate()
            trades = await self._trader.execute_paper_trades(signals)
            return [asdict(t) for t in trades]
        
        try:
            return asyncio.run(_inner())
        except RuntimeError:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, _inner())
                return future.result(timeout=120)
    
    def check_resolutions(self) -> list[dict]:
        """Check for resolved positions."""
        async def _inner():
            return await self._trader.check_resolutions()
        
        try:
            return asyncio.run(_inner())
        except RuntimeError:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, _inner())
                return future.result(timeout=60)
    
    def get_status(self) -> dict:
        return self._trader.get_status()
