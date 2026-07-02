# Strategy

A trend-following system for an exchange-listed equity-index future. Order
placement is constrained to the venue's day session, but **positions are carried
overnight** — and that single design choice is the whole reason the strategy
clears costs when every intraday variant did not.

Every number in this document is reproducible from the committed data with
`idx_trend_backtest.py` (headline) and `idx_trend_backtest.py --full` (the leg,
per-year, parameter, and cost diagnostics). Backtested on real day-session index
futures, 1760 sessions, 2019-03-26 → 2026-06-18, with the exchange's index-futures
fee, tax, and slippage baked in:

- **Sharpe (leverage-invariant): 1.25**  (IS 1.03, **OOS 1.66**)
- Total return: **+18.8%/yr (1×)**, **+56.5%/yr (3× leverage)**
- Max drawdown: **−20.5% (1×)** / **−52.2% (3×)**
- Turnover: **117 trend flips over 7 years ≈ 17/yr** (not per day)
- Benchmark: buy-and-hold Sharpe 1.13, max drawdown −31.5% — the strategy beats
  it on both risk-adjusted return *and* drawdown

Out-of-sample Sharpe (1.66) exceeding in-sample (1.03) is the signature we look
for: the rule was not curve-fit to the training window. Live execution is on the
mini/micro index future for liquidity and sizing granularity; the mini contract
tracks the same underlying index, so the full-size future's daily history is a
valid trend proxy.

---

## 1. The core idea

Equity indices trend. Over the sample the index spent most of its time in
sustained up-legs punctuated by sharp, shorter drawdowns. Two facts follow, and
they define the entire design:

**(a) A slow trend filter captures most of the up-legs and sidesteps the worst of
the down-legs.** You do not predict tops and bottoms; you stay long during the
persistent up-moves and flat during the persistent down-moves. A 50-day EMA on
daily closes is slow enough to ignore day-to-day noise and fast enough to be out
before a bear market does real damage. In 2022 it took the position flat and lost
−13.2% against buy-and-hold's −22.4% — that 9-point gap is most of the drawdown
improvement over simply owning the index.

**(b) The edge is daily, so the holding period must be daily.** The trend signal
changes ~17 times a year. Expressed by trading in and out every session (flat by
the close), you would pay the round-trip spread ~250 times a year to harvest an
edge that updates ~17 times a year. On this venue's wide tick structure that
transaction cost exceeds the daily alpha — so the *intraday* expression of the
same signal is net-negative. Holding overnight pays the spread only when the trend
actually flips. **Match the holding period to the frequency of the signal, and the
cost wall disappears.** Section 7 proves this empirically.

Everything else — sizing, the supervisor, tick filtering — is machinery around
those two facts.

## 2. The daily decision, precisely

One decision per day, at the session open, a pure function of data known *before*
that open (no lookahead):

```
trend_ok = close[yesterday] > EMA50[yesterday]      # both known before today opens
if trend_ok and flat:        go long, sized to target (Section 4)
if trend_ok and long:        hold
if not trend_ok and long:    flatten the whole position
if not trend_ok and flat:    stay flat
```

The EMA(50) is computed on daily closes through *yesterday*. Using today's close
in the comparison would be a lookahead bug — the exact bug that inflated a
different variant's Sharpe from ~0.5 to ~3 before review caught it (Section 6). The
one-day lag is deliberate and load-bearing.

Entries and exits happen only on the days `trend_ok` changes state, so the
position is a long / flat / long / flat step function, not a daily churn — 117
state changes across 1760 sessions.

## 3. What the returns actually look like

This is a trend-follower, and it has the classic trend-following shape — which is
worth internalizing before trusting the Sharpe, because the return stream is *not*
a smooth accrual:

- **Time long: 72.5%. Time flat: 27.5%.** It is exposed most of the time and steps
  aside about a quarter of it.
- **59 long legs, mean length 22 days, median 6 days.** The mean-vs-median gap is
  the whole story: most legs are short whipsaws (the filter flips out within a
  week), while a handful of long runners carry the P&L.
- **Leg win rate: 36%.** Nearly two-thirds of entries lose. The strategy is *not*
  accurate.
- **Best leg +40.0%, worst leg −5.0%.** It wins by **asymmetry, not accuracy** —
  losers are cut fast and small by the EMA flip, winners are allowed to run. An
  8:1 best-to-worst ratio with a 36% hit rate is textbook trend-following.

The practical consequence: this equity curve has long flat/choppy stretches and
occasional steep climbs. A user who expects a high win rate will abandon it during
a whipsaw cluster right before the runner that pays for them. The low win rate is
a feature; the discipline to sit through it is the hard part.

## 4. Year by year — the honest trade-off

| Year | Strategy (1×) | Sharpe | Buy & hold | Read |
|---|---|---|---|---|
| 2019 | +13.7% | +1.86 | +14.9% | tracks the bull, slight drag |
| 2020 | +21.5% | +1.38 | +22.4% | tracks the bull |
| 2021 | +19.0% | +1.38 | +24.1% | gives up some upside |
| 2022 | **−13.2%** | −1.55 | **−22.4%** | **the payoff: −9pts less than b&h** |
| 2023 | +14.7% | +1.31 | +26.4% | leaves a lot of bull on the table |
| 2024 | +10.7% | +0.74 | +29.0% | worst relative year — choppy bull, whipsawed |
| 2025 | +22.9% | +1.48 | +25.9% | tracks the bull |
| 2026* | +54.3% | +3.42 | +61.2% | tracks the bull (partial year) |

The pattern is unambiguous and worth stating plainly: **in bull years the strategy
under-performs buy-and-hold, sometimes badly (2023, 2024); it earns its keep in the
one bear year (2022) by losing far less.** This is a drawdown-reduction and
risk-adjustment play, not an outperformance-in-all-weather play. Its higher Sharpe
(1.25 vs 1.13) and shallower drawdown (−20.5% vs −31.5%) come *entirely* from bear
protection, paid for with give-up in strong bulls. If your objective is raw return
in a continuing bull, leveraged buy-and-hold wins; if it is risk-adjusted return
across the cycle, the filter wins.

## 5. Position sizing and leverage

The `max_margin` sizing mode — the leverage dial, opt-in via `--sizing-mode
max_margin` (`vol_target` is the default) — deploys a fixed fraction of equity as
exchange margin, directionally:

```
margin_lot   = margin_rate * price * contract_multiplier
target_lots  = floor( equity * margin_fraction / margin_lot )
```

Because `margin_lot` uses the live index level, the position auto-scales as the
index moves — sizing never goes stale the way a fixed lots-per-account rule would.

**Worked example** (index 46,000, `margin_rate 0.11`, `contract_multiplier 50`,
equity 2,000,000, `margin_fraction 0.50`):

```
margin_lot  = 0.11 * 46,000 * 50               = 253,000 per lot
target_lots = floor(2,000,000 * 0.50 / 253,000) = floor(3.95) = 3 lots
notional    = 3 * 46,000 * 50                   = 6,900,000
leverage    = 6,900,000 / 2,000,000             = 3.45x
```

**Theoretical vs realized leverage.** Theoretical leverage is
`margin_fraction / margin_rate = 0.50 / 0.11 ≈ 4.5×`, but integer-lot rounding
(3.95 → 3) pulls *realized* leverage to ~3.45× on a 2M account. Smaller accounts
round more coarsely (a 1M account rounds 1.97 → 1 lot, ~2.3× realized, well under
the ceiling); larger accounts approach the theoretical ceiling. Raise
`margin_fraction` to 0.80 to lift the ceiling to ~7×. Sharpe is leverage-invariant
(both returns and per-flip costs scale with size), so leverage sets the *magnitude*
of P&L and drawdown, not the risk-adjusted quality — 3× buys the same 1.25 Sharpe
for a −52% drawdown instead of −20%.

The alternate `vol_target` mode sizes to a target annualized volatility (the
conservative, gold-GDVT calibration); it ships for reference and is not deployed.

## 6. Why each alternative was rejected

The winner is the survivor of a deliberate cull. Every idea below was real *gross*
and died *net*, or died to a modeling error caught in review. The rejections are as
much the point of this study as the winner.

| Approach | Gross → Net | Why it failed |
|---|---|---|
| Gap-continuation (cash-index proxy) | Sharpe 3–6 | **Execution mirage.** Assumes you transact the exact opening print. You can't — and on the real futures, which trade all night, the overnight session already absorbed the move. |
| Raw gap (real futures) | Sharpe ≈ −0.2 | On the tradable instrument the "edge" is a coin flip. |
| Gap + trend filter (V4) | 3.0 → 0.1 | **Lookahead bug.** The trend EMA used *today's* close. Lagged correctly it nets ~0.1 — break-even after costs. |
| Single-stock gap | none | The single-name gap is efficiently priced; the fade is smaller than one tick of cost. |
| Cross-sectional intraday reversal | gross +2.0, **net −2.4** | **The cost wall.** Strongest gross edge in the study, worst net — proof that forced intraday turnover × wide ticks beats any daily alpha. |
| **Trend-hold, overnight** | **net 1.25, OOS 1.66** | **Selected.** A daily edge held with a daily holding period. |
| XS 20d momentum L/S | net 1.22, OOS 1.57 | Genuine market-neutral alpha — kept as research, not deployed here. |

The through-line: the intraday ideas were not wrong about *direction*, they were
wrong about *tradability*. Gross Sharpe measures the signal; net Sharpe measures
the business. Only the overnight-hold expression let a real daily signal survive
contact with the venue's cost structure.

## 7. Cost robustness — the thesis, tested

If the "escape the cost wall by holding overnight" thesis is real, the result
should be nearly *insensitive* to transaction costs. It is (EMA50, varying
slippage per side):

| Slippage/side | Sharpe | Annualized |
|---|---|---|
| 0.0 pt | +1.26 | +18.9% |
| 0.5 pt | +1.26 | +18.9% |
| 1.0 pt | +1.25 | +18.8% |
| 2.0 pt | +1.25 | +18.7% |
| 4.0 pt | +1.23 | +18.5% |

Quadrupling slippage costs 0.03 of Sharpe. Contrast this with the sibling findings:
the gold GDVT study lost ~10% of its risk-adjusted return to a *single* tick of
slippage, and the cross-sectional intraday reversal went from **+2.0 gross to −2.4
net**. Same venue, same tick ladder — the difference is entirely turnover. ~17
flips a year makes the cost line a rounding error; ~250 round-trips a year makes it
the whole game. This table is the single strongest piece of evidence for the design.

## 8. Parameter robustness — is 50 special?

No — and that is reassuring. Sweeping the EMA length shows a broad plateau, not a
lonely spike, which is what you want from a rule that is not curve-fit:

| EMA | Sharpe FULL | IS | OOS | Ann | maxDD |
|---|---|---|---|---|---|
| 20 | +1.37 | +1.35 | +1.47 | +19.2% | −21.5% |
| 30 | +1.28 | +1.22 | +1.43 | +18.4% | −18.2% |
| **50** | **+1.25** | **+1.03** | **+1.66** | **+18.8%** | **−20.5%** |
| 75 | +1.18 | +0.87 | +1.70 | +18.5% | −25.8% |
| 100 | +1.37 | +1.11 | +1.84 | +21.7% | −16.6% |
| 150 | +1.24 | +1.16 | +1.45 | +21.1% | −25.1% |
| 200 | +1.20 | +1.12 | +1.39 | +21.0% | −20.7% |

Every length from 20 to 200 lands in a Sharpe band of 1.18–1.37. There is no cliff.
Two honest observations: (1) **50 is not the in-sample optimum** — EMA100 posts a
higher full-sample and OOS Sharpe. 50 was fixed *a priori* as a conventional
medium-term trend length, not tuned to the peak, which is precisely why the OOS
result is trustworthy. (2) A practitioner could argue for 100 on this data; the fact
that the choice barely matters is the point. Picking the sweep winner (100) would be
a mild form of overfitting to this one sample.

## 9. Overnight gap risk

The cost of escaping the cost wall is directional overnight exposure with no
intraday stop between the close and the next open:

- **Worst single in-position day: −6.12% at 1×**, which is **−18.4% at 3×**, in one
  session. A macro shock or offshore selloff hits the full leveraged position at
  once.
- There is no overnight stop-loss by construction — the exit is the *next* day's
  EMA check, not an intraday level. This is deliberate (an overnight stop would
  reintroduce fills and costs) but it means tail risk is real and lumpy.
- At 3× leverage, a cluster of adverse gaps is the realistic path to the −52%
  drawdown. Size to the drawdown you can sit through, not the return you want.

## 10. Live execution mechanics

The live path (`idx_live.py`, supervised by `idx_supervisor.py`) reuses the gold
GDVT reliability architecture. The strategy is ~10% of the code; the rest is
defensive plumbing against an undocumented broker bridge (see
`../docs/BROKER_BRIDGE_NOTES.md`).

- **One-shot open decision.** Unlike the per-bar gold system, the index bot makes a
  single `decide_open()` call on the first valid in-session tick of each new date,
  then is idempotent for the rest of the session (`_decided_today`).
- **Tick hygiene.** `SHOWQUOTEDATA` drops placeholder ticks (`Price="-"`) and stale
  cached ticks (age > 1h) before they can build a phantom bar or fire a false
  signal — the bridge emits both as keep-alive noise on a quiet book.
- **Position persistence.** `current_position` is written atomically (temp file +
  rename) on every change, so an overnight restart recovers the carried position.
  On startup a non-zero persisted position triggers a loud warning to manually
  confirm in the bridge, because the SDK sample exposes no callable position query.
- **`HOLD_OVERNIGHT = True` is the strategy.** It disables the 13:30 wall-clock
  force-flatten. Flip it False and you are back to the (rejected) intraday `gap`
  mode.
- **Supervisor.** Restarts the bot on crash, on a silent feed drop during session,
  and on a scheduled daily restart; sizes the silence timer to the product's real
  liquidity so a normal quiet stretch does not thrash it.
- **Published disabled.** `DRY_RUN = True` is hard-set and `BRIDGE_PORT` is a sample
  value; the order path cannot fire as published.

## 11. Configuration parameters

All tunables live in `idx_strategy.py:IndexGapConfig` and the `idx_live.py` header.

| Parameter | Value | Rationale |
|---|---|---|
| `mode` | `trend_hold` | The selected approach; `idx_live.py` defaults to it (the `IndexGapConfig` dataclass default remains the legacy `gap`) |
| `trend_n` | 50 | EMA length; long while prior close > EMA50. Broad plateau (Section 8) |
| `sizing_mode` | `vol_target` | Risk-calibrated default; set `max_margin` to deploy a fixed margin fraction (the leverage dial) |
| `margin_fraction` | 0.80 | Used only in `max_margin` mode. ~7×; lower to 0.50 for a theoretical ~4.5× (realized ~3.5× after integer-lot rounding) |
| `margin_rate` | 0.11 | ≈ the exchange's index-futures initial margin; scales with the index level. VERIFY the current rate |
| `contract_multiplier` | 50 | Currency units per index point |
| `HOLD_OVERNIGHT` | True | Carry positions; disables the 13:30 force-flatten. This flag *is* the strategy |
| `SYMBOL` | FRONTc1 | Active near-month (VERIFY; the exchange uses its own month-coding scheme) |

## 12. Validation methodology

- **Chronological IS/OOS split (70/30).** Examined on the first 70% of history,
  confirmed on the untouched final 30%. No shuffling — time order preserved, so the
  test set is genuinely "the future" relative to the train set.
- **Selection on out-of-sample, never in-sample.** A variant is credible only if its
  OOS result holds; the study rejects things that looked good only in-sample.
- **Lookahead discipline.** Every signal uses data strictly prior to the decision
  point. The one violation (the V4 gap filter) is documented, not hidden — it is why
  that row reads "3.0 → 0.1".
- **Costs modeled explicitly.** Fee, tax, and slippage are in the equity curve;
  gross numbers are reported only alongside their net counterparts.
- **Robustness over optimization.** Parameters were fixed to conventional values and
  then *swept to confirm insensitivity* (Sections 7–8), rather than tuned to a peak.

## 13. Risk

An honest accounting, not a disclaimer:

- **Leveraged bull-beta, not alpha.** It earns because the index trended up and the
  filter dodged the worst down-legs. In a prolonged choppy or bear regime the filter
  whipsaws and the return degrades; it does not short, so it makes nothing in a
  sustained decline — it only avoids the loss.
- **Overnight gap risk** (Section 9): −6.12% worst 1× day, −18.4% at 3×, no intraday
  stop.
- **Leverage symmetry.** At 3× the same Sharpe comes with a −52% drawdown.
- **Filter lag.** EMA(50) gives back part of a move before flipping flat; faster
  filters cut the giveback but whipsaw more.
- **Low win rate (36%)** demands discipline through whipsaw clusters — a behavioral
  risk as much as a market one.
- **Single-bet concentration**: one instrument, one direction, one regime dependence.
- **Sample and proxy caveats.** ~7 years is one macro cycle; live is on the
  mini/micro contract while the backtest uses the full-size future as proxy —
  correlated, not identical.

## 14. Operating

Files in `index-futures/`: `idx_strategy.py` (pure logic), `idx_live.py`
(broker-bridge live wrapper), `idx_supervisor.py` (watchdog), `idx_trend_backtest.py`
(this strategy's backtest + `--full` diagnostics), `idx_futures_backtest.py` (the
rejected gap variants), `fetch_futures.py`, `RUNBOOK.md`.

```
cd /path/to/repo
py -3.13 index-trend/index-futures/idx_trend_backtest.py --full   # reproduce every number here
py -3.13 index-trend/index-futures/idx_supervisor.py               # launch live (published copy is disabled)
```

Pre-flight: confirm the active near-month, subscribe the symbol in the bridge app,
verify `margin_rate`, and watch the first launch for a clean
`trend_long → NEWORDER → Code 0000`.

## 15. Possible improvements

1. **Market-neutral keeper.** XS 20-day momentum long/short (net Sharpe 1.22,
   OOS 1.57) is uncorrelated alpha — build it into its own basket bot. Research in
   `../xsec/`. Pairing it with this trend leg would diversify the bull-beta.
2. **Trend-filter refinement.** The EMA crossover lags reversals; a slope or
   Donchian-style exit could get flat faster without adding a fitted parameter.
3. **Volatility-scaled leverage.** `margin_fraction` is static; scaling it inversely
   to recent realized vol would tame the −52% tail without giving up the bull.
4. **A short leg.** The system only avoids bears; a filtered short would monetize
   them, at the cost of the (harder) problem of shorting an index that drifts up.

---

> **Honest framing:** this is leveraged bull-beta with trend protection, not alpha.
> It earns if the index uptrend continues and the EMA(50) filter limits (not
> eliminates) reversal damage. Backtested as a simulation — the downside of
> aggressive sizing is a deep, real drawdown. The genuine market-neutral edge in
> this repo is the XS momentum long/short, kept as research for a follow-up.
