"""
Trend-hold (overnight) backtest — the SELECTED strategy.
========================================================

Long the index while the prior close is above its EMA(50), carried OVERNIGHT,
flat otherwise; flip only on a trend break. This is the configuration the live
bot runs in `trend_hold` mode. It is the only index approach that cleared costs,
precisely because holding overnight pays the spread only on trend flips instead
of on a forced daily round-trip.

Lookahead-free: the position for day t is decided from the EMA(50) and close
through day t-1 only. Return earned on day t is the day-t close-to-close move of
the real day-session index future (overnight carry included). Costs are charged
only on the days the position actually changes.

Sharpe is leverage-invariant (returns and per-flip costs both scale with size),
so one Sharpe is reported; annualized return and drawdown are shown at 1x and 3x.
Chronological 70/30 IS/OOS. Buy-and-hold shown as the beta benchmark.

Run:
  python idx_trend_backtest.py           # headline result
  python idx_trend_backtest.py --full    # + leg stats, per-year, EMA & cost sweeps
"""
from __future__ import annotations
import sys, math
from pathlib import Path
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
IS_FRAC = 0.70
TREND_N = 50

# cost model (per lot, per side), expressed as a return drag on flip days.
MULT      = 50.0        # currency units per index point
FEE_SIDE  = 20.0        # per lot per side
TAX_RATE  = 0.00002     # per side, index futures
SLIP_PTS  = 1.0         # index points per side


def _raw():
    df = pd.read_csv(HERE / "index_fut_daily.csv", parse_dates=["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return df[df["close"] > 0].reset_index(drop=True)


def build(trend_n: int = TREND_N, slip: float = SLIP_PTS):
    """Return (frame, daily strategy return series, flip mask) for given params."""
    df = _raw().copy()
    df["ema"] = df["close"].ewm(span=trend_n, adjust=False).mean()
    df["pos"] = (df["close"].shift(1) > df["ema"].shift(1)).astype(float)   # no lookahead
    df["ret"] = df["close"].pct_change()
    df = df.iloc[trend_n:].reset_index(drop=True)
    flips = np.abs(np.diff(np.concatenate([[0.0], df["pos"].values])))
    price = df["close"].values
    cost = flips * (FEE_SIDE / (price * MULT) + TAX_RATE + slip * MULT / (price * MULT))
    r = pd.Series(df["pos"].values * df["ret"].fillna(0).values - cost, index=df["date"])
    return df, r, flips


def sharpe(x):
    x = x.dropna()
    return x.mean() / x.std() * math.sqrt(252) if x.std() > 0 else float("nan")


def metrics(r, lev=1.0):
    x = (r * lev).dropna()
    eq = (1 + x).cumprod()
    return {"sharpe": sharpe(x), "ann": x.mean() * 252 * 100,
            "dd": (eq / eq.cummax() - 1).min() * 100}


def headline():
    df, r, flips = build()
    n = len(r); k = int(n * IS_FRAC)
    n_flips = int(flips.sum())
    print("Trend-hold (overnight) on real day-session index futures")
    print(f"{n} sessions  {df['date'].iloc[0].date()} -> {df['date'].iloc[-1].date()}  "
          f"| EMA{TREND_N} regime | {n_flips} flips ({n_flips/(n/252):.1f}/yr)\n")
    print(f"  Sharpe (leverage-invariant):  FULL {sharpe(r):+.2f}   "
          f"IS {sharpe(r.iloc[:k]):+.2f}   OOS {sharpe(r.iloc[k:]):+.2f}")
    print(f"  Annualized return:   1x {metrics(r,1)['ann']:+.1f}%    3x {metrics(r,3)['ann']:+.1f}%")
    print(f"  Max drawdown:        1x {metrics(r,1)['dd']:+.1f}%    3x {metrics(r,3)['dd']:+.1f}%")
    bh = df.set_index("date")["ret"]; mb = metrics(bh)
    print(f"\n  buy & hold benchmark:  Sharpe {mb['sharpe']:+.2f}   "
          f"ann {mb['ann']:+.1f}%   maxDD {mb['dd']:+.1f}%")
    return df, r


def diagnostics():
    df, r, _ = build()
    pos = df["pos"].values

    # --- leg analysis ---
    legs = []; i = 0
    while i < len(pos):
        if pos[i] == 1:
            j = i
            while j < len(pos) and pos[j] == 1:
                j += 1
            legs.append((j - i, (1 + r.iloc[i:j]).prod() - 1)); i = j
        else:
            i += 1
    legs = pd.DataFrame(legs, columns=["days", "ret"])
    print("\n=== LEG ANALYSIS (each long run between trend flips) ===")
    print(f"  time long: {pos.mean()*100:.1f}%   flat: {(1-pos.mean())*100:.1f}%")
    print(f"  long legs: {len(legs)}   avg {legs['days'].mean():.0f}d, median {legs['days'].median():.0f}d")
    print(f"  leg win rate: {(legs['ret']>0).mean()*100:.0f}%   "
          f"best {legs['ret'].max()*100:+.1f}%   worst {legs['ret'].min()*100:+.1f}%")
    print(f"  worst single in-position day (1x): {r.min()*100:+.2f}%  "
          f"(= {r.min()*3*100:+.1f}% at 3x — the overnight gap risk)")

    # --- per-year ---
    bh = df.set_index("date")["ret"]
    print("\n=== PER-YEAR (strategy 1x vs buy & hold) ===")
    for yr in sorted(set(r.index.year)):
        rs = r[r.index.year == yr]; rb = bh[bh.index.year == yr]
        print(f"  {yr}: strat {(1+rs).prod()*100-100:+6.1f}% (Sh {sharpe(rs):+.2f})   "
              f"b&h {(1+rb).prod()*100-100:+6.1f}%")

    # --- EMA-length robustness ---
    print("\n=== PARAMETER ROBUSTNESS (EMA length) ===")
    for nn in [20, 30, 50, 75, 100, 150, 200]:
        _, rr, _ = build(trend_n=nn); kk = int(len(rr) * IS_FRAC)
        print(f"  EMA{nn:>3}: Sharpe FULL {sharpe(rr):+.2f}  IS {sharpe(rr[:kk]):+.2f}  "
              f"OOS {sharpe(rr[kk:]):+.2f}  ann {metrics(rr)['ann']:+.1f}%  maxDD {metrics(rr)['dd']:.1f}%")

    # --- cost robustness (the thesis test) ---
    print("\n=== COST ROBUSTNESS (slippage per side, EMA50) ===")
    for s in [0.0, 0.5, 1.0, 2.0, 4.0]:
        _, rr, _ = build(slip=s)
        print(f"  slip {s:>3} pt: Sharpe {sharpe(rr):+.2f}  ann {metrics(rr)['ann']:+.1f}%")
    print("  (near-flat across slippage = overnight hold escapes the cost wall)")


def main():
    headline()
    if "--full" in sys.argv:
        diagnostics()


if __name__ == "__main__":
    main()
