#!/usr/bin/env python3
"""Scan for arb between Kalshi and sportsbooks.

Usage:
    export ODDS_API_KEY="your_key"
    python scripts/scan_kalshi_sportsbook.py

    # Specific sport
    python scripts/scan_kalshi_sportsbook.py --sport basketball_nba

    # Specific sportsbook
    python scripts/scan_kalshi_sportsbook.py --books draftkings,fanduel

    # Lower edge threshold
    python scripts/scan_kalshi_sportsbook.py --min-edge 1.0
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
import structlog

from src.sports_arb.feeds.odds_api import OddsAPIClient, SUPPORTED_SPORTS
from src.sports_arb.detector.kalshi_sportsbook import (
    KalshiSportsbookArb,
    kalshi_market_to_dict,
    extract_team_from_kalshi_ticker,
)

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.dev.ConsoleRenderer(),
    ]
)
logger = structlog.get_logger()


SPORT_TO_KALSHI_SERIES = {
    "basketball_nba": "KXNBAGAME",
    "basketball_wnba": "KXWNBAGAME",
    "icehockey_nhl": "KXNHLGAME",
    "baseball_mlb": "KXMLBGAME",
    "americanfootball_nfl": "KXNFLGAME",
    "soccer_uefa_champs_league": "KXUCLGAME",
    "soccer_epl": "KXEPLGAME",
}


async def fetch_kalshi_markets(client: httpx.AsyncClient, series: str) -> list[dict]:
    """Fetch all open markets in a Kalshi series with orderbooks."""
    resp = await client.get(
        "https://api.elections.kalshi.com/trade-api/v2/markets",
        params={"series_ticker": series, "limit": 100, "status": "open"},
    )
    if resp.status_code != 200:
        return []

    markets = resp.json().get("markets", [])
    enriched = []

    # Get orderbook for each
    tasks = []
    for m in markets:
        ticker = m.get("ticker", "")
        if ticker:
            tasks.append(_fetch_orderbook(client, ticker, m))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if not isinstance(r, Exception) and r:
            enriched.append(r)

    return enriched


async def _fetch_orderbook(
    client: httpx.AsyncClient,
    ticker: str,
    market: dict,
) -> dict:
    """Fetch orderbook and convert to detector format."""
    resp = await client.get(
        f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}/orderbook"
    )
    if resp.status_code != 200:
        return None

    book = resp.json().get("orderbook_fp", {})
    team = extract_team_from_kalshi_ticker(ticker)
    return kalshi_market_to_dict(market, book, team)


async def main(args):
    print("=" * 70)
    print("KALSHI vs SPORTSBOOK ARB SCANNER")
    print("=" * 70)
    print(f"Sport: {args.sport} ({SUPPORTED_SPORTS.get(args.sport, args.sport)})")
    print(f"Sportsbooks: {args.books or 'all'}")
    print(f"Min edge: {args.min_edge}%")
    print(f"Min Kalshi depth: ${args.min_depth:,.0f}")
    print()

    api_key = os.getenv("ODDS_API_KEY")
    if not api_key:
        print("ERROR: ODDS_API_KEY not set!")
        print("Sign up at https://the-odds-api.com/ for free key")
        print("Then: export ODDS_API_KEY='your_key'")
        return

    series = SPORT_TO_KALSHI_SERIES.get(args.sport)
    if not series:
        print(f"ERROR: No Kalshi series mapping for {args.sport}")
        print(f"Supported: {list(SPORT_TO_KALSHI_SERIES.keys())}")
        return

    async with httpx.AsyncClient(timeout=30.0) as kalshi_client:
        # Fetch in parallel
        print("Fetching Kalshi markets...")
        kalshi_task = fetch_kalshi_markets(kalshi_client, series)

        async with OddsAPIClient(api_key) as odds_client:
            print("Fetching sportsbook odds...")
            odds_task = odds_client.get_odds(
                args.sport,
                bookmakers=args.books,
            )

            kalshi_markets, sportsbook_games = await asyncio.gather(
                kalshi_task,
                odds_task,
            )

            print(f"\nKalshi markets: {len(kalshi_markets)}")
            print(f"Sportsbook games: {len(sportsbook_games)}")
            print(f"API quota: {odds_client.quota['used']} used, {odds_client.quota['remaining']} remaining")

        # Show what we found
        if not kalshi_markets:
            print(f"\nNo Kalshi markets found for {series}")
            return

        if not sportsbook_games:
            print(f"\nNo sportsbook games found for {args.sport}")
            return

        print("\nMatching teams...")
        for game in sportsbook_games[:5]:
            print(f"  {game.away_team} @ {game.home_team} ({game.commence_time.strftime('%Y-%m-%d %H:%M')})")

        # Detect arbs
        detector = KalshiSportsbookArb(
            min_edge_pct=args.min_edge,
            min_kalshi_depth=args.min_depth,
            max_position_per_arb=args.max_position,
        )

        arbs = detector.find_arbs(
            kalshi_markets=kalshi_markets,
            sportsbook_games=sportsbook_games,
            sportsbook_filter=args.books.split(",") if args.books else None,
        )

        # Display results
        print("\n" + "=" * 70)
        print(f"ARBITRAGE OPPORTUNITIES: {len(arbs)}")
        print("=" * 70)

        if not arbs:
            print("\nNo arb opportunities meet threshold.")
            print("Try lowering --min-edge or check during active game hours.")

            # Show closest near-misses
            print("\nDoing a wider scan to show near-misses...")
            wider = KalshiSportsbookArb(
                min_edge_pct=0.0,
                min_kalshi_depth=args.min_depth,
                max_position_per_arb=args.max_position,
            )
            near = wider.find_arbs(
                kalshi_markets=kalshi_markets,
                sportsbook_games=sportsbook_games,
                sportsbook_filter=args.books.split(",") if args.books else None,
            )
            near.sort(key=lambda a: -a.edge_pct)
            print(f"\nTop 5 near-misses (edge below threshold):")
            for arb in near[:5]:
                print(f"  {arb.game_description}: {arb.kalshi_team} - edge {arb.edge_pct:.1f}%")
            return

        for i, arb in enumerate(arbs, 1):
            print(f"\n[{i}] {arb.game_description} ({arb.sport})")
            print(f"    Kalshi: BUY {arb.suggested_kalshi_size}x {arb.kalshi_team} @ {float(arb.kalshi_price)*100:.1f}c")
            print(f"            Ticker: {arb.kalshi_ticker}")
            print(f"            Cost: ${float(arb.kalshi_price) * arb.suggested_kalshi_size:.2f}")
            print(f"    {arb.sportsbook}: BET ${arb.suggested_sportsbook_stake:.2f} on {arb.sportsbook_team} @ {arb.sportsbook_american_odds:+d}")
            print(f"    Edge: {arb.edge_pct:.2f}% (after vig removal)")
            print(f"    Estimated profit: {arb.estimated_profit_pct:.2f}% of capital")
            print(f"    Total capital: ${float(arb.kalshi_price) * arb.suggested_kalshi_size + float(arb.suggested_sportsbook_stake):.2f}")


def cli():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sport",
        default="basketball_nba",
        choices=list(SPORT_TO_KALSHI_SERIES.keys()),
        help="Sport to scan",
    )
    parser.add_argument(
        "--books",
        type=str,
        default="draftkings,fanduel",
        help="Comma-separated sportsbook keys",
    )
    parser.add_argument(
        "--min-edge",
        type=float,
        default=2.0,
        help="Minimum edge %% after vig removal",
    )
    parser.add_argument(
        "--min-depth",
        type=float,
        default=1000.0,
        help="Minimum Kalshi depth in USD",
    )
    parser.add_argument(
        "--max-position",
        type=float,
        default=100.0,
        help="Max position size in USD per arb",
    )

    asyncio.run(main(parser.parse_args()))


if __name__ == "__main__":
    cli()
