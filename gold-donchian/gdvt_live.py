"""
Live trading wrapper for GDVT strategy.

Bridges the BrokerBridge Python API to gdvt_strategy.GDVTStrategy.

WHAT THIS DOES:
  1. Subscribes to the gold contract tick stream via the bridge's QUOTEDATA
  2. Aggregates ticks into 15-minute bars in memory
  3. On each bar close (during day session 08:45–13:45), calls the strategy
  4. Computes the order delta and submits via the bridge's NEWORDER
  5. Logs everything to disk for post-mortem review

WHAT YOU MUST EDIT before running:
  • SYMBOL — set to active near-month gold contract (or the local-currency
    gold contract if you switch products)
  • PORT — your bridge port (9000 in the sample is just an example)
  • DAILY_HISTORY_CSV — path to a CSV of daily gold closes for trend warmup
  • Verify NEWORDER field semantics with the bridge documentation; the field
    codes below match the sample but exchange spec strings can differ by broker

PRE-FLIGHT CHECKLIST:
  • Run gdvt_backtest.py first and confirm metrics
  • Make sure the bridge app is running and you can place a manual test order
  • Run with `--dry-run` to verify orders log correctly without sending
  • Start with vol_target = 0.05 (1/3 of design) for first live week
"""

from __future__ import annotations
import argparse
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

EXCHANGE_TZ = ZoneInfo("UTC")  # exchange-local timezone (placeholder)
STALE_TICK_MAX_AGE_SEC = 3600  # drop any tick whose TickTime is > 60 min behind
                               # exchange-local wall clock. Originally 300s (5 min) but the
                               # bridge's broken streaming sends today's intraday ticks 30-60 min
                               # delayed and we'd reject all of them. 60 min lets us at
                               # least process the stuck-cached-tick into the aggregator.
                               # Trade-off: if the bridge sends a 50-min-old tick, strategy may
                               # build bars with stale prices. Acceptable given 1h bars.

# Make the bridge sample directory importable
BRIDGE_SAMPLE = Path(__file__).resolve().parent / "BrokerBridge" / "Sample" / "Python"
if BRIDGE_SAMPLE.exists():
    sys.path.insert(0, str(BRIDGE_SAMPLE))

try:
    from BridgeHelp import BridgeModule  # type: ignore
except Exception as e:
    print("WARNING: BridgeHelp not importable here. This file must run on the PC "
          "that has BrokerBridge installed and the bridge app running.")
    print(f"  Detail: {e}")
    BridgeModule = object  # so this file is at least syntax-checkable everywhere

from gdvt_strategy import GDVTStrategy, StrategyConfig, Bar


# ----- USER CONFIG -----------------------------------------------------------
# The gold contract delivery months are 6 consecutive EVEN months
# (Feb, Apr, Jun, Aug, Oct, Dec), per the exchange's contract spec.
#
# The exchange uses its own contract-month coding scheme; specifics omitted.
#
# Roll to the next near-month a few sessions before the front month's last
# trading day so we never trade into the expiry-day liquidity hole.
SYMBOL          = "FRONTc1"         # near-month gold contract. The near, liquid
                                   # contract gets ~200+ ticks/2min vs the next-near's
                                   # ~48/2min, so we trade the front month.
                                   # ⚠️ ROLL: switch to the next near-month a few
                                   # sessions before the front month's last trading day
                                   # — see roll calendar section in README/Notion.
BRIDGE_HOST     = "http://localhost:8080"

# !!! TODO: REPLACE BEFORE GOING LIVE !!!
# The bridge app assigns a unique port to each user. The 9000 you see
# in BrokerBridge/Sample/Python/main.py is a sample value, NOT your
# actual port. Find yours in the bridge UI (it's shown on the main bridge
# window when the bridge is running) and paste it below, then delete this banner.
BRIDGE_PORT     = 9000             # your ZMQ port from the bridge app → Settings → API

DAILY_HISTORY_CSV     = "gold_daily.csv"   # warms up daily trend filter
INTRADAY_HISTORY_CSV  = "gold_1h.csv"      # warms up Donchian/ATR — 1h matches live bar size
INTRADAY_STATE_FILE   = "logs/intraday_state.json"  # persisted across restarts within 24h
POSITION_STATE_FILE   = "logs/position_state.json"  # persists self.current_position
                                                    # across restarts so supervisor's
                                                    # 08:30 daily kill doesn't blow
                                                    # away our knowledge of an open
                                                    # position carried overnight.
                                                    # Critical guard: the script must
                                                    # not re-initialize current_position
                                                    # to 0 on every spawn regardless of
                                                    # the bridge's actual book state.
INTRADAY_BARS_KEEP    = 100                # only this many recent bars need to live in memory
INTRADAY_PERSIST_MAX_AGE_HRS = 24          # ignore persist file older than this; fall back to CSV
LOG_DIR         = "logs"
DRY_RUN         = True             # Hard-set True in this published copy of
                                   # the file. The live-trading path is
                                   # deliberately disabled — even if someone
                                   # supplies the proprietary broker SDK and
                                   # a valid BRIDGE_PORT, no NEWORDER calls will
                                   # actually fire. See the README disclaimer.

# Exchange order field codes (matches sample/main.py)
SIDE_BUY        = "1"
SIDE_SELL       = "2"
ORDER_TYPE_LIMIT = "2"
ORDER_TYPE_MARKET = "1"            # confirm with the bridge docs
TIME_IN_FORCE_ROD = "1"            # rest-of-day; verify
DAY_TRADE_OFF   = "0"
POSITION_OPEN   = ""               # blank = "auto" — the bridge figures out it's an open
                                   # because no existing position.
POSITION_CLOSE  = ""               # ALSO blank — the bridge's "auto" mode auto-detects close
                                   # because position exists. "4" was rejected; "C" untested.
                                   # The bridge reports PositionEffect="auto" on the entry
                                   # order, confirming "" is the right value.

# Day session window (per exchange rules)
SESSION_OPEN  = (8, 45)
SESSION_CLOSE = (13, 45)
# -----------------------------------------------------------------------------


def _setup_logger() -> logging.Logger:
    Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
    fname = Path(LOG_DIR) / f"gdvt_{datetime.now().strftime('%Y%m%d_%H%M')}.log"
    log = logging.getLogger("gdvt")
    log.setLevel(logging.INFO)
    fh = logging.FileHandler(fname, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    log.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
    log.addHandler(sh)
    return log


# ----- Bar aggregator --------------------------------------------------------

class HourlyAggregator:
    """Accumulate ticks into closed 1h bars (matches the backtest timeframe)."""

    def __init__(self):
        self.current_start: Optional[pd.Timestamp] = None
        self.open: Optional[float] = None
        self.high: Optional[float] = None
        self.low: Optional[float] = None
        self.close: Optional[float] = None
        self.volume: float = 0.0

    @staticmethod
    def _bar_start(ts: pd.Timestamp) -> pd.Timestamp:
        # floor to hour boundary
        return ts.replace(minute=0, second=0, microsecond=0)

    def add_tick(self, ts: pd.Timestamp, price: float, qty: float) -> Optional[Bar]:
        bar_start = self._bar_start(ts)
        closed: Optional[Bar] = None
        if self.current_start is None:
            self.current_start = bar_start
            self.open = self.high = self.low = self.close = price
        elif bar_start > self.current_start:
            closed = Bar(
                timestamp=self.current_start, open=self.open, high=self.high,
                low=self.low, close=self.close, volume=self.volume,
            )
            # roll over
            self.current_start = bar_start
            self.open = self.high = self.low = self.close = price
            self.volume = 0.0
        # update current
        self.high = max(self.high, price)  # type: ignore
        self.low = min(self.low, price)    # type: ignore
        self.close = price
        self.volume += qty
        return closed


# ----- Live wrapper ----------------------------------------------------------

class GDVTLive(BridgeModule):
    """Subclass of BridgeModule that runs the GDVT strategy."""

    def __init__(self, host: str, port: int, log: logging.Logger,
                 starting_equity: float, dry_run: bool):
        super().__init__(host, port)  # type: ignore  ← connects to the bridge

        # SUBSCRIBE IMMEDIATELY after connect — hypothesis:
        # the bridge may lock the data stream into "cached replay only" mode if QUOTEDATA
        # doesn't fire fast enough after connect. Smoke test (works) subscribes
        # within ~100ms; supervisor (fails) was subscribing after ~2.6s of warmup.
        # Test: do QUOTEDATA FIRST, warmup AFTER. Mimics smoke test timing.
        for sym in ["FRONTc1", "FRONTc2"]:
            self.QUOTEDATA(sym)  # type: ignore
        log.info(f"early-subscribed to FRONTc1+FRONTc2 (immediately after bridge connect)")

        self.log = log
        self.dry_run = dry_run
        self.cfg = StrategyConfig()
        self.strategy = GDVTStrategy(config=self.cfg, starting_equity=starting_equity)
        self.aggregator = HourlyAggregator()
        self.last_tick_price: Optional[float] = None  # for force-flatten orders
        self._heartbeat_lock = threading.Lock()       # heartbeat is fired from
                                                       # multiple bridge worker threads;
                                                       # lock prevents 4-7× duplicate
                                                       # log lines per heartbeat
        self._stale_dropped_count = 0                  # diagnostic counter
        self._invalid_dropped_count = 0                # ticks with non-numeric
                                                       # Price (e.g. "-") — the bridge
                                                       # sends these as "I'm
                                                       # alive" placeholders
                                                       # when no actual trade
                                                       # has occurred yet
        # REENTRANT lock guarding current_position. Two threads access it:
        # (a) bridge worker thread inside SHOWQUOTEDATA → _on_bar_close → _execute_diff,
        # (b) main thread inside check_session_cutoff → _execute_diff.
        # Without this lock the 13:30 force-flatten can race a worker-thread
        # entry from the 13:00 bar close — main reads current_position==0
        # while worker is mid-NEWORDER and hasn't yet set current_position=+1,
        # main returns "nothing to flatten", worker finishes the open → we
        # carry overnight. RLock (not Lock) so check_session_cutoff can hold
        # it while calling _execute_diff which also wants it.
        self._position_lock = threading.RLock()
        self.current_position: int = 0      # signed lots actually placed
        self._load_position(log)            # restore from sidecar if non-zero

        # warmup the daily trend filter
        if Path(DAILY_HISTORY_CSV).exists():
            df = pd.read_csv(DAILY_HISTORY_CSV)
            df.columns = [c.lower() for c in df.columns]
            df["datetime"] = pd.to_datetime(df["datetime"] if "datetime" in df.columns else df["date"])
            bars = [Bar(timestamp=r.datetime, open=r.open, high=r.high,
                        low=r.low, close=r.close, volume=getattr(r, "volume", 0.0))
                    for r in df.itertuples()]
            self.strategy.warmup_daily(bars)
            log.info(f"warmed up trend filter with {len(bars)} daily bars")
        else:
            log.warning(f"no daily history at {DAILY_HISTORY_CSV} — trend filter "
                        f"will be inactive until {self.cfg.trend_slow} live days pass")

        # warmup the intraday Donchian/ATR — without this, strategy needs ~5h of live bars
        # before it'll fire any signal, which is longer than a single day session.
        self._warmup_intraday(log)

    def _warmup_intraday(self, log: logging.Logger) -> None:
        """Pre-populate strategy.intraday_bars from the persist file (if recent)
        or from the historical 15-min CSV (yfinance GC=F proxy). Either way the
        strategy is signal-ready by the first live bar."""
        # 1. try the persist file first — real exchange bars from the most recent run
        state_path = Path(INTRADAY_STATE_FILE)
        if state_path.exists():
            age_hrs = (time.time() - state_path.stat().st_mtime) / 3600
            if age_hrs < INTRADAY_PERSIST_MAX_AGE_HRS:
                try:
                    with open(state_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    bars = [Bar(timestamp=pd.Timestamp(b["t"]), open=b["o"],
                                high=b["h"], low=b["l"], close=b["c"],
                                volume=b.get("v", 0.0)) for b in data]
                    self.strategy.warmup_intraday(bars)
                    log.info(f"warmed up intraday with {len(bars)} bars from "
                             f"persist file (age {age_hrs:.1f}h)")
                    return
                except Exception as e:
                    # Loud warning — silent fallback to yfinance CSV proxies can
                    # quietly change the strategy's first few signals of the day
                    # vs. what real exchange bars would have produced. The atomic
                    # write should prevent partial JSON, but other unreadability
                    # paths (disk full, permissions) remain.
                    log.error(f"⚠️  persist file unreadable ({e}); falling back to "
                              f"GC=F CSV proxy. First ~5h of signals may differ from "
                              f"what real exchange bars would produce. MANUALLY VERIFY "
                              f"the first bar close.")
            else:
                log.info(f"persist file is {age_hrs:.1f}h old (>{INTRADAY_PERSIST_MAX_AGE_HRS}h), "
                         f"using CSV instead")

        # 2. fall back to the historical CSV (yfinance GC=F proxy)
        csv_path = Path(INTRADAY_HISTORY_CSV)
        if csv_path.exists():
            df = pd.read_csv(csv_path)
            df.columns = [c.lower() for c in df.columns]
            df["datetime"] = pd.to_datetime(df["datetime"])
            df = df.tail(INTRADAY_BARS_KEEP)
            bars = [Bar(timestamp=r.datetime, open=r.open, high=r.high,
                        low=r.low, close=r.close, volume=getattr(r, "volume", 0.0))
                    for r in df.itertuples()]
            self.strategy.warmup_intraday(bars)
            log.info(f"warmed up intraday with {len(bars)} bars from {INTRADAY_HISTORY_CSV} "
                     f"(GC=F proxy; will drift to real exchange bars during the day)")
        else:
            log.warning(f"no intraday history at {INTRADAY_HISTORY_CSV} — strategy "
                        f"will need {max(self.cfg.donchian_n, self.cfg.atr_n)+2} live "
                        f"bars (~{(max(self.cfg.donchian_n, self.cfg.atr_n)+2)*15} min) "
                        f"before first signal. Run refresh_data.py.")

    def _persist_intraday_state(self) -> None:
        """Save the most recent N intraday bars so the next launch can resume
        without warmup. Trims the in-memory list to the same window.

        Writes atomically: dump to a sibling .tmp file then os.replace into
        place. Prevents corrupt-JSON-on-disk if supervisor's proc.terminate()
        hits mid-write (which silently falls back to the yfinance CSV on next
        startup, with no loud warning — a real footgun)."""
        bars = self.strategy.intraday_bars[-INTRADAY_BARS_KEEP:]
        self.strategy.intraday_bars = bars  # cap memory growth
        data = [{"t": b.timestamp.isoformat(), "o": b.open, "h": b.high,
                 "l": b.low, "c": b.close, "v": b.volume} for b in bars]
        path = Path(INTRADAY_STATE_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)  # atomic rename on Windows + POSIX

    def _persist_position(self) -> None:
        """Write current_position to sidecar JSON. Called whenever the position
        changes via _execute_diff so an overnight restart can recover it.
        Atomic write same as _persist_intraday_state."""
        path = Path(POSITION_STATE_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "current_position": self.current_position,
            "symbol": SYMBOL,
            "ts": datetime.now(tz=EXCHANGE_TZ).replace(tzinfo=None).isoformat(),
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)

    def _load_position(self, log: logging.Logger) -> None:
        """Read persisted current_position on startup. If it's non-zero, log a
        LOUD warning: the script can't query the bridge directly to confirm the
        position is still actually held (the bridge SDK has no callable
        position-query in the Sample/, only restore-report flow which we don't
        trust enough to wire up live), so the operator must manually verify in
        the bridge's open-positions tab and either kill the script or let it resume.

        Safe default: if the sidecar is missing/corrupt OR the symbol doesn't
        match (e.g. just rolled FRONTc1 → FRONTc2), assume flat. Worst case is a
        false-positive warning the operator dismisses; the alternative (silently
        assuming flat when we shouldn't) is what we're fixing here."""
        path = Path(POSITION_STATE_FILE)
        if not path.exists():
            log.info("no position state file — starting flat (current_position=0)")
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            persisted_pos = int(data.get("current_position", 0))
            persisted_sym = data.get("symbol", "")
            persisted_ts = data.get("ts", "?")
        except Exception as e:
            log.error(f"⚠️  position state file unreadable ({e}); assuming flat. "
                      f"MANUALLY VERIFY position in the bridge's open-positions tab "
                      f"before any trade fires.")
            return
        if persisted_sym != SYMBOL:
            log.warning(f"persisted position is for {persisted_sym!r}, current SYMBOL "
                        f"is {SYMBOL!r} (likely contract rolled); assuming flat. "
                        f"MANUALLY VERIFY no open {persisted_sym} position remains.")
            return
        if persisted_pos == 0:
            log.info(f"position state restored: flat (persisted at {persisted_ts})")
            return
        # Non-zero persisted position. This is the critical path.
        self.current_position = persisted_pos
        banner = "⚠️ " * 10
        log.error(
            "\n" + banner + "\n"
            f"⚠️  CARRIED POSITION DETECTED: {persisted_pos:+d} lot(s) of {SYMBOL}\n"
            f"⚠️  Persisted at: {persisted_ts}\n"
            f"⚠️  Script is resuming with current_position={persisted_pos:+d}.\n"
            f"⚠️  ACTION: open the bridge app → open-positions tab → confirm {persisted_pos:+d} lot(s) of\n"
            f"⚠️     {SYMBOL} is actually held. If the bridge shows flat (= persist file is\n"
            f"⚠️     stale), Ctrl+C this script, edit logs/position_state.json to set\n"
            f"⚠️     current_position=0, then restart.\n"
            + banner
        )

    # Called by BridgeModule on each tick
    def SHOWQUOTEDATA(self, obj):  # noqa: N802 (matches superclass)
        try:
            if obj.get("Symbol") != SYMBOL:
                return
            # Use exchange-local wall clock as ground truth for both tick timestamp and
            # the staleness check. PC clock may be on a different zone, but the bridge
            # sends exchange-local time.
            now_exch = datetime.now(tz=EXCHANGE_TZ).replace(tzinfo=None)
            ts_raw = obj.get("TickTime")
            # The bridge occasionally sends ts_raw="-" (a literal dash) as a placeholder
            # when there's a quote update but no actual trade timestamp yet —
            # happens around session-open and during halts. A naive truthy
            # check passes "-" through to pd.to_datetime() which crashes with
            # DateParseError. Filter those plus any other non-numeric placeholders.
            if (not ts_raw) or str(ts_raw).strip() in {"", "-", "--", "N/A", "n/a"}:
                ts = pd.Timestamp(now_exch)
            else:
                try:
                    ts = pd.to_datetime(ts_raw)
                except (ValueError, TypeError):
                    # any other parse failure: fall back to wall clock, don't crash
                    ts = pd.Timestamp(now_exch)

            # STALE TICK FILTER: the bridge occasionally replays cached ticks from a
            # prior session when we (re-)subscribe. Those ticks have old
            # timestamps and would pollute the bar aggregator (e.g. firing a
            # phantom donchian breakout on yesterday's 4779 print). Drop them.
            ts_dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
            age_sec = (now_exch - ts_dt).total_seconds()
            if age_sec > STALE_TICK_MAX_AGE_SEC:
                self._stale_dropped_count += 1
                # log only the first stale tick per minute to avoid spam
                if self._stale_dropped_count == 1 or self._stale_dropped_count % 100 == 0:
                    self.log.info(f"dropped stale tick (age {age_sec:.0f}s, "
                                  f"ts={ts}, count={self._stale_dropped_count})")
                return

            price_str = str(obj.get("Price", "")).replace(",", "").strip()
            # The bridge sends "placeholder ticks" with Price="-" (and TickTime="-")
            # whenever it wants to push a quote-board update but no actual
            # trade has occurred. These carry no usable price for our strategy
            # — drop them entirely with a counter, same pattern as the stale
            # filter. Gold's pre-trade hours generate hundreds of these per
            # session, and a naive float() crashes on every one.
            if price_str in {"", "-", "--", "N/A", "n/a"}:
                self._invalid_dropped_count += 1
                if (self._invalid_dropped_count == 1
                        or self._invalid_dropped_count % 100 == 0):
                    self.log.info(
                        f"dropped placeholder tick (Price={price_str!r}, "
                        f"count={self._invalid_dropped_count})")
                return
            try:
                price = float(price_str)
            except ValueError:
                self._invalid_dropped_count += 1
                if (self._invalid_dropped_count == 1
                        or self._invalid_dropped_count % 100 == 0):
                    self.log.info(
                        f"dropped tick with unparseable Price={price_str!r} "
                        f"(count={self._invalid_dropped_count})")
                return
            qty = float(obj.get("Qty", 0) or 0)
            self.last_tick_price = price
            # heartbeat: log a line every ~5 minutes when ticks flow, so the
            # supervisor's silent-fail detector sees activity even before any
            # bar closes. Lock prevents duplicate log lines from concurrent
            # bridge worker threads racing the debounce check.
            now_t = time.time()
            with self._heartbeat_lock:
                if (not hasattr(self, "_last_heartbeat")
                        or now_t - self._last_heartbeat > 300):
                    self.log.info(f"heartbeat: tick {ts} price={price:.2f}")
                    self._last_heartbeat = now_t
            closed_bar = self.aggregator.add_tick(ts, price, qty)
            if closed_bar:
                self._on_bar_close(closed_bar)
        except Exception as e:
            self.log.exception(f"SHOWQUOTEDATA error: {e}")

    # Called when a 15m bar closes
    def _on_bar_close(self, bar: Bar):
        # ignore bars outside the day session
        if not self._in_day_session(bar.timestamp):
            return
        sig = self.strategy.update_intraday_bar(bar)
        # persist intraday state so the next restart can resume without warmup
        self._persist_intraday_state()
        # Hold the position lock from the current_position READ through the
        # _execute_diff call. Without this lock, check_session_cutoff (main
        # thread) could read a stale current_position == 0 while we're mid-
        # ordering here, decide "nothing to flatten," and miss the 13:30
        # force-flatten. RLock makes _execute_diff's own acquire a no-op.
        with self._position_lock:
            target_signed = sig.direction * sig.target_lots
            if target_signed == self.current_position:
                self.log.info(f"bar {bar.timestamp} close={bar.close:.2f} "
                              f"signal=hold reason={sig.reason}")
                return
            self.log.info(f"bar {bar.timestamp} close={bar.close:.2f} "
                          f"signal={sig.direction}*{sig.target_lots} "
                          f"reason={sig.reason}")
            self._execute_diff(target_signed, bar.close)

    def _execute_diff(self, target_signed: int, ref_price: float,
                      is_force_flatten: bool = False) -> bool:
        """Send orders to move from current_position to target_signed.

        Returns True if the resulting state matches target_signed (whether by
        the path that sent orders successfully or by the early-return when
        delta == 0). Returns False if any order was rejected — caller must NOT
        update downstream state (e.g. strategy._set_flat) on a False return.

        is_force_flatten=True swaps LIMIT → MARKET on every leg. Use this from
        check_session_cutoff so the 13:30 close goes through even if the
        ref price is stale or the book has gapped. Sending a LIMIT at
        last_tick_price would be 0.0 if no ticks all session → the exchange
        rejects → position stays open into overnight session. MARKET is the
        right primitive for "I MUST be flat in the next few seconds" intent.
        """
        with self._position_lock:
            delta = target_signed - self.current_position
            if delta == 0:
                return True
            # cross zero in two trades to keep it simple: close-then-open
            steps = []
            if (self.current_position > 0 and target_signed < 0) or \
               (self.current_position < 0 and target_signed > 0):
                steps.append((-self.current_position, POSITION_CLOSE))
                steps.append((target_signed, POSITION_OPEN))
            elif (self.current_position > 0 and target_signed == 0) or \
                 (self.current_position < 0 and target_signed == 0):
                steps.append((-self.current_position, POSITION_CLOSE))
            elif self.current_position == 0 and target_signed != 0:
                steps.append((target_signed, POSITION_OPEN))
            else:
                # same side, size change
                d = target_signed - self.current_position
                steps.append((d, POSITION_OPEN if abs(target_signed) > abs(self.current_position)
                              else POSITION_CLOSE))

            order_type = ORDER_TYPE_MARKET if is_force_flatten else ORDER_TYPE_LIMIT
            for qty_signed, position_effect in steps:
                if qty_signed == 0:
                    continue
                side = SIDE_BUY if qty_signed > 0 else SIDE_SELL
                order = {
                    "Symbol1": SYMBOL,
                    # Market orders ignore Price but the bridge schema still wants the field;
                    # send 0 as a placeholder. Limit orders use the ref_price as before.
                    "Price": f"{ref_price:.1f}" if not is_force_flatten else "0",
                    "TimeInForce": TIME_IN_FORCE_ROD,
                    "Side1": side,
                    "OrderType": order_type,
                    "OrderQty": str(abs(qty_signed)),
                    "DayTrade": DAY_TRADE_OFF,
                    "Symbol2": "",
                    "Side2": "",
                    "PositionEffect": position_effect,
                }
                if self.dry_run:
                    self.log.info(f"DRY-RUN order ({'MARKET-flatten' if is_force_flatten else 'LIMIT'}): "
                                  f"{json.dumps(order, ensure_ascii=False)}")
                else:
                    resp = self.NEWORDER(order)  # type: ignore
                    self.log.info(f"NEWORDER -> {resp}")
                    # If the bridge rejected the order, do NOT advance our position state.
                    # Code "0000" = success per the bridge's convention; anything else = failure.
                    if isinstance(resp, dict) and resp.get("Code") != "0000":
                        self.log.error(f"order rejected (resp={resp}); aborting "
                                       f"position state update. Current internal "
                                       f"position stays at {self.current_position}. "
                                       f"MANUALLY VERIFY in the bridge's open-positions tab.")
                        return False  # caller must not assume state matches target
            self.current_position = target_signed
            self._persist_position()  # sidecar JSON so restart can recover
            return True

    @staticmethod
    def _in_day_session(ts: pd.Timestamp) -> bool:
        t = ts.time()
        oh, om = SESSION_OPEN
        ch, cm = SESSION_CLOSE
        return (t.hour, t.minute) >= (oh, om) and (t.hour, t.minute) <= (ch, cm)

    def check_session_cutoff(self) -> None:
        """Wall-clock-based force-flatten at 13:30 exchange-local. Required for 1h bars
        because no in-session bar has timestamp >= 13:30 (last is 13:00), so
        the strategy's own time-stop never fires from bar processing alone.
        Called periodically from the main loop.

          - holds position lock across the read+execute (matches _on_bar_close)
          - refuses to send if last_tick_price is None (don't send LIMIT@0)
          - sends MARKET orders, not LIMIT, so a stale ref or gapped book can't
            leave the position open
          - only calls strategy._set_flat() if the close actually succeeded —
            otherwise strategy state would lie about being flat while
            current_position is still ±1
        """
        with self._position_lock:
            if self.current_position == 0:
                return
            now_exch = datetime.now(tz=EXCHANGE_TZ).replace(tzinfo=None)
            flat_h, flat_m = [int(x) for x in self.cfg.flat_by_time.split(":")]
            is_after_flat = (now_exch.hour, now_exch.minute) >= (flat_h, flat_m)
            is_before_close = (now_exch.hour, now_exch.minute) <= SESSION_CLOSE
            if not (is_after_flat and is_before_close):
                return
            if self.last_tick_price is None:
                # No ticks all session → no reference price → can't safely send
                # any order. Worse, we don't actually know if the position is
                # still held (we haven't seen ANY market data). Refuse to send,
                # log loud, repeat every 30s until the operator intervenes.
                self.log.error(
                    f"⚠️  FORCE-FLATTEN NEEDED but last_tick_price is None — "
                    f"position is {self.current_position:+d} lot(s) of {SYMBOL} "
                    f"and we've received no ticks this session. NOT sending an "
                    f"order (would be unsafe). MANUAL ACTION: open the bridge app → "
                    f"open-positions tab → close {SYMBOL} manually if held. Will retry "
                    f"this check every 30s until session ends."
                )
                return
            ref = self.last_tick_price
            self.log.info(f"wall-clock {now_exch.time()} >= {self.cfg.flat_by_time}, "
                          f"force-flattening {self.current_position:+d} lots @ MARKET "
                          f"(ref last_tick={ref:.2f})")
            ok = self._execute_diff(0, ref, is_force_flatten=True)
            if ok:
                # only sync strategy state if the close actually went through
                self.strategy._set_flat(reason="wall_clock_time_stop")
            else:
                self.log.error(
                    f"⚠️  force-flatten order was REJECTED by the bridge. Strategy "
                    f"state NOT updated to flat. current_position remains "
                    f"{self.current_position:+d}. Will retry every 30s until "
                    f"session ends. MANUAL ACTION: check the bridge's open-positions tab."
                )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--equity", type=float, default=2_000_000.0)
    p.add_argument("--dry-run", action="store_true",
                   help="log orders without sending them to the bridge")
    args = p.parse_args()

    log = _setup_logger()
    # Hard guards: refuse to start if config is incomplete or live without dry-run flag.
    if BRIDGE_PORT is None:
        log.error("BRIDGE_PORT is not set. Open gdvt_live.py and replace the TODO "
                  "with your actual bridge port (shown in the bridge app's main window).")
        sys.exit(2)
    effective_dry_run = args.dry_run or DRY_RUN
    if not effective_dry_run:
        log.warning("Running LIVE (orders WILL be sent). Re-run with --dry-run "
                    "to smoke-test, or set DRY_RUN=True at the top of this file.")
    log.info(f"GDVT live starting | symbol={SYMBOL} port={BRIDGE_PORT} "
             f"dry_run={effective_dry_run}")
    bot = GDVTLive(BRIDGE_HOST, BRIDGE_PORT, log, starting_equity=args.equity,
                   dry_run=effective_dry_run)
    # Subscription already happened in __init__ (early-subscribe hypothesis test).
    # Don't re-subscribe here — that's what the disabled periodic refresh was doing
    # and it didn't help. Just wait for ticks to flow.
    KEEPALIVE_SYMBOLS = ["FRONTc1", "FRONTc2"]
    log.info(f"waiting for ticks on {SYMBOL}… (subscribed in __init__)")
    last_resubscribe = time.time()
    try:
        while True:
            time.sleep(30)
            bot.check_session_cutoff()  # wall-clock force-flatten at 13:30 exchange-local
            # PERIODIC RE-SUBSCRIBE DISABLED. Hypothesis: every
            # QUOTEDATA() call resets the bridge to "send cached tick once, then go
            # silent" mode. A known-working test only subscribed once and
            # listened — no periodic refresh. Our smoke test (which works) also
            # subscribes once. The supervisor (which fails) was the only thing
            # periodically re-subscribing. Removing the refresh to mimic the
            # working pattern. If ticks still stop flowing after a few hours,
            # we'll re-enable.
            # if time.time() - last_resubscribe > 1500:
            #     for sym in KEEPALIVE_SYMBOLS:
            #         bot.QUOTEDATA(sym)
            #     log.info(f"re-subscribed to {KEEPALIVE_SYMBOLS} (periodic refresh)")
            #     last_resubscribe = time.time()
            _ = last_resubscribe  # silence unused-var lint
    except KeyboardInterrupt:
        log.info("shutdown requested")
        os._exit(0)  # force exit; the bridge's ZMQ threads are non-daemon and would hang


if __name__ == "__main__":
    main()
