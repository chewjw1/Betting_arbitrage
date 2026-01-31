"""Fee calculations for each platform."""

from dataclasses import dataclass
from decimal import Decimal

from src.config import PLATFORM_FEES


@dataclass
class FeeBreakdown:
    """Breakdown of fees for a position."""

    platform: str
    position_size: Decimal
    entry_fee: Decimal
    profit_fee: Decimal  # Applied only if winning
    total_estimated: Decimal  # Conservative estimate

    def __repr__(self) -> str:
        return f"<FeeBreakdown {self.platform}: entry={self.entry_fee}, profit={self.profit_fee}>"


class FeeCalculator:
    """Calculate trading fees for each platform."""

    def __init__(self):
        self.fees = PLATFORM_FEES

    def calculate_entry_fee(
        self,
        platform: str,
        position_size: Decimal,
    ) -> Decimal:
        """Calculate fee to enter a position.

        Args:
            platform: Platform name.
            position_size: Dollar amount of the position.

        Returns:
            Entry fee in dollars.
        """
        fee_config = self.fees.get(platform, {})
        trading_fee_pct = Decimal(str(fee_config.get("trading_fee_pct", 0)))
        return position_size * trading_fee_pct

    def calculate_profit_fee(
        self,
        platform: str,
        profit: Decimal,
    ) -> Decimal:
        """Calculate fee on profits.

        Args:
            platform: Platform name.
            profit: Profit amount in dollars.

        Returns:
            Profit fee in dollars.
        """
        if profit <= 0:
            return Decimal("0")

        fee_config = self.fees.get(platform, {})
        profit_fee_pct = Decimal(str(fee_config.get("profit_fee_pct", 0)))
        return profit * profit_fee_pct

    def calculate_withdrawal_fee(
        self,
        platform: str,
        amount: Decimal,
    ) -> Decimal:
        """Calculate withdrawal fee.

        Args:
            platform: Platform name.
            amount: Withdrawal amount in dollars.

        Returns:
            Withdrawal fee in dollars.
        """
        fee_config = self.fees.get(platform, {})

        # Fixed fee
        withdrawal_fee = Decimal(str(fee_config.get("withdrawal_fee", 0)))

        # Percentage fee
        withdrawal_pct = Decimal(str(fee_config.get("withdrawal_pct", 0)))
        withdrawal_fee += amount * withdrawal_pct

        return withdrawal_fee

    def get_full_breakdown(
        self,
        platform: str,
        position_size: Decimal,
        expected_profit: Decimal,
    ) -> FeeBreakdown:
        """Get complete fee breakdown for a position.

        Args:
            platform: Platform name.
            position_size: Dollar amount of the position.
            expected_profit: Expected profit (for profit fee calculation).

        Returns:
            FeeBreakdown with all fee components.
        """
        entry_fee = self.calculate_entry_fee(platform, position_size)
        profit_fee = self.calculate_profit_fee(platform, expected_profit)

        # Total estimated includes entry + expected profit fee
        total_estimated = entry_fee + profit_fee

        return FeeBreakdown(
            platform=platform,
            position_size=position_size,
            entry_fee=entry_fee,
            profit_fee=profit_fee,
            total_estimated=total_estimated,
        )

    def calculate_arbitrage_fees(
        self,
        platform_a: str,
        platform_b: str,
        position_size: Decimal,
        gross_profit: Decimal,
    ) -> dict:
        """Calculate total fees for an arbitrage trade.

        For arbitrage, we enter on both platforms but only win on one.

        Args:
            platform_a: First platform.
            platform_b: Second platform.
            position_size: Position size per platform.
            gross_profit: Expected gross profit.

        Returns:
            Dict with fee details and net profit.
        """
        # Entry fees on both platforms
        entry_fee_a = self.calculate_entry_fee(platform_a, position_size)
        entry_fee_b = self.calculate_entry_fee(platform_b, position_size)

        # Profit fee applies to the winning side only
        # Since we win on exactly one side, we estimate profit fee for the expected profit
        profit_fee_a = self.calculate_profit_fee(platform_a, gross_profit / 2)
        profit_fee_b = self.calculate_profit_fee(platform_b, gross_profit / 2)

        total_fees = entry_fee_a + entry_fee_b + profit_fee_a + profit_fee_b
        net_profit = gross_profit - total_fees

        return {
            "entry_fee_a": entry_fee_a,
            "entry_fee_b": entry_fee_b,
            "profit_fee_a": profit_fee_a,
            "profit_fee_b": profit_fee_b,
            "total_fees": total_fees,
            "gross_profit": gross_profit,
            "net_profit": net_profit,
            "net_profit_pct": (net_profit / (position_size * 2)) * 100,
        }
