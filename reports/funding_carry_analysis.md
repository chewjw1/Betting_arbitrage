# Funding-Rate Carry: Pressure Test

**Question:** Is the crypto perpetual funding-rate carry ("cash-and-carry")
a real, automatable, continuously-rollable income strategy — and does it
clear the bar of "don't tie up capital, don't take dumb risk"?

**Data:** Binance USDT-M funding history, BTCUSDT + ETHUSDT, 2021-01-01 →
2026-05-31 (5,931 eight-hour intervals each), via `data.binance.vision`.
Reproduce with `python scripts/funding_carry_backtest.py`.

**The trade:** long 1 unit spot + short 1 unit perpetual. Price moves cancel
(delta-neutral), so principal is hedged. Positive funding = longs pay shorts,
so the short-perp leg *receives* funding when it is positive. Historically it
is positive ~85% of intervals, so the leg earns a carry.

---

## Verdict

**It is real — but it is a risk premium, not arbitrage, and right now it is
barely paying.** Over 5.4 years the gross carry averaged **~11% APR** (BTC
11.1%, ETH 11.9%) with 85% of intervals positive. That is genuinely
positive, unlike the prediction-market "edge" we already debunked. But:

1. **It has compressed hard as the trade got crowded.** 2021: ~31% APR. 2024:
   ~12%. **2026 YTD: ~0.9%, with 41% of intervals negative. Last 12 months:
   3.5% — below the ~4.5% risk-free rate.** You would currently be taking
   crypto-exchange tail risk to *underperform T-bills*.
2. **The funding P&L curve looks almost riskless and that is a trap.** Max
   drawdown on the funding curve is <2% — but only because that curve
   *excludes the tail risks the premium exists to pay for*: exchange
   insolvency (FTX = ~-100%), stablecoin depeg (your collateral is USDT),
   and short-leg liquidation. The Sharpe looks spectacular precisely because
   the risk is hidden in the tails, not the day-to-day.

| Year | BTC gross APR | ETH gross APR | Negative intervals |
|------|--------------|--------------|--------------------|
| 2021 | 30.6% | 37.5% | 4–7% |
| 2022 | 4.2% | 0.8% | 22–34% |
| 2023 | 7.9% | 8.3% | 9–10% |
| 2024 | 11.9% | 13.0% | 4–8% |
| 2025 | 5.1% | 4.9% | 13–16% |
| 2026 (YTD) | 0.9% | 0.4% | 41–42% |

The pattern is the point: the carry pays when leveraged longs dominate (bull
markets) and dries up or inverts in bears. It is long crypto exuberance.

---

## The real risks (not in the funding numbers)

- **Exchange insolvency.** For cross-margin efficiency you hold spot and the
  short on one venue. If it blows up, you lose everything — one event erases
  a decade of 11% carry. Splitting legs across two venues cuts this but
  introduces liquidation risk (below).
- **Stablecoin depeg.** Collateral/quote is USDT. UST went to zero in 2022;
  USDT/USDC have wobbled. A depeg while holding is a direct loss.
- **Short-leg liquidation.** If legs are split (not cross-margined), a sharp
  rally can liquidate the short before you rebalance, turning a hedged book
  into a directional loss. So you must choose between exchange-concentration
  risk and liquidation risk.
- **Basis/ADL/execution** at entry and exit.

Fees, by contrast, are *not* the problem: carry is a hold strategy, so
round-trip cost amortizes to <0.1%/yr. Churning it (trying to time in/out
frequently) is what kills it — monthly churn at taker fees drops net to ~4–8%.

---

## How you would actually run it

- **Regime-time it.** Holding the carry only when trailing-30-day funding
  beats a threshold (else sit in T-bills) earns ~the same ~10–11% while
  exposed to crypto tail risk only **35–63% of the time** — a far better
  risk-adjusted profile. The overlay would correctly tell you to be in
  T-bills *right now*.
- **It fits the stated constraints** (automatic, continuously rollable, no
  multi-year lockup) — but at today's funding it is not worth the tail risk.
  It becomes attractive again only when funding re-elevates in a bull regime.
- **Capital efficiency:** APR is on 1× notional (you own the spot). A 1.3×
  margin buffer on the short knocks ~11% down to ~8.5% gross.

## Alternative noted, not pursued

The **fixed-expiry futures basis trade** (long spot, short a dated future at a
premium, capture convergence) is closer to true arbitrage — the convergence
at expiry is mechanical — but it locks capital until expiry, which violates
the "don't tie up capital" constraint. Same family, different tradeoff.

---

## Bottom line

Funding carry is the most honest "automatic income" instrument available on
the infra we already have, and historically it paid ~11%. But it is
compensation for bearing crypto-exchange tail risk, that compensation has
compressed below the risk-free rate as of mid-2026, and the smart way to run
it (regime-timed) says stand down until funding re-elevates. Do not deploy
into 3.5% carry while wearing FTX-shaped tail risk.
