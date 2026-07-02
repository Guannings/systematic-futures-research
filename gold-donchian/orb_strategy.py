"""
ORB (Opening Range Breakout) strategy — variant A.

Entry logic:
  - Track the first 1h bar of each day session (the 09:00 bar)
  - That bar's high/low define the opening range
  - On subsequent in-session bars: break above range high -> long,
                                    break below range low  -> short
  - Daily EMA(50/200) trend filter still applies (only longs in uptrend, etc.)

Exits:
  - Stop at the opposite side of the opening range
  - Or wall-clock time stop at 13:30 (handled by the live wrapper / backtest)

Same starting-equity / sizing / fee model as GDVTStrategy. Pure logic, no I/O.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, List
import math
import pandas as pd

from gdvt_strategy import Bar, Signal, StrategyConfig, ema, atr, true_range


class ORBStrategy:
    def __init__(self, config: Optional[StrategyConfig] = None,
                 starting_equity: float = 2_000_000.0):
        self.cfg = config or StrategyConfig()
        self.equity = float(starting_equity)
        self.peak_equity = self.equity
        self.dd_active = False

        self.daily_bars: List[Bar] = []
        self.intraday_bars: List[Bar] = []

        self.position_lots: int = 0
        self.entry_price: Optional[float] = None
        self.stop_price: Optional[float] = None

        # ORB session state
        self.session_date = None
        self.orb_high: Optional[float] = None
        self.orb_low: Optional[float] = None

    def warmup_daily(self, bars: List[Bar]) -> None:
        self.daily_bars = list(bars)

    def warmup_intraday(self, bars: List[Bar]) -> None:
        self.intraday_bars = list(bars)

    def update_daily_bar(self, bar: Bar) -> None:
        self.daily_bars.append(bar)

    def update_intraday_bar(self, bar: Bar) -> Signal:
        self.intraday_bars.append(bar)

        bar_date = bar.timestamp.date()
        bar_time = bar.timestamp.time()

        # Reset ORB state at start of each new day
        if self.session_date != bar_date:
            self.session_date = bar_date
            self.orb_high = bar.high
            self.orb_low = bar.low
            # First bar of day defines the opening range — no entry yet
            return self._hold("orb_window")

        # Hard time-stop at 13:30
        cutoff_h, cutoff_m = [int(x) for x in self.cfg.flat_by_time.split(":")]
        if (bar_time.hour, bar_time.minute) >= (cutoff_h, cutoff_m):
            return self._set_flat("time_stop")

        # Need daily warmup
        if len(self.daily_bars) < self.cfg.trend_slow:
            return self._hold("warmup_daily")

        # Update DD state
        self._update_dd_state()

        # Trend filter
        daily_closes = [b.close for b in self.daily_bars]
        ema_fast = ema(daily_closes, self.cfg.trend_fast)
        ema_slow = ema(daily_closes, self.cfg.trend_slow)
        if ema_fast is None or ema_slow is None:
            return self._hold("trend_unset")
        trend_dir = 1 if ema_fast > ema_slow else -1

        # Stop check
        if self._stop_hit(bar):
            return self._set_flat("stop_hit")

        # ORB entry logic
        if self.position_lots == 0:
            if self.orb_high is None or self.orb_low is None:
                return self._hold("orb_pending")
            atr_v = atr(self.intraday_bars, self.cfg.atr_n) or 0.0
            if atr_v <= 0:
                return self._hold("atr_unset")
            target_lots = self._target_lots(atr_v, bar.close)
            if trend_dir > 0 and bar.close > self.orb_high:
                self.position_lots = target_lots
                self.entry_price = bar.close
                self.stop_price = self.orb_low
                return Signal(direction=+1, target_lots=target_lots,
                              stop_price=self.stop_price, reason="orb_break_up")
            if trend_dir < 0 and bar.close < self.orb_low:
                self.position_lots = -target_lots
                self.entry_price = bar.close
                self.stop_price = self.orb_high
                return Signal(direction=-1, target_lots=target_lots,
                              stop_price=self.stop_price, reason="orb_break_down")

        return self._hold("no_signal")

    def realize_pnl(self, pnl: float) -> None:
        self.equity += pnl
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity

    def _hold(self, reason: str) -> Signal:
        return Signal(
            direction=int(math.copysign(1, self.position_lots)) if self.position_lots else 0,
            target_lots=abs(self.position_lots),
            stop_price=self.stop_price,
            reason=reason,
        )

    def _set_flat(self, reason: str) -> Signal:
        self.position_lots = 0
        self.entry_price = None
        self.stop_price = None
        return Signal(direction=0, target_lots=0, reason=reason)

    def _stop_hit(self, bar: Bar) -> bool:
        if self.position_lots == 0 or self.stop_price is None:
            return False
        if self.position_lots > 0 and bar.low <= self.stop_price:
            return True
        if self.position_lots < 0 and bar.high >= self.stop_price:
            return True
        return False

    def _target_lots(self, atr_value: float, price: float) -> int:
        target_dollar_risk = (self.equity * self.cfg.vol_target_annual
                              / math.sqrt(self.cfg.bars_per_year))
        if self.dd_active:
            target_dollar_risk *= 0.5
        per_lot_dollar_risk = atr_value * self.cfg.contract_multiplier
        if per_lot_dollar_risk <= 0:
            return self.cfg.min_lots
        lots = int(target_dollar_risk / per_lot_dollar_risk)
        margin_budget = self.equity * self.cfg.max_margin_pct
        max_by_margin = int(margin_budget / max(self.cfg.margin_per_lot, 1.0))
        lots = min(lots, max_by_margin)
        return max(self.cfg.min_lots, min(self.cfg.max_lots, lots))

    def _update_dd_state(self) -> None:
        if self.peak_equity <= 0:
            return
        dd = 1.0 - (self.equity / self.peak_equity)
        if dd >= self.cfg.dd_circuit_threshold and not self.dd_active:
            self.dd_active = True
        elif dd <= self.cfg.dd_recover_threshold and self.dd_active:
            self.dd_active = False
