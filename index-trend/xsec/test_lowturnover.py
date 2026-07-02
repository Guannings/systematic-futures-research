"""
Low-turnover, OVERNIGHT-HELD strategies (now legal: can hold positions across
days, just no night trading). These pay the spread only when rebalancing, so
they escape the tick-cost wall that killed every intraday strategy.

Return basis = close-to-close (captures the overnight drift we can now keep).
Cost charged only on rebalance turnover. Lookahead-free (signal through t-1).
"""
import numpy as np, pandas as pd

cl = pd.read_csv("xsec/stocks_close.csv", index_col=0, parse_dates=True).dropna(axis=1, how="all")
cl = cl.dropna(how="any")
ret = cl.pct_change()

def tick(p):
    if p<10: return 0.01
    if p<50: return 0.05
    if p<100: return 0.1
    if p<500: return 0.5
    if p<1000: return 1.0
    return 5.0
cost1way = cl.map(lambda p: 0.5*tick(p)/p + 0.00004)   # half-spread + tax/fee

def sh(x): return x.mean()/x.std()*np.sqrt(252) if x.std()>0 else 0
def years(idx): return (idx[-1]-idx[0]).days/365.25

def backtest(weight_fn, rebal=20, label="", long_only=False):
    dates = cl.index
    held = pd.Series(0.0, index=cl.columns)
    pnl = []
    for i, d in enumerate(dates):
        # rebalance every `rebal` days using info through i-1
        if i % rebal == 0 and i > 130:
            w = weight_fn(i)
            if w is not None:
                turn = (w - held).abs()
                c = (turn * cost1way.iloc[i]).sum()
                held = w
                pnl.append((d, -c))   # cost hit on rebalance day (besides the return below)
        # daily return from held weights
        if i > 0:
            r = (held * ret.iloc[i]).sum()
            pnl.append((d, r))
    s = pd.Series(dict(pnl)) if False else pd.Series([p for _,p in pnl], index=[d for d,_ in pnl])
    s = s.groupby(s.index).sum()
    s = s.replace([np.inf,-np.inf], np.nan).dropna()
    n=len(s); k=int(n*0.7)
    print(f"  {label:<38s} NET Sh={sh(s):+.2f}  ann={s.mean()*252*100:+.1f}%  "
          f"IS={sh(s[:k]):+.2f} OOS={sh(s[k:]):+.2f}  maxDD={((1+s).cumprod()/(1+s).cumprod().cummax()-1).min()*100:.1f}%")
    return s

def mom_weights(form, long_only=False, topk=None):
    def f(i):
        past = cl.iloc[i-1] / cl.iloc[i-1-form] - 1.0
        if past.isna().any(): past = past.dropna()
        if len(past) < 6: return None
        if topk:
            w = pd.Series(0.0, index=cl.columns)
            srt = past.sort_values()
            if not long_only:
                for c in srt.index[:topk]: w[c] = -1.0
            for c in srt.index[-topk:]: w[c] = +1.0
        else:
            w = past - past.mean()
            if long_only: w = w.clip(lower=0)
            w = w.reindex(cl.columns).fillna(0.0)
        s = w.abs().sum()
        return w/s if s>0 else None
    return f

print(f"{len(cl)} days, {cl.shape[1]} stocks, {cl.index[0].date()}->{cl.index[-1].date()}")
bh = ret.mean(axis=1)
print(f"\n  {'equal-weight buy&hold (beta benchmark)':<38s} Sh={sh(bh):+.2f}  ann={bh.mean()*252*100:+.1f}%")
print("\nLong-SHORT (market-neutral alpha), rebalance every 20d:")
backtest(mom_weights(60), rebal=20, label="XS momentum 60d L/S")
backtest(mom_weights(120), rebal=20, label="XS momentum 120d L/S")
backtest(mom_weights(20), rebal=20, label="XS momentum 20d L/S")
backtest(mom_weights(60, topk=4), rebal=20, label="XS mom 60d L/S extremes 4x4")
print("\nLONG-ONLY momentum (alpha + bull beta), rebalance every 20d:")
backtest(mom_weights(60, long_only=True, topk=5), rebal=20, label="long-only top-5 mom 60d")
backtest(mom_weights(120, long_only=True, topk=5), rebal=20, label="long-only top-5 mom 120d")
