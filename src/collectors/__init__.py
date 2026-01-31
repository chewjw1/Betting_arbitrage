"""Data collectors for prediction market platforms."""

from src.collectors.base import BaseCollector, MarketData
from src.collectors.kalshi import KalshiCollector
from src.collectors.polymarket import PolymarketCollector
from src.collectors.predictit import PredictItCollector

__all__ = [
    "BaseCollector",
    "MarketData",
    "KalshiCollector",
    "PolymarketCollector",
    "PredictItCollector",
]
