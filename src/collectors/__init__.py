"""Data collectors for prediction market platforms."""

from src.collectors.base import BaseCollector, MarketData
from src.collectors.kalshi import KalshiCollector
from src.collectors.polymarket import PolymarketCollector
from src.collectors.predictit import PredictItCollector
from src.collectors.draftkings import DraftKingsCollector
from src.collectors.fanduel import FanDuelCollector
from src.collectors.ibkr import IBKRCollector

__all__ = [
    "BaseCollector",
    "MarketData",
    "KalshiCollector",
    "PolymarketCollector",
    "PredictItCollector",
    "DraftKingsCollector",
    "FanDuelCollector",
    "IBKRCollector",
]
