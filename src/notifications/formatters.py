"""Message formatters for notifications."""

from datetime import datetime
from decimal import Decimal
from typing import Optional, Union

import discord

from src.arbitrage.calculator import ArbitrageResult

# Try to import LogicalArbitrageResult
try:
    from src.arbitrage.logical import LogicalArbitrageResult, FeeBreakdown
except ImportError:
    LogicalArbitrageResult = None
    FeeBreakdown = None


# Opportunity type display info
TYPE_CONFIG = {
    "cross_platform": {
        "emoji": ":arrows_counterclockwise:",
        "color": discord.Color.blue(),
        "label": "Cross-Platform",
    },
    "logical": {
        "emoji": ":brain:",
        "color": discord.Color.purple(),
        "label": "Logical",
    },
    "cross_platform_logical": {
        "emoji": ":globe_with_meridians:",
        "color": discord.Color.teal(),
        "label": "Cross-Platform Logical",
    },
}


def format_opportunity_embed(
    result: Union[ArbitrageResult, "LogicalArbitrageResult"],
    opportunity_id: Optional[str] = None,
) -> discord.Embed:
    """Format an arbitrage opportunity as a Discord embed.

    Args:
        result: ArbitrageResult or LogicalArbitrageResult to format.
        opportunity_id: Optional database ID for reference.

    Returns:
        Discord Embed object.
    """
    # Determine opportunity type
    opp_type = getattr(result, 'opportunity_type', 'cross_platform')
    type_config = TYPE_CONFIG.get(opp_type, TYPE_CONFIG["cross_platform"])

    # Get net profit percentage
    if hasattr(result, 'net_profit_pct'):
        net_profit_pct = result.net_profit_pct
    else:
        net_profit_pct = Decimal("0")

    # Color based on profitability
    if net_profit_pct >= Decimal("3"):
        color = discord.Color.green()
        profit_emoji = ":moneybag:"
    elif net_profit_pct >= Decimal("2"):
        color = discord.Color.gold()
        profit_emoji = ":chart_with_upwards_trend:"
    else:
        color = type_config["color"]
        profit_emoji = ":mag:"

    # Build title
    title = f"{profit_emoji} {type_config['label']}: {net_profit_pct:.2f}% Net Profit"

    embed = discord.Embed(
        title=title,
        color=color,
        timestamp=datetime.utcnow(),
    )

    # Add subtype if available
    subtype_display = getattr(result, 'subtype_display', None)
    if subtype_display:
        embed.description = f"**Type:** {subtype_display}"

    # Get market info - handle both ArbitrageResult and LogicalArbitrageResult
    if hasattr(result, 'market_a'):
        market_a = result.market_a
        market_b = result.market_b
        platform_a = market_a.platform
        platform_b = market_b.platform
    elif hasattr(result, 'relationship'):
        market_a = result.relationship.market_a
        market_b = result.relationship.market_b
        platform_a = market_a.platform
        platform_b = market_b.platform
    else:
        market_a = market_b = None
        platform_a = platform_b = "Unknown"

    # Get prices and sides
    if hasattr(result, 'price_a'):
        price_a = result.price_a
        price_b = result.price_b
        side_a = getattr(result, 'side_a', 'yes')
        side_b = getattr(result, 'side_b', 'no')
    else:
        price_a = getattr(result, 'price_a', None) or (market_a.yes_price if market_a else None)
        price_b = getattr(result, 'price_b', None) or (market_b.yes_price if market_b else None)
        side_a = getattr(result, 'side_a', 'yes')
        side_b = getattr(result, 'side_b', 'no')

    # Platform A details with fee breakdown
    fee_a = getattr(result, 'fee_breakdown_a', None)
    market_a_value = f"**Side:** {side_a.upper()}\n**Price:** ${float(price_a):.4f}" if price_a else "N/A"
    if fee_a:
        market_a_value += f"\n**Fee:** ${float(fee_a.total_fee):.2f} ({float(fee_a.fee_pct):.1f}%)"
    if market_a and market_a.url:
        market_a_value += f"\n[View Market]({market_a.url})"

    embed.add_field(
        name=f":one: {platform_a.title()}",
        value=market_a_value,
        inline=True,
    )

    # Platform B details with fee breakdown (only if different from A)
    if platform_a != platform_b or market_a != market_b:
        fee_b = getattr(result, 'fee_breakdown_b', None)
        market_b_value = f"**Side:** {side_b.upper()}\n**Price:** ${float(price_b):.4f}" if price_b else "N/A"
        if fee_b:
            market_b_value += f"\n**Fee:** ${float(fee_b.total_fee):.2f} ({float(fee_b.fee_pct):.1f}%)"
        if market_b and market_b.url:
            market_b_value += f"\n[View Market]({market_b.url})"

        embed.add_field(
            name=f":two: {platform_b.title()}",
            value=market_b_value,
            inline=True,
        )

        # Spacer
        embed.add_field(name="\u200b", value="\u200b", inline=True)

    # Market titles
    if market_a:
        embed.add_field(
            name=":label: Market A",
            value=market_a.title[:100] + ("..." if len(market_a.title) > 100 else ""),
            inline=False,
        )

    if market_b and market_a != market_b and market_a.title != market_b.title:
        embed.add_field(
            name=":label: Market B",
            value=market_b.title[:100] + ("..." if len(market_b.title) > 100 else ""),
            inline=False,
        )

    # Profit breakdown
    position_size = getattr(result, 'position_size', Decimal("100"))
    gross_spread = getattr(result, 'gross_spread', Decimal("0"))
    gross_profit = getattr(result, 'gross_profit', position_size * gross_spread)
    total_fees = getattr(result, 'total_fees', None) or getattr(result, 'estimated_fees', Decimal("0"))
    net_profit = getattr(result, 'net_profit', gross_profit - total_fees)

    profit_details = (
        f"**Position Size:** ${float(position_size):.2f} per side\n"
        f"**Gross Spread:** {float(gross_spread * 100):.2f}%\n"
        f"**Gross Profit:** ${float(gross_profit):.2f}\n"
        f"**Total Fees:** ${float(total_fees):.2f}\n"
        f"**Net Profit:** ${float(net_profit):.2f} ({float(net_profit_pct):.2f}%)"
    )
    embed.add_field(
        name=":bar_chart: Profit Analysis",
        value=profit_details,
        inline=False,
    )

    # Recommended action if available
    recommended_action = getattr(result, 'recommended_action', None)
    if recommended_action:
        embed.add_field(
            name=":dart: Recommended Action",
            value=recommended_action,
            inline=False,
        )

    # Notes if any
    notes = getattr(result, 'notes', None)
    if notes:
        embed.add_field(
            name=":memo: Notes",
            value=notes,
            inline=False,
        )

    # Footer
    footer_parts = ["React with \u2705 if you acted on this, \u274c if you passed"]
    if opportunity_id:
        footer_parts.append(f"ID: {opportunity_id[:8]}")
    footer_parts.append(f"Type: {type_config['label']}")

    embed.set_footer(text=" | ".join(footer_parts))

    return embed


def format_summary_message(
    opportunities: list,
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

    # Count by type
    cross_platform = sum(1 for o in opportunities if getattr(o, 'opportunity_type', 'cross_platform') == 'cross_platform')
    logical = sum(1 for o in opportunities if getattr(o, 'opportunity_type', None) == 'logical')
    cross_logical = sum(1 for o in opportunities if getattr(o, 'opportunity_type', None) == 'cross_platform_logical')

    lines = [
        f":mag: **Scan Complete** ({scan_time_seconds:.1f}s)",
        f"Found **{len(opportunities)}** opportunities above threshold",
    ]

    # Type breakdown
    type_parts = []
    if cross_platform:
        type_parts.append(f"{cross_platform} cross-platform")
    if logical:
        type_parts.append(f"{logical} logical")
    if cross_logical:
        type_parts.append(f"{cross_logical} cross-platform logical")
    if type_parts:
        lines.append(f"Types: {', '.join(type_parts)}\n")

    # Top opportunities
    for i, opp in enumerate(opportunities[:5], 1):
        opp_type = getattr(opp, 'opportunity_type', 'cross_platform')
        type_emoji = TYPE_CONFIG.get(opp_type, TYPE_CONFIG["cross_platform"])["emoji"]

        if hasattr(opp, 'market_a'):
            platforms = f"{opp.market_a.platform}/{opp.market_b.platform}"
        elif hasattr(opp, 'relationship'):
            platforms = f"{opp.relationship.market_a.platform}/{opp.relationship.market_b.platform}"
        else:
            platforms = "unknown"

        net_pct = float(getattr(opp, 'net_profit_pct', 0))
        lines.append(f"{i}. {type_emoji} {platforms}: **{net_pct:.2f}%** net")

    if len(opportunities) > 5:
        lines.append(f"\n...and {len(opportunities) - 5} more")

    return "\n".join(lines)


def format_opportunity_text(result: Union[ArbitrageResult, "LogicalArbitrageResult"]) -> str:
    """Format an opportunity as plain text (for non-embed contexts).

    Args:
        result: ArbitrageResult or LogicalArbitrageResult to format.

    Returns:
        Formatted text string.
    """
    opp_type = getattr(result, 'opportunity_type', 'cross_platform')
    type_label = TYPE_CONFIG.get(opp_type, TYPE_CONFIG["cross_platform"])["label"]
    subtype_display = getattr(result, 'subtype_display', '')

    # Get market info
    if hasattr(result, 'market_a'):
        market_a = result.market_a
        market_b = result.market_b
    elif hasattr(result, 'relationship'):
        market_a = result.relationship.market_a
        market_b = result.relationship.market_b
    else:
        return "Unable to format opportunity"

    # Get prices
    price_a = getattr(result, 'price_a', None) or market_a.yes_price
    price_b = getattr(result, 'price_b', None) or market_b.yes_price
    side_a = getattr(result, 'side_a', 'yes')
    side_b = getattr(result, 'side_b', 'no')

    # Get profit info
    net_profit_pct = getattr(result, 'net_profit_pct', Decimal("0"))
    position_size = getattr(result, 'position_size', Decimal("100"))
    gross_profit = getattr(result, 'gross_profit', Decimal("0"))
    total_fees = getattr(result, 'total_fees', None) or getattr(result, 'estimated_fees', Decimal("0"))
    net_profit = getattr(result, 'net_profit', gross_profit - total_fees)

    text = f"""
**{type_label} Opportunity: {float(net_profit_pct):.2f}% Net Profit**
{f'Subtype: {subtype_display}' if subtype_display else ''}

Platform A: {market_a.platform.title()}
  - Market: {market_a.title[:80]}
  - Side: {side_a.upper()} @ ${float(price_a):.4f}

Platform B: {market_b.platform.title()}
  - Market: {market_b.title[:80]}
  - Side: {side_b.upper()} @ ${float(price_b):.4f}

Analysis:
  - Position Size: ${float(position_size):.2f} per side
  - Gross Profit: ${float(gross_profit):.2f}
  - Fees: ${float(total_fees):.2f}
  - Net Profit: ${float(net_profit):.2f}
""".strip()

    recommended_action = getattr(result, 'recommended_action', None)
    if recommended_action:
        text += f"\n\nRecommended: {recommended_action}"

    return text


def format_logical_opportunity_embed(
    result: "LogicalArbitrageResult",
    opportunity_id: Optional[str] = None,
) -> discord.Embed:
    """Format a logical arbitrage opportunity as a Discord embed.

    This is a specialized formatter for LogicalArbitrageResult.
    Falls back to the generic formatter.

    Args:
        result: LogicalArbitrageResult to format.
        opportunity_id: Optional database ID for reference.

    Returns:
        Discord Embed object.
    """
    return format_opportunity_embed(result, opportunity_id)
