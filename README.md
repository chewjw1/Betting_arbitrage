# Prediction Market Arbitrage System

A system for detecting arbitrage opportunities across prediction markets (Kalshi, Polymarket, PredictIt, DraftKings) with Discord notifications and a web dashboard.

## Features

- **Multi-Platform Data Collection**: Fetch market data from Kalshi, Polymarket, and PredictIt
- **Cross-Platform Arbitrage Detection**: Identify price discrepancies for the same events across platforms
- **Fee-Aware Calculations**: Account for platform-specific trading fees in profit calculations
- **Discord Notifications**: Real-time alerts when profitable opportunities are detected
- **User Response Tracking**: Track which opportunities you acted on vs passed
- **Web Dashboard**: View all opportunities, filter by status, and analyze performance

## Quick Start

### Prerequisites

- Python 3.11+
- PostgreSQL 16+
- Redis 7+
- Docker & Docker Compose (recommended)

### Setup

1. **Clone and configure**:
   ```bash
   cd Betting_arbitrage
   cp .env.example .env
   # Edit .env with your API keys and settings
   ```

2. **Get API Credentials**:
   - **Kalshi**: Create account at [kalshi.com](https://kalshi.com), generate API key in settings
   - **Discord**: Create bot at [Discord Developer Portal](https://discord.com/developers/applications)

3. **Start with Docker**:
   ```bash
   docker-compose up -d
   ```

   Or **manual setup**:
   ```bash
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt

   # Start services
   python -m src.scheduler.jobs  # Data collector
   python -m src.notifications.discord_bot  # Discord bot
   python -m src.api.main  # Dashboard API
   ```

4. **Access the dashboard**: http://localhost:8000/docs

## Configuration

Key environment variables in `.env`:

| Variable | Description | Default |
|----------|-------------|---------|
| `KALSHI_API_KEY` | Kalshi API key | Required |
| `KALSHI_PRIVATE_KEY_PATH` | Path to Kalshi RSA private key | `./kalshi_private_key.pem` |
| `DISCORD_BOT_TOKEN` | Discord bot token | Required for alerts |
| `DISCORD_CHANNEL_ID` | Channel ID for alerts | Required for alerts |
| `MIN_NET_SPREAD_PCT` | Minimum net profit % to alert | `1.0` |
| `MAX_POSITION_SIZE` | Max $ per side | `500` |
| `POLL_INTERVAL_SECONDS` | Scan frequency | `30` |

## Platform Status

| Platform | API Access | Trading | Categories | Notes |
|----------|-----------|---------|------------|-------|
| Kalshi | ✅ Full REST | ✅ Yes | Politics, Economics, Crypto | Main platform for US users |
| Polymarket | ✅ REST API | ⚠️ Complex | All | US access restored, monitor ToS |
| PredictIt | ✅ Read-only API | ❌ Manual | Politics only | No trading API, good for price comparison |
| DraftKings | ✅ REST API | ❌ Manual | Economics, Sports | Reverse-engineered API |
| IBKR ForecastTrader | 🔜 Planned | ✅ Yes | All | Requires Client Portal auth, zero commission |
| Robinhood/Coinbase | ❌ Skip | N/A | Same as Kalshi | Uses Kalshi backend (same order book) |

## Fee Structure

| Platform | Trading Fee | Profit Fee |
|----------|------------|------------|
| Kalshi | ~1.2% | ~2% on profits |
| Polymarket (US) | 0.01% | 0% |
| PredictIt | 0% | 10% on profits + 5% withdrawal |

## How Arbitrage Works

1. **Same-Event Arbitrage**: When YES on Platform A + NO on Platform B < $1.00
   - Example: Kalshi YES at $0.45, Polymarket NO at $0.48
   - Total cost: $0.93 for guaranteed $1.00 return
   - Gross profit: 7%, minus fees

2. **Logical Arbitrage**: Related bets with inconsistent probabilities
   - Mutually exclusive outcomes that don't sum to 100%
   - Temporal inconsistencies (earlier date > later date)

## API Endpoints

- `GET /api/v1/opportunities` - List all opportunities
- `GET /api/v1/opportunities/{id}` - Get specific opportunity
- `PATCH /api/v1/opportunities/{id}` - Update (mark as acted/passed)
- `GET /api/v1/stats` - Dashboard statistics
- `GET /api/v1/markets` - List tracked markets

## Discord Commands

- `/status` - Check bot status
- `/threshold [value]` - View/set minimum profit threshold
- `/test` - Send a test notification
- `/help` - Show help

React to notifications:
- ✅ - Mark as acted upon
- ❌ - Mark as passed

## Project Structure

```
src/
├── collectors/     # Platform API clients
├── matching/       # Market title matching
├── arbitrage/      # Profit calculations
├── notifications/  # Discord bot
├── database/       # SQLAlchemy models
├── api/           # FastAPI dashboard
└── scheduler/     # Job scheduling
```

## Important Warnings

1. **Resolution Risk**: Different platforms may resolve the same event differently. Always verify resolution criteria match before taking a position.

2. **Legal Considerations**: Prediction market legality varies by state. Verify compliance with Georgia regulations.

3. **Capital Lock-up**: Funds may be locked until market resolution (weeks/months).

4. **Not Financial Advice**: This is a tool for identifying opportunities. All trading decisions are your own.

## TODO / Roadmap

### High Priority
- [ ] **Discord Integration Testing** - Verify reaction handling (✅/❌) updates opportunity status in database
- [ ] **Dashboard Review** - Confirm opportunities display with market URLs for quick execution
- [ ] **Kalshi Authenticated API** - Add RSA key auth for full market access and better rate limits

### Medium Priority
- [ ] **IBKR ForecastTrader API** - Implement Client Portal API (requires IBKR account)
  - Zero commission = great for small spreads
  - Endpoint: `GET /v1/api/trsrv/event/category-tree`
- [ ] **Better fee modeling** - Add slippage estimation based on order book depth
- [ ] **Historical analysis** - Price movement patterns to identify best scan times

### Nice to Have
- [ ] **Auto-execution** - Trading API integration for Kalshi/Polymarket
- [ ] **Mobile notifications** - Push notifications via Pushover/Telegram
- [ ] **Backtesting** - Test strategies against historical data

### Completed
- [x] Multi-platform API collectors (Kalshi, Polymarket, PredictIt, DraftKings)
- [x] LLM validation for cross-platform matching (GPT-4o-mini with persistent caching)
- [x] Fee-aware arbitrage calculations
- [x] Discord notifications with deduplication
- [x] Web dashboard with auto-refresh
- [x] Persistent LLM cache to avoid re-validating same pairs

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Format code
black src/ tests/
ruff check src/ tests/

# Type checking
mypy src/
```

## Resources

- [Kalshi API Docs](https://docs.kalshi.com/welcome)
- [Polymarket Docs](https://docs.polymarket.com/)
- [PredictIt API](https://www.predictit.org/api/marketdata/all/)
- [EventArb Calculator](https://www.eventarb.com/)

## License

MIT License - See LICENSE file
