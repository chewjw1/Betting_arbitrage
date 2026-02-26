"""
Prediction Market Data Sources - Quick Reference

ACTIVE API SOURCES (4 platforms):
=================================

1. Kalshi (API)
   - Backend: Kalshi DCM (CFTC-regulated)
   - Auth: RSA key optional (public read works)
   - Categories: Politics, Economics, Crypto, Elections
   - Docs: https://docs.kalshi.com/welcome

2. Polymarket (API)
   - Backend: Polygon blockchain
   - Auth: None for read-only
   - Categories: Politics, Crypto, Sports, Pop Culture
   - Docs: https://docs.polymarket.com/

3. PredictIt (API)
   - Backend: PredictIt (CFTC no-action letter)
   - Auth: None
   - Categories: Politics only
   - Endpoint: https://www.predictit.org/api/marketdata/all/

4. DraftKings Predictions (API)
   - Backend: Railbird Exchange (acquired DCM)
   - Auth: None (reverse-engineered API)
   - Categories: Economics, Sports
   - URL: https://predictions.draftkings.com


5. IBKR ForecastTrader (API)
   - Backend: ForecastEx DCM + CME contracts
   - Auth: Client Portal Gateway (IBeam for headless)
   - Categories: Economics (Fed Funds, CPI, Unemployment, GDP)
   - Zero commission - excellent for arbitrage
   - Docs: https://www.interactivebrokers.com/api/doc.html


PLANNED SOURCES (not yet implemented):
======================================

(none currently)


REMOVED SOURCES:
================

- FanDuel Predicts: CME contracts, required Playwright scraping


DUPLICATE SOURCES (skip these):
==============================

- Robinhood Predictions → Uses Kalshi backend (same order book)
- Coinbase → Uses Kalshi backend (same order book)


ARBITRAGE OPPORTUNITIES:
========================

Cross-Platform (same event, different prices):
- Kalshi ↔ Polymarket (different user bases, different fees)
- Kalshi ↔ PredictIt (different fee structures)
- Kalshi ↔ DraftKings (Railbird vs Kalshi exchange)
- Any combination of the 4 active sources

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
from src.collectors.ibkr import IBKRCollector

# All collectors are now API-based (no scraping)
API_COLLECTORS = {
    "kalshi": KalshiCollector,
    "polymarket": PolymarketCollector,
    "predictit": PredictItCollector,
    "draftkings": DraftKingsCollector,
    "ibkr": IBKRCollector,
}

ALL_COLLECTORS = API_COLLECTORS

# These use Kalshi backend - skip to avoid duplicate data
KALSHI_FRONTENDS = ["robinhood", "coinbase"]

__all__ = [
    "BaseCollector",
    "MarketData",
    "KalshiCollector",
    "PolymarketCollector",
    "PredictItCollector",
    "DraftKingsCollector",
    "IBKRCollector",
    "API_COLLECTORS",
    "ALL_COLLECTORS",
    "KALSHI_FRONTENDS",
]
