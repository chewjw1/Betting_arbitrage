"""
Prediction Market Data Sources - Quick Reference

UNIQUE PRICE SOURCES (use these):
================================

1. Kalshi (API)
   - Backend: Kalshi DCM (CFTC-regulated)
   - Auth: RSA key required
   - Docs: https://docs.kalshi.com/welcome

2. Polymarket (API)
   - Backend: Polygon blockchain
   - Auth: None for read-only
   - Docs: https://docs.polymarket.com/

3. PredictIt (API)
   - Backend: PredictIt (CFTC no-action letter)
   - Auth: None
   - Endpoint: https://www.predictit.org/api/marketdata/all/

4. DraftKings Predictions (API)
   - Backend: Railbird Exchange (acquired DCM)
   - Method: Reverse-engineered REST API (no auth, full bid/ask)
   - URL: https://predictions.draftkings.com

5. FanDuel Predicts (Scraping)
   - Backend: CME Group contracts
   - Method: Playwright browser automation
   - URL: https://www.fanduel.com/predicts

6. IBKR ForecastTrader (Scraping)
   - Backend: ForecastEx DCM + CME contracts
   - Method: Playwright browser automation
   - URL: https://forecasttrader.interactivebrokers.com
   - Note: Zero commission!


DUPLICATE SOURCES (skip these):
==============================

- Robinhood Predictions → Uses Kalshi backend (same prices)
- Coinbase → Uses Kalshi backend (same prices)

These are just different frontends to Kalshi. No arbitrage possible
between them and Kalshi since they share the same order book.


ARBITRAGE OPPORTUNITIES:
========================

Cross-Platform (same event, different prices):
- Kalshi ↔ Polymarket (different user bases)
- Kalshi ↔ PredictIt (different fee structures)
- Kalshi ↔ DraftKings (Railbird vs Kalshi exchange)
- Kalshi ↔ FanDuel (CME vs Kalshi)
- DraftKings ↔ FanDuel (both retail, different users)
- Any combination of the 6 unique sources

Logical (related events, probability inconsistencies):
- Within any single platform
- Complement violations (Yes + No != 100%)
- Temporal relationships (earlier ≤ later deadline)
- Mutually exclusive outcomes
"""

# Data collectors for prediction market platforms

from src.collectors.base import BaseCollector, MarketData
from src.collectors.kalshi import KalshiCollector
from src.collectors.polymarket import PolymarketCollector
from src.collectors.predictit import PredictItCollector
from src.collectors.draftkings import DraftKingsCollector
from src.collectors.fanduel import FanDuelCollector
from src.collectors.ibkr import IBKRCollector

# Primary collectors (unique price sources)
API_COLLECTORS = {
    "kalshi": KalshiCollector,
    "polymarket": PolymarketCollector,
    "predictit": PredictItCollector,
    "draftkings": DraftKingsCollector,
}

SCRAPING_COLLECTORS = {
    "fanduel": FanDuelCollector,
    "ibkr": IBKRCollector,
}

ALL_COLLECTORS = {**API_COLLECTORS, **SCRAPING_COLLECTORS}

# These use Kalshi backend - skip to avoid duplicate data
KALSHI_FRONTENDS = ["robinhood", "coinbase"]

__all__ = [
    "BaseCollector",
    "MarketData",
    "KalshiCollector",
    "PolymarketCollector",
    "PredictItCollector",
    "DraftKingsCollector",
    "FanDuelCollector",
    "IBKRCollector",
    "API_COLLECTORS",
    "SCRAPING_COLLECTORS",
    "ALL_COLLECTORS",
    "KALSHI_FRONTENDS",
]
