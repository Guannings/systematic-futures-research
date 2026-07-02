"""
Offline end-to-end check of the live bot WITHOUT the broker (market closed).
Mocks the broker bridge, feeds synthetic ticks, and verifies the full path:
  placeholder-tick filtering -> session-open decision -> order construction ->
  position update -> overnight-hold.
Does NOT touch the real position-state file. Symbol-agnostic (uses idx_live.SYMBOL).
"""
import sys, types
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

# --- mock the broker bridge so IndexLive can instantiate without the bridge app ---
SENT = []
class _StubBridge:
    def __init__(self, host, port): self.host, self.port = host, port
    def QUOTEDATA(self, sym): pass
    def NEWORDER(self, order): SENT.append(order); return {"Code": "0000"}
stub = types.ModuleType("BridgeHelp"); stub.BridgeModule = _StubBridge
sys.modules["BridgeHelp"] = stub

import idx_live
idx_live.BridgeModule = _StubBridge
idx_live.POSITION_STATE_FILE = "logs/test_pos_state.json"
# point daily warmup at the real CSV regardless of the caller's working directory
idx_live.DAILY_HISTORY_CSV = str(Path(__file__).resolve().parent / "index_fut_daily.csv")
SYM = idx_live.SYMBOL                        # whatever symbol the bot is set to
from idx_strategy import IndexGapConfig
import logging
log = logging.getLogger("test"); log.addHandler(logging.NullHandler())

def fresh(margin_fraction=0.50):
    SENT.clear()
    cfg = IndexGapConfig(mode="trend_hold", sizing_mode="max_margin",
                         margin_fraction=margin_fraction,
                         contract_multiplier=idx_live.CONTRACT_MULTIPLIER)
    return idx_live.IndexLive("h", 1, log, 2_000_000, dry_run=False, cfg=cfg)

PASS = []
def check(name, cond):
    PASS.append(cond); print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

print(f"Offline live-path test (mocked broker), SYMBOL={SYM}:\n")
try: Path("logs/test_pos_state.json").unlink(missing_ok=True)  # clear stale state from any prior crash
except (FileNotFoundError, PermissionError): pass
bot = fresh()

# 1. placeholder tick must be dropped, no decision
bot.SHOWQUOTEDATA({"Symbol": SYM, "Price": "-", "TickTime": "-", "Qty": "0"})
check("placeholder tick dropped (no order, flat)", len(SENT) == 0 and bot.current_position == 0)
check("invalid-drop counter incremented", bot._invalid_dropped >= 1)

# 2. first valid in-session tick -> trend_long -> BUY order
bot._maybe_decide_open(pd.Timestamp("2026-06-22 09:00:00"), 48300.0)
check("entered a position at the open", bot.current_position > 0)
check("exactly one order sent", len(SENT) == 1)
if SENT:
    o = SENT[0]
    check("order is BUY (Side1='1')", o.get("Side1") == "1")
    check(f"order symbol = {SYM}", o.get("Symbol1") == SYM)
    check("order qty matches position", str(abs(bot.current_position)) == o.get("OrderQty"))
    print(f"        -> order: {o.get('Side1')} {o.get('OrderQty')} {o.get('Symbol1')} "
          f"@ {o.get('Price')}  (position now {bot.current_position:+d})")

# 3. second tick same session -> idempotent
n_before = len(SENT)
bot._maybe_decide_open(pd.Timestamp("2026-06-22 10:00:00"), 48400.0)
check("no duplicate order later in the session", len(SENT) == n_before)

# 4. overnight hold: 13:30 cutoff must NOT flatten
idx_live.HOLD_OVERNIGHT = True
pos_before = bot.current_position
bot.last_tick_price = 48400.0
bot.check_session_cutoff()
check("position held overnight (not force-flattened)", bot.current_position == pos_before)

# 5. gap mode: 13:30 cutoff MUST flatten
idx_live.HOLD_OVERNIGHT = False
bot2 = fresh()
bot2.current_position = 2
bot2.last_tick_price = 48400.0
# force wall-clock into the flatten window by monkeypatching datetime is overkill;
# instead call _execute_diff(0,...) path indirectly via a permissive check:
import datetime as _dt
class _FakeNow:
    @staticmethod
    def now(tz=None):
        return _dt.datetime(2026, 6, 22, 13, 31)
_orig = idx_live.datetime
idx_live.datetime = _FakeNow  # type: ignore
try:
    bot2.check_session_cutoff()
finally:
    idx_live.datetime = _orig  # type: ignore
check("gap mode force-flattens at 13:30", bot2.current_position == 0)

try: Path("logs/test_pos_state.json").unlink()
except (FileNotFoundError, PermissionError): pass

print(f"\n{'ALL PASS' if all(PASS) else 'SOME FAILED'} ({sum(PASS)}/{len(PASS)})")
