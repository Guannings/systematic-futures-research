"""
Cross-sectional exploration across the tradable single-stock-future underlyings.
A genuinely different family from the (dead) single-instrument index strategies.

All signals are lookahead-free and DAY-SESSION TRADABLE: the signal is known by
the open of day t, and we capture the open->close (intraday) return of day t,
which is exactly what a day-session stock-future trade earns.

Portfolio = dollar-neutral cross-sectional long/short, gross exposure 1.
Reports gross Sharpe; costs assessed after.
"""
import numpy as np, pandas as pd

cl = pd.read_csv("xsec/stocks_close.csv", index_col=0, parse_dates=True).dropna(axis=1, how="all")
op = pd.read_csv("xsec/stocks_open.csv", index_col=0, parse_dates=True)[cl.columns]
cl, op = cl.align(op, join="inner")
cl = cl.dropna(how="any"); op = op.loc[cl.index]

oc = (cl / op - 1.0)                 # intraday open->close (tradable return), day t
cc = cl.pct_change()                 # close-to-close
gap = (op / cl.shift(1) - 1.0)       # overnight gap (known at open of day t)
mom20 = cl.shift(1) / cl.shift(21) - 1.0   # 20d momentum through t-1

def xs_demean(x):  # cross-sectional demean each day
    return x.sub(x.mean(axis=1), axis=0)

def port(signal, ret, lag_signal=True):
    s = signal.shift(1) if lag_signal else signal      # if signal already known at open of t, no lag
    w = xs_demean(s)
    w = w.div(w.abs().sum(axis=1), axis=0)             # gross exposure = 1
    r = (w * ret).sum(axis=1)
    r = r.replace([np.inf, -np.inf], np.nan).dropna()
    return r

def rpt(name, r):
    if len(r) < 50 or r.std() == 0:
        print(f"  {name:<34s} insufficient"); return
    sh = r.mean()/r.std()*np.sqrt(252)
    print(f"  {name:<34s} mean/day={r.mean()*100:+.4f}%  Sharpe={sh:+.2f}  "
          f"ann={r.mean()*252*100:+.1f}%  hit={(r>0).mean()*100:.1f}%")

print(f"{len(cl)} days, {cl.shape[1]} stocks, {cl.index[0].date()}->{cl.index[-1].date()}")
print("\nCross-sectional, return = intraday open->close of day t (day-session tradable):")
# signal known at PRIOR close -> must lag into day t
rpt("1d reversal (-cc[t-1])",        port(-cc, oc, lag_signal=True))
rpt("intraday reversal (-oc[t-1])",  port(-oc, oc, lag_signal=True))
rpt("20d momentum (+mom20)",         port(mom20, oc, lag_signal=False))  # mom20 already thru t-1
rpt("20d reversal (-mom20)",         port(-mom20, oc, lag_signal=False))
# gap is known AT the open of day t -> no lag needed
rpt("GAP fade (-gap[t]) intraday",   port(-gap, oc, lag_signal=False))
rpt("GAP momentum (+gap[t]) intraday",port(gap, oc, lag_signal=False))

print("\nFor reference, return = close-to-close of day t (needs overnight hold = NOT day-session-legal):")
rpt("1d reversal -> cc",             port(-cc, cc, lag_signal=True))
rpt("20d momentum -> cc",            port(mom20, cc, lag_signal=False))
