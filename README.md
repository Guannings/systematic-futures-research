# Systematic Futures Research

A personal research framework for building and **honestly validating** systematic
futures strategies on an Asian derivatives exchange. **Two fully separate studies**,
one shared engineering core, and a deliberate emphasis on the part most trading
repos hide: **rigorous, cost-aware, lookahead-free validation — including the edges
that failed it.**

> Independent project by **PARVAUX** (Public Finance & Economics student).
> Educational and research purposes only; **published in a non-functional state** —
> live order routing is disabled in code, account parameters are sample values, and
> the proprietary broker SDK is omitted. Full terms below; each subproject repeats
> them. Futures trading involves substantial risk of loss.

# Disclaimer and Terms of Use

**1. Educational Purpose Only.** This repository is for educational and research
purposes only and was built as a personal project by PARVAUX, a Public Finance and
Economics student. It is not a source of financial advice, and the author is not a
registered financial advisor. The strategies, signal generators, sizing rules, and
live-trading wrappers herein are demonstrations of well-known quantitative concepts
and are not a recommendation to buy, sell, or hold any security, commodity, or
derivative contract.

**2. No Financial Advice.** Nothing here constitutes professional financial, legal,
or tax advice. Futures trading involves substantial risk of loss and is not suitable
for every investor. Make investment decisions based on your own research and
consultation with a qualified professional in your jurisdiction.

**3. Methodological and Modeling Risk.**

a. **Past performance** is not indicative of future results. The sample period does
   not characterize all market regimes.

b. **Proxy data.** Backtests run on third-party daily and hourly bars as a proxy for
   the exchange-listed contracts a live wrapper would trade. The products are
   correlated but not identical; spread, liquidity, microstructure, and trading-hour
   differences are not modeled.

c. **Cost model.** Fees, exchange tax, and slippage are modeled at conservative
   constants and may materially exceed modeled values in illiquid conditions.

d. **Survivorship of edges.** These docs deliberately document strategies that were
   tested and **rejected** (execution mirages, a lookahead bug, cost-wall failures).
   They are kept as a record of the validation process, not as deployable strategies.

**4. Published in a Non-Functional State.** The live-trading path will not execute as
published, by design: (a) the proprietary broker bridge SDK is redistribution-
restricted and omitted (`.gitignore`); (b) the bridge-connection parameters are the
SDK's sample values, not a real account; and (c) `DRY_RUN` is hard-set to `True`, so
the order-submission path is unreachable regardless of configuration. This is
intentional, not a bug. The backtest harnesses run standalone against the included
CSVs and are the only paths intended to execute end-to-end.

**5. "AS-IS" Software Warranty.** THIS SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY
OF ANY KIND, EXPRESS OR IMPLIED. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
CLAIM, DAMAGES, OR OTHER LIABILITY ARISING FROM, OUT OF, OR IN CONNECTION WITH THE
SOFTWARE OR ITS USE. BY USING THIS SOFTWARE YOU ASSUME ALL RISKS ASSOCIATED WITH YOUR
TRADING, INVESTMENT, AND HARDWARE DECISIONS, RELEASING THE AUTHOR (PARVAUX) FROM ANY
LIABILITY REGARDING YOUR FINANCIAL OUTCOMES OR SYSTEM INTEGRITY.

## Repository layout — two independent studies

The two studies are kept in separate top-level folders. They share design lessons
and a broker-bridge integration pattern, but each is self-contained with its own
README, disclaimer, data, strategy, backtest, and live wrapper.

```
gold-donchian/    STUDY 1 — Gold day-session trend bot (Donchian breakout family).
index-trend/      STUDY 2 — Equity-index trend-hold bot + the rejected-approaches record.
  index-futures/    Strategy, live wrapper, supervisor, backtest, offline tests.
  xsec/             Cross-sectional momentum (the keeper alpha) + single-stock study inputs.
  single-stock/     Single-stock gap study (rejected).
  docs/             Broker-bridge integration reference.
LICENSE           MIT, shared.
```

Start with each study's own README:
[`gold-donchian/README.md`](gold-donchian/README.md) ·
[`index-trend/README.md`](index-trend/README.md).

## Why this repo exists

Most strategy repositories show one backtest that makes money. The harder, more
honest question — *does the edge survive transaction costs, real execution, and
out-of-sample data?* — is where most ideas quietly die. This repo documents that
process end-to-end across two asset classes. The headline result is not a P&L
curve; it is a **method**, and a record of what the method rejected.

---

## STUDY 1 — `gold-donchian/`  (day-session trend following on gold futures)

A daily EMA(100/400) regime filter gating intraday Donchian-20 breakouts, ATR
stops, volatility-targeted sizing, flat by session close. Selected over four
alternatives via IS/OOS testing (only this variant held its OOS Sharpe: 0.99 IS /
0.95 OOS). Backtest **Sharpe 0.84** with modeled costs.
**Honest live result: zero signals** — the underlying barely traded, so the
breakout had nothing to break out of. Documented, not hidden.

Full write-up: [`gold-donchian/README.md`](gold-donchian/README.md).

---

## STUDY 2 — `index-trend/`  (what actually survives on the equity index)

A clean-sheet study that tested a sequence of intraday edges and **rejected almost
all of them on their merits:**

| Approach | Result | Verdict |
|---|---|---|
| Gap-continuation (cash-index proxy) | Sharpe 3–6 | execution mirage — the open isn't tradable; futures pre-absorb it overnight |
| Same, on real futures | Sharpe ≈ −0.2 | coin flip |
| Gap + trend filter | 3.0 → **0.1** | **lookahead bug** (EMA used the same day's close); break-even after costs |
| Single-stock gap | none | gap is efficiently priced; tick cost > edge |
| Cross-sectional intraday reversal | gross +2.0, **net −2.4** | killed by the tick-cost wall |
| **Trend-hold, held overnight** | **net Sharpe 1.25, OOS 1.66** | **selected** |
| Cross-sectional 20d momentum L/S | net 1.22, OOS 1.57 | market-neutral keeper (research) |

The survivor and the research live in
[`index-trend/index-futures/`](index-trend/index-futures) and
[`index-trend/xsec/`](index-trend/xsec).

---

## Cross-cutting themes (the actual transferable skills)

- **Validation discipline.** Chronological IS/OOS splits; selection on
  out-of-sample, never in-sample; explicit fee/tax/slippage cost models. A
  same-day-close **lookahead bug** that inflated a Sharpe from ~0.5 to ~3 was found
  and corrected — and the corrected number is what's reported.
- **Honest reporting of failure.** A gold system that fired zero live signals; an
  intraday edge that was real gross and **net-negative** after costs. Both are
  documented as findings, because the negative results are the point.
- **Market-microstructure reasoning.** The recurring lesson — forced daily
  round-trips × a wide tick structure cost more than the daily alpha — is what
  motivated the overnight-hold design that finally cleared costs.
- **Production engineering.** A reusable broker-bridge integration layer, a
  supervisor/watchdog (crash, silent-feed-drop, and daily restart handling),
  atomic position persistence, and an offline end-to-end test harness (mocked
  broker) — reused across both strategies.

## Engineering notes

The strategy logic is a small fraction of the code; most of it is defensive
plumbing against undocumented broker-bridge behavior (silent subscription
dormancy, placeholder ticks, acceptance-vs-fill ambiguity, contract month codes).
Those notes — useful to anyone integrating against the same class of bridge — are
in each study's `docs/BROKER_BRIDGE_NOTES.md`.

## License

MIT. See [LICENSE](LICENSE). Each study retains its own disclaimer.
