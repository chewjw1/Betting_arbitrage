"""Arbitrage detection engine."""

from src.arbitrage.calculator import ArbitrageCalculator, ArbitrageResult
from src.arbitrage.cross_platform import CrossPlatformDetector
from src.arbitrage.fees import FeeCalculator
from src.arbitrage.logical import (
    LogicalArbitrageDetector,
    LogicalArbitrageResult,
    LogicalRelationship,
    RelationshipType,
)

__all__ = [
    "ArbitrageCalculator",
    "ArbitrageResult",
    "CrossPlatformDetector",
    "FeeCalculator",
    "LogicalArbitrageDetector",
    "LogicalArbitrageResult",
    "LogicalRelationship",
    "RelationshipType",
]
