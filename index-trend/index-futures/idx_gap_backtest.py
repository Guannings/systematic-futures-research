"""
index Gap-Momentum strategy — backtest + variant bracket.
========================================================

Clean-sheet design for the mini index futures contract, separate from the gold
GDVT trend system. The edge (established in eda_twii.py): the cash index
gaps at the 09:00 open to react to the overnight US semis move, then CONTINUES
that direction into the 13:30 close 62% of the time (slope +0.46, corr +0.34).

Core trade
  - At the session open, measure gap = open / prev_close - 1.
  - If |gap| >= threshold, enter in the gap direction (continuation).
  - Hold intraday; exit at the close (force-flat). Optional ATR stop.
  - Vol-targeted sizing; aggressive (the objective is absolute return).

No overnight hold — fits the free cash-index data AND reuses the existing
day-flat live machinery (force-flatten, position persistence, tick filter).

Validation: chronological 70/30 train/test split. A variant is real only if
BOTH in-sample and out-of-sample Sharpe are positive and OOS doesn't collapse.

Run:  python idx_gap_backtest.py [start_equity]
"""
from __future__ import annotations
import sys
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
INTRADAY_CSV = HERE / "cash_index_1h.csv"
START_EQUITY = float(sys.argv[1]) if len(sys.argv) > 1 else 2_000_000.0
IS_FRAC = 0.70

# --- index-futures contract + cost model (account currency) ---------------------------------------
IDX_MULTIPLIER   = 50.0       # 50 per index point
IDX_MARGIN_LOT   = 50_000.0   # originating margin/lot (account currency) — VERIFY vs the exchange's current table
IDX_FEE_LOT_SIDE = 20.0       # broker commission per lot per side (account currency, retail ~12-30)
IDX_TAX_RATE     = 0.00002    # futures transaction tax, index futures: 0.002% notional/side
IDX_SLIP_POINTS  = 1.0        # 1 index point slippage per side (= 50/lot)

MAX_MARGIN_PCT = 0.50
MAX_LOTS       = 200          # scale ceiling; sizing binds first
MIN_LOTS       = 1
BARS_PER_YEAR  = 252.0        # one gap trade per session


@dataclass
class Variant:
    name: str
    entry_threshold: float = 0.0015   # |gap| min to trade (fraction, 0.15%)
    entry_at: str = "open"            # "open" | "first_close" (10:00)
    direction: str = "continuation"   # "continuation" | "fade"
    stop_atr_mult: Optional[float] = None   # intraday stop in ATR units
    max_gap: Optional[float] = None         # skip gaps larger than this (exhaustion)
    trend_filter: Optional[str] = None      # None | "ema20" (daily EMA20 slope)
    vol_target_annual: float = 0.20


# ---------------------------------------------------------------------------
def load_sessions() -> pd.DataFrame:
    df = pd.read_csv(INTRADAY_CSV, parse_dates=["datetime"])
    df["date"] = df["datetime"].dt.date
    df["hr"] = df["datetime"].dt.hour
    g = df.groupby("date")
    sess = pd.DataFrame({
        "open":  g["open"].first(),
        "high":  g["high"].max(),
        "low":   g["low"].min(),
        "close": g["close"].last(),
        "n":     g.size(),
    }).reset_index()
    sess = sess[sess["n"] >= 4].reset_index(drop=True)
    sess["prev_close"] = sess["close"].shift(1)
    sess["first_close"] = df[df["hr"] == 10].groupby("date")["close"].last().reindex(sess["date"]).values
    sess = sess.dropna(subset=["prev_close"]).reset_index(drop=True)
    sess["gap"] = sess["open"] / sess["prev_close"] - 1.0
    # 14-session ATR (points) for sizing/stops
    tr = np.maximum(sess["high"] - sess["low"],
                    np.maximum((sess["high"] - sess["prev_close"]).abs(),
                               (sess["low"] - sess["prev_close"]).abs()))
    sess["atr"] = tr.rolling(14).mean()
    # daily EMA20 of close for the optional trend filter
    sess["ema20"] = sess["close"].ewm(span=20, adjust=False).mean()
    sess["ema20_prev"] = sess["ema20"].shift(1)
    # keep hourly bars for intraday stop checking
    return sess, df


def _round_trip_cost(price: float, lots: int) -> float:
    notional = price * IDX_MULTIPLIER * lots
    side = (IDX_FEE_LOT_SIDE * lots
            + IDX_TAX_RATE * notional
            + IDX_SLIP_POINTS * IDX_MULTIPLIER * lots)
    return 2.0 * side


def _target_lots(equity: float, atr_pts: float, vol_target: float) -> int:
    if atr_pts <= 0:
        return MIN_LOTS
    target_risk = equity * vol_target / math.sqrt(BARS_PER_YEAR)
    per_lot_risk = atr_pts * IDX_MULTIPLIER
    lots = int(target_risk / per_lot_risk)
    max_by_margin = int((equity * MAX_MARGIN_PCT) / IDX_MARGIN_LOT)
    lots = min(lots, max_by_margin, MAX_LOTS)
    return max(MIN_LOTS, lots)


def run(variant: Variant, sess: pd.DataFrame, hourly: pd.DataFrame,
        equity0: float) -> dict:
    equity = equity0
    curve = []
    trades = []
    hourly_by_date = {d: g for d, g in hourly.groupby("date")}

    for r in sess.itertuples():
        if not np.isfinite(r.atr) or r.atr <= 0:
            curve.append({"date": r.date, "equity": equity})
            continue

        gap = r.gap
        if abs(gap) < variant.entry_threshold:
            curve.append({"date": r.date, "equity": equity}); continue
        if variant.max_gap is not None and abs(gap) > variant.max_gap:
            curve.append({"date": r.date, "equity": equity}); continue

        side = int(np.sign(gap))
        if variant.direction == "fade":
            side = -side

        # optional daily-trend filter: only trade with EMA20 slope
        if variant.trend_filter == "ema20":
            slope = r.ema20 - r.ema20_prev
            if np.sign(slope) != side:
                curve.append({"date": r.date, "equity": equity}); continue

        # entry price
        entry = r.open if variant.entry_at == "open" else r.first_close
        if not np.isfinite(entry):
            curve.append({"date": r.date, "equity": equity}); continue

        lots = _target_lots(equity, r.atr, variant.vol_target_annual)

        # exit: close, unless an intraday ATR stop is hit first
        exit_price = r.close
        reason = "eod_close"
        if variant.stop_atr_mult is not None:
            stop = entry - side * variant.stop_atr_mult * r.atr
            bars = hourly_by_date.get(r.date)
            if bars is not None:
                start_hr = 9 if variant.entry_at == "open" else 10
                for b in bars[bars["hr"] >= start_hr].itertuples():
                    if side > 0 and b.low <= stop:
                        exit_price = stop; reason = "stop"; break
                    if side < 0 and b.high >= stop:
                        exit_price = stop; reason = "stop"; break

        gross = side * (exit_price - entry) * lots * IDX_MULTIPLIER
        cost = _round_trip_cost((entry + exit_price) / 2, lots)
        net = gross - cost
        equity += net
        trades.append({"date": r.date, "side": side, "lots": lots,
                       "gap_pct": gap * 100, "entry": entry, "exit": exit_price,
                       "gross": gross, "cost": cost, "net": net, "reason": reason})
        curve.append({"date": r.date, "equity": equity})

    eq = pd.DataFrame(curve)
    tr = pd.DataFrame(trades)
    return _metrics(eq, tr, equity0)


def _metrics(eq: pd.DataFrame, tr: pd.DataFrame, equity0: float) -> dict:
    if eq.empty:
        return {"error": "empty"}
    ret = eq["equity"].pct_change().fillna(0.0)
    sharpe = ret.mean() / ret.std() * math.sqrt(252) if ret.std() > 0 else float("nan")
    total = eq["equity"].iloc[-1] / equity0 - 1.0
    peak = eq["equity"].cummax()
    dd = (eq["equity"] / peak - 1.0).min()
    n = len(tr)
    if n:
        wins = tr[tr["net"] > 0]
        wr = len(wins) / n
        pf = (wins["net"].sum() / abs(tr[tr["net"] <= 0]["net"].sum())
              if (tr["net"] <= 0).any() and tr[tr["net"] <= 0]["net"].sum() != 0 else float("inf"))
    else:
        wr = pf = float("nan")
    return {"sharpe": sharpe, "total_return_pct": total * 100,
            "max_drawdown_pct": dd * 100, "n_trades": n,
            "win_rate_pct": wr * 100, "profit_factor": pf}


def slice_df(df, a, b):
    n = len(df)
    return df.iloc[int(n * a):int(n * b)].reset_index(drop=True)


def fmt(name, label, m):
    if "error" in m:
        return f"  {name:<26s} {label:<5s}  -- {m['error']}"
    return (f"  {name:<26s} {label:<5s}  Sharpe={m['sharpe']:>6.2f}  "
            f"ret={m['total_return_pct']:>8.2f}%  DD={m['max_drawdown_pct']:>7.2f}%  "
            f"trades={m['n_trades']:>4d}  WR={m['win_rate_pct']:>5.1f}%  PF={m['profit_factor']:>5.2f}")


def main():
    sess, hourly = load_sessions()
    print(f"Loaded {len(sess)} sessions  {sess['date'].iloc[0]} -> {sess['date'].iloc[-1]}")
    print(f"Start equity: {START_EQUITY:,.0f}   IS/OOS split at {IS_FRAC:.0%}\n")

    variants = [
        Variant("V1 gap-mom baseline"),
        Variant("V2 thr=0.30%", entry_threshold=0.0030),
        Variant("V3 thr=0.00% (all gaps)", entry_threshold=0.0),
        Variant("V4 +EMA20 trend filter", trend_filter="ema20"),
        Variant("V5 +2xATR stop", stop_atr_mult=2.0),
        Variant("V6 skip gaps>2%", max_gap=0.02),
        Variant("V7 enter@10:00 (confirm)", entry_at="first_close"),
        Variant("V8 FADE (control)", direction="fade"),
    ]

    print("=" * 108)
    for v in variants:
        for lab, sl in (("IS", slice_df(sess, 0, IS_FRAC)),
                        ("OOS", slice_df(sess, IS_FRAC, 1.0)),
                        ("FULL", sess)):
            m = run(v, sl, hourly, START_EQUITY)
            print(fmt(v.name, lab, m))
        print()


if __name__ == "__main__":
    main()
