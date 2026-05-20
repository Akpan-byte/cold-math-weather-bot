"""
Cold Math Weather Bot — Telegram Alert System
Real-time trade notifications, P&L updates, and circuit breaker alerts.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from core.config import ColdMathConfig
from core.engine import TradeRecord, TradeStatus, FilterResult

logger = logging.getLogger("coldmath.alerts")


class TelegramAlerter:
    """
    Sends formatted alerts to Telegram.
    Trade entries, resolutions, circuit breakers, daily summaries.
    """
    
    # Emoji mapping
    EMOJI = {
        "trade_entered": "📝",
        "trade_won": "✅",
        "trade_lost": "❌",
        "circuit_breaker": "🚨",
        "daily_summary": "📊",
        "kill_switch": "🛑",
        "scan_result": "🔍",
        "error": "⚠️",
        "bankroll": "💰",
        "ice": "❄️",
    }
    
    def __init__(self, config: ColdMathConfig):
        self.cfg = config
        self.bot_token = config.telegram_bot_token
        self.chat_id = config.telegram_chat_id
        self._session: Optional[aiohttp.ClientSession] = None
    
    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)
    
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )
        return self._session
    
    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
    
    async def send(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send a message to Telegram."""
        if not self.enabled:
            logger.debug(f"Telegram disabled — would send: {text[:100]}")
            return False
        
        session = await self._get_session()
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    return True
                else:
                    body = await resp.text()
                    logger.error(f"Telegram API error {resp.status}: {body[:200]}")
                    return False
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error(f"Telegram send error: {e}")
            return False
    
    # ─── Formatted Alert Methods ───
    
    async def alert_trade_entered(self, trade: TradeRecord, 
                                   nws_confidence: float = 0,
                                   expected_value: float = 0):
        """Alert when a new trade is entered."""
        emoji = self.EMOJI["trade_entered"]
        text = (
            f"{emoji} <b>Cold Math Trade Entered</b>\n\n"
            f"<b>Market:</b> {trade.question[:80]}\n"
            f"<b>Side:</b> {trade.side}\n"
            f"<b>Entry:</b> ${trade.entry_price:.4f}\n"
            f"<b>Size:</b> ${trade.position_size_usd:.2f}\n"
            f"<b>Shares:</b> {trade.shares:.4f}\n"
            f"<b>NWS Confidence:</b> {nws_confidence:.1%}\n"
            f"<b>Expected Value:</b> ${expected_value:.4f}\n"
            f"<b>Mode:</b> {self.cfg.mode}\n"
            f"<b>ID:</b> <code>{trade.trade_id}</code>"
        )
        return await self.send(text)
    
    async def alert_trade_resolved(self, trade_id: str, question: str,
                                    won: bool, pnl: float, 
                                    bankroll: float, consecutive_losses: int = 0):
        """Alert when a trade resolves."""
        emoji = self.EMOJI["trade_won"] if won else self.EMOJI["trade_lost"]
        pnl_str = f"+${pnl:.4f}" if pnl >= 0 else f"-${abs(pnl):.4f}"
        
        text = (
            f"{emoji} <b>Trade {'WON' if won else 'LOST'}</b>\n\n"
            f"<b>Market:</b> {question[:80]}\n"
            f"<b>P&L:</b> {pnl_str}\n"
            f"<b>Bankroll:</b> ${bankroll:.2f}\n"
            f"<b>Consecutive Losses:</b> {consecutive_losses}\n"
            f"<b>ID:</b> <code>{trade_id}</code>"
        )
        return await self.send(text)
    
    async def alert_circuit_breaker(self, consecutive_losses: int,
                                     bankroll: float):
        """Alert when circuit breaker activates."""
        emoji = self.EMOJI["circuit_breaker"]
        text = (
            f"{emoji} <b>CIRCUIT BREAKER ACTIVATED</b>\n\n"
            f"Trading paused after {consecutive_losses} consecutive losses.\n"
            f"Current bankroll: ${bankroll:.2f}\n\n"
            f"Use /resume to continue trading."
        )
        return await self.send(text)
    
    async def alert_kill_switch(self):
        """Alert when kill switch is activated."""
        emoji = self.EMOJI["kill_switch"]
        text = (
            f"{emoji} <b>KILL SWITCH ACTIVATED</b>\n\n"
            f"All trading stopped. Open orders cancelled.\n"
            f"Investigate before resuming."
        )
        return await self.send(text)
    
    async def alert_daily_summary(self, stats: dict):
        """Send daily trading summary."""
        emoji = self.EMOJI["daily_summary"]
        ice = self.EMOJI["ice"]
        pnl_color = "📈" if stats.get("total_pnl", 0) >= 0 else "📉"
        
        text = (
            f"{emoji} {ice} <b>Cold Math Daily Report</b> {ice}\n\n"
            f"<b>Bankroll:</b> ${stats.get('bankroll', 0):.2f}\n"
            f"<b>Today's P&L:</b> {pnl_color} ${stats.get('daily_pnl', 0):.4f}\n"
            f"<b>Total Trades:</b> {stats.get('total_trades', 0)}\n"
            f"<b>Win Rate:</b> {stats.get('win_rate', 0):.1%}\n"
            f"<b>Best Trade:</b> ${stats.get('best_trade', 0):.4f}\n"
            f"<b>Worst Trade:</b> ${stats.get('worst_trade', 0):.4f}\n"
            f"<b>Consecutive Losses:</b> {stats.get('consecutive_losses', 0)}"
        )
        return await self.send(text)
    
    async def alert_scan_results(self, total_scanned: int, 
                                  passing: int, signals: list):
        """Alert after a market scan cycle."""
        emoji = self.EMOJI["scan_result"]
        text = (
            f"{emoji} <b>Market Scan Complete</b>\n\n"
            f"<b>Scanned:</b> {total_scanned} markets\n"
            f"<b>Passing:</b> {passing} signals\n"
        )
        
        if signals:
            text += "\n<b>Passing Markets:</b>\n"
            for sig in signals[:5]:  # Top 5
                text += (f"• {sig.market.question[:50]}... "
                        f"@ {sig.entry_price:.2f} "
                        f"(Win≈{sig.win_probability:.1%})\n")
        
        return await self.send(text)
    
    async def alert_error(self, error_msg: str):
        """Alert on critical errors."""
        emoji = self.EMOJI["error"]
        text = f"{emoji} <b>Cold Math Error</b>\n\n<code>{error_msg[:500]}</code>"
        return await self.send(text)


class AlertManager:
    """
    Manages alert delivery with rate limiting and batching.
    Prevents spam while ensuring critical alerts always go through.
    """
    
    # Rate limits
    MAX_ALERTS_PER_MINUTE = 5
    MAX_ALERTS_PER_HOUR = 30
    
    # Priority levels
    CRITICAL = 0  # kill switch, circuit breaker — always send
    HIGH = 1      # trade entered/resolved — send immediately
    MEDIUM = 2    # scan results — batch and send periodically
    LOW = 3       # status updates — daily summary only
    
    def __init__(self, config: ColdMathConfig):
        self.cfg = config
        self.alerter = TelegramAlerter(config)
        self._alert_timestamps: list[float] = []
        self._pending_medium: list[str] = []
        self._pending_low: list[str] = []
    
    async def send(self, text: str, priority: int = HIGH,
                   parse_mode: str = "HTML") -> bool:
        """Send an alert with priority-based rate limiting."""
        now = asyncio.get_event_loop().time() if asyncio.get_event_loop().is_running() else 0
        
        # Critical alerts bypass rate limiting
        if priority == self.CRITICAL:
            return await self.alerter.send(text, parse_mode)
        
        # Check rate limit
        if not self._check_rate_limit():
            if priority == self.HIGH:
                # Still send high priority, but log warning
                logger.warning("Alert rate limit hit — sending high priority anyway")
                return await self.alerter.send(text, parse_mode)
            elif priority == self.MEDIUM:
                self._pending_medium.append(text)
                return False
            else:
                self._pending_low.append(text)
                return False
        
        return await self.alerter.send(text, parse_mode)
    
    def _check_rate_limit(self) -> bool:
        """Check if we're within rate limits."""
        import time
        now = time.time()
        
        # Clean old timestamps
        self._alert_timestamps = [t for t in self._alert_timestamps 
                                   if now - t < 3600]
        
        # Check per-minute limit
        recent = [t for t in self._alert_timestamps if now - t < 60]
        if len(recent) >= self.MAX_ALERTS_PER_MINUTE:
            return False
        
        # Check per-hour limit
        if len(self._alert_timestamps) >= self.MAX_ALERTS_PER_HOUR:
            return False
        
        self._alert_timestamps.append(now)
        return True
    
    async def flush_pending(self):
        """Send all pending medium/low priority alerts as a batch."""
        if self._pending_medium:
            batch = "\n\n".join(self._pending_medium)
            self._pending_medium.clear()
            await self.alerter.send(f"📋 <b>Batched Updates:</b>\n\n{batch}")
        
        if self._pending_low:
            batch = "\n\n".join(self._pending_low)
            self._pending_low.clear()
            await self.alerter.send(f"📊 <b>Status Updates:</b>\n\n{batch}")
    
    async def close(self):
        await self.flush_pending()
        await self.alerter.close()
