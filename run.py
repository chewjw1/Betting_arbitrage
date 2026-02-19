#!/usr/bin/env python3
"""Unified launcher: runs scheduler + Discord bot + API in a single process.

No Redis or PostgreSQL required — uses SQLite and in-process asyncio.Queue.

Usage:
    python run.py

This is the recommended way to run on a seedbox or VPS without Docker.
For Docker deployments, use docker-compose.yml instead (separate processes + Redis).
"""

import asyncio
import os
import signal
import sys

# Set SQLite as default if no DATABASE_URL is configured
if not os.environ.get("DATABASE_URL"):
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///data/arbitrage.db"
    os.environ["DATABASE_URL_SYNC"] = "sqlite:///data/arbitrage.db"

import structlog
import uvicorn

from src.config import get_settings
from src.database import init_db
from src.notifications.redis_bridge import InProcessPublisher, InProcessSubscriber
from src.scheduler.jobs import ArbitrageScanner

logger = structlog.get_logger()
settings = get_settings()


async def run_scheduler(publisher, scanner):
    """Run periodic scans."""
    interval = settings.api_poll_interval_seconds

    logger.info("Running initial scan...")
    try:
        await scanner.run_scan()
    except Exception as e:
        logger.error("Initial scan failed", error=str(e))

    logger.info("Scheduler running", interval_seconds=interval)
    while True:
        await asyncio.sleep(interval)
        try:
            await scanner.run_scan()
        except Exception as e:
            logger.error("Scan failed", error=str(e))


async def run_api():
    """Run FastAPI dashboard in background."""
    config = uvicorn.Config(
        "src.api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    await server.serve()


async def run_discord_bot(subscriber):
    """Run Discord bot with in-process subscriber."""
    if not settings.discord_bot_token:
        logger.warning(
            "DISCORD_BOT_TOKEN not set — running without Discord notifications. "
            "Opportunities will still be stored in the database and visible on the dashboard."
        )
        # Drain the queue so the scanner doesn't block
        async for _ in subscriber.listen():
            pass
        return

    if not settings.discord_channel_id:
        logger.warning(
            "DISCORD_CHANNEL_ID not set — bot will connect but cannot send alerts. "
            "Set DISCORD_CHANNEL_ID in .env to your alerts channel ID."
        )

    from discord import LoginFailure
    from src.notifications.discord_bot import ArbitrageBot

    logger.info(
        "Starting Discord bot",
        channel_id=settings.discord_channel_id or "NOT SET",
    )

    bot = ArbitrageBot(subscriber=subscriber)
    try:
        async with bot:
            await bot.start(settings.discord_bot_token)
    except LoginFailure:
        logger.error(
            "Discord login failed — check DISCORD_BOT_TOKEN in .env. "
            "Get a bot token from https://discord.com/developers/applications"
        )
        # Drain queue so scanner doesn't block
        async for _ in subscriber.listen():
            pass
    except Exception as e:
        logger.error("Discord bot crashed", error=str(e))
        # Drain queue so scanner doesn't block
        async for _ in subscriber.listen():
            pass


async def main():
    """Run all services in a single process."""
    # Ensure data directory exists (for SQLite)
    os.makedirs("data", exist_ok=True)

    logger.info("=" * 60)
    logger.info("Prediction Market Arbitrage System")
    logger.info("=" * 60)

    # Initialize database
    await init_db()
    logger.info("Database initialized", url=settings.database_url[:50] + "...")

    # Create in-process queue (replaces Redis)
    queue = asyncio.Queue()
    publisher = InProcessPublisher(queue)
    subscriber = InProcessSubscriber(queue)
    await subscriber.connect()

    # Create scanner
    scanner = ArbitrageScanner(publisher=publisher)

    logger.info(
        "Configuration",
        scan_interval=f"{settings.api_poll_interval_seconds}s",
        min_profit=f"{settings.min_net_spread_pct}%",
        llm_validation=settings.llm_validation_enabled and bool(settings.openai_api_key),
        discord=bool(settings.discord_bot_token),
        dashboard=f"http://0.0.0.0:{settings.api_port}",
    )

    # Run all services concurrently
    tasks = [
        asyncio.create_task(run_scheduler(publisher, scanner), name="scheduler"),
        asyncio.create_task(run_api(), name="api"),
        asyncio.create_task(run_discord_bot(subscriber), name="discord"),
    ]

    # Handle shutdown gracefully
    def shutdown(sig):
        logger.info("Shutting down...", signal=sig)
        for t in tasks:
            t.cancel()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown, sig.name)

    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        # Log any task failures that were silently swallowed
        for task, result in zip(tasks, results):
            if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                logger.error(
                    "Task failed",
                    task=task.get_name(),
                    error=str(result),
                    error_type=type(result).__name__,
                )
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("Shutdown complete")


if __name__ == "__main__":
    print(f"""
    ================================================
    Prediction Market Arbitrage System
    ================================================
    Dashboard:  http://jfk21.phoebe.usbx.me:{settings.api_port}/
    API docs:   http://jfk21.phoebe.usbx.me:{settings.api_port}/docs
    Discord:    {"Enabled (channel: " + str(settings.discord_channel_id) + ")" if settings.discord_bot_token else "NOT CONFIGURED (set DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID)"}
    LLM:        {"Enabled" if settings.openai_api_key else "NOT CONFIGURED (set OPENAI_API_KEY)"}
    Database:   {settings.database_url[:60]}
    Scan every: {settings.api_poll_interval_seconds}s
    ================================================
    """)
    asyncio.run(main())
