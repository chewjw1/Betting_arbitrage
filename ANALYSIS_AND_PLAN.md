# Prediction Market Arbitrage System - Analysis & Implementation Plan

## Executive Summary

This document outlines the research, pressure testing, and implementation plan for a prediction market arbitrage system targeting platforms available in Georgia, USA.

---

## Part 1: Platform Research & API Availability

### Platform Comparison Matrix

| Platform | US Legal Status | Georgia Available | API Type | Auth Required | Fees | Real Money |
|----------|----------------|-------------------|----------|---------------|------|------------|
| **Kalshi** | CFTC-regulated DCM | ✅ Yes | REST + WebSocket + FIX | Yes (API key + RSA) | ~1.2-2% on profits | Yes |
| **Polymarket** | Relaunched US late 2025 | ⚠️ Complex (see notes) | REST + WebSocket | Read: No, Trade: Yes | 0.01% US | Yes |
| **PredictIt** | CFTC no-action letter | ✅ Yes | REST (read-only) | No | ~10% on profits | Yes (winding down) |
| **Robinhood** | Via Kalshi partnership | ✅ Yes | No public API | N/A | Included in spread | Yes |
| **Manifold Markets** | N/A | ✅ Yes | Full REST API | Yes for trading | N/A | No (play money) |
| **Metaculus** | N/A | ✅ Yes | API available | Varies | N/A | No (forecasting only) |

### Detailed Platform Analysis

#### 1. Kalshi (PRIMARY - Best for arbitrage)
- **Status**: Fully CFTC-regulated, operates in 46 states (blocked in IL, NV, NJ, OH)
- **API Documentation**: https://docs.kalshi.com/welcome
- **API Features**:
  - REST API for market data, orders, portfolio
  - WebSocket for real-time streaming (order books, trades)
  - FIX 4.4 for institutional/high-frequency trading
  - Python client: `kalshi-python` on PyPI
- **Authentication**: RSA key-based, tokens expire every 30 minutes
- **Rate Limits**: Documented in their API docs
- **Data Available**: Order books, market stats, trades, portfolio history
- **Fee Structure**: ~1.2% trading fee, ~2% on profits
- **Markets**: Sports (87% of volume), elections, economics, events

#### 2. Polymarket (SECONDARY - Legal complexity)
- **Status**: Re-entered US market late 2025 after acquiring QCEX (CFTC-licensed DCM)
- **API Documentation**: https://docs.polymarket.com/
- **API Components**:
  - **Gamma API**: Market metadata, indexed volume, categories
  - **CLOB API**: Order book, trading, market prices
  - **Data API**: On-chain activity, historical data
- **Authentication**: Public read-only; trading requires API key generation
- **Platform**: Built on Polygon blockchain
- **Fee Structure**: 0.01% for US (when fully launched), 0% international
- **⚠️ CRITICAL ISSUE**: ToS technically prohibits US persons from trading. Data is viewable globally. The platform's US legal status is evolving - monitor closely.

#### 3. PredictIt (TERTIARY - Limited but useful)
- **Status**: Operating under CFTC no-action letter, winding down some markets
- **API Endpoint**: https://www.predictit.org/api/marketdata/all/
- **Limitations**:
  - Read-only (no trading via API)
  - Updates every ~60 seconds
  - 1 request/second rate limit
  - No historical data via API (CSV downloads available)
  - No WebSocket
- **Fee Structure**: ~10% on profits + 5% withdrawal
- **Best Use**: Price monitoring, arbitrage detection (manual execution)

#### 4. Robinhood Prediction Markets
- **Status**: Launched October 2024, rapidly growing (11B+ contracts traded)
- **Integration**: Via Kalshi partnership
- **API**: **No public API available** for prediction markets
- **Workaround Options**:
  - Use Kalshi API (same underlying markets)
  - Web scraping (ToS violation risk)
  - Monitor for future API release

#### 5. Manifold Markets (TESTING/RESEARCH)
- **Status**: Play money only (real money sunset March 2025)
- **API**: Full REST API at api.manifold.markets
- **Python client**: `manifoldpy` on PyPI
- **Best Use**: Testing arbitrage logic, backtesting strategies

---

## Part 2: Pressure Testing the Concept

### Critical Risks & Challenges

#### 1. Resolution Risk (HIGHEST PRIORITY)
**Problem**: Different platforms may resolve the SAME event differently.

**Real Example (2024)**: A US government shutdown market resolved "Yes" on Polymarket but "No" on Kalshi for the same event. Holding opposite positions = **total loss**.

**Mitigation**:
- Carefully compare resolution criteria before entering positions
- Build resolution criteria comparison into the matching algorithm
- Flag markets with potentially different resolution rules
- Consider only taking arbitrage on markets with identical resolution language

#### 2. Fee Erosion
**Problem**: Platform fees can turn a 3% gross arbitrage into 1-2% net, or a loss.

**Fee Impact Analysis**:
```
Example: 3% gross arbitrage opportunity
- Kalshi fee: ~1.2% on entry + 2% on profit
- Polymarket US: 0.01%

Scenario: $1000 on each side
- Gross profit: $30
- Kalshi fees: ~$12 + ~$0.60 = ~$12.60
- Polymarket fees: ~$0.10
- Net profit: ~$17.30 (1.73% return)

Break-even point: ~1.3% gross spread required
```

**Mitigation**:
- Build accurate fee calculator into opportunity detection
- Set minimum spread threshold accounting for fees
- Track actual vs expected returns to refine models

#### 3. Liquidity Constraints
**Problem**: Displayed prices may only be available for small quantities.

**Mitigation**:
- Check order book depth, not just top-of-book prices
- Calculate expected slippage for target position sizes
- Set maximum position sizes per market
- Use limit orders when possible

#### 4. Execution Latency
**Problem**: Prices can move between detection and execution.

**Typical latencies**:
- API polling: 1-5 seconds (REST)
- WebSocket: <100ms
- Human reaction: 30+ seconds

**Mitigation**:
- Use WebSocket feeds where available
- Build confidence intervals for price stability
- Consider automated execution (requires more capital, risk)

#### 5. Capital Lock-up
**Problem**: Markets may not resolve for weeks/months.

**Impact**:
- A 2% profit over 3 months = ~8% annualized
- A 2% profit locked for 6 months = ~4% annualized

**Mitigation**:
- Prioritize short-duration markets
- Track capital efficiency (return/time)
- Set maximum capital allocation per market

#### 6. Regulatory Risk
**Problem**: Platform legal status is evolving (19 lawsuits against Kalshi alone).

**Mitigation**:
- Monitor regulatory news
- Don't over-concentrate on any single platform
- Understand state-specific restrictions

#### 7. Logical Arbitrage Complexity
**Problem**: "Related bets" require understanding complex probability relationships.

**Examples**:
- "Trump wins" + "Republican wins" should have specific relationship
- "Event A by March" should be ≤ "Event A by June"
- Multiple mutually exclusive outcomes should sum to 100%

**Mitigation**:
- Start with simpler same-market arbitrage
- Build probability consistency checker as Phase 2
- Require higher spreads for logical arbitrage (more uncertainty)

### Viability Assessment

| Factor | Assessment | Score |
|--------|------------|-------|
| Market existence | Opportunities exist, documented by others | ✅ High |
| API accessibility | Kalshi + Polymarket have good APIs | ✅ High |
| Fee impact | Reduces but doesn't eliminate opportunities | ⚠️ Medium |
| Execution feasibility | Manual execution viable, automation complex | ⚠️ Medium |
| Resolution risk | Significant, requires careful matching | ⚠️ Medium |
| Capital requirements | ~$2-10K recommended for meaningful returns | ⚠️ Medium |
| Legal risk | Complex but manageable for personal use | ⚠️ Medium |

**Overall Viability**: MODERATE-HIGH for manual/semi-automated arbitrage with careful market selection.

**Realistic Expectations**:
- Gross spreads: 1-5% (typical), 5-15% (rare events)
- Net returns after fees: 0.5-3% per trade
- Frequency: 5-20 opportunities per week (varying quality)
- Monthly return potential: 2-8% on active capital (estimate)

---

## Part 3: Implementation Plan

### System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     Data Collection Layer                        │
├─────────────────────────────────────────────────────────────────┤
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐        │
│  │  Kalshi  │  │Polymarket│  │ PredictIt│  │  Manifold│        │
│  │ Collector│  │ Collector│  │ Collector│  │(testing) │        │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘        │
│       │             │             │             │               │
│       └─────────────┼─────────────┼─────────────┘               │
│                     ▼                                           │
│           ┌─────────────────┐                                   │
│           │ Market Normalizer│                                  │
│           │ (Event Matching) │                                  │
│           └────────┬────────┘                                   │
└────────────────────┼────────────────────────────────────────────┘
                     ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Arbitrage Detection Engine                    │
├─────────────────────────────────────────────────────────────────┤
│  ┌──────────────────┐  ┌──────────────────┐                     │
│  │ Cross-Platform   │  │ Logical/Related  │                     │
│  │ Same-Event Arb   │  │ Bet Arbitrage    │                     │
│  └────────┬─────────┘  └────────┬─────────┘                     │
│           │                     │                               │
│           └──────────┬──────────┘                               │
│                      ▼                                          │
│           ┌─────────────────┐                                   │
│           │  Fee Calculator │                                   │
│           │ & Net Profit    │                                   │
│           └────────┬────────┘                                   │
└────────────────────┼────────────────────────────────────────────┘
                     ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Notification & Logging                        │
├─────────────────────────────────────────────────────────────────┤
│  ┌──────────────────┐  ┌──────────────────┐                     │
│  │ Discord Bot      │  │ PostgreSQL DB    │                     │
│  │ (Alerts + Input) │  │ (Opportunity Log)│                     │
│  └────────┬─────────┘  └────────┬─────────┘                     │
│           │                     │                               │
│           └──────────┬──────────┘                               │
│                      ▼                                          │
│           ┌─────────────────┐                                   │
│           │ Dashboard (Web) │                                   │
│           │ FastAPI + React │                                   │
│           └─────────────────┘                                   │
└─────────────────────────────────────────────────────────────────┘
```

### Technology Stack

| Component | Technology | Rationale |
|-----------|------------|-----------|
| Language | Python 3.11+ | Best API client support, existing libraries |
| Data Collection | asyncio + aiohttp | Concurrent API calls |
| Database | PostgreSQL + SQLAlchemy | Structured data, good querying |
| Discord Bot | discord.py | Mature, well-documented |
| Web Dashboard | FastAPI + React/Next.js | Fast API, modern UI |
| Scheduling | APScheduler or Celery | Reliable task scheduling |
| Caching | Redis | Real-time price caching |

### Implementation Phases

#### Phase 1: Foundation (Week 1-2)
**Goal**: Basic data collection and storage

1. **Project Setup**
   - Repository structure
   - Environment configuration
   - Docker setup for local development

2. **Kalshi Integration**
   - Authentication flow
   - Market data fetching
   - WebSocket connection for real-time data

3. **PredictIt Integration** (simpler, good for testing)
   - REST API polling
   - Data normalization

4. **Database Schema**
   - Markets table
   - Prices/quotes table
   - Opportunities table
   - Actions table (user responses)

**Deliverable**: Can fetch and store market data from 2 platforms

#### Phase 2: Arbitrage Detection (Week 2-3)
**Goal**: Identify and calculate opportunities

1. **Market Matching Algorithm**
   - Fuzzy string matching for event titles
   - Manual override/confirmation system
   - Resolution criteria comparison

2. **Same-Event Arbitrage Calculator**
   - Cross-platform price comparison
   - Fee-adjusted profit calculation
   - Minimum spread threshold

3. **Opportunity Logging**
   - Store all detected opportunities
   - Track price evolution
   - Calculate time-to-expire

**Deliverable**: System identifies and logs arbitrage opportunities

#### Phase 3: Discord Integration (Week 3-4)
**Goal**: Real-time notifications and user input

1. **Discord Bot Setup**
   - Bot creation and server setup
   - Command handling

2. **Alert System**
   - Formatted opportunity notifications
   - Include: platforms, prices, net profit, links
   - Configurable minimum spread threshold

3. **Response Tracking**
   - Reaction-based input (✅ acted, ❌ passed)
   - Alternative: slash commands
   - Store user decisions

**Deliverable**: Working Discord notifications with response tracking

#### Phase 4: Dashboard (Week 4-5)
**Goal**: Visual interface for opportunity review

1. **Backend API**
   - FastAPI endpoints
   - Filtering and pagination
   - Statistics endpoints

2. **Frontend Dashboard**
   - Opportunity list view
   - Filter by: platform, status, date, acted/not-acted
   - Charts: opportunity frequency, profit potential over time
   - Individual opportunity detail view

**Deliverable**: Functional web dashboard

#### Phase 5: Polymarket Integration (Week 5-6)
**Goal**: Add Polymarket as data source

1. **API Integration**
   - Gamma API for market data
   - CLOB API for order book data
   - Handle blockchain-specific data

2. **Cross-Platform Matching**
   - Kalshi ↔ Polymarket event matching
   - Resolution criteria flagging

**Deliverable**: Three-platform arbitrage detection

#### Phase 6: Logical Arbitrage (Week 6-8)
**Goal**: Detect probability inconsistencies

1. **Relationship Definitions**
   - Same event, different timeframes
   - Mutually exclusive outcomes
   - Conditional probabilities

2. **Inconsistency Detection**
   - Probability sum validation
   - Temporal consistency checks
   - Cross-event relationship detection

**Deliverable**: Logical arbitrage detection working

#### Phase 7: Optimization & Monitoring (Ongoing)
- Performance tuning
- False positive reduction
- Execution tracking
- P&L reporting

---

## Part 4: Project Structure

```
betting_arbitrage/
├── README.md
├── docker-compose.yml
├── .env.example
├── requirements.txt
├── pyproject.toml
│
├── src/
│   ├── __init__.py
│   ├── config.py                 # Configuration management
│   │
│   ├── collectors/               # Data collection
│   │   ├── __init__.py
│   │   ├── base.py              # Abstract collector
│   │   ├── kalshi.py            # Kalshi API client
│   │   ├── polymarket.py        # Polymarket API client
│   │   ├── predictit.py         # PredictIt API client
│   │   └── manifold.py          # Manifold API client (testing)
│   │
│   ├── matching/                 # Event matching
│   │   ├── __init__.py
│   │   ├── fuzzy_matcher.py     # Fuzzy string matching
│   │   ├── resolution_checker.py # Resolution criteria comparison
│   │   └── manual_overrides.py   # Manual match confirmations
│   │
│   ├── arbitrage/                # Arbitrage detection
│   │   ├── __init__.py
│   │   ├── calculator.py        # Profit calculations
│   │   ├── cross_platform.py    # Same-event arbitrage
│   │   ├── logical.py           # Related-bet arbitrage
│   │   └── fees.py              # Fee calculations per platform
│   │
│   ├── notifications/            # Alert system
│   │   ├── __init__.py
│   │   ├── discord_bot.py       # Discord integration
│   │   └── formatters.py        # Message formatting
│   │
│   ├── database/                 # Data persistence
│   │   ├── __init__.py
│   │   ├── models.py            # SQLAlchemy models
│   │   ├── session.py           # DB session management
│   │   └── migrations/          # Alembic migrations
│   │
│   ├── api/                      # Dashboard API
│   │   ├── __init__.py
│   │   ├── main.py              # FastAPI app
│   │   ├── routes/
│   │   │   ├── opportunities.py
│   │   │   ├── markets.py
│   │   │   └── stats.py
│   │   └── schemas.py           # Pydantic models
│   │
│   └── scheduler/                # Task scheduling
│       ├── __init__.py
│       └── jobs.py              # Scheduled tasks
│
├── dashboard/                    # Frontend (Next.js/React)
│   ├── package.json
│   ├── src/
│   │   ├── components/
│   │   ├── pages/
│   │   └── lib/
│   └── ...
│
├── tests/
│   ├── __init__.py
│   ├── test_collectors/
│   ├── test_matching/
│   ├── test_arbitrage/
│   └── fixtures/
│
└── scripts/
    ├── setup_db.py
    └── seed_data.py
```

---

## Part 5: Database Schema

```sql
-- Markets from each platform
CREATE TABLE markets (
    id UUID PRIMARY KEY,
    platform VARCHAR(50) NOT NULL,  -- 'kalshi', 'polymarket', 'predictit'
    platform_market_id VARCHAR(255) NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    resolution_criteria TEXT,
    category VARCHAR(100),
    end_date TIMESTAMP,
    status VARCHAR(50),  -- 'open', 'closed', 'resolved'
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(platform, platform_market_id)
);

-- Price snapshots
CREATE TABLE prices (
    id UUID PRIMARY KEY,
    market_id UUID REFERENCES markets(id),
    yes_price DECIMAL(10, 4),
    no_price DECIMAL(10, 4),
    yes_volume DECIMAL(20, 2),
    no_volume DECIMAL(20, 2),
    timestamp TIMESTAMP DEFAULT NOW()
);

-- Matched markets across platforms
CREATE TABLE matched_markets (
    id UUID PRIMARY KEY,
    market_a_id UUID REFERENCES markets(id),
    market_b_id UUID REFERENCES markets(id),
    match_confidence DECIMAL(5, 4),  -- 0.0 to 1.0
    match_method VARCHAR(50),  -- 'automatic', 'manual'
    resolution_compatible BOOLEAN,
    notes TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Detected arbitrage opportunities
CREATE TABLE opportunities (
    id UUID PRIMARY KEY,
    matched_market_id UUID REFERENCES matched_markets(id),
    opportunity_type VARCHAR(50),  -- 'cross_platform', 'logical'

    -- Platform A details
    platform_a VARCHAR(50),
    market_a_id UUID REFERENCES markets(id),
    side_a VARCHAR(10),  -- 'yes' or 'no'
    price_a DECIMAL(10, 4),

    -- Platform B details
    platform_b VARCHAR(50),
    market_b_id UUID REFERENCES markets(id),
    side_b VARCHAR(10),
    price_b DECIMAL(10, 4),

    -- Calculations
    gross_spread DECIMAL(10, 4),
    estimated_fees DECIMAL(10, 4),
    net_profit_pct DECIMAL(10, 4),

    -- Status
    detected_at TIMESTAMP DEFAULT NOW(),
    expired_at TIMESTAMP,
    status VARCHAR(50) DEFAULT 'active',  -- 'active', 'expired', 'executed'

    -- User action
    user_acted BOOLEAN,
    user_action_at TIMESTAMP,
    user_notes TEXT,

    -- Actual results (if executed)
    actual_profit DECIMAL(20, 2),
    actual_fees DECIMAL(20, 2)
);

-- Discord notification log
CREATE TABLE notifications (
    id UUID PRIMARY KEY,
    opportunity_id UUID REFERENCES opportunities(id),
    discord_message_id VARCHAR(50),
    sent_at TIMESTAMP DEFAULT NOW(),
    response_received_at TIMESTAMP,
    response VARCHAR(50)  -- 'acted', 'passed', 'no_response'
);
```

---

## Part 6: Fee Calculation Details

```python
# Fee structures by platform (as of Jan 2026)

PLATFORM_FEES = {
    'kalshi': {
        'trading_fee_pct': 0.012,    # 1.2% per trade
        'profit_fee_pct': 0.02,      # 2% on profits
        'deposit_fee': 0,            # Free bank/wire
        'withdrawal_fee': 0,         # Free bank/wire
        'debit_deposit_pct': 0.02,   # 2% for debit
        'debit_withdrawal': 2.00,    # $2 flat
    },
    'polymarket_us': {
        'trading_fee_pct': 0.0001,   # 0.01%
        'profit_fee_pct': 0,
        'deposit_fee': 0,            # Via USDC
        'withdrawal_fee': 0,         # Network fees only
    },
    'polymarket_intl': {
        'trading_fee_pct': 0,        # Free
        'profit_fee_pct': 0,
    },
    'predictit': {
        'trading_fee_pct': 0,
        'profit_fee_pct': 0.10,      # 10% on profits
        'withdrawal_pct': 0.05,      # 5% withdrawal
    },
}

def calculate_net_arbitrage(
    platform_a: str,
    price_a: float,  # e.g., 0.45 for 45 cents
    platform_b: str,
    price_b: float,  # e.g., 0.52 for 52 cents
    position_size: float = 100.0  # dollars
) -> dict:
    """
    Calculate net profit from arbitrage opportunity.

    Assumes buying YES on cheaper platform, NO on expensive platform.
    """
    # Ensure price_a is the lower price (buy YES here)
    if price_a > price_b:
        platform_a, platform_b = platform_b, platform_a
        price_a, price_b = price_b, price_a

    fees_a = PLATFORM_FEES[platform_a]
    fees_b = PLATFORM_FEES[platform_b]

    # Gross spread
    gross_spread = price_b - price_a

    # Position: Buy YES at price_a, buy NO at (1 - price_b)
    cost_yes = position_size * price_a
    cost_no = position_size * (1 - price_b)
    total_cost = cost_yes + cost_no

    # If event resolves YES: win $1 per contract from platform_a
    # If event resolves NO: win $1 per contract from platform_b
    guaranteed_return = position_size  # Always win one side

    gross_profit = guaranteed_return - total_cost

    # Calculate fees
    trading_fees = (
        cost_yes * fees_a['trading_fee_pct'] +
        cost_no * fees_b['trading_fee_pct']
    )

    profit_fees = (
        gross_profit * fees_a.get('profit_fee_pct', 0) * 0.5 +  # Estimate
        gross_profit * fees_b.get('profit_fee_pct', 0) * 0.5
    )

    total_fees = trading_fees + profit_fees
    net_profit = gross_profit - total_fees

    return {
        'gross_spread': gross_spread,
        'gross_profit': gross_profit,
        'total_fees': total_fees,
        'net_profit': net_profit,
        'net_profit_pct': net_profit / total_cost * 100,
        'break_even_spread': total_fees / position_size,
    }
```

---

## Part 7: Risk Mitigation Checklist

### Before Going Live
- [ ] Test with Manifold (play money) first
- [ ] Verify API authentication works for all platforms
- [ ] Confirm Georgia residency requirements met
- [ ] Start with small position sizes ($50-100)
- [ ] Set up monitoring for API rate limits
- [ ] Create manual override system for market matching

### Per-Opportunity Checklist
- [ ] Verify resolution criteria match between platforms
- [ ] Check order book depth for desired position size
- [ ] Confirm both markets are still active
- [ ] Verify no recent news that could cause price movement
- [ ] Calculate exact fees for your position size
- [ ] Ensure net profit exceeds minimum threshold (suggest 1%+)

### Ongoing Operations
- [ ] Monitor regulatory news weekly
- [ ] Track actual vs expected returns
- [ ] Review false positives monthly
- [ ] Rotate API keys periodically
- [ ] Backup database regularly

---

## Part 8: Getting Started

### Immediate Next Steps

1. **Account Setup**
   - Create Kalshi account, generate API keys
   - Create Polymarket account (if US trading available)
   - Join Kalshi Discord #dev channel
   - Join Polymarket Discord

2. **Development Environment**
   - Set up Python environment
   - Install platform client libraries
   - Configure PostgreSQL
   - Create Discord bot

3. **Initial Implementation**
   - Start with Kalshi + PredictIt (simplest API combination)
   - Build basic data collection
   - Implement simple matching algorithm
   - Create Discord notification prototype

### Recommended Starting Capital
- **Testing Phase**: $0 (Manifold play money)
- **Initial Live**: $500-1,000
- **Scaled Operation**: $5,000-10,000

### Time Investment Estimate
- Phase 1-4 (MVP): 4-5 weeks part-time
- Phase 5-6 (Full system): 2-3 additional weeks
- Ongoing maintenance: 2-4 hours/week

---

## Sources

### Platform Documentation
- [Kalshi API Docs](https://docs.kalshi.com/welcome)
- [Kalshi Help Center](https://help.kalshi.com/kalshi-api)
- [Polymarket Documentation](https://docs.polymarket.com/)
- [PredictIt API](https://www.predictit.org/api/marketdata/all/)
- [Manifold API Docs](https://docs.manifold.markets/api)

### Tools & Libraries
- [kalshi-python PyPI](https://pypi.org/project/kalshi-python/)
- [polymarket-apis PyPI](https://pypi.org/project/polymarket-apis/)
- [manifoldpy PyPI](https://github.com/vluzko/manifoldpy)
- [EventArb Calculator](https://www.eventarb.com/)
- [GetArbitrageBets](https://getarbitragebets.com/)

### News & Analysis
- [GPB: Kalshi Lawsuits](https://www.gpb.org/news/2026/01/30/kalshi-in-court-over-19-federal-lawsuits-whats-the-future-of-prediction-markets)
- [NPR: How Traders Make Money](https://www.npr.org/2026/01/17/nx-s1-5672615/kalshi-polymarket-prediction-market-boom-traders-slang-glossary)
- [Polymarket vs Kalshi Comparison](https://www.polytrackhq.app/blog/polymarket-vs-kalshi-comparison)
- [Limits of Arbitrage Analysis](https://rajivsethi.substack.com/p/the-limits-of-arbitrage)
- [Front Office Sports: Prediction Markets 2025](https://frontofficesports.com/prediction-markets-exploded-in-2025-what-comes-next/)
- [Robinhood Prediction Markets](https://robinhood.com/us/en/prediction-markets/)
