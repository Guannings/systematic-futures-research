"""
Cost-aware backtest of the cross-sectional INTRADAY reversal.
Signal: -(yesterday close-to-close return), cross-sectionally demeaned, lagged.
Trade: enter at open, exit at close (day-session only). Flatten daily.

The make-or-break is cost. the venue tick sizes are large in % for high-priced
stocks (5 tick on a 1000 stock = 0.5%), so we:
  - model realistic per-name one-way cost from the the exchange tick ladder
  - test the full basket vs a LOW-COST subset vs concentrated extremes
  - split IS/OOS and show per-year
"""
import numpy as np, pandas as pd

cl = pd.read_csv("xsec/stocks_close.csv", index_col=0, parse_dates=True).dropna(axis=1, how="all")
op = pd.read_csv("xsec/stocks_open.csv", index_col=0, parse_dates=True)[cl.columns]
cl, op = cl.align(op, join="inner"); cl = cl.dropna(how="any"); op = op.loc[cl.index]
oc = cl/op - 1.0
cc = cl.pct_change()

def tick(p):
    if p < 10: return 0.01
    if p < 50: return 0.05
    if p < 100: return 0.1
    if p < 500: return 0.5
    if p < 1000: return 1.0
    return 5.0

# one-way cost fraction per name per day = half a tick (spread) + tax + fee
TAX_FEE = 0.00004
cost1way = cl.map(lambda p: 0.5*tick(p)/p + TAX_FEE if p==p and p>0 else np.nan)
med_tickpct = (cl.map(lambda p: tick(p)/p if p==p and p>0 else np.nan)).median()*100

def run(cols, topk=None, label=""):
    C = list(cols)
    sig = (-cc[C]).shift(1)
    w = sig.sub(sig.mean(axis=1), axis=0)
    if topk:  # keep only the topk most extreme longs and shorts each day
        def keepk(row):
            r = row.copy(); r[:] = 0.0
            s = row.dropna().sort_values()
            for c in s.index[:topk]: r[c] = s[c]      # most negative weight (shorts)
            for c in s.index[-topk:]: r[c] = s[c]      # most positive (longs)
            return r
        w = w.apply(keepk, axis=1)
    w = w.div(w.abs().sum(axis=1), axis=0)            # gross exposure 1
    gross = (w * oc[C]).sum(axis=1)
    # cost: enter full |w| at open + exit full |w| at close = 2 * |w| * cost1way
    cost = (w.abs() * cost1way[C] * 2).sum(axis=1)
    net = (gross - cost).replace([np.inf,-np.inf], np.nan).dropna()
    gross = gross.loc[net.index]
    def sh(x): return x.mean()/x.std()*np.sqrt(252) if x.std()>0 else 0
    n=len(net); k=int(n*0.7)
    print(f"  {label:<30s} N={cl.shape[1] if not topk else f'{topk}x2':<5} "
          f"GROSS Sh={sh(gross):+.2f}({gross.mean()*252*100:+.0f}%)  "
          f"NET Sh={sh(net):+.2f}({net.mean()*252*100:+.0f}%)  "
          f"IS={sh(net[:k]):+.2f} OOS={sh(net[k:]):+.2f}  dailycost={cost.mean()*100:.3f}%")
    return net

print(f"{len(cl)} days, {cl.shape[1]} stocks")
print("median tick% per name:"); print(med_tickpct.sort_values().to_string())
allc = list(cl.columns)
lowcost = list(med_tickpct[med_tickpct < 0.15].index)   # < 0.15% tick names
print(f"\nlow-cost subset (tick%<0.15): {len(lowcost)} names: {lowcost}")
print("\nResults (gross then net of realistic costs):")
run(allc, label="full basket")
run(lowcost, label="low-cost subset")
run(lowcost, topk=3, label="low-cost extremes (3x3)")
run(allc, topk=3, label="full extremes (3x3)")
r = run(lowcost, topk=4, label="low-cost extremes (4x4)")
print("\nPer-year (low-cost extremes 4x4, NET):")
for yr,g in r.groupby(r.index.year):
    print(f"  {yr}: {g.sum()*100:+6.1f}%  Sharpe={g.mean()/g.std()*np.sqrt(252) if g.std()>0 else 0:+.2f}")
