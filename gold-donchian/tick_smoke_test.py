"""
Smoke test for the bridge's tick flow.

Subscribes to several symbols and prints raw ticks for 2 minutes. Tells us
which symbols the bridge actually sends data for vs which are silent.

If the index-futures contract (definitely active) ticks come through but the
gold contract doesn't, that proves the issue is gold subscription specifically
— not your code, not the symbol format.

CAVEAT: this smoke test counts ALL incoming ticks, including the bridge's
quote-board placeholder pings (Price="-" / TickTime="-"). On the gold contract
~100% of the "ticks" are these placeholders; real trade ticks are a tiny
minority. A high tick count here proves the ZMQ bridge is alive — it does
NOT prove gold is actually trading. Use the gdvt_live.py log's
`heartbeat: tick` lines for that signal instead.

Run during exchange-local market hours (08:45-13:45 day session for futures):
    py -3.13 tick_smoke_test.py
"""

from __future__ import annotations
import sys
import time
from collections import Counter
from pathlib import Path

BRIDGE_SAMPLE = Path(__file__).resolve().parent / "BrokerBridge" / "Sample" / "Python"
if BRIDGE_SAMPLE.exists():
    sys.path.insert(0, str(BRIDGE_SAMPLE))

from BridgeHelp import BridgeModule  # type: ignore


# Symbols to test — mix of products to isolate which ones the bridge has data for.
# The exchange uses its own contract-month coding scheme; specifics omitted.
TEST_SYMBOLS = [
    "IDXc1",     # index-futures front month (confirmed working)
    "IDXc2",     # index-futures near-month
    "FRONTc1",   # gold front month (your strategy target)
    "FRONTc2",   # gold next-near
    "ALTc1",     # local-currency gold contract, front month
]
TEST_DURATION_SEC = 120   # 2 minutes


class TickProbe(BridgeModule):
    def __init__(self, host, port):
        super().__init__(host, port)
        self.tick_counts: Counter = Counter()
        self.first_seen: dict = {}
        self.last_price: dict = {}

    def SHOWQUOTEDATA(self, obj):  # noqa: N802
        sym = obj.get("Symbol", "?")
        self.tick_counts[sym] += 1
        if sym not in self.first_seen:
            self.first_seen[sym] = time.time()
            print(f"  [first tick]   {sym}  price={obj.get('Price')}  "
                  f"bid={obj.get('BidPs')}  ask={obj.get('AskPs')}")
        self.last_price[sym] = obj.get("Price")


def main():
    print("Connecting to the bridge at localhost:8080 ZMQ port 9000...")
    probe = TickProbe("http://localhost:8080", 9000)
    print(f"Subscribing to {len(TEST_SYMBOLS)} symbols:")
    for sym in TEST_SYMBOLS:
        print(f"  -> {sym}")
        probe.QUOTEDATA(sym)  # type: ignore
    print(f"\nWatching for ticks for {TEST_DURATION_SEC} seconds...\n")

    start = time.time()
    while time.time() - start < TEST_DURATION_SEC:
        time.sleep(5)
        elapsed = int(time.time() - start)
        print(f"  [{elapsed:3d}s] tick counts so far: {dict(probe.tick_counts)}")

    print("\n" + "=" * 60)
    print("RESULTS:")
    print("=" * 60)
    for sym in TEST_SYMBOLS:
        n = probe.tick_counts.get(sym, 0)
        last = probe.last_price.get(sym, "—")
        status = "✓ ALIVE" if n > 0 else "✗ SILENT"
        print(f"  {sym:8s}  {status:10s}  ticks={n:5d}  last_price={last}")

    silent = [s for s in TEST_SYMBOLS if probe.tick_counts.get(s, 0) == 0]
    alive = [s for s in TEST_SYMBOLS if probe.tick_counts.get(s, 0) > 0]
    print()
    if not alive:
        print("CONCLUSION: the bridge sent NO ticks for any symbol. Either market is closed,")
        print("            the bridge is not connected to live data, or no symbols subscribed in the bridge UI.")
    elif not silent:
        print("CONCLUSION: All symbols received ticks. ZMQ bridge is alive — relaunch")
        print("            supervisor. Note: tick counts here include placeholder pings")
        print("            (Price='-'); real-trade rate only visible in gdvt_live.py log's")
        print("            `heartbeat: tick` lines.")
    else:
        print(f"CONCLUSION: the bridge has data for {alive} but NOT for {silent}.")
        print(f"            This means {silent} need to be subscribed in the bridge's UI before")
        print(f"            our script can receive their ticks.")
    import os
    os._exit(0)  # bypass the bridge's non-daemon threads


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\ninterrupted")
        import os
        os._exit(0)
