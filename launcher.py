"""Bono v2 — supervisor launcher.

Spawns 5 child processes (core/playback/input/mcp/telegram) in CreateNoWindow detached mode.
Each child writes its own log. Launcher monitors PIDs and restarts on crash.

GPT-5.4 audit P2 — anti restart-storm :
- Exponential backoff (1s, 2s, 4s, 8s, capped at 60s)
- Max 5 restarts per 10-min rolling window → DEGRADED state, stop respawn
- Per-service crash count + cumulative uptime tracking
- Healthcheck core /health endpoint after spawn

Logs : launcher.log + per-service stdout/stderr.
"""
import subprocess
import sys
import time
import os
import json
from pathlib import Path
from collections import deque
from datetime import datetime, timezone

ROOT = Path(__file__).parent
LOGS = ROOT / "logs"
LOGS.mkdir(exist_ok=True)
PY = r"C:\Users\danjo\AppData\Local\Programs\Python\Python311\python.exe"
PROCS = {
    "core": str(ROOT / "core_service.py"),
    "playback": str(ROOT / "playback_service.py"),
    "input": str(ROOT / "input_service.py"),
    "mcp": str(ROOT / "mcp_server.py"),
    "telegram": str(ROOT / "telegram_bot.py"),
}
SPAWN_ORDER = ["core", "playback", "input", "mcp", "telegram"]

# Anti restart-storm config
MAX_RESTARTS_WINDOW = 5      # max crashes
RESTART_WINDOW_S = 600       # window 10 min
BACKOFF_INITIAL_S = 1.0
BACKOFF_MAX_S = 60.0
HEALTH_CHECK_URL = "http://127.0.0.1:8766/health"
HEALTH_CHECK_TIMEOUT_S = 8


def log(msg: str):
    line = f"[launcher {datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOGS / "launcher.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


class ServiceState:
    def __init__(self, name: str):
        self.name = name
        self.proc: subprocess.Popen | None = None
        self.crash_times: deque = deque(maxlen=20)  # timestamps
        self.consecutive_crashes = 0
        self.next_backoff_s = BACKOFF_INITIAL_S
        self.degraded = False
        self.degraded_since = 0.0
        self.started_at = 0.0
        self.total_uptime_s = 0.0

    def record_crash(self):
        now = time.time()
        # uptime accounting
        if self.started_at > 0:
            self.total_uptime_s += now - self.started_at
        self.crash_times.append(now)
        self.consecutive_crashes += 1
        # Prune old crashes outside window
        while self.crash_times and self.crash_times[0] < now - RESTART_WINDOW_S:
            self.crash_times.popleft()
        # Storm check
        if len(self.crash_times) >= MAX_RESTARTS_WINDOW:
            self.degraded = True
            self.degraded_since = now
            log(f"[!]{self.name} DEGRADED — {len(self.crash_times)} crashes in {RESTART_WINDOW_S}s, stop respawn")
        # Exponential backoff
        self.next_backoff_s = min(self.next_backoff_s * 2, BACKOFF_MAX_S)

    def record_healthy_start(self):
        """Called when process appears stable after spawn."""
        self.consecutive_crashes = 0
        self.next_backoff_s = BACKOFF_INITIAL_S
        if self.degraded and (time.time() - self.degraded_since) > 3 * RESTART_WINDOW_S:
            self.degraded = False  # forgiveness after 30 min
            log(f"[OK]{self.name} healing : exit DEGRADED state")


def spawn(state: ServiceState, script_path: str) -> subprocess.Popen | None:
    """Spawn process. Returns Popen or None if degraded/skipped."""
    if state.degraded:
        return None
    if state.next_backoff_s > BACKOFF_INITIAL_S:
        log(f"  ~{state.name} backoff {state.next_backoff_s:.1f}s")
        time.sleep(state.next_backoff_s)
    log_out = open(LOGS / f"{state.name}.stdout.log", "a", encoding="utf-8")
    log_err = open(LOGS / f"{state.name}.stderr.log", "a", encoding="utf-8")
    p = subprocess.Popen(
        [PY, script_path],
        cwd=str(ROOT),
        stdout=log_out, stderr=log_err,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    state.proc = p
    state.started_at = time.time()
    log(f"  &gt;{state.name:10} PID={p.pid}")
    return p


def healthcheck_core(timeout_s: float = HEALTH_CHECK_TIMEOUT_S) -> bool:
    """Probe core_service /health after spawn."""
    import urllib.request
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = urllib.request.urlopen(HEALTH_CHECK_URL, timeout=1)
            if r.status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def write_status_snapshot(states: dict[str, ServiceState]):
    """Periodic status file for external monitoring."""
    snapshot = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "services": {},
    }
    for name, st in states.items():
        snapshot["services"][name] = {
            "pid": st.proc.pid if st.proc else None,
            "alive": (st.proc is not None and st.proc.poll() is None),
            "consecutive_crashes": st.consecutive_crashes,
            "crashes_window": len(st.crash_times),
            "degraded": st.degraded,
            "uptime_total_s": int(st.total_uptime_s + (time.time() - st.started_at if st.started_at and st.proc and st.proc.poll() is None else 0)),
        }
    try:
        with open(LOGS / "launcher_status.json", "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2)
    except Exception:
        pass


def main():
    log("=== Bono v2 launcher (with backoff + healthcheck) ===")
    log(f"Root  : {ROOT}")
    log(f"Python: {PY}")
    states = {name: ServiceState(name) for name in PROCS}
    try:
        for name in SPAWN_ORDER:
            spawn(states[name], PROCS[name])
            if name == "core":
                # Wait for core /health before spawning rest (dependency order)
                if healthcheck_core():
                    log("  [OK]core /health OK")
                    states["core"].record_healthy_start()
                else:
                    log("  [!]core /health timeout — continuing anyway")
            else:
                time.sleep(1.5)  # let settle ZMQ bind
                if states[name].proc and states[name].proc.poll() is None:
                    states[name].record_healthy_start()
        log("")
        log("All services launched. Dashboard: http://127.0.0.1:8766")
        log("CTRL+C to stop all.")
        log("")
        last_snapshot = 0.0
        while True:
            now = time.time()
            for name, st in list(states.items()):
                if st.proc is None:
                    # Was degraded or never spawned — try one re-spawn periodically
                    if not st.degraded and (now - st.degraded_since) > 30:
                        spawn(st, PROCS[name])
                    continue
                if st.proc.poll() is not None:
                    rc = st.proc.returncode
                    log(f"  [X]{name} DIED (exit {rc}), recording crash")
                    st.record_crash()
                    if not st.degraded:
                        spawn(st, PROCS[name])
                else:
                    # Process alive >30s = mark healthy
                    if now - st.started_at > 30 and st.consecutive_crashes > 0:
                        st.record_healthy_start()
            # Status snapshot every 15s
            if now - last_snapshot > 15:
                write_status_snapshot(states)
                last_snapshot = now
            time.sleep(2)
    except KeyboardInterrupt:
        log("=== Shutdown ===")
        for name, st in states.items():
            if st.proc:
                try:
                    st.proc.terminate()
                    log(f"  {name} term")
                except Exception:
                    pass
        time.sleep(1)
        for name, st in states.items():
            if st.proc:
                try:
                    if st.proc.poll() is None:
                        st.proc.kill()
                        log(f"  {name} kill")
                except Exception:
                    pass


if __name__ == "__main__":
    main()
