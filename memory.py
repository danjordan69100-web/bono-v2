"""Bono v2 — conversation + driver memory via SQLite WAL.

Tables:
- exchanges: (id, ts, session_id, driver_msg, bono_msg, model, tools_used, tokens_in, tokens_out, latency_ms)
- sessions:  (id, started_at, track, car, session_type, ended_at, summary)
- driver:    (key, value) — long-lived prefs (preferred name, favorite tracks, lifetime stats)
- events:    (id, ts, session_id, type, severity, payload_json)
- costs:     (id, ts, model, tokens_in, tokens_out, cost_usd)
"""
import sqlite3
import time
import json
import threading
from pathlib import Path
from typing import Optional

from config import DB_DIR


DB_PATH = DB_DIR / "bono.db"
_lock = threading.Lock()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _lock:
        c = get_conn()
        c.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at REAL NOT NULL,
            track TEXT, car TEXT, session_type TEXT,
            ended_at REAL, summary TEXT
        );
        CREATE TABLE IF NOT EXISTS exchanges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            session_id INTEGER,
            driver_msg TEXT, bono_msg TEXT,
            model TEXT, tools_used TEXT,
            tokens_in INTEGER, tokens_out INTEGER,
            latency_ms INTEGER,
            FOREIGN KEY(session_id) REFERENCES sessions(id)
        );
        CREATE TABLE IF NOT EXISTS driver (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            session_id INTEGER,
            type TEXT, severity TEXT, payload TEXT
        );
        CREATE TABLE IF NOT EXISTS costs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            model TEXT, tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL
        );
        -- V2.3 monitoring : pipeline timings per turn
        CREATE TABLE IF NOT EXISTS pipeline_timings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            session_id INTEGER,
            turn_id INTEGER,
            stt_ms INTEGER, stt_provider TEXT,
            llm_ttft_ms INTEGER, llm_total_ms INTEGER, llm_model TEXT,
            tts_ms INTEGER, tts_provider TEXT,
            playback_ms INTEGER,
            total_e2e_ms INTEGER,
            tools_used TEXT, tools_parallel INTEGER,
            tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL,
            cache_hit INTEGER,
            FOREIGN KEY(session_id) REFERENCES sessions(id)
        );
        -- V2.3 monitoring : audio metrics per turn
        CREATE TABLE IF NOT EXISTS audio_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            session_id INTEGER,
            turn_id INTEGER,
            duration_s REAL, rms REAL, peak REAL,
            sample_rate INTEGER, bytes INTEGER,
            transcript_len INTEGER, lang_detected TEXT,
            FOREIGN KEY(session_id) REFERENCES sessions(id)
        );
        -- V2.3 monitoring : SHM snapshot at event fire (correlate stutter with event)
        CREATE TABLE IF NOT EXISTS system_snapshot_at_event (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            session_id INTEGER,
            event_id INTEGER,
            speed_kmh REAL, rpm INTEGER, gear INTEGER,
            position INTEGER, lap INTEGER,
            fuel_l REAL, tyre_press_avg REAL, tyre_temp_avg REAL,
            FOREIGN KEY(session_id) REFERENCES sessions(id),
            FOREIGN KEY(event_id) REFERENCES events(id)
        );
        -- V3.B lap_history : per-completed-lap tracking for gap trend + sector analysis + strategy
        CREATE TABLE IF NOT EXISTS lap_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            session_id INTEGER,
            lap_num INTEGER,
            lap_time_ms INTEGER,
            s1_ms INTEGER, s2_ms INTEGER, s3_ms INTEGER,
            position INTEGER,
            gap_ahead_ms INTEGER, gap_behind_ms INTEGER,
            fuel_l REAL, fuel_per_lap REAL,
            tyre_press_avg REAL, tyre_temp_avg REAL, tyre_wear_max REAL,
            valid_lap INTEGER,
            track TEXT, car TEXT,
            FOREIGN KEY(session_id) REFERENCES sessions(id)
        );
        CREATE INDEX IF NOT EXISTS idx_lap_history_session ON lap_history(session_id);
        CREATE INDEX IF NOT EXISTS idx_lap_history_track_car ON lap_history(track, car);
        -- V3.O driving_trace : per-corner Vmin / brake_max / throttle_release for coaching micro
        CREATE TABLE IF NOT EXISTS driving_trace (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            session_id INTEGER,
            lap_num INTEGER,
            sector_index INTEGER,  -- 0=S1, 1=S2, 2=S3
            speed_min_kmh REAL,    -- Vmin within this sector
            speed_min_at_position REAL,  -- normalized track position [0,1] where Vmin reached
            brake_max_pct REAL,    -- max brake pressure observed in this sector
            throttle_release_position REAL,  -- normalized pos where throttle dropped
            wheel_slip_max REAL,   -- max wheel_slip across 4 corners during sector
            tc_active_pct REAL,    -- % time TC was active
            abs_active_pct REAL,
            track TEXT, car TEXT,
            FOREIGN KEY(session_id) REFERENCES sessions(id)
        );
        CREATE INDEX IF NOT EXISTS idx_driving_trace_session ON driving_trace(session_id);
        CREATE INDEX IF NOT EXISTS idx_driving_trace_track_car ON driving_trace(track, car);
        CREATE INDEX IF NOT EXISTS idx_exchanges_session ON exchanges(session_id);
        CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
        CREATE INDEX IF NOT EXISTS idx_timings_session ON pipeline_timings(session_id);
        CREATE INDEX IF NOT EXISTS idx_audio_session ON audio_metrics(session_id);
        """)
        c.commit(); c.close()


def log_pipeline_timing(session_id: int, turn_id: int, stt_ms: int, stt_provider: str,
                         llm_ttft_ms: int, llm_total_ms: int, llm_model: str,
                         tts_ms: int, tts_provider: str, playback_ms: int,
                         total_e2e_ms: int, tools_used: list, tools_parallel: int,
                         tokens_in: int, tokens_out: int, cost_usd: float, cache_hit: bool):
    with _lock:
        c = get_conn()
        c.execute("""INSERT INTO pipeline_timings(ts,session_id,turn_id,stt_ms,stt_provider,llm_ttft_ms,llm_total_ms,llm_model,tts_ms,tts_provider,playback_ms,total_e2e_ms,tools_used,tools_parallel,tokens_in,tokens_out,cost_usd,cache_hit) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (time.time(), session_id, turn_id, stt_ms, stt_provider, llm_ttft_ms, llm_total_ms, llm_model, tts_ms, tts_provider, playback_ms, total_e2e_ms, json.dumps(tools_used), tools_parallel, tokens_in, tokens_out, cost_usd, int(cache_hit)))
        c.commit(); c.close()


def log_lap_metric(session_id: int, lap_num: int, lap_time_ms: int,
                   s1_ms: int = 0, s2_ms: int = 0, s3_ms: int = 0,
                   position: int = 0, gap_ahead_ms: int = 0, gap_behind_ms: int = 0,
                   fuel_l: float = 0.0, fuel_per_lap: float = 0.0,
                   tyre_press_avg: float = 0.0, tyre_temp_avg: float = 0.0, tyre_wear_max: float = 0.0,
                   valid_lap: bool = True, track: str = "", car: str = ""):
    """V3.B : log one completed lap snapshot for trend/strategy/coaching analysis."""
    with _lock:
        c = get_conn()
        c.execute("""INSERT INTO lap_history(ts,session_id,lap_num,lap_time_ms,s1_ms,s2_ms,s3_ms,position,gap_ahead_ms,gap_behind_ms,fuel_l,fuel_per_lap,tyre_press_avg,tyre_temp_avg,tyre_wear_max,valid_lap,track,car) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (time.time(), session_id, lap_num, lap_time_ms, s1_ms, s2_ms, s3_ms,
                   position, gap_ahead_ms, gap_behind_ms, fuel_l, fuel_per_lap,
                   tyre_press_avg, tyre_temp_avg, tyre_wear_max, int(valid_lap), track, car))
        c.commit(); c.close()


def get_recent_laps(session_id: int, n: int = 5) -> list[dict]:
    """Returns last N completed laps for trend analysis."""
    with _lock:
        c = get_conn()
        rows = c.execute("SELECT * FROM lap_history WHERE session_id=? ORDER BY lap_num DESC LIMIT ?",
                         (session_id, n)).fetchall()
        c.close()
        return [dict(r) for r in reversed(rows)]


def get_best_lap_for_track_car(track: str, car: str) -> dict | None:
    """Returns best valid lap historically for this combo (driver_history coaching)."""
    with _lock:
        c = get_conn()
        r = c.execute("SELECT * FROM lap_history WHERE track=? AND car=? AND valid_lap=1 AND lap_time_ms > 0 ORDER BY lap_time_ms ASC LIMIT 1",
                      (track, car)).fetchone()
        c.close()
        return dict(r) if r else None


def log_driving_trace(session_id: int, lap_num: int, sector_index: int,
                      speed_min_kmh: float, speed_min_at_position: float,
                      brake_max_pct: float, throttle_release_position: float,
                      wheel_slip_max: float, tc_active_pct: float, abs_active_pct: float,
                      track: str = "", car: str = ""):
    """V3.O : log per-sector driving trace for coaching."""
    with _lock:
        c = get_conn()
        c.execute("""INSERT INTO driving_trace(ts,session_id,lap_num,sector_index,speed_min_kmh,speed_min_at_position,brake_max_pct,throttle_release_position,wheel_slip_max,tc_active_pct,abs_active_pct,track,car) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (time.time(), session_id, lap_num, sector_index, speed_min_kmh, speed_min_at_position,
                   brake_max_pct, throttle_release_position, wheel_slip_max, tc_active_pct, abs_active_pct,
                   track, car))
        c.commit(); c.close()


def get_driving_trace_recent(session_id: int, n: int = 20) -> list[dict]:
    """Returns recent driving trace entries for analysis."""
    with _lock:
        c = get_conn()
        rows = c.execute("SELECT * FROM driving_trace WHERE session_id=? ORDER BY ts DESC LIMIT ?",
                         (session_id, n)).fetchall()
        c.close()
        return [dict(r) for r in reversed(rows)]


def driver_history_patterns(track: str, car: str) -> dict:
    """V3.J : extract heuristic patterns from session history for this track/car combo.
    Returns insights : invalid_lap_rate, fade_over_stint, sector_weakness, best/avg cumul.
    Used to pre-brief driver : 'last time at Spa, 30% laps invalid in T1 cold, expect 3 warmup laps.'
    """
    with _lock:
        c = get_conn()
        all_laps = c.execute("SELECT lap_time_ms, valid_lap, s1_ms, s2_ms, s3_ms, lap_num FROM lap_history WHERE track=? AND car=? AND lap_time_ms > 0 ORDER BY ts DESC LIMIT 500",
                              (track, car)).fetchall()
        c.close()
    if not all_laps:
        return {"track": track, "car": car, "n_laps_history": 0, "has_history": False}
    total = len(all_laps)
    valid_laps = [l for l in all_laps if l["valid_lap"]]
    valid_rate = len(valid_laps) / total if total else 0
    times = [l["lap_time_ms"] for l in valid_laps]
    best = min(times) if times else 0
    avg = sum(times) / len(times) if times else 0
    # Sector weakness : which sector has highest delta to best
    if len(valid_laps) >= 5:
        best_lap = min(valid_laps, key=lambda l: l["lap_time_ms"])
        # Average delta per sector across all valid laps
        s1_deltas = [l["s1_ms"] - best_lap["s1_ms"] for l in valid_laps if l["s1_ms"] > 0]
        s2_deltas = [l["s2_ms"] - best_lap["s2_ms"] for l in valid_laps if l["s2_ms"] > 0]
        s3_deltas = [l["s3_ms"] - best_lap["s3_ms"] for l in valid_laps if l["s3_ms"] > 0]
        avg_s1 = sum(s1_deltas) / len(s1_deltas) if s1_deltas else 0
        avg_s2 = sum(s2_deltas) / len(s2_deltas) if s2_deltas else 0
        avg_s3 = sum(s3_deltas) / len(s3_deltas) if s3_deltas else 0
        weakness = max([("S1", avg_s1), ("S2", avg_s2), ("S3", avg_s3)], key=lambda kv: kv[1])
    else:
        weakness = (None, 0)
    # Cold tyre tendency : invalidations on lap 1-3
    early_invalids = sum(1 for l in all_laps if not l["valid_lap"] and l["lap_num"] <= 3)
    early_invalid_rate = early_invalids / total if total else 0
    # Fade over stint : avg time gap between first 3 laps vs last 3 laps in any single stint
    return {
        "track": track, "car": car,
        "n_laps_history": total,
        "has_history": True,
        "valid_lap_rate": round(valid_rate, 2),
        "best_lap_ms": best,
        "avg_lap_ms": round(avg, 0),
        "weakness_sector": weakness[0],
        "weakness_avg_delta_ms": int(weakness[1]),
        "early_invalid_rate_pct": round(early_invalid_rate * 100, 1),
        "insight": (
            f"Last {total} laps at {track}/{car}. Best {best/1000:.3f}s avg {avg/1000:.3f}s. "
            f"Weakness {weakness[0]} (+{weakness[1]/1000:.2f}s avg). "
            f"Invalid lap rate {round(valid_rate*100,0):.0f}% valid. "
            f"{round(early_invalid_rate*100,0):.0f}% early-lap invalidations (cold tyres watch)."
            if weakness[0] else f"Last {total} laps, best {best/1000:.3f}s. Need more data."
        ),
    }


def log_audio_metric(session_id: int, turn_id: int, duration_s: float, rms: float, peak: float,
                     sample_rate: int, bytes_: int, transcript_len: int, lang_detected: str | None = None):
    with _lock:
        c = get_conn()
        c.execute("""INSERT INTO audio_metrics(ts,session_id,turn_id,duration_s,rms,peak,sample_rate,bytes,transcript_len,lang_detected) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                  (time.time(), session_id, turn_id, duration_s, rms, peak, sample_rate, bytes_, transcript_len, lang_detected))
        c.commit(); c.close()


def start_session(track: str, car: str, session_type: str) -> int:
    with _lock:
        c = get_conn()
        cur = c.execute("INSERT INTO sessions(started_at, track, car, session_type) VALUES (?,?,?,?)",
                        (time.time(), track, car, session_type))
        sid = cur.lastrowid
        c.commit(); c.close()
        return sid


def end_session(session_id: int, summary: str = ""):
    with _lock:
        c = get_conn()
        c.execute("UPDATE sessions SET ended_at=?, summary=? WHERE id=?",
                  (time.time(), summary, session_id))
        c.commit(); c.close()


def get_or_resume_session(track: str, car: str, session_type: str, resume_window_s: int = 1800) -> tuple[int, bool]:
    """Bug 5 fix : si dernière session ouverte date de < resume_window_s, la réutiliser.
    Sinon créer une nouvelle. Returns (session_id, resumed_bool).
    Effets de bord : close_stale_sessions() ferme aussi les sessions orphelines."""
    now = time.time()
    with _lock:
        c = get_conn()
        # Auto-close sessions abandonnées (last activity > 2h) basé sur la dernière ts exchange/event/lap
        c.execute("""
            UPDATE sessions SET ended_at = COALESCE(
              (SELECT MAX(ts) FROM exchanges WHERE session_id=sessions.id),
              (SELECT MAX(ts) FROM events    WHERE session_id=sessions.id),
              (SELECT MAX(ts) FROM lap_history WHERE session_id=sessions.id),
              started_at
            ),
            summary = COALESCE(summary, 'auto-closed (stale > 2h)')
            WHERE ended_at IS NULL AND started_at < ?
        """, (now - 7200,))
        # Chercher session récente encore ouverte
        recent = c.execute("""SELECT id, track, car FROM sessions
                              WHERE ended_at IS NULL AND started_at >= ?
                              ORDER BY id DESC LIMIT 1""",
                           (now - resume_window_s,)).fetchone()
        if recent:
            sid = recent["id"]
            c.commit(); c.close()
            return sid, True
        # Bug 4 fix : avant de créer une nouvelle session, fermer toutes les sessions précédentes
        # orphelines (ended_at IS NULL) qui sont remplacées de fait par celle qu'on va créer.
        # ended_at = MAX(activity) sinon started_at (heuristique : on ne sait pas exactement quand
        # elles se sont arrêtées, mais elles ne sont plus actives).
        c.execute("""
            UPDATE sessions SET ended_at = COALESCE(
              (SELECT MAX(ts) FROM exchanges WHERE session_id=sessions.id),
              (SELECT MAX(ts) FROM events    WHERE session_id=sessions.id),
              (SELECT MAX(ts) FROM lap_history WHERE session_id=sessions.id),
              started_at
            ),
            summary = COALESCE(NULLIF(summary,''), 'auto-closed (superseded by newer session)')
            WHERE ended_at IS NULL
        """)
        cur = c.execute("INSERT INTO sessions(started_at, track, car, session_type) VALUES (?,?,?,?)",
                        (now, track, car, session_type))
        sid = cur.lastrowid
        c.commit(); c.close()
        return sid, False


def update_session_meta(session_id: int, track: str = "", car: str = "", session_type: str = ""):
    """Bug 2 fix : back-fill track/car/session_type sur la session si encore 'unknown'.
    No-op si la valeur passée est vide ou si la session a déjà une valeur non-'unknown'."""
    if not session_id:
        return
    with _lock:
        c = get_conn()
        sets = []
        params = []
        if track and track.lower() not in ("", "unknown", "no_track"):
            sets.append("track = CASE WHEN track IN ('unknown','','no_track') THEN ? ELSE track END")
            params.append(track)
        if car and car.lower() not in ("", "unknown", "no_car"):
            sets.append("car = CASE WHEN car IN ('unknown','','no_car') THEN ? ELSE car END")
            params.append(car)
        if session_type and session_type.lower() not in ("", "boot", "unknown"):
            sets.append("session_type = CASE WHEN session_type IN ('boot','unknown','') THEN ? ELSE session_type END")
            params.append(session_type)
        if not sets:
            c.close(); return
        params.append(session_id)
        c.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id=?", params)
        c.commit(); c.close()


def log_exchange(session_id: int, driver_msg: str, bono_msg: str,
                 model: str, tools_used: list, tokens_in: int, tokens_out: int, latency_ms: int):
    with _lock:
        c = get_conn()
        c.execute("INSERT INTO exchanges(ts,session_id,driver_msg,bono_msg,model,tools_used,tokens_in,tokens_out,latency_ms) VALUES (?,?,?,?,?,?,?,?,?)",
                  (time.time(), session_id, driver_msg, bono_msg, model, json.dumps(tools_used), tokens_in, tokens_out, latency_ms))
        c.commit(); c.close()


def log_event(session_id: int, type_: str, severity: str = "info", payload: dict | None = None):
    with _lock:
        c = get_conn()
        c.execute("INSERT INTO events(ts,session_id,type,severity,payload) VALUES (?,?,?,?,?)",
                  (time.time(), session_id, type_, severity, json.dumps(payload or {})))
        c.commit(); c.close()


def log_cost(model: str, tokens_in: int, tokens_out: int, cost_usd: float):
    with _lock:
        c = get_conn()
        c.execute("INSERT INTO costs(ts,model,tokens_in,tokens_out,cost_usd) VALUES (?,?,?,?,?)",
                  (time.time(), model, tokens_in, tokens_out, cost_usd))
        c.commit(); c.close()


def get_recent_exchanges(session_id: int, n: int = 5) -> list[dict]:
    with _lock:
        c = get_conn()
        rows = c.execute("SELECT driver_msg, bono_msg, model FROM exchanges WHERE session_id=? ORDER BY ts DESC LIMIT ?",
                         (session_id, n)).fetchall()
        c.close()
        return [dict(r) for r in reversed(rows)]


def driver_get(key: str, default: str | None = None) -> str | None:
    with _lock:
        c = get_conn()
        r = c.execute("SELECT value FROM driver WHERE key=?", (key,)).fetchone()
        c.close()
        return r["value"] if r else default


def driver_set(key: str, value: str):
    with _lock:
        c = get_conn()
        c.execute("INSERT OR REPLACE INTO driver(key,value) VALUES (?,?)", (key, value))
        c.commit(); c.close()


def stats() -> dict:
    with _lock:
        c = get_conn()
        n_sess = c.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
        n_exch = c.execute("SELECT COUNT(*) AS n FROM exchanges").fetchone()["n"]
        n_evt = c.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
        cost = c.execute("SELECT COALESCE(SUM(cost_usd),0) AS t FROM costs").fetchone()["t"]
        c.close()
        return {"sessions": n_sess, "exchanges": n_exch, "events": n_evt, "total_cost_usd": round(cost, 4)}


# Init at import
init_db()
