"""The-Odds-API client for sportsbook odds.

Aggregates odds from DraftKings, FanDuel, BetMGM, Caesars, etc.
Free tier: 500 requests/month at https://the-odds-api.com/

Usage:
    export ODDS_API_KEY="your_key_here"
    python -m src.sports_arb.feeds.odds_api
"""

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
import httpx
import structlog

logger = structlog.get_logger()

ODDS_API_BASE = "https://api.the-odds-api.com/v4"

SUPPORTED_SPORTS = {
    "basketball_nba": "NBA",
    "basketball_wnba": "WNBA",
    "basketball_ncaab": "NCAA Basketball",
    "icehockey_nhl": "NHL",
    "baseball_mlb": "MLB",
    "americanfootball_nfl": "NFL",
    "americanfootball_ncaaf": "NCAA Football",
    "soccer_uefa_champs_league": "UEFA Champions League",
    "soccer_epl": "Premier League",
    "mma_mixed_martial_arts": "MMA",
}


@dataclass
class SportsbookOdds:
    """Odds from a specific sportsbook for one team in one game."""
    sportsbook: str  # 'draftkings', 'fanduel', etc.
    team: str
    american_odds: int  # +340, -450, etc.
    decimal_odds: float
    implied_probability: float  # Without vig adjustment


@dataclass
class GameOdds:
    """Aggregated odds for a single game across sportsbooks."""
    sport: str
    sport_key: str
    event_id: str
    home_team: str
    away_team: str
    commence_time: datetime
    home_odds: list[SportsbookOdds]
    away_odds: list[SportsbookOdds]
    raw_data: dict

    @property
    def best_home_odds(self) -> Optional[SportsbookOdds]:
        """Best (highest payout) odds for home team."""
        return max(self.home_odds, key=lambda x: x.american_odds) if self.home_odds else None

    @property
    def best_away_odds(self) -> Optional[SportsbookOdds]:
        """Best odds for away team."""
        return max(self.away_odds, key=lambda x: x.american_odds) if self.away_odds else None

    def get_odds_for_book(self, sportsbook: str) -> tuple[Optional[SportsbookOdds], Optional[SportsbookOdds]]:
        """Get home/away odds for specific sportsbook."""
        home = next((o for o in self.home_odds if o.sportsbook == sportsbook), None)
        away = next((o for o in self.away_odds if o.sportsbook == sportsbook), None)
        return home, away

    def calculate_vig(self, sportsbook: str) -> Optional[float]:
        """Calculate the vig (overround) for a specific sportsbook."""
        home, away = self.get_odds_for_book(sportsbook)
        if home and away:
            return (home.implied_probability + away.implied_probability) - 1.0
        return None

    def vig_free_probability(self, sportsbook: str) -> Optional[tuple[float, float]]:
        """Get vig-removed (true) probabilities for home/away."""
        home, away = self.get_odds_for_book(sportsbook)
        if not (home and away):
            return None

        total = home.implied_probability + away.implied_probability
        return (
            home.implied_probability / total,
            away.implied_probability / total,
        )


def american_to_decimal(american: int) -> float:
    """Convert American odds to decimal odds."""
    if american > 0:
        return (american / 100) + 1
    else:
        return (100 / abs(american)) + 1


def american_to_implied_prob(american: int) -> float:
    """Convert American odds to implied probability (with vig)."""
    if american > 0:
        return 100 / (american + 100)
    else:
        return abs(american) / (abs(american) + 100)


def decimal_to_implied_prob(decimal: float) -> float:
    """Convert decimal odds to implied probability."""
    return 1 / decimal


class OddsAPIClient:
    """Client for The-Odds-API."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("ODDS_API_KEY")
        if not self.api_key:
            raise ValueError(
                "API key required. Get free key at https://the-odds-api.com/ "
                "and set ODDS_API_KEY environment variable"
            )
        self._client: Optional[httpx.AsyncClient] = None
        self._requests_used = 0
        self._requests_remaining = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=15.0)
        return self

    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()

    async def get_sports(self) -> list[dict]:
        """Get list of available sports."""
        resp = await self._client.get(
            f"{ODDS_API_BASE}/sports",
            params={"apiKey": self.api_key},
        )
        self._update_quota(resp)
        resp.raise_for_status()
        return resp.json()

    async def get_odds(
        self,
        sport: str,
        regions: str = "us",
        markets: str = "h2h",
        bookmakers: Optional[str] = None,
    ) -> list[GameOdds]:
        """Get odds for a sport.

        Args:
            sport: Sport key (e.g., 'basketball_nba')
            regions: 'us', 'uk', 'eu', 'au'
            markets: 'h2h' (moneyline), 'spreads', 'totals'
            bookmakers: Comma-separated list, e.g. 'draftkings,fanduel'
        """
        params = {
            "apiKey": self.api_key,
            "regions": regions,
            "markets": markets,
            "oddsFormat": "american",
            "dateFormat": "iso",
        }
        if bookmakers:
            params["bookmakers"] = bookmakers

        resp = await self._client.get(
            f"{ODDS_API_BASE}/sports/{sport}/odds",
            params=params,
        )
        self._update_quota(resp)

        if resp.status_code == 401:
            raise ValueError("Invalid API key")
        if resp.status_code == 422:
            logger.warning("invalid_sport_or_no_games", sport=sport)
            return []

        resp.raise_for_status()
        return self._parse_odds(resp.json(), sport)

    def _parse_odds(self, data: list[dict], sport_key: str) -> list[GameOdds]:
        """Parse API response into GameOdds objects."""
        games = []
        sport_name = SUPPORTED_SPORTS.get(sport_key, sport_key)

        for game in data:
            home_team = game.get("home_team", "")
            away_team = game.get("away_team", "")
            event_id = game.get("id", "")

            try:
                commence = datetime.fromisoformat(
                    game.get("commence_time", "").replace("Z", "+00:00")
                )
            except:
                commence = datetime.now(timezone.utc)

            home_odds = []
            away_odds = []

            for bookmaker in game.get("bookmakers", []):
                book_key = bookmaker.get("key", "")

                for market in bookmaker.get("markets", []):
                    if market.get("key") != "h2h":
                        continue

                    for outcome in market.get("outcomes", []):
                        team = outcome.get("name", "")
                        american = int(outcome.get("price", 0))

                        odds = SportsbookOdds(
                            sportsbook=book_key,
                            team=team,
                            american_odds=american,
                            decimal_odds=american_to_decimal(american),
                            implied_probability=american_to_implied_prob(american),
                        )

                        if team == home_team:
                            home_odds.append(odds)
                        elif team == away_team:
                            away_odds.append(odds)

            games.append(GameOdds(
                sport=sport_name,
                sport_key=sport_key,
                event_id=event_id,
                home_team=home_team,
                away_team=away_team,
                commence_time=commence,
                home_odds=home_odds,
                away_odds=away_odds,
                raw_data=game,
            ))

        return games

    def _update_quota(self, resp: httpx.Response) -> None:
        """Track API quota usage."""
        used = resp.headers.get("x-requests-used")
        remaining = resp.headers.get("x-requests-remaining")
        if used:
            self._requests_used = int(used)
        if remaining:
            self._requests_remaining = int(remaining)

    @property
    def quota(self) -> dict:
        return {
            "used": self._requests_used,
            "remaining": self._requests_remaining,
        }


async def main():
    """Quick test of the API."""
    print("=" * 60)
    print("THE-ODDS-API TEST")
    print("=" * 60)

    api_key = os.getenv("ODDS_API_KEY")
    if not api_key:
        print("\nNo ODDS_API_KEY set!")
        print("Sign up at https://the-odds-api.com/ for free key")
        print("Then: export ODDS_API_KEY='your_key'")
        return

    async with OddsAPIClient(api_key) as client:
        # Get NBA odds
        print("\nFetching NBA odds...")
        games = await client.get_odds("basketball_nba", bookmakers="draftkings,fanduel")

        print(f"Quota: {client.quota['used']} used, {client.quota['remaining']} remaining")
        print(f"\nFound {len(games)} NBA games")

        for game in games[:5]:
            print(f"\n{game.away_team} @ {game.home_team}")
            print(f"  Starts: {game.commence_time}")

            for book in ["draftkings", "fanduel"]:
                home, away = game.get_odds_for_book(book)
                if home and away:
                    vig = game.calculate_vig(book) * 100
                    true_probs = game.vig_free_probability(book)
                    print(f"  {book}:")
                    print(f"    {away.team}: {away.american_odds:+d} ({away.implied_probability*100:.1f}% w/vig, {true_probs[1]*100:.1f}% true)")
                    print(f"    {home.team}: {home.american_odds:+d} ({home.implied_probability*100:.1f}% w/vig, {true_probs[0]*100:.1f}% true)")
                    print(f"    Vig: {vig:.2f}%")


if __name__ == "__main__":
    asyncio.run(main())
