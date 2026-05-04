"""Live Sports Arbitrage Module.

Real-time WebSocket-based arbitrage detection and execution
for live sports events across Kalshi and Polymarket.

Features:
- Sub-second price streaming via WebSocket
- Simultaneous cross-platform execution
- Paper trading mode for validation
- Configurable profit thresholds and position limits
"""

from .runner import SportsArbRunner

__all__ = ["SportsArbRunner"]
