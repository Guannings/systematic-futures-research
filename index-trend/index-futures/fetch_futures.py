"""
THE decisive test: does the gap-continuation edge survive on the REAL index
futures, day session only?

Day-session futures data (front-month, day session 08:45-13:45) is pulled from a
third-party data API and split so that only the day session is kept. We take the
day-session front-month (highest-volume contract per date) and test whether the
futures open->close continues in the overnight-gap direction.

Gap here = prev day-session CLOSE (13:45) -> today day-session OPEN (08:45). This
already bakes in the entire night session, so it is the HONEST day-only-trader gap
on the actual tradable instrument. If continuation holds here, the edge is real and
deployable. If it's gone, the cash-index result was an execution mirage.

Saves index_fut_daily.csv.

NOTE: like the rest of the live path, the data-provider specifics are omitted from
this published copy. Point `fetch_futures()` at your own day-session futures source
that returns date/open/high/low/close/volume/contract_date rows.
"""
from __future__ import annotations
from pathlib import Path
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent


def fetch_futures(start_year=2019):
    """Return a day-session front-month futures DataFrame with columns
    [date, open, high, low, close, volume, contract_date].

    Placeholder in the published copy — wire up your own day-session futures
    data source here. Expected shape after your fetch:
      - one row per trading date (day session only)
      - front month = highest-volume contract per date
      - positive OHLC
    """
    raise NotImplementedError(
        "Supply your own day-session index-futures data source. "
        "Return columns: date, open, high, low, close, volume, contract_date."
    )


def test(df, label):
    s = df.copy()
    s["prev"] = s["close"].shift(1)
    s = s.dropna().reset_index(drop=True)
    s["gap"] = (s["open"] / s["prev"] - 1) * 100
    s["intra"] = (s["close"] / s["open"] - 1) * 100
    s["ema20"] = s["close"].ewm(span=20, adjust=False).mean()
    s["ema20_prev"] = s["ema20"].shift(1)

    b = np.polyfit(s["gap"], s["intra"], 1)
    big = s[s["gap"].abs() > 0.1]
    same = (np.sign(big["gap"]) == np.sign(big["intra"])).mean()
    r = np.sign(s["gap"]) * s["intra"]
    print(f"\n=== {label}  ({len(s)} sessions, {s['date'].iloc[0].date()} -> {s['date'].iloc[-1].date()}) ===")
    print(f"avg|gap|={s['gap'].abs().mean():.3f}%  avg|intra|={s['intra'].abs().mean():.3f}%")
    print(f"GAP->intraday: slope={b[0]:+.3f}  corr={np.corrcoef(s['gap'], s['intra'])[0,1]:+.3f}  continue={same*100:.1f}%")
    print(f"  gap-continuation GROSS: mean/day={r.mean():+.4f}%  hit={(r>0).mean()*100:.1f}%  "
          f"Sharpe~{r.mean()/r.std()*np.sqrt(252):+.2f}")
    slope_dir = np.sign(s["ema20"] - s["ema20_prev"])
    mask = slope_dir == np.sign(s["gap"])
    r4 = (np.sign(s["gap"]) * s["intra"])[mask]
    print(f"  V4 (gap + EMA20-trend agree): N={mask.sum()}  mean/day={r4.mean():+.4f}%  "
          f"hit={(r4>0).mean()*100:.1f}%  Sharpe~{r4.mean()/r4.std()*np.sqrt(252):+.2f}")
    return s


def main():
    df = fetch_futures()
    df.to_csv(HERE / "index_fut_daily.csv", index=False)
    print(f"Saved index_fut_daily.csv: {len(df)} day-session bars")
    test(df, "day-session index futures (REAL instrument)")


if __name__ == "__main__":
    main()
