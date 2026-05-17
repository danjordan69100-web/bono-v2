"""Bono v2 — Performance monitor standalone.

Monitors during ACC sessions :
- FPS / frametime (via PresentMon or pygame fallback)
- CPU/RAM/GPU/VRAM utilisation (psutil + pynvml)
- API ping (Anthropic, Deepgram, ElevenLabs, Fish)
- Bono services health (5 ports)
- ACC.exe detection + session SHM events

Output : per-session CSV in C:/Users/danjo/Desktop/bono_sessions/<ts>_<track>_<car>.csv
+ summary JSON post-session.

Run : python bono_perf_monitor.py [--no-presentmon]
Or auto-start via launcher.py (added as 6th service if BONO_ENABLE_PERF_MONITOR=1).
"""
import sys
import os
import csv
import json
import time
import socket
import argparse
import subprocess
import threading
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

import psutil

OUT_DIR = Path(r"C:\Users\danjo\Desktop\bono_sessions")
OUT_DIR.mkdir(parents=True, exist_ok=True)
PRESENTMON_EXE = Path(r"C:\dev\bono_v2\tools\presentmon.exe")

ACC_PROCESS_NAMES = {"AC2-Win64-Shipping.exe", "acc.exe"}

# V4 FPS monitoring via PresentMon (download GameTechDev v2.3.1)
_presentmon_proc = None
_presentmon_csv: Path | None = None


def start_presentmon(session_name: str) -> bool:
    """Spawn PresentMon as background subprocess capturing AC2-Win64-Shipping.exe FPS to CSV.
    Returns True if started. PresentMon écrit 1 ligne par frame présentée → on agrège dans la boucle perf_monitor."""
    global _presentmon_proc, _presentmon_csv
    if not PRESENTMON_EXE.exists():
        print("[fps] presentmon.exe not found, skip FPS monitoring")
        return False
    _presentmon_csv = OUT_DIR / f"{session_name}_presentmon.csv"
    try:
        _presentmon_proc = subprocess.Popen(
            [str(PRESENTMON_EXE),
             "--process_name", "AC2-Win64-Shipping.exe",
             "--output_file", str(_presentmon_csv),
             "--no_console_stats",
             "--terminate_on_proc_exit"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        print(f"[fps] PresentMon started PID={_presentmon_proc.pid} -> {_presentmon_csv}")
        return True
    except Exception as e:
        print(f"[fps] PresentMon start fail: {e}")
        return False


def stop_presentmon():
    global _presentmon_proc
    if _presentmon_proc is not None:
        try:
            _presentmon_proc.terminate()
            _presentmon_proc.wait(timeout=3)
        except Exception:
            try: _presentmon_proc.kill()
            except Exception: pass
        _presentmon_proc = None


def fps_metrics_window(last_n_lines: int = 60) -> dict:
    """Read last N lines from PresentMon CSV and compute FPS stats over the recent window (~1s if FPS~60).
    Returns dict with fps_avg, fps_min, fps_max, frame_time_p99_ms, fps_1pct_low.
    """
    if _presentmon_csv is None or not _presentmon_csv.exists():
        return {"fps_avail": False}
    try:
        # PresentMon CSV header includes 'MsBetweenPresents' = frame time ms
        with open(_presentmon_csv, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        if len(lines) < 5:
            return {"fps_avail": False, "fps_reason": "warmup"}
        header = lines[0].strip().split(",")
        try:
            ms_idx = header.index("MsBetweenPresents")
        except ValueError:
            return {"fps_avail": False, "fps_reason": "no_MsBetweenPresents_col"}
        # Take last N data lines
        data_lines = lines[-last_n_lines:]
        times_ms = []
        for ln in data_lines:
            cols = ln.strip().split(",")
            if len(cols) > ms_idx:
                try:
                    t = float(cols[ms_idx])
                    if 0.1 < t < 1000:  # sanity (1000ms = 1 FPS, lower limit anti-divbyzero)
                        times_ms.append(t)
                except ValueError:
                    pass
        if not times_ms:
            return {"fps_avail": False, "fps_reason": "no_data"}
        # FPS = 1000 / frame_time
        fpss = [1000.0 / t for t in times_ms]
        fpss_sorted = sorted(fpss)
        n = len(fpss_sorted)
        p1_idx = max(0, int(n * 0.01) - 1)
        p99_ft_idx = min(n - 1, int(n * 0.99))
        return {
            "fps_avail": True,
            "fps_avg": round(sum(fpss) / n, 1),
            "fps_min": round(min(fpss), 1),
            "fps_max": round(max(fpss), 1),
            "fps_1pct_low": round(fpss_sorted[p1_idx], 1),
            "frame_time_p99_ms": round(sorted(times_ms)[p99_ft_idx], 2),
            "fps_samples": n,
        }
    except Exception as e:
        return {"fps_avail": False, "fps_err": str(e)[:60]}
BONO_PORTS = {8766: "core", 8767: "mcp", 5555: "telemetry", 5556: "audio", 5557: "playback", 5558: "events"}
API_PROBES = {
    "anthropic": "https://api.anthropic.com/v1/messages",
    "deepgram": "https://api.deepgram.com",
    "elevenlabs": "https://api.elevenlabs.io/v1/voices",
    "fish_audio": "https://api.fish.audio/v1/tts",
}


def find_acc_process():
    for p in psutil.process_iter(["pid", "name", "exe"]):
        try:
            if p.info["name"] in ACC_PROCESS_NAMES:
                return p
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return None


def find_bono_processes():
    """Locate python.exe processes whose cmdline matches Bono modules."""
    out = {}
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmd = " ".join(p.info.get("cmdline") or [])
            for mod in ("core_service", "playback_service", "input_service", "mcp_server", "telegram_bot"):
                if mod in cmd:
                    out[mod] = p
                    break
        except Exception:
            pass
    return out


def port_alive(port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except Exception:
        return False


def ping_api(url: str, timeout: float = 2.0) -> int | None:
    """Returns latency_ms or None on fail."""
    try:
        t0 = time.time()
        req = urllib.request.Request(url, method="HEAD")
        urllib.request.urlopen(req, timeout=timeout)
        return int((time.time() - t0) * 1000)
    except urllib.error.HTTPError as e:
        # 4xx still = reachable
        return int((time.time() - t0) * 1000)
    except Exception:
        return None


_nvml_initialized = False
_nvml_handle = None


def init_nvml():
    global _nvml_initialized, _nvml_handle
    if _nvml_initialized:
        return _nvml_handle is not None
    _nvml_initialized = True
    try:
        import pynvml
        pynvml.nvmlInit()
        _nvml_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        return True
    except Exception as e:
        print(f"[nvml] init fail : {e}")
        return False


def gpu_metrics() -> dict:
    if not init_nvml():
        return {"gpu_avail": False}
    try:
        import pynvml
        h = _nvml_handle
        util = pynvml.nvmlDeviceGetUtilizationRates(h)
        mem = pynvml.nvmlDeviceGetMemoryInfo(h)
        temp = pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
        power = pynvml.nvmlDeviceGetPowerUsage(h) / 1000.0  # mW -&gt; W
        clock_sm = pynvml.nvmlDeviceGetClockInfo(h, pynvml.NVML_CLOCK_SM)
        clock_mem = pynvml.nvmlDeviceGetClockInfo(h, pynvml.NVML_CLOCK_MEM)
        return {
            "gpu_avail": True,
            "gpu_util_pct": util.gpu,
            "vram_used_mb": int(mem.used / 1024 / 1024),
            "vram_total_mb": int(mem.total / 1024 / 1024),
            "gpu_temp_c": temp,
            "gpu_power_w": round(power, 1),
            "gpu_clock_mhz": clock_sm,
            "vram_clock_mhz": clock_mem,
        }
    except Exception as e:
        return {"gpu_avail": False, "gpu_err": str(e)[:80]}


def cpu_ram_metrics() -> dict:
    cpu = psutil.cpu_percent(interval=None)
    ram = psutil.virtual_memory()
    return {
        "cpu_pct": cpu,
        "ram_used_mb": int(ram.used / 1024 / 1024),
        "ram_pct": ram.percent,
        "ram_avail_mb": int(ram.available / 1024 / 1024),
    }


def disk_pressure() -> dict:
    try:
        d = psutil.disk_usage("C:/")
        return {"disk_c_pct": d.percent, "disk_c_free_gb": round(d.free / 1024 / 1024 / 1024, 1)}
    except Exception:
        return {}


def proc_metrics(p: psutil.Process | None, prefix: str) -> dict:
    if not p:
        return {f"{prefix}_alive": False}
    try:
        with p.oneshot():
            cpu = p.cpu_percent(interval=None)
            mem = p.memory_info()
            return {
                f"{prefix}_alive": True,
                f"{prefix}_cpu_pct": cpu,
                f"{prefix}_rss_mb": int(mem.rss / 1024 / 1024),
                f"{prefix}_pid": p.pid,
            }
    except Exception:
        return {f"{prefix}_alive": False}


def bono_health() -> dict:
    out = {}
    for port, label in BONO_PORTS.items():
        out[f"bono_{label}_port{port}_alive"] = port_alive(port)
    return out


def api_health() -> dict:
    """Probe APIs but throttled — every 30s only, not every tick."""
    out = {}
    for name, url in API_PROBES.items():
        lat = ping_api(url)
        out[f"api_{name}_ms"] = lat if lat is not None else -1
    return out


def shm_session_state() -> dict:
    """Read ACC SHM if available to extract session-level metrics."""
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from shm.acc_ctypes import AccShmReader
        r = AccShmReader()
        r.open()
        try:
            snap = r.snapshot()
        finally:
            r.close()
        if snap.get("shm_ok"):
            return {
                "acc_shm_ok": True,
                "acc_status": snap.get("status"),
                "acc_track": snap.get("track"),
                "acc_car": snap.get("car"),
                "acc_session": snap.get("session"),
                "acc_completed_laps": snap.get("completed_laps", 0),
                "acc_speed_kmh": round(snap.get("speed_kmh", 0), 1),
                "acc_rpm": snap.get("rpm", 0),
                "acc_position": snap.get("position", 0),
                "acc_fuel_l": round(snap.get("fuel_l", 0), 1),
            }
        else:
            return {"acc_shm_ok": False, "acc_reason": snap.get("reason", "?")}
    except Exception as e:
        return {"acc_shm_ok": False, "acc_err": str(e)[:60]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=1.0, help="Sampling interval seconds")
    ap.add_argument("--api-interval", type=float, default=30.0, help="API ping interval seconds")
    ap.add_argument("--session-name", default=None, help="Override session name")
    args = ap.parse_args()

    print(f"[perf_monitor] start | interval={args.interval}s api={args.api_interval}s out={OUT_DIR}")

    # Determine session start
    ts_start = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    # Try to read ACC SHM for track/car immediately
    shm = shm_session_state()
    track = (shm.get("acc_track") or "no_acc").strip().replace(" ", "_")
    car = (shm.get("acc_car") or "no_car").strip().replace(" ", "_")
    session_name = args.session_name or f"{ts_start}_{track}_{car}"
    csv_path = OUT_DIR / f"{session_name}.csv"
    print(f"[perf_monitor] csv -&gt; {csv_path}")

    # Prepare CSV
    last_api_ts = 0.0
    api_cache = api_health()
    last_api_ts = time.time()

    fields = [
        "ts_iso", "epoch", "uptime_s",
        # CPU/RAM/disk
        "cpu_pct", "ram_used_mb", "ram_pct", "ram_avail_mb", "disk_c_pct", "disk_c_free_gb",
        # GPU
        "gpu_avail", "gpu_util_pct", "vram_used_mb", "vram_total_mb", "gpu_temp_c", "gpu_power_w", "gpu_clock_mhz", "vram_clock_mhz",
        # FPS (V4 PresentMon integration)
        "fps_avail", "fps_avg", "fps_min", "fps_max", "fps_1pct_low", "frame_time_p99_ms", "fps_samples",
        # ACC SHM
        "acc_shm_ok", "acc_status", "acc_track", "acc_car", "acc_session", "acc_completed_laps", "acc_speed_kmh", "acc_rpm", "acc_position", "acc_fuel_l",
        # Bono services
        "bono_core_port8766_alive", "bono_mcp_port8767_alive", "bono_telemetry_port5555_alive", "bono_audio_port5556_alive", "bono_playback_port5557_alive", "bono_events_port5558_alive",
        # Bono processes
        "core_alive", "core_cpu_pct", "core_rss_mb", "core_pid",
        "playback_alive", "playback_cpu_pct", "playback_rss_mb", "playback_pid",
        "input_alive", "input_cpu_pct", "input_rss_mb", "input_pid",
        # ACC process
        "acc_proc_alive", "acc_cpu_pct", "acc_rss_mb", "acc_pid",
        # API ping (cached, refreshed periodic)
        "api_anthropic_ms", "api_deepgram_ms", "api_elevenlabs_ms", "api_fish_audio_ms",
    ]
    # Start PresentMon (V4 FPS monitoring)
    start_presentmon(session_name)

    t_start = time.time()
    # V3 : auto-stop when ACC has been off for >60s (auto-trigger analyze post-session)
    # Fix 17/05 (B5 audit) : avant ce fix, perf_monitor crashait à 60s si ACC pas encore lancé au boot
    # → CSV vide pour toute la session (session 49 Monza 17/05 a 6 lignes seulement).
    # Correction : `acc_seen_live` tracker — on autorise auto-stop UNIQUEMENT après avoir vu ACC LIVE au moins une fois.
    # Tant qu'ACC n'a jamais été détecté, on attend indéfiniment (wait-loop) sans terminer.
    acc_off_since = 0.0
    acc_seen_live = False  # passe à True dès qu'on observe acc_proc_alive ou acc_shm_ok
    AUTO_STOP_AFTER_ACC_OFF_S = 60
    # Bug 1 fix : track les valeurs ACC observées EN COURS (pas seulement au boot)
    # → permet de renommer le CSV à la fin avec le vrai track/car même si ACC pas encore up au start
    observed_track = track
    observed_car = car
    with open(csv_path, "w", encoding="utf-8", newline="") as fcsv:
        writer = csv.DictWriter(fcsv, fieldnames=fields)
        writer.writeheader()
        try:
            while True:
                t_now = time.time()
                row = {"ts_iso": datetime.now(timezone.utc).isoformat(), "epoch": t_now, "uptime_s": round(t_now - t_start, 1)}
                row.update(cpu_ram_metrics())
                row.update(disk_pressure())
                row.update(gpu_metrics())
                row.update(fps_metrics_window())
                row.update(shm_session_state())
                row.update(bono_health())

                # Process metrics : ACC + Bono services
                acc_proc = find_acc_process()
                row.update(proc_metrics(acc_proc, "acc_proc"))
                bono_procs = find_bono_processes()
                for mod_short, prefix in [("core_service", "core"), ("playback_service", "playback"), ("input_service", "input")]:
                    row.update(proc_metrics(bono_procs.get(mod_short), prefix))

                # API ping (throttled)
                if (t_now - last_api_ts) > args.api_interval:
                    api_cache = api_health()
                    last_api_ts = t_now
                row.update(api_cache)

                # Fill missing fields with None
                for f in fields:
                    row.setdefault(f, None)

                writer.writerow(row)
                fcsv.flush()

                # Bug 1 fix : capture track/car observés quand ACC SHM devient valide
                acc_t = (row.get("acc_track") or "").strip().replace(" ", "_")
                acc_c = (row.get("acc_car") or "").strip().replace(" ", "_")
                if acc_t and observed_track in ("no_acc", "no_track", ""):
                    observed_track = acc_t
                if acc_c and observed_car in ("no_car", "unknown", ""):
                    observed_car = acc_c

                # Compact stdout log every 10s
                if int(t_now - t_start) % 10 == 0:
                    print(f"[perf] {row['uptime_s']:6.0f}s | cpu={row['cpu_pct']}% ram={row['ram_pct']}% | gpu={row.get('gpu_util_pct','-')}% vram={row.get('vram_used_mb','-')}MB temp={row.get('gpu_temp_c','-')}C | acc={row.get('acc_status','-')} | core={row['core_alive']} play={row['playback_alive']}")
                # V3 : auto-stop trigger if ACC has been off for too long (post-session)
                # Fix 17/05 : exige `acc_seen_live=True` AVANT toute terminaison. Sinon on attend
                # indéfiniment au boot. Évite le bug de session 49 où perf_monitor s'est terminé
                # à 00:15:16 alors qu'ACC a démarré après.
                acc_running = row.get("acc_proc_alive", False) or row.get("acc_shm_ok", False)
                if acc_running:
                    acc_seen_live = True
                    acc_off_since = 0.0
                elif acc_seen_live:
                    # ACC a tourné au moins une fois et est maintenant off → counter peut démarrer
                    if acc_off_since == 0.0:
                        acc_off_since = t_now
                    elif (t_now - acc_off_since) > AUTO_STOP_AFTER_ACC_OFF_S:
                        print(f"[perf_monitor] ACC off since {AUTO_STOP_AFTER_ACC_OFF_S}s post-session - auto-stop + analyse")
                        raise KeyboardInterrupt
                # else : acc_seen_live=False, on attend indéfiniment qu'ACC apparaisse

                # Wait
                elapsed = time.time() - t_now
                if elapsed < args.interval:
                    time.sleep(args.interval - elapsed)
        except KeyboardInterrupt:
            # Stop PresentMon proprement
            stop_presentmon()
            # Bug 1 fix : rename le CSV avec track/car observés en cours de session
            # (si init était no_acc/no_car mais ACC a tourné après → on récupère le bon nom)
            if (observed_track != track or observed_car != car) and observed_track and observed_car:
                new_session_name = f"{ts_start}_{observed_track}_{observed_car}"
                new_csv_path = OUT_DIR / f"{new_session_name}.csv"
                try:
                    fcsv.close()
                    csv_path.rename(new_csv_path)
                    print(f"[perf_monitor] CSV renamed: {csv_path.name} -> {new_csv_path.name}")
                    csv_path = new_csv_path
                    session_name = new_session_name
                    track = observed_track
                    car = observed_car
                except Exception as _e:
                    print(f"[perf_monitor] CSV rename fail (non-fatal): {_e}")
            print(f"\n[perf_monitor] stopping, csv saved : {csv_path}")
            # Write summary JSON
            summary_path = csv_path.with_suffix(".summary.json")
            summary = {
                "session_name": session_name,
                "csv_file": str(csv_path),
                "ts_start": ts_start,
                "duration_s": round(time.time() - t_start, 1),
                "track": track,
                "car": car,
            }
            summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
            print(f"[perf_monitor] summary -&gt; {summary_path}")
            # V3 — Auto-trigger analyze_session.py post-session for Claude review
            try:
                import subprocess
                bono_dir = Path(__file__).parent
                analyze = bono_dir / "analyze_session.py"
                if analyze.exists():
                    print(f"[perf_monitor] triggering auto-analyze for Claude review...")
                    subprocess.run([sys.executable, str(analyze), "--auto-latest"], cwd=str(bono_dir), timeout=30, check=False)
                    print(f"[perf_monitor] analyse_session report generated in C:\\Users\\danjo\\Desktop\\bono_sessions\\")
            except Exception as e:
                print(f"[perf_monitor] auto-analyze fail : {e}")


if __name__ == "__main__":
    main()
