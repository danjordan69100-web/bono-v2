"""Bono v2 — Hardening utilities 24/7.

Provides standalone helpers + cron-friendly subcommands for production maintenance :
- SQLite VACUUM + WAL checkpoint
- TTS cache LRU enforcement (in addition to inline cap)
- Disk pressure monitoring + alert
- Log/db purge by age
- Watchdog cross-service health check

Usage :
  python hardening_24_7.py vacuum-db
  python hardening_24_7.py purge-cache --older-days 7
  python hardening_24_7.py purge-logs --older-days 30
  python hardening_24_7.py disk-check --warn-pct 90
  python hardening_24_7.py healthcheck
  python hardening_24_7.py soak-stats   # last 24h memory/cost overview

Schedule via Windows Task Scheduler :
  Daily 04:00 : vacuum-db + purge-cache + purge-logs
  Hourly      : disk-check + healthcheck (alert if anomaly)
"""
import sys
import os
import argparse
import sqlite3
import time
import json
import shutil
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).parent
DB_PATH = ROOT / "db" / "bono.db"
TTS_CACHE = ROOT / "tts_cache"
LOGS_DIR = ROOT / "logs"
SESSIONS_DIR = Path(r"C:\Users\danjo\Desktop\bono_sessions")

CACHE_MAX_MB = 500
CACHE_PURGE_TARGET_MB = 350  # purge down to this
WARN_DISK_PCT = 90
WARN_DISK_FREE_GB = 5


def vacuum_db():
    if not DB_PATH.exists():
        print(f"[vacuum] DB not found: {DB_PATH}")
        return
    size_before = DB_PATH.stat().st_size
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()
    size_after = DB_PATH.stat().st_size
    saved = (size_before - size_after) / 1024 / 1024
    print(f"[vacuum] {DB_PATH.name} : {size_before/1024/1024:.1f} MB -> {size_after/1024/1024:.1f} MB (saved {saved:.1f} MB)")


def purge_cache(older_days: int = 7):
    if not TTS_CACHE.exists():
        print("[cache] No tts_cache dir")
        return
    cutoff = time.time() - older_days * 86400
    removed = 0; freed = 0
    for f in TTS_CACHE.glob("*.mp3"):
        try:
            if f.stat().st_atime < cutoff:
                freed += f.stat().st_size
                f.unlink()
                removed += 1
        except Exception:
            pass
    # Enforce hard cap after age-purge
    files = sorted(TTS_CACHE.glob("*.mp3"), key=lambda p: p.stat().st_atime)
    total_mb = sum(f.stat().st_size for f in files) / 1024 / 1024
    if total_mb > CACHE_MAX_MB:
        print(f"[cache] over cap ({total_mb:.0f} MB > {CACHE_MAX_MB} MB), LRU evict to {CACHE_PURGE_TARGET_MB} MB")
        for f in files:
            if total_mb < CACHE_PURGE_TARGET_MB:
                break
            try:
                sz = f.stat().st_size / 1024 / 1024
                f.unlink()
                total_mb -= sz
                removed += 1
                freed += int(sz * 1024 * 1024)
            except Exception:
                pass
    print(f"[cache] removed {removed} files, freed {freed/1024/1024:.1f} MB")


def purge_logs(older_days: int = 30):
    cutoff = time.time() - older_days * 86400
    removed = 0; freed = 0
    for d in (LOGS_DIR, SESSIONS_DIR):
        if not d.exists():
            continue
        for f in d.glob("*.log*"):
            try:
                if f.stat().st_mtime < cutoff:
                    freed += f.stat().st_size; f.unlink(); removed += 1
            except Exception:
                pass
        for f in d.glob("*.csv"):
            try:
                if f.stat().st_mtime < cutoff:
                    freed += f.stat().st_size; f.unlink(); removed += 1
            except Exception:
                pass
    print(f"[logs] removed {removed} files (>{older_days}d), freed {freed/1024/1024:.1f} MB")


def disk_check(warn_pct: int = WARN_DISK_PCT, warn_free_gb: float = WARN_DISK_FREE_GB):
    d = shutil.disk_usage("C:/")
    pct = (d.used / d.total) * 100
    free_gb = d.free / 1024 / 1024 / 1024
    alert = []
    if pct > warn_pct: alert.append(f"USED_PCT {pct:.1f}% > {warn_pct}%")
    if free_gb < warn_free_gb: alert.append(f"FREE {free_gb:.1f} GB < {warn_free_gb} GB")
    status = "ALERT" if alert else "OK"
    print(f"[disk] C: used={pct:.1f}% free={free_gb:.1f}GB total={d.total/1024**3:.0f}GB | {status} {' '.join(alert)}")
    return not alert


def healthcheck() -> dict:
    """Probe Bono services and write status to logs/healthcheck.json."""
    import socket
    out = {"ts": datetime.now(timezone.utc).isoformat(), "services": {}}
    ports = {"core": 8766, "mcp": 8767, "telemetry": 5555, "audio": 5556, "playback": 5557}
    for label, port in ports.items():
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                out["services"][label] = "alive"
        except Exception:
            out["services"][label] = "down"
    # DB read sanity
    try:
        c = sqlite3.connect(str(DB_PATH), timeout=3)
        n = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        out["db"] = {"ok": True, "sessions": n}
        c.close()
    except Exception as e:
        out["db"] = {"ok": False, "err": str(e)[:80]}
    out["disk"] = disk_status_dict()
    LOGS_DIR.mkdir(exist_ok=True)
    (LOGS_DIR / "healthcheck.json").write_text(json.dumps(out, indent=2))
    summary = ", ".join(f"{k}={v}" for k, v in out["services"].items())
    print(f"[health] {summary} | db: {out['db'].get('sessions', '?')} sessions | disk free: {out['disk']['free_gb']:.1f}GB")
    return out


def disk_status_dict():
    d = shutil.disk_usage("C:/")
    return {"used_pct": round((d.used/d.total)*100, 1), "free_gb": round(d.free/1024**3, 1), "total_gb": round(d.total/1024**3, 1)}


def soak_stats():
    """Last 24h overview from bono.db."""
    if not DB_PATH.exists():
        print("DB missing"); return
    cutoff = time.time() - 86400
    c = sqlite3.connect(str(DB_PATH), timeout=10)
    c.row_factory = sqlite3.Row
    try:
        n_sess = c.execute("SELECT COUNT(*) AS n FROM sessions WHERE started_at > ?", (cutoff,)).fetchone()["n"]
        n_exch = c.execute("SELECT COUNT(*) AS n FROM exchanges WHERE ts > ?", (cutoff,)).fetchone()["n"]
        n_evt = c.execute("SELECT COUNT(*) AS n FROM events WHERE ts > ?", (cutoff,)).fetchone()["n"]
        cost = c.execute("SELECT COALESCE(SUM(cost_usd),0) AS t FROM costs WHERE ts > ?", (cutoff,)).fetchone()["t"]
        avg_lat = c.execute("SELECT COALESCE(AVG(total_e2e_ms),0) AS a FROM pipeline_timings WHERE ts > ?", (cutoff,)).fetchone()["a"]
    finally:
        c.close()
    print(f"[soak-24h] sessions={n_sess} exchanges={n_exch} events={n_evt} cost=${cost:.4f} avg_e2e={avg_lat:.0f}ms")
    return {"sessions": n_sess, "exchanges": n_exch, "events": n_evt, "cost_usd": cost, "avg_e2e_ms": avg_lat}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["vacuum-db", "purge-cache", "purge-logs", "disk-check", "healthcheck", "soak-stats", "all"])
    ap.add_argument("--older-days", type=int, default=7)
    ap.add_argument("--warn-pct", type=int, default=WARN_DISK_PCT)
    args = ap.parse_args()
    if args.command == "vacuum-db" or args.command == "all":
        vacuum_db()
    if args.command == "purge-cache" or args.command == "all":
        purge_cache(args.older_days)
    if args.command == "purge-logs" or args.command == "all":
        purge_logs(args.older_days if args.older_days else 30)
    if args.command == "disk-check" or args.command == "all":
        disk_check(args.warn_pct)
    if args.command == "healthcheck" or args.command == "all":
        healthcheck()
    if args.command == "soak-stats":
        soak_stats()


if __name__ == "__main__":
    main()
