"""Tests for the arbitrage calculator."""

from decimal import Decimal

import pytest

from src.arbitrage.calculator import ArbitrageCalculator
from src.arbitrage.fees import FeeCalculator
from src.collectors.base import MarketData


@pytest.fixture
def calculator():
    """Create a calculator with default settings."""
    return ArbitrageCalculator(min_net_spread_pct=1.0, default_position_size=100.0)


@pytest.fixture
def fee_calculator():
    """Create a fee calculator."""
    return FeeCalculator()


@pytest.fixture
def market_a():
    """Create a sample Kalshi market."""
    return MarketData(
        platform="kalshi",
        platform_market_id="KALSHI-TEST-1",
        title="Will it rain tomorrow?",
        yes_price=Decimal("0.45"),
        no_price=Decimal("0.55"),
        url="https://kalshi.com/test",
    )


@pytest.fixture
def market_b():
    """Create a sample Polymarket market."""
    return MarketData(
        platform="polymarket",
        platform_market_id="POLY-TEST-1",
        title="Rain tomorrow?",
        yes_price=Decimal("0.52"),
        no_price=Decimal("0.48"),
        url="https://polymarket.com/test",
    )


class TestArbitrageCalculator:
    """Tests for ArbitrageCalculator."""

    def test_no_arbitrage_when_prices_sum_to_one(self, calculator):
        """Test that no arbitrage is detected when prices are fair."""
        market_a = MarketData(
            platform="kalshi",
            platform_market_id="TEST-1",
            title="Test market",
            yes_price=Decimal("0.50"),
            no_price=Decimal("0.50"),
        )
        market_b = MarketData(
            platform="polymarket",
            platform_market_id="TEST-2",
            title="Test market",
            yes_price=Decimal("0.50"),
            no_price=Decimal("0.50"),
        )

        result = calculator.calculate_cross_platform(market_a, market_b)
        assert result is None or not result.is_profitable

    def test_arbitrage_detected(self, calculator, market_a, market_b):
        """Test that arbitrage is detected when spread exists."""
        result = calculator.calculate_cross_platform(market_a, market_b)

        assert result is not None
        assert result.gross_spread > 0
        assert result.market_a.platform == "kalshi"
        assert result.market_b.platform == "polymarket"

    def test_correct_sides_assigned(self, calculator, market_a, market_b):
        """Test that buy sides are correctly assigned."""
        result = calculator.calculate_cross_platform(market_a, market_b)

        assert result is not None
        # Should buy YES on cheaper platform (kalshi at 0.45)
        # and NO on other platform (polymarket NO = 1 - 0.52 = 0.48)
        assert result.side_a == "yes"
        assert result.side_b == "no"

    def test_profit_calculation(self, calculator, market_a, market_b):
        """Test that profit is calculated correctly."""
        result = calculator.calculate_cross_platform(market_a, market_b)

        assert result is not None
        # YES on kalshi: 0.45, NO on polymarket: 0.48
        # Total cost: 0.93 for guaranteed $1 return
        # Gross spread: 0.07 (7%)
        assert result.gross_spread == Decimal("0.07")

    def test_fees_reduce_profit(self, calculator, market_a, market_b):
        """Test that fees reduce the net profit."""
        result = calculator.calculate_cross_platform(market_a, market_b)

        assert result is not None
        assert result.total_fees > 0
        assert result.net_profit < result.gross_profit

    def test_missing_prices_returns_none(self, calculator):
        """Test that missing prices return None."""
        market_a = MarketData(
            platform="kalshi",
            platform_market_id="TEST-1",
            title="Test",
            yes_price=None,
        )
        market_b = MarketData(
            platform="polymarket",
            platform_market_id="TEST-2",
            title="Test",
            yes_price=Decimal("0.50"),
        )

        result = calculator.calculate_cross_platform(market_a, market_b)
        assert result is None


class TestFeeCalculator:
    """Tests for FeeCalculator."""

    def test_kalshi_entry_fee(self, fee_calculator):
        """Test Kalshi entry fee calculation."""
        fee = fee_calculator.calculate_entry_fee("kalshi", Decimal("100"))
        # 1.2% of $100 = $1.20
        assert fee == Decimal("1.2")

    def test_polymarket_entry_fee(self, fee_calculator):
        """Test Polymarket entry fee calculation."""
        fee = fee_calculator.calculate_entry_fee("polymarket", Decimal("100"))
        # 0.01% of $100 = $0.01
        assert fee == Decimal("0.01")

    def test_profit_fee(self, fee_calculator):
        """Test profit fee calculation."""
        kalshi_fee = fee_calculator.calculate_profit_fee("kalshi", Decimal("10"))
        # 2% of $10 = $0.20
        assert kalshi_fee == Decimal("0.2")

        polymarket_fee = fee_calculator.calculate_profit_fee("polymarket", Decimal("10"))
        # 0% = $0
        assert polymarket_fee == Decimal("0")

    def test_no_profit_fee_on_loss(self, fee_calculator):
        """Test no profit fee on losses."""
        fee = fee_calculator.calculate_profit_fee("kalshi", Decimal("-10"))
        assert fee == Decimal("0")

    def test_arbitrage_fees(self, fee_calculator):
        """Test complete arbitrage fee calculation."""
        result = fee_calculator.calculate_arbitrage_fees(
            platform_a="kalshi",
            platform_b="polymarket",
            position_size=Decimal("100"),
            gross_profit=Decimal("7"),
        )

        assert result["entry_fee_a"] > 0  # Kalshi entry
        assert result["entry_fee_b"] > 0  # Polymarket entry (tiny)
        assert result["total_fees"] > 0
        assert result["net_profit"] < result["gross_profit"]
