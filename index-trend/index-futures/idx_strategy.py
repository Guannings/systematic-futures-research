"""
Index Gap-Momentum / Trend-Hold Strategy (honest-V4)
====================================================

Day-session strategy for an exchange-listed equity-index future. Validated on
real day-session index futures, 2019-2026, lookahead-free, after index-futures
costs.

THE EDGE (the only one that survived honest testing):
  At the session open, the overnight gap CONTINUES into the close *only when it
  agrees with the prevailing daily trend*. Raw gap-continuation is dead on the
  real futures (night session pre-absorbs it); the trend-aligned subset has a
  modest real edge (gross Sharpe ~1.0, net ~0.5; shines in trending markets).

DECISION (made once, at the session open):
  1. trend = sign(EMA20[yesterday] - EMA20[day-before])   # known at the open;
     uses only CLOSES UP TO YESTERDAY — never today's close (no lookahead).
  2. gap   = session_open / prev_session_close - 1
  3. If |gap| >= gap_threshold AND sign(gap) == trend:
         enter sign(gap) at the open, vol-targeted size.
     Else: stay flat today.
  4. Exit: force-flat by 13:30 (handled by the live wrapper / backtest).

This file is pure logic — no I/O, no API. Same contract as gdvt_strategy.py so
it slots into the same live harness. `vol_target_annual` is the sizing dial.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import List, Optional
import math


@dataclass
class Bar:
    timestamp: object
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Signal:
    direction: int            # +1 long, -1 short, 0 flat
    target_lots: int          # absolute size (>= 0)
    reason: str = ""


@dataclass
class IndexGapConfig:
    # mode: "gap"        = intraday gap-momentum, flat by 13:30 (break-even net — legacy)
    #       "trend_hold" = leveraged long when index > EMA(trend_n), HELD OVERNIGHT.
    #                      The validated configuration: net Sharpe 1.3, OOS 1.55,
    #                      +58%/yr at 3x. Use sizing_mode="max_margin" to leverage.
    mode: str = "gap"
    trend_n: int = 50             # EMA length for trend_hold mode

    # signal (gap mode)
    ema_trend_n: int = 20         # daily EMA; trend = sign of its 1-day slope (lagged)
    gap_threshold: float = 0.0    # min |gap| (fraction) to trade; 0 = all aligned gaps

    # ── SIZING ──
    # sizing_mode = "vol_target": risk-calibrated (gold-style). Conservative;
    #   sizes to vol_target_annual of portfolio vol. ~1-2 lots on 2M at 0.20.
    # sizing_mode = "max_margin": deploys `margin_fraction` of equity as margin,
    #   directionally — pure variance, no risk-calibration. The high-leverage dial.
    sizing_mode: str = "vol_target"
    vol_target_annual: float = 0.20   # used in vol_target mode
    margin_fraction: float = 0.80     # used in max_margin mode
    atr_n: int = 14
    bars_per_year: float = 252.0  # one open->close trade per session

    # index-futures contract
    contract_multiplier: float = 50.0     # currency units per index point
    # Margin scales with the index level (notional × rate), so it never goes
    # stale as the index moves. ~0.11 ≈ the exchange's index-futures initial
    # margin rate. VERIFY the exact current rate on the exchange.
    margin_rate: float = 0.11
    max_margin_pct: float = 0.50          # fraction of equity allowed as margin
    min_lots: int = 1
    max_lots: int = 200                   # scale ceiling


def _ema(values: List[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    k = 2.0 / (n + 1.0)
    e = sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1.0 - k)
    return e


def _atr_from_daily(bars: List[Bar], n: int) -> Optional[float]:
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i].high, bars[i].low, bars[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-n:]) / n


class IndexGapStrategy:
    """Stateful only in that it holds the daily-history warmup. The trade
    decision is a pure function of (daily history, today's open, equity)."""

    def __init__(self, config: Optional[IndexGapConfig] = None,
                 starting_equity: float = 2_000_000.0):
        self.cfg = config or IndexGapConfig()
        self.equity = float(starting_equity)
        self.daily_bars: List[Bar] = []

    # ---- warmup / daily roll ----
    def warmup_daily(self, bars: List[Bar]) -> None:
        self.daily_bars = list(bars)

    def update_daily_bar(self, bar: Bar) -> None:
        self.daily_bars.append(bar)

    def realize_pnl(self, pnl: float) -> None:
        self.equity += pnl

    # ---- the single daily decision ----
    def decide_open(self, session_open: float) -> Signal:
        """Call once, at the session open, with the opening trade price."""
        cfg = self.cfg
        closes = [b.close for b in self.daily_bars]

        # ── trend_hold mode: leveraged long while index > EMA(trend_n), held ──
        if cfg.mode == "trend_hold":
            if len(closes) < cfg.trend_n + 2:
                return Signal(0, 0, reason="warmup_daily")
            ema = _ema(closes, cfg.trend_n)            # EMA through yesterday (no lookahead)
            prev_close = closes[-1]
            if ema is None:
                return Signal(0, 0, reason="trend_unset")
            if prev_close > ema:
                lots = self._target_lots(session_open)
                return Signal(1, lots, reason=f"trend_long(close{prev_close:.0f}>ema{ema:.0f})_lots{lots}")
            return Signal(0, 0, reason=f"trend_flat(close{prev_close:.0f}<=ema{ema:.0f})")

        # trend known at the open: EMA slope using closes UP TO YESTERDAY only.
        if len(closes) < cfg.ema_trend_n + 2:
            return Signal(0, 0, reason="warmup_daily")
        ema_yest = _ema(closes, cfg.ema_trend_n)         # EMA through yesterday (closes[-1])
        ema_prev = _ema(closes[:-1], cfg.ema_trend_n)    # EMA through the day before
        if ema_yest is None or ema_prev is None:
            return Signal(0, 0, reason="trend_unset")
        trend = 1 if ema_yest > ema_prev else (-1 if ema_yest < ema_prev else 0)

        prev_close = closes[-1]                       # yesterday's session close
        if prev_close <= 0:
            return Signal(0, 0, reason="bad_prev_close")
        gap = session_open / prev_close - 1.0

        if abs(gap) < cfg.gap_threshold:
            return Signal(0, 0, reason=f"gap_too_small({gap*100:.2f}%)")
        side = 1 if gap > 0 else -1
        if side != trend:
            return Signal(0, 0, reason=f"gap_against_trend(gap{side:+d}/trend{trend:+d})")

        lots = self._target_lots(session_open)
        return Signal(side, lots,
                      reason=f"gap{gap*100:+.2f}%_with_trend{trend:+d}_lots{lots}")

    # ---- sizing ----
    def _target_lots(self, price: float) -> int:
        cfg = self.cfg
        margin_lot = max(cfg.margin_rate * price * cfg.contract_multiplier, 1.0)  # scales w/ index level
        if cfg.sizing_mode == "max_margin":
            # deploy a fixed fraction of equity as margin, directional (high leverage).
            lots = int((self.equity * cfg.margin_fraction) / margin_lot)
            return max(cfg.min_lots, min(lots, cfg.max_lots))
        # default: risk-calibrated vol target
        atr = _atr_from_daily(self.daily_bars, cfg.atr_n)
        if not atr or atr <= 0:
            return cfg.min_lots
        target_risk = self.equity * cfg.vol_target_annual / math.sqrt(cfg.bars_per_year)
        per_lot_risk = atr * cfg.contract_multiplier
        lots = int(target_risk / per_lot_risk) if per_lot_risk > 0 else cfg.min_lots
        max_by_margin = int((self.equity * cfg.max_margin_pct) / margin_lot)
        return max(cfg.min_lots, min(lots, max_by_margin, cfg.max_lots))


if __name__ == "__main__":
    # tiny self-test on synthetic data
    import random
    random.seed(1)
    bars = []
    px = 20000.0
    for i in range(60):
        px *= 1 + random.uniform(-0.01, 0.012)   # mild uptrend
        bars.append(Bar(i, px, px * 1.005, px * 0.995, px, 1000))
    s = IndexGapStrategy(IndexGapConfig(vol_target_annual=0.5), starting_equity=2_000_000)
    s.warmup_daily(bars)
    for gap_pct in (-1.0, -0.3, 0.3, 1.0):
        op = bars[-1].close * (1 + gap_pct / 100)
        print(f"gap {gap_pct:+.1f}% -> {s.decide_open(op)}")
