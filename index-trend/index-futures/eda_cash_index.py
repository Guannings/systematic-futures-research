"""
Exploratory analysis of the cash index day-session behavior, to decide which
strategy families are worth putting in the index variant bracket.

Answers two questions that determine the whole design:
  1. Do overnight GAPS continue or fade?
  2. Is the intraday session MOMENTUM or MEAN-REVERTING?

Uses cash_index_1h.csv (day-session hourly bars, exchange-local time).
"""
from __future__ import annotations
import numpy as np
import pandas as pd

df = pd.read_csv("cash_index_1h.csv", parse_dates=["datetime"])
df["date"] = df["datetime"].dt.date
df["hr"] = df["datetime"].dt.hour

# Per-session OHLC from hourly bars
g = df.groupby("date")
sess = pd.DataFrame({
    "open":  g["open"].first(),
    "high":  g["high"].max(),
    "low":   g["low"].min(),
    "close": g["close"].last(),
    "n":     g.size(),
}).reset_index()
sess = sess[sess["n"] >= 4].reset_index(drop=True)   # full sessions only

sess["prev_close"] = sess["close"].shift(1)
sess = sess.dropna().reset_index(drop=True)

# Returns (in %)
sess["gap"]       = (sess["open"] / sess["prev_close"] - 1) * 100      # overnight gap
sess["intraday"]  = (sess["close"] / sess["open"] - 1) * 100           # open->close
sess["range_pct"] = (sess["high"] - sess["low"]) / sess["open"] * 100

# First-hour move vs rest-of-day, from hourly bars
h0 = df[df["hr"] == 9].set_index("date")["open"]
h1 = df[df["hr"] == 10].set_index("date")["close"]
hL = df[df["hr"] == 13].set_index("date")["close"]
fh = pd.DataFrame({"o9": h0, "c10": h1, "c13": hL}).dropna()
fh["first_hr"] = (fh["c10"] / fh["o9"] - 1) * 100
fh["rest"]     = (fh["c13"] / fh["c10"] - 1) * 100

N = len(sess)
print(f"sessions analyzed: {N}   ({sess['date'].iloc[0]} -> {sess['date'].iloc[-1]})")
print(f"avg |gap|     = {sess['gap'].abs().mean():.3f}%   "
      f"avg |intraday| = {sess['intraday'].abs().mean():.3f}%   "
      f"avg range = {sess['range_pct'].mean():.3f}%")
print()

# --- Q1: gap continuation vs fade ---
b = np.polyfit(sess["gap"], sess["intraday"], 1)
cont = sess[sess["gap"].abs() > 0.1]
same = (np.sign(cont["gap"]) == np.sign(cont["intraday"])).mean()
print("Q1  GAP -> intraday(open->close)")
print(f"    slope (intraday per 1% gap) = {b[0]:+.3f}   "
      f"corr = {np.corrcoef(sess['gap'], sess['intraday'])[0,1]:+.3f}")
print(f"    after a >0.1% gap, intraday continues same direction: {same*100:.1f}% of days")
print(f"    => {'CONTINUATION' if b[0] > 0.05 else 'FADE' if b[0] < -0.05 else 'NEUTRAL'} bias")
print()

# --- Q2: intraday momentum vs mean-reversion (first hour -> rest) ---
b2 = np.polyfit(fh["first_hr"], fh["rest"], 1)
same2 = (np.sign(fh["first_hr"]) == np.sign(fh["rest"])).mean()
print("Q2  FIRST-HOUR(09->10) -> REST-OF-DAY(10->13)")
print(f"    slope (rest per 1% first-hr) = {b2[0]:+.3f}   "
      f"corr = {np.corrcoef(fh['first_hr'], fh['rest'])[0,1]:+.3f}")
print(f"    rest-of-day continues first-hour direction: {same2*100:.1f}% of days")
print(f"    => {'MOMENTUM' if b2[0] > 0.05 else 'MEAN-REVERSION' if b2[0] < -0.05 else 'NEUTRAL'} bias")
print()

# --- crude edge sketches (no costs) ---
print("Crude directional edges (no costs, 1 unit, % per session):")
# Gap continuation: take open in gap direction, hold to close
ret_gapcont = np.sign(sess["gap"]) * sess["intraday"]
# Gap fade: fade the gap intraday
ret_gapfade = -np.sign(sess["gap"]) * sess["intraday"]
# First-hour momentum: trade rest-of-day in first-hour direction
ret_fhmom = np.sign(fh["first_hr"]) * fh["rest"]
for name, r in [("gap-continuation", ret_gapcont),
                ("gap-fade", ret_gapfade),
                ("first-hr momentum", ret_fhmom)]:
    r = r.dropna()
    sharpe = r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else float("nan")
    print(f"    {name:<20} mean/day={r.mean():+.4f}%  hit={ (r>0).mean()*100:4.1f}%  "
          f"ann.Sharpe(gross)={sharpe:+.2f}")
