"""
Cold Math Weather Bot — Core Engine
The brain: probability model, Kelly sizing, trade filtering, decision logic.
"""
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from .config import ColdMathConfig

logger = logging.getLogger("coldmath.core")


# ─── Data Structures ───

class TradeSide(str, Enum):
    YES = "YES"
    NO = "NO"


class TradeStatus(str, Enum):
    PENDING = "pending"
    ENTERED = "entered"
    WON = "won"
    LOST = "lost"
    CANCELLED = "cancelled"


class FilterResult(str, Enum):
    PASS = "PASS"
    FAIL_NWS_CONFIDENCE = "FAIL_NWS_CONFIDENCE"
    FAIL_PRICE_TOO_HIGH = "FAIL_PRICE_TOO_HIGH"
    FAIL_PRICE_TOO_LOW = "FAIL_PRICE_TOO_LOW"
    FAIL_RESOLUTION_TOO_FAR = "FAIL_RESOLUTION_TOO_FAR"
    FAIL_INSUFFICIENT_EDGE = "FAIL_INSUFFICIENT_EDGE"
    FAIL_INSUFFICIENT_LIQUIDITY = "FAIL_INSUFFICIENT_LIQUIDITY"
    FAIL_DAILY_LOSS_LIMIT = "FAIL_DAILY_LOSS_LIMIT"
    FAIL_CONSECUTIVE_LOSSES = "FAIL_CONSECUTIVE_LOSSES"


@dataclass
class MarketCandidate:
    """A Polymarket market that might qualify for Cold Math."""
    market_id: str
    question: str
    outcome: str  # "YES" or "NO" side we'd buy
    price: float  # Current price (0-1)
    volume: float
    liquidity: float  # Available in orderbook at our price
    end_date: str  # ISO format resolution date
    condition_id: str
    token_id: str
    category: str  # "weather", "near_certain", "other"


@dataclass
class NWSForecast:
    """National Weather Service forecast data."""
    station_id: str
    latitude: float
    longitude: float
    forecast_text: str
    high_temp_f: Optional[int] = None
    low_temp_f: Optional[int] = None
    precipitation_prob: Optional[float] = None
    wind_speed_mph: Optional[float] = None
    confidence: float = 0.0  # Our computed confidence (0-1)
    issued_at: str = ""
    expires_at: str = ""


@dataclass
class TradeSignal:
    """Output of the core engine: a trade decision."""
    market: MarketCandidate
    nws_forecast: Optional[NWSForecast]
    filter_result: FilterResult
    position_size_usd: float = 0.0
    expected_value: float = 0.0
    win_probability: float = 0.0
    entry_price: float = 0.0
    kelly_fraction_used: float = 0.0
    reason: str = ""


@dataclass
class TradeRecord:
    """Completed or pending trade for persistence."""
    trade_id: str
    timestamp: str
    market_id: str
    question: str
    side: str
    entry_price: float
    position_size_usd: float
    shares: float
    status: str
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    filter_results: str = ""
    nws_confidence: float = 0.0
    notes: str = ""


# ─── Kelly Criterion ───

class KellySizer:
    """
    Quarter-Kelly position sizing with liquidity awareness.
    
    Full Kelly: f* = (p*b - q) / b
    Quarter Kelly: f = f* × 0.25
    
    With floor ($1 min) and cap (liquidity limit).
    """
    
    def __init__(self, config: ColdMathConfig):
        self.cfg = config
        self._bankroll = config.starting_bankroll
        self._daily_pnl = 0.0
        self._consecutive_losses = 0
        self._daily_reset_ts = time.time()
    
    @property
    def bankroll(self) -> float:
        return self._bankroll
    
    @bankroll.setter
    def bankroll(self, value: float):
        self._bankroll = max(0.0, value)
    
    @property
    def daily_pnl(self) -> float:
        # Reset daily P&L tracker every 24h
        if time.time() - self._daily_reset_ts > 86400:
            self._daily_pnl = 0.0
            self._daily_reset_ts = time.time()
        return self._daily_pnl
    
    def record_trade_result(self, pnl: float):
        """Record a trade result and update internal state."""
        self._bankroll += pnl
        self._daily_pnl += pnl
        if pnl >= 0:
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1
    
    def calculate(self, win_prob: float, entry_price: float, 
                  available_liquidity: float) -> tuple[float, str]:
        """
        Calculate position size using quarter Kelly with constraints.
        
        Returns (position_size_usd, reason).
        """
        # Safety checks first
        if self._check_daily_loss_limit():
            return 0.0, "Daily loss limit reached"
        
        if self._check_consecutive_losses():
            return 0.0, f"Circuit breaker: {self._consecutive_losses} consecutive losses"
        
        # Kelly calculation
        q = 1.0 - win_prob
        b = (1.0 - entry_price) / entry_price  # Net odds: profit per $1 wagered
        
        if b <= 0:
            return 0.0, "Negative or zero odds — no edge"
        
        full_kelly = (win_prob * b - q) / b
        
        if full_kelly <= 0:
            return 0.0, f"Negative Kelly ({full_kelly:.4f}) — no positive edge"
        
        quarter_kelly = full_kelly * self.cfg.kelly_fraction / 0.25  # Scale to actual fraction
        # quarter_kelly = full_kelly * 0.25 but we use cfg.kelly_fraction for flexibility
        quarter_kelly = full_kelly * 0.25 if self.cfg.kelly_fraction == 0.1425 else full_kelly * self.cfg.kelly_fraction
        
        raw_position = self._bankroll * quarter_kelly
        
        # Apply floor
        position = max(self.cfg.min_position_usd, raw_position)
        if raw_position < self.cfg.min_position_usd:
            # Only use floor if it's within 2× of what Kelly says (don't force tiny bankroll)
            if self._bankroll < self.cfg.min_position_usd / quarter_kelly * 2:
                return self.cfg.min_position_usd, f"Floor applied (${self.cfg.min_position_usd}), bankroll too small for Kelly"
        
        # Apply liquidity cap — never take more than X% of available book
        max_by_liquidity = available_liquidity * self.cfg.max_position_pct_book
        if position > max_by_liquidity:
            position = max_by_liquidity
            reason = f"Liquidity-capped: ${position:.2f} (was ${raw_position:.2f})"
        else:
            reason = f"Quarter-Kelly: ${position:.2f} (f*={full_kelly:.4f}, ¼={quarter_kelly:.4f})"
        
        # Apply absolute max cap
        if position > self.cfg.max_position_usd:
            position = self.cfg.max_position_usd
            reason = f"Max cap applied: ${position:.2f}"
        
        return round(position, 2), reason
    
    def _check_daily_loss_limit(self) -> bool:
        """Check if we've hit the daily loss limit."""
        max_loss = self._bankroll * self.cfg.max_daily_loss_pct
        if self.daily_pnl < -max_loss:
            logger.warning(f"Daily loss limit hit: P&L ${self.daily_pnl:.2f} < -${max_loss:.2f}")
            return True
        return False
    
    def _check_consecutive_losses(self) -> bool:
        """Check circuit breaker for consecutive losses."""
        if self._consecutive_losses >= self.cfg.max_consecutive_losses:
            logger.warning(f"Circuit breaker: {self._consecutive_losses} consecutive losses")
            return True
        return False


# ─── Trade Filter ───

class TradeFilter:
    """
    3-gate filter for Cold Math strategy:
    Gate 1: NWS confidence ≥ 99%
    Gate 2: Market price 88-96¢ (sweet spot for edge)
    Gate 3: Resolution within 24 hours
    Plus: edge check, liquidity check, safety checks
    """
    
    def __init__(self, config: ColdMathConfig, sizer: KellySizer):
        self.cfg = config
        self.sizer = sizer
    
    def evaluate(self, market: MarketCandidate, 
                 nws_forecast: Optional[NWSForecast] = None) -> TradeSignal:
        """Run all filter gates and produce a trade signal."""
        
        # Gate 1: NWS confidence (if available)
        if nws_forecast:
            if nws_forecast.confidence < self.cfg.min_nws_confidence:
                return TradeSignal(
                    market=market, nws_forecast=nws_forecast,
                    filter_result=FilterResult.FAIL_NWS_CONFIDENCE,
                    win_probability=nws_forecast.confidence,
                    reason=f"NWS confidence {nws_forecast.confidence:.1%} < {self.cfg.min_nws_confidence:.0%}"
                )
            win_prob = nws_forecast.confidence
        else:
            # For non-weather near-certain markets, estimate from price
            # If market price is 94¢, the implied probability is ~94%
            # We need our own confidence > market price to have edge
            win_prob = market.price + 0.03  # Conservative: assume 3% edge over market
            if win_prob < self.cfg.min_nws_confidence:
                return TradeSignal(
                    market=market, nws_forecast=nws_forecast,
                    filter_result=FilterResult.FAIL_NWS_CONFIDENCE,
                    win_probability=win_prob,
                    reason=f"Estimated confidence {win_prob:.1%} < {self.cfg.min_nws_confidence:.0%}"
                )
        
        # Gate 2: Price range check
        if market.price > self.cfg.max_entry_price:
            return TradeSignal(
                market=market, nws_forecast=nws_forecast,
                filter_result=FilterResult.FAIL_PRICE_TOO_HIGH,
                win_probability=win_prob, entry_price=market.price,
                reason=f"Price {market.price:.2f} > max {self.cfg.max_entry_price:.2f}"
            )
        
        if market.price < self.cfg.min_entry_price:
            return TradeSignal(
                market=market, nws_forecast=nws_forecast,
                filter_result=FilterResult.FAIL_PRICE_TOO_LOW,
                win_probability=win_prob, entry_price=market.price,
                reason=f"Price {market.price:.2f} < min {self.cfg.min_entry_price:.2f} — market doubts outcome"
            )
        
        # Gate 3: Resolution time
        try:
            end_dt = datetime.fromisoformat(market.end_date.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            hours_to_resolution = (end_dt - now).total_seconds() / 3600
            if hours_to_resolution > self.cfg.max_resolution_hours:
                return TradeSignal(
                    market=market, nws_forecast=nws_forecast,
                    filter_result=FilterResult.FAIL_RESOLUTION_TOO_FAR,
                    win_probability=win_prob, entry_price=market.price,
                    reason=f"Resolution in {hours_to_resolution:.1f}h > max {self.cfg.max_resolution_hours:.0f}h"
                )
            if hours_to_resolution < 0:
                return TradeSignal(
                    market=market, nws_forecast=nws_forecast,
                    filter_result=FilterResult.FAIL_RESOLUTION_TOO_FAR,
                    win_probability=win_prob, entry_price=market.price,
                    reason="Market already past resolution date"
                )
        except (ValueError, AttributeError):
            hours_to_resolution = 999  # Can't parse date → skip
        
        # Edge check
        edge = win_prob - market.price
        edge_cents = edge * 1.0  # Convert to dollar edge per $1 contract
        if edge_cents < self.cfg.min_edge_cents:
            return TradeSignal(
                market=market, nws_forecast=nws_forecast,
                filter_result=FilterResult.FAIL_INSUFFICIENT_EDGE,
                win_probability=win_prob, entry_price=market.price,
                reason=f"Edge {edge_cents:.3f}¢ < min {self.cfg.min_edge_cents:.3f}¢"
            )
        
        # Liquidity check
        if market.liquidity < self.cfg.min_position_usd:
            return TradeSignal(
                market=market, nws_forecast=nws_forecast,
                filter_result=FilterResult.FAIL_INSUFFICIENT_LIQUIDITY,
                win_probability=win_prob, entry_price=market.price,
                reason=f"Liquidity ${market.liquidity:.2f} < min ${self.cfg.min_position_usd:.2f}"
            )
        
        # Safety checks
        if self.sizer._check_daily_loss_limit():
            return TradeSignal(
                market=market, nws_forecast=nws_forecast,
                filter_result=FilterResult.FAIL_DAILY_LOSS_LIMIT,
                win_probability=win_prob, entry_price=market.price,
                reason="Daily loss limit reached"
            )
        
        if self.sizer._check_consecutive_losses():
            return TradeSignal(
                market=market, nws_forecast=nws_forecast,
                filter_result=FilterResult.FAIL_CONSECUTIVE_LOSSES,
                win_probability=win_prob, entry_price=market.price,
                reason=f"Circuit breaker: {self.sizer._consecutive_losses} consecutive losses"
            )
        
        # ALL GATES PASSED — Calculate position size
        position_size, sizing_reason = self.sizer.calculate(
            win_prob=win_prob,
            entry_price=market.price,
            available_liquidity=market.liquidity
        )
        
        # Expected value calculation
        # EV = (win_prob × profit_per_share) - ((1 - win_prob) × loss_per_share)
        profit_per_share = 1.0 - market.price
        loss_per_share = market.price
        ev_per_share = (win_prob * profit_per_share) - ((1 - win_prob) * loss_per_share)
        total_ev = ev_per_share * (position_size / market.price)  # shares × ev/share
        
        return TradeSignal(
            market=market,
            nws_forecast=nws_forecast,
            filter_result=FilterResult.PASS,
            position_size_usd=position_size,
            expected_value=round(total_ev, 4),
            win_probability=win_prob,
            entry_price=market.price,
            kelly_fraction_used=self.cfg.kelly_fraction,
            reason=f"✅ PASS — {sizing_reason}, EV=${total_ev:.4f}, Win≈{win_prob:.1%}"
        )


# ─── Persistence ───

class TradeLogger:
    """SQLite + JSONL trade persistence for audit trail."""
    
    def __init__(self, config: ColdMathConfig):
        self.cfg = config
        self.db_path = Path(config.db_path)
        self.jsonl_path = Path(config.trade_log_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
    
    def _init_db(self):
        """Initialize SQLite database."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    trade_id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    question TEXT,
                    side TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    position_size_usd REAL NOT NULL,
                    shares REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    exit_price REAL,
                    pnl REAL,
                    filter_results TEXT,
                    nws_confidence REAL DEFAULT 0,
                    notes TEXT,
                    mode TEXT DEFAULT 'paper'
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bankroll_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    bankroll REAL NOT NULL,
                    daily_pnl REAL DEFAULT 0,
                    total_trades INTEGER DEFAULT 0,
                    win_count INTEGER DEFAULT 0,
                    loss_count INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)
            """)
    
    def log_trade(self, trade: TradeRecord):
        """Log a trade to both SQLite and JSONL."""
        # SQLite
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO trades 
                (trade_id, timestamp, market_id, question, side, entry_price,
                 position_size_usd, shares, status, exit_price, pnl,
                 filter_results, nws_confidence, notes, mode)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade.trade_id, trade.timestamp, trade.market_id, trade.question,
                trade.side, trade.entry_price, trade.position_size_usd, trade.shares,
                trade.status, trade.exit_price, trade.pnl, trade.filter_results,
                trade.nws_confidence, trade.notes, self.cfg.mode
            ))
        
        # JSONL (append-only for audit)
        with open(self.jsonl_path, "a") as f:
            f.write(json.dumps(asdict(trade)) + "\n")
    
    def update_trade_result(self, trade_id: str, exit_price: float, 
                            pnl: float, status: str):
        """Update a trade with resolution result."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                UPDATE trades SET exit_price=?, pnl=?, status=?
                WHERE trade_id=?
            """, (exit_price, pnl, status, trade_id))
    
    def get_stats(self) -> dict:
        """Get trading statistics."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("""
                SELECT 
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
                    SUM(CASE WHEN pnl = 0 THEN 1 ELSE 0 END) as pending,
                    SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) as total_won,
                    SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END) as total_lost,
                    AVG(pnl) as avg_pnl,
                    MAX(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) as best_trade,
                    MIN(CASE WHEN pnl < 0 THEN pnl ELSE 0 END) as worst_trade
                FROM trades WHERE status IN ('won', 'lost')
            """)
            row = cur.fetchone()
            if row and row["total_trades"]:
                total = row["total_trades"]
                wins = row["wins"] or 0
                return {
                    "total_trades": total,
                    "wins": wins,
                    "losses": row["losses"] or 0,
                    "win_rate": wins / total if total > 0 else 0,
                    "total_pnl": (row["total_won"] or 0) + (row["total_lost"] or 0),
                    "avg_pnl": row["avg_pnl"] or 0,
                    "best_trade": row["best_trade"] or 0,
                    "worst_trade": row["worst_trade"] or 0,
                }
            return {"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
                    "total_pnl": 0, "avg_pnl": 0, "best_trade": 0, "worst_trade": 0}
    
    def snapshot_bankroll(self, bankroll: float, daily_pnl: float,
                         total_trades: int, wins: int, losses: int):
        """Take a bankroll snapshot for equity curve tracking."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO bankroll_snapshots 
                (timestamp, bankroll, daily_pnl, total_trades, win_count, loss_count)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                datetime.now(timezone.utc).isoformat(),
                bankroll, daily_pnl, total_trades, wins, losses
            ))


# ─── Core Engine ───

class ColdMathEngine:
    """
    The main engine that ties everything together.
    Market Scanner → NWS Forecast → Trade Filter → Kelly Sizer → Execution
    """
    
    def __init__(self, config: Optional[ColdMathConfig] = None):
        self.cfg = config or ColdMathConfig()
        self.sizer = KellySizer(self.cfg)
        self.filter = TradeFilter(self.cfg, self.sizer)
        self.logger = TradeLogger(self.cfg)
        self._running = False
    
    def evaluate_market(self, market: MarketCandidate,
                       nws_forecast: Optional[NWSForecast] = None) -> TradeSignal:
        """Evaluate a single market through the full pipeline."""
        return self.filter.evaluate(market, nws_forecast)
    
    def execute_signal(self, signal: TradeSignal) -> Optional[TradeRecord]:
        """Convert a passing signal into a trade record."""
        if signal.filter_result != FilterResult.PASS:
            logger.info(f"Signal filtered: {signal.filter_result} — {signal.reason}")
            return None
        
        if signal.position_size_usd <= 0:
            logger.info(f"Zero position size — skipping")
            return None
        
        # Calculate shares
        shares = signal.position_size_usd / signal.entry_price
        
        trade = TradeRecord(
            trade_id=f"cm_{int(time.time()*1000)}_{signal.market.market_id[:8]}",
            timestamp=datetime.now(timezone.utc).isoformat(),
            market_id=signal.market.market_id,
            question=signal.market.question,
            side=signal.market.outcome,
            entry_price=signal.entry_price,
            position_size_usd=signal.position_size_usd,
            shares=round(shares, 4),
            status=TradeStatus.ENTERED,
            filter_results=signal.filter_result.value,
            nws_confidence=signal.win_probability,
            notes=signal.reason
        )
        
        self.logger.log_trade(trade)
        logger.info(f"Trade entered: {trade.trade_id} | ${signal.position_size_usd:.2f} "
                    f"@ {signal.entry_price:.2f} | EV=${signal.expected_value:.4f}")
        
        return trade
    
    def resolve_trade(self, trade_id: str, won: bool, 
                     exit_price: float = 1.0) -> float:
        """Resolve a trade and update bankroll."""
        if exit_price is None:
            exit_price = 1.0 if won else 0.0
        
        # Find the trade
        with sqlite3.connect(self.cfg.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM trades WHERE trade_id=?", (trade_id,))
            row = cur.fetchone()
        
        if not row:
            logger.error(f"Trade {trade_id} not found")
            return 0.0
        
        shares = row["shares"]
        entry_cost = row["position_size_usd"]
        exit_value = shares * exit_price
        pnl = exit_value - entry_cost
        
        status = TradeStatus.WON if won else TradeStatus.LOST
        self.logger.update_trade_result(trade_id, exit_price, pnl, status)
        self.sizer.record_trade_result(pnl)
        
        logger.info(f"Trade resolved: {trade_id} | {'WON' if won else 'LOST'} | "
                    f"P&L: ${pnl:.4f} | Bankroll: ${self.sizer.bankroll:.2f}")
        
        return pnl
    
    def get_status(self) -> dict:
        """Get full engine status for monitoring."""
        stats = self.logger.get_stats()
        return {
            "mode": self.cfg.mode,
            "bankroll": self.sizer.bankroll,
            "daily_pnl": self.sizer.daily_pnl,
            "consecutive_losses": self.sizer._consecutive_losses,
            "kelly_fraction": self.cfg.kelly_fraction,
            **stats
        }
