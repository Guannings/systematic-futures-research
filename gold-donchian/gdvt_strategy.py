"""
GDVT — Gold Day-Session Vol-Targeted Trend Strategy
====================================================

Designed for the exchange's gold futures contract on the day session.
Optimized for risk-adjusted return (annualized Sharpe) under a
day-session-only trading constraint.

Components:
  1. Daily trend filter   — EMA(100) vs EMA(400) on daily closes (0.5% dead-band)
  2. Intraday entry       — Donchian-20 breakout on 1h bars (gated by trend)
  3. Vol-targeted sizing  — targets 15% annualized portfolio vol
  4. Exits                — initial 2x ATR stop, 4x ATR trailing chandelier
  5. Hard time stop       — flat by 13:30 every day (no overnight gap risk)
  6. DD circuit breaker   — halve sizing if account DD > 15%

This file is pure logic. No I/O, no API calls. Testable in isolation.
Backtest harness is in gdvt_backtest.py. Live wrapper is in gdvt_live.py.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, List
import math
import pandas as pd


# -----------------------------------------------------------------------------
# Data classes
# -----------------------------------------------------------------------------

@dataclass
class Bar:
    timestamp: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Signal:
    """Output of strategy on each bar close. Direction is the *target*
    position direction; target_lots is the absolute size."""
    direction: int            # +1 long, -1 short, 0 flat
    target_lots: int          # absolute size (always >= 0)
    stop_price: Optional[float] = None
    reason: str = ""


@dataclass
class StrategyConfig:
    # Trend filter — slow daily EMAs (variant C). EMA(50/200) was Sharpe 0.63
    # full / 0.34 OOS; EMA(100/400) is 0.99 full / 0.95 OOS. The slower filter
    # captures gold's multi-month regimes and avoids whipsaws on monthly chop.
    trend_fast: int = 100
    trend_slow: int = 400
    # Neutral dead-band: when the two EMAs are within this fraction of each
    # other the regime is treated as no-trade. Prevents whipsaw entries when
    # the EMAs are effectively equal near a crossover.
    trend_deadband_pct: float = 0.005

    # Entry trigger
    donchian_n: int = 20

    # Volatility / sizing
    atr_n: int = 14
    vol_target_annual: float = 0.15
    bars_per_year: float = 252.0 * 5.0    # ~5 one-hour bars per session (08:45–13:45) × 252

    # Exits
    init_stop_atr_mult: float = 2.0
    trail_atr_mult: float = 4.0
    flat_by_time: str = "13:30"           # day session ends 13:45

    # Risk / capital
    contract_point_value: float = 1.0     # $1 USD per tick
    contract_multiplier: float = 10.0     # 10 troy oz per lot
    max_margin_pct: float = 0.50          # never deploy more than 50% of equity
    margin_per_lot: float = 5500.0        # USD initial margin per lot
                                          # (per the exchange's contract spec).
                                          # Maintenance is $4,220. Both USD-denominated.
    dd_circuit_threshold: float = 0.15    # halve sizing if DD > 15%
    dd_recover_threshold: float = 0.08    # un-halve at DD < 8%

    # Practical caps — week-1 live training wheels: max_lots=1 means every
    # trade is exactly 1 lot regardless of vol-target's preference. Keeps
    # downside small while we verify the bridge order field codes work in production.
    # Bump to 8 after ~1 week of clean live trades (project guardrail at the
    # capital guardrail).
    min_lots: int = 1
    max_lots: int = 1

    # News blackout — list of (date, start_time, end_time) tuples to skip entries
    news_blackout: List[tuple] = field(default_factory=list)


# -----------------------------------------------------------------------------
# Indicator helpers
# -----------------------------------------------------------------------------

def ema(values: List[float], n: int) -> Optional[float]:
    """Standard EMA. Returns None if insufficient data."""
    if len(values) < n:
        return None
    k = 2.0 / (n + 1.0)
    e = sum(values[:n]) / n           # seed with SMA
    for v in values[n:]:
        e = v * k + e * (1.0 - k)
    return e


def true_range(prev_close: float, h: float, l: float) -> float:
    return max(h - l, abs(h - prev_close), abs(l - prev_close))


def atr(bars: List[Bar], n: int) -> Optional[float]:
    if len(bars) < n + 1:
        return None
    trs = [
        true_range(bars[i - 1].close, bars[i].high, bars[i].low)
        for i in range(1, len(bars))
    ]
    # Wilder smoothing approximation via simple mean over last n
    return sum(trs[-n:]) / n


# -----------------------------------------------------------------------------
# Strategy
# -----------------------------------------------------------------------------

class GDVTStrategy:
    """
    Stateful strategy. Feed it daily bars (for trend filter) once at start,
    then call update_intraday_bar() on each 15m bar close. It returns a
    Signal describing the desired position; caller is responsible for
    submitting orders to reach that position.
    """

    def __init__(self, config: Optional[StrategyConfig] = None,
                 starting_equity: float = 2_000_000.0):
        self.cfg = config or StrategyConfig()
        self.equity = float(starting_equity)
        self.peak_equity = self.equity
        self.dd_active = False              # circuit-breaker state

        # daily and intraday rolling histories (lists of Bar)
        self.daily_bars: List[Bar] = []
        self.intraday_bars: List[Bar] = []

        # current position state
        self.position_lots: int = 0          # signed; +long, -short
        self.entry_price: Optional[float] = None
        self.stop_price: Optional[float] = None
        self.high_water_since_entry: Optional[float] = None
        self.low_water_since_entry: Optional[float] = None

    # ---- public API ----

    def warmup_daily(self, bars: List[Bar]) -> None:
        """Seed the daily history. Call once before live trading."""
        self.daily_bars = list(bars)

    def warmup_intraday(self, bars: List[Bar]) -> None:
        """Pre-load recent intraday bars so the strategy doesn't have to wait
        for max(donchian_n, atr_n)+2 live bars (~5+ hours) after each restart."""
        self.intraday_bars = list(bars)

    def update_daily_bar(self, bar: Bar) -> None:
        """Append a new completed daily bar (call once per day after close)."""
        self.daily_bars.append(bar)

    def update_intraday_bar(self, bar: Bar) -> Signal:
        """
        Main entry point. Call on every 15m bar close. Returns a Signal
        indicating desired position. Caller decides how to execute the diff.
        """
        self.intraday_bars.append(bar)

        # 1. Hard time stop — flat after configured cutoff
        cutoff_h, cutoff_m = [int(x) for x in self.cfg.flat_by_time.split(":")]
        bar_time = bar.timestamp.time()
        is_after_cutoff = (
            bar_time.hour > cutoff_h
            or (bar_time.hour == cutoff_h and bar_time.minute >= cutoff_m)
        )
        if is_after_cutoff:
            return self._set_flat(reason=f"time_stop>={self.cfg.flat_by_time}")

        # 2. Need enough history for indicators
        if len(self.daily_bars) < self.cfg.trend_slow:
            return self._hold(reason="warmup_daily")
        if len(self.intraday_bars) < max(self.cfg.donchian_n, self.cfg.atr_n) + 2:
            return self._hold(reason="warmup_intraday")

        # 3. Update DD circuit breaker
        self._update_dd_state()

        # 4. Update trailing stop / exit checks if in a position
        self._update_trail()
        if self._stop_hit(bar):
            return self._set_flat(reason="stop_hit")

        # 5. Compute trend regime from daily bars
        daily_closes = [b.close for b in self.daily_bars]
        ema_fast = ema(daily_closes, self.cfg.trend_fast)
        ema_slow = ema(daily_closes, self.cfg.trend_slow)
        if ema_fast is None or ema_slow is None:
            return self._hold(reason="trend_unset")
        # Tri-state regime with a dead-band: within trend_deadband_pct the EMAs
        # are treated as equal => no-trade regime (trend_dir == 0). A neutral
        # regime blocks new entries but does not force-exit an open position;
        # only a strict opposite-sign flip (or stops/time) closes a trade.
        band = self.cfg.trend_deadband_pct * ema_slow
        if ema_fast - ema_slow > band:
            trend_dir = 1
        elif ema_slow - ema_fast > band:
            trend_dir = -1
        else:
            trend_dir = 0
        if trend_dir == 0 and self.position_lots == 0:
            return self._hold(reason="trend_neutral")

        # 6. Donchian breakout on intraday
        recent = self.intraday_bars[-(self.cfg.donchian_n + 1):-1]   # exclude current
        donchian_high = max(b.high for b in recent)
        donchian_low = min(b.low for b in recent)

        # 7. Volatility-targeted sizing
        atr_intraday = atr(self.intraday_bars, self.cfg.atr_n)
        if atr_intraday is None or atr_intraday <= 0:
            return self._hold(reason="atr_unset")
        target_lots = self._target_lots(atr_intraday, bar.close)

        # 8. News blackout — no new entries during blackout windows
        if self._in_news_blackout(bar.timestamp) and self.position_lots == 0:
            return self._hold(reason="news_blackout")

        # 9. Entry logic
        if self.position_lots == 0:
            if trend_dir > 0 and bar.close > donchian_high:
                return self._enter_long(bar, target_lots, atr_intraday,
                                        reason="donchian_break_up")
            if trend_dir < 0 and bar.close < donchian_low:
                return self._enter_short(bar, target_lots, atr_intraday,
                                         reason="donchian_break_down")
        else:
            # Already in a position. Exit on opposite breakout (trend flip).
            if self.position_lots > 0 and trend_dir < 0:
                return self._set_flat(reason="trend_flip_down")
            if self.position_lots < 0 and trend_dir > 0:
                return self._set_flat(reason="trend_flip_up")

        return self._hold(reason="no_signal")

    def mark_to_market(self, last_price: float) -> None:
        """Update equity given the last close. Caller computes unrealized
        P&L externally; this just refreshes the peak equity tracker."""
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity

    def realize_pnl(self, pnl: float) -> None:
        """Caller calls this when a position is closed at known P&L."""
        self.equity += pnl
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity

    # ---- internal helpers ----

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
        self.high_water_since_entry = None
        self.low_water_since_entry = None
        return Signal(direction=0, target_lots=0, reason=reason)

    def _enter_long(self, bar: Bar, lots: int, atr_v: float, reason: str) -> Signal:
        self.position_lots = lots
        self.entry_price = bar.close
        self.high_water_since_entry = bar.high
        self.low_water_since_entry = bar.low
        self.stop_price = bar.close - self.cfg.init_stop_atr_mult * atr_v
        return Signal(direction=+1, target_lots=lots,
                      stop_price=self.stop_price, reason=reason)

    def _enter_short(self, bar: Bar, lots: int, atr_v: float, reason: str) -> Signal:
        self.position_lots = -lots
        self.entry_price = bar.close
        self.high_water_since_entry = bar.high
        self.low_water_since_entry = bar.low
        self.stop_price = bar.close + self.cfg.init_stop_atr_mult * atr_v
        return Signal(direction=-1, target_lots=lots,
                      stop_price=self.stop_price, reason=reason)

    def _update_trail(self) -> None:
        if self.position_lots == 0 or not self.intraday_bars:
            return
        bar = self.intraday_bars[-1]
        atr_v = atr(self.intraday_bars, self.cfg.atr_n) or 0.0
        if atr_v <= 0:
            return
        if self.position_lots > 0:
            if bar.high > (self.high_water_since_entry or 0.0):
                self.high_water_since_entry = bar.high
            new_stop = self.high_water_since_entry - self.cfg.trail_atr_mult * atr_v
            if self.stop_price is None or new_stop > self.stop_price:
                self.stop_price = new_stop
        else:
            if bar.low < (self.low_water_since_entry or float("inf")):
                self.low_water_since_entry = bar.low
            new_stop = self.low_water_since_entry + self.cfg.trail_atr_mult * atr_v
            if self.stop_price is None or new_stop < self.stop_price:
                self.stop_price = new_stop

    def _stop_hit(self, bar: Bar) -> bool:
        if self.position_lots == 0 or self.stop_price is None:
            return False
        if self.position_lots > 0 and bar.low <= self.stop_price:
            return True
        if self.position_lots < 0 and bar.high >= self.stop_price:
            return True
        return False

    def _target_lots(self, atr_value: float, price: float) -> int:
        """
        Vol-target sizing: lots so that ATR_dollars * lots ≈ target_per_bar_$.
        target_per_bar_$ = equity * vol_target_annual / sqrt(bars_per_year).

        ATR is in price units; per-lot ATR P&L = ATR * point_value * tick_factor.
        contract_multiplier=10 oz, point_value=$1 per $0.10 move, so $1 of
        price move per lot. We use contract_multiplier directly.
        """
        sigma_per_bar = atr_value
        target_dollar_risk = (
            self.equity * self.cfg.vol_target_annual / math.sqrt(self.cfg.bars_per_year)
        )
        # halve risk during drawdown
        if self.dd_active:
            target_dollar_risk *= 0.5

        per_lot_dollar_risk = sigma_per_bar * self.cfg.contract_multiplier
        if per_lot_dollar_risk <= 0:
            return self.cfg.min_lots
        lots = int(target_dollar_risk / per_lot_dollar_risk)

        # margin cap
        margin_budget = self.equity * self.cfg.max_margin_pct
        max_by_margin = int(margin_budget / max(self.cfg.margin_per_lot, 1.0))
        lots = min(lots, max_by_margin)

        lots = max(self.cfg.min_lots, min(self.cfg.max_lots, lots))
        return lots

    def _update_dd_state(self) -> None:
        if self.peak_equity <= 0:
            return
        dd = 1.0 - (self.equity / self.peak_equity)
        if dd >= self.cfg.dd_circuit_threshold and not self.dd_active:
            self.dd_active = True
        elif dd <= self.cfg.dd_recover_threshold and self.dd_active:
            self.dd_active = False

    def _in_news_blackout(self, ts: pd.Timestamp) -> bool:
        for dt, start, end in self.cfg.news_blackout:
            if ts.date() == dt and start <= ts.time() <= end:
                return True
        return False
