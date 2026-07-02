"""
Refresh gold data from Yahoo Finance.

Pulls GC=F (COMEX gold futures, USD/oz) for:
  - daily bars, ~5 years   -> overwrites gold_daily.csv  (used for trend filter)
  - 1-hour bars, last 2y   -> overwrites gold_1h.csv     (used for backtest)

Run:
    py -3.13 refresh_data.py
"""

from __future__ import annotations
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf


HERE = Path(__file__).resolve().parent
DAILY_CSV = HERE / "gold_daily.csv"
H1_CSV    = HERE / "gold_1h.csv"
M15_CSV   = HERE / "gold_15m.csv"
TICKER    = "GC=F"


def _flatten(df: pd.DataFrame) -> pd.DataFrame:
    """yfinance can return a MultiIndex on columns when downloading a single
    ticker. Flatten so we always get plain ['Open','High','Low','Close','Volume']."""
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)
    return df


def _to_csv_format(df: pd.DataFrame, daily: bool) -> pd.DataFrame:
    df = _flatten(df).reset_index()
    # yfinance index can be named "Date", "Datetime", or "index" depending on interval
    ts_col = next((c for c in df.columns if c.lower() in ("date", "datetime", "index")), df.columns[0])
    df = df.rename(columns={ts_col: "datetime"})
    keep = ["datetime", "Open", "High", "Low", "Close", "Volume"]
    df = df[[c for c in keep if c in df.columns]]
    df.columns = [c.lower() for c in df.columns]
    df["datetime"] = pd.to_datetime(df["datetime"])
    if df["datetime"].dt.tz is not None:
        df["datetime"] = df["datetime"].dt.tz_localize(None)
    if daily:
        df["datetime"] = df["datetime"].dt.strftime("%Y-%m-%d")
    else:
        df["datetime"] = df["datetime"].dt.strftime("%Y-%m-%d %H:%M:%S")
    df = df.dropna(subset=["open", "high", "low", "close"])
    return df


def fetch(period: str, interval: str) -> pd.DataFrame:
    print(f"  yfinance.download(ticker={TICKER!r}, period={period!r}, interval={interval!r}) ...")
    df = yf.download(TICKER, period=period, interval=interval,
                     auto_adjust=False, progress=False)
    if df is None or df.empty:
        raise RuntimeError(f"empty data for {TICKER} {period}/{interval}")
    return df


def main():
    print(f"Refreshing gold data into {HERE}")

    print("[1/3] daily, period=5y")
    daily_df = _to_csv_format(fetch(period="5y", interval="1d"), daily=True)
    daily_df.to_csv(DAILY_CSV, index=False)
    print(f"  wrote {len(daily_df)} rows  ->  {DAILY_CSV.name}")
    print(f"        first: {daily_df['datetime'].iloc[0]}   last: {daily_df['datetime'].iloc[-1]}")

    print("[2/3] hourly, period=730d (yfinance max)")
    h1_df = _to_csv_format(fetch(period="730d", interval="1h"), daily=False)
    h1_df.to_csv(H1_CSV, index=False)
    print(f"  wrote {len(h1_df)} rows  ->  {H1_CSV.name}")
    print(f"        first: {h1_df['datetime'].iloc[0]}   last: {h1_df['datetime'].iloc[-1]}")

    print("[3/3] 15-min, period=60d (yfinance max for 15m)")
    m15_df = _to_csv_format(fetch(period="60d", interval="15m"), daily=False)
    m15_df.to_csv(M15_CSV, index=False)
    print(f"  wrote {len(m15_df)} rows  ->  {M15_CSV.name}")
    print(f"        first: {m15_df['datetime'].iloc[0]}   last: {m15_df['datetime'].iloc[-1]}")

    print("done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
