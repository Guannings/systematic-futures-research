"""
Fetch the cash index data from Yahoo Finance for the index strategy backtest.

^INDEX is the the cash index. It trades the day session only (09:00-13:30
exchange-local), so there is NO night-session data here — but the overnight gap
(today's open vs yesterday's close) is fully captured, which is the richest
intraday feature on this market. Live execution is on index futures; the cash
index is the backtest proxy (same approach the gold system used with GC=F).

Outputs (matching the gold CSV format: datetime,open,high,low,close,volume):
  - cash_index_daily.csv : full history (1997->now), date-only datetime
  - cash_index_1h.csv    : ~730 days of hourly bars, naive UTC timestamps

Usage:  python fetch_cash_index.py
"""
from __future__ import annotations
import warnings
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

SYMBOL = "^INDEX"
EXCHANGE_TZ_NAME = "UTC"


def _flatten(df: pd.DataFrame) -> pd.DataFrame:
    """yfinance may return MultiIndex columns for a single ticker."""
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [str(c).lower() for c in df.columns]
    return df


def _tidy(df: pd.DataFrame) -> pd.DataFrame:
    df = _flatten(df)
    cols = ["open", "high", "low", "close", "volume"]
    df = df[[c for c in cols if c in df.columns]].copy()
    df = df.dropna(subset=["open", "high", "low", "close"])
    df["volume"] = df.get("volume", 0).fillna(0)
    return df


def fetch_daily() -> pd.DataFrame:
    df = yf.download(SYMBOL, period="max", interval="1d",
                     progress=False, auto_adjust=False)
    df = _tidy(df)
    df.index = pd.to_datetime(df.index)
    # daily index is tz-naive midnight; keep date-only string
    out = df.reset_index()
    out = out.rename(columns={out.columns[0]: "datetime"})
    out["datetime"] = pd.to_datetime(out["datetime"]).dt.strftime("%Y-%m-%d")
    return out[["datetime", "open", "high", "low", "close", "volume"]]


def fetch_hourly() -> pd.DataFrame:
    df = yf.download(SYMBOL, period="730d", interval="1h",
                     progress=False, auto_adjust=False)
    df = _tidy(df)
    idx = pd.to_datetime(df.index)
    # convert UTC -> exchange-local, then drop tz so the backtest's naive session
    # filter (08:45-13:45 local) works directly
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    idx = idx.tz_convert(EXCHANGE_TZ_NAME).tz_localize(None)
    df.index = idx
    out = df.reset_index()
    out = out.rename(columns={out.columns[0]: "datetime"})
    out["datetime"] = out["datetime"].dt.strftime("%Y-%m-%d %H:%M:%S")
    return out[["datetime", "open", "high", "low", "close", "volume"]]


def main() -> None:
    daily = fetch_daily()
    daily.to_csv("cash_index_daily.csv", index=False)
    print(f"cash_index_daily.csv : {len(daily):>6} bars  "
          f"{daily['datetime'].iloc[0]} -> {daily['datetime'].iloc[-1]}")

    hourly = fetch_hourly()
    hourly.to_csv("cash_index_1h.csv", index=False)
    print(f"cash_index_1h.csv    : {len(hourly):>6} bars  "
          f"{hourly['datetime'].iloc[0]} -> {hourly['datetime'].iloc[-1]}")

    # quick session sanity check
    t = pd.to_datetime(hourly["datetime"]).dt.time
    in_sess = [(x.hour, x.minute) for x in t]
    hrs = sorted(set(h for h, m in in_sess))
    print(f"hourly bar hours present (exchange-local): {hrs}")


if __name__ == "__main__":
    main()
