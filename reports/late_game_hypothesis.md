# Late-Game Prediction Market Edge Hypothesis

## Executive Summary

We hypothesize that **late-game sports markets offer superior systematic edge** compared to crypto 15-minute markets due to fundamental differences in how uncertainty resolves over time.

---

## Part 1: The Problem with BTC 15-Minute Markets

### Observed Patterns (3 windows collected)

| Window | Outcome | Category | Predictability | What Happened |
|--------|---------|----------|----------------|---------------|
| 062245-45 | UP | flat | unpredictable | YES locked at 100c, no movement |
| 071445-45 | DOWN | late_drop | unpredictable | 99c→10c in final minute |
| 071515-15 | DOWN | grinding | **early** | Stayed below target throughout |

### Core Issue: No Convergence Mechanism

BTC has **no natural constraint** on late volatility:
- Price can move 0.2% in the final 10 seconds
- A "99% certain" position can flip to 10% instantly
- The 15-minute cutoff is arbitrary - BTC doesn't "know" the window is ending

**Evidence:** Window 071445-45 showed YES at 99.4c with 73 seconds remaining, then collapsed to 10.3c after a sudden BTC drop. This is fundamentally unpredictable without additional data (order flow, liquidation cascades, etc.).

### When BTC Markets ARE Tradeable

The "grinding" pattern (Window 071515-15) suggests edge exists when:
- **Distance from target > 0.15%** (harder to reverse)
- **Consistent direction** (not oscillating around target)
- **Declining YES prices** (market confirming direction)

But these conditions occur unpredictably, and even then, late reversals remain possible.

---

## Part 2: Why Sports Late-Game is Different

### The Clock as a Constraint

In sports, time is a **real physical constraint**, not an arbitrary cutoff:

| Factor | BTC 15-min | Sports Late-Game |
|--------|------------|------------------|
| Time remaining | Arbitrary window | Real game clock |
| Score reversal | Always possible | Requires possessions + points |
| Variance decay | None | Mathematically decreasing |
| Information | Price only | Score + time + possession |

### Mathematical Certainty Examples

**NFL (American Football):**
- 14-point lead, 2 minutes left, opponent has no timeouts
- Requires: 2 TDs + 2 conversions + 2 onside kicks + clock stops
- Probability: <1%

**NBA Basketball:**
- 12-point lead, 90 seconds left
- Requires: ~4-5 possessions, opponent must score every time AND you must miss
- Probability: ~2-3%

**Soccer:**
- 2-0 lead, 85th minute
- Requires: 2 goals in ~10 minutes (including stoppage)
- Probability: ~1-2% (higher than others due to low-scoring nature)

### Kalshi Sports Markets Available

We found these relevant series on Kalshi:

**Quarter/Half Markets (NFL):**
- KXNFL4QWINNER: 4th Quarter Winner
- KXNFL2HWINNER: 2nd Half Winner
- KXNFL4QSPREAD: 4th Quarter Spread

**Quarter/Half Markets (NBA):**
- KXNBA4QWINNER: 4th Quarter Winner
- KXNBA2HWINNER: 2nd Half Winner
- KXNBA4QTOTAL: 4th Quarter Total

**First 5 Innings (MLB):**
- KXMLBF5: First 5 Innings Winner
- KXMLBF5SPREAD: First 5 Innings Spread

**Soccer Half Markets:**
- KXEPL1H: Premier League First Half Winner
- KXLALIGA1H: La Liga First Half Winner
- KXBUNDESLIGA1H: Bundesliga First Half Winner

---

## Part 3: Hypotheses to Test

### Hypothesis 1: Late-Game Sports Convergence
**Claim:** Sports markets converge to certainty faster and more predictably than BTC markets as time expires.

**Test Method:**
- Collect tick data from NBA/NFL 4th quarter markets
- Compare rate of convergence to certainty (YES→99c or YES→1c)
- Measure frequency of late reversals vs BTC markets

**Success Metric:** <5% reversal rate when market shows >90% confidence with <2 min remaining (vs ~15%+ for BTC)

### Hypothesis 2: Score Differential as Edge
**Claim:** Large score differentials create systematic edge that BTC distance-from-target cannot match.

**Test Method:**
- Track outcome accuracy when betting on leading team with:
  - NFL: 14+ point lead, <5 min remaining
  - NBA: 15+ point lead, <3 min remaining
  - Soccer: 2+ goal lead, <15 min remaining

**Success Metric:** >95% win rate on above criteria

### Hypothesis 3: Information Advantage
**Claim:** Multi-factor sports data (score + time + possession + fouls) provides better prediction than single-factor BTC data (price).

**Test Method:**
- Build simple models for each:
  - BTC: distance_from_target + time_remaining + kalshi_price
  - Sports: score_diff + time_remaining + possession + timeouts + fouls
- Compare prediction accuracy at equivalent time-to-close

**Success Metric:** Sports model outperforms by >10% accuracy

### Hypothesis 4: Late Upset Patterns
**Claim:** Sports upsets follow identifiable patterns (momentum shifts, key player events) that can be detected and avoided.

**Test Method:**
- Analyze historical upsets in late-game situations
- Identify common precursors (scoring runs, injuries, red cards)
- Develop filters to avoid high-risk situations

**Success Metric:** Reduce upset exposure by >50% while maintaining opportunity volume

---

## Part 4: Implementation Plan

### Phase 1: Data Collection (1-2 weeks)
1. Build sports market collector similar to BTC collector
2. Capture live game data (scores, time, events) from free APIs
3. Store tick-level Kalshi prices alongside game state
4. Focus on: NFL (if in season), NBA playoffs, MLB, Premier League

### Phase 2: Pattern Analysis (1 week)
1. Analyze collected data for convergence patterns
2. Identify "safe zone" criteria (score diff + time remaining)
3. Measure actual vs expected upset rates
4. Build LLM categorization similar to BTC windows

### Phase 3: Strategy Development (1 week)
1. Define entry criteria (when to bet)
2. Define exit criteria (when to close position)
3. Define avoid criteria (high-risk situations)
4. Backtest on collected data

### Phase 4: Paper Trading (2 weeks)
1. Simulate trades without real money
2. Track performance vs expected
3. Refine criteria based on results

### Phase 5: Live Trading (ongoing)
1. Start with small positions
2. Scale based on verified edge
3. Monitor for market changes

---

## Part 5: Risk Assessment

### BTC 15-Minute Risks
- **Late reversals:** High (observed 33% in our sample)
- **Liquidity:** Variable (sometimes YES locked at extreme prices)
- **Information disadvantage:** High (whales, order flow invisible)

### Sports Late-Game Risks
- **Late upsets:** Low but non-zero (estimated 1-5% depending on sport/situation)
- **Liquidity:** Unknown (need to test)
- **Market efficiency:** Unknown (may be well-arbitraged)
- **Timing:** Limited to game schedules

### Recommended Starting Allocation
- Sports late-game: 70% of capital (if hypothesis confirmed)
- BTC 15-min (grinding only): 30% of capital
- Avoid: BTC small-gap situations, sports close-game situations

---

## Conclusion

**Primary Thesis:** Sports markets with large score differentials in late-game situations offer mathematically superior edge compared to BTC 15-minute markets because:

1. **Time is a real constraint** - the clock enforces uncertainty reduction
2. **Score differential matters** - 14 points with 2 min left is nearly insurmountable
3. **Multi-factor prediction** - more information enables better models
4. **Upset patterns identifiable** - unlike BTC volatility spikes

**Next Step:** Build sports data collector and validate hypotheses with real data before risking capital.

---

*Report generated: 2026-05-07*
*Data source: Kalshi API, Binance API*
*Windows analyzed: 3 (BTC 15-min)*
