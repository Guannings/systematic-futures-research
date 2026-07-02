"""
Single-stock gap-FADE strategy — backtest + variant bracket.
============================================================

The stock's open gap over-reacts to the overnight ADR move and mean-reverts
during the day session (eda showed continuation is dead; fade is +EV gross). This
is cleanly tradable on the single-stock future (mini size, day-session only, no
overnight pre-absorption).

DANGER: fade strategies have negative skew — a real trend day (earnings, macro)
hands a big loss. So this harness reports the WORST single-day loss and tests an
ATR stop, not just Sharpe. A pretty Sharpe with a catastrophic worst day is a
trap for a leveraged account.

Contract: the mini single-stock future = 100 shares of the underlying per lot,
day session only. Backtest on the cash stock (the future tracks it tightly
intraday). Ex-dividend sessions excluded (mechanical gaps that don't revert like
over-reactions).

NOTE: the underlying ticker is omitted from this published copy — set
STOCK_TICKER to your own symbol to refresh the ex-dividend calendar.
"""
from __future__ import annotations
import sys, math, warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")
HERE = Path(__file__).resolve().parent
START_EQUITY = float(sys.argv[1]) if len(sys.argv) > 1 else 2_000_000.0
IS_FRAC = 0.70
STOCK_TICKER = "STOCK.X"   # placeholder — set your own for the ex-dividend calendar

# mini single-stock future contract + cost model (account currency)
SHARES_PER_LOT = 100.0          # mini = 100 shares; standard = 2000
MARGIN_RATE    = 0.135          # stock-futures margin ~13.5% of notional
FEE_LOT_SIDE   = 10.0           # broker commission per lot per side
TAX_RATE       = 0.00002        # stock-futures transaction tax per side
SLIP_TICKS     = 1.0            # 1 tick slippage/side
TICK_CCY       = 5.0            # currency value of one tick (high-priced name)
MAX_MARGIN_PCT = 0.50
MAX_LOTS       = 500
MIN_LOTS       = 1
BARS_PER_YEAR  = 252.0


@dataclass
class Variant:
    name: str
    direction: str = "fade"           # "fade" | "continuation"
    entry_threshold: float = 0.0      # |gap| min (fraction)
    stop_atr_mult: Optional[float] = None
    take_atr_mult: Optional[float] = None   # profit target in ATR
    vol_target_annual: float = 0.20


def load():
    h = pd.read_csv(HERE / "stock_1h.csv")
    h.columns = [str(c).lower() for c in h.columns]
    dtcol = "datetime" if "datetime" in h.columns else h.columns[0]
    h["datetime"] = pd.to_datetime(h[dtcol])
    h["date"] = h["datetime"].dt.date
    h["hr"] = h["datetime"].dt.hour
    g = h.groupby("date")
    s = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(),
                      "low": g["low"].min(), "close": g["close"].last(),
                      "n": g.size()}).reset_index()
    s = s[s["n"] >= 4].reset_index(drop=True)
    s["prev"] = s["close"].shift(1)
    s = s.dropna().reset_index(drop=True)
    s["gap"] = s["open"] / s["prev"] - 1.0
    tr = np.maximum(s["high"] - s["low"],
                    np.maximum((s["high"] - s["prev"]).abs(), (s["low"] - s["prev"]).abs()))
    s["atr"] = tr.rolling(14).mean()
    # exclude ex-dividend sessions
    try:
        divs = yf.Ticker(STOCK_TICKER).dividends
        exdates = set(pd.to_datetime(divs.index).date)
        s["exdiv"] = s["date"].isin(exdates)
    except Exception:
        s["exdiv"] = False
    return s, h


def round_trip_cost(price, lots):
    notional = price * SHARES_PER_LOT * lots
    side = FEE_LOT_SIDE * lots + TAX_RATE * notional + SLIP_TICKS * TICK_CCY * SHARES_PER_LOT * lots
    return 2.0 * side


def target_lots(equity, atr_pts, vol_target, price):
    if atr_pts <= 0:
        return MIN_LOTS
    target_risk = equity * vol_target / math.sqrt(BARS_PER_YEAR)
    per_lot_risk = atr_pts * SHARES_PER_LOT
    lots = int(target_risk / per_lot_risk)
    margin_lot = MARGIN_RATE * price * SHARES_PER_LOT
    max_by_margin = int((equity * MAX_MARGIN_PCT) / max(margin_lot, 1))
    return max(MIN_LOTS, min(lots, max_by_margin, MAX_LOTS))


def run(v, sess, hourly, eq0):
    equity = eq0
    curve, trades = [], []
    hbd = {d: g for d, g in hourly.groupby("date")}
    for r in sess.itertuples():
        curve.append({"date": r.date, "equity": equity})
        if r.exdiv or not np.isfinite(r.atr) or r.atr <= 0:
            continue
        if abs(r.gap) < v.entry_threshold:
            continue
        side = int(np.sign(r.gap))
        if v.direction == "fade":
            side = -side
        entry = r.open
        lots = target_lots(equity, r.atr, v.vol_target_annual, entry)
        exit_price, reason = r.close, "eod"
        stop = entry - side * v.stop_atr_mult * r.atr if v.stop_atr_mult else None
        take = entry + side * v.take_atr_mult * r.atr if v.take_atr_mult else None
        bars = hbd.get(r.date)
        if (stop or take) and bars is not None:
            for b in bars[bars["hr"] >= 9].itertuples():
                if stop is not None and ((side > 0 and b.low <= stop) or (side < 0 and b.high >= stop)):
                    exit_price, reason = stop, "stop"; break
                if take is not None and ((side > 0 and b.high >= take) or (side < 0 and b.low <= take)):
                    exit_price, reason = take, "take"; break
        gross = side * (exit_price - entry) * lots * SHARES_PER_LOT
        cost = round_trip_cost((entry + exit_price) / 2, lots)
        net = gross - cost
        equity += net
        curve[-1]["equity"] = equity
        trades.append({"date": r.date, "side": side, "lots": lots, "net": net,
                       "gap_pct": r.gap * 100, "reason": reason})
    return metrics(pd.DataFrame(curve), pd.DataFrame(trades), eq0)


def metrics(eq, tr, eq0):
    if eq.empty:
        return {"error": "empty"}
    ret = eq["equity"].pct_change().fillna(0.0)
    sharpe = ret.mean() / ret.std() * math.sqrt(252) if ret.std() > 0 else float("nan")
    total = eq["equity"].iloc[-1] / eq0 - 1.0
    dd = (eq["equity"] / eq["equity"].cummax() - 1.0).min()
    n = len(tr)
    if n:
        wr = (tr["net"] > 0).mean() * 100
        worst = tr["net"].min()
        worst_pct = worst / eq0 * 100
        skew = tr["net"].skew()
        pf = (tr[tr.net > 0].net.sum() / abs(tr[tr.net <= 0].net.sum())
              if (tr.net <= 0).any() and tr[tr.net <= 0].net.sum() != 0 else float("inf"))
    else:
        wr = worst = worst_pct = skew = pf = float("nan")
    return {"sharpe": sharpe, "ret": total * 100, "dd": dd * 100, "n": n,
            "wr": wr, "pf": pf, "worst_day_pct": worst_pct, "skew": skew}


def fmt(name, lab, m):
    if "error" in m:
        return f"  {name:<26s} {lab:<5s}  -- {m['error']}"
    return (f"  {name:<26s} {lab:<5s}  Sharpe={m['sharpe']:>6.2f}  ret={m['ret']:>7.2f}%  "
            f"DD={m['dd']:>6.2f}%  worstDay={m['worst_day_pct']:>6.2f}%  "
            f"skew={m['skew']:>5.2f}  N={m['n']:>4d}  WR={m['wr']:>4.1f}%  PF={m['pf']:>4.2f}")


def sl(df, a, b):
    n = len(df); return df.iloc[int(n*a):int(n*b)].reset_index(drop=True)


def main():
    sess, hourly = load()
    nex = int(sess["exdiv"].sum())
    print(f"Loaded {len(sess)} sessions ({nex} ex-div excluded)  "
          f"{sess['date'].iloc[0]} -> {sess['date'].iloc[-1]}")
    print(f"Start equity {START_EQUITY:,.0f}   IS/OOS {IS_FRAC:.0%}   (mini single-stock future, day-only)\n")
    variants = [
        Variant("F1 fade all gaps"),
        Variant("F2 fade |gap|>0.5%", entry_threshold=0.005),
        Variant("F3 fade +2xATR stop", stop_atr_mult=2.0),
        Variant("F4 fade +1xATR stop", stop_atr_mult=1.0),
        Variant("F5 fade stop+target", stop_atr_mult=1.5, take_atr_mult=1.0),
        Variant("F6 CONTINUATION (ctrl)", direction="continuation"),
    ]
    print("=" * 120)
    for v in variants:
        for lab, d in (("IS", sl(sess, 0, IS_FRAC)), ("OOS", sl(sess, IS_FRAC, 1)), ("FULL", sess)):
            print(fmt(v.name, lab, run(v, d, hourly, START_EQUITY)))
        print()


if __name__ == "__main__":
    main()
