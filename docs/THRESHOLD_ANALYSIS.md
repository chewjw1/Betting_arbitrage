# Threshold Analysis: Is 3% the Right Minimum?

## TL;DR

**3% NET profit is a reasonable starting threshold**, but consider:
- Start at 2-3% to catch more opportunities
- Increase to 4-5% if too many false positives or resolution risk concerns
- Different thresholds for different arbitrage types

## Fee Breakdown by Platform

| Platform | Trading Fee | Profit Fee | Withdrawal | Effective Total |
|----------|-------------|------------|------------|-----------------|
| Kalshi | 1.2% | 2% on profit | 0% | ~2-3% |
| Polymarket US | 0.01% | 0% | ~0% | ~0.01% |
| PredictIt | 0% | 10% on profit | 5% | ~12-15% |
| DraftKings | ~1% | 0% | 0% | ~1% |
| FanDuel | ~1% | 0% | 0% | ~1% |
| IBKR | 0% | 0% | 0% | **0%** |

## Minimum Viable Arbitrage by Platform Pair

| Pair | Combined Fees | Min Gross Needed | Recommended Net Threshold |
|------|---------------|------------------|---------------------------|
| Kalshi ↔ Polymarket | ~2-3% | 3-4% | **2%** |
| Kalshi ↔ PredictIt | ~14-17% | 17-20% | **5%** (risky) |
| Kalshi ↔ IBKR | ~2-3% | 3-4% | **2%** |
| Polymarket ↔ IBKR | ~0% | 1% | **1%** |
| DraftKings ↔ FanDuel | ~2% | 3% | **2%** |

## Risk-Adjusted Thresholds

### Low Risk (same resolution criteria)
- Threshold: **2% net**
- Example: Kalshi ↔ IBKR for same Fed rate market

### Medium Risk (similar but not identical markets)
- Threshold: **3% net**
- Example: Kalshi "Bitcoin above $100k" ↔ Polymarket "BTC $100k+"

### High Risk (different resolution criteria possible)
- Threshold: **5% net**
- Example: Any cross-platform political market
- Reason: 2024 shutdown market resolved differently on Polymarket vs Kalshi

## Recommended Configuration

```bash
# Conservative (fewer alerts, higher quality)
MIN_NET_SPREAD_PCT=3.0

# Aggressive (more alerts, review carefully)
MIN_NET_SPREAD_PCT=2.0

# PredictIt-involved (must be higher due to fees)
# Consider platform-specific thresholds in code
```

## What 3% Net Actually Means

With $500 per side ($1000 total):

| Gross Spread | Fees (~3%) | Net Spread | Net Profit |
|--------------|------------|------------|------------|
| 4% | ~$10 | 3% | **$15** |
| 5% | ~$12 | 3.5% | **$17.50** |
| 6% | ~$15 | 4% | **$20** |

**At 3% threshold, expect $15-30 profit per opportunity on $1000.**

## Frequency Expectations

Based on market research:

| Threshold | Expected Frequency | Quality |
|-----------|-------------------|---------|
| 1% net | 10-30/week | Many false positives |
| 2% net | 5-15/week | Good, some noise |
| **3% net** | **2-8/week** | **High quality** |
| 5% net | 0-3/week | Rare, very high quality |

## Capital Efficiency Consideration

Arbitrage locks capital until resolution. Consider:

```
Annualized Return = (Net % / Days Locked) × 365

Example:
- 3% profit locked for 30 days = 36.5% annualized
- 3% profit locked for 90 days = 12.2% annualized
- 3% profit locked for 7 days = 156% annualized
```

**Recommendation**: Prioritize short-duration markets with 3%+ spread.

## Dynamic Threshold Idea

Consider implementing:

```python
def get_threshold(market_a, market_b, days_to_resolution):
    base = 2.0  # 2% base

    # Add risk premium for PredictIt
    if "predictit" in [market_a.platform, market_b.platform]:
        base += 2.0  # PredictIt has high fees

    # Reduce for very short duration (< 7 days)
    if days_to_resolution < 7:
        base -= 0.5  # Accept lower for quick resolution

    # Increase for long duration (> 60 days)
    if days_to_resolution > 60:
        base += 1.0  # Require more for capital lockup

    return max(base, 1.5)  # Never below 1.5%
```

## Conclusion

**Start with 3% net threshold.** This filters out:
- Noise from bid-ask spreads
- Most fee-related false positives
- Low-quality opportunities

Adjust based on experience:
- Too many alerts? → Raise to 4%
- Missing opportunities? → Lower to 2%
- PredictIt involved? → Always 5%+
