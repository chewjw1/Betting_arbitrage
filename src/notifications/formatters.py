"""Message formatters for notifications."""

from datetime import datetime
from decimal import Decimal
from typing import Optional

import discord

from src.arbitrage.calculator import ArbitrageResult


def format_opportunity_embed(
    result: ArbitrageResult,
    opportunity_id: Optional[str] = None,
) -> discord.Embed:
    """Format an arbitrage opportunity as a Discord embed.

    Args:
        result: ArbitrageResult to format.
        opportunity_id: Optional database ID for reference.

    Returns:
        Discord Embed object.
    """
    # Color based on profitability
    if result.net_profit_pct >= Decimal("3"):
        color = discord.Color.green()
        emoji = ":moneybag:"
    elif result.net_profit_pct >= Decimal("2"):
        color = discord.Color.gold()
        emoji = ":chart_with_upwards_trend:"
    else:
        color = discord.Color.blue()
        emoji = ":mag:"

    title = f"{emoji} Arbitrage Opportunity: {result.net_profit_pct:.2f}% Net Profit"

    embed = discord.Embed(
        title=title,
        color=color,
        timestamp=datetime.utcnow(),
    )

    # Market A details
    market_a_value = (
        f"**Side:** {result.side_a.upper()}\n"
        f"**Price:** ${float(result.price_a):.4f}\n"
        f"[View Market]({result.market_a.url})" if result.market_a.url else ""
    )
    embed.add_field(
        name=f":one: {result.market_a.platform.title()}",
        value=market_a_value,
        inline=True,
    )

    # Market B details
    market_b_value = (
        f"**Side:** {result.side_b.upper()}\n"
        f"**Price:** ${float(result.price_b):.4f}\n"
        f"[View Market]({result.market_b.url})" if result.market_b.url else ""
    )
    embed.add_field(
        name=f":two: {result.market_b.platform.title()}",
        value=market_b_value,
        inline=True,
    )

    # Spacer
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    # Market titles
    embed.add_field(
        name=":label: Market A",
        value=result.market_a.title[:100] + ("..." if len(result.market_a.title) > 100 else ""),
        inline=False,
    )

    if result.market_a.title != result.market_b.title:
        embed.add_field(
            name=":label: Market B",
            value=result.market_b.title[:100] + ("..." if len(result.market_b.title) > 100 else ""),
            inline=False,
        )

    # Profit breakdown
    profit_details = (
        f"**Position Size:** ${float(result.position_size):.2f} per side\n"
        f"**Gross Spread:** {float(result.gross_spread * 100):.2f}%\n"
        f"**Gross Profit:** ${float(result.gross_profit):.2f}\n"
        f"**Estimated Fees:** ${float(result.total_fees):.2f}\n"
        f"**Net Profit:** ${float(result.net_profit):.2f} ({float(result.net_profit_pct):.2f}%)"
    )
    embed.add_field(
        name=":bar_chart: Profit Analysis",
        value=profit_details,
        inline=False,
    )

    # Notes if any
    if result.notes:
        embed.add_field(
            name=":memo: Notes",
            value=result.notes,
            inline=False,
        )

    # Footer
    footer_text = "React with ✅ if you acted on this, ❌ if you passed"
    if opportunity_id:
        footer_text += f" | ID: {opportunity_id[:8]}"
    embed.set_footer(text=footer_text)

    return embed


def format_summary_message(
    opportunities: list[ArbitrageResult],
    scan_time_seconds: float,
) -> str:
    """Format a summary of all opportunities found in a scan.

    Args:
        opportunities: List of opportunities found.
        scan_time_seconds: Time taken for the scan.

    Returns:
        Formatted summary string.
    """
    if not opportunities:
        return (
            f":mag: **Scan Complete** ({scan_time_seconds:.1f}s)\n"
            "No profitable opportunities found above threshold."
        )

    lines = [
        f":mag: **Scan Complete** ({scan_time_seconds:.1f}s)",
        f"Found **{len(opportunities)}** opportunities above threshold:\n",
    ]

    for i, opp in enumerate(opportunities[:5], 1):  # Top 5
        lines.append(
            f"{i}. {opp.market_a.platform}/{opp.market_b.platform}: "
            f"**{float(opp.net_profit_pct):.2f}%** net"
        )

    if len(opportunities) > 5:
        lines.append(f"\n...and {len(opportunities) - 5} more")

    return "\n".join(lines)


def format_opportunity_text(result: ArbitrageResult) -> str:
    """Format an opportunity as plain text (for non-embed contexts).

    Args:
        result: ArbitrageResult to format.

    Returns:
        Formatted text string.
    """
    return f"""
**Arbitrage Opportunity: {float(result.net_profit_pct):.2f}% Net Profit**

Platform A: {result.market_a.platform.title()}
  - Market: {result.market_a.title[:80]}
  - Side: {result.side_a.upper()} @ ${float(result.price_a):.4f}

Platform B: {result.market_b.platform.title()}
  - Market: {result.market_b.title[:80]}
  - Side: {result.side_b.upper()} @ ${float(result.price_b):.4f}

Analysis:
  - Position Size: ${float(result.position_size):.2f} per side
  - Gross Profit: ${float(result.gross_profit):.2f}
  - Fees: ${float(result.total_fees):.2f}
  - Net Profit: ${float(result.net_profit):.2f}
""".strip()
