# Index Trend-Hold Bot — Runbook

Automated day-session bot for an exchange-listed equity-index future, built on the
same broker bridge as the gold GDVT system. Trades the **only edge that survived
honest testing**: a trend-hold carried overnight (with a legacy trend-aligned
gap-continuation mode kept for reference).

## TWO MODES — read this first
- **`gap` mode (legacy):** intraday gap-momentum, flat by 13:30. Tested honestly:
  **net break-even after costs** (Sharpe ~0.1). Forced daily round-trips × the
  venue's fat ticks eat the edge. Kept for reference; NOT recommended.
- **`trend_hold` mode (THE EARNER):** leveraged long the index while it's above
  EMA50, **held overnight** (the venue permits overnight holds). Validated on real
  day-session index futures 2019-2026, lookahead-free, net of costs:
  **Sharpe 1.25, OOS 1.66, maxDD −20.5% (1×); +56.5%/yr at 3× leverage**
  (reproduce with `idx_trend_backtest.py`). The trend
  filter sidesteps bear markets (went flat in 2022). This is mostly leveraged
  bull-beta with trend protection — honest, robust, and the right tool for
  "maximize return" in a persistent index bull.

The keeper *alpha* (market-neutral, not in this bot): cross-sectional 20d momentum
long/short over the stock-future basket, net Sharpe ~1.2 — see `../xsec/`.

## Recommended high-leverage config
In `idx_live.py` set `HOLD_OVERNIGHT = True`, then run:
```
python index-trend/index-futures/idx_live.py --mode trend_hold --sizing-mode max_margin \
    --margin-fraction 0.50      # 0.50≈3.5x leverage, 0.80≈6.9x. Higher = bigger bet.
```
It goes long the index when >EMA50 and HOLDS; flattens only when the trend breaks.
`margin_rate` (in `idx_strategy.py`) scales margin with the index level — VERIFY
the exact current index-futures margin on the exchange and set it if needed.

## Files
| File | Role |
|---|---|
| `idx_strategy.py` | Pure logic — the one-shot open decision. Testable, no I/O. |
| `idx_live.py` | Live wrapper (broker bridge). Decides at the open; overnight hold or 13:30 flatten. |
| `idx_supervisor.py` | Watchdog: restarts on crash / silent drop / daily 08:30. |
| `idx_trend_backtest.py` | IS/OOS backtest of the SELECTED trend-hold strategy. |
| `idx_futures_backtest.py` | IS/OOS backtest of the (rejected) gap variants. |
| `fetch_futures.py` | Refreshes `index_fut_daily.csv` (daily history). |
| `index_fut_daily.csv` | Daily closes → EMA20 trend + prev_close. Keep current. |

## The strategy, precisely
At the session open (08:45), once per day:
1. `trend = sign(EMA20[yesterday] − EMA20[day-before])`  ← only closes up to
   yesterday (no lookahead).
2. `gap = open / prev_close − 1`.
3. If `|gap| ≥ gap_threshold` **and** `sign(gap) == trend` → enter `sign(gap)`,
   vol-targeted size. Else stay flat.
4. In gap mode, force-flatten at 13:30 (wall-clock, MARKET) — no overnight
   exposure. In trend_hold mode, carry overnight and exit on the trend break.

## Before each session
1. **Refresh daily data** so trend & prev_close use yesterday's real close:
   `python index-trend/index-futures/fetch_futures.py`
2. **Verify the active near-month** `SYMBOL` in `idx_live.py`. The exchange uses
   its own contract-month coding scheme (specifics omitted); the front contract
   rolls on a fixed monthly schedule. The full-size index future tracks the same
   index, so its daily history is the trend proxy.
3. Set `BRIDGE_PORT` to your bridge port.
4. In the bridge app, subscribe the index symbol in the quote board (the bridge
   won't forward ticks otherwise — see ../docs/BROKER_BRIDGE_NOTES.md #2).

## Go-live sequence
```
# 1. backtest sanity — the selected strategy, then the rejected gap variants
python index-trend/index-futures/idx_trend_backtest.py
python index-trend/index-futures/idx_futures_backtest.py 2000000

# 2. dry-run during a live session (logs orders, sends nothing)
python index-trend/index-futures/idx_live.py --dry-run --vol-target 0.20

# 3. when the dry-run logs a clean SESSION OPEN decision + force-flatten,
#    set DRY_RUN=False in idx_live.py and run under the supervisor:
py -3.13 index-trend/index-futures/idx_supervisor.py
```

## The sizing dial (`--vol-target`)
- `0.20` ≈ conservative (gold-style). Sizes ~1-2 lots on a 2M account.
- Risk scales ~linearly until the margin cap binds. Margin cap = 50% of equity.
  To push toward that, raise `--vol-target`, or raise `max_margin_pct` in the config.
- **Higher = bigger swings up AND down.** It's a simulation, so the downside is
  rank, not real money — size to the target you want.

## Safety / known gotchas (inherited from gold)
- `DRY_RUN=True` by default — flip to False to trade (published copy stays disabled).
- Position persisted to `logs/idx_position_state.json`; on restart it warns if a
  carried position exists — verify in the bridge app's open-positions tab.
- Force-flatten uses MARKET and refuses to fire if no ticks seen (won't send a
  blind order).
- Placeholder ("-") and stale ticks are filtered.
