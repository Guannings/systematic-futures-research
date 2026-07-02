"""
Single-stock gap study (for a single-stock future on the exchange).

WHY A SINGLE STOCK: single-stock futures on this exchange have NO night session,
so the open gap is fully intact and capturable in the day session (which is all
the venue allows). The chosen name is a large-cap with a liquid overseas ADR that
trades overnight — the driver of the gap. The backtest is on the cash stock,
which IS the tradable day session, so there is no cash-vs-futures overnight blind
spot (the problem the index future had).

Questions:
  Q1  Does the cash-stock open gap CONTINUE into the close? (same test as the index)
  Q2  Does last night's overseas ADR move predict the stock's day-session direction?

Outputs CSVs into this folder.

NOTE: the specific tickers are omitted from this published copy. Set STOCK_TICKER
(cash stock, day session) and ADR_TICKER (its overnight overseas listing) to your
own symbols before running.
"""
from __future__ import annotations
import warnings
from pathlib import Path
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")
HERE = Path(__file__).resolve().parent

STOCK_TICKER = "STOCK.X"   # cash stock, day session only (placeholder — set your own)
ADR_TICKER   = "ADR.X"     # its overnight overseas ADR (placeholder — set your own)


def flat(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [str(c).lower() for c in df.columns]
    return df


def to_exchange_local(df):
    idx = pd.to_datetime(df.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    df.index = idx.tz_localize(None)   # exchange-local naive timestamps
    return df


# --- fetch ---
h = to_exchange_local(flat(yf.download(STOCK_TICKER, period="730d", interval="1h",
                                       progress=False, auto_adjust=False)))
h = h.dropna(subset=["open", "high", "low", "close"])
h["date"] = h.index.date
h["hr"] = h.index.hour
h.reset_index().rename(columns={"index": "datetime"}).to_csv(HERE / "stock_1h.csv", index=False)

d = flat(yf.download(STOCK_TICKER, period="max", interval="1d",
                     progress=False, auto_adjust=False)).dropna()
d.to_csv(HERE / "stock_daily.csv")

adr = flat(yf.download(ADR_TICKER, period="2y", interval="1d",
                       progress=False, auto_adjust=False)).dropna()

print(f"stock hourly: {len(h)} bars, hours={sorted(set(h['hr']))}, "
      f"{h.index[0].date()} -> {h.index[-1].date()}")

# --- sessions ---
g = h.groupby("date")
s = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(),
                  "low": g["low"].min(), "close": g["close"].last(),
                  "n": g.size()}).reset_index()
s = s[s["n"] >= 4].reset_index(drop=True)
s["prev"] = s["close"].shift(1)
s = s.dropna().reset_index(drop=True)
s["gap"] = (s["open"] / s["prev"] - 1) * 100
s["intra"] = (s["close"] / s["open"] - 1) * 100

# --- Q1: gap continuation ---
b = np.polyfit(s["gap"], s["intra"], 1)
big = s[s["gap"].abs() > 0.1]
same = (np.sign(big["gap"]) == np.sign(big["intra"])).mean()
r = np.sign(s["gap"]) * s["intra"]
print(f"\nsessions={len(s)}  avg|gap|={s['gap'].abs().mean():.3f}%  "
      f"avg|intra|={s['intra'].abs().mean():.3f}%")
print("Q1 GAP -> intraday(open->close)")
print(f"   slope={b[0]:+.3f}  corr={np.corrcoef(s['gap'], s['intra'])[0,1]:+.3f}  "
      f"continue={same*100:.1f}%")
print(f"   gap-continuation gross: mean/day={r.mean():+.4f}%  hit={(r>0).mean()*100:.1f}%  "
      f"Sharpe~{r.mean()/r.std()*np.sqrt(252):+.2f}")

# --- Q2: ADR overnight -> next day-session ---
# The overseas ADR close on day t should predict the stock's open on day t+1.
adr_ret = (adr["close"] / adr["close"].shift(1) - 1) * 100
adr_ret.index = pd.to_datetime(adr_ret.index).date
adr_df = pd.DataFrame({"adr_overnight": adr_ret}).reset_index().rename(columns={"index": "adr_date"})
s2 = s.copy()
s2["adr_prev"] = np.nan
adr_map = dict(zip(adr_df["adr_date"], adr_df["adr_overnight"]))
loc_dates = list(s2["date"])
for i, dt in enumerate(loc_dates):
    # find ADR return for the overseas session that closed before this open
    for back in range(1, 5):
        cand = dt - pd.Timedelta(days=back)
        if cand in adr_map:
            s2.loc[i, "adr_prev"] = adr_map[cand]
            break
s2 = s2.dropna(subset=["adr_prev"]).reset_index(drop=True)
b2 = np.polyfit(s2["adr_prev"], s2["intra"], 1)
b3 = np.polyfit(s2["adr_prev"], s2["gap"], 1)
print("\nQ2 ADR overnight return -> next day session")
print(f"   ADR -> gap:      slope={b3[0]:+.3f}  corr={np.corrcoef(s2['adr_prev'], s2['gap'])[0,1]:+.3f} "
      f"(how much the gap already reflects the ADR)")
print(f"   ADR -> intraday: slope={b2[0]:+.3f}  corr={np.corrcoef(s2['adr_prev'], s2['intra'])[0,1]:+.3f} "
      f"(continuation left for the day session)")
radr = np.sign(s2["adr_prev"]) * s2["intra"]
print(f"   trade-day-in-ADR-direction gross: mean/day={radr.mean():+.4f}%  "
      f"hit={(radr>0).mean()*100:.1f}%  Sharpe~{radr.mean()/radr.std()*np.sqrt(252):+.2f}")
