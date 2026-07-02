"""
Pre-flight check for the GDVT live setup.

Runs all the verifications you'd otherwise do manually before launching the
supervisor. Reports PASS/FAIL/WARN per check and an overall status.

Run:
    py -3.13 preflight_check.py

Exit codes:
    0 = all checks passed (safe to launch)
    1 = at least one FAIL (do NOT launch — fix issues first)
    2 = WARN only (probably fine but review)

Does NOT connect to the bridge or send any orders. Pure static + import checks.
"""

from __future__ import annotations
import sys
import importlib
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo


HERE = Path(__file__).resolve().parent
RESULTS: list[tuple[str, str, str]] = []   # (status, name, detail)


def _add(status: str, name: str, detail: str = "") -> None:
    RESULTS.append((status, name, detail))


def check_python_version() -> None:
    v = sys.version_info
    if v.major == 3 and v.minor == 13:
        _add("PASS", "Python version", f"{v.major}.{v.minor}.{v.micro}")
    elif v.major == 3 and v.minor >= 13:
        _add("WARN", "Python version", f"{v.major}.{v.minor}.{v.micro} — BridgeHelp.pyc is locked to 3.13 magic bytes")
    else:
        _add("FAIL", "Python version", f"got {v.major}.{v.minor} — need 3.13 for BridgeHelp.pyc")


def check_packages() -> None:
    required = ["pandas", "numpy", "zmq", "requests", "yfinance"]
    missing = []
    for pkg in required:
        try:
            importlib.import_module(pkg)
        except ImportError:
            missing.append(pkg)
    if not missing:
        _add("PASS", "Required packages", ", ".join(required))
    else:
        _add("FAIL", "Required packages", f"missing: {', '.join(missing)}")


def check_data_files() -> None:
    daily = HERE / "gold_daily.csv"
    h1 = HERE / "gold_1h.csv"
    if not daily.exists():
        _add("FAIL", "gold_daily.csv", "missing — run refresh_data.py")
    else:
        import pandas as pd
        df = pd.read_csv(daily)
        if len(df) < 400:
            _add("FAIL", "gold_daily.csv",
                 f"only {len(df)} rows; need >=400 for EMA(400) trend filter")
        else:
            last = pd.to_datetime(df["datetime"]).iloc[-1]
            age_days = (datetime.now() - last).days
            if age_days > 7:
                _add("WARN", "gold_daily.csv",
                     f"{len(df)} rows; last={last.date()} ({age_days}d old) — consider refresh")
            else:
                _add("PASS", "gold_daily.csv",
                     f"{len(df)} rows, last bar {last.date()}")
    if not h1.exists():
        _add("FAIL", "gold_1h.csv", "missing — run refresh_data.py (intraday warmup needs this)")
    else:
        import pandas as pd
        df = pd.read_csv(h1)
        if len(df) < 100:
            _add("WARN", "gold_1h.csv", f"only {len(df)} rows — warmup will be thin")
        else:
            _add("PASS", "gold_1h.csv", f"{len(df)} rows")


def check_strategy_config() -> None:
    try:
        from gdvt_strategy import StrategyConfig
        cfg = StrategyConfig()
        issues = []
        if cfg.trend_fast != 100:
            issues.append(f"trend_fast={cfg.trend_fast} (expected 100)")
        if cfg.trend_slow != 400:
            issues.append(f"trend_slow={cfg.trend_slow} (expected 400)")
        if cfg.flat_by_time != "13:30":
            issues.append(f"flat_by_time={cfg.flat_by_time!r} (expected '13:30')")
        if not (0.10 <= cfg.vol_target_annual <= 0.18):
            issues.append(f"vol_target_annual={cfg.vol_target_annual} (out of safe 0.10-0.18 range)")
        if cfg.max_lots > 8:
            issues.append(f"max_lots={cfg.max_lots} (max safe is 8 at the capital guardrail)")
        if issues:
            _add("FAIL", "StrategyConfig (variant C)", "; ".join(issues))
        else:
            _add("PASS", "StrategyConfig (variant C)",
                 f"EMA({cfg.trend_fast}/{cfg.trend_slow}), Donchian-{cfg.donchian_n}, "
                 f"flat={cfg.flat_by_time}, vol={cfg.vol_target_annual:.0%}, max_lots={cfg.max_lots}")
    except Exception as e:
        _add("FAIL", "StrategyConfig", f"could not import: {e}")


def check_live_config() -> None:
    try:
        sys.path.insert(0, str(HERE))
        # Use importlib so re-imports show fresh values
        import gdvt_live
        importlib.reload(gdvt_live)
        issues = []
        if gdvt_live.BRIDGE_PORT != 9000:
            issues.append(f"BRIDGE_PORT={gdvt_live.BRIDGE_PORT} (expected 9000)")
        # DRY_RUN=False is allowed (live mode); just warn so the user sees it explicitly.
        live_mode_warning = (not gdvt_live.DRY_RUN)
        if not gdvt_live.SYMBOL:
            issues.append(f"SYMBOL is empty/None")
        elif not gdvt_live.SYMBOL.startswith("FRONT"):
            issues.append(f"SYMBOL={gdvt_live.SYMBOL!r} doesn't start with 'FRONT'")
        # any front-month symbol is acceptable — the bridge will tell us if it's wrong
        if "HourlyAggregator" not in dir(gdvt_live):
            issues.append("HourlyAggregator class missing (still on FifteenMinAggregator?)")
        if gdvt_live.INTRADAY_HISTORY_CSV != "gold_1h.csv":
            issues.append(f"INTRADAY_HISTORY_CSV={gdvt_live.INTRADAY_HISTORY_CSV!r} (expected 'gold_1h.csv')")
        if issues:
            _add("FAIL", "gdvt_live config", "; ".join(issues))
        elif live_mode_warning:
            _add("WARN", "gdvt_live config",
                 f"port={gdvt_live.BRIDGE_PORT}, **DRY_RUN=False (LIVE MODE)**, "
                 f"symbol={gdvt_live.SYMBOL}, 1h aggregator")
        else:
            _add("PASS", "gdvt_live config",
                 f"port={gdvt_live.BRIDGE_PORT}, dry_run=True, symbol={gdvt_live.SYMBOL}, 1h aggregator")
    except Exception as e:
        _add("FAIL", "gdvt_live config", f"import error: {e}")


def check_supervisor_config() -> None:
    sup_path = HERE / "gdvt_supervisor.py"
    if not sup_path.exists():
        _add("FAIL", "gdvt_supervisor.py", "missing")
        return
    src = sup_path.read_text(encoding="utf-8")
    issues = []
    if "EXCHANGE_TZ" not in src:
        issues.append("not exchange-TZ-aware")
    if "now.weekday() >= 5" not in src:
        issues.append("missing weekend skip")
    # Accept any silent-fail threshold >= 4200s (1h-bar friendly)
    import re
    m = re.search(r"SILENT_FAIL_SECS\s*=\s*(\d+)", src)
    if not m:
        issues.append("SILENT_FAIL_SECS not found in supervisor")
    elif int(m.group(1)) < 4200:
        issues.append(f"SILENT_FAIL_SECS={m.group(1)} < 4200 (too tight for 1h bars)")
    if issues:
        _add("FAIL", "gdvt_supervisor.py", "; ".join(issues))
    else:
        _add("PASS", "gdvt_supervisor.py", "exchange-TZ-aware, weekend-skip, 70-min silent-fail")


def check_bridge_pyc() -> None:
    pyc = HERE / "BrokerBridge" / "Sample" / "Python" / "BridgeHelp.pyc"
    if not pyc.exists():
        _add("FAIL", "BridgeHelp.pyc", f"missing at {pyc}")
        return
    try:
        sys.path.insert(0, str(pyc.parent))
        import BridgeHelp  # noqa: F401
        _add("PASS", "BridgeHelp.pyc", "imports cleanly under Python 3.13")
    except Exception as e:
        _add("FAIL", "BridgeHelp.pyc", f"import error: {e}")


def check_timezone() -> None:
    try:
        exch = datetime.now(tz=ZoneInfo("UTC"))  # exchange-local timezone (placeholder)
        loc = datetime.now()
        _add("PASS", "Timezone (exchange-local)",
             f"PC={loc.strftime('%H:%M')} local, exchange={exch.strftime('%H:%M')}")
    except Exception as e:
        _add("FAIL", "Timezone (exchange-local)", f"zoneinfo issue: {e}")


def check_logs_dir() -> None:
    logs = HERE / "logs"
    if logs.exists():
        _add("PASS", "logs/ directory", f"{logs}")
    else:
        try:
            logs.mkdir(parents=True, exist_ok=True)
            _add("PASS", "logs/ directory", f"created {logs}")
        except Exception as e:
            _add("FAIL", "logs/ directory", f"can't create: {e}")


def main() -> int:
    print(f"Pre-flight check  ({HERE})")
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (PC local)")
    print("=" * 75)

    check_python_version()
    check_packages()
    check_data_files()
    check_strategy_config()
    check_live_config()
    check_supervisor_config()
    check_bridge_pyc()
    check_timezone()
    check_logs_dir()

    pad = max(len(name) for _, name, _ in RESULTS) + 2
    fail_count = warn_count = 0
    for status, name, detail in RESULTS:
        marker = {"PASS": "[PASS]", "WARN": "[WARN]", "FAIL": "[FAIL]"}[status]
        print(f"  {marker}  {name.ljust(pad)} {detail}")
        if status == "FAIL":
            fail_count += 1
        elif status == "WARN":
            warn_count += 1

    print("=" * 75)
    if fail_count:
        print(f"  {fail_count} FAIL, {warn_count} WARN  ->  DO NOT launch. Fix issues above.")
        return 1
    if warn_count:
        print(f"  All checks passed with {warn_count} WARN(s).  ->  Probably OK, review warnings.")
        return 2
    print(f"  All {len(RESULTS)} checks passed.  ->  Safe to launch:  py -3.13 gdvt_supervisor.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
