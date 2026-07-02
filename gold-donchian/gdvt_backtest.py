"""
Backtest harness for GDVT strategy.

Loads historical 15-min gold bars from a CSV, simulates day-session-only
trading following exchange rules, and reports Sharpe / return / DD.

CSV format expected (columns):
    datetime, open, high, low, close, volume

`datetime` should be in exchange-local time (or naive, treated as exchange-local).
We filter to the day session (08:45–13:45) since the strategy is designed
for day-session-only trading.

Usage:
    python gdvt_backtest.py path/to/gold_1h.csv
"""

from __future__ import annotations
import sys
import math
import argparse
from pathlib import Path

import pandas as pd
import numpy as np

from gdvt_strategy import GDVTStrategy, StrategyConfig, Bar


# Exchange cost model --------------------------------------------------------
FEE_USD_PER_SIDE = 2.0           # USD$2 per lot per side
TAX_RATE_NOMINAL = 0.0000125     # 0.00125% on notional per side
SLIPPAGE_USD_PER_SIDE = 1.0      # 1 tick ($0.10/oz × 10 oz) per lot per side;
                                 # conservative estimate of bid-ask half-spread
                                 # plus market-order fill drift on the day session


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_bars(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    # tolerate a few common column-name conventions
    df.columns = [c.lower() for c in df.columns]
    if "datetime" not in df.columns:
        for cand in ("timestamp", "time", "date"):
            if cand in df.columns:
                df = df.rename(columns={cand: "datetime"})
                break
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.sort_values("datetime").reset_index(drop=True)
    needed = {"datetime", "open", "high", "low", "close"}
    if not needed.issubset(df.columns):
        missing = needed - set(df.columns)
        raise ValueError(f"CSV missing columns: {missing}")
    if "volume" not in df.columns:
        df["volume"] = 0.0
    return df


def filter_day_session(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only bars in the day session (08:45–13:45 exchange-local)."""
    t = df["datetime"].dt.time
    start = pd.Timestamp("08:45").time()
    end = pd.Timestamp("13:45").time()
    return df[(t >= start) & (t <= end)].reset_index(drop=True)


def build_daily_bars(intraday: pd.DataFrame) -> pd.DataFrame:
    g = intraday.groupby(intraday["datetime"].dt.date)
    daily = pd.DataFrame({
        "open":   g["open"].first(),
        "high":   g["high"].max(),
        "low":    g["low"].min(),
        "close":  g["close"].last(),
        "volume": g["volume"].sum(),
    }).reset_index().rename(columns={"datetime": "date"})
    daily["datetime"] = pd.to_datetime(daily["date"])
    return daily


# ---------------------------------------------------------------------------
# Backtest loop
# ---------------------------------------------------------------------------

def run_backtest(csv_path: str, starting_equity: float = 2_000_000.0,
                 config: StrategyConfig | None = None,
                 warmup_daily: int = 250,
                 daily_csv: str | None = None) -> dict:
    df = load_bars(csv_path)
    df_day = filter_day_session(df)
    daily = build_daily_bars(df_day)

    cfg = config or StrategyConfig()
    strat = GDVTStrategy(config=cfg, starting_equity=starting_equity)

    # Daily warmup: prefer external daily CSV (longer history), else slice from intraday
    if daily_csv is not None and Path(daily_csv).exists():
        ext_daily = pd.read_csv(daily_csv)
        ext_daily.columns = [c.lower() for c in ext_daily.columns]
        ext_daily["datetime"] = pd.to_datetime(ext_daily["datetime"])
        if "volume" not in ext_daily.columns:
            ext_daily["volume"] = 0.0
        strat.warmup_daily([
            Bar(timestamp=row.datetime, open=row.open, high=row.high,
                low=row.low, close=row.close, volume=row.volume)
            for row in ext_daily.itertuples()
        ])
        last_seeded_date = ext_daily["datetime"].iloc[-1].date()
        df_test = df_day  # test on all intraday bars; daily warmup is independent
    else:
        seed_daily = daily.iloc[:warmup_daily]
        strat.warmup_daily([
            Bar(timestamp=row.datetime, open=row.open, high=row.high,
                low=row.low, close=row.close, volume=row.volume)
            for row in seed_daily.itertuples()
        ])
        last_seeded_date = seed_daily["date"].iloc[-1] if len(seed_daily) else None
        if last_seeded_date is not None:
            df_test = df_day[df_day["datetime"].dt.date > last_seeded_date].reset_index(drop=True)
        else:
            df_test = df_day

    equity_curve = []
    trades = []
    last_signal = None
    last_position = 0
    last_position_entry = None
    last_seen_date = None

    prev_bar_close: float | None = None
    for row in df_test.itertuples():
        bar = Bar(
            timestamp=row.datetime, open=row.open, high=row.high,
            low=row.low, close=row.close, volume=row.volume,
        )

        # add a daily bar at session open of a new date
        bar_date = row.datetime.date()
        if last_seen_date is not None and bar_date != last_seen_date:
            day_rows = daily[daily["date"] == last_seen_date]
            if len(day_rows):
                d = day_rows.iloc[0]
                strat.update_daily_bar(Bar(
                    timestamp=pd.Timestamp(d.date), open=d.open, high=d.high,
                    low=d.low, close=d.close, volume=d.volume,
                ))
            # FLAT-BY-END-OF-DAY: if the strategy didn't already close on the
            # last bar of the prior session (which it won't on bars whose
            # timestamp < flat_by_time), force-close at the prior bar's close.
            if last_position != 0 and last_position_entry is not None and prev_bar_close is not None:
                exit_price = prev_bar_close
                pnl = _trade_pnl(side=last_position, entry=last_position_entry,
                                 exit=exit_price, lots=abs(last_position),
                                 multiplier=cfg.contract_multiplier)
                fees = _round_trip_costs(lots=abs(last_position), price=exit_price,
                                         multiplier=cfg.contract_multiplier)
                net = pnl - fees
                strat.realize_pnl(net)
                trades.append({
                    "entry_time": last_signal["timestamp"] if last_signal else None,
                    "exit_time": pd.Timestamp(last_seen_date),
                    "side": last_position, "lots": abs(last_position),
                    "entry": last_position_entry, "exit": exit_price,
                    "gross_pnl": pnl, "fees": fees, "net_pnl": net,
                    "reason": "eod_flat",
                })
                strat.position_lots = 0
                strat.entry_price = None
                strat.stop_price = None
                last_position = 0
                last_position_entry = None
                last_signal = None
        last_seen_date = bar_date

        sig = strat.update_intraday_bar(bar)

        # execute position changes at this bar's close (slippage built into fee)
        target_signed = sig.direction * sig.target_lots
        if target_signed != last_position:
            # close any open trade record
            if last_position != 0 and last_position_entry is not None:
                exit_price = bar.close
                pnl = _trade_pnl(
                    side=last_position, entry=last_position_entry,
                    exit=exit_price, lots=abs(last_position),
                    multiplier=cfg.contract_multiplier,
                )
                fees = _round_trip_costs(
                    lots=abs(last_position), price=exit_price,
                    multiplier=cfg.contract_multiplier,
                )
                net = pnl - fees
                strat.realize_pnl(net)
                trades.append({
                    "entry_time": last_signal["timestamp"] if last_signal else None,
                    "exit_time": bar.timestamp,
                    "side": last_position,
                    "lots": abs(last_position),
                    "entry": last_position_entry,
                    "exit": exit_price,
                    "gross_pnl": pnl,
                    "fees": fees,
                    "net_pnl": net,
                    "reason": sig.reason,
                })
            # open new trade record if entering a fresh position
            if target_signed != 0:
                last_position_entry = bar.close
                last_signal = {"timestamp": bar.timestamp, "reason": sig.reason}
            else:
                last_position_entry = None
                last_signal = None
            last_position = target_signed

        # mark-to-market equity for curve
        unrealized = 0.0
        if last_position != 0 and last_position_entry is not None:
            unrealized = _trade_pnl(
                side=last_position, entry=last_position_entry,
                exit=bar.close, lots=abs(last_position),
                multiplier=cfg.contract_multiplier,
            )
        equity_curve.append({
            "datetime": bar.timestamp,
            "equity": strat.equity + unrealized,
            "position": last_position,
        })
        prev_bar_close = bar.close

    eq = pd.DataFrame(equity_curve)
    tr = pd.DataFrame(trades)
    metrics = compute_metrics(eq, tr, starting_equity)
    return {
        "config": cfg,
        "equity": eq,
        "trades": tr,
        "metrics": metrics,
        "final_equity": strat.equity,
    }


def _trade_pnl(side: int, entry: float, exit: float, lots: int,
               multiplier: float) -> float:
    return side * (exit - entry) * lots * multiplier


def _round_trip_costs(lots: int, price: float, multiplier: float) -> float:
    """Two sides × (fee + tax + slippage)."""
    notional = price * multiplier * lots
    side_cost = (
        FEE_USD_PER_SIDE * lots
        + TAX_RATE_NOMINAL * notional
        + SLIPPAGE_USD_PER_SIDE * lots
    )
    return 2.0 * side_cost


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def compute_metrics(eq: pd.DataFrame, tr: pd.DataFrame,
                    starting_equity: float) -> dict:
    if eq.empty:
        return {"error": "no equity points"}
    eq = eq.copy()
    eq["datetime"] = pd.to_datetime(eq["datetime"])
    eq = eq.sort_values("datetime").reset_index(drop=True)
    eq["ret"] = eq["equity"].pct_change().fillna(0.0)

    # daily returns for Sharpe (resample to end-of-session equity)
    daily = (eq.set_index("datetime")["equity"]
               .resample("1D").last().dropna())
    daily_ret = daily.pct_change().dropna()
    if daily_ret.std() > 0:
        sharpe = (daily_ret.mean() / daily_ret.std()) * math.sqrt(252)
    else:
        sharpe = float("nan")

    total_return = eq["equity"].iloc[-1] / starting_equity - 1.0
    n_days = max((eq["datetime"].iloc[-1] - eq["datetime"].iloc[0]).days, 1)
    annual_return = (1.0 + total_return) ** (365.0 / n_days) - 1.0

    # max drawdown
    running_peak = eq["equity"].cummax()
    dd = (eq["equity"] / running_peak) - 1.0
    max_dd = dd.min()

    # trades
    n_trades = len(tr)
    if n_trades > 0:
        wins = tr[tr["net_pnl"] > 0]
        losses = tr[tr["net_pnl"] <= 0]
        win_rate = len(wins) / n_trades
        avg_win = wins["net_pnl"].mean() if len(wins) else 0.0
        avg_loss = losses["net_pnl"].mean() if len(losses) else 0.0
        profit_factor = (wins["net_pnl"].sum() / abs(losses["net_pnl"].sum())
                         if len(losses) and losses["net_pnl"].sum() != 0 else float("inf"))
    else:
        win_rate = avg_win = avg_loss = 0.0
        profit_factor = float("nan")

    return {
        "starting_equity": starting_equity,
        "final_equity": eq["equity"].iloc[-1],
        "total_return_pct": total_return * 100,
        "annual_return_pct": annual_return * 100,
        "sharpe": sharpe,
        "max_drawdown_pct": max_dd * 100,
        "n_trades": n_trades,
        "win_rate_pct": win_rate * 100,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
    }


def print_report(result: dict) -> None:
    m = result["metrics"]
    print("=" * 60)
    print("GDVT BACKTEST RESULTS")
    print("=" * 60)
    print(f"  Starting equity:    {m['starting_equity']:>15,.0f}")
    print(f"  Final equity:       {m['final_equity']:>15,.0f}")
    print(f"  Total return:       {m['total_return_pct']:>14.2f} %")
    print(f"  Annualized return:  {m['annual_return_pct']:>14.2f} %")
    print(f"  Sharpe (daily):     {m['sharpe']:>14.2f}")
    print(f"  Max drawdown:       {m['max_drawdown_pct']:>14.2f} %")
    print(f"  Trades:             {m['n_trades']:>15d}")
    print(f"  Win rate:           {m['win_rate_pct']:>14.2f} %")
    print(f"  Avg win  $:         {m['avg_win']:>15,.2f}")
    print(f"  Avg loss $:         {m['avg_loss']:>15,.2f}")
    print(f"  Profit factor:      {m['profit_factor']:>14.2f}")
    print("=" * 60)


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser()
    p.add_argument("csv", help="path to 1h gold OHLCV csv")
    p.add_argument("--equity", type=float, default=2_000_000.0,
                   help="starting equity (default: 2M)")
    p.add_argument("--vol-target", type=float, default=0.15)
    p.add_argument("--warmup-daily", type=int, default=250,
                   help="daily bars to consume as trend-filter warmup before testing")
    p.add_argument("--daily-csv", type=str, default=None,
                   help="optional external daily CSV for trend warmup (e.g. gold_daily.csv)")
    p.add_argument("--save-equity", type=str, default=None,
                   help="optional path to save equity-curve csv")
    p.add_argument("--save-trades", type=str, default=None,
                   help="optional path to save trades csv")
    args = p.parse_args()

    cfg = StrategyConfig(vol_target_annual=args.vol_target)
    result = run_backtest(args.csv, starting_equity=args.equity, config=cfg,
                          warmup_daily=args.warmup_daily, daily_csv=args.daily_csv)
    print_report(result)

    if args.save_equity:
        result["equity"].to_csv(args.save_equity, index=False)
        print(f"  equity curve saved to {args.save_equity}")
    if args.save_trades:
        result["trades"].to_csv(args.save_trades, index=False)
        print(f"  trades saved to {args.save_trades}")


if __name__ == "__main__":
    main()
