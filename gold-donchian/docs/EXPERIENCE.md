# Project experience: building and running an automated futures strategy

*A first-person account of designing, validating, and live-deploying an
algorithmic trend-following system as a hands-on trading practice. Written
as a portfolio piece — it focuses on the reasoning, the engineering, and the
lessons rather than on reproducible specifics.*

---

## Overview

I built a fully automated trend-following system end to end: strategy design,
a backtesting harness with realistic cost modeling, a live execution wrapper
integrated against a vendor broker platform, and the supervisory tooling to
run it unattended during market hours. It traded a single exchange-listed
gold futures contract, day-session only, and was deployed live as a
hands-on trading practice.

**What the project demonstrates:** quantitative strategy design, disciplined
in-sample/out-of-sample validation, honest cost and risk modeling, and — the
largest part by far — the reliability engineering required to run an
automated trading system against an unreliable, poorly-documented platform.

## The design, and why

What mattered was consistency more than raw P&L, so the single
highest-leverage decision was reducing variance rather than chasing return. For an overnight-sensitive commodity, the biggest variance source is
the gap, so I built the whole system around never holding through one:
day-session only, flat before the close, every day.

From there the design stayed deliberately simple — a small number of
high-quality directional trades rather than constant activity:

- a **slow daily trend filter** acting as a regime gate (long / short / flat),
- **intraday breakout entries** taken only in the direction the regime allowed,
- **volatility-targeted sizing**, scaling position size inversely to recent
  volatility so each trade contributes roughly constant risk —

$$
\text{size} \;\propto\; \frac{\sigma_{\text{target}}}{\sigma_{\text{instrument}}}
$$

- **ATR-scaled stops** (a tight initial stop, a wider trailing stop), and
- a hard **wall-clock force-flatten** before session close.

The tuned values behind these are intentionally not published here; the
reasoning is the transferable part. See
[STRATEGY.md](STRATEGY.md) for the full design discussion.

## Research rigor

The final design wasn't my first idea — it was the survivor of a structured
comparison. I tested several variants against each other on a multi-year
sample with a strict chronological train/test split, fitting on the older
data and judging only on held-out recent data.

The lesson was the classic one, learned firsthand: the variants that looked
best *in-sample* were usually the ones that fell apart *out-of-sample*. The
design I shipped wasn't the flashiest in-sample performer — it was the one
whose out-of-sample result most closely matched its in-sample result, which
is the only honest evidence that an edge is structural rather than fit to
noise. **I now treat the in-sample/out-of-sample gap as more informative
than either number on its own.**

I also modeled execution costs explicitly. The headline finding stuck with
me: adding even a single tick of slippage removed roughly a tenth of the
risk-adjusted return. Gross numbers flatter; only the net number pays.

## The engineering reality (the 90%)

I went in thinking of this as a strategy project. It turned out to be a
systems-reliability project with a strategy attached. The trading logic was
maybe 10% of the code; the other 90% was defensive engineering against a
vendor platform that behaved nothing like its documentation:

- data feeds that went silently dormant while still *looking* connected,
- a flood of placeholder messages that crashed naive parsers and made a
  message-rate health check read green while zero real trades occurred,
- order acknowledgements that meant "accepted," not "filled," forcing me to
  persist and reconcile position state across restarts, and
- timing logic that had to run on a wall clock in the exchange's timezone
  rather than off bar events, which on an illiquid product could never fire
  in time.

Handling these reliably — with watchdogs, monitoring that distinguished a
dead market from a dead feed, and atomic on-disk state — was the real work,
and the part I'm proudest of. The portable lesson: **on an obscure platform,
the reliability engineering *is* the project.** See
[BROKER_BRIDGE_NOTES.md](BROKER_BRIDGE_NOTES.md).

## The live outcome

Worth stating plainly, because it's the most instructive part: the live
contract barely traded. On many days the underlying printed on the order of
a single real trade per day. A breakout system needs a range to break out
of, and there was nothing to break, so the strategy fired almost no signals
over the live window.

That wasn't a bug — the system did exactly what it was designed to do, which
in a dead market is *nothing*. The real lesson is about the gap between a
backtest on deep, liquid proxy data and a live deployment on a thin, illiquid
contract. Liquidity is not a detail you can proxy away, and "correctly doing
nothing" beats forcing trades into a market with no edge to give. Recognizing
that, rather than overriding the system to manufacture activity, is the call
I'd defend.

## What I'd do differently

- **Test liquidity before strategy.** Measure the live product's real trade
  frequency first, and let that decide whether a movement-dependent strategy
  even fits it.
- **Treat the platform as the primary risk.** Prove I can stay connected and
  track position correctly before investing in signal logic.
- **Trust the out-of-sample gap over the headline number** — every time.

## What I take from it

I came out of this a markedly stronger engineer, and a more skeptical and
honest analyst — better at telling a real edge from a fitted one, and at
building software that survives contact with an uncooperative real-world
system. A trading strategy turned out to be a small, clean idea wrapped in a
large amount of careful plumbing, and learning to build that plumbing well
was the most valuable outcome of the project.
