"""
Supervisor for gdvt_live.py.

Wraps the live script and restarts it under three failure modes:
  1. Process exits unexpectedly (crash / bridge drop / unhandled exception).
  2. Process is running but the log file hasn't been updated in 10 minutes
     during a trading session (= silent subscription drop).
  3. Daily restart at 08:30 exchange-local (10 min before session open) to
     clear any stale state from overnight idle.

Run:
    py -3.13 gdvt_supervisor.py

Stop:
    Ctrl+C in this terminal (sends SIGINT to wrapper + child).

Tunables in CONFIG below.
"""

from __future__ import annotations
import os
import subprocess
import sys
import time
from datetime import datetime, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo


# ---- CONFIG -----------------------------------------------------------------
EXCHANGE_TZ = ZoneInfo("UTC")          # exchange-local timezone (placeholder).
                                       # all session/restart logic uses exchange-local
                                       # time regardless of the PC's clock setting

SCRIPT = "gdvt_live.py"
SCRIPT_ARGS = ["--equity", "31250"]    # LIVE MODE. Fix now active: POSITION_CLOSE = "".
                                       # max_lots=1 still capping week-1 trades to 1 lot.
PYTHON = "py"
PYTHON_VER_FLAG = "-3.13"
LOG_DIR = Path("logs")
WRAPPER_LOG = LOG_DIR / "wrapper.log"

SESSION_OPEN  = dt_time(8, 45)         # exchange-local time (script's local clock)
SESSION_CLOSE = dt_time(13, 45)        # aligned with gdvt_live.py:SESSION_CLOSE
                                       # so silent-fail timer keeps watching
                                       # through the 13:30→13:45 force-flatten
                                       # window. A 13:30 close would silently
                                       # drop supervision during the most
                                       # critical 15 min of the trading day
                                       # (when force-flatten is running and
                                       # must complete cleanly).
DAILY_RESTART = dt_time(8, 30)         # 15 min before session open

SILENT_FAIL_SECS    = 7800             # 130 min — for 1h gold bars, the first session bar
                                       # (09:00 timestamp) only closes when the first tick
                                       # arrives AFTER 10:00 exchange-local. Sparse gold ticks
                                       # mean this could be 10:30+ = ~2h after 08:30 start.
TICK_ALERT_MIN_AFTER_OPEN = 15         # if 15 min past session open and no heartbeat/bar
                                       # line in the latest log, loudly warn the user that
                                       # the bridge subscription is probably missing
MAX_RESTARTS_PER_HR = 6                # circuit-breaker for runaway crashes
BACKOFFS_SECS       = [10, 30, 60, 120, 300, 600]  # progressive wait between restarts
HEALTHY_MIN_SECS    = 1800             # 30 min uptime resets the backoff counter
POLL_INTERVAL       = 15               # seconds between health checks
# -----------------------------------------------------------------------------


def now_local() -> datetime:
    """Always returns exchange-local wall-clock time, naive (tzinfo stripped) so
    it composes with the dt_time SESSION_OPEN/CLOSE/DAILY_RESTART constants.
    Works regardless of the PC's timezone setting."""
    return datetime.now(tz=EXCHANGE_TZ).replace(tzinfo=None)


def in_session(t: dt_time | None = None) -> bool:
    now = now_local()
    if now.weekday() >= 5:   # 5=Sat, 6=Sun — exchange closed
        return False
    t = t or now.time()
    return SESSION_OPEN <= t <= SESSION_CLOSE


def latest_log_mtime() -> float | None:
    """mtime of the newest gdvt_*.log file, or None if none yet."""
    LOG_DIR.mkdir(exist_ok=True)
    logs = sorted(LOG_DIR.glob("gdvt_*.log"), key=os.path.getmtime, reverse=True)
    return os.path.getmtime(logs[0]) if logs else None


def latest_log_has_tick_activity() -> bool:
    """True if the newest gdvt_*.log contains any heartbeat or bar line.
    Used to detect 'bridge subscription missing' (script is alive but no ticks)."""
    LOG_DIR.mkdir(exist_ok=True)
    logs = sorted(LOG_DIR.glob("gdvt_*.log"), key=os.path.getmtime, reverse=True)
    if not logs:
        return False
    try:
        with open(logs[0], "r", encoding="utf-8") as f:
            content = f.read()
        return ("heartbeat: tick" in content) or ("| bar " in content)
    except Exception:
        return False


def minutes_into_session() -> int:
    """How many minutes past session open (08:45 exchange-local) we are right now.
    Returns negative if before open. Uses now_local() (exchange-local time)."""
    t = now_local().time()
    return (t.hour - SESSION_OPEN.hour) * 60 + (t.minute - SESSION_OPEN.minute)


def wrap_log(msg: str) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    ts = now_local().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} | supervisor | {msg}"
    print(line, flush=True)
    with open(WRAPPER_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def spawn_child() -> subprocess.Popen:
    cmd = [PYTHON, PYTHON_VER_FLAG, SCRIPT] + SCRIPT_ARGS
    wrap_log(f"spawning: {' '.join(cmd)}")
    return subprocess.Popen(cmd)  # inherit stdio so live output streams to wrapper terminal


def is_daily_restart_window(last_restart: float) -> bool:
    """Trigger one restart per day at DAILY_RESTART, but only if we haven't
    restarted in the last hour (avoids double-fire if process was just spawned)."""
    t = now_local().time()
    in_window = (t.hour, t.minute) == (DAILY_RESTART.hour, DAILY_RESTART.minute)
    return in_window and (time.time() - last_restart) > 3600


def main():
    wrap_log("supervisor started")
    wrap_log(f"  script        = {SCRIPT} {' '.join(SCRIPT_ARGS)}")
    wrap_log(f"  silent-fail   = {SILENT_FAIL_SECS}s during session "
             f"({SESSION_OPEN.strftime('%H:%M')}-{SESSION_CLOSE.strftime('%H:%M')})")
    wrap_log(f"  max restarts  = {MAX_RESTARTS_PER_HR}/hr, backoffs={BACKOFFS_SECS}")
    wrap_log(f"  daily restart = {DAILY_RESTART.strftime('%H:%M')}")

    restart_history: list[float] = []
    backoff_idx = 0

    while True:
        # circuit breaker for runaway crashes
        now = time.time()
        restart_history = [t for t in restart_history if now - t < 3600]
        if len(restart_history) >= MAX_RESTARTS_PER_HR:
            wrap_log(f"⚠️  {len(restart_history)} restarts in last hour — "
                     f"sleeping 1 hour to break the loop")
            time.sleep(3600)
            restart_history = []
            backoff_idx = 0
            continue

        # spawn
        proc = spawn_child()
        spawn_time = time.time()
        restart_history.append(spawn_time)
        last_log_mtime = latest_log_mtime() or spawn_time
        last_log_change = spawn_time
        tick_alert_fired = False   # one-shot alert per child run

        # supervise this run
        exit_reason = None
        while True:
            time.sleep(POLL_INTERVAL)

            # 1. did it crash?
            ret = proc.poll()
            if ret is not None:
                exit_reason = f"process exited (code={ret})"
                break

            # NO-TICKS early-warning. Catches the case where the bridge lost the
            # front-month subscription (silent failure mode that otherwise wouldn't
            # trip silent_fail_secs for 130+ min). Fires exactly once per
            # child run, ONLY after BOTH the child has been running ≥ 15 min
            # AND the session has been open for ≥ 15 min.
            #
            # Why both conditions: daily restart spawns the child at 08:30
            # exchange-local. Session opens at 08:45. With only the child-uptime
            # gate, at 08:45 the child has been up 15 min AND in-session is
            # true AND no ticks have arrived yet (session just opened) — so
            # the alert fires immediately at session open every single day.
            # That's a false positive (the system hasn't actually failed; it's
            # 0 seconds into the session). The previous "uptime only" fix was
            # itself a fix for the inverse bug (mid-session relaunches firing
            # instantly because session was already old). Requiring BOTH gates
            # is what we actually want: we need 15 min of in-session uptime
            # before we can conclude "no ticks = bridge dormant."
            child_uptime_min = (time.time() - spawn_time) / 60.0
            session_age_min = minutes_into_session()
            if (in_session() and not tick_alert_fired
                    and child_uptime_min >= TICK_ALERT_MIN_AFTER_OPEN
                    and session_age_min >= TICK_ALERT_MIN_AFTER_OPEN
                    and not latest_log_has_tick_activity()):
                wrap_log("⚠️  NO TICKS — child up " +
                         f"{child_uptime_min:.0f} min, session open " +
                         f"{session_age_min} min, still zero heartbeat/bar "
                         "lines in the log. Likely the bridge subscription is dormant. "
                         "ACTION: open the bridge app → quote-subscription settings → "
                         "DELETE the front-month row → add subscription → re-add the "
                         "front month → wait 60s and verify prices tick → relaunch supervisor.")
                tick_alert_fired = True

            # 2. silent failure during session?
            if in_session():
                cur_mtime = latest_log_mtime()
                if cur_mtime and cur_mtime > last_log_mtime:
                    last_log_mtime = cur_mtime
                    last_log_change = time.time()
                if time.time() - last_log_change > SILENT_FAIL_SECS:
                    exit_reason = (f"no log activity for {SILENT_FAIL_SECS}s during session "
                                   f"(silent subscription drop?)")
                    proc.terminate()
                    try:
                        proc.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        wrap_log("child didn't exit in 15s, killing")
                        proc.kill()
                    break
            else:
                # outside session: silence is normal, don't restart, just keep polling
                last_log_change = time.time()  # reset so we don't fire when session opens

            # 3. daily restart window
            if is_daily_restart_window(spawn_time):
                exit_reason = f"daily restart at {DAILY_RESTART.strftime('%H:%M')}"
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break

        # log + decide backoff
        uptime = time.time() - spawn_time
        wrap_log(f"child stopped after {uptime:.0f}s — reason: {exit_reason}")
        if uptime > HEALTHY_MIN_SECS:
            backoff_idx = 0  # was healthy, reset
        wait = BACKOFFS_SECS[min(backoff_idx, len(BACKOFFS_SECS) - 1)]
        wrap_log(f"backoff {wait}s before next restart "
                 f"(restart #{len(restart_history)+1} this hour)")
        time.sleep(wait)
        backoff_idx += 1


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        wrap_log("Ctrl+C received — supervisor exiting")
        sys.exit(0)
