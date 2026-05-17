"""Bono v2 — Tools concrets ACC racing. Tous sourcent les data live via SHM snapshot partagé.

Pattern : chaque tool importe `_snap()` qui retourne le SHM dict figé pour le turn courant si activé,
sinon snapshot live.

Snapshot consistency (GPT-5.4 reco P1) :
- Pendant un turn LLM, tous les tools doivent voir le MÊME snapshot (figé au début du turn).
- Sinon : query_telemetry à T0, query_fuel à T0+1.2s = 2 valeurs différentes → réponse contradictoire.
- Le core_service appelle `set_turn_snapshot(snap)` avant le dispatch loop, et `clear_turn_snapshot()` après.
- Si pas de turn snapshot actif → fallback live (auto-events).
"""
import sys
import contextvars
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.registry import bono_tool

# Shared snapshot reference, set by core_service at runtime
_snapshot_provider = None
# Per-context frozen snapshot for the duration of one PTT turn.
# CRITICAL FIX (V2.3 cross-check 3 IA unanimous) : was threading.local() which is NOT propagated
# to ThreadPoolExecutor workers. contextvars.ContextVar IS propagated automatically. This makes
# the turn snapshot consistent across parallel tool dispatch.
_turn_snapshot_var: contextvars.ContextVar[dict | None] = contextvars.ContextVar("turn_snapshot", default=None)


def set_snapshot_provider(provider):
    """Core service injects its `get_snapshot` callable here at boot."""
    global _snapshot_provider
    _snapshot_provider = provider


def set_turn_snapshot(snap: dict | None) -> contextvars.Token:
    """Freeze a snapshot for the current turn (contextvar, propagates to ThreadPool).
    Returns token for clear_turn_snapshot()."""
    return _turn_snapshot_var.set(dict(snap) if snap is not None else None)


def clear_turn_snapshot(token: contextvars.Token | None = None):
    """Reset turn snapshot. If token provided, restore previous state precisely."""
    if token is not None:
        try:
            _turn_snapshot_var.reset(token)
        except Exception:
            _turn_snapshot_var.set(None)
    else:
        _turn_snapshot_var.set(None)


def _snap() -> dict:
    """Returns turn-frozen snapshot if active (via contextvar, propagated to ThreadPool),
    otherwise live snapshot via provider. None-safe."""
    snap = _turn_snapshot_var.get()
    if snap is not None:
        return snap
    if _snapshot_provider:
        return _snapshot_provider() or {}
    return {}


def _err(error_code: str, human_message: str, retryable: bool = False, **extra) -> dict:
    """Uniform tool error contract (GPT-5.4 reco P1) :
    All tools return either {ok: True, ...payload} OR {ok: False, error_code, human_message, retryable, ...}
    LLM is prompted (persona) to say honest 'I don't have that data' on ok=False, never fabricate.
    """
    out = {"ok": False, "error_code": error_code, "human_message": human_message, "retryable": retryable}
    out.update(extra)
    return out


def _ok(payload: dict) -> dict:
    """Uniform tool success contract."""
    return {"ok": True, **payload}


def _fmt_lap(ms) -> str:
    """Lap time ms → 'mm:ss.f' compact non-ambigu (anti-hallucination LLM '1:10.5' bug
    Gemini diag 17/05). Tools doivent retourner strings pré-formatées en plus du raw int."""
    if not ms or ms <= 0:
        return "?:??.?"
    total_s = ms / 1000.0
    mins = int(total_s // 60)
    secs = total_s - mins * 60
    return f"{mins}:{secs:04.1f}"


# Fix bug A/B brief V3 17/05 nuit : session-aware setup file selection.
# Avant : query_setup_state et bono_engineer_setup_update_acc utilisaient `max(files, key=mtime)`
# → si le dernier fichier modifié était _Wet.json, Bono croyait que Wet était actif en RACE sec.
# Maintenant : match par suffixe dans le nom de fichier selon session_type + weather.
SESSION_FILE_SUFFIXES = {
    # session_type → liste de suffixes acceptables, par ordre de priorité
    "RACE":     ["_Race", "_race"],
    "QUALIFY":  ["_Qualif", "_Qualifying", "_Quali", "_qualif"],
    "PRACTICE": ["_Practice", "_practice"],
    "HOTLAP":   ["_Hotlap", "_hotlap"],
    "HOTSTINT": ["_Hotstint", "_hotstint"],
}
# Suffixes wet (priorité sur session_type si pluie détectée)
WET_SUFFIXES = ["_Wet", "_wet", "_Rain", "_rain"]


def match_setup_file_for_session(setup_files: list[str], session_type: str = "", is_wet: bool = False) -> str | None:
    """Returns the setup file path best matching the current session.
    Priority : (1) wet match if is_wet, (2) session_type suffix, (3) fallback most-recent.
    setup_files : list of full paths to .json (excl. .bak)."""
    if not setup_files:
        return None
    # Use file basename for matching
    import os as _o
    if is_wet:
        for suf in WET_SUFFIXES:
            for f in setup_files:
                bn = _o.path.basename(f)
                if suf in bn and not bn.lower().endswith('.bak.json'):
                    return f
    session_upper = (session_type or "").upper()
    for suf in SESSION_FILE_SUFFIXES.get(session_upper, []):
        for f in setup_files:
            bn = _o.path.basename(f)
            if suf in bn:
                return f
    # Fallback : si un fichier match exactly "Setup" (generic), sinon mtime le plus récent
    for f in setup_files:
        bn = _o.path.basename(f).lower()
        if "_setup" in bn and not any(suf.lower() in bn for suf in WET_SUFFIXES):
            return f
    return max(setup_files, key=_o.path.getmtime)


# ============================================================
# query_telemetry
# ============================================================
@bono_tool(
    name="query_telemetry",
    description="Get COMPACT live ACC telemetry summary (~20 essential fields). USE WHEN you need values FRESHER than the system context snapshot (during long turns >10s when telemetry may have moved). DON'T USE for static info already in system context (track, car, best_lap, current_fuel) — those are pre-injected in fat context.",
    parameters={"type": "object", "properties": {}, "required": []}
)
def query_telemetry() -> dict:
    """Filtré : ~20 fields essentiels au lieu de 60+ pour éviter explosion tokens Anthropic (Gemini #4 audit)."""
    s = _snap()
    if not s.get("shm_ok"):
        return _err("shm_unavailable", "Pas de télémétrie SHM disponible. ACC lancé ?", retryable=True)
    if s.get("status") not in ("LIVE", "REPLAY"):
        return _err("shm_not_live", f"ACC status={s.get('status')} (pas en session live)", retryable=True, status=s.get("status"))
    tyre_press_avg = round(sum(s.get(f"tyre_press_{p}", 0) for p in ("fl","fr","rl","rr")) / 4, 2)
    tyre_temp_avg = round(sum(s.get(f"tyre_temp_{p}", 0) for p in ("fl","fr","rl","rr")) / 4, 1)
    return _ok({
        "track": s.get("track"), "car": s.get("car"), "session": s.get("session"),
        "speed_kmh": round(s.get("speed_kmh", 0), 1),
        "rpm": s.get("rpm"), "gear": s.get("gear"),
        "fuel_l": round(s.get("fuel_l", 0), 2),
        "fuel_laps_remaining": round(s.get("fuel_estimated_laps", 0), 1),
        "current_lap": s.get("completed_laps", 0) + 1,
        "position": s.get("position"),
        "last_lap_ms": s.get("last_time_ms"),
        "last_lap": _fmt_lap(s.get("last_time_ms") or 0),
        "best_lap_ms": s.get("best_time_ms"),
        "best_lap": _fmt_lap(s.get("best_time_ms") or 0),
        "delta_lap_ms": s.get("delta_lap_ms"),
        "tyre_press_psi_avg": tyre_press_avg,
        "tyre_temp_c_avg": tyre_temp_avg,
        "brake_bias_pct": round(s.get("brake_bias", 0), 1),
        "is_in_pit": s.get("is_in_pit"),
        "global_yellow": s.get("global_yellow"),
        "global_red": s.get("global_red"),
        "rain_intensity": s.get("rain_intensity"),
    })


# ============================================================
# query_fuel_strategy
# ============================================================
@bono_tool(
    name="query_fuel_strategy",
    description="Compute fuel strategy : laps remaining with current fuel, fuel save needed, pit lap window. Use when driver asks about fuel margin or pit strategy.",
    parameters={"type": "object", "properties": {"laps_to_finish": {"type": "integer", "description": "Target laps to finish race (0 if N/A)"}}, "required": []}
)
def query_fuel_strategy(laps_to_finish: int = 0) -> dict:
    s = _snap()
    if not s.get("shm_ok"):
        return _err("shm_unavailable", "Pas de SHM dispo, ACC pas lancé.", retryable=True)
    fuel = s.get("fuel_l", 0); per_lap = s.get("fuel_per_lap", 0)
    if fuel <= 0.1 or per_lap <= 0.05:
        return _err("no_fuel_telemetry", "Pas de data fuel valide encore (besoin 1+ tour complet pour mesurer per_lap).", retryable=True, fuel=fuel, per_lap=per_lap)
    laps_left = fuel / per_lap
    payload = {
        "fuel_now_l": round(fuel, 2),
        "fuel_per_lap_l": round(per_lap, 2),
        "laps_remaining": round(laps_left, 1),
    }
    if laps_to_finish > 0:
        deficit = laps_to_finish - laps_left
        if deficit > 0:
            payload["status"] = "short"; payload["save_per_lap_l"] = round(deficit * per_lap / laps_to_finish, 2)
            payload["save_per_lap_pct"] = round(payload["save_per_lap_l"] / per_lap * 100, 1)
        else:
            payload["status"] = "ok"; payload["margin_laps"] = round(-deficit, 1)
    return _ok(payload)


# ============================================================
# query_fuel_for_session_plan : combien de litres embarquer pré-session
# ============================================================
@bono_tool(
    name="query_fuel_for_session_plan",
    description="Compute fuel litres to load BEFORE a session starts. Driver asks 'combien d'essence pour ce sprint/race/quali ?'. Uses session duration + track avg lap + avg fuel/lap + safety margin.",
    parameters={
        "type": "object",
        "properties": {
            "session_type": {"type": "string", "enum": ["practice", "qualifying", "sprint", "race", "endurance", "hotlap"], "description": "Session type"},
            "duration_min": {"type": "number", "description": "Session duration in minutes (e.g. 20 for sprint, 60 for race, 30 for quali)"},
            "track": {"type": "string", "description": "Track name (e.g. zolder, monza). If empty, uses live SHM."},
            "fuel_per_lap_override": {"type": "number", "description": "Custom fuel/lap if known (optional, else uses track default ~3.0)"},
            "safety_margin_pct": {"type": "number", "description": "Extra fuel margin in % (default 10%, more for race)"}
        },
        "required": ["session_type", "duration_min"]
    }
)
def query_fuel_for_session_plan(session_type: str, duration_min: float, track: str = "",
                                  fuel_per_lap_override: float = 0, safety_margin_pct: float = 10) -> dict:
    if duration_min <= 0:
        return _err("invalid_duration", "duration_min must be > 0", retryable=False)
    if not track:
        s = _snap()
        track = (s.get("track") or "").strip() if s.get("shm_ok") else ""
    if not track:
        return _err("track_required", "Track non spécifié et SHM pas dispo", retryable=False)
    avg_lap_s = _get_avg_lap_s(track)
    fuel_per_lap = fuel_per_lap_override if fuel_per_lap_override > 0 else _get_avg_fuel_per_lap(track)
    # Estimated laps in session
    laps_estimated = (duration_min * 60) / avg_lap_s
    # +1 out-lap + 1 in-lap typical
    laps_with_inout = laps_estimated + 2
    # Base fuel
    fuel_base = laps_with_inout * fuel_per_lap
    # Safety margin (more for race vs hotlap)
    base_margin = safety_margin_pct
    if session_type.lower() in ("race", "endurance"):
        base_margin = max(safety_margin_pct, 12)
    elif session_type.lower() in ("qualifying", "hotlap"):
        base_margin = min(safety_margin_pct, 5)  # quali = juste qq laps push, pas besoin de marge
    fuel_with_margin = fuel_base * (1 + base_margin / 100)
    # Round up to nearest litre (always over-fuel rather than under)
    import math
    fuel_to_load = math.ceil(fuel_with_margin)
    # Cap to typical GT3 tank ~120 L
    GT3_TANK_MAX_L = 120
    capped = fuel_to_load > GT3_TANK_MAX_L
    if capped:
        fuel_to_load = GT3_TANK_MAX_L
    return _ok({
        "session_type": session_type,
        "track": track,
        "duration_min": duration_min,
        "avg_lap_s": round(avg_lap_s, 1),
        "fuel_per_lap_l": round(fuel_per_lap, 2),
        "laps_estimated": round(laps_estimated, 1),
        "laps_with_in_out": round(laps_with_inout, 1),
        "safety_margin_pct": base_margin,
        "fuel_recommended_l": fuel_to_load,
        "fuel_base_l": round(fuel_base, 1),
        "fuel_with_margin_l": round(fuel_with_margin, 1),
        "tank_max_capped": capped,
        "tank_max_l": GT3_TANK_MAX_L if capped else None,
        "advice": (
            f"Sur {track}, pour {session_type} de {duration_min} min, embarquer {fuel_to_load} L "
            f"(~{round(laps_with_inout, 0):.0f} laps à {fuel_per_lap:.1f} L/lap, marge {base_margin}%). "
            + ("PIT obligatoire en course longue, tank cappé." if capped else "OK sans pit forcé.")
        ),
    })


# ============================================================
# query_tire_state
# ============================================================
@bono_tool(
    name="query_tire_state",
    description="Get DETAILED tyre state per wheel (pressure, temp, wear, optimal window assessment). USE WHEN driver asks 'how are the tyres ?' or after tyre_cliff event. DON'T USE for the global tyre temps/pressures avg already in fat context. Get current tyre pressures, temperatures, wear with quick assessment (in optimal window or not for Pirelli DHF GT3). Use when driver asks pneus / tyres status.",
    parameters={"type": "object", "properties": {}, "required": []}
)
def query_tire_state() -> dict:
    """V3.N : pressure status now car-specific (BMW M4 target 26.6 FL/FR, Porsche 992 26.6 front/27.0 rear, etc.)"""
    s = _snap()
    if not s.get("shm_ok"):
        return _err("shm_unavailable", "Pas de SHM dispo.", retryable=True)
    pressures = [s.get(f"tyre_press_{p}", 0) for p in ("fl","fr","rl","rr")]
    temps = [s.get(f"tyre_temp_{p}", 0) for p in ("fl","fr","rl","rr")]
    wear = [s.get(f"tyre_wear_{p}", 0) for p in ("fl","fr","rl","rr")]
    if all(p <= 0.5 for p in pressures):
        return _err("no_tyre_data", "Pressions pneus à zéro = pas de telemetry pneus encore.", retryable=True)
    # V3.N — load car-specific target pressures
    car_target = None
    try:
        from tools.acc_knowledge import get_car_knowledge
        car = (s.get("car") or "").strip()
        if car:
            kb = get_car_knowledge(car)
            if kb:
                car_target = kb.get("tyre_pressure_target_hot_psi")
    except Exception:
        pass
    # Default fallback Pirelli DHF (26.8 PSI per axle)
    targets = car_target or {"FL": 26.8, "FR": 26.8, "RL": 26.8, "RR": 26.8}
    pos_map = [("FL", pressures[0]), ("FR", pressures[1]), ("RL", pressures[2]), ("RR", pressures[3])]
    press_status = []
    for label, p in pos_map:
        if p == 0:
            press_status.append("no_data"); continue
        target = targets.get(label, 26.8)
        delta = p - target
        if p < target - 1.0: press_status.append("critical_low")
        elif p < target - 0.4: press_status.append("low")
        elif p > target + 1.5: press_status.append("critical_high")
        elif p > target + 0.2: press_status.append("high")
        else: press_status.append("optimal")
    temp_avg = sum(temps) / 4 if all(t > 0 for t in temps) else 0
    return _ok({
        "pressures_psi": [round(p, 2) for p in pressures],
        "pressure_target_psi": targets,
        "pressure_status": press_status,
        "pressure_status_by_corner": {"FL": press_status[0], "FR": press_status[1], "RL": press_status[2], "RR": press_status[3]},
        "temperatures_c": [round(t, 1) for t in temps],
        "temp_avg_c": round(temp_avg, 1),
        "temp_assessment": "hot" if temp_avg > 95 else ("cold" if 0 < temp_avg < 70 else "in_window"),
        "wear_pct": [round(w * 100, 1) for w in wear],
        "compound": s.get("tyre_compound"),
        "car_specific_target_used": bool(car_target),
    })


# ============================================================
# query_opponents
# ============================================================
@bono_tool(
    name="query_opponents",
    description="Get nearby opponents (gap ahead/behind in ms + opponent IDs). USE WHEN driver asks 'who is behind/ahead', or for strategic call. DON'T USE for the simple gap values already in fat context (gap_ahead, gap_behind). Get nearby opponents (gap ahead, gap behind in ms). Use when driver asks who's behind/ahead, gap, position.",
    parameters={"type": "object", "properties": {}, "required": []}
)
def query_opponents() -> dict:
    s = _snap()
    if not s.get("shm_ok"):
        return _err("shm_unavailable", "Pas de SHM dispo.", retryable=True)
    return _ok({
        "position": s.get("position"),
        "active_cars": s.get("active_cars"),
        "gap_ahead_ms": s.get("gap_ahead_ms"),
        "gap_behind_ms": s.get("gap_behind_ms"),
    })


# ============================================================
# query_weather
# ============================================================
@bono_tool(
    name="query_weather",
    description="Get current + forecast weather : air/road temp, rain intensity now / in 10min / in 30min, wind, track grip status.",
    parameters={"type": "object", "properties": {}, "required": []}
)
def query_weather() -> dict:
    s = _snap()
    if not s.get("shm_ok"):
        return _err("shm_unavailable", "Pas de SHM dispo.", retryable=True)
    return _ok({
        "air_temp_c": round(s.get("air_temp", 0), 1),
        "road_temp_c": round(s.get("road_temp", 0), 1),
        "rain_now": s.get("rain_intensity"),
        "rain_in_10min": s.get("rain_intensity_in_10min"),
        "rain_in_30min": s.get("rain_intensity_in_30min"),
        "wind_speed_kmh": round(s.get("wind_speed", 0), 1),
        "wind_direction_deg": round(s.get("wind_direction", 0), 0),
        "track_grip_status": s.get("track_grip_status"),
        "rain_tyres_on": s.get("rain_tyres"),
    })


# ============================================================
# query_session_state
# ============================================================
@bono_tool(
    name="query_session_state",
    description="Get session info: type, status, time left, completed laps, position. DON'T USE if you just need track/car/session_type/best_lap (those are in fat context). USE WHEN driver asks 'how much time left ?' or status changed recently. Get session info: type, status, time left, completed laps, flags.",
    parameters={"type": "object", "properties": {}, "required": []}
)
def query_session_state() -> dict:
    s = _snap()
    if not s.get("shm_ok"):
        return _err("shm_unavailable", "Pas de SHM dispo.", retryable=True)
    return _ok({
        "session": s.get("session"),
        "status": s.get("status"),
        "session_time_left_s": round(s.get("session_time_left_s", 0), 1),
        "completed_laps": s.get("completed_laps"),
        "valid_lap": s.get("valid_lap"),
        "is_in_pit": s.get("is_in_pit"),
        "global_yellow": s.get("global_yellow"),
        "global_red": s.get("global_red"),
        "global_chequered": s.get("global_chequered"),
        "flag_code": s.get("flag"),
        "track_status": s.get("track_status"),
    })


# ============================================================
# bono_engineer_setup_update_acc
# ============================================================
SETUP_FIELD_MAP = {
    # field name → (JSON path list, delta scale, allowed range [min,max])
    # ACC values are CLICKS from minimum (not absolute). Verified via mémoire feedback_acc_setup_format.
    # V2.3 EXTENDED — 36 champs : ajout suspensions, dampers fast/slow, toe, caster, brake_ducts, brake_pads, tyre_set, steer_ratio
    # === MECHANICAL / AERO ===
    "brake_bias":         (["advancedSetup", "mechanicalBalance", "brakeBias"], 1, [0, 200]),
    "splitter":           (["advancedSetup", "aeroBalance", "splitter"], 1, [0, 5]),
    "rear_wing":          (["advancedSetup", "aeroBalance", "wing"], 1, [0, 12]),
    "ride_height_front":  (["advancedSetup", "aeroBalance", "rideHeight", 0], 1, [0, 30]),
    "ride_height_rear":   (["advancedSetup", "aeroBalance", "rideHeight", 1], 1, [0, 30]),
    "arb_front":          (["advancedSetup", "mechanicalBalance", "aRBFront"], 1, [0, 16]),
    "arb_rear":           (["advancedSetup", "mechanicalBalance", "aRBRear"], 1, [0, 16]),
    "preload":            (["advancedSetup", "drivetrain", "preload"], 1, [0, 14]),
    "wheel_rate_lf":      (["advancedSetup", "mechanicalBalance", "wheelRate", 0], 1, [0, 30]),
    "wheel_rate_rf":      (["advancedSetup", "mechanicalBalance", "wheelRate", 1], 1, [0, 30]),
    "wheel_rate_lr":      (["advancedSetup", "mechanicalBalance", "wheelRate", 2], 1, [0, 30]),
    "wheel_rate_rr":      (["advancedSetup", "mechanicalBalance", "wheelRate", 3], 1, [0, 30]),
    "bump_stop_rate_lf":  (["advancedSetup", "mechanicalBalance", "bumpStopRateUp", 0], 1, [0, 30]),
    "bump_stop_rate_rf":  (["advancedSetup", "mechanicalBalance", "bumpStopRateUp", 1], 1, [0, 30]),
    "bump_stop_rate_lr":  (["advancedSetup", "mechanicalBalance", "bumpStopRateUp", 2], 1, [0, 30]),
    "bump_stop_rate_rr":  (["advancedSetup", "mechanicalBalance", "bumpStopRateUp", 3], 1, [0, 30]),
    "bump_stop_window_lf":(["advancedSetup", "mechanicalBalance", "bumpStopWindow", 0], 1, [0, 30]),
    "bump_stop_window_rf":(["advancedSetup", "mechanicalBalance", "bumpStopWindow", 1], 1, [0, 30]),
    "bump_stop_window_lr":(["advancedSetup", "mechanicalBalance", "bumpStopWindow", 2], 1, [0, 30]),
    "bump_stop_window_rr":(["advancedSetup", "mechanicalBalance", "bumpStopWindow", 3], 1, [0, 30]),
    "steer_ratio":        (["advancedSetup", "mechanicalBalance", "steerRatio"], 1, [0, 16]),
    "brake_power":        (["advancedSetup", "mechanicalBalance", "brakeTorque"], 1, [0, 100]),
    # === DAMPERS (4 corners × 4 valves = 16 fields) ===
    "damper_bump_slow_lf":   (["advancedSetup", "dampers", "bumpSlow", 0], 1, [0, 20]),
    "damper_bump_slow_rf":   (["advancedSetup", "dampers", "bumpSlow", 1], 1, [0, 20]),
    "damper_bump_slow_lr":   (["advancedSetup", "dampers", "bumpSlow", 2], 1, [0, 20]),
    "damper_bump_slow_rr":   (["advancedSetup", "dampers", "bumpSlow", 3], 1, [0, 20]),
    "damper_bump_fast_lf":   (["advancedSetup", "dampers", "bumpFast", 0], 1, [0, 20]),
    "damper_bump_fast_rf":   (["advancedSetup", "dampers", "bumpFast", 1], 1, [0, 20]),
    "damper_bump_fast_lr":   (["advancedSetup", "dampers", "bumpFast", 2], 1, [0, 20]),
    "damper_bump_fast_rr":   (["advancedSetup", "dampers", "bumpFast", 3], 1, [0, 20]),
    "damper_reb_slow_lf":    (["advancedSetup", "dampers", "reboundSlow", 0], 1, [0, 20]),
    "damper_reb_slow_rf":    (["advancedSetup", "dampers", "reboundSlow", 1], 1, [0, 20]),
    "damper_reb_slow_lr":    (["advancedSetup", "dampers", "reboundSlow", 2], 1, [0, 20]),
    "damper_reb_slow_rr":    (["advancedSetup", "dampers", "reboundSlow", 3], 1, [0, 20]),
    "damper_reb_fast_lf":    (["advancedSetup", "dampers", "reboundFast", 0], 1, [0, 20]),
    "damper_reb_fast_rf":    (["advancedSetup", "dampers", "reboundFast", 1], 1, [0, 20]),
    "damper_reb_fast_lr":    (["advancedSetup", "dampers", "reboundFast", 2], 1, [0, 20]),
    "damper_reb_fast_rr":    (["advancedSetup", "dampers", "reboundFast", 3], 1, [0, 20]),
    # === ALIGNMENT (camber + toe) ===
    "front_camber_lf":    (["basicSetup", "alignment", "camber", 0], 1, [-50, 0]),
    "front_camber_rf":    (["basicSetup", "alignment", "camber", 1], 1, [-50, 0]),
    "rear_camber_lr":     (["basicSetup", "alignment", "camber", 2], 1, [-50, 0]),
    "rear_camber_rr":     (["basicSetup", "alignment", "camber", 3], 1, [-50, 0]),
    "front_toe_lf":       (["basicSetup", "alignment", "toe", 0], 1, [-50, 50]),
    "front_toe_rf":       (["basicSetup", "alignment", "toe", 1], 1, [-50, 50]),
    "rear_toe_lr":        (["basicSetup", "alignment", "toe", 2], 1, [-50, 50]),
    "rear_toe_rr":        (["basicSetup", "alignment", "toe", 3], 1, [-50, 50]),
    "front_caster_lf":    (["basicSetup", "alignment", "casterLF"], 1, [0, 100]),
    "front_caster_rf":    (["basicSetup", "alignment", "casterRF"], 1, [0, 100]),
    # === TYRES (pressure + tyre set + compound) ===
    "tyre_pressure_fl":   (["basicSetup", "tyres", "tyrePressure", 0], 1, [0, 30]),
    "tyre_pressure_fr":   (["basicSetup", "tyres", "tyrePressure", 1], 1, [0, 30]),
    "tyre_pressure_rl":   (["basicSetup", "tyres", "tyrePressure", 2], 1, [0, 30]),
    "tyre_pressure_rr":   (["basicSetup", "tyres", "tyrePressure", 3], 1, [0, 30]),
    "tyre_set":           (["basicSetup", "tyres", "tyreSet"], 1, [0, 50]),
    "tyre_compound":      (["basicSetup", "tyres", "tyreCompound"], 1, [0, 1]),  # 0=dry 1=wet
    # === ELECTRONICS ===
    "tc1":                (["basicSetup", "electronics", "tC1"], 1, [0, 11]),
    "tc2":                (["basicSetup", "electronics", "tC2"], 1, [0, 11]),
    "abs":                (["basicSetup", "electronics", "abs"], 1, [0, 11]),
    "ecu_map":            (["basicSetup", "electronics", "ecuMap"], 1, [0, 11]),
    "fuel_mix":           (["basicSetup", "electronics", "fuelMix"], 1, [0, 5]),
    # === STRATEGY === (fix 17/05 nuit : ajout tyre_set + brake pad compound — full pre-race strategy editable)
    "fuel":               (["basicSetup", "strategy", "fuel"], 1, [0, 120]),
    "n_pit_stops":        (["basicSetup", "strategy", "nPitStops"], 1, [0, 4]),
    "tyre_set":           (["basicSetup", "strategy", "tyreSet"], 1, [0, 49]),
    "front_brake_pad":    (["basicSetup", "strategy", "frontBrakePadCompound"], 1, [0, 3]),
    "rear_brake_pad":     (["basicSetup", "strategy", "rearBrakePadCompound"], 1, [0, 3]),
    # === BRAKES ===
    "brake_duct_front":   (["advancedSetup", "mechanicalBalance", "brakeDuct", 0], 1, [0, 6]),
    "brake_duct_rear":    (["advancedSetup", "mechanicalBalance", "brakeDuct", 1], 1, [0, 6]),
    "brake_pads_front":   (["basicSetup", "brakeSystem", "brakePadCompoundFront"], 1, [0, 3]),
    "brake_pads_rear":    (["basicSetup", "brakeSystem", "brakePadCompoundRear"], 1, [0, 3]),
}


@bono_tool(
    name="bono_plan_race_fuel_pits",
    description="""ALL-IN-ONE pre-race fuel + pit strategy planner. Calcule fuel optimal + nb pit stops + applique au setup ACC.

USE FOR:
- Driver demande "Bono prépare la stratégie pour cette course de 30/60/120 min"
- Avant un départ race : auto-calc fuel à charger + nb pit stops + applique au setup file
- Endurance : compute multi-stint avec marges adaptées

DON'T USE FOR:
- Pit stop ACTION en course (fuel à ajouter / tyre change) → ça se fait via MFD ingame (Bono ne peut pas pousser touches MFD)
- Just check fuel needed → use query_fuel_for_session_plan (read-only)

Output : detailed plan + applies fuel/n_pit_stops to current Race setup file.""",
    parameters={"type": "object", "properties": {
        "duration_min": {"type": "number", "description": "Race duration in minutes"},
        "max_pit_stops": {"type": "integer", "description": "Max pit stops planifies (default 1 pour sprint, 2-3 endurance)"},
        "safety_margin_pct": {"type": "number", "description": "Extra fuel safety margin % (default 10)"},
        "apply": {"type": "boolean", "description": "Apply au setup ACC (default true). False = dry-run report only."},
    }, "required": ["duration_min"]}
)
def bono_plan_race_fuel_pits(duration_min: float, max_pit_stops: int = 1, safety_margin_pct: float = 10, apply: bool = True) -> dict:
    """Fix 17/05 nuit : tool integre pour planifier la strategie complete race avant le start."""
    import math
    s = _snap()
    if not s.get("shm_ok"):
        return _err("shm_unavailable", "ACC pas en LIVE", retryable=True)
    car = (s.get("car") or "").strip()
    track = (s.get("track") or "").strip()
    fuel_per_lap = s.get("fuel_per_lap", 0) or 0
    if fuel_per_lap < 0.5:
        # Fallback track default
        fuel_per_lap = _get_avg_fuel_per_lap(track)
    # Track lap time estimate
    try:
        from tools.acc_knowledge import get_track_knowledge
        track_kb = get_track_knowledge(track) or {}
        avg_lap_s = track_kb.get("avg_lap_time_s") or track_kb.get("typical_lap_s") or 100
    except Exception:
        avg_lap_s = 100
    # Total laps
    total_laps = math.ceil((duration_min * 60) / avg_lap_s) + 1  # +1 for safety/in-lap
    total_fuel_needed = total_laps * fuel_per_lap * (1 + safety_margin_pct / 100)
    # Tank capacity GT3 ~120L
    tank_max = 120
    # Calcul stops
    n_stops = max(0, min(max_pit_stops, math.ceil(total_fuel_needed / tank_max) - 1))
    fuel_start = math.ceil(min(total_fuel_needed / (n_stops + 1), tank_max))
    plan = {
        "track": track, "car": car,
        "duration_min": duration_min,
        "estimated_total_laps": total_laps,
        "fuel_per_lap_l": round(fuel_per_lap, 2),
        "avg_lap_s": avg_lap_s,
        "total_fuel_needed_l": math.ceil(total_fuel_needed),
        "n_pit_stops_recommended": n_stops,
        "fuel_at_start_l": fuel_start,
        "tank_max_l": tank_max,
        "stints_estimated_laps": [math.ceil(total_laps / (n_stops + 1))] * (n_stops + 1) if n_stops >= 0 else [total_laps],
        "applied": False,
    }
    if apply:
        # Apply via bono_engineer_setup_update_acc
        try:
            r_fuel = bono_engineer_setup_update_acc(field="fuel", absolute_value=fuel_start)
            r_pits = bono_engineer_setup_update_acc(field="n_pit_stops", absolute_value=n_stops)
            plan["fuel_apply_result"] = r_fuel
            plan["pits_apply_result"] = r_pits
            plan["applied"] = bool(r_fuel.get("ok") and r_pits.get("ok"))
        except Exception as e:
            plan["apply_error"] = str(e)
    return _ok(plan)


@bono_tool(
    name="bono_engineer_setup_update_acc",
    description="Modify the current ACC setup JSON file. Auto backup pre-edit. Range-checked. field ∈ SETUP_FIELD_MAP (44 fields). Pass either delta OR absolute_value (not both). delta = signed clicks (+1/-2). absolute_value = target absolute clicks value (e.g. fuel=61).",
    parameters={"type": "object", "properties": {
        "field": {"type": "string", "description": "Setup field name (see SETUP_FIELD_MAP)"},
        "delta": {"type": "integer", "description": "Signed delta in clicks (e.g. +1, -2, +3). Use 0 if absolute_value provided."},
        "absolute_value": {"type": "integer", "description": "Optional target absolute clicks value (e.g. 61 for fuel). If provided, computes delta auto."},
    }, "required": ["field"]}
)
def bono_engineer_setup_update_acc(field: str, delta: float = 0, absolute_value: int | None = None) -> dict:
    import json as _json, shutil as _shutil, time as _t, os as _o
    if field not in SETUP_FIELD_MAP:
        return {"ok": False, "error_code": "unknown_field", "retryable": False,
                "human_message": f"Champ '{field}' inconnu. Allowed: {', '.join(SETUP_FIELD_MAP.keys())}"}
    snap = _snap()
    car = (snap.get("car") or "").strip()
    track = (snap.get("track") or "").strip()
    if not car or not track:
        return {"ok": False, "error_code": "no_car_or_track_in_shm", "retryable": True,
                "human_message": "Pas de car/track détecté dans SHM, ACC pas lancé ?"}
    setup_dir = _o.path.expanduser(rf"~\Documents\Assetto Corsa Competizione\Setups\{car}\{track}")
    if not _o.path.isdir(setup_dir):
        return {"ok": False, "error_code": "setup_dir_not_found", "retryable": False,
                "human_message": f"Dossier setup introuvable : {setup_dir}", "path": setup_dir}
    # Find session-matching .json (= current setup correct) — Fix bug B 17/05 nuit.
    jsons = [_o.path.join(setup_dir, f) for f in _o.listdir(setup_dir) if f.endswith(".json") and not f.endswith(".bak.json")]
    if not jsons:
        return {"ok": False, "error_code": "no_json_setup_in_dir", "retryable": False,
                "human_message": f"Aucun setup .json dans {setup_dir}", "path": setup_dir}
    # Session-aware match : avant ce fix, on prenait `jsons.sort(mtime)[0]` → modif sur le mauvais
    # fichier (ex : _Wet.json modifié alors qu'on tournait en RACE). Désormais on cherche le suffixe
    # qui matche le session_type + wet detection.
    session_type = (snap.get("session") or "").upper()
    is_wet = (snap.get("rain_intensity", 0) or 0) > 0.05
    target = match_setup_file_for_session(jsons, session_type=session_type, is_wet=is_wet)
    if not target:
        jsons.sort(key=_o.path.getmtime, reverse=True)
        target = jsons[0]  # ultime fallback
    # Backup
    backup_dir = _o.path.join(setup_dir, ".backups")
    _o.makedirs(backup_dir, exist_ok=True)
    bk_name = _o.path.basename(target).replace(".json", f".{int(_t.time())}.bak.json")
    bk_path = _o.path.join(backup_dir, bk_name)
    try:
        _shutil.copy2(target, bk_path)
    except Exception as e:
        return {"ok": False, "error_code": "backup_fail", "retryable": True,
                "human_message": f"Backup pre-edit échoué : {e}"}
    # Edit
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = _json.load(f)
        path_keys, scale, rng = SETUP_FIELD_MAP[field]
        # Walk path
        node = data
        for k in path_keys[:-1]:
            if isinstance(k, int):
                node = node[k]
            else:
                if k not in node:
                    return {"ok": False, "error_code": "path_not_in_json", "retryable": False,
                            "human_message": f"Path JSON '{'.'.join(map(str, path_keys))}' inexistant dans setup (voiture probablement incompatible avec ce champ).",
                            "missing_key": k, "available_keys": list(node.keys())}
                node = node[k]
        last = path_keys[-1]
        old_val = node[last]
        # V5 : si absolute_value passé, calcule le delta auto (set absolu)
        if absolute_value is not None:
            new_val = int(absolute_value)
            effective_delta = new_val - old_val
        else:
            new_val = old_val + int(delta * scale)
            effective_delta = delta
        # Range check (anti-fabrication)
        if not (rng[0] <= new_val <= rng[1]):
            return {"ok": False, "error_code": "value_out_of_range", "retryable": False,
                    "human_message": f"Valeur {new_val} hors range autorisé {rng} pour {field}. Delta réduit nécessaire.",
                    "old": old_val, "would_be_new": new_val, "allowed_range": rng}
        node[last] = new_val
        with open(target, "w", encoding="utf-8") as f:
            _json.dump(data, f, indent=4)
        # Phase B1 brief V3 17/05 : audit trail des modifications setup
        try:
            mem = _memory()
            sid = None
            try:
                import sys as _sys
                if 'core_service' in _sys.modules:
                    sid = _sys.modules['core_service']._state.session_id
            except Exception: pass
            mem.log_setup_change(session_id=sid, turn_id=None, track=track, car=car,
                                 setup_file=_o.path.basename(target), field=field,
                                 old_value=old_val, new_value=new_val, source="ptt", success=True)
        except Exception as _e:
            pass  # never block on audit log fail
        return {"ok": True, "field": field, "delta": effective_delta, "old": old_val, "new": new_val,
                "absolute_mode": absolute_value is not None,
                "file": _o.path.basename(target), "backup": bk_name, "in_range": rng}
    except Exception as e:
        # restore backup on failure
        try: _shutil.copy2(bk_path, target)
        except Exception: pass
        # Audit trail aussi en cas d'erreur (visibilité)
        try:
            mem = _memory()
            sid = None
            import sys as _sys
            if 'core_service' in _sys.modules:
                sid = _sys.modules['core_service']._state.session_id
            mem.log_setup_change(session_id=sid, turn_id=None, track=track, car=car,
                                 setup_file=_o.path.basename(target) if 'target' in locals() else '',
                                 field=field, old_value=None, new_value=None,
                                 source="ptt", success=False, error_msg=str(e)[:200])
        except Exception: pass
        return {"ok": False, "error_code": "edit_fail", "retryable": True,
                "human_message": f"Edit raté, backup restauré. Erreur : {e}"}


# ============================================================
# query_setup_state : lecture seule du setup ACC courant
# ============================================================
@bono_tool(
    name="query_setup_state",
    description="Read current ACC setup file (read-only). Returns key setup values (fuel, ARB, TC/ABS, brake bias, tyre pressure, ride height, etc.) without modifying anything.",
    parameters={"type": "object", "properties": {
        "fields": {"type": "array", "items": {"type": "string"}, "description": "Optional list of specific fields to return. Empty = all key fields."}
    }, "required": []}
)
def query_setup_state(fields: list | None = None) -> dict:
    import os as _o, json as _json, glob as _g
    # Fix bug A 17/05 nuit : lire le BON fichier selon session_type + weather, pas mtime.
    s = _snap()
    car = (s.get("car") or "").strip()
    track = (s.get("track") or "").strip()
    session_type = (s.get("session") or "").upper()
    is_wet = (s.get("rain_intensity", 0) or 0) > 0.05
    cm_dir = _o.path.expanduser(r"~\Documents\Assetto Corsa Competizione\Setups")
    if not _o.path.isdir(cm_dir):
        return _err("setup_dir_missing", "ACC Setups dir absent", retryable=False)
    # Si on connaît car+track, scope aux fichiers de ce dossier uniquement
    if car and track:
        scope_dir = _o.path.join(cm_dir, car, track)
        if _o.path.isdir(scope_dir):
            files = [_o.path.join(scope_dir, f) for f in _o.listdir(scope_dir)
                     if f.endswith(".json") and not f.endswith(".bak.json")]
        else:
            files = _g.glob(_o.path.join(cm_dir, "**", "*.json"), recursive=True)
            files = [f for f in files if not f.endswith(".bak.json")]
    else:
        files = _g.glob(_o.path.join(cm_dir, "**", "*.json"), recursive=True)
        files = [f for f in files if not f.endswith(".bak.json")]
    if not files:
        return _err("no_setup_files", "Aucun setup ACC trouvé", retryable=False)
    # Session-aware match
    latest = match_setup_file_for_session(files, session_type=session_type, is_wet=is_wet)
    if not latest:
        latest = max(files, key=_o.path.getmtime)  # ultime fallback
    try:
        with open(latest, "r", encoding="utf-8") as f:
            data = _json.load(f)
        result = {"setup_file": _o.path.basename(latest), "session_type_matched": session_type or "?", "wet_mode": is_wet}
        key_paths = {
            "fuel": ["basicSetup", "strategy", "fuel"],
            "tyrePressure": ["basicSetup", "tyres", "tyrePressure"],
            "tyreCompound": ["basicSetup", "tyres", "tyreCompound"],
            "tC1": ["basicSetup", "electronics", "tC1"],
            "tC2": ["basicSetup", "electronics", "tC2"],
            "abs": ["basicSetup", "electronics", "abs"],
            "ecuMap": ["basicSetup", "electronics", "ecuMap"],
            "brakeBias": ["advancedSetup", "mechanicalBalance", "brakeBias"],
            "aRBFront": ["advancedSetup", "mechanicalBalance", "aRBFront"],
            "aRBRear": ["advancedSetup", "mechanicalBalance", "aRBRear"],
            "rideHeight": ["advancedSetup", "aeroBalance", "rideHeight"],
            "rearWing": ["advancedSetup", "aeroBalance", "wing"],
            "splitter": ["advancedSetup", "aeroBalance", "splitter"],
            "wheelRate": ["advancedSetup", "mechanicalBalance", "wheelRate"],
            "camber": ["basicSetup", "alignment", "camber"],
            "toe": ["basicSetup", "alignment", "toe"],
        }
        if fields:
            key_paths = {k: v for k, v in key_paths.items() if k in fields}
        for label, path in key_paths.items():
            node = data
            try:
                for k in path:
                    node = node[k]
                result[label] = node
            except (KeyError, TypeError):
                result[label] = None
        return _ok(result)
    except Exception as e:
        return _err("read_fail", f"Lecture setup err: {e}", retryable=True)


# ============================================================
# grid_engineer_audit : Phase C brief V3 17/05 — audit setup pre-race
# ============================================================
@bono_tool(
    name="grid_engineer_audit",
    description="GRID ENGINEER MODE pre-race : audit du current setup vs car/track knowledge base. Compare brake bias, ARB, wing, tyre pressure (hot target Pirelli DHF), fuel calc. Retourne suggestions chiffrées. USE WHEN driver demande 'audit setup' / 'check setup' / 'recommandations setup' / avant un départ.",
    parameters={"type": "object", "properties": {
        "laps_to_finish": {"type": "integer", "description": "Optional race target laps (for fuel calc). 0 = ignore fuel audit."}
    }, "required": []}
)
def grid_engineer_audit(laps_to_finish: int = 0) -> dict:
    """Compare current setup vs KB. Suggests deltas with reasoning.
    Fix bug C 17/05 nuit : session_type + weather adaptation des targets (sec vs wet,
    practice vs quali vs race)."""
    from tools.acc_knowledge import (
        get_car_knowledge, get_track_knowledge, get_combo_hint,
        click_to_psi_hot, psi_hot_target_to_click,
        get_session_tyre_target_psi, get_session_bb_target_pct,
    )
    s = _snap()
    car = (s.get("car") or "").strip()
    track = (s.get("track") or "").strip()
    session_type = (s.get("session") or "").upper()
    is_wet = (s.get("rain_intensity", 0) or 0) > 0.05
    if not car or not track:
        return _err("no_car_or_track", "Pas de car/track dans SHM, ACC pas lancé?", retryable=True)
    car_kb = get_car_knowledge(car) or {}
    track_kb = get_track_knowledge(track) or {}
    combo_hint = get_combo_hint(car, track) or {}
    # Read current setup (session-aware file selection)
    state = query_setup_state()
    if not state.get("ok"):
        return state
    audit = []  # list of {field, current, target, delta, severity, reason}
    # 1. Brake bias session-aware
    bb_cur = state.get("brakeBias")
    bb_range = car_kb.get("bb_range_pct")
    if bb_cur is not None and bb_range:
        bb_pct_cur = round(50 + bb_cur / 10.0, 1)
        bb_target = get_session_bb_target_pct(car_kb, combo_hint, session_type, is_wet)
        if bb_target is None:
            bb_target = (bb_range[0] + bb_range[1]) / 2
        if not (bb_range[0] <= bb_pct_cur <= bb_range[1]):
            audit.append({"field": "brakeBias", "current_pct": bb_pct_cur, "target_pct": bb_target,
                          "allowed_range": bb_range, "severity": "high",
                          "reason": f"BB {bb_pct_cur}% hors range {car} ({bb_range[0]}-{bb_range[1]}%) — session {session_type}{' WET' if is_wet else ''}"})
        elif abs(bb_pct_cur - bb_target) > 1.0:
            audit.append({"field": "brakeBias", "current_pct": bb_pct_cur, "target_pct": bb_target,
                          "severity": "medium",
                          "reason": f"BB {bb_pct_cur}% éloigné target {session_type}{' WET' if is_wet else ''} ({bb_target}%)"})
    # 2. Tyre pressure (per wheel) — session + weather adapté
    tp_cur = state.get("tyrePressure") or []
    tp_targets = get_session_tyre_target_psi(car_kb, session_type, is_wet)
    if isinstance(tp_cur, list) and len(tp_cur) == 4 and tp_targets:
        labels = ("FL", "FR", "RL", "RR")
        for i, lbl in enumerate(labels):
            psi_hot_obs = click_to_psi_hot(int(tp_cur[i]))
            psi_target = tp_targets.get(lbl, 26.8)
            delta_psi = round(psi_hot_obs - psi_target, 2)
            if abs(delta_psi) > 0.3:  # > 0.3 PSI = écart significatif
                click_target = psi_hot_target_to_click(psi_target)
                audit.append({"field": f"tyrePressure_{lbl}", "current_click": tp_cur[i],
                              "current_psi_hot_est": psi_hot_obs, "target_psi_hot": psi_target,
                              "delta_psi": delta_psi, "suggested_click": click_target,
                              "delta_clicks": click_target - int(tp_cur[i]),
                              "severity": "high" if abs(delta_psi) > 0.6 else "medium",
                              "reason": f"PSI hot estimé {psi_hot_obs} vs target {psi_target} (Pirelli DHF)"})
    # 3. Rear wing
    wing_cur = state.get("rearWing")
    wing_target = (combo_hint.get("wing_target") if combo_hint else None)
    if wing_cur is not None and wing_target is not None and abs(wing_cur - wing_target) > 1:
        audit.append({"field": "rearWing", "current": wing_cur, "target": wing_target,
                      "delta": wing_target - wing_cur, "severity": "medium",
                      "reason": f"Wing {wing_cur} vs combo target {wing_target} ({track_kb.get('downforce_pref','?')} DF preference)"})
    # 4. ARB front/rear vs car preference
    for arb_label, kb_key in [("aRBFront", "arb_front_pref"), ("aRBRear", "arb_rear_pref")]:
        arb_cur = state.get(arb_label)
        arb_pref = car_kb.get(kb_key)
        if arb_cur is not None and arb_pref:
            if not (arb_pref[0] <= arb_cur <= arb_pref[1]):
                audit.append({"field": arb_label, "current": arb_cur, "target_range": arb_pref,
                              "severity": "medium",
                              "reason": f"{arb_label} {arb_cur} hors préf {car} ({arb_pref[0]}-{arb_pref[1]})"})
    # 5. Fuel for race
    if laps_to_finish > 0:
        fuel_cur_setup = state.get("fuel")  # litres entiers
        fuel_per_lap_obs = s.get("fuel_per_lap", 0) or 0
        if fuel_per_lap_obs > 0.1 and fuel_cur_setup is not None:
            fuel_needed = round(laps_to_finish * fuel_per_lap_obs + 2, 1)  # +2L safety
            if abs(fuel_cur_setup - fuel_needed) > 1.5:
                audit.append({"field": "fuel", "current_l": fuel_cur_setup, "target_l": fuel_needed,
                              "delta_l": round(fuel_needed - fuel_cur_setup, 1), "severity": "high",
                              "reason": f"Fuel {fuel_cur_setup}L vs besoin estimé {fuel_needed}L ({laps_to_finish} laps × {fuel_per_lap_obs:.2f}L/lap + 2L safety)"})
    # Summary
    severity_counts = {"high": 0, "medium": 0, "low": 0}
    for a in audit:
        severity_counts[a.get("severity", "medium")] += 1
    return _ok({
        "track": track, "car": car,
        "session_type": session_type, "wet_mode": is_wet,
        "setup_file": state.get("setup_file"),
        "n_issues": len(audit),
        "severity_breakdown": severity_counts,
        "audit": audit,
        "overall_verdict": (
            f"Setup {session_type}{' WET' if is_wet else ''} OK, peu/pas d'ajustements." if not audit
            else f"{severity_counts['high']} ajustements critiques, {severity_counts['medium']} mineurs recommandés (session {session_type}{' WET' if is_wet else ''})."
        ),
        "note_psi_caveat": "PSI hot estimations utilisent COLD_TO_HOT_DELTA=2.0 PSI. Targets adaptés session_type + wet/dry.",
    })


# ============================================================
# query_brake_state : check brake temps + pad/disc life
# ============================================================
@bono_tool(
    name="query_brake_state",
    description="Get brake temperatures (°C) per wheel + pad life + disc life. Use when driver asks about brakes, fade, or after brake_temp events.",
    parameters={"type": "object", "properties": {}, "required": []}
)
def query_brake_state() -> dict:
    s = _snap()
    if not s.get("shm_ok"):
        return _err("shm_unavailable", "Pas de SHM dispo.", retryable=True)
    bt = [s.get(f"brake_temp_{p}", 0) for p in ("fl","fr","rl","rr")]
    pl = [s.get(f"pad_life_{p}", 0) for p in ("fl","fr","rl","rr")]
    dl = [s.get(f"disc_life_{p}", 0) for p in ("fl","fr","rl","rr")]
    bt_avg = sum(bt) / 4 if bt else 0
    bt_max = max(bt) if bt else 0
    bt_min = min(bt) if bt else 0
    # Window assessment GT3 typical : 400-650°C optimal
    status = "cold" if bt_avg < 300 else ("hot" if bt_avg > 650 else "optimal")
    return _ok({
        "brake_temp_c": {"fl": round(bt[0], 0), "fr": round(bt[1], 0), "rl": round(bt[2], 0), "rr": round(bt[3], 0)},
        "brake_temp_avg_c": round(bt_avg, 0),
        "brake_temp_min_c": round(bt_min, 0),
        "brake_temp_max_c": round(bt_max, 0),
        "pad_life_mm": [round(p, 1) for p in pl],
        "disc_life_mm": [round(d, 1) for d in dl],
        "thermal_status": status,
        "advice": (
            "Freins froids, attaque hard pour chauffer." if status == "cold"
            else "Freins trop chauds, attention fade." if status == "hot"
            else "Freins dans la fenêtre optimale."
        ),
    })


# ============================================================
# bono_pit_decision : combo macro = gap_trend + strategy_projection + fuel_strategy
# ============================================================
@bono_tool(
    name="bono_pit_decision",
    description="ALL-IN-ONE pit wall macro tool : combine gap trend + strategy projection + fuel strategy. Returns single recommendation 'box now / stay out / box +1 / box -1'. Use when driver asks 'box ?' or 'pit decision ?' in RACE.",
    parameters={"type": "object", "properties": {}, "required": []}
)
def bono_pit_decision() -> dict:
    s = _snap()
    if not s.get("shm_ok"):
        return _err("shm_unavailable", "Pas de SHM.", retryable=True)
    if (s.get("session") or "").upper() != "RACE":
        return _err("not_in_race", "Pit decision RACE-only.", retryable=False, current_session=s.get("session"))
    track = (s.get("track") or "").strip()
    pit_loss = _get_pit_loss(track)
    gap_ahead = (s.get("gap_ahead_ms", 0) or 0) / 1000.0
    gap_behind = (s.get("gap_behind_ms", 0) or 0) / 1000.0
    fuel_l = s.get("fuel_l", 0)
    fuel_per_lap = s.get("fuel_per_lap", 0)
    laps_fuel = (fuel_l / fuel_per_lap) if fuel_per_lap > 0 else 0
    tyres_wear = [s.get(f"tyre_wear_{p}", 0) for p in ("fl","fr","rl","rr")]
    max_wear = max(tyres_wear) if tyres_wear else 0
    position = s.get("position", 0)
    # Decision logic
    decision = "stay_out"
    reasons = []
    if laps_fuel < 1.5:
        decision = "box_now"
        reasons.append(f"Fuel critical ({laps_fuel:.1f} laps)")
    elif max_wear > 0.85:
        decision = "box_now"
        reasons.append(f"Tyres cliff ({int(max_wear*100)}%)")
    elif laps_fuel < 3:
        decision = "box_now"
        reasons.append(f"Fuel low ({laps_fuel:.1f} laps)")
    elif max_wear > 0.70 and laps_fuel < 10:
        decision = "box_plus_1"
        reasons.append(f"Tyres degraded ({int(max_wear*100)}%) + fuel finite")
    elif 0 < gap_behind < pit_loss * 0.8:
        decision = "stay_out_traffic"
        reasons.append(f"Traffic risk pit exit (gap behind {gap_behind:.1f}s < pit_loss {pit_loss:.0f}s)")
    elif 0 < gap_ahead < pit_loss + 2:
        decision = "undercut_window"
        reasons.append(f"Undercut viable (gap ahead {gap_ahead:.1f}s ≤ pit_loss+2)")
    else:
        reasons.append("Pas de pression immédiate fuel/tyres/traffic")
    return _ok({
        "decision": decision,
        "reasons": reasons,
        "track": track,
        "position": position,
        "pit_loss_s": pit_loss,
        "fuel_l": round(fuel_l, 2),
        "fuel_laps": round(laps_fuel, 1),
        "max_tyre_wear_pct": round(max_wear * 100, 1),
        "gap_ahead_s": round(gap_ahead, 2),
        "gap_behind_s": round(gap_behind, 2),
        "advice": (
            f"BOX NOW. {' + '.join(reasons)}" if decision == "box_now"
            else f"BOX +1 lap. {' + '.join(reasons)}" if decision == "box_plus_1"
            else f"STAY OUT, attention pit exit traffic. {' + '.join(reasons)}" if decision == "stay_out_traffic"
            else f"UNDERCUT NOW si tyres ok. {' + '.join(reasons)}" if decision == "undercut_window"
            else f"STAY OUT. {' + '.join(reasons)}"
        ),
    })


# ============================================================
# V3 — Race engineer strategic tools (gap trend, strategy projection, sectors, debrief, proximity)
# ============================================================
ACC_PIT_LOSS_S = {
    "spa": 26.0, "monza": 22.0, "imola": 25.0, "brands_hatch": 21.0,
    "zolder": 20.0, "nurburgring": 24.0, "silverstone": 23.0, "hungaroring": 21.0,
    "paul_ricard": 24.0, "barcelona": 22.0, "misano": 21.0, "suzuka": 23.0,
    "mount_panorama": 30.0, "kyalami": 21.0, "laguna_seca": 21.0, "cota": 27.0,
    "donington": 20.0, "oulton_park": 19.0, "snetterton": 20.0,
}

# Avg lap times GT3 typical (Pirelli DHF, fast amateur ~+2s from pro)
# Used by query_fuel_for_session_plan when no live telemetry yet
ACC_AVG_LAP_S = {
    "spa": 138.0, "monza": 110.0, "imola": 102.0, "brands_hatch": 86.0,
    "zolder": 92.0, "nurburgring": 117.0, "silverstone": 121.0, "hungaroring": 108.0,
    "paul_ricard": 116.0, "barcelona": 108.0, "misano": 100.0, "suzuka": 124.0,
    "mount_panorama": 124.0, "kyalami": 100.0, "laguna_seca": 86.0, "cota": 134.0,
    "donington": 84.0, "oulton_park": 92.0, "snetterton": 110.0,
}

# Avg fuel consumption GT3 (L/lap) - varies by car but ~3.0-3.5 typical
ACC_AVG_FUEL_PER_LAP_L = {
    "spa": 3.6, "monza": 3.2, "imola": 2.8, "brands_hatch": 2.6,
    "zolder": 3.0, "nurburgring": 3.4, "silverstone": 3.4, "hungaroring": 2.9,
    "paul_ricard": 3.3, "barcelona": 3.0, "misano": 2.9, "suzuka": 3.5,
    "mount_panorama": 3.6, "kyalami": 2.9, "laguna_seca": 2.5, "cota": 3.7,
    "donington": 2.4, "oulton_park": 2.6, "snetterton": 3.1,
}


def _get_pit_loss(track: str) -> float:
    t = (track or "").lower().replace("-", "_")
    return ACC_PIT_LOSS_S.get(t, 24.0)


def _get_avg_lap_s(track: str) -> float:
    t = (track or "").lower().replace("-", "_")
    return ACC_AVG_LAP_S.get(t, 110.0)


def _get_avg_fuel_per_lap(track: str) -> float:
    t = (track or "").lower().replace("-", "_")
    return ACC_AVG_FUEL_PER_LAP_L.get(t, 3.0)


def _memory():
    import importlib, sys as _s
    if "memory" in _s.modules:
        return _s.modules["memory"]
    return importlib.import_module("memory")


@bono_tool(
    name="gap_trend_engine",
    description="Compute gap closure trend over last 3-5 laps. Returns gap delta per lap. Use for catch/threat analysis.",
    parameters={"type": "object", "properties": {"laps_window": {"type": "integer"}}, "required": []}
)
def gap_trend_engine(laps_window: int = 3) -> dict:
    s = _snap()
    if not s.get("shm_ok"):
        return _err("shm_unavailable", "Pas de SHM dispo.", retryable=True)
    session_id = s.get("_session_id") or s.get("session_id")
    if not session_id:
        return _err("no_session_context", "Session ID inconnu.", retryable=True)
    try:
        mem = _memory()
        laps = mem.get_recent_laps(session_id, n=max(2, laps_window))
    except Exception as e:
        return _err("db_read_fail", f"DB err: {e}", retryable=True)
    if len(laps) < 2:
        return _err("insufficient_laps", f"Besoin 2+ tours pour tendance, j'en ai {len(laps)}.", retryable=True, n_laps=len(laps))
    gaps_ahead = [l["gap_ahead_ms"] for l in laps if l.get("gap_ahead_ms") is not None]
    gaps_behind = [l["gap_behind_ms"] for l in laps if l.get("gap_behind_ms") is not None]
    def _trend(vals):
        if len(vals) < 2: return None
        return round((vals[-1] - vals[0]) / (len(vals) - 1), 0)
    ta = _trend(gaps_ahead)
    tb = _trend(gaps_behind)
    out = {"laps_analyzed": len(laps),
           "current_gap_ahead_ms": gaps_ahead[-1] if gaps_ahead else None,
           "current_gap_behind_ms": gaps_behind[-1] if gaps_behind else None,
           "trend_ahead_ms_per_lap": ta, "trend_behind_ms_per_lap": tb}
    if ta is not None:
        if ta < -100:
            out["interpretation_ahead"] = f"catching_at_{abs(ta)/1000:.2f}s_per_lap"
        elif ta > 100:
            out["interpretation_ahead"] = f"dropping_at_{ta/1000:.2f}s_per_lap"
        else:
            out["interpretation_ahead"] = "stable_pace_with_car_ahead"
    if tb is not None:
        if tb > 100:
            out["interpretation_behind"] = f"pulling_away_at_{tb/1000:.2f}s_per_lap"
        elif tb < -100:
            out["interpretation_behind"] = f"car_behind_closing_at_{abs(tb)/1000:.2f}s_per_lap"
        else:
            out["interpretation_behind"] = "stable_behind"
    return _ok(out)


@bono_tool(
    name="strategy_projection",
    description="Project race position after box-now vs stay-out scenarios. Critical pitwall tool. Computes pit_loss + rejoin + undercut/overcut.",
    parameters={"type": "object", "properties": {"scenarios": {"type": "array", "items": {"type": "string"}}}, "required": []}
)
def strategy_projection(scenarios: list | None = None) -> dict:
    s = _snap()
    if not s.get("shm_ok"):
        return _err("shm_unavailable", "Pas de SHM.", retryable=True)
    if (s.get("session") or "").upper() != "RACE":
        return _err("not_in_race", "Strategy projection RACE-only.", retryable=False, current_session=s.get("session"))
    track = (s.get("track") or "").strip()
    pit_loss = _get_pit_loss(track)
    gap_ahead = (s.get("gap_ahead_ms", 0) or 0) / 1000.0
    gap_behind = (s.get("gap_behind_ms", 0) or 0) / 1000.0
    position = s.get("position", 0)
    fuel_left = s.get("fuel_l", 0)
    fuel_per_lap = s.get("fuel_per_lap", 0)
    last_ms = s.get("last_time_ms", 0)
    lap_time_s = last_ms / 1000.0 if last_ms > 0 else None
    session_id = s.get("_session_id") or s.get("session_id")
    pace_trend_s = None
    if session_id:
        try:
            mem = _memory()
            recent = mem.get_recent_laps(session_id, n=3)
            valid = [l for l in recent if l.get("valid_lap") and l.get("lap_time_ms", 0) > 0]
            if valid:
                pace_trend_s = sum(l["lap_time_ms"] for l in valid) / len(valid) / 1000.0
        except Exception: pass
    pace_s = pace_trend_s or lap_time_s or 100.0
    scenarios = scenarios or ["box_now", "box_plus_1", "stay_out_2"]
    projections = {}
    for sc in scenarios:
        if sc == "box_now":
            projections[sc] = {
                "pit_loss_s": pit_loss,
                "rejoin_loss_to_leader_s": round(pit_loss, 1),
                "advice": f"Box now → {pit_loss:.0f}s loss vs staying out. Position drops ~{int(pit_loss / max(pace_s, 1))} cars if tight field."
            }
        elif sc == "box_plus_1":
            projections[sc] = {
                "pit_loss_s": pit_loss,
                "extra_lap_on_old_tyres": True,
                "advice": f"Box +1 → same {pit_loss:.0f}s loss, +1 lap old tyres. Overcut viable if car ahead just pitted."
            }
        elif sc == "stay_out_2":
            tyre_wear_max = max((s.get(f"tyre_wear_{p}", 0) for p in ("fl","fr","rl","rr")), default=0)
            projections[sc] = {
                "tyre_wear_max_now_pct": round(tyre_wear_max * 100, 1),
                "tyre_wear_projected_2laps_pct": round((tyre_wear_max + 0.04) * 100, 1),
                "fuel_after_2_laps_l": round(fuel_left - 2 * fuel_per_lap, 2) if fuel_per_lap > 0 else None,
                "advice": f"Stay out +2 → tyres ~{round((tyre_wear_max + 0.04) * 100):.0f}%, fuel ok if {fuel_per_lap:.1f} L/lap."
            }
    return _ok({
        "track": track, "pit_loss_s": pit_loss, "position": position,
        "gap_ahead_s": round(gap_ahead, 2), "gap_behind_s": round(gap_behind, 2),
        "lap_time_s": round(pace_s, 3),
        "fuel_left_l": round(fuel_left, 2), "fuel_per_lap_l": round(fuel_per_lap, 2),
        "projections": projections,
        "undercut_window_open": 0 < gap_ahead < pit_loss + 5,
        "overcut_window_open": pit_loss - 5 < gap_ahead < pit_loss + 10,
        "rejoin_clear_air_estimate": "likely_clear" if gap_behind > pit_loss + 3 else "traffic_risk",
    })


@bono_tool(
    name="query_sector_performance",
    description="Compare current lap S1/S2/S3 vs best lap. Identifies where time is lost/gained per sector.",
    parameters={"type": "object", "properties": {}, "required": []}
)
def query_sector_performance() -> dict:
    s = _snap()
    if not s.get("shm_ok"):
        return _err("shm_unavailable", "Pas de SHM.", retryable=True)
    session_id = s.get("_session_id") or s.get("session_id")
    if not session_id:
        return _err("no_session_context", "Session ID inconnu.", retryable=True)
    try:
        mem = _memory()
        recent = mem.get_recent_laps(session_id, n=20)
    except Exception as e:
        return _err("db_read_fail", f"DB err: {e}", retryable=True)
    valid = [l for l in recent if l.get("valid_lap") and l.get("lap_time_ms", 0) > 0 and l.get("s1_ms", 0) > 0]
    if len(valid) < 2:
        return _err("insufficient_laps", f"Besoin 2+ tours valides avec secteurs (j'en ai {len(valid)}).", retryable=True)
    best = min(valid, key=lambda l: l["lap_time_ms"])
    current = valid[-1]
    deltas = {
        "s1_delta_ms": current["s1_ms"] - best["s1_ms"],
        "s2_delta_ms": current["s2_ms"] - best["s2_ms"],
        "s3_delta_ms": current["s3_ms"] - best["s3_ms"],
        "total_delta_ms": current["lap_time_ms"] - best["lap_time_ms"],
    }
    sl = sorted([("S1", deltas["s1_delta_ms"]), ("S2", deltas["s2_delta_ms"]), ("S3", deltas["s3_delta_ms"])], key=lambda kv: -kv[1])
    return _ok({
        "current_lap_num": current["lap_num"],
        "current_total_ms": current["lap_time_ms"],
        "best_lap_num": best["lap_num"],
        "best_total_ms": best["lap_time_ms"],
        "deltas": deltas,
        "biggest_loss_sector": sl[0][0],
        "biggest_loss_ms": sl[0][1],
        "current_s1_s2_s3_ms": [current["s1_ms"], current["s2_ms"], current["s3_ms"]],
        "best_s1_s2_s3_ms": [best["s1_ms"], best["s2_ms"], best["s3_ms"]],
    })


@bono_tool(
    name="post_session_debrief",
    description="Generate structured post-session debrief : best/avg lap, consistency, vs historical, headline.",
    parameters={"type": "object", "properties": {"session_id": {"type": "integer"}}, "required": []}
)
def post_session_debrief(session_id: int = 0) -> dict:
    s = _snap()
    sid = session_id or s.get("_session_id") or s.get("session_id")
    if not sid:
        return _err("no_session_context", "Session ID inconnu.", retryable=True)
    try:
        mem = _memory()
        recent = mem.get_recent_laps(sid, n=200)
    except Exception as e:
        return _err("db_read_fail", f"DB err: {e}", retryable=True)
    valid = [l for l in recent if l.get("valid_lap") and l.get("lap_time_ms", 0) > 0]
    if not valid:
        return _err("no_valid_laps", "Pas de tours valides loggués.", retryable=False, n_laps=len(recent))
    times = [l["lap_time_ms"] for l in valid]
    best = min(times)
    avg = sum(times) / len(times)
    var = sum((t - avg) ** 2 for t in times) / len(times)
    std = var ** 0.5
    cons = round(std, 0)
    historical = None
    last = recent[-1] if recent else None
    if last:
        try:
            historical = mem.get_best_lap_for_track_car(last.get("track", ""), last.get("car", ""))
        except Exception: pass
    return _ok({
        "session_id": sid,
        "laps_completed": len(valid),
        "best_lap_ms": best,
        "best_lap": _fmt_lap(best),
        "avg_lap_ms": round(avg, 0),
        "avg_lap": _fmt_lap(avg),
        "consistency_std_ms": cons,
        "consistency_assessment": "very_consistent" if std < 300 else ("decent" if std < 800 else "inconsistent"),
        "historical_best_for_combo": historical["lap_time_ms"] if historical else None,
        "historical_best": _fmt_lap(historical["lap_time_ms"]) if historical and historical.get("lap_time_ms") else None,
        "vs_historical_delta_ms": (best - historical["lap_time_ms"]) if historical and historical.get("lap_time_ms") else None,
        "headline": f"{len(valid)} valid laps, best {_fmt_lap(best)}, avg {_fmt_lap(avg)}, consistency plus minus {cons/1000:.2f}s.",
    })


@bono_tool(
    name="query_driving_trace",
    description="V3.O : returns last 3-N sector driving traces (Vmin, brake max, throttle release position, slip, TC/ABS active %). Coaching pilotage micro : 'You over-slowed S2 by 5 km/h, brake later'.",
    parameters={"type": "object", "properties": {
        "n": {"type": "integer", "description": "Number of recent sector traces to return (default 6, ~2 laps)"},
    }, "required": []}
)
def query_driving_trace(n: int = 6) -> dict:
    s = _snap()
    if not s.get("shm_ok"):
        return _err("shm_unavailable", "Pas de SHM.", retryable=True)
    session_id = s.get("_session_id") or s.get("session_id")
    if not session_id:
        return _err("no_session_context", "Session ID inconnu.", retryable=True)
    try:
        mem = _memory()
        traces = mem.get_driving_trace_recent(session_id, n=n)
    except Exception as e:
        return _err("db_read_fail", f"DB err: {e}", retryable=True)
    if not traces:
        return _err("no_traces_yet", "Aucune trace pilotage encore. Faut compléter au moins 1 secteur en LIVE.", retryable=True)
    # Aggregate insights : avg Vmin per sector, slip events, TC overuse
    by_sector = {0: [], 1: [], 2: []}
    for t in traces:
        si = t.get("sector_index", -1)
        if si in by_sector:
            by_sector[si].append(t)
    sector_stats = {}
    for si, items in by_sector.items():
        if not items: continue
        vmins = [i["speed_min_kmh"] for i in items if i.get("speed_min_kmh", 0) > 0]
        brakes = [i["brake_max_pct"] for i in items if i.get("brake_max_pct", 0) > 0]
        slips = [i["wheel_slip_max"] for i in items if i.get("wheel_slip_max", 0) > 0]
        tcs = [i["tc_active_pct"] for i in items if i.get("tc_active_pct", 0) >= 0]
        sector_stats[f"S{si+1}"] = {
            "n_samples": len(items),
            "avg_vmin_kmh": round(sum(vmins)/len(vmins), 1) if vmins else None,
            "min_vmin_kmh": round(min(vmins), 1) if vmins else None,
            "avg_brake_max_pct": round(sum(brakes)/len(brakes), 1) if brakes else None,
            "avg_wheel_slip_max": round(sum(slips)/len(slips), 3) if slips else None,
            "avg_tc_active_pct": round(sum(tcs)/len(tcs), 1) if tcs else None,
        }
    # Identify concerns
    concerns = []
    for k, st in sector_stats.items():
        if st.get("avg_tc_active_pct", 0) and st["avg_tc_active_pct"] > 30:
            concerns.append(f"{k}: TC active {st['avg_tc_active_pct']:.0f}% → throttle trop tôt, sortie virage perdue")
        if st.get("avg_wheel_slip_max", 0) and st["avg_wheel_slip_max"] > 0.15:
            concerns.append(f"{k}: wheel_slip moyenne {st['avg_wheel_slip_max']:.2f} → grip perdu, attention vitesse mini")
    return _ok({
        "n_traces": len(traces),
        "sector_stats": sector_stats,
        "concerns": concerns,
        "raw_last_3": traces[-3:] if len(traces) >= 3 else traces,
    })


@bono_tool(
    name="query_setup_context",
    description="ONE-SHOT fat tool (fix F5 17/05). Returns CAR knowledge + TRACK knowledge + COMBO hints in a single call. Use this INSTEAD of calling query_car_knowledge + query_track_knowledge + query_combo_setup_hints separately (audit 17/05 a observé des chaînes de 10 tool calls inutiles). Auto-detects car/track from SHM if not specified.",
    parameters={"type": "object", "properties": {
        "car_id": {"type": "string", "description": "Optional override (default: SHM current)"},
        "track_id": {"type": "string", "description": "Optional override (default: SHM current)"},
    }, "required": []}
)
def query_setup_context(car_id: str = "", track_id: str = "") -> dict:
    """Fat tool — fusion car KB + track KB + combo hints. F5 17/05 audit."""
    from tools.acc_knowledge import get_car_knowledge, get_track_knowledge, get_combo_hint, all_known_cars, all_known_tracks
    s = _snap()
    car = car_id or (s.get("car") or "").strip()
    track = track_id or (s.get("track") or "").strip()
    out = {"car_id": car, "track_id": track}
    if car:
        car_kb = get_car_knowledge(car)
        if car_kb:
            out["car_knowledge"] = car_kb
        else:
            out["car_unknown"] = f"Car '{car}' not in KB. Known: {', '.join(all_known_cars()[:8])}..."
    if track:
        track_kb = get_track_knowledge(track)
        if track_kb:
            out["track_knowledge"] = track_kb
        else:
            out["track_unknown"] = f"Track '{track}' not in KB. Known: {', '.join(all_known_tracks()[:8])}..."
    if car and track:
        combo = get_combo_hint(car, track)
        if combo:
            out["combo_hints"] = combo
        else:
            out["combo_note"] = "No specific combo entry — LLM should synthesize car_knowledge + track_knowledge."
    if not car and not track:
        return _err("no_car_no_track", "Ni car ni track détectés (SHM off ou aucun override).", retryable=True)
    return _ok(out)


# Tools legacy (deprecated mais conservés pour backward compat des prompts existants).
# Le LLM devrait préférer query_setup_context (1 call) à ces 3 (jusqu'à 3 calls + overhead).
@bono_tool(
    name="query_car_knowledge",
    description="[LEGACY — préfère query_setup_context qui retourne car+track+combo en 1 call]. Returns car-specific engineering knowledge (BB range, TC/ABS, tyre target, ARB pref, strengths/weaknesses).",
    parameters={"type": "object", "properties": {
        "car_id": {"type": "string", "description": "ACC car identifier (e.g. ferrari_488_gt3_evo). Optional — uses current SHM car if absent."},
    }, "required": []}
)
def query_car_knowledge(car_id: str = "") -> dict:
    from tools.acc_knowledge import get_car_knowledge, all_known_cars
    car = car_id or (_snap().get("car") or "").strip()
    if not car:
        return _err("no_car_specified", f"Aucune voiture détectée dans SHM ni fournie. Cars connues : {', '.join(all_known_cars()[:5])}...", retryable=True)
    kb = get_car_knowledge(car)
    if not kb:
        return _err("unknown_car", f"Voiture '{car}' pas dans KB. Cars connues : {', '.join(all_known_cars())}", retryable=False, requested_car=car)
    return _ok({"car_id": car, **kb})


@bono_tool(
    name="query_track_knowledge",
    description="[LEGACY — préfère query_setup_context qui retourne car+track+combo en 1 call]. Returns track-specific knowledge (DF preference, brake events, kerb usage, tyre stress zones, pit_loss).",
    parameters={"type": "object", "properties": {
        "track_id": {"type": "string", "description": "ACC track identifier (e.g. spa, monza). Optional — uses current SHM track if absent."},
    }, "required": []}
)
def query_track_knowledge(track_id: str = "") -> dict:
    from tools.acc_knowledge import get_track_knowledge, all_known_tracks
    track = track_id or (_snap().get("track") or "").strip()
    if not track:
        return _err("no_track_specified", f"Aucun track détecté. Tracks connus : {', '.join(all_known_tracks()[:5])}...", retryable=True)
    kb = get_track_knowledge(track)
    if not kb:
        return _err("unknown_track", f"Track '{track}' pas dans KB. Tracks connus : {', '.join(all_known_tracks())}", retryable=False, requested_track=track)
    return _ok({"track_id": track, **kb})


@bono_tool(
    name="query_combo_setup_hints",
    description="[LEGACY — préfère query_setup_context qui retourne car+track+combo en 1 call]. Returns car+track specific setup hints (wing_target, BB_target, ARB targets).",
    parameters={"type": "object", "properties": {
        "car_id": {"type": "string"}, "track_id": {"type": "string"},
    }, "required": []}
)
def query_combo_setup_hints(car_id: str = "", track_id: str = "") -> dict:
    from tools.acc_knowledge import get_combo_hint, get_car_knowledge, get_track_knowledge
    s = _snap()
    car = car_id or (s.get("car") or "").strip()
    track = track_id or (s.get("track") or "").strip()
    if not car or not track:
        return _err("no_car_or_track", "Besoin car_id et track_id (auto-détectés depuis SHM si manquants).", retryable=True)
    combo = get_combo_hint(car, track)
    if combo:
        return _ok({"car": car, "track": track, "has_combo_hint": True, **combo})
    # Fallback : retourne car+track KB séparés pour que LLM combine
    car_kb = get_car_knowledge(car)
    track_kb = get_track_knowledge(track)
    if not car_kb and not track_kb:
        return _err("unknown_combo", f"Ni car '{car}' ni track '{track}' dans KB.", retryable=False)
    return _ok({
        "car": car, "track": track, "has_combo_hint": False,
        "car_kb_summary": {"engine": car_kb.get("engine"), "bb_range": car_kb.get("bb_range_pct"), "strengths": car_kb.get("strengths")} if car_kb else None,
        "track_kb_summary": {"downforce_pref": track_kb.get("downforce_pref"), "kerb_usage": track_kb.get("kerb_usage")} if track_kb else None,
        "note": "No specific combo entry. LLM should combine car_kb + track_kb for advice.",
    })


@bono_tool(
    name="query_driver_memory",
    description="""B.5/B.6 brief V3 17/05 : DEEP cross-session memory pour ce car+track combo.
Retourne : driver_history_patterns + setup_changes appliqués ici + recurring issues
(>=3x cross-session : understeer, track_limits, brake_temp) + PB trend over sessions +
optimal tyre pressure observée aux meilleurs laps historiques.

USE FOR:
- Pre-session brief ("Mon histoire sur Spa avec Porsche ?")
- Coaching context ("Ai-je déjà eu ce souci ?")
- Setup recommendations basées sur passé
- PB trend awareness pour gauge progression

DON'T USE FOR:
- Current session laps only -> use query_recent_laps
- Real-time telemetry -> use query_telemetry
- Single-session sector deltas -> use query_sector_performance""",
    parameters={"type": "object", "properties": {}, "required": []}
)
def query_driver_memory() -> dict:
    """B.6 brief V3 17/05 nuit : DEEP cross-session memory tool exposé au LLM."""
    s = _snap()
    track = (s.get("track") or "").strip()
    car = (s.get("car") or "").strip()
    sid = s.get("_session_id")
    if not track or not car:
        return _err("no_track_car", "Track/car inconnu, ACC pas lancé.", retryable=True)
    try:
        mem = _memory()
        data = mem.deep_driver_memory(track, car, current_session_id=sid)
    except Exception as e:
        return _err("db_read_fail", f"DB err: {e}", retryable=True)
    if not data.get("has_history"):
        return _err("no_history_yet", f"Pas d'historique cross-session sur {track}/{car}.", retryable=False, n_laps_history=0)
    return _ok(data)


@bono_tool(
    name="query_driver_history",
    description="[LEGACY — préfère query_driver_memory qui retourne deep memory complet]. Retourne driver patterns basiques (valid rate, weakness sector, best historical lap).",
    parameters={"type": "object", "properties": {}, "required": []}
)
def query_driver_history() -> dict:
    s = _snap()
    if not s.get("shm_ok"):
        return _err("shm_unavailable", "Pas de SHM.", retryable=True)
    track = (s.get("track") or "").strip()
    car = (s.get("car") or "").strip()
    if not track or not car:
        return _err("no_track_car", "Track/car inconnu, ACC pas lancé ou en menu.", retryable=True)
    try:
        mem = _memory()
        patterns = mem.driver_history_patterns(track, car)
    except Exception as e:
        return _err("db_read_fail", f"DB err: {e}", retryable=True)
    if not patterns.get("has_history"):
        return _err("no_history_yet", f"Pas d'historique sur {track}/{car} encore — première session de ce combo.", retryable=False, n_laps_history=0)
    return _ok(patterns)


@bono_tool(
    name="query_proximity_map",
    description="V3.Q : Spotter complet avec car_left/right/ahead/behind via géométrie vectorielle SHM car_coordinates. Détecte voitures dans rayon 10m sur axes latéral/longitudinal.",
    parameters={"type": "object", "properties": {
        "lateral_threshold_m": {"type": "number", "description": "Threshold latéral pour détecter côté (default 5m)"},
        "longitudinal_threshold_m": {"type": "number", "description": "Threshold longitudinal pour détecter ahead/behind (default 10m)"},
    }, "required": []}
)
def query_proximity_map(lateral_threshold_m: float = 5.0, longitudinal_threshold_m: float = 10.0) -> dict:
    """V3.Q : vraie spotter geometry via car_coordinates SHM ACC."""
    s = _snap()
    if not s.get("shm_ok"):
        return _err("shm_unavailable", "Pas de SHM.", retryable=True)
    # Fallback gaps if no coordinates
    ga = (s.get("gap_ahead_ms", 0) or 0) / 1000.0
    gb = (s.get("gap_behind_ms", 0) or 0) / 1000.0
    coords = s.get("car_coordinates_raw") or []
    car_ids = s.get("car_ids_raw") or []
    player_id = s.get("player_car_id", -1)
    if not coords or player_id < 0:
        # Fallback : just gaps
        prox = {"ahead": "clear", "behind": "clear", "side": "unknown"}
        if 0 < ga < 1.0: prox["ahead"] = "very_close"
        elif 0 < ga < 2.0: prox["ahead"] = "close"
        if 0 < gb < 1.0: prox["behind"] = "very_close"
        elif 0 < gb < 2.0: prox["behind"] = "close"
        return _ok({
            "gap_ahead_s": round(ga, 2), "gap_behind_s": round(gb, 2),
            "proximity": prox,
            "side_detection_available": False,
            "note": "car_coordinates pas disponible (ACC pas en LIVE ou activeCars=0). Fallback gaps.",
        })
    # Find player coords
    player_idx = -1
    for i, cid in enumerate(car_ids):
        if cid == player_id:
            player_idx = i
            break
    if player_idx < 0 or player_idx >= len(coords):
        return _err("player_not_in_coords", f"player_car_id={player_id} pas trouvé dans car_ids.", retryable=True)
    px, py, pz = coords[player_idx]
    # Compute proximity for each other car
    nearby = []
    for i, (cx, cy, cz) in enumerate(coords):
        if i == player_idx: continue
        cid = car_ids[i] if i < len(car_ids) else -1
        if cid < 0: continue
        # ACC coords : X=longitudinal (forward+), Y=vertical, Z=lateral (right+)
        # NOTE: actual axes orientation depends on ACC convention, à valider en runtime
        dx = cx - px  # longitudinal delta
        dz = cz - pz  # lateral delta
        dist = (dx*dx + dz*dz) ** 0.5
        if dist > 30: continue  # ignore far cars
        # Side classification
        side = "clear"
        if abs(dx) < longitudinal_threshold_m:  # roughly same longitudinal
            if abs(dz) < lateral_threshold_m:
                # Very close, same level → side-by-side
                if dz > 0.5:
                    side = "right"
                elif dz < -0.5:
                    side = "left"
                else:
                    side = "overlap"
            elif dz > 0:
                side = "wide_right"
            else:
                side = "wide_left"
        elif dx > 0:
            side = "ahead"
        else:
            side = "behind"
        nearby.append({
            "car_id": cid,
            "dist_m": round(dist, 1),
            "dx_long_m": round(dx, 1),
            "dz_lat_m": round(dz, 1),
            "side": side,
        })
    # Aggregate proximity
    prox = {"left": "clear", "right": "clear", "ahead": "clear", "behind": "clear"}
    for c in nearby:
        side = c["side"]
        if side in ("left", "wide_left") and c["dist_m"] < 8:
            prox["left"] = "very_close" if c["dist_m"] < 3 else "close"
        elif side in ("right", "wide_right") and c["dist_m"] < 8:
            prox["right"] = "very_close" if c["dist_m"] < 3 else "close"
        elif side == "ahead" and c["dist_m"] < 15:
            prox["ahead"] = "very_close" if c["dist_m"] < 5 else "close"
        elif side == "behind" and c["dist_m"] < 15:
            prox["behind"] = "very_close" if c["dist_m"] < 5 else "close"
        elif side == "overlap":
            prox["left"] = prox["right"] = "overlap"
    return _ok({
        "n_cars_total": len(coords),
        "n_cars_within_30m": len(nearby),
        "player_id": player_id,
        "gap_ahead_s": round(ga, 2),
        "gap_behind_s": round(gb, 2),
        "proximity": prox,
        "nearby_cars": sorted(nearby, key=lambda c: c["dist_m"])[:5],
        "side_detection_available": True,
        "note": "Side detection via car_coordinates SHM vector geometry. Axis convention X=long, Z=lateral.",
    })
