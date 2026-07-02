"""
Live trading wrapper for the index Gap-Momentum / Trend-Hold strategy.

Same broker-bridge integration as gdvt_live.py, but the trade logic is a SINGLE
decision at the session open instead of per-bar:

  1. Subscribe to the active near-month index-future tick stream.
  2. On the first valid trade tick at/after 08:45 (= the session open price),
     call strategy.decide_open(open_price) ONCE for the day.
  3. If it returns a non-flat signal, enter immediately at MARKET.
  4. Hold intraday. In trend_hold mode, carry overnight (no 13:30 flatten);
     in gap mode, force-flatten at 13:30 (wall-clock), same as gold.
  5. Persist position; reuse all the tick-filter / staleness / safety patterns.

EDIT BEFORE LIVE:
  • SYMBOL   — active near-month index future. The exchange uses its own
               contract-month coding scheme; specifics omitted. VERIFY the
               active near-month (the front contract rolls on a fixed monthly
               schedule).
  • BRIDGE_PORT — your bridge port (9000 in the sample is just an example).
  • DAILY_HISTORY_CSV — current daily closes for EMA trend + prev_close.
               Refresh it before each session so trend & gap are computed
               against yesterday's real close.
  • DRY_RUN  — set False to actually send orders (published copy hard-sets True).
  • cfg.vol_target_annual — the sizing dial (see idx_strategy.py).

Like gold, the published copy cannot route live orders: the vendor broker
bridge SDK is omitted, BRIDGE_PORT is a sample value, and DRY_RUN is hard-set
True. See the repo disclaimer.
"""
from __future__ import annotations
import argparse, json, logging, os, sys, threading, time
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

EXCHANGE_TZ = ZoneInfo("UTC")   # exchange-local timezone (placeholder)
STALE_TICK_MAX_AGE_SEC = 3600

# Make the bridge sample directory importable (vendor SDK, gitignored)
BRIDGE_SAMPLE = Path(__file__).resolve().parent.parent / "BrokerBridge" / "Sample" / "Python"
if BRIDGE_SAMPLE.exists():
    sys.path.insert(0, str(BRIDGE_SAMPLE))
try:
    from BridgeHelp import BridgeModule  # type: ignore
except Exception as e:
    print(f"WARNING: BridgeHelp not importable ({e}). Run on the PC with the "
          f"bridge app installed and running.")
    BridgeModule = object

from idx_strategy import IndexGapStrategy, IndexGapConfig, Bar, Signal

# ---- USER CONFIG ----
SYMBOL          = "FRONTc1"        # near-month index future; verify before live.
CONTRACT_MULTIPLIER = 50.0         # currency units per index point
BRIDGE_HOST     = "http://localhost:8080"

# !!! TODO: REPLACE BEFORE GOING LIVE !!!
# The bridge app assigns a unique port to each user. The 9000 in the SDK sample
# is a sample value, NOT your actual port. Find yours in the bridge UI.
BRIDGE_PORT     = 9000             # your bridge port
# The full-size index future tracks the SAME index, so its daily closes are the
# correct trend/price proxy for the EMA(trend_n) signal and notional/margin scaling.
DAILY_HISTORY_CSV   = "index-futures/index_fut_daily.csv"   # daily closes for EMA(trend_n) + level
POSITION_STATE_FILE = "logs/idx_position_state.json"
LOG_DIR         = "logs"
DRY_RUN         = True             # Hard-set True in this published copy. The
                                   # live-trading path is deliberately disabled —
                                   # no NEWORDER calls will fire regardless of
                                   # configuration. See the README disclaimer.

# order field codes (match the bridge sample)
SIDE_BUY = "1"; SIDE_SELL = "2"
ORDER_TYPE_LIMIT = "2"; ORDER_TYPE_MARKET = "1"
TIME_IN_FORCE_ROD = "1"; DAY_TRADE_OFF = "0"
POSITION_OPEN = ""; POSITION_CLOSE = ""    # "" = the bridge's "auto" mode, confirmed on gold

SESSION_OPEN  = (8, 45)
SESSION_CLOSE = (13, 45)
FLAT_BY       = (13, 30)
HOLD_OVERNIGHT = True    # carry positions overnight (trend_hold mode);
                         # disables the 13:30 force-flatten. The venue permits
                         # holding overnight.


def _setup_logger() -> logging.Logger:
    Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
    fname = Path(LOG_DIR) / f"idx_{datetime.now().strftime('%Y%m%d_%H%M')}.log"
    log = logging.getLogger("idx")
    log.setLevel(logging.INFO)
    fh = logging.FileHandler(fname, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    log.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
    log.addHandler(sh)
    return log


class IndexLive(BridgeModule):
    def __init__(self, host, port, log, starting_equity, dry_run, cfg: IndexGapConfig):
        super().__init__(host, port)  # type: ignore
        self.QUOTEDATA(SYMBOL)        # type: ignore  early subscribe (gold lesson)
        log.info(f"early-subscribed to {SYMBOL}")
        self.log = log
        self.dry_run = dry_run
        self.cfg = cfg
        self.strategy = IndexGapStrategy(config=cfg, starting_equity=starting_equity)
        self.last_tick_price: Optional[float] = None
        self._heartbeat_lock = threading.Lock()
        self._position_lock = threading.RLock()
        self._stale_dropped = 0
        self._invalid_dropped = 0
        self.current_position: int = 0
        # per-session state
        self._session_date = None
        self._decided_today = False
        self._load_position(log)
        self._warmup_daily(log)

    def _warmup_daily(self, log):
        p = Path(DAILY_HISTORY_CSV)
        if not p.exists():
            log.error(f"no daily history at {DAILY_HISTORY_CSV} — strategy will stay "
                      f"flat (no trend). Refresh it before the session!")
            return
        df = pd.read_csv(p)
        df.columns = [c.lower() for c in df.columns]
        dcol = "date" if "date" in df.columns else "datetime"
        df[dcol] = pd.to_datetime(df[dcol])
        df = df.sort_values(dcol).reset_index(drop=True)
        bars = [Bar(timestamp=getattr(r, dcol), open=r.open, high=r.high,
                    low=r.low, close=r.close, volume=getattr(r, "volume", 0.0))
                for r in df.itertuples(index=False)]
        self.strategy.warmup_daily(bars)
        log.info(f"warmed up {len(bars)} daily bars; last close={bars[-1].close:.0f}")

    # ---- position persistence (atomic, same as gold) ----
    def _persist_position(self):
        path = Path(POSITION_STATE_FILE); path.parent.mkdir(parents=True, exist_ok=True)
        data = {"current_position": self.current_position, "symbol": SYMBOL,
                "ts": datetime.now(tz=EXCHANGE_TZ).replace(tzinfo=None).isoformat()}
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)

    def _load_position(self, log):
        path = Path(POSITION_STATE_FILE)
        if not path.exists():
            log.info("no position state — starting flat"); return
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception as e:
            log.error(f"⚠️ position state unreadable ({e}); assuming flat. VERIFY in "
                      f"the bridge app's open-positions tab.")
            return
        if data.get("symbol") != SYMBOL or int(data.get("current_position", 0)) == 0:
            log.info(f"position state: flat or different symbol — starting flat"); return
        self.current_position = int(data["current_position"])
        # The published bridge SDK sample exposes no callable position query, so a
        # carried position can't be auto-confirmed — the operator must verify manually.
        log.error("⚠️ " * 8 + f"\n⚠️ CARRIED POSITION {self.current_position:+d} {SYMBOL} "
                  f"(persisted {data.get('ts')}). VERIFY in the bridge app's "
                  f"open-positions tab; edit {POSITION_STATE_FILE} to 0 if stale.\n"
                  + "⚠️ " * 8)

    # ---- tick handler ----
    def SHOWQUOTEDATA(self, obj):  # noqa: N802
        try:
            # DIAGNOSTIC: log the first few raw ticks of ANY symbol so we can see
            # whether the bridge is forwarding at all, and what Symbol string it uses.
            if getattr(self, "_raw_seen", 0) < 8:
                self._raw_seen = getattr(self, "_raw_seen", 0) + 1
                self.log.info(f"RAW TICK #{self._raw_seen}: Symbol={obj.get('Symbol')!r} "
                              f"Price={obj.get('Price')!r} keys={list(obj.keys())}")
            if obj.get("Symbol") != SYMBOL:
                return
            now_exch = datetime.now(tz=EXCHANGE_TZ).replace(tzinfo=None)
            ts_raw = obj.get("TickTime")
            if (not ts_raw) or str(ts_raw).strip() in {"", "-", "--", "N/A", "n/a"}:
                ts = pd.Timestamp(now_exch)
            else:
                try: ts = pd.to_datetime(ts_raw)
                except (ValueError, TypeError): ts = pd.Timestamp(now_exch)
            age = (now_exch - (ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts)).total_seconds()
            if age > STALE_TICK_MAX_AGE_SEC:
                self._stale_dropped += 1; return
            ps = str(obj.get("Price", "")).replace(",", "").strip()
            if ps in {"", "-", "--", "N/A", "n/a"}:
                self._invalid_dropped += 1; return
            try: price = float(ps)
            except ValueError:
                self._invalid_dropped += 1; return
            self.last_tick_price = price
            now_t = time.time()
            with self._heartbeat_lock:
                if not hasattr(self, "_last_hb") or now_t - self._last_hb > 30:
                    self.log.info(f"heartbeat: {ts} price={price:.1f} pos={self.current_position:+d}")
                    self._last_hb = now_t
            self._maybe_decide_open(ts, price)
        except Exception as e:
            self.log.exception(f"SHOWQUOTEDATA error: {e}")

    def _maybe_decide_open(self, ts, price: float):
        """On the first valid in-session tick of a new date, make the one-shot
        gap decision and enter. Idempotent for the rest of the session."""
        t = ts.time() if hasattr(ts, "time") else None
        if t is None:
            return
        in_sess = (t.hour, t.minute) >= SESSION_OPEN and (t.hour, t.minute) <= SESSION_CLOSE
        if not in_sess:
            return
        date = ts.date() if hasattr(ts, "date") else None
        if date != self._session_date:
            # new session: reset one-shot flag
            self._session_date = date
            self._decided_today = False
        if self._decided_today:
            return
        with self._position_lock:
            if self._decided_today:
                return
            self._decided_today = True
            sig = self.strategy.decide_open(price)
            self.log.info(f"SESSION OPEN {ts} open_price={price:.1f} -> "
                          f"signal={sig.direction}*{sig.target_lots} reason={sig.reason}")
            target = sig.direction * sig.target_lots
            if target != self.current_position:
                self._execute_diff(target, price)

    # ---- order execution (same shape as gold) ----
    def _execute_diff(self, target_signed: int, ref_price: float,
                      is_force_flatten: bool = False) -> bool:
        with self._position_lock:
            if target_signed == self.current_position:
                return True
            steps = []
            cp = self.current_position
            if cp != 0 and target_signed != 0 and (cp > 0) != (target_signed > 0):
                steps = [(-cp, POSITION_CLOSE), (target_signed, POSITION_OPEN)]
            elif target_signed == 0:
                steps = [(-cp, POSITION_CLOSE)]
            elif cp == 0:
                steps = [(target_signed, POSITION_OPEN)]
            else:
                d = target_signed - cp
                steps = [(d, POSITION_OPEN if abs(target_signed) > abs(cp) else POSITION_CLOSE)]
            otype = ORDER_TYPE_MARKET if is_force_flatten else ORDER_TYPE_LIMIT
            for qty_signed, pe in steps:
                if qty_signed == 0:
                    continue
                order = {
                    "Symbol1": SYMBOL,
                    "Price": "0" if is_force_flatten else f"{ref_price:.1f}",
                    "TimeInForce": TIME_IN_FORCE_ROD,
                    "Side1": SIDE_BUY if qty_signed > 0 else SIDE_SELL,
                    "OrderType": otype,
                    "OrderQty": str(abs(qty_signed)),
                    "DayTrade": DAY_TRADE_OFF, "Symbol2": "", "Side2": "",
                    "PositionEffect": pe,
                }
                if self.dry_run:
                    self.log.info(f"DRY-RUN {'MKT-flat' if is_force_flatten else 'LIMIT'}: "
                                  f"{json.dumps(order, ensure_ascii=False)}")
                else:
                    resp = self.NEWORDER(order)  # type: ignore
                    self.log.info(f"NEWORDER -> {resp}")
                    if isinstance(resp, dict) and resp.get("Code") != "0000":
                        self.log.error(f"order REJECTED ({resp}); position stays "
                                       f"{self.current_position:+d}. VERIFY in the bridge app.")
                        return False
            self.current_position = target_signed
            self._persist_position()
            return True

    def check_session_cutoff(self):
        """Wall-clock force-flatten at 13:30 exchange-local (MARKET).
        Skipped entirely in HOLD_OVERNIGHT mode — positions carry across days."""
        if HOLD_OVERNIGHT:
            return
        with self._position_lock:
            if self.current_position == 0:
                return
            now = datetime.now(tz=EXCHANGE_TZ).replace(tzinfo=None)
            if not ((now.hour, now.minute) >= FLAT_BY and (now.hour, now.minute) <= SESSION_CLOSE):
                return
            if self.last_tick_price is None:
                self.log.error(f"⚠️ FORCE-FLATTEN needed ({self.current_position:+d} {SYMBOL}) "
                               f"but no ticks seen — NOT sending. Close manually in the bridge app.")
                return
            self.log.info(f"{now.time()} >= 13:30 — force-flattening "
                          f"{self.current_position:+d} @ MARKET (ref={self.last_tick_price:.1f})")
            self._execute_diff(0, self.last_tick_price, is_force_flatten=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--equity", type=float, default=2_000_000.0)
    p.add_argument("--mode", choices=["gap", "trend_hold"], default="trend_hold",
                   help="trend_hold (default)=the validated strategy: leveraged long >EMA50, "
                        "held overnight (consistent with HOLD_OVERNIGHT=True). "
                        "gap=intraday legacy edge (rejected in OOS — opt in explicitly; "
                        "note it assumes a 13:30 flatten, so set HOLD_OVERNIGHT=False if used). "
                        "For the aggressive sizing add --sizing-mode max_margin.")
    p.add_argument("--trend-n", type=int, default=50)
    p.add_argument("--sizing-mode", choices=["vol_target", "max_margin"],
                   default="vol_target",
                   help="vol_target=risk-calibrated; max_margin=high leverage (deploy "
                        "--margin-fraction of equity, directional)")
    p.add_argument("--vol-target", type=float, default=0.20,
                   help="sizing dial for vol_target mode")
    p.add_argument("--margin-fraction", type=float, default=0.80,
                   help="fraction of equity to deploy as margin in max_margin mode")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    log = _setup_logger()
    effective_dry = args.dry_run or DRY_RUN
    if not effective_dry:
        log.warning("LIVE MODE — orders WILL be sent.")
    cfg = IndexGapConfig(mode=args.mode, trend_n=args.trend_n,
                         contract_multiplier=CONTRACT_MULTIPLIER,
                         sizing_mode=args.sizing_mode, vol_target_annual=args.vol_target,
                         margin_fraction=args.margin_fraction)
    log.info(f"index bot | symbol={SYMBOL} port={BRIDGE_PORT} dry_run={effective_dry} "
             f"mode={cfg.mode} hold_overnight={HOLD_OVERNIGHT} sizing={cfg.sizing_mode} "
             f"vol_target={cfg.vol_target_annual} margin_frac={cfg.margin_fraction}")
    bot = IndexLive(BRIDGE_HOST, BRIDGE_PORT, log, args.equity, effective_dry, cfg)
    log.info(f"waiting for ticks on {SYMBOL}…")
    try:
        while True:
            time.sleep(30)
            bot.check_session_cutoff()
    except KeyboardInterrupt:
        log.info("shutdown"); os._exit(0)


if __name__ == "__main__":
    main()
