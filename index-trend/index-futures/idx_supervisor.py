"""
Supervisor for idx_live.py — same watchdog as gdvt_supervisor.py.

Restarts the live bot on: (1) crash, (2) silent subscription drop during
session, (3) daily 08:30 restart to clear overnight state.

the index future is far more liquid than gold, so the silent-fail timer is much tighter
(30 min vs gold's 130) — a healthy index-future session never goes 30 min without ticks.

Run from the repo root:  py -3.13 index-futures/idx_supervisor.py
Stop: Ctrl+C.
"""
from __future__ import annotations
import os, subprocess, sys, time
from datetime import datetime, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

EXCHANGE_TZ = ZoneInfo("UTC")
SCRIPT = "index-futures/idx_live.py"
# index trend_hold, held overnight. margin-fraction tuned so target = 2 lots, to
# HOLD an existing 2-lot position — bot adopts the real
# +2 on startup, target is also 2, so it just holds (no trim). Trend break still
# flattens all 2. Robust to the index ~45k-50k. Raise only if you free up margin.
SCRIPT_ARGS = ["--equity", "2000000", "--mode", "trend_hold",
               "--sizing-mode", "max_margin", "--margin-fraction", "0.30"]
REFRESH_SCRIPT = "index-futures/fetch_futures.py"  # run before each session to refresh daily data
PYTHON = "py"; PYTHON_VER_FLAG = "-3.13"
LOG_DIR = Path("logs"); WRAPPER_LOG = LOG_DIR / "idx_wrapper.log"

SESSION_OPEN  = dt_time(8, 45)
SESSION_CLOSE = dt_time(13, 45)
DAILY_RESTART = dt_time(8, 30)
SILENT_FAIL_SECS = 1800          # 30 min — the index future is liquid; longer = real drop
TICK_ALERT_MIN_AFTER_OPEN = 10
MAX_RESTARTS_PER_HR = 6
BACKOFFS_SECS = [10, 30, 60, 120, 300, 600]
HEALTHY_MIN_SECS = 1800
POLL_INTERVAL = 15


def now_local() -> datetime:
    return datetime.now(tz=EXCHANGE_TZ).replace(tzinfo=None)


def in_session(t=None) -> bool:
    now = now_local()
    if now.weekday() >= 5:
        return False
    t = t or now.time()
    return SESSION_OPEN <= t <= SESSION_CLOSE


def latest_log_mtime():
    LOG_DIR.mkdir(exist_ok=True)
    logs = sorted(LOG_DIR.glob("idx_2*.log"), key=os.path.getmtime, reverse=True)
    return os.path.getmtime(logs[0]) if logs else None


def latest_log_has_tick_activity() -> bool:
    LOG_DIR.mkdir(exist_ok=True)
    logs = sorted(LOG_DIR.glob("idx_2*.log"), key=os.path.getmtime, reverse=True)
    if not logs:
        return False
    try:
        c = open(logs[0], encoding="utf-8").read()
        return ("heartbeat:" in c) or ("SESSION OPEN" in c)
    except Exception:
        return False


def minutes_into_session() -> int:
    t = now_local().time()
    return (t.hour - SESSION_OPEN.hour) * 60 + (t.minute - SESSION_OPEN.minute)


def wrap_log(msg: str):
    LOG_DIR.mkdir(exist_ok=True)
    line = f"{now_local().strftime('%Y-%m-%d %H:%M:%S')} | idx-supervisor | {msg}"
    print(line, flush=True)
    open(WRAPPER_LOG, "a", encoding="utf-8").write(line + "\n")


def spawn_child():
    cmd = [PYTHON, PYTHON_VER_FLAG] + SCRIPT.split() + SCRIPT_ARGS
    wrap_log(f"spawning: {' '.join(cmd)}")
    return subprocess.Popen(cmd)


def is_daily_restart_window(last_restart: float) -> bool:
    t = now_local().time()
    return (t.hour, t.minute) == (DAILY_RESTART.hour, DAILY_RESTART.minute) and \
           (time.time() - last_restart) > 3600


def main():
    wrap_log("index supervisor started")
    wrap_log(f"  script={SCRIPT} {' '.join(SCRIPT_ARGS)}  silent_fail={SILENT_FAIL_SECS}s")
    restart_history = []; backoff_idx = 0
    while True:
        now = time.time()
        restart_history = [t for t in restart_history if now - t < 3600]
        if len(restart_history) >= MAX_RESTARTS_PER_HR:
            wrap_log(f"⚠️ {len(restart_history)} restarts/hr — sleeping 1h"); time.sleep(3600)
            restart_history = []; backoff_idx = 0; continue
        proc = spawn_child(); spawn_time = time.time(); restart_history.append(spawn_time)
        last_log_mtime = latest_log_mtime() or spawn_time; last_log_change = spawn_time
        tick_alert_fired = False; exit_reason = None
        while True:
            time.sleep(POLL_INTERVAL)
            if proc.poll() is not None:
                exit_reason = f"process exited (code={proc.returncode})"; break
            up_min = (time.time() - spawn_time) / 60.0
            if (in_session() and not tick_alert_fired and up_min >= TICK_ALERT_MIN_AFTER_OPEN
                    and minutes_into_session() >= TICK_ALERT_MIN_AFTER_OPEN
                    and not latest_log_has_tick_activity()):
                wrap_log(f"⚠️ NO TICKS — up {up_min:.0f}min, session open "
                         f"{minutes_into_session()}min, no heartbeat/open lines. "
                         f"the bridge subscription likely dormant: in the bridge app delete the "
                         f"index row and re-add it, wait 60s, relaunch.")
                tick_alert_fired = True
            if in_session():
                cur = latest_log_mtime()
                if cur and cur > last_log_mtime:
                    last_log_mtime = cur; last_log_change = time.time()
                if time.time() - last_log_change > SILENT_FAIL_SECS:
                    exit_reason = f"no log activity {SILENT_FAIL_SECS}s during session"
                    proc.terminate()
                    try: proc.wait(timeout=15)
                    except subprocess.TimeoutExpired: proc.kill()
                    break
            else:
                last_log_change = time.time()
            if is_daily_restart_window(spawn_time):
                exit_reason = "daily 08:30 restart"; proc.terminate()
                try: proc.wait(timeout=15)
                except subprocess.TimeoutExpired: proc.kill()
                break
        uptime = time.time() - spawn_time
        wrap_log(f"child stopped after {uptime:.0f}s — {exit_reason}")
        if uptime > HEALTHY_MIN_SECS:
            backoff_idx = 0
        wait = BACKOFFS_SECS[min(backoff_idx, len(BACKOFFS_SECS) - 1)]
        wrap_log(f"backoff {wait}s"); time.sleep(wait); backoff_idx += 1


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        wrap_log("Ctrl+C — exiting"); sys.exit(0)
