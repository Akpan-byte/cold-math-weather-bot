"""Cold Math Weather Bot — Core package."""
from .config import ColdMathConfig, config
from .engine import (
    ColdMathEngine,
    KellySizer,
    TradeFilter,
    TradeLogger,
    MarketCandidate,
    NWSForecast,
    TradeSignal,
    TradeRecord,
    FilterResult,
    TradeSide,
    TradeStatus,
)

__all__ = [
    "ColdMathConfig", "config",
    "ColdMathEngine", "KellySizer", "TradeFilter", "TradeLogger",
    "MarketCandidate", "NWSForecast", "TradeSignal", "TradeRecord",
    "FilterResult", "TradeSide", "TradeStatus",
]
