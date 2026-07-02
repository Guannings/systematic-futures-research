# Cross-sectional research — the market-neutral keeper, and the cost wall

A cross-sectional study over a basket of ~23 single-name stock-future underlyings
(anonymized here as `STK01…STK23`). It answers a different question from the
single-instrument index work: *is there a dollar-neutral, long/short edge across
names that survives realistic per-name transaction costs?*

Two findings, one kept and one rejected — both instructive.

## Data

`stocks_close.csv`, `stocks_open.csv` — daily open and close for the basket,
2021-06 → 2026-06 (1215 trading days, 22 usable names after dropping all-NaN
columns). Ticker identities are anonymized to `STK01…STK23`; the analysis is
purely cross-sectional so the labels are irrelevant to the result.

## Finding 1 — the cost wall (rejected)

Intraday cross-sectional reversal: short yesterday's winners, buy yesterday's
losers, enter at the open, exit at the close (day-session only, flat overnight),
lagged and cross-sectionally demeaned so it is lookahead-free and dollar-neutral.

```bash
python explore_xsec.py                # signal scan; 1d reversal gross Sharpe ~+2.0
python backtest_xsec_reversal.py      # the same edge, net of a realistic tick-cost model
```

| Basket | Gross Sharpe | Net Sharpe | Daily cost |
|---|---|---|---|
| full basket (22 names) | **+2.04** | **−2.40** | 0.230%/day |
| low-cost subset (4 names, tick% < 0.15) | +1.43 | −0.46 | 0.153%/day |

The reversal signal is *strongly* real gross (Sharpe +2.04) and *strongly*
unprofitable net (−2.40). The cost model uses the venue's actual tick ladder — a
fixed-size tick is a large *percentage* on a high-priced name, so a forced daily
round-trip across the basket pays ~0.23%/day, which swamps the edge. This is the
same cost wall that killed the intraday index approaches, shown here in its purest
form: **the strongest gross edge in the whole repo has the worst net result.**

## Finding 2 — the keeper alpha (research, not deployed)

Move the *same* cross-sectional idea to a low-turnover, overnight-held expression:
rank on trailing momentum, rebalance every 20 days, hold across days. Turnover
collapses, so the cost drag collapses, and a genuine market-neutral edge appears.

```bash
python test_lowturnover.py
```

| Strategy | Net Sharpe | IS | OOS | maxDD |
|---|---|---|---|---|
| **XS 20d momentum L/S** | **+1.22** | +1.04 | **+1.57** | −12.3% |
| XS 60d momentum L/S | +0.72 | +0.97 | +0.26 | −16.3% |
| XS 120d momentum L/S | +0.75 | +0.64 | +0.97 | −14.3% |
| long-only top-5 60d | +1.26 | +1.00 | +1.74 | −42.5% |

The 20-day long/short is the keeper: **net Sharpe 1.22, OOS 1.57**, maxDD −12%,
and — being dollar-neutral — largely uncorrelated with the index trend-hold bot.
It is documented as research here rather than deployed; the natural next step is to
pair it with the index leg to diversify that book's bull-beta (see
`../index-futures/STRATEGY.md`, "Possible improvements").

## The lesson

The reversal and the momentum L/S are the same family of cross-sectional edge; the
only thing that changed between the −2.40 rejection and the +1.22 keeper is the
**holding period**. Frequency of the signal must match frequency of trading — the
recurring theme of the whole repo.

## Files

```
explore_xsec.py            Signal scan across the basket (reversal, momentum, gap), gross.
backtest_xsec_reversal.py  Cost-aware intraday reversal — the net-negative finding.
test_lowturnover.py        Overnight-held momentum L/S — the market-neutral keeper.
stocks_close.csv           Daily closes (anonymized tickers STK01..STK23).
stocks_open.csv            Daily opens (same basket).
```
