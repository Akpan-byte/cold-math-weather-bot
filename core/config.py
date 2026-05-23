"""
Cold Math Weather Bot — Configuration
All tunable parameters in one place.
"""
from dataclasses import dataclass, field
from typing import Optional
import os


@dataclass
class ColdMathConfig:
    # ─── Strategy Parameters ───
    min_nws_confidence: float = 0.51       # Optimized BUNDLE threshold (was 0.99)
    max_entry_price: float = 0.96          # Buy contracts at ≤96¢
    min_entry_price: float = 0.10          # Wider coverage (was 0.88)
    max_resolution_hours: float = 72.0     # Horizon extension (was 24.0)
    min_edge_cents: float = 0.04           # Minimum 4¢ edge per BUNDLE (was 0.03)
    
    # ─── Kelly Sizing ───
    kelly_fraction: float = 0.30           # Optimized Kelly BUNDLE (was 0.1425)
    min_position_usd: float = 1.00         # Polymarket minimum trade
    max_position_usd: float = 1000.00      # Liquidity safety cap
    max_position_pct_book: float = 0.25    # Never take more than 25% of available liquidity
    
    # ─── Bankroll ───
    starting_bankroll: float = 25.00       # Starting capital
    max_daily_loss_pct: float = 0.10       # Stop trading if lost 10% of bankroll today
    max_consecutive_losses: int = 5        # Circuit breaker: pause after N consecutive losses
    
    # ─── Win Rate Estimates (for Kelly calc) ───
    estimated_win_rate: float = 0.97       # Expected win rate on filtered markets
    
    # ─── NWS API ───
    nws_api_base: str = "https://api.weather.gov"
    nws_user_agent: str = "ColdMathBot/1.0 (coldmath@akpanbrain)"
    nws_cache_ttl: int = 300               # Cache NWS forecasts for 5 minutes
    
    # ─── Polymarket API ───
    gamma_api_base: str = "https://gamma-api.polymarket.com"
    clob_api_base: str = "https://clob.polymarket.com"
    data_api_base: str = "https://data-api.polymarket.com"
    
    # ─── Market Scanner ───
    scan_interval_seconds: int = 300       # Scan for new markets every 5 minutes
    weather_keywords: list = field(default_factory=lambda: [
        "temperature", "weather", "high of", "low of", "degrees",
        "fahrenheit", "celsius", "rain", "snow", "wind", "heat",
        "cold snap", "freeze", "tornado", "hurricane", "flood",
        "drought", "storm", "precipitation", "humidity"
    ])
    near_certain_keywords: list = field(default_factory=lambda: [
        "will resolve", "already occurred", "confirmed",
        "official result", "final score", "certified",
        "already happened", "past event"
    ])
    
    # ─── Execution ───
    mode: str = "paper"                    # "backtest", "paper", "live"
    dry_run: bool = True                   # If True, never submit real orders
    
    # ─── Polymarket Credentials (for live mode) ───
    polymarket_private_key: Optional[str] = None
    polymarket_api_key: Optional[str] = None
    polymarket_passphrase: Optional[str] = None
    
    # ─── Telegram Alerts ───
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    
    # ─── Logging ───
    log_dir: str = "/config/coldmath/logs"
    data_dir: str = "/config/coldmath/data"
    log_level: str = "INFO"
    
    # ─── Backtest ───
    backtest_start_date: str = "2025-01-01"
    backtest_end_date: str = "2026-05-19"
    backtest_initial_bankroll: float = 25.00
    
    # ─── Persistence ───
    db_path: str = "/config/coldmath/data/coldmath.db"
    trade_log_path: str = "/config/coldmath/data/trades.jsonl"
    
    @classmethod
    def from_env(cls):
        """Load config from environment variables, with .env file support."""
        cfg = cls()
        cfg.polymarket_private_key = os.getenv("POLYMARKET_PRIVATE_KEY")
        cfg.polymarket_api_key = os.getenv("POLYMARKET_API_KEY")
        cfg.polymarket_passphrase = os.getenv("POLYMARKET_PASSPHRASE")
        cfg.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        cfg.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        mode_val = os.getenv("COLDMATH_MODE")
        if mode_val:
            cfg.mode = mode_val
        dry_run_val = os.getenv("COLDMATH_DRY_RUN")
        if dry_run_val:
            cfg.dry_run = dry_run_val.lower() == "true"
        bankroll_val = os.getenv("COLDMATH_BANKROLL")
        if bankroll_val:
            cfg.starting_bankroll = float(bankroll_val)
            cfg.backtest_initial_bankroll = cfg.starting_bankroll
        return cfg


# Singleton
config = ColdMathConfig.from_env()
