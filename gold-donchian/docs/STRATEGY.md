# Strategy

A trend-following system for an exchange-listed gold futures contract,
constrained to the venue's day session. This document describes the
**design philosophy and the research process** behind it. It deliberately
omits the specific parameter values, the per-variant result tables, and the
production configuration — those were the output of a lot of testing, and
publishing them turns a hard-won design into a copy-paste artifact. The
*reasoning* is the transferable part; that's what's here.

---

## Design philosophy

The system was built around one observation about its scoring environment:
**consistency was rewarded more than raw P&L**. When risk-adjusted return is
what counts, the highest-leverage decision isn't a cleverer entry — it's
removing your largest source of variance. For an overnight-sensitive
commodity, that source is the gap. So the design's first principle was
*never hold through one*: day-session only, flat before the close, every
day.

Everything else followed from wanting a small number of high-quality
directional trades rather than a lot of activity:

1. **A slow daily trend regime gate.** A daily-timeframe trend filter
   decides whether the system is allowed to be long, short, or flat at all.
   It is intentionally slow — a regime classifier, not a trade trigger — so
   that intraday noise can't flip it.

2. **Intraday breakout entries.** When the regime gate is open, entries
   come from price breaking out of its recent range on an intraday bar, in
   the direction the regime permits. Breakouts are a natural fit for a
   trend system: they only fire when the market is actually moving.

3. **Volatility-targeted sizing.** Position size scales inversely with
   recent instrument volatility, targeting a roughly constant risk
   contribution per trade. In generic form,

   $$
   \text{size} \;\propto\; \frac{\sigma_{\text{target}}}{\sigma_{\text{instrument}}}
   $$

   so quiet markets get larger positions and volatile ones smaller, holding
   book-level risk approximately steady. Sizing is additionally capped by a
   margin ceiling and a hard maximum lot count.

4. **ATR-scaled stops.** A tight initial stop limits damage on an immediate
   adverse move; a wider trailing stop lets winners run. Both are scaled to
   recent average true range so they breathe with the market rather than
   sitting at fixed distances.

5. **A hard wall-clock exit.** Positions are force-flattened before session
   close on a wall-clock timer — not on a bar event — because the final
   in-session bar may never "close" within the session on an illiquid
   product.

The exact lookback lengths, multipliers, volatility target, and caps are
not published here by design.

## How the design was chosen

The final design was not the first one tried. Several variants were tested
against each other on a multi-year sample, using a strict **chronological
train/test split**: parameters were chosen on the older portion and judged
only on the held-out recent portion.

The recurring pattern was the standard cautionary tale of backtesting: the
variants that looked **best in-sample were frequently the ones that
collapsed out-of-sample**. A design with an extra confirmation filter posted
a strong in-sample number and then fell apart on the held-out data — the
classic signature of fitting to a directional regime that happened to live
in the training window.

The variant that survived was the least exciting: a slower, simpler trend
gate that ignored most intra-month chop. What recommended it was not a
flashy headline number but the fact that its **out-of-sample result closely
matched its in-sample result**. That match is the only honest evidence that
an edge is structural rather than fit to noise.

> **The transferable lesson.** The gap between in-sample and out-of-sample
> performance is more informative than either number on its own. A smaller
> in-sample result that holds up out-of-sample beats a larger one that
> doesn't.

Opening-range-breakout and shorter-horizon momentum variants were also
tested and discarded; the specific comparison results are omitted here for
the same reason as the parameters.

## Cost modeling

After the design was selected, the backtest was re-run with explicit
execution costs: exchange fees, transaction tax, and a conservative
allowance for slippage. The headline finding is the one worth keeping:
**adding even a single tick of slippage cost roughly a tenth of the
risk-adjusted return.** Gross performance flatters; only the net number
pays. Any backtest quoted without realistic costs should be treated as an
upper bound, not an estimate.

## The proxy-data caveat

Clean historical bars for the exact traded contract were not available, so
the backtest used a closely correlated global gold proxy. The two products
are correlated but not identical — spread, liquidity, trading hours, and
microstructure all differ, and none of those differences appear in a proxy
backtest. This caveat turned out to be the most important one of all (see
the live-deployment notes), and it should sit on top of every result in
this document.

## Directions for a future iteration

Stated as general directions rather than tuned recipes:

- **Match the signal horizon to the product's liquidity.** A breakout
  system needs the market to actually traverse a range within the signal
  window. On a thin product that rarely happens on an hourly horizon;
  either a slower horizon (cleaner, fewer signals) or a faster one with a
  wider channel may fit better. Decide this *after* measuring how often the
  live product really trades.
- **Consider a slope-based regime test** instead of a crossover, to respond
  to major reversals sooner without adding a tunable parameter.
- **Add a liquidity filter on session entry**, skipping sessions where the
  prior day showed too few real trades — so the system doesn't sit live
  through dead markets.
