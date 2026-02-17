"""Discord bot for arbitrage notifications.

Subscribes to Redis pub/sub for opportunities published by the scheduler,
sends rich embed notifications to Discord, and handles user reactions
(✅ acted, ❌ passed) by updating the database.

Architecture:
    Scheduler (jobs.py) --[Redis pub/sub]--> Discord Bot --[Discord API]--> User
    User --[reaction]--> Discord Bot --[DB write]--> Opportunity.user_acted
"""

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Optional
import uuid

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select
import structlog

from src.arbitrage.calculator import ArbitrageResult
from src.collectors.base import MarketData
from src.config import get_settings
from src.database import async_session_factory, init_db, Opportunity
from src.notifications.formatters import format_opportunity_embed
from src.notifications.redis_bridge import OpportunitySubscriber

logger = structlog.get_logger()


def _rebuild_result_from_data(data: dict) -> tuple[Optional[ArbitrageResult], Optional[discord.Embed]]:
    """Rebuild an ArbitrageResult from Redis data for embed formatting.

    Args:
        data: Dict from Redis with opportunity data.

    Returns:
        Tuple of (ArbitrageResult, None) for cross-platform,
        or (None, Embed) for logical (built manually).
    """
    opp_type = data.get("type", "cross_platform")

    if opp_type == "cross_platform":
        ma = data["market_a"]
        mb = data["market_b"]

        market_a = MarketData(
            platform=ma["platform"],
            platform_market_id=ma["platform_market_id"],
            title=ma["title"],
            url=ma.get("url"),
            yes_price=Decimal(ma["yes_price"]) if ma.get("yes_price") else None,
            no_price=Decimal(ma["no_price"]) if ma.get("no_price") else None,
        )
        market_b = MarketData(
            platform=mb["platform"],
            platform_market_id=mb["platform_market_id"],
            title=mb["title"],
            url=mb.get("url"),
            yes_price=Decimal(mb["yes_price"]) if mb.get("yes_price") else None,
            no_price=Decimal(mb["no_price"]) if mb.get("no_price") else None,
        )

        result = ArbitrageResult(
            market_a=market_a,
            market_b=market_b,
            side_a=data["side_a"],
            side_b=data["side_b"],
            price_a=Decimal(data["price_a"]),
            price_b=Decimal(data["price_b"]),
            gross_spread=Decimal(data["gross_spread"]),
            gross_profit=Decimal(data["gross_profit"]),
            total_fees=Decimal(data["total_fees"]),
            net_profit=Decimal(data["net_profit"]),
            net_profit_pct=Decimal(data["net_profit_pct"]),
            is_profitable=data.get("is_profitable", True),
            position_size=Decimal(data["position_size"]),
            notes=data.get("notes"),
            slippage_warning=data.get("slippage_warning"),
            effective_net_profit_pct=Decimal(data["effective_net_profit_pct"]) if data.get("effective_net_profit_pct") else None,
        )
        return result, None

    elif opp_type == "logical":
        # Build a simple embed for logical opportunities
        ma = data["market_a"]
        mb = data.get("market_b", ma)
        net_pct = data.get("net_profit_pct", "0")

        embed = discord.Embed(
            title=f":brain: Logical Arbitrage: {net_pct}% Net Profit",
            color=discord.Color.purple(),
            timestamp=datetime.utcnow(),
        )

        if data.get("subtype_display"):
            embed.description = f"**Type:** {data['subtype_display']}"

        embed.add_field(
            name=f":one: {ma['platform'].title()}",
            value=f"**Market:** {ma['title'][:100]}\n[View Market]({ma['url']})" if ma.get("url") else f"**Market:** {ma['title'][:100]}",
            inline=False,
        )

        if mb["platform_market_id"] != ma["platform_market_id"]:
            embed.add_field(
                name=f":two: {mb['platform'].title()}",
                value=f"**Market:** {mb['title'][:100]}\n[View Market]({mb['url']})" if mb.get("url") else f"**Market:** {mb['title'][:100]}",
                inline=False,
            )

        embed.add_field(
            name=":bar_chart: Analysis",
            value=(
                f"**Constraint:** {data.get('expected_constraint', 'N/A')}\n"
                f"**Violation:** {data.get('violation_amount', 'N/A')}\n"
                f"**Net Profit:** {net_pct}%\n"
                f"**Action:** {data.get('recommended_action', 'See details')}"
            ),
            inline=False,
        )

        embed.set_footer(
            text=f"React with \u2705 if you acted on this, \u274c if you passed | ID: {data['opportunity_id'][:8]}"
        )

        return None, embed

    return None, None


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

        # Redis subscriber for receiving opportunities from scheduler
        self._subscriber: Optional[OpportunitySubscriber] = None
        self._listener_task: Optional[asyncio.Task] = None

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

        # Start Redis listener for opportunities
        if self._listener_task is None or self._listener_task.done():
            self._listener_task = asyncio.create_task(self._redis_listener())
            self.logger.info("Started Redis opportunity listener")

    async def _redis_listener(self):
        """Background task: subscribe to Redis and forward opportunities to Discord."""
        self._subscriber = OpportunitySubscriber()

        while True:
            try:
                await self._subscriber.connect()
                self.logger.info("Redis subscriber connected, listening for opportunities...")

                async for opp_data in self._subscriber.listen():
                    await self._handle_opportunity(opp_data)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("Redis listener error, reconnecting in 5s", error=str(e))
                await asyncio.sleep(5)
            finally:
                if self._subscriber:
                    try:
                        await self._subscriber.close()
                    except Exception:
                        pass

    async def _handle_opportunity(self, data: dict):
        """Process an opportunity received from Redis and send to Discord.

        Args:
            data: Deserialized opportunity dict from Redis.
        """
        if not self.alert_channel:
            self.logger.warning("No alert channel set, dropping opportunity")
            return

        opportunity_id = data.get("opportunity_id", str(uuid.uuid4()))

        try:
            result, embed = _rebuild_result_from_data(data)

            if result is not None:
                # Cross-platform: use the rich formatter
                embed = format_opportunity_embed(result, opportunity_id)

            if embed is None:
                self.logger.error("Failed to build embed for opportunity", data=data)
                return

            message = await self.alert_channel.send(embed=embed)
            await message.add_reaction("\u2705")
            await message.add_reaction("\u274c")

            # Track for reaction handling
            self.pending_notifications[message.id] = opportunity_id

            self.logger.info(
                "Opportunity sent to Discord",
                opportunity_id=opportunity_id,
                message_id=message.id,
                opp_type=data.get("type", "unknown"),
            )

        except Exception as e:
            self.logger.error(
                "Failed to send opportunity to Discord",
                opportunity_id=opportunity_id,
                error=str(e),
            )

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Handle reaction adds for opportunity responses.

        Updates the Opportunity record in the database with user_acted status.
        """
        # Ignore bot's own reactions
        if payload.user_id == self.user.id:
            return

        message_id = payload.message_id
        if message_id not in self.pending_notifications:
            return

        opportunity_id = self.pending_notifications[message_id]
        emoji = str(payload.emoji)

        response = None
        if emoji == "\u2705":
            response = "acted"
        elif emoji == "\u274c":
            response = "passed"

        if not response:
            return

        # Update database
        try:
            async with async_session_factory() as session:
                query = select(Opportunity).where(
                    Opportunity.id == opportunity_id,
                )
                result = await session.execute(query)
                opportunity = result.scalar_one_or_none()

                if opportunity:
                    opportunity.user_acted = (response == "acted")
                    opportunity.user_action_at = datetime.utcnow()
                    opportunity.user_notes = f"Discord reaction by user {payload.user_id}"
                    await session.commit()

                    self.logger.info(
                        "User response recorded in database",
                        opportunity_id=opportunity_id,
                        response=response,
                        user_id=payload.user_id,
                    )
                else:
                    self.logger.warning(
                        "Opportunity not found in database",
                        opportunity_id=opportunity_id,
                    )
        except Exception as e:
            self.logger.error(
                "Failed to update opportunity in database",
                opportunity_id=opportunity_id,
                error=str(e),
            )

        # Remove from pending
        del self.pending_notifications[message_id]

    async def send_opportunity(
        self,
        result: ArbitrageResult,
        opportunity_id: Optional[str] = None,
        channel: Optional[discord.TextChannel] = None,
    ) -> Optional[discord.Message]:
        """Send an opportunity notification directly (for /test command).

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
            await message.add_reaction("\u2705")
            await message.add_reaction("\u274c")

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

    async def close(self):
        """Clean up resources on shutdown."""
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
        if self._subscriber:
            await self._subscriber.close()
        await super().close()


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

        redis_status = "Connected" if (self.bot._subscriber and self.bot._subscriber._redis) else "Disconnected"
        embed.add_field(
            name="Redis",
            value=redis_status,
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
        \u2705 - Mark that you acted on an opportunity
        \u274c - Mark that you passed on an opportunity
        """

        embed.add_field(name="Commands", value=commands_text, inline=False)

        await interaction.response.send_message(embed=embed)


async def run_bot():
    """Run the Discord bot with Redis subscription."""
    settings = get_settings()

    if not settings.discord_bot_token:
        logger.error("DISCORD_BOT_TOKEN not configured")
        return

    # Initialize database (for reaction -> DB writes)
    await init_db()

    bot = ArbitrageBot()

    async with bot:
        await bot.start(settings.discord_bot_token)


if __name__ == "__main__":
    asyncio.run(run_bot())
