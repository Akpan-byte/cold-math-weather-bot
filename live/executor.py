"""
Cold Math Weather Bot — Live Trading Executor
Real money execution on Polymarket with multiple safety layers.
NEVER runs without explicit user confirmation and dry_run=False.
"""
import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from core.config import ColdMathConfig
from core.engine import (
    ColdMathEngine, MarketCandidate, TradeSignal, 
    TradeRecord, TradeStatus, FilterResult
)

logger = logging.getLogger("coldmath.live")

# Safety: never import or use py_clob_client unless explicitly in live mode


@dataclass
class SafetyCheckResult:
    passed: bool
    checks: list[dict]  # [{name, passed, reason}]
    blocking_reason: str = ""


class LiveSafetyGuard:
    """
    Multi-layer safety system for live trading.
    All checks must pass before any real order is submitted.
    """
    
    # Maximum single trade as fraction of total bankroll
    MAX_TRADE_FRACTION = 0.15  # 15% max per trade
    
    # Maximum daily trades
    MAX_DAILY_TRADES = 20
    
    # Minimum time between trades (seconds)
    MIN_TRADE_INTERVAL = 30
    
    # Maximum slippage tolerance
    MAX_SLIPPAGE = 0.02  # 2%
    
    # Require manual confirmation for trades > $50
    MANUAL_CONFIRM_THRESHOLD = 50.0
    
    def __init__(self, config: ColdMathConfig, engine: ColdMathEngine):
        self.cfg = config
        self.engine = engine
        self._daily_trade_count = 0
        self._daily_reset = time.time()
        self._last_trade_ts = 0.0
        self._kill_switch = False  # Emergency stop
    
    def activate_kill_switch(self):
        """Emergency stop — blocks all trading until manually reset."""
        self._kill_switch = True
        logger.critical("🚨 KILL SWITCH ACTIVATED — all trading blocked")
    
    def deactivate_kill_switch(self):
        """Reset kill switch after investigation."""
        self._kill_switch = False
        logger.info("Kill switch deactivated — trading resumed")
    
    def check(self, signal: TradeSignal) -> SafetyCheckResult:
        """Run all safety checks. ALL must pass for trade to proceed."""
        checks = []
        
        # 0. Kill switch
        kill_passed = not self._kill_switch
        checks.append({
            "name": "kill_switch",
            "passed": kill_passed,
            "reason": "Kill switch is active" if not kill_passed else "OK"
        })
        
        # 1. Dry run check
        dry_run_ok = self.cfg.dry_run
        checks.append({
            "name": "dry_run",
            "passed": not dry_run_ok,  # Should be False for live
            "reason": "dry_run=True — no real orders" if dry_run_ok else "OK"
        })
        
        # 2. Mode check
        mode_ok = self.cfg.mode == "live"
        checks.append({
            "name": "mode",
            "passed": mode_ok,
            "reason": f"Mode is '{self.cfg.mode}', not 'live'" if not mode_ok else "OK"
        })
        
        # 3. Credentials check
        creds_ok = all([
            self.cfg.polymarket_private_key,
            self.cfg.polymarket_api_key,
        ])
        checks.append({
            "name": "credentials",
            "passed": creds_ok,
            "reason": "Missing Polymarket credentials" if not creds_ok else "OK"
        })
        
        # 4. Filter passed
        filter_ok = signal.filter_result == FilterResult.PASS
        checks.append({
            "name": "filter",
            "passed": filter_ok,
            "reason": f"Filter result: {signal.filter_result}" if not filter_ok else "OK"
        })
        
        # 5. Position size vs bankroll
        bankroll = self.engine.sizer.bankroll
        if bankroll > 0:
            size_fraction = signal.position_size_usd / bankroll
            size_ok = size_fraction <= self.MAX_TRADE_FRACTION
        else:
            size_ok = False
        checks.append({
            "name": "position_size",
            "passed": size_ok,
            "reason": f"Position {signal.position_size_usd:.2f} exceeds "
                     f"{self.MAX_TRADE_FRACTION:.0%} of bankroll" if not size_ok else "OK"
        })
        
        # 6. Daily trade limit
        if time.time() - self._daily_reset > 86400:
            self._daily_trade_count = 0
            self._daily_reset = time.time()
        daily_ok = self._daily_trade_count < self.MAX_DAILY_TRADES
        checks.append({
            "name": "daily_limit",
            "passed": daily_ok,
            "reason": f"Daily limit reached ({self._daily_trade_count}/{self.MAX_DAILY_TRADES})" 
                     if not daily_ok else "OK"
        })
        
        # 7. Trade interval
        interval_ok = (time.time() - self._last_trade_ts) >= self.MIN_TRADE_INTERVAL
        checks.append({
            "name": "trade_interval",
            "passed": interval_ok,
            "reason": f"Too soon after last trade ({time.time() - self._last_trade_ts:.0f}s < {self.MIN_TRADE_INTERVAL}s)"
                     if not interval_ok else "OK"
        })
        
        # 8. Manual confirmation threshold
        needs_confirm = signal.position_size_usd > self.MANUAL_CONFIRM_THRESHOLD
        checks.append({
            "name": "manual_confirm",
            "passed": not needs_confirm,  # Requires separate confirmation step
            "reason": f"Trade ${signal.position_size_usd:.2f} > ${self.MANUAL_CONFIRM_THRESHOLD} — needs manual confirm"
                     if needs_confirm else "OK"
        })
        
        # 9. Kelly sanity — position should never exceed bankroll
        kelly_ok = signal.position_size_usd <= bankroll
        checks.append({
            "name": "kelly_sanity",
            "passed": kelly_ok,
            "reason": f"Position ${signal.position_size_usd:.2f} > bankroll ${bankroll:.2f}"
                     if not kelly_ok else "OK"
        })
        
        # 10. Edge still positive (market might have moved)
        edge_ok = signal.expected_value > 0
        checks.append({
            "name": "positive_ev",
            "passed": edge_ok,
            "reason": f"Negative EV: ${signal.expected_value:.4f}" if not edge_ok else "OK"
        })
        
        all_passed = all(c["passed"] for c in checks)
        blocking = [c for c in checks if not c["passed"]]
        blocking_reason = "; ".join(c["reason"] for c in blocking) if blocking else ""
        
        return SafetyCheckResult(
            passed=all_passed,
            checks=checks,
            blocking_reason=blocking_reason
        )
    
    def confirm_large_trade(self, signal: TradeSignal) -> bool:
        """
        For trades above MANUAL_CONFIRM_THRESHOLD, require explicit confirmation.
        In production, this would prompt the user via Telegram.
        For now, returns True if auto-confirm is enabled.
        """
        # This is a placeholder — in production, send Telegram prompt
        # and wait for user's /confirm or /reject response
        logger.warning(f"⚠️ Large trade requires manual confirmation: "
                      f"${signal.position_size_usd:.2f} on '{signal.market.question[:50]}'")
        return False  # Default: require manual confirmation


class LiveExecutor:
    """
    Executes real trades on Polymarket CLOB.
    Uses py_clob_client for order submission.
    
    CRITICAL: This module NEVER executes trades unless:
    1. config.mode == "live"
    2. config.dry_run == False
    3. All safety checks pass
    4. Kill switch is not active
    5. Kelly sanity check passes
    """
    
    def __init__(self, config: ColdMathConfig, engine: ColdMathEngine):
        self.cfg = config
        self.engine = engine
        self.safety = LiveSafetyGuard(config, engine)
        self._clob_client = None
    
    def _init_clob_client(self):
        """Initialize Polymarket CLOB client (only in live mode)."""
        if self.cfg.mode != "live" or self.cfg.dry_run:
            return None
        
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
            
            creds = ApiCreds(
                api_key=self.cfg.polymarket_api_key,
                api_secret=self.cfg.polymarket_private_key,
                api_passphrase=self.cfg.polymarket_passphrase or ""
            )
            
            self._clob_client = ClobClient(
                self.cfg.clob_api_base,
                chain_id=137,  # Polygon
                key=self.cfg.polymarket_private_key,
                creds=creds
            )
            logger.info("CLOB client initialized for live trading")
            return self._clob_client
        except ImportError:
            logger.error("py_clob_client not installed — cannot execute live trades")
            return None
        except Exception as e:
            logger.error(f"CLOB client init failed: {e}")
            return None
    
    async def execute(self, signal: TradeSignal) -> Optional[TradeRecord]:
        """
        Execute a trade signal with full safety checks.
        Returns None if any check fails.
        """
        # Run safety checks
        safety = self.safety.check(signal)
        if not safety.passed:
            logger.warning(f"Trade blocked by safety: {safety.blocking_reason}")
            for check in safety.checks:
                if not check["passed"]:
                    logger.warning(f"  ❌ {check['name']}: {check['reason']}")
            return None
        
        # Large trade confirmation
        if signal.position_size_usd > self.safety.MANUAL_CONFIRM_THRESHOLD:
            if not self.safety.confirm_large_trade(signal):
                logger.info("Large trade awaiting manual confirmation")
                return None
        
        # Initialize CLOB client if needed
        if self._clob_client is None:
            self._init_clob_client()
        
        if self._clob_client is None:
            # Fall back to paper execution
            logger.warning("No CLOB client — executing as paper trade")
            trade = self.engine.execute_signal(signal)
            if trade:
                trade.notes = "PAPER FALLBACK — no CLOB client"
            return trade
        
        # Real execution
        try:
            # Create limit order at our target price
            order = self._create_order(signal)
            if not order:
                return None
            
            # Submit to CLOB
            response = self._clob_client.create_and_post_order(order)
            
            if response and response.get("orderID"):
                trade = self.engine.execute_signal(signal)
                if trade:
                    trade.notes = f"LIVE | OrderID: {response['orderID']}"
                    self.safety._daily_trade_count += 1
                    self.safety._last_trade_ts = time.time()
                logger.info(f"✅ LIVE ORDER: {response.get('orderID')} | "
                           f"${signal.position_size_usd:.2f} @ {signal.entry_price:.2f}")
                return trade
            else:
                logger.error(f"Order rejected: {response}")
                return None
                
        except Exception as e:
            logger.error(f"Live execution error: {e}")
            return None
    
    def _create_order(self, signal: TradeSignal) -> Optional[dict]:
        """Create a CLOB limit order from a trade signal."""
        try:
            from py_clob_client.order_builder.constants import BUY
            
            return {
                "token_id": signal.market.token_id,
                "price": signal.entry_price,
                "size": signal.position_size_usd / signal.entry_price,
                "side": BUY,
                "fee_rate_bps": 0,
            }
        except ImportError:
            return None
    
    def cancel_all_orders(self) -> bool:
        """Emergency: cancel all open orders."""
        if self._clob_client is None:
            return False
        
        try:
            self._clob_client.cancel_all()
            logger.info("All orders cancelled")
            return True
        except Exception as e:
            logger.error(f"Cancel failed: {e}")
            return False
    
    def get_open_orders(self) -> list:
        """Get all open orders."""
        if self._clob_client is None:
            return []
        
        try:
            return self._clob_client.get_orders()
        except Exception as e:
            logger.error(f"Get orders failed: {e}")
            return []
