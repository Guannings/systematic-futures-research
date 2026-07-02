# Index Trend-Hold — a day-managed, overnight-held index futures bot

# **Disclaimer and Terms of Use**

**1. Educational Purpose Only**

This software is for educational and research purposes only and was built as a
personal project by PARVAUX, a Public Finance and Economics student. It is not
intended to be a source of financial advice, and the author is not a registered
financial advisor. The algorithms, signal generators, sizing rules, and
live-trading wrappers implemented herein — EMA trend regimes, gap-momentum
entries, volatility/margin-based position sizing, and cross-sectional momentum
research — are demonstrations of well-known quantitative concepts and should not
be construed as a recommendation to buy, sell, or hold any specific security,
commodity, or derivative contract.

**2. No Financial Advice**

Nothing in this repository constitutes professional financial, legal, or tax
advice. Futures trading involves substantial risk of loss and is not suitable for
every investor. Investment decisions should be made based on your own research and
consultation with a qualified professional in your jurisdiction.

**3. Methodological and Modeling Risk**

a. **Past Performance.** Historical backtest results are not indicative of future
   results. The sample period does not characterize all regimes.

b. **Proxy Data.** Backtests run on third-party daily and hourly bars as a proxy
   for the exchange-listed contracts a live wrapper would trade. The products are
   correlated but not identical; spread, liquidity, microstructure, and
   trading-hour differences are not modeled.

c. **Cost Model.** Fees, exchange tax, and slippage are modeled at conservative
   constants and may materially exceed modeled values in illiquid conditions.

d. **Survivorship of Edges.** The README and `STRATEGY.md` deliberately document
   strategies that were tested and **rejected** (execution mirages, a lookahead
   bug, cost-wall failures). They are kept as a record of the validation process,
   not as deployable strategies.

**4. Published in Non-Functional State**

This repository is published in a deliberately non-functional state. The
live-trading path will not execute as published, by design:

a. **Proprietary broker bridge SDK omitted.** The vendor broker bridge SDK
   required for live execution is redistribution-restricted; it is excluded
   (`.gitignore`) and must be obtained from the platform provider.

b. **Account-specific parameters replaced with sample values.** The
   bridge-connection port in the live wrapper is the SDK's sample value, not a
   real account port.

c. **Live order routing disabled at the code level.** `DRY_RUN` is hard-set to
   `True`; the order-submission path is unreachable as published. This is
   intentional, not a bug.

The backtest harnesses run standalone against the included CSVs and are the only
paths intended to execute end-to-end.

**5. "AS-IS" Software Warranty**

**THIS SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND. IN NO EVENT
SHALL THE AUTHOR BE LIABLE FOR ANY CLAIM, DAMAGES, OR OTHER LIABILITY ARISING
FROM, OUT OF, OR IN CONNECTION WITH THE SOFTWARE OR ITS USE. BY USING THIS
SOFTWARE YOU ASSUME ALL RISKS ASSOCIATED WITH YOUR TRADING, INVESTMENT, AND
HARDWARE DECISIONS, RELEASING THE AUTHOR (PARVAUX) FROM ANY LIABILITY REGARDING
YOUR FINANCIAL OUTCOMES OR SYSTEM INTEGRITY.**

---

A trend-following system for an exchange-listed **equity-index** futures contract
(mini/micro index family). Day-session order placement, **positions held
overnight**. Sized to the only edge that survived honest, cost-aware,
lookahead-free testing.

## The honest story (this is the point of the repo)

Most of the work here is a record of edges that **looked** real and weren't.
Every intraday idea was real *gross* but died *net*, because forced daily
round-trips × the venue's wide ticks cost more than the daily alpha:

| Approach | Result | Verdict |
|---|---|---|
| Gap-continuation (cash-index proxy) | Sharpe 3–6 | execution mirage — can't trade the open; futures pre-absorb it overnight |
| Gap-continuation (real futures) | Sharpe ≈ −0.2 | coin flip |
| Gap + EMA trend filter | 3.0 → **0.1** | lookahead bug (EMA used same-day close); break-even after costs |
| Single-stock gap | none | gap is efficient; tick cost > edge |
| Cross-sectional intraday reversal | gross +2.0, **net −2.4** | killed by costs |
| **Trend-hold, overnight** | **net Sharpe 1.25, OOS 1.66** | **selected** |
| Cross-sectional 20d momentum L/S | net 1.22, OOS 1.57 | keeper market-neutral alpha (research only) |

**The survivor:** hold a leveraged long the index while it's above its EMA(50),
carried overnight (so the spread is paid only on trend flips, not daily), exit on
the trend break. Beats buy-and-hold on Sharpe (1.25 vs 1.13) *and* drawdown; it
went flat through the 2022 bear. Full detail in
[index-futures/STRATEGY.md](index-futures/STRATEGY.md).

## Backtest (real day-session index futures, 2019–2026, lookahead-free, costed)

Reproduce with `idx_trend_backtest.py` (the numbers below are its actual output):

| Metric | Value |
|---|---|
| Sharpe (leverage-invariant) | 1.25 (IS 1.03, OOS 1.66) |
| Annualized | +18.8% (1×), +56.5% (3×) |
| Max drawdown | −20.5% (1×), −52.2% (3×) |
| Turnover | ~17 trend flips/yr |
| Buy & hold benchmark | Sharpe 1.13, maxDD −31.5% |

```bash
py -3.13 index-futures/idx_trend_backtest.py        # the selected trend-hold strategy
py -3.13 index-futures/idx_futures_backtest.py 2000000   # the rejected gap variants
```

## Files

```
index-futures/idx_strategy.py         Pure logic — trend-hold + gap modes, sizing. No I/O.
index-futures/idx_live.py             Live wrapper — broker-bridge integration, one decision
                                      at the open, overnight hold, position persistence.
index-futures/idx_supervisor.py       Auto-restart watchdog (crash / silent drop / daily).
index-futures/idx_trend_backtest.py   IS/OOS backtest of the SELECTED trend-hold strategy.
index-futures/idx_futures_backtest.py IS/OOS backtest of the (rejected) gap variants.
index-futures/fetch_futures.py        Pulls day-session futures history (source omitted).
index-futures/test_offline.py         Offline end-to-end test (mocked broker).
index-futures/STRATEGY.md             Approach selection, decision tree, config, cost model.
index-futures/RUNBOOK.md              Operating guide.

xsec/                                 Cross-sectional momentum/reversal research (the
                                      market-neutral keeper alpha + the cost-wall finding).
single-stock/                         Single-stock gap study (rejected — gaps are efficient).
docs/BROKER_BRIDGE_NOTES.md           Undocumented broker-bridge behavior + defensive
                                      patterns. Useful to anyone integrating from Python.
```

## Running it live

Requires the vendor broker bridge app (Windows-only) with its Python SDK, obtained
from the platform provider (gitignored here). Then:

```powershell
py -3.13 index-futures/idx_supervisor.py
```

See [index-futures/RUNBOOK.md](index-futures/RUNBOOK.md) and
[docs/BROKER_BRIDGE_NOTES.md](docs/BROKER_BRIDGE_NOTES.md) — the bridge has a
subscription-dormancy quirk that needs a delete-and-readd ritual each session.

## License

MIT. See [../LICENSE](../LICENSE).
