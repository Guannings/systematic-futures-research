"""
Honest comparison of GDVT improvement variants A-E plus the baseline (v4).

For each variant we run on:
  - In-Sample slice  (~70% of data)
  - Out-of-Sample    (~30%, never used for design)
  - Full data        (sanity)

A variant is "real" only if BOTH IS and OOS show positive Sharpe AND OOS
Sharpe doesn't collapse vs IS. Pure parameter sweeping of the IS Sharpe
without checking OOS is overfitting.

Variants:
  baseline = GDVT v4 (Donchian-20 / ATR-14 / 2x stop / 4x trail / 15% vol target)
  A = ORB    (Opening-range breakout)
  B = weekly trend filter
  C = daily+weekly trend agreement (slower aggregate trend)
  D = wider stops (3x init, 6x trail)
  E = longs only (skip the never-tested short side)

Run:
    py -3.13 compare_variants.py
"""

from __future__ import annotations
import math
from pathlib import Path
import pandas as pd
import numpy as np

from gdvt_strategy import GDVTStrategy, StrategyConfig, Bar
from gdvt_backtest import (
    load_bars, filter_day_session, build_daily_bars,
    _trade_pnl, _round_trip_costs, compute_metrics,
)
from orb_strategy import ORBStrategy


HERE = Path(__file__).resolve().parent
INTRADAY_CSV = HERE / "gold_1h.csv"
DAILY_CSV    = HERE / "gold_daily.csv"
import sys
START_EQUITY = float(sys.argv[1]) if len(sys.argv) > 1 else 62500.0
IS_FRAC = 0.70  # first 70% in-sample, last 30% out-of-sample


def _build_weekly_from_daily(daily_df: pd.DataFrame) -> list[Bar]:
    """Resample daily OHLC into weekly bars (Mon-Fri)."""
    df = daily_df.copy()
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.set_index("datetime")
    weekly = df.resample("W-FRI").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna()
    return [Bar(timestamp=ts, open=r.open, high=r.high, low=r.low,
                close=r.close, volume=r.volume) for ts, r in weekly.iterrows()]


def run_variant(strategy_obj, df_intraday: pd.DataFrame,
                daily_df: pd.DataFrame, cfg: StrategyConfig) -> dict:
    """Run a backtest with the strategy already constructed and warmed."""
    df_test = filter_day_session(df_intraday).reset_index(drop=True)

    daily_lookup = build_daily_bars(df_test)
    last_seen_date = None
    last_position = 0
    last_position_entry = None
    last_signal = None
    prev_bar_close = None
    trades = []
    equity_curve = []

    for row in df_test.itertuples():
        bar = Bar(timestamp=row.datetime, open=row.open, high=row.high,
                  low=row.low, close=row.close, volume=row.volume)
        bar_date = row.datetime.date()

        # day-change handling (push prior daily bar + force EOD flat)
        if last_seen_date is not None and bar_date != last_seen_date:
            day_rows = daily_lookup[daily_lookup["date"] == last_seen_date]
            if len(day_rows):
                d = day_rows.iloc[0]
                strategy_obj.update_daily_bar(Bar(
                    timestamp=pd.Timestamp(d.date), open=d.open, high=d.high,
                    low=d.low, close=d.close, volume=d.volume,
                ))
            if last_position != 0 and last_position_entry is not None and prev_bar_close is not None:
                exit_price = prev_bar_close
                pnl = _trade_pnl(side=last_position, entry=last_position_entry,
                                 exit=exit_price, lots=abs(last_position),
                                 multiplier=cfg.contract_multiplier)
                fees = _round_trip_costs(lots=abs(last_position), price=exit_price,
                                         multiplier=cfg.contract_multiplier)
                net = pnl - fees
                strategy_obj.realize_pnl(net)
                trades.append({"entry_time": last_signal["timestamp"] if last_signal else None,
                               "exit_time": pd.Timestamp(last_seen_date),
                               "side": last_position, "lots": abs(last_position),
                               "entry": last_position_entry, "exit": exit_price,
                               "gross_pnl": pnl, "fees": fees, "net_pnl": net,
                               "reason": "eod_flat"})
                strategy_obj.position_lots = 0
                strategy_obj.entry_price = None
                strategy_obj.stop_price = None
                last_position = 0
                last_position_entry = None
                last_signal = None
        last_seen_date = bar_date

        sig = strategy_obj.update_intraday_bar(bar)
        target_signed = sig.direction * sig.target_lots
        if target_signed != last_position:
            if last_position != 0 and last_position_entry is not None:
                exit_price = bar.close
                pnl = _trade_pnl(side=last_position, entry=last_position_entry,
                                 exit=exit_price, lots=abs(last_position),
                                 multiplier=cfg.contract_multiplier)
                fees = _round_trip_costs(lots=abs(last_position), price=exit_price,
                                         multiplier=cfg.contract_multiplier)
                net = pnl - fees
                strategy_obj.realize_pnl(net)
                trades.append({"entry_time": last_signal["timestamp"] if last_signal else None,
                               "exit_time": bar.timestamp, "side": last_position,
                               "lots": abs(last_position), "entry": last_position_entry,
                               "exit": exit_price, "gross_pnl": pnl, "fees": fees,
                               "net_pnl": net, "reason": sig.reason})
            if target_signed != 0:
                last_position_entry = bar.close
                last_signal = {"timestamp": bar.timestamp, "reason": sig.reason}
            else:
                last_position_entry = None
                last_signal = None
            last_position = target_signed

        unrealized = 0.0
        if last_position != 0 and last_position_entry is not None:
            unrealized = _trade_pnl(side=last_position, entry=last_position_entry,
                                    exit=bar.close, lots=abs(last_position),
                                    multiplier=cfg.contract_multiplier)
        equity_curve.append({"datetime": bar.timestamp,
                             "equity": strategy_obj.equity + unrealized,
                             "position": last_position})
        prev_bar_close = bar.close

    eq = pd.DataFrame(equity_curve)
    tr = pd.DataFrame(trades)
    return compute_metrics(eq, tr, START_EQUITY)


def _build_strategy(variant: str, daily_bars: list[Bar],
                    daily_df: pd.DataFrame) -> tuple[object, StrategyConfig]:
    """Construct + warmup a strategy for the named variant."""
    if variant == "baseline":
        cfg = StrategyConfig()
        s = GDVTStrategy(config=cfg, starting_equity=START_EQUITY)
        s.warmup_daily(daily_bars)
        return s, cfg
    if variant == "A_orb":
        cfg = StrategyConfig()
        s = ORBStrategy(config=cfg, starting_equity=START_EQUITY)
        s.warmup_daily(daily_bars)
        return s, cfg
    if variant == "B_weekly":
        weekly_bars = _build_weekly_from_daily(daily_df)
        cfg = StrategyConfig(trend_fast=10, trend_slow=40)  # 10 weeks / 40 weeks
        s = GDVTStrategy(config=cfg, starting_equity=START_EQUITY)
        s.warmup_daily(weekly_bars)  # feed weekly into the "daily" slot
        return s, cfg
    if variant == "C_slow_trend":
        # daily+weekly agreement ≈ very slow daily EMA
        cfg = StrategyConfig(trend_fast=100, trend_slow=400)
        s = GDVTStrategy(config=cfg, starting_equity=START_EQUITY)
        s.warmup_daily(daily_bars)
        return s, cfg
    if variant == "D_wider_stops":
        cfg = StrategyConfig(init_stop_atr_mult=3.0, trail_atr_mult=6.0)
        s = GDVTStrategy(config=cfg, starting_equity=START_EQUITY)
        s.warmup_daily(daily_bars)
        return s, cfg
    if variant == "E_longs_only":
        # GDVTStrategy doesn't have a flag for this; we'll wrap it.
        # Easiest: subclass and short-circuit short signals.
        class GDVTLongOnly(GDVTStrategy):
            def update_intraday_bar(self, bar: Bar):
                sig = super().update_intraday_bar(bar)
                if sig.direction < 0:
                    return self._set_flat("longs_only_skip_short")
                return sig
        cfg = StrategyConfig()
        s = GDVTLongOnly(config=cfg, starting_equity=START_EQUITY)
        s.warmup_daily(daily_bars)
        return s, cfg
    raise ValueError(variant)


def slice_df(df: pd.DataFrame, start_frac: float, end_frac: float) -> pd.DataFrame:
    n = len(df)
    a = int(n * start_frac)
    b = int(n * end_frac)
    return df.iloc[a:b].reset_index(drop=True)


def fmt_row(name: str, label: str, m: dict) -> str:
    if "error" in m:
        return f"  {name:<22s} {label:<8s}  -- error: {m['error']}"
    return (f"  {name:<22s} {label:<8s}  "
            f"Sharpe={m['sharpe']:>6.2f}  "
            f"return={m['total_return_pct']:>7.2f}%  "
            f"DD={m['max_drawdown_pct']:>7.2f}%  "
            f"trades={m['n_trades']:>4d}  "
            f"PF={m['profit_factor']:>4.2f}")


def main():
    print(f"Loading {INTRADAY_CSV.name} ...")
    df = load_bars(str(INTRADAY_CSV))
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.sort_values("datetime").reset_index(drop=True)
    print(f"  {len(df)} 1h bars from {df['datetime'].iloc[0]} to {df['datetime'].iloc[-1]}")

    print(f"Loading {DAILY_CSV.name} ...")
    daily_df = pd.read_csv(DAILY_CSV)
    daily_df.columns = [c.lower() for c in daily_df.columns]
    daily_df["datetime"] = pd.to_datetime(daily_df["datetime"])
    if "volume" not in daily_df.columns:
        daily_df["volume"] = 0.0
    daily_bars = [Bar(timestamp=row.datetime, open=row.open, high=row.high,
                      low=row.low, close=row.close, volume=row.volume)
                  for row in daily_df.itertuples()]
    print(f"  {len(daily_bars)} daily bars")

    df_is   = slice_df(df, 0.0, IS_FRAC)
    df_oos  = slice_df(df, IS_FRAC, 1.0)
    print(f"\nIS:  {df_is['datetime'].iloc[0]} -> {df_is['datetime'].iloc[-1]}  ({len(df_is)} bars)")
    print(f"OOS: {df_oos['datetime'].iloc[0]} -> {df_oos['datetime'].iloc[-1]}  ({len(df_oos)} bars)")
    print(f"Starting equity: {START_EQUITY:,.0f} (apples-to-apples basis)\n")

    variants = [
        ("baseline (v4)", "baseline"),
        ("A. ORB", "A_orb"),
        ("B. weekly trend", "B_weekly"),
        ("C. slow trend (100/400)", "C_slow_trend"),
        ("D. wider stops (3/6)", "D_wider_stops"),
        ("E. longs only", "E_longs_only"),
    ]

    print("=" * 100)
    print(f"  {'Variant':<22s} {'Slice':<8s}  {'Sharpe':>10s} {'Return':>14s} {'DD':>11s} {'Trades':>10s} {'PF':>7s}")
    print("=" * 100)
    for label, key in variants:
        for slice_label, slice_data in (("IS", df_is), ("OOS", df_oos), ("FULL", df)):
            s, cfg = _build_strategy(key, daily_bars, daily_df)
            try:
                m = run_variant(s, slice_data, daily_df, cfg)
            except Exception as e:
                m = {"error": str(e)}
            print(fmt_row(label, slice_label, m))
        print()


if __name__ == "__main__":
    main()
