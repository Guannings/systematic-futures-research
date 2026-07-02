"""
Honest index backtest on the REAL day-session index futures (index_fut_daily.csv from
a third-party data API). Session-level: enter at day-session open, exit at day-session close.
Costs = the mini index future. Chronological 70/30 IS/OOS. Vol-targeted sizing.

The cash-index version was an execution mirage (raw gap-continuation dies on the
real futures). The candidate here is V4: trade the gap ONLY when it agrees with
the daily EMA20 trend. This script checks whether V4 holds out-of-sample.

Run:  python index-futures/idx_futures_backtest.py [start_equity]
"""
from __future__ import annotations
import sys, math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
START_EQUITY = float(sys.argv[1]) if len(sys.argv) > 1 else 2_000_000.0
IS_FRAC = 0.70

MULT = 50.0            # index currency per point
MARGIN_LOT = 50_000.0
FEE_SIDE = 20.0
TAX = 0.00002
SLIP_PTS = 1.0
MAX_MARGIN_PCT = 0.50
MAX_LOTS = 200
MIN_LOTS = 1
BPY = 252.0


@dataclass
class V:
    name: str
    use_trend: bool = True
    threshold: float = 0.0      # |gap| min (fraction)
    direction: str = "continuation"
    vol_target: float = 0.20


def load():
    df = pd.read_csv(HERE / "index_fut_daily.csv", parse_dates=["date"])
    df = df.sort_values("date").reset_index(drop=True)
    df["prev"] = df["close"].shift(1)
    df = df.dropna().reset_index(drop=True)
    df["gap"] = df["open"] / df["prev"] - 1.0
    tr = np.maximum(df["high"] - df["low"],
                    np.maximum((df["high"] - df["prev"]).abs(), (df["low"] - df["prev"]).abs()))
    df["atr"] = tr.rolling(14).mean()
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    # trend KNOWN AT THE OPEN of day t = slope of EMA through t-1 (NO lookahead).
    # Using ema20[t]-ema20[t-1] would peek at today's close — the bug that
    # inflated this to Sharpe ~3. Lag it by one day.
    df["trend"] = np.sign(df["ema20"].shift(1) - df["ema20"].shift(2))
    return df.dropna().reset_index(drop=True)


def rt_cost(price, lots):
    notional = price * MULT * lots
    side = FEE_SIDE * lots + TAX * notional + SLIP_PTS * MULT * lots
    return 2 * side


def lots_for(eq, atr, vt):
    if atr <= 0:
        return MIN_LOTS
    risk = eq * vt / math.sqrt(BPY)
    per_lot = atr * MULT
    lots = int(risk / per_lot)
    max_margin = int((eq * MAX_MARGIN_PCT) / MARGIN_LOT)
    return max(MIN_LOTS, min(lots, max_margin, MAX_LOTS))


def run(v, df, eq0):
    eq = eq0
    curve, trades = [], []
    for r in df.itertuples():
        curve.append({"date": r.date, "equity": eq})
        if abs(r.gap) < v.threshold:
            continue
        side = int(np.sign(r.gap))
        if v.direction == "fade":
            side = -side
        if v.use_trend:
            if r.trend != side:
                continue
        lots = lots_for(eq, r.atr, v.vol_target)
        entry, exit_ = r.open, r.close
        gross = side * (exit_ - entry) * lots * MULT
        cost = rt_cost((entry + exit_) / 2, lots)
        net = gross - cost
        eq += net
        curve[-1]["equity"] = eq
        trades.append({"net": net, "date": r.date})
    e = pd.DataFrame(curve)
    t = pd.DataFrame(trades)
    ret = e["equity"].pct_change().fillna(0)
    sh = ret.mean() / ret.std() * math.sqrt(252) if ret.std() > 0 else float("nan")
    dd = (e["equity"] / e["equity"].cummax() - 1).min() * 100
    tot = (e["equity"].iloc[-1] / eq0 - 1) * 100
    n = len(t)
    wr = (t["net"] > 0).mean() * 100 if n else float("nan")
    worst = t["net"].min() / eq0 * 100 if n else float("nan")
    pf = (t[t.net > 0].net.sum() / abs(t[t.net <= 0].net.sum())
          if n and (t.net <= 0).any() and t[t.net <= 0].net.sum() != 0 else float("inf"))
    cagr = ((e["equity"].iloc[-1] / eq0) ** (252 / max(len(e), 1)) - 1) * 100
    return dict(sharpe=sh, ret=tot, cagr=cagr, dd=dd, n=n, wr=wr, worst=worst, pf=pf)


def fmt(name, lab, m):
    return (f"  {name:<24s} {lab:<5s}  Sharpe={m['sharpe']:>6.2f}  ret={m['ret']:>8.1f}%  "
            f"CAGR={m['cagr']:>6.1f}%  DD={m['dd']:>6.1f}%  worstDay={m['worst']:>5.2f}%  "
            f"N={m['n']:>4d}  WR={m['wr']:>4.1f}%  PF={m['pf']:>4.2f}")


def sl(d, a, b):
    n = len(d); return d.iloc[int(n*a):int(n*b)].reset_index(drop=True)


def main():
    df = load()
    print(f"REAL day-session index futures: {len(df)} sessions "
          f"{df['date'].iloc[0].date()} -> {df['date'].iloc[-1].date()}")
    print(f"Start {START_EQUITY:,.0f}  IS/OOS {IS_FRAC:.0%}  (index-futures costs, vol-target 0.20)\n")
    variants = [
        V("raw gap (no filter)", use_trend=False),
        V("V4 gap + EMA20 trend"),
        V("V4 + |gap|>0.3%", threshold=0.003),
        V("FADE control", use_trend=True, direction="fade"),
    ]
    print("=" * 116)
    for v in variants:
        for lab, d in (("IS", sl(df, 0, IS_FRAC)), ("OOS", sl(df, IS_FRAC, 1)), ("FULL", df)):
            print(fmt(v.name, lab, run(v, d, START_EQUITY)))
        print()


if __name__ == "__main__":
    main()
