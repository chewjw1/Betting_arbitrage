"""Discord bot for arbitrage notifications."""

import asyncio
from datetime import datetime
from typing import Optional
import uuid

import discord
from discord import app_commands
from discord.ext import commands, tasks
import structlog

from src.arbitrage.calculator import ArbitrageResult
from src.config import get_settings
from src.notifications.formatters import format_opportunity_embed, format_summary_message

logger = structlog.get_logger()


class ArbitrageBot(commands.Bot):
    """Discord bot for prediction market arbitrage alerts."""

    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True

        super().__init__(
            command_prefix="!arb ",
            intents=intents,
            description="Prediction Market Arbitrage Bot",
        )

        self.settings = get_settings()
        self.logger = logger.bind(component="DiscordBot")
        self.alert_channel: Optional[discord.TextChannel] = None

        # Track sent notifications for reaction handling
        self.pending_notifications: dict[int, str] = {}  # message_id -> opportunity_id

        # Callback for opportunity updates
        self.on_user_response: Optional[callable] = None

    async def setup_hook(self):
        """Called when the bot is starting up."""
        # Add cog with commands
        await self.add_cog(ArbitrageCog(self))

        # Sync slash commands
        try:
            synced = await self.tree.sync()
            self.logger.info("Synced slash commands", count=len(synced))
        except Exception as e:
            self.logger.error("Failed to sync commands", error=str(e))

    async def on_ready(self):
        """Called when the bot is fully ready."""
        self.logger.info(
            "Bot is ready",
            user=str(self.user),
            guilds=len(self.guilds),
        )

        # Get alert channel
        if self.settings.discord_channel_id:
            self.alert_channel = self.get_channel(self.settings.discord_channel_id)
            if self.alert_channel:
                self.logger.info(
                    "Alert channel found",
                    channel=self.alert_channel.name,
                )
            else:
                self.logger.warning(
                    "Alert channel not found",
                    channel_id=self.settings.discord_channel_id,
                )

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Handle reaction adds for opportunity responses."""
        # Ignore bot's own reactions
        if payload.user_id == self.user.id:
            return

        message_id = payload.message_id
        if message_id not in self.pending_notifications:
            return

        opportunity_id = self.pending_notifications[message_id]
        emoji = str(payload.emoji)

        response = None
        if emoji == "✅":
            response = "acted"
        elif emoji == "❌":
            response = "passed"

        if response and self.on_user_response:
            await self.on_user_response(
                opportunity_id=opportunity_id,
                response=response,
                user_id=payload.user_id,
            )

            self.logger.info(
                "User response recorded",
                opportunity_id=opportunity_id,
                response=response,
                user_id=payload.user_id,
            )

            # Remove from pending
            del self.pending_notifications[message_id]

    async def send_opportunity(
        self,
        result: ArbitrageResult,
        opportunity_id: Optional[str] = None,
        channel: Optional[discord.TextChannel] = None,
    ) -> Optional[discord.Message]:
        """Send an opportunity notification.

        Args:
            result: ArbitrageResult to send.
            opportunity_id: Database ID for tracking.
            channel: Channel to send to (defaults to alert channel).

        Returns:
            Sent message or None if failed.
        """
        target_channel = channel or self.alert_channel
        if not target_channel:
            self.logger.error("No channel available for notification")
            return None

        if opportunity_id is None:
            opportunity_id = str(uuid.uuid4())

        try:
            embed = format_opportunity_embed(result, opportunity_id)
            message = await target_channel.send(embed=embed)

            # Add reaction options
            await message.add_reaction("✅")
            await message.add_reaction("❌")

            # Track for response handling
            self.pending_notifications[message.id] = opportunity_id

            self.logger.info(
                "Opportunity sent",
                opportunity_id=opportunity_id,
                message_id=message.id,
                net_profit_pct=float(result.net_profit_pct),
            )

            return message

        except Exception as e:
            self.logger.error(
                "Failed to send opportunity",
                opportunity_id=opportunity_id,
                error=str(e),
            )
            return None

    async def send_scan_summary(
        self,
        opportunities: list[ArbitrageResult],
        scan_time_seconds: float,
        channel: Optional[discord.TextChannel] = None,
    ) -> Optional[discord.Message]:
        """Send a summary of a scan.

        Args:
            opportunities: List of opportunities found.
            scan_time_seconds: Time taken for the scan.
            channel: Channel to send to.

        Returns:
            Sent message or None if failed.
        """
        target_channel = channel or self.alert_channel
        if not target_channel:
            return None

        try:
            content = format_summary_message(opportunities, scan_time_seconds)
            return await target_channel.send(content)
        except Exception as e:
            self.logger.error("Failed to send summary", error=str(e))
            return None


class ArbitrageCog(commands.Cog):
    """Commands for the arbitrage bot."""

    def __init__(self, bot: ArbitrageBot):
        self.bot = bot
        self.logger = logger.bind(component="ArbitrageCog")

    @app_commands.command(name="status", description="Check bot status")
    async def status(self, interaction: discord.Interaction):
        """Show bot status."""
        embed = discord.Embed(
            title=":robot: Arbitrage Bot Status",
            color=discord.Color.green(),
            timestamp=datetime.utcnow(),
        )

        embed.add_field(
            name="Status",
            value=":green_circle: Online",
            inline=True,
        )

        embed.add_field(
            name="Pending Alerts",
            value=str(len(self.bot.pending_notifications)),
            inline=True,
        )

        embed.add_field(
            name="Alert Channel",
            value=f"#{self.bot.alert_channel.name}" if self.bot.alert_channel else "Not set",
            inline=True,
        )

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="threshold", description="View or set minimum profit threshold")
    @app_commands.describe(new_threshold="New minimum net profit percentage (e.g., 1.5)")
    async def threshold(
        self,
        interaction: discord.Interaction,
        new_threshold: Optional[float] = None,
    ):
        """View or set the minimum profit threshold."""
        current = self.bot.settings.min_net_spread_pct

        if new_threshold is None:
            await interaction.response.send_message(
                f":bar_chart: Current threshold: **{current}%** net profit"
            )
        else:
            if new_threshold < 0 or new_threshold > 100:
                await interaction.response.send_message(
                    ":x: Threshold must be between 0 and 100",
                    ephemeral=True,
                )
                return

            # Note: This only updates the runtime setting, not .env
            self.bot.settings.min_net_spread_pct = new_threshold
            await interaction.response.send_message(
                f":white_check_mark: Threshold updated: **{new_threshold}%** net profit"
            )

    @app_commands.command(name="test", description="Send a test notification")
    async def test_notification(self, interaction: discord.Interaction):
        """Send a test notification."""
        from decimal import Decimal
        from src.collectors.base import MarketData

        # Create dummy data
        market_a = MarketData(
            platform="kalshi",
            platform_market_id="TEST-A",
            title="Will it rain tomorrow in New York?",
            yes_price=Decimal("0.45"),
            no_price=Decimal("0.55"),
            url="https://kalshi.com",
        )

        market_b = MarketData(
            platform="polymarket",
            platform_market_id="TEST-B",
            title="Rain in NYC tomorrow?",
            yes_price=Decimal("0.52"),
            no_price=Decimal("0.48"),
            url="https://polymarket.com",
        )

        result = ArbitrageResult(
            market_a=market_a,
            market_b=market_b,
            side_a="yes",
            side_b="no",
            price_a=Decimal("0.45"),
            price_b=Decimal("0.48"),
            gross_spread=Decimal("0.07"),
            gross_profit=Decimal("7.00"),
            total_fees=Decimal("2.50"),
            net_profit=Decimal("4.50"),
            net_profit_pct=Decimal("2.25"),
            is_profitable=True,
            position_size=Decimal("100"),
            notes="This is a test notification",
        )

        await interaction.response.send_message(
            ":white_check_mark: Sending test notification...",
            ephemeral=True,
        )

        await self.bot.send_opportunity(result, opportunity_id="test-" + str(uuid.uuid4())[:8])

    @app_commands.command(name="help", description="Show available commands")
    async def help_command(self, interaction: discord.Interaction):
        """Show help information."""
        embed = discord.Embed(
            title=":question: Arbitrage Bot Help",
            description="Commands for managing prediction market arbitrage alerts",
            color=discord.Color.blue(),
        )

        commands_text = """
        `/status` - Check bot status
        `/threshold [value]` - View or set profit threshold
        `/test` - Send a test notification
        `/help` - Show this help message

        **Reactions:**
        ✅ - Mark that you acted on an opportunity
        ❌ - Mark that you passed on an opportunity
        """

        embed.add_field(name="Commands", value=commands_text, inline=False)

        await interaction.response.send_message(embed=embed)


async def run_bot():
    """Run the Discord bot."""
    settings = get_settings()

    if not settings.discord_bot_token:
        logger.error("DISCORD_BOT_TOKEN not configured")
        return

    bot = ArbitrageBot()

    async with bot:
        await bot.start(settings.discord_bot_token)


if __name__ == "__main__":
    asyncio.run(run_bot())
