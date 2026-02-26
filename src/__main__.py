"""Combined entry point: API server + Scheduler + Discord bot in one process.

Usage:
    python -m src                    # Run everything (API + scheduler + Discord)
    python -m src --no-discord       # Run API + scheduler only
    python -m src --scheduler-only   # Run scheduler only (no API)
    python -m src --api-only         # Run API only (no scheduler)
"""

import argparse
import asyncio
import signal
import sys
import threading
from typing import Optional

import structlog
import uvicorn

from src.config import get_settings
from src.database import init_db

logger = structlog.get_logger()
settings = get_settings()

# Global shutdown event
shutdown_event = asyncio.Event()


async def run_scheduler(publisher=None):
    """Run the arbitrage scanner on a schedule."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    from src.scheduler.jobs import ArbitrageScanner

    scanner = ArbitrageScanner(publisher=publisher)
    scheduler = AsyncIOScheduler()

    # Add scan job
    scheduler.add_job(
        scanner.run_scan,
        IntervalTrigger(minutes=settings.api_poll_interval_seconds // 60 or 10),
        id="arbitrage_scan",
        name="Arbitrage Scan",
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()
    logger.info(
        "Scheduler started",
        interval_minutes=settings.api_poll_interval_seconds // 60 or 10,
    )

    # Run initial scan
    try:
        await scanner.run_scan()
    except Exception as e:
        logger.error("Initial scan failed", error=str(e))

    # Wait for shutdown
    await shutdown_event.wait()
    scheduler.shutdown()
    logger.info("Scheduler stopped")


async def run_discord_bot(subscriber):
    """Run the Discord bot for notifications."""
    try:
        from src.notifications.discord_bot import ArbitrageBot

        if not settings.discord_bot_token:
            logger.warning("DISCORD_BOT_TOKEN not set, skipping Discord bot")
            return

        bot = ArbitrageBot(subscriber=subscriber)
        async with bot:
            await bot.start(settings.discord_bot_token)
    except ImportError:
        logger.warning("Discord bot not available (missing discord.py?)")
    except Exception as e:
        logger.error("Discord bot failed", error=str(e))


def run_api_server():
    """Run the FastAPI server in a thread."""
    config = uvicorn.Config(
        "src.api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level="info",
        access_log=False,  # Reduce noise
    )
    server = uvicorn.Server(config)

    # Run in current thread
    asyncio.run(server.serve())


async def main(
    run_api: bool = True,
    run_sched: bool = True,
    run_discord: bool = True,
):
    """Main entry point combining all services.

    Args:
        run_api: Whether to run the API server.
        run_sched: Whether to run the scheduler.
        run_discord: Whether to run the Discord bot.
    """
    logger.info(
        "Starting combined services",
        api=run_api,
        scheduler=run_sched,
        discord=run_discord,
    )

    # Initialize database
    await init_db()

    # Set up signal handlers
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: shutdown_event.set())

    # Set up in-process pub/sub (no Redis needed for combined process)
    publisher = None
    subscriber = None
    notification_queue: Optional[asyncio.Queue] = None

    if run_sched or run_discord:
        from src.notifications.redis_bridge import InProcessPublisher, InProcessSubscriber
        notification_queue = asyncio.Queue()
        publisher = InProcessPublisher(notification_queue)
        subscriber = InProcessSubscriber(notification_queue)
        await publisher.connect()
        await subscriber.connect()
        logger.info("In-process notification queue initialized (no Redis needed)")

    tasks = []

    # Start API server in a thread (uvicorn has its own event loop)
    api_thread: Optional[threading.Thread] = None
    if run_api:
        api_thread = threading.Thread(target=run_api_server, daemon=True)
        api_thread.start()
        logger.info(
            "API server started",
            host=settings.api_host,
            port=settings.api_port,
            url=f"http://{settings.api_host}:{settings.api_port}",
        )

    # Start scheduler
    if run_sched:
        tasks.append(asyncio.create_task(run_scheduler(publisher)))

    # Start Discord bot
    if run_discord and subscriber:
        tasks.append(asyncio.create_task(run_discord_bot(subscriber)))

    # Wait for all tasks or shutdown
    if tasks:
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass

    # Cleanup
    if publisher:
        await publisher.close()
    if subscriber:
        await subscriber.close()

    logger.info("All services stopped")


def cli():
    """Command-line interface."""
    parser = argparse.ArgumentParser(
        description="Prediction Market Arbitrage System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m src                    Run everything (API + scheduler + Discord)
  python -m src --no-discord       Run API + scheduler only
  python -m src --scheduler-only   Run scheduler only
  python -m src --api-only         Run API only
        """,
    )
    parser.add_argument(
        "--no-discord",
        action="store_true",
        help="Disable Discord bot",
    )
    parser.add_argument(
        "--scheduler-only",
        action="store_true",
        help="Run scheduler only (no API)",
    )
    parser.add_argument(
        "--api-only",
        action="store_true",
        help="Run API only (no scheduler)",
    )

    args = parser.parse_args()

    # Determine what to run
    run_api = True
    run_sched = True
    run_discord = not args.no_discord

    if args.scheduler_only:
        run_api = False
        run_discord = False
    elif args.api_only:
        run_sched = False
        run_discord = False

    try:
        asyncio.run(main(run_api=run_api, run_sched=run_sched, run_discord=run_discord))
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(0)


if __name__ == "__main__":
    cli()
