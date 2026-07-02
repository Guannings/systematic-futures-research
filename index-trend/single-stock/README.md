# Single-stock gap study — rejected

A short study testing whether a single large-cap name's overnight gap is tradable
in the day session. It was **rejected on its merits** and is kept as part of the
honest record, not as a deployable strategy.

## The hypothesis

Single-stock futures on this venue have no night session, so — unlike the index
future — the overnight gap is fully intact and capturable in the day session. The
chosen name is a large-cap with a liquid overseas ADR that trades overnight (the
driver of the gap). If the gap over-reacts to the ADR move and mean-reverts intraday,
a day-session fade on the single-stock future would be tradable and free of the
cash-vs-futures overnight blind spot the index study had.

## The result — rejected

Two things killed it:

1. **The gap is efficiently priced.** The open gap does not systematically continue
   or reliably over-react; there is no robust directional edge left for the day
   session once the ADR move is public.
2. **The tick cost is larger than any residual edge.** On a high-priced name, one
   tick is a meaningful percentage; a fade that clears that hurdle net simply is not
   there. The same cost wall that dominates the rest of the repo applies here too.

Fade strategies also carry negative skew — a genuine trend day (earnings, macro)
hands a large loss — so `stock_fade_backtest.py` deliberately reports the worst
single-day loss and tests ATR stops, not just Sharpe. Even with stops, the study did
not clear costs, and it was dropped.

## Reproducing

The published copy omits the specific tickers (set `STOCK_TICKER` / `ADR_TICKER` in
the scripts to your own symbols). `stock_1h.csv` / `stock_daily.csv` are the cached
cash-stock bars used for the backtest.

```bash
python fetch_and_eda.py          # gap-continuation + ADR-linkage exploratory analysis
python stock_fade_backtest.py    # fade variant bracket with stops, worst-day, skew
```

## Files

```
fetch_and_eda.py         Fetch + exploratory analysis (gap continuation, ADR linkage).
stock_fade_backtest.py   Fade variant bracket — stops, worst-day loss, skew reporting.
stock_1h.csv             Cached hourly cash-stock bars.
stock_daily.csv          Cached daily cash-stock bars.
```
