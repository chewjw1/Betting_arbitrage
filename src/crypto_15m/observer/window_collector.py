#!/usr/bin/env python3
"""Window-based collector with LLM analysis.

For each 15-minute window:
1. Captures all tick data (BTC price, Kalshi bid/ask, every second)
2. After settlement, calls Claude to analyze and categorize
3. Stores raw data + LLM summary in structured format

Categories the LLM will identify:
- Trend type: steady_up, steady_down, volatile, reversal, flat
- Key moments: breakouts, fakeouts, late rallies
- Predictability signals: early indicators that predicted outcome
"""

import asyncio
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import Optional
import time

import httpx

# Try to import anthropic, graceful fallback if not available
try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False
    print("Warning: anthropic package not installed. LLM analysis disabled.")
    print("Install with: pip install anthropic")

DATA_DIR = Path(__file__).parent.parent.parent.parent / "data" / "windows"
DATA_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class WindowTick:
    """Single tick within a window."""
    timestamp: float
    secs_remaining: int
    btc_price: float
    target_price: float
    distance_pct: float
    yes_bid: float
    yes_ask: float
    yes_mid: float


@dataclass
class WindowData:
    """Complete data for one 15-minute window."""
    window_id: str
    target_price: float
    start_time: str
    end_time: str
    ticks: list[WindowTick] = field(default_factory=list)

    # Computed after window closes
    outcome: Optional[str] = None  # "UP" or "DOWN"
    open_btc: Optional[float] = None
    close_btc: Optional[float] = None
    high_btc: Optional[float] = None
    low_btc: Optional[float] = None
    btc_range_pct: Optional[float] = None

    # LLM analysis
    llm_summary: Optional[str] = None
    llm_category: Optional[str] = None
    llm_patterns: Optional[list] = None
    llm_predictability: Optional[str] = None


@dataclass
class LLMAnalysis:
    """Structured LLM analysis of a window."""
    category: str  # steady_up, steady_down, volatile, reversal, late_rally, flat
    summary: str  # 2-3 sentence description
    patterns: list[str]  # Key patterns observed
    predictability: str  # early, mid, late, unpredictable
    confidence_curve: str  # Description of how market confidence evolved
    key_moments: list[dict]  # Notable price/market movements


class WindowCollector:
    """Collect and analyze 15-minute windows."""

    BINANCE_REST = "https://api.binance.us/api/v3"
    KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

    def __init__(self, anthropic_api_key: Optional[str] = None):
        self.api_key = anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.current_window: Optional[WindowData] = None
        self._running = False

    async def start(self):
        """Start collecting windows."""
        print("=" * 70)
        print("WINDOW COLLECTOR WITH LLM ANALYSIS")
        print("=" * 70)
        print(f"\nData directory: {DATA_DIR}")
        print(f"LLM analysis: {'Enabled' if HAS_ANTHROPIC and self.api_key else 'Disabled'}")
        print("\nPress Ctrl+C to stop\n")

        self._running = True

        async with httpx.AsyncClient(timeout=5) as client:
            while self._running:
                try:
                    await self._collection_cycle(client)
                except Exception as e:
                    print(f"Error: {e}")

                await asyncio.sleep(1)

    async def _collection_cycle(self, client: httpx.AsyncClient):
        """One collection cycle."""
        ts = time.time()

        # Get BTC price
        btc_resp = await client.get(
            f"{self.BINANCE_REST}/ticker/price",
            params={"symbol": "BTCUSDT"}
        )
        btc_price = float(btc_resp.json()["price"]) if btc_resp.status_code == 200 else 0

        # Get Kalshi market
        kalshi_resp = await client.get(
            f"{self.KALSHI_API}/markets",
            params={"series_ticker": "KXBTC15M", "status": "open", "limit": 5}
        )

        market = None
        if kalshi_resp.status_code == 200:
            markets = kalshi_resp.json().get("markets", [])
            active = [m for m in markets if m.get("status") == "active"]
            if active:
                market = active[0]

        if not market or btc_price == 0:
            return

        window_id = market.get("ticker", "")
        target = float(market.get("floor_strike", 0) or 0)
        yes_bid = float(market.get("yes_bid_dollars", 0) or 0)
        yes_ask = float(market.get("yes_ask_dollars", 0) or 0)

        close_time = datetime.fromisoformat(
            market.get("close_time", "").replace("Z", "+00:00")
        )
        secs_remaining = max(0, int((close_time - datetime.now(timezone.utc)).total_seconds()))

        # New window?
        if self.current_window is None or self.current_window.window_id != window_id:
            # Finalize previous window
            if self.current_window and len(self.current_window.ticks) > 0:
                await self._finalize_window()

            # Start new window
            self.current_window = WindowData(
                window_id=window_id,
                target_price=target,
                start_time=datetime.now(timezone.utc).isoformat(),
                end_time="",
            )
            print(f"\n{'='*60}")
            print(f"NEW WINDOW: {window_id}")
            print(f"Target: ${target:,.2f}")
            print(f"{'='*60}")

        # Add tick
        distance_pct = (btc_price - target) / target * 100 if target > 0 else 0

        tick = WindowTick(
            timestamp=ts,
            secs_remaining=secs_remaining,
            btc_price=btc_price,
            target_price=target,
            distance_pct=distance_pct,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            yes_mid=(yes_bid + yes_ask) / 2,
        )
        self.current_window.ticks.append(tick)

        # Display
        direction = "↑" if distance_pct > 0 else "↓"
        print(
            f"{datetime.now().strftime('%H:%M:%S')} | "
            f"BTC: ${btc_price:,.0f} {direction}{distance_pct:+.3f}% | "
            f"T-{secs_remaining:3}s | "
            f"Yes: {yes_bid*100:.0f}/{yes_ask*100:.0f}c | "
            f"Ticks: {len(self.current_window.ticks)}"
        )

    async def _finalize_window(self):
        """Finalize and analyze completed window."""
        window = self.current_window
        if not window or len(window.ticks) < 10:
            return

        window.end_time = datetime.now(timezone.utc).isoformat()

        # Compute stats
        btc_prices = [t.btc_price for t in window.ticks]
        window.open_btc = btc_prices[0]
        window.close_btc = btc_prices[-1]
        window.high_btc = max(btc_prices)
        window.low_btc = min(btc_prices)
        window.btc_range_pct = (window.high_btc - window.low_btc) / window.target_price * 100
        window.outcome = "UP" if window.close_btc >= window.target_price else "DOWN"

        print(f"\n{'='*60}")
        print(f"WINDOW SETTLED: {window.window_id}")
        print(f"Outcome: {window.outcome}")
        print(f"Target: ${window.target_price:,.2f}")
        print(f"Close: ${window.close_btc:,.2f} ({(window.close_btc-window.target_price)/window.target_price*100:+.3f}%)")
        print(f"Range: {window.btc_range_pct:.3f}%")
        print(f"Ticks: {len(window.ticks)}")

        # LLM Analysis
        if HAS_ANTHROPIC and self.api_key:
            print("\nAnalyzing with Claude...")
            analysis = await self._analyze_with_llm(window)
            if analysis:
                window.llm_summary = analysis.summary
                window.llm_category = analysis.category
                window.llm_patterns = analysis.patterns
                window.llm_predictability = analysis.predictability

                print(f"\nCategory: {analysis.category}")
                print(f"Predictability: {analysis.predictability}")
                print(f"Summary: {analysis.summary}")
                if analysis.patterns:
                    print(f"Patterns: {', '.join(analysis.patterns)}")

        # Save window data
        self._save_window(window)
        print(f"Saved to: {DATA_DIR / f'{window.window_id}.json'}")
        print("=" * 60)

    async def _analyze_with_llm(self, window: WindowData) -> Optional[LLMAnalysis]:
        """Analyze window with Claude."""
        if not HAS_ANTHROPIC or not self.api_key:
            return None

        # Prepare data summary for LLM
        ticks = window.ticks

        # Sample key moments (every 60 seconds + first/last)
        sampled_ticks = []
        for i, tick in enumerate(ticks):
            if i == 0 or i == len(ticks) - 1 or i % 60 == 0:
                sampled_ticks.append({
                    "secs_left": tick.secs_remaining,
                    "btc": tick.btc_price,
                    "distance_pct": round(tick.distance_pct, 4),
                    "yes_mid": round(tick.yes_mid * 100, 1),
                })

        # Calculate key metrics
        yes_prices = [t.yes_mid for t in ticks]
        btc_prices = [t.btc_price for t in ticks]

        # Find biggest moves
        biggest_btc_move = 0
        biggest_yes_move = 0
        for i in range(1, len(ticks)):
            btc_move = abs(ticks[i].btc_price - ticks[i-1].btc_price)
            yes_move = abs(ticks[i].yes_mid - ticks[i-1].yes_mid)
            biggest_btc_move = max(biggest_btc_move, btc_move)
            biggest_yes_move = max(biggest_yes_move, yes_move)

        prompt = f"""Analyze this 15-minute crypto prediction market window and categorize the behavior.

WINDOW DATA:
- Target price: ${window.target_price:,.2f}
- Outcome: {window.outcome}
- Open BTC: ${window.open_btc:,.2f}
- Close BTC: ${window.close_btc:,.2f}
- High: ${window.high_btc:,.2f}, Low: ${window.low_btc:,.2f}
- Price range: {window.btc_range_pct:.3f}%
- Total ticks: {len(ticks)}

KEY MOMENTS (sampled every 60s):
{json.dumps(sampled_ticks, indent=2)}

MARKET BEHAVIOR:
- YES price range: {min(yes_prices)*100:.0f}c to {max(yes_prices)*100:.0f}c
- Biggest BTC move between ticks: ${biggest_btc_move:.2f}
- Biggest YES move between ticks: {biggest_yes_move*100:.1f}c

Respond with a JSON object containing:
{{
    "category": "one of: steady_up, steady_down, volatile, reversal, late_rally, late_drop, grinding, flat",
    "summary": "2-3 sentence description of what happened",
    "patterns": ["list", "of", "key", "patterns", "observed"],
    "predictability": "one of: early (outcome clear by min 5), mid (clear by min 10), late (clear by min 13), unpredictable",
    "confidence_curve": "describe how market confidence (YES price) evolved",
    "key_moments": [
        {{"time": "T-Xs", "event": "description"}}
    ]
}}

Only respond with the JSON, no other text."""

        try:
            client = anthropic.Anthropic(api_key=self.api_key)
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1000,
                messages=[{"role": "user", "content": prompt}]
            )

            # Parse response
            text = response.content[0].text
            # Try to extract JSON
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0]
            elif "```" in text:
                text = text.split("```")[1].split("```")[0]

            data = json.loads(text.strip())

            return LLMAnalysis(
                category=data.get("category", "unknown"),
                summary=data.get("summary", ""),
                patterns=data.get("patterns", []),
                predictability=data.get("predictability", "unknown"),
                confidence_curve=data.get("confidence_curve", ""),
                key_moments=data.get("key_moments", []),
            )

        except Exception as e:
            print(f"LLM analysis error: {e}")
            return None

    def _save_window(self, window: WindowData):
        """Save window data to JSON file."""
        filename = DATA_DIR / f"{window.window_id}.json"

        # Convert to dict
        data = {
            "window_id": window.window_id,
            "target_price": window.target_price,
            "start_time": window.start_time,
            "end_time": window.end_time,
            "outcome": window.outcome,
            "open_btc": window.open_btc,
            "close_btc": window.close_btc,
            "high_btc": window.high_btc,
            "low_btc": window.low_btc,
            "btc_range_pct": window.btc_range_pct,
            "tick_count": len(window.ticks),
            "llm_summary": window.llm_summary,
            "llm_category": window.llm_category,
            "llm_patterns": window.llm_patterns,
            "llm_predictability": window.llm_predictability,
            "ticks": [asdict(t) for t in window.ticks],
        }

        with open(filename, 'w') as f:
            json.dump(data, f, indent=2)

    def stop(self):
        """Stop collection."""
        self._running = False


async def main():
    # Check for API key
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("\nNote: Set ANTHROPIC_API_KEY env var to enable LLM analysis")
        print("Example: export ANTHROPIC_API_KEY='your-key-here'\n")

    collector = WindowCollector(anthropic_api_key=api_key)

    try:
        await collector.start()
    except KeyboardInterrupt:
        print("\n\nStopping...")
        collector.stop()

        # Show collected windows
        windows = list(DATA_DIR.glob("*.json"))
        print(f"\nCollected {len(windows)} windows")

        if windows:
            print("\nRecent windows:")
            for w in sorted(windows)[-5:]:
                with open(w) as f:
                    data = json.load(f)
                print(f"  {data['window_id'][-15:]}: {data['outcome']} | {data.get('llm_category', 'no analysis')}")


if __name__ == "__main__":
    asyncio.run(main())
