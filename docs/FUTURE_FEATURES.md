# Future Features & Enhancements

## High Priority (Quick Wins)

### 1. Historical Price Tracking
Track price movements over time to identify patterns.

```python
# New model: price_history
class PriceHistory(Base):
    market_id: str
    platform: str
    yes_price: Decimal
    no_price: Decimal
    timestamp: datetime
```

**Benefits:**
- See how long arbitrage windows stay open
- Identify best times to scan (e.g., market opens, news events)
- Backtest strategies

### 2. Market Liquidity Alerts
Current system doesn't check if there's enough liquidity to execute.

```python
# Add to collectors
"volume_24h": market.get("volume"),
"open_interest": market.get("openInterest"),
"depth_yes": {"$0.50": 500, "$0.51": 300},  # Order book depth
```

**Why it matters:** A 5% arbitrage is worthless if you can only get $10 through.

### 3. Resolution Date Filtering
Prioritize short-duration markets (higher annualized return).

```python
# In arbitrage detection
if days_to_resolution < 7:
    priority = "HIGH"  # Quick turnaround
elif days_to_resolution < 30:
    priority = "MEDIUM"
else:
    priority = "LOW"  # Capital locked too long
```

### 4. Discord Command Improvements
Add slash commands for manual control:

```
/scan now         - Trigger immediate scan
/status           - Show collector health
/history <market> - Show price history
/mute <hours>     - Pause notifications
/threshold <pct>  - Adjust threshold on the fly
```

## Medium Priority (Valuable Additions)

### 5. Execution Tracking
Track whether opportunities were successfully acted on.

```python
class OpportunityExecution(Base):
    opportunity_id: int
    executed: bool
    platform_a_order_id: str
    platform_b_order_id: str
    actual_fill_price_a: Decimal
    actual_fill_price_b: Decimal
    actual_profit: Decimal
    notes: str
```

**Benefits:**
- Calculate actual vs expected profits
- Identify slippage patterns
- Build performance metrics

### 6. Multi-Leg Arbitrage
Current system only looks at 2-platform opportunities. Extend to 3+.

```python
# Example: Buy YES on Kalshi, NO on Polymarket, hedge on PredictIt
def find_multi_leg_opportunities(markets_by_event):
    for event_id, platforms in markets_by_event.items():
        if len(platforms) >= 3:
            # Try all combinations
            for combo in itertools.combinations(platforms, 3):
                check_triangle_arbitrage(combo)
```

### 7. News/Event Integration
Scan faster when relevant news breaks.

```python
# Integrate with news APIs
async def on_news_event(event):
    if event.category in ["politics", "fed", "crypto"]:
        # Trigger immediate scan
        await run_full_scan()

# Sources: NewsAPI, Twitter/X API, RSS feeds
```

### 8. Paper Trading Mode
Test strategies without real money.

```python
# Simulate execution
class PaperTrader:
    def execute(self, opportunity):
        self.virtual_balance -= opportunity.cost
        self.open_positions.append({
            "opportunity": opportunity,
            "opened_at": datetime.now(),
        })

    def on_market_resolution(self, market_id, outcome):
        # Calculate P&L
```

## Lower Priority (Nice to Have)

### 9. Mobile Push Notifications
Discord isn't always the fastest. Add:
- Pushover integration
- Telegram bot
- SMS via Twilio (for high-value opportunities only)

### 10. Machine Learning Confidence
Train a model to predict if an opportunity is "real" vs noise.

Features:
- Historical accuracy of this platform pair
- Market liquidity
- Time since last price update
- Market category (politics, sports, crypto)
- Days to resolution

### 11. Auto-Execution (Advanced)
Automatically execute arbitrage trades.

**Warning:** This is high-risk. Only consider if:
- You have significant experience
- You've paper traded extensively
- You understand the regulatory implications
- You have proper risk limits

```python
class AutoExecutor:
    def __init__(self, max_position=100, dry_run=True):
        self.max_position = max_position
        self.dry_run = dry_run

    async def execute_if_valid(self, opportunity):
        if opportunity.net_spread_pct < self.min_threshold:
            return False
        if opportunity.estimated_profit > self.max_position:
            return False
        # Execute...
```

### 12. Web Dashboard Enhancements
Current dashboard is basic. Add:
- Real-time price charts (Chart.js/Plotly)
- P&L tracking over time
- Filter by platform, category, date range
- Export to CSV
- Dark mode

### 13. Platform Health Monitoring
Track if collectors are working properly.

```python
class HealthCheck:
    def check_collector(self, name):
        return {
            "last_success": self.last_success[name],
            "last_failure": self.last_failure[name],
            "failure_rate_1h": self.calculate_failure_rate(name),
            "avg_response_time": self.avg_response_time[name],
        }
```

## Architecture Improvements

### 14. Message Queue for Scalability
Replace direct function calls with Redis/RabbitMQ.

```
[Scheduler] --> [Redis Queue] --> [Worker Pool]
                                       |
                              [Kalshi] [Poly] [PI] ...
```

Benefits:
- Horizontal scaling
- Better failure isolation
- Replay failed jobs

### 15. Database Migration to TimescaleDB
For better time-series queries on price history.

```sql
-- Hypertable for prices
SELECT create_hypertable('price_history', 'timestamp');

-- Efficient queries
SELECT time_bucket('1 hour', timestamp) AS hour,
       avg(yes_price)
FROM price_history
WHERE market_id = 'xyz'
GROUP BY hour;
```

### 16. API Rate Limit Management
Smarter handling of API limits.

```python
class RateLimiter:
    limits = {
        "kalshi": {"requests": 10, "per_seconds": 1},
        "polymarket": {"requests": 100, "per_seconds": 60},
    }

    async def wait_if_needed(self, platform):
        # Adaptive backoff based on response headers
```

## Quick Implementation Checklist

If you want to improve the system incrementally:

1. **Day 1:** Add resolution date to market data
2. **Day 2:** Add liquidity/volume filtering
3. **Day 3:** Implement Discord slash commands
4. **Day 4:** Add price history tracking
5. **Day 5:** Build execution tracking
6. **Week 2:** Paper trading mode
7. **Week 3:** Dashboard improvements
8. **Month 2:** Consider auto-execution (carefully)

## Community/Open Source Ideas

- Share anonymized opportunity data (without execution details)
- Build a public dashboard showing "market efficiency" across platforms
- Create a Discord community for prediction market traders
- Open-source the matching algorithm (keep execution private)
