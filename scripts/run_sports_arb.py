#!/usr/bin/env python3
"""Run the live sports arbitrage scanner.

Usage:
    # Phase 0: Measurement only (log opportunities, no trades)
    python scripts/run_sports_arb.py

    # Paper trading mode
    python scripts/run_sports_arb.py --mode paper

    # Live trading (requires API keys in environment)
    python scripts/run_sports_arb.py --mode live

Environment variables for live trading:
    KALSHI_API_KEY
    KALSHI_PRIVATE_KEY
    POLYMARKET_API_KEY
    POLYMARKET_API_SECRET
    POLYMARKET_PASSPHRASE
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import structlog
from src.sports_arb import SportsArbRunner
from src.sports_arb.matcher import MarketMatcher

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.dev.ConsoleRenderer(),
    ]
)
logger = structlog.get_logger()


async def main(args):
    """Main entry point."""
    print("=" * 60)
    print("Live Sports Arbitrage Scanner")
    print("=" * 60)
    print(f"Mode: {args.mode}")
    print(f"Min profit: {args.min_profit}%")
    print(f"Max position: ${args.max_position}")
    print()

    runner = SportsArbRunner(
        mode=args.mode,
        min_profit_pct=args.min_profit,
        max_position_usd=args.max_position,
        log_dir=args.log_dir,
        kalshi_api_key=os.getenv("KALSHI_API_KEY"),
        kalshi_private_key=os.getenv("KALSHI_PRIVATE_KEY"),
        polymarket_api_key=os.getenv("POLYMARKET_API_KEY"),
        polymarket_api_secret=os.getenv("POLYMARKET_API_SECRET"),
        polymarket_passphrase=os.getenv("POLYMARKET_PASSPHRASE"),
    )

    print("Connecting to feeds...")
    await runner.kalshi_feed.connect()
    await runner.polymarket_feed.connect()

    if args.auto_match:
        print("\nAuto-matching markets across platforms...")
        async with MarketMatcher() as matcher:
            matches = await matcher.find_matches(
                min_confidence=args.match_confidence,
                categories=args.categories.split(",") if args.categories else None,
            )

        if not matches:
            print("No matching markets found!")
            return

        print(f"Found {len(matches)} matching markets")

        market_pairs = [
            {
                "event_key": m.event_key,
                "title": m.title,
                "kalshi_market_id": m.kalshi_market_id,
                "polymarket_token_id": m.polymarket_token_id,
            }
            for m in matches
            if m.kalshi_market_id and m.polymarket_token_id
        ]

        if not market_pairs:
            print("No valid market pairs to monitor!")
            return

        print(f"Registering {len(market_pairs)} market pairs...")
        await runner.register_markets(market_pairs)

    print("\n" + "-" * 60)
    print("Scanner running. Press Ctrl+C to stop.")
    print("-" * 60 + "\n")

    try:
        await runner.start()
    except KeyboardInterrupt:
        print("\nShutting down...")
        await runner.stop()

    print("\n" + "=" * 60)
    print("Session Summary")
    print("=" * 60)
    print(f"Opportunities logged: {runner._opportunities_logged}")
    print(f"Trades executed: {runner._trades_executed}")
    print(f"Total P&L: ${runner._total_pnl}")
    print(f"Log file: {runner.log_dir}")


def cli():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Live Sports Arbitrage Scanner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--mode",
        choices=["measurement", "paper", "live"],
        default="measurement",
        help="Operating mode (default: measurement)",
    )

    parser.add_argument(
        "--min-profit",
        type=float,
        default=0.5,
        help="Minimum profit percentage to trigger (default: 0.5)",
    )

    parser.add_argument(
        "--max-position",
        type=float,
        default=100.0,
        help="Maximum position size in USD (default: 100)",
    )

    parser.add_argument(
        "--log-dir",
        default="./arb_logs",
        help="Directory for log files (default: ./arb_logs)",
    )

    parser.add_argument(
        "--auto-match",
        action="store_true",
        default=True,
        help="Automatically match markets across platforms (default: True)",
    )

    parser.add_argument(
        "--match-confidence",
        type=float,
        default=0.5,
        help="Minimum confidence for market matching (default: 0.5)",
    )

    parser.add_argument(
        "--categories",
        type=str,
        default=None,
        help="Comma-separated list of categories to filter (e.g., 'sports,crypto')",
    )

    args = parser.parse_args()

    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        print("\nInterrupted")
        sys.exit(0)


if __name__ == "__main__":
    cli()
