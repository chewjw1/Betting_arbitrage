"""Cross-platform arbitrage between Kalshi and sportsbooks.

Detects when prediction market prices on Kalshi diverge from
sportsbook odds (DraftKings, FanDuel, etc) enough to capture
guaranteed profit by hedging across platforms.

Key insight: Sportsbooks have vig (4-5% house edge). Kalshi is an
exchange (~1% spread). When Kalshi's price falls outside the
sportsbook's vig-adjusted range, true arb exists.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional
import structlog

from ..feeds.odds_api import GameOdds, american_to_decimal

logger = structlog.get_logger()


TEAM_NAME_MAPPINGS = {
    "los angeles lakers": ["LAL", "Lakers", "Los Angeles L"],
    "oklahoma city thunder": ["OKC", "Thunder", "Oklahoma City"],
    "new york knicks": ["NYK", "Knicks", "New York"],
    "philadelphia 76ers": ["PHI", "76ers", "Philadelphia"],
    "minnesota timberwolves": ["MIN", "Timberwolves", "Minnesota", "Wolves"],
    "san antonio spurs": ["SAS", "Spurs", "San Antonio"],
    "cleveland cavaliers": ["CLE", "Cavaliers", "Cleveland"],
    "toronto raptors": ["TOR", "Raptors", "Toronto"],
    "detroit pistons": ["DET", "Pistons", "Detroit"],
    "boston celtics": ["BOS", "Celtics", "Boston"],
    # NHL
    "minnesota wild": ["MIN", "Wild", "Minnesota"],
    "colorado avalanche": ["COL", "Avalanche", "Colorado"],
    "philadelphia flyers": ["PHI", "Flyers", "Philadelphia"],
    "carolina hurricanes": ["CAR", "Hurricanes", "Carolina"],
    "tampa bay lightning": ["TB", "Lightning", "Tampa Bay"],
    "montreal canadiens": ["MTL", "Canadiens", "Montreal"],
    "vegas golden knights": ["VGK", "Golden Knights", "Vegas"],
    "anaheim ducks": ["ANA", "Ducks", "Anaheim"],
    # MLB
    "toronto blue jays": ["TOR", "Blue Jays", "Toronto"],
    "tampa bay rays": ["TB", "Rays", "Tampa Bay"],
    "cleveland guardians": ["CLE", "Guardians", "Cleveland"],
    "kansas city royals": ["KC", "Royals", "Kansas City"],
    "texas rangers": ["TEX", "Rangers", "Texas"],
    "new york yankees": ["NYY", "Yankees", "New York Y"],
    "boston red sox": ["BOS", "Red Sox", "Boston"],
    "detroit tigers": ["DET", "Tigers", "Detroit"],
}


def normalize_team(name: str) -> str:
    """Normalize team name for matching."""
    return name.lower().strip()


def teams_match(team_a: str, team_b: str) -> bool:
    """Check if two team names refer to the same team."""
    a_norm = normalize_team(team_a)
    b_norm = normalize_team(team_b)

    if a_norm == b_norm:
        return True

    # Check mappings
    for full_name, aliases in TEAM_NAME_MAPPINGS.items():
        full_lower = full_name.lower()
        aliases_lower = [a.lower() for a in aliases]

        a_in = a_norm == full_lower or a_norm in aliases_lower or any(
            a_norm in alias.lower() or alias.lower() in a_norm
            for alias in aliases
        )
        b_in = b_norm == full_lower or b_norm in aliases_lower or any(
            b_norm in alias.lower() or alias.lower() in b_norm
            for alias in aliases
        )

        if a_in and b_in:
            return True

    return False


@dataclass
class CrossPlatformArb:
    """A detected cross-platform arbitrage opportunity."""
    detected_at: datetime
    game_description: str
    sport: str

    # Kalshi side
    kalshi_ticker: str
    kalshi_team: str
    kalshi_action: str  # "buy_yes" or "buy_no"
    kalshi_price: Decimal  # 0-1 (e.g., 0.88)
    kalshi_depth: Decimal  # USD available

    # Sportsbook side
    sportsbook: str
    sportsbook_team: str
    sportsbook_american_odds: int
    sportsbook_decimal_odds: float
    sportsbook_implied_prob: float

    # Analysis
    kalshi_implied_prob: float
    vig_free_sportsbook_prob: float  # Sportsbook prob minus vig
    edge_pct: float  # Edge after removing vig
    estimated_profit_pct: float  # Conservative estimate

    # Suggested trade
    suggested_kalshi_size: int  # Contracts
    suggested_sportsbook_stake: Decimal  # USD


class KalshiSportsbookArb:
    """Detects arb between Kalshi and sportsbooks.

    Strategy:
    1. Get Kalshi orderbook for game markets
    2. Get sportsbook odds via The-Odds-API
    3. Match games across platforms
    4. For each match, compute "true" probability after removing sportsbook vig
    5. Compare to Kalshi price
    6. If gap is large enough to overcome execution costs, flag as arb
    """

    def __init__(
        self,
        min_edge_pct: float = 3.0,  # Minimum % edge after vig removal
        min_kalshi_depth: float = 1000.0,  # Minimum $ depth on Kalshi side
        max_position_per_arb: float = 100.0,  # Max $ per arb
    ):
        self.min_edge_pct = min_edge_pct
        self.min_kalshi_depth = min_kalshi_depth
        self.max_position_per_arb = Decimal(str(max_position_per_arb))

    def find_arbs(
        self,
        kalshi_markets: list[dict],
        sportsbook_games: list[GameOdds],
        sportsbook_filter: Optional[list[str]] = None,
    ) -> list[CrossPlatformArb]:
        """Find arb opportunities.

        Args:
            kalshi_markets: List of {ticker, title, yes_bid, yes_ask, depth, team} dicts
            sportsbook_games: List of GameOdds from The-Odds-API
            sportsbook_filter: Limit to specific sportsbooks (e.g. ['draftkings'])
        """
        arbs = []

        for k_market in kalshi_markets:
            k_team = k_market.get("team", "")

            for game in sportsbook_games:
                # Check if Kalshi market is for one of these teams
                if teams_match(k_team, game.home_team):
                    k_is_home = True
                elif teams_match(k_team, game.away_team):
                    k_is_home = False
                else:
                    continue

                # Check each sportsbook
                for book_key in self._get_books_to_check(game, sportsbook_filter):
                    home_odds, away_odds = game.get_odds_for_book(book_key)
                    if not (home_odds and away_odds):
                        continue

                    # Get vig-free probabilities
                    vig_free = game.vig_free_probability(book_key)
                    if not vig_free:
                        continue

                    home_true_prob, away_true_prob = vig_free

                    # The "fair" probability for the Kalshi team
                    if k_is_home:
                        true_prob = home_true_prob
                        opposite_odds = away_odds
                        opposite_team = game.away_team
                    else:
                        true_prob = away_true_prob
                        opposite_odds = home_odds
                        opposite_team = game.home_team

                    arb = self._evaluate_pair(
                        k_market=k_market,
                        game=game,
                        sportsbook=book_key,
                        k_team_true_prob=true_prob,
                        opposite_odds=opposite_odds,
                        opposite_team=opposite_team,
                    )

                    if arb:
                        arbs.append(arb)

        return arbs

    def _get_books_to_check(
        self,
        game: GameOdds,
        filter_books: Optional[list[str]],
    ) -> set[str]:
        """Get set of sportsbook keys to evaluate."""
        all_books = set()
        for o in game.home_odds + game.away_odds:
            all_books.add(o.sportsbook)

        if filter_books:
            return all_books & set(filter_books)
        return all_books

    def _evaluate_pair(
        self,
        k_market: dict,
        game: GameOdds,
        sportsbook: str,
        k_team_true_prob: float,
        opposite_odds,
        opposite_team: str,
    ) -> Optional[CrossPlatformArb]:
        """Evaluate a Kalshi market vs sportsbook pair."""
        k_team = k_market.get("team", "")
        k_yes_bid = float(k_market.get("yes_bid", 0))
        k_yes_ask = float(k_market.get("yes_ask", 1))
        k_depth = float(k_market.get("depth", 0))

        if k_depth < self.min_kalshi_depth:
            return None

        # Strategy 1: Buy team on Kalshi (cheap), bet against on sportsbook
        # When: Kalshi YES ask < vig-free true probability
        if k_yes_ask > 0 and k_yes_ask < k_team_true_prob:
            kalshi_implied = k_yes_ask
            edge = (k_team_true_prob - kalshi_implied) * 100

            if edge >= self.min_edge_pct:
                # Calculate hedge sizing for guaranteed profit regardless of outcome
                # Buy X Kalshi YES contracts at k_yes_ask each
                # If team wins: receive $X (profit: X * (1 - k_yes_ask))
                # Bet S$ on opposite team at decimal odds D
                # If team loses: receive S * D (profit: S * (D - 1))
                # Hedge equation: X * (1 - k_yes_ask) = S * (D - 1)
                # So: S = X * (1 - k_yes_ask) / (D - 1)

                contracts = min(
                    int(self.max_position_per_arb / Decimal(str(k_yes_ask))),
                    int(k_depth / k_yes_ask) // 10,  # Use only 10% of depth
                )
                contracts = max(contracts, 1)

                kalshi_cost = Decimal(str(contracts * k_yes_ask))
                sportsbook_decimal = opposite_odds.decimal_odds
                sportsbook_stake = (
                    Decimal(str(contracts * (1 - k_yes_ask))) /
                    Decimal(str(sportsbook_decimal - 1))
                )

                # Net profit if team wins (receive $contracts, lose stake)
                profit_if_kalshi_wins = (
                    Decimal(str(contracts)) - kalshi_cost - sportsbook_stake
                )
                # Net profit if team loses (lose kalshi cost, receive stake * decimal)
                profit_if_kalshi_loses = (
                    sportsbook_stake * Decimal(str(sportsbook_decimal)) -
                    kalshi_cost - sportsbook_stake
                )

                min_profit = min(profit_if_kalshi_wins, profit_if_kalshi_loses)
                total_capital = kalshi_cost + sportsbook_stake
                profit_pct = float(min_profit / total_capital * 100) if total_capital > 0 else 0

                if profit_pct > 0:
                    return CrossPlatformArb(
                        detected_at=datetime.utcnow(),
                        game_description=f"{game.away_team} @ {game.home_team}",
                        sport=game.sport,
                        kalshi_ticker=k_market.get("ticker", ""),
                        kalshi_team=k_team,
                        kalshi_action="buy_yes",
                        kalshi_price=Decimal(str(k_yes_ask)),
                        kalshi_depth=Decimal(str(k_depth)),
                        sportsbook=sportsbook,
                        sportsbook_team=opposite_team,
                        sportsbook_american_odds=opposite_odds.american_odds,
                        sportsbook_decimal_odds=sportsbook_decimal,
                        sportsbook_implied_prob=opposite_odds.implied_probability,
                        kalshi_implied_prob=kalshi_implied,
                        vig_free_sportsbook_prob=k_team_true_prob,
                        edge_pct=edge,
                        estimated_profit_pct=profit_pct,
                        suggested_kalshi_size=contracts,
                        suggested_sportsbook_stake=sportsbook_stake,
                    )

        # Strategy 2: Sell team on Kalshi (expensive), bet for on sportsbook
        # When: Kalshi YES bid > vig-free true probability
        if k_yes_bid > k_team_true_prob:
            kalshi_implied = k_yes_bid
            edge = (kalshi_implied - k_team_true_prob) * 100

            if edge >= self.min_edge_pct:
                # This requires "selling" on Kalshi which means buying NO
                # Skipping for now - can add if needed
                pass

        return None


def kalshi_market_to_dict(market: dict, orderbook: dict, team: str) -> dict:
    """Convert Kalshi market + orderbook to detector input format."""
    yes_orders = orderbook.get("yes_dollars", [])
    no_orders = orderbook.get("no_dollars", [])

    yes_bid = max(float(o[0]) for o in yes_orders) if yes_orders else 0
    no_bid = max(float(o[0]) for o in no_orders) if no_orders else 0
    yes_ask = 1 - no_bid if no_bid > 0 else 1.0

    yes_depth = sum(float(o[1]) for o in yes_orders)
    no_depth = sum(float(o[1]) for o in no_orders)

    return {
        "ticker": market.get("ticker", ""),
        "title": market.get("title", ""),
        "team": team,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "depth": yes_depth + no_depth,
        "yes_depth": yes_depth,
        "no_depth": no_depth,
    }


def extract_team_from_kalshi_ticker(ticker: str) -> str:
    """Extract team abbreviation from Kalshi ticker.

    Examples:
        KXNBAGAME-26MAY05LALOKC-LAL -> LAL
        KXNHLGAME-26MAY04ANAVGK-VGK -> VGK
        KXMLBGAME-26MAY061940CLEKC-CLE -> CLE
    """
    parts = ticker.split("-")
    if parts:
        return parts[-1]
    return ""
