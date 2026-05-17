"""Bono — Session Health Watchdog 17/05.

Surveille fin de session ACC et audit auto-magique :
  - MoTeC .ld généré ? (ACC autosave OK)
  - Results JSON présent ? (session finalisée proprement)
  - perf_monitor CSV alimenté ? (hardware metrics OK)
  - Bono DB tables (audio_metrics, system_snapshot, setup_changes) reçoivent rows ?

Si KO : Toast Windows + log + écrit rapport JSON dans bono_sessions/.

Lance comme service depuis launcher.py. Tourne en arrière-plan en permanence.
Trigger end-of-session = ACC.exe disparu (process kill) OU SHM status OFF > 3min après LIVE.

Pas de Telegram nécessaire. Toast Windows = visible si Dan revient au desktop.
"""
import os
import sys
import time
import json
import subprocess
import sqlite3
from datetime import datetime
from pathlib import Path

import psutil
from loguru import logger

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import memory  # noqa

# === Paths ===
MOTEC_DIR = Path(os.path.expanduser(r"~\Documents\Assetto Corsa Competizione\MoTeC"))
RESULTS_DIR = Path(os.path.expanduser(r"~\Documents\Assetto Corsa Competizione\Results"))
REPLAY_DIR = Path(os.path.expanduser(r"~\Documents\Assetto Corsa Competizione\Replay\Temp"))
PERF_CSV_DIR = Path(os.path.expanduser(r"~\Desktop\bono_sessions"))
SESSION_HEALTH_DIR = PERF_CSV_DIR  # write health JSON next to perf CSVs

ACC_PROCESS_NAMES = ("AC2-Win64-Shipping.exe", "acs.exe")

POLL_S = 5.0
ACC_OFF_GRACE_S = 180  # 3 min after ACC SHM OFF before considering "session ended"


def acc_running() -> bool:
    """ACC.exe process alive."""
    for p in psutil.process_iter(["name"]):
        try:
            if p.info["name"] in ACC_PROCESS_NAMES:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def get_bono_shm_status() -> dict | None:
    """Read Bono /snapshot to get last known SHM state."""
    try:
        import urllib.request
        r = urllib.request.urlopen("http://127.0.0.1:8766/snapshot", timeout=2).read().decode()
        return json.loads(r)
    except Exception:
        return None


def find_new_files_since(folder: Path, since_ts: float, pattern: str = "*") -> list[Path]:
    """Files in folder modified after since_ts."""
    if not folder.exists():
        return []
    return [f for f in folder.glob(pattern) if f.is_file() and f.stat().st_mtime > since_ts]


def audit_session(session_start_ts: float, session_end_ts: float) -> dict:
    """Audit complete health post-session."""
    issues = []
    info = {}

    # 1. MoTeC .ld nouveau ?
    new_motec = find_new_files_since(MOTEC_DIR, session_start_ts - 60, "*.ld")
    info["motec_new_ld_count"] = len(new_motec)
    info["motec_files"] = [f.name for f in new_motec[:3]]
    if not new_motec:
        issues.append("MoTeC: AUCUN .ld exporté pour cette session. ACC autosave OFF ou session mal terminée.")

    # 2. Results JSON nouveau ?
    # Fix 17/05 nuit : ACC ne génère Results JSON QUE pour sessions officielles (MP race weekend,
    # Special Events, Championship). PAS pour single-player Practice/Hotlap/Free Practice.
    # On flagge l'absence seulement si on est sûr d'avoir été en mode officiel.
    new_results = find_new_files_since(RESULTS_DIR, session_start_ts - 60, "*.json")
    info["results_new_json_count"] = len(new_results)
    # Check session_type via SHM snapshot pour décider si Results attendu
    snap_now = get_bono_shm_status() or {}
    session_type_observed = (snap_now.get("session") or "").upper()
    results_expected = session_type_observed in ("RACE", "QUALIFY")
    if results_expected and not new_results:
        issues.append("Results: AUCUN session JSON. ACC n'a pas finalisé la session (Alt+F4 ? crash ?).")

    # 3. Replay nouveau ? (info, not critical)
    new_replays = find_new_files_since(REPLAY_DIR, session_start_ts - 60, "*.rpy")
    info["replay_new_count"] = len(new_replays)

    # 4. perf_monitor CSV : nouveau fichier OU alimenté ?
    new_perf = find_new_files_since(PERF_CSV_DIR, session_start_ts - 60, "*.csv")
    new_perf_real = [f for f in new_perf if "no_acc_no_car" not in f.name and "presentmon" not in f.name and "_health" not in f.name]
    info["perf_csv_count"] = len(new_perf_real)
    if not new_perf_real:
        issues.append("perf_monitor: AUCUN CSV hardware. Service probablement mort. Vérifier launcher.")
    else:
        # Check CSV has > 60 rows (= au moins 1 min de session loggée)
        for csv in new_perf_real[:1]:
            try:
                rows = len(open(csv, encoding="utf-8").readlines())
                info["perf_csv_first_rows"] = rows
                if rows < 60:
                    issues.append(f"perf_monitor: CSV {csv.name} a seulement {rows} lignes (<60 = <1min). Service crashé tôt ?")
            except Exception:
                pass

    # 5. PresentMon FPS ?
    new_presentmon = find_new_files_since(PERF_CSV_DIR, session_start_ts - 60, "*presentmon*.csv")
    info["presentmon_csv_count"] = len(new_presentmon)
    if not new_presentmon:
        issues.append("PresentMon: AUCUN FPS log. Vérifier tools/presentmon.exe + perf_monitor.start_presentmon.")

    # 6. Bono DB tables — rows ajoutées pendant la session ?
    try:
        c = sqlite3.connect(str(ROOT / "db/bono.db"))
        c.row_factory = sqlite3.Row
        for table, friendly in [
            ("audio_metrics", "Audio metrics (PTT logs)"),
            ("system_snapshot_at_event", "System snapshot at event"),
            ("setup_changes", "Setup changes audit"),
            ("lap_history", "Lap history"),
            ("driving_trace", "Driving trace sectorial"),
            ("events", "Auto-events"),
            ("pipeline_timings", "Pipeline timings (STT/LLM/TTS)"),
        ]:
            try:
                n = c.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE ts > ?" if table in ("audio_metrics","system_snapshot_at_event","lap_history","driving_trace","events","pipeline_timings")
                    else f"SELECT COUNT(*) FROM {table} WHERE created_at > ?",
                    (session_start_ts if table != "setup_changes" else datetime.fromtimestamp(session_start_ts).isoformat(),)
                ).fetchone()[0]
                info[f"db_{table}_new_rows"] = n
                if table in ("audio_metrics", "system_snapshot_at_event") and n == 0:
                    issues.append(f"{friendly}: 0 row added during session. Instrumentation cassée ?")
                if table == "lap_history" and n == 0:
                    issues.append(f"{friendly}: 0 lap loggé. ACC pas en LIVE ou session de boot.")
            except Exception:
                info[f"db_{table}_new_rows"] = "ERR"
        c.close()
    except Exception as e:
        issues.append(f"DB read fail: {e}")

    duration_min = (session_end_ts - session_start_ts) / 60
    return {
        "session_started_at": datetime.fromtimestamp(session_start_ts).isoformat(),
        "session_ended_at": datetime.fromtimestamp(session_end_ts).isoformat(),
        "duration_min": round(duration_min, 1),
        "n_issues": len(issues),
        "issues": issues,
        "info": info,
        "overall": "OK" if not issues else f"{len(issues)} issue(s)",
    }


def toast(title: str, message: str, critical: bool = False):
    """Windows 10/11 toast notification via PowerShell BurntToast or fallback msg."""
    try:
        # Use Windows native ToastNotification via PowerShell — no dep
        title_e = title.replace("'", "''")
        msg_e = message.replace("'", "''")[:200]
        ps = (
            f"[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
            f"$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
            f"$nodes = $template.GetElementsByTagName('text');"
            f"$nodes.Item(0).AppendChild($template.CreateTextNode('{title_e}')) | Out-Null;"
            f"$nodes.Item(1).AppendChild($template.CreateTextNode('{msg_e}')) | Out-Null;"
            f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Bono').Show([Windows.UI.Notifications.ToastNotification]::new($template));"
        )
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:
        logger.warning(f"[toast] fail: {e}")


def write_alert_file(report: dict):
    """Write SESSION_HEALTH_ALERT_*.txt on Desktop for high visibility."""
    try:
        desktop = Path(os.path.expanduser(r"~\Desktop"))
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = desktop / f"BONO_SESSION_HEALTH_{ts}.txt"
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"=== BONO SESSION HEALTH ALERT ===\n\n")
            f.write(f"Session : {report['session_started_at']} → {report['session_ended_at']}\n")
            f.write(f"Duration: {report['duration_min']} min\n")
            f.write(f"Overall : {report['overall']}\n\n")
            f.write("ISSUES:\n")
            for i in report["issues"]:
                f.write(f"  - {i}\n")
            f.write("\nINFO:\n")
            for k, v in report["info"].items():
                f.write(f"  {k}: {v}\n")
        logger.info(f"[health] alert file written: {path}")
    except Exception as e:
        logger.warning(f"[health] write alert file fail: {e}")


def write_history_json(report: dict):
    """Append health report to bono_sessions/session_health_history.json (and per-session file)."""
    try:
        SESSION_HEALTH_DIR.mkdir(parents=True, exist_ok=True)
        # Per-session file
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        per_session = SESSION_HEALTH_DIR / f"{ts}_health.json"
        per_session.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        # History append
        history_file = SESSION_HEALTH_DIR / "session_health_history.jsonl"
        with open(history_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(report, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"[health] history write fail: {e}")


def main():
    logger.add("logs/session_health_watchdog.log", rotation="5 MB", retention=3)
    logger.info("=== Bono session_health_watchdog start ===")

    acc_was_running = False
    acc_was_live = False
    session_start_ts: float | None = None
    last_live_ts = 0.0

    while True:
        try:
            acc_now = acc_running()
            snap = get_bono_shm_status() or {}
            shm_live = snap.get("status") == "LIVE" and snap.get("shm_ok")

            # Entry : ACC process appears + SHM LIVE
            if acc_now and shm_live and not acc_was_live:
                session_start_ts = time.time()
                acc_was_live = True
                logger.info(f"[health] session START detected at {datetime.now().isoformat()}")
            if shm_live:
                last_live_ts = time.time()

            # Exit : ACC process gone (clean quit) OR SHM OFF > GRACE_S after being LIVE
            session_ended = False
            if acc_was_live:
                if not acc_now:
                    logger.info("[health] session END detected: ACC process gone")
                    session_ended = True
                elif not shm_live and (time.time() - last_live_ts) > ACC_OFF_GRACE_S:
                    logger.info(f"[health] session END detected: SHM OFF > {ACC_OFF_GRACE_S}s")
                    session_ended = True

            if session_ended and session_start_ts:
                end_ts = time.time()
                logger.info(f"[health] auditing session {session_start_ts} → {end_ts} ({(end_ts-session_start_ts)/60:.1f} min)")
                report = audit_session(session_start_ts, end_ts)
                write_history_json(report)
                logger.info(f"[health] result: {report['overall']} | issues={report['n_issues']}")
                if report["n_issues"] > 0:
                    write_alert_file(report)
                    toast(
                        title=f"⚠️ Bono Session Health — {report['n_issues']} issue(s)",
                        message=f"{report['duration_min']}min, voir Desktop BONO_SESSION_HEALTH_*.txt",
                        critical=True,
                    )
                else:
                    # OK quiet toast for confirmation
                    toast(
                        title="✅ Bono Session Health OK",
                        message=f"Session {report['duration_min']}min terminée, télémétrie complète.",
                        critical=False,
                    )
                # Reset state
                acc_was_live = False
                session_start_ts = None
                last_live_ts = 0.0

            acc_was_running = acc_now

        except Exception as e:
            logger.exception(f"[health] tick error: {e}")
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
