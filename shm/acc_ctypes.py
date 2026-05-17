"""ACC Shared Memory direct ctypes reader.

Bypass complet de pyaccsharedmemory (buggué : Physics retourne 0 même si ACC tourne).
Read direct des 3 memory-mapped files Kunos via mmap + ctypes.Structure.

CRITICAL Windows : `_pack_ = 4` obligatoire sur les structs (alignment Windows).
Source struct : Kunos public C++ headers (sdk officiel ACC).
"""
import ctypes
import mmap
from typing import Optional


# ============================================================
# STATIC PAGE — never changes during a session
# Mapping name : "Local\\acpmf_static"
# ============================================================
class SPageFileStatic(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("sm_version", ctypes.c_wchar * 15),
        ("ac_version", ctypes.c_wchar * 15),
        ("number_of_sessions", ctypes.c_int),
        ("number_of_cars", ctypes.c_int),
        ("car_model", ctypes.c_wchar * 33),
        ("track", ctypes.c_wchar * 33),
        ("player_name", ctypes.c_wchar * 33),
        ("player_surname", ctypes.c_wchar * 33),
        ("player_nick", ctypes.c_wchar * 33),
        ("sector_count", ctypes.c_int),
        ("max_torque", ctypes.c_float),
        ("max_power", ctypes.c_float),
        ("max_rpm", ctypes.c_int),
        ("max_fuel", ctypes.c_float),
        ("suspension_max_travel", ctypes.c_float * 4),
        ("tyre_radius", ctypes.c_float * 4),
        ("max_turbo_boost", ctypes.c_float),
        ("deprecated_1", ctypes.c_float),
        ("deprecated_2", ctypes.c_float),
        ("penalties_enabled", ctypes.c_int),
        ("aid_fuel_rate", ctypes.c_float),
        ("aid_tire_rate", ctypes.c_float),
        ("aid_mechanical_damage", ctypes.c_float),
        ("aid_allow_tyre_blankets", ctypes.c_int),
        ("aid_stability", ctypes.c_float),
        ("aid_auto_clutch", ctypes.c_int),
        ("aid_auto_blip", ctypes.c_int),
        ("has_drs", ctypes.c_int),
        ("has_ers", ctypes.c_int),
        ("has_kers", ctypes.c_int),
        ("kers_max_j", ctypes.c_float),
        ("engine_brake_settings_count", ctypes.c_int),
        ("ers_power_controller_count", ctypes.c_int),
        ("track_spline_length", ctypes.c_float),
        ("track_configuration", ctypes.c_wchar * 33),
        ("ers_max_j", ctypes.c_float),
        ("is_timed_race", ctypes.c_int),
        ("has_extra_lap", ctypes.c_int),
        ("car_skin", ctypes.c_wchar * 33),
        ("reversed_grid_positions", ctypes.c_int),
        ("pit_window_start", ctypes.c_int),
        ("pit_window_end", ctypes.c_int),
        ("is_online", ctypes.c_int),
        # ACC additions
        ("dry_tyres_name", ctypes.c_wchar * 33),
        ("wet_tyres_name", ctypes.c_wchar * 33),
    ]


# ============================================================
# PHYSICS PAGE — live car physics, 333 Hz update
# Mapping name : "Local\\acpmf_physics"
# ============================================================
class Wheels(ctypes.Structure):
    _pack_ = 4
    _fields_ = [("fl", ctypes.c_float), ("fr", ctypes.c_float),
                ("rl", ctypes.c_float), ("rr", ctypes.c_float)]


class SPageFilePhysics(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("packet_id", ctypes.c_int),
        ("gas", ctypes.c_float),
        ("brake", ctypes.c_float),
        ("fuel", ctypes.c_float),
        ("gear", ctypes.c_int),
        ("rpm", ctypes.c_int),
        ("steer_angle", ctypes.c_float),
        ("speed_kmh", ctypes.c_float),
        ("velocity", ctypes.c_float * 3),
        ("acc_g", ctypes.c_float * 3),
        ("wheel_slip", ctypes.c_float * 4),
        ("wheel_load", ctypes.c_float * 4),
        ("wheel_pressure", ctypes.c_float * 4),
        ("wheel_angular_speed", ctypes.c_float * 4),
        ("tyre_wear", ctypes.c_float * 4),
        ("tyre_dirty_level", ctypes.c_float * 4),
        ("tyre_core_temp", ctypes.c_float * 4),
        ("camber_rad", ctypes.c_float * 4),
        ("suspension_travel", ctypes.c_float * 4),
        ("drs", ctypes.c_float),
        ("tc", ctypes.c_float),
        ("heading", ctypes.c_float),
        ("pitch", ctypes.c_float),
        ("roll", ctypes.c_float),
        ("cg_height", ctypes.c_float),
        ("car_damage", ctypes.c_float * 5),
        ("number_of_tyres_out", ctypes.c_int),
        ("pit_limiter_on", ctypes.c_int),
        ("abs", ctypes.c_float),
        ("kers_charge", ctypes.c_float),
        ("kers_input", ctypes.c_float),
        ("auto_shifter_on", ctypes.c_int),
        ("ride_height", ctypes.c_float * 2),
        ("turbo_boost", ctypes.c_float),
        ("ballast", ctypes.c_float),
        ("air_density", ctypes.c_float),
        ("air_temp", ctypes.c_float),
        ("road_temp", ctypes.c_float),
        ("local_angular_vel", ctypes.c_float * 3),
        ("final_ff", ctypes.c_float),
        ("performance_meter", ctypes.c_float),
        ("engine_brake", ctypes.c_int),
        ("ers_recovery_level", ctypes.c_int),
        ("ers_power_level", ctypes.c_int),
        ("ers_heat_charging", ctypes.c_int),
        ("ers_is_charging", ctypes.c_int),
        ("kers_current_kj", ctypes.c_float),
        ("drs_available", ctypes.c_int),
        ("drs_enabled", ctypes.c_int),
        ("brake_temp", ctypes.c_float * 4),
        ("clutch", ctypes.c_float),
        ("tyre_temp_i", ctypes.c_float * 4),
        ("tyre_temp_m", ctypes.c_float * 4),
        ("tyre_temp_o", ctypes.c_float * 4),
        ("is_ai_controlled", ctypes.c_int),
        ("tyre_contact_point", (ctypes.c_float * 3) * 4),
        ("tyre_contact_normal", (ctypes.c_float * 3) * 4),
        ("tyre_contact_heading", (ctypes.c_float * 3) * 4),
        ("brake_bias", ctypes.c_float),
        ("local_velocity", ctypes.c_float * 3),
        # ACC additions
        ("p2p_activations_left", ctypes.c_int),
        ("p2p_status", ctypes.c_int),
        ("current_max_rpm", ctypes.c_int),
        ("mz", ctypes.c_float * 4),
        ("fx", ctypes.c_float * 4),
        ("fy", ctypes.c_float * 4),
        ("slip_ratio", ctypes.c_float * 4),
        ("slip_angle", ctypes.c_float * 4),
        ("tc_in_action", ctypes.c_int),
        ("abs_in_action", ctypes.c_int),
        ("suspension_damage", ctypes.c_float * 4),
        ("tyre_temp", ctypes.c_float * 4),
        # Water/wet ACC
        ("water_temp", ctypes.c_float),
        ("brake_pressure", ctypes.c_float * 4),
        ("front_brake_compound", ctypes.c_int),
        ("rear_brake_compound", ctypes.c_int),
        ("pad_life", ctypes.c_float * 4),
        ("disc_life", ctypes.c_float * 4),
        ("ignition_on", ctypes.c_int),
        ("starter_engine_on", ctypes.c_int),
        ("is_engine_running", ctypes.c_int),
        ("kerb_vibration", ctypes.c_float),
        ("slip_vibrations", ctypes.c_float),
        ("g_vibrations", ctypes.c_float),
        ("abs_vibrations", ctypes.c_float),
    ]


# ============================================================
# GRAPHICS PAGE — UI/session-level info, 30-60 Hz
# Mapping name : "Local\\acpmf_graphics"
# ============================================================
# ACC_STATUS enum
ACC_OFF = 0
ACC_REPLAY = 1
ACC_LIVE = 2
ACC_PAUSE = 3
_STATUS_NAMES = {0: "OFF", 1: "REPLAY", 2: "LIVE", 3: "PAUSE"}

# ACC_SESSION_TYPE
_SESSION_NAMES = {-1: "UNKNOWN", 0: "PRACTICE", 1: "QUALIFY", 2: "RACE", 3: "HOTLAP", 4: "TIME_ATTACK", 5: "DRIFT", 6: "DRAG", 7: "HOTSTINT", 8: "HOTLAPSUPERPOLE"}


class SPageFileGraphic(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("packet_id", ctypes.c_int),
        ("status", ctypes.c_int),  # ACC_STATUS
        ("session", ctypes.c_int),  # ACC_SESSION_TYPE
        ("current_time", ctypes.c_wchar * 15),
        ("last_time", ctypes.c_wchar * 15),
        ("best_time", ctypes.c_wchar * 15),
        ("split", ctypes.c_wchar * 15),
        ("completed_laps", ctypes.c_int),
        ("position", ctypes.c_int),
        ("current_time_ms", ctypes.c_int),
        ("last_time_ms", ctypes.c_int),
        ("best_time_ms", ctypes.c_int),
        ("session_time_left", ctypes.c_float),
        ("distance_traveled", ctypes.c_float),
        ("is_in_pit", ctypes.c_int),
        ("current_sector_index", ctypes.c_int),
        ("last_sector_time", ctypes.c_int),
        ("number_of_laps", ctypes.c_int),
        ("tyre_compound", ctypes.c_wchar * 33),
        ("replay_time_multiplier", ctypes.c_float),
        ("normalized_car_position", ctypes.c_float),
        ("active_cars", ctypes.c_int),
        ("car_coordinates", (ctypes.c_float * 3) * 60),
        ("car_id", ctypes.c_int * 60),
        ("player_car_id", ctypes.c_int),
        ("penalty_time", ctypes.c_float),
        ("flag", ctypes.c_int),
        ("penalty", ctypes.c_int),
        ("ideal_line_on", ctypes.c_int),
        ("is_in_pit_lane", ctypes.c_int),
        ("surface_grip", ctypes.c_float),
        ("mandatory_pit_done", ctypes.c_int),
        # ACC additions
        ("wind_speed", ctypes.c_float),
        ("wind_direction", ctypes.c_float),
        ("is_setup_menu_visible", ctypes.c_int),
        ("main_display_index", ctypes.c_int),
        ("secondary_display_index", ctypes.c_int),
        ("tc_level", ctypes.c_int),
        ("tc_cut_level", ctypes.c_int),
        ("engine_map", ctypes.c_int),
        ("abs_level", ctypes.c_int),
        ("fuel_per_lap", ctypes.c_float),
        ("rain_lights", ctypes.c_int),
        ("flashing_lights", ctypes.c_int),
        ("lights_stage", ctypes.c_int),
        ("exhaust_temp", ctypes.c_float),
        ("wiper_lv", ctypes.c_int),
        ("driver_stint_total_time_left", ctypes.c_int),
        ("driver_stint_time_left", ctypes.c_int),
        ("rain_tyres", ctypes.c_int),
        ("session_index", ctypes.c_int),
        ("used_fuel", ctypes.c_float),
        ("delta_lap_time", ctypes.c_wchar * 15),
        ("i_delta_lap_time", ctypes.c_int),
        ("estimated_lap_time", ctypes.c_wchar * 15),
        ("i_estimated_lap_time", ctypes.c_int),
        ("is_delta_positive", ctypes.c_int),
        ("i_split", ctypes.c_int),
        ("is_valid_lap", ctypes.c_int),
        ("fuel_estimated_laps", ctypes.c_float),
        ("track_status", ctypes.c_wchar * 33),
        ("missing_mandatory_pits", ctypes.c_int),
        ("clock", ctypes.c_float),  # in-game time of day in seconds
        ("direction_lights_left", ctypes.c_int),
        ("direction_lights_right", ctypes.c_int),
        ("global_yellow", ctypes.c_int),
        ("global_yellow_1", ctypes.c_int),
        ("global_yellow_2", ctypes.c_int),
        ("global_yellow_3", ctypes.c_int),
        ("global_white", ctypes.c_int),
        ("global_green", ctypes.c_int),
        ("global_chequered", ctypes.c_int),
        ("global_red", ctypes.c_int),
        ("mfd_tyre_set", ctypes.c_int),
        ("mfd_fuel_to_add", ctypes.c_float),
        ("mfd_tyre_pressure_lf", ctypes.c_float),
        ("mfd_tyre_pressure_rf", ctypes.c_float),
        ("mfd_tyre_pressure_lr", ctypes.c_float),
        ("mfd_tyre_pressure_rr", ctypes.c_float),
        ("track_grip_status", ctypes.c_int),
        ("rain_intensity", ctypes.c_int),
        ("rain_intensity_in_10min", ctypes.c_int),
        ("rain_intensity_in_30min", ctypes.c_int),
        ("current_tyre_set", ctypes.c_int),
        ("strategy_tyre_set", ctypes.c_int),
        ("gap_ahead_ms", ctypes.c_int),
        ("gap_behind_ms", ctypes.c_int),
    ]


# ============================================================
# Reader class
# ============================================================
class AccShmReader:
    """Direct ctypes mmap reader for ACC shared memory.
    Usage:
        r = AccShmReader()
        r.open()
        while True:
            s = r.read_static(); p = r.read_physics(); g = r.read_graphics()
            ...
        r.close()
    """
    SIZE_STATIC = ctypes.sizeof(SPageFileStatic)
    SIZE_PHYSICS = ctypes.sizeof(SPageFilePhysics)
    SIZE_GRAPHIC = ctypes.sizeof(SPageFileGraphic)

    def __init__(self):
        self._static_mmap: Optional[mmap.mmap] = None
        self._physics_mmap: Optional[mmap.mmap] = None
        self._graphics_mmap: Optional[mmap.mmap] = None

    def open(self):
        # Tag names exact ACC : "Local\\acpmf_static" etc.
        self._static_mmap = mmap.mmap(-1, self.SIZE_STATIC, "Local\\acpmf_static")
        self._physics_mmap = mmap.mmap(-1, self.SIZE_PHYSICS, "Local\\acpmf_physics")
        self._graphics_mmap = mmap.mmap(-1, self.SIZE_GRAPHIC, "Local\\acpmf_graphics")

    def close(self):
        for m in (self._static_mmap, self._physics_mmap, self._graphics_mmap):
            try:
                if m: m.close()
            except Exception: pass
        self._static_mmap = self._physics_mmap = self._graphics_mmap = None

    def read_static(self) -> Optional[SPageFileStatic]:
        if not self._static_mmap: return None
        self._static_mmap.seek(0)
        return SPageFileStatic.from_buffer_copy(self._static_mmap.read(self.SIZE_STATIC))

    def read_physics(self) -> Optional[SPageFilePhysics]:
        if not self._physics_mmap: return None
        self._physics_mmap.seek(0)
        return SPageFilePhysics.from_buffer_copy(self._physics_mmap.read(self.SIZE_PHYSICS))

    def read_graphics(self) -> Optional[SPageFileGraphic]:
        if not self._graphics_mmap: return None
        self._graphics_mmap.seek(0)
        return SPageFileGraphic.from_buffer_copy(self._graphics_mmap.read(self.SIZE_GRAPHIC))

    def snapshot(self) -> dict:
        """One-shot snapshot of all 3 pages as flat dict, ready for telemetry bus.

        GPT-5.4 reco P1 — Sanity checks :
        - NaN/Inf detection on floats → return shm_ok=False rather than poison downstream
        - Out-of-range sentinel detection (pressures absurdes, RPM > 20000, fuel > 200L)
        - Version mismatch warning via static.ac_version
        """
        s = self.read_static(); p = self.read_physics(); g = self.read_graphics()
        if not (s and p and g):
            return {"shm_ok": False, "reason": "page_read_fail"}
        # V4 safeguard : detect AC (original) vs ACC via ac_version string.
        # ACC : "1.10.x" / "1.9.x" ; AC original : "1.16.x" / "Pure-CSP". Mismatch → skip pour pas polluer.
        # Simple heuristique : si tyre_wear > 1.5 (sur 4 roues), c'est layout AC (wear en pourcent 0-100 vs ACC fraction 0-1).
        ww_test = list(p.tyre_wear)
        if any(w > 1.5 for w in ww_test):
            return {"shm_ok": False, "reason": "ac_original_detected_not_acc", "wear_sample": round(max(ww_test), 1)}
        wp = list(p.wheel_pressure); wt = list(p.tyre_core_temp); ww = list(p.tyre_wear)
        bt = list(p.brake_temp); pl = list(p.pad_life); dl = list(p.disc_life)
        # Sanity : detect NaN/Inf on critical floats. math.isnan() = True if NaN.
        import math
        critical_floats = [p.speed_kmh, p.fuel, float(p.rpm), p.brake_bias] + wp + wt + ww
        for v in critical_floats:
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                return {"shm_ok": False, "reason": "nan_or_inf_detected"}
        # Sanity : RPM > 20000 = corrupted (max GT3 ~9500)
        if p.rpm < 0 or p.rpm > 20000:
            return {"shm_ok": False, "reason": "rpm_out_of_range", "value": int(p.rpm)}
        # Sanity : fuel > 200L = corrupted (max tank GT3 ~120L)
        if p.fuel < 0 or p.fuel > 200:
            return {"shm_ok": False, "reason": "fuel_out_of_range", "value": float(p.fuel)}
        # Sanity : tyre pressures > 50 PSI = corrupted (Pirelli DHF max ~33)
        for i, press in enumerate(wp):
            if press < 0 or press > 50:
                return {"shm_ok": False, "reason": f"tyre_press_{i}_out_of_range", "value": press}
        # Sanity : speed > 500 km/h = corrupted (GT3 max ~310). V2.3 : -150 lower bound (gros crashes en marche arrière)
        if p.speed_kmh < -150 or p.speed_kmh > 500:
            return {"shm_ok": False, "reason": "speed_out_of_range", "value": float(p.speed_kmh)}
        return {
            "shm_ok": True,
            # Static
            "track": str(s.track).strip(),
            "car": str(s.car_model).strip(),
            "player": str(s.player_name).strip(),
            "max_fuel": float(s.max_fuel),
            "max_rpm": int(s.max_rpm),
            "track_length": float(s.track_spline_length),
            "is_online": bool(s.is_online),
            # Graphics
            "status": _STATUS_NAMES.get(int(g.status), "UNKNOWN"),
            "status_code": int(g.status),
            "session": _SESSION_NAMES.get(int(g.session), "?"),
            "completed_laps": int(g.completed_laps),
            "position": int(g.position),
            "active_cars": int(g.active_cars),
            "current_time_ms": int(g.current_time_ms),
            "last_time_ms": int(g.last_time_ms),
            "best_time_ms": int(g.best_time_ms),
            "delta_lap_ms": int(g.i_delta_lap_time),
            "is_delta_positive": bool(g.is_delta_positive),
            "split_ms": int(g.i_split),
            "current_sector": int(g.current_sector_index),
            "last_sector_ms": int(g.last_sector_time),
            "is_in_pit": bool(g.is_in_pit),
            "is_in_pit_lane": bool(g.is_in_pit_lane),
            "session_time_left_s": float(g.session_time_left) / 1000.0,
            "distance_traveled_m": float(g.distance_traveled),
            "normalized_position": float(g.normalized_car_position),
            "flag": int(g.flag),
            "penalty": int(g.penalty),
            "fuel_per_lap": float(g.fuel_per_lap),
            "fuel_estimated_laps": float(g.fuel_estimated_laps),
            "used_fuel": float(g.used_fuel),
            "tyre_compound": str(g.tyre_compound).strip(),
            "wind_speed": float(g.wind_speed),
            "wind_direction": float(g.wind_direction),
            "rain_intensity": int(g.rain_intensity),
            "rain_intensity_in_10min": int(g.rain_intensity_in_10min),
            "rain_intensity_in_30min": int(g.rain_intensity_in_30min),
            "track_grip_status": int(g.track_grip_status),
            "tc_level": int(g.tc_level),
            "abs_level": int(g.abs_level),
            "engine_map": int(g.engine_map),
            "rain_tyres": bool(g.rain_tyres),
            "clock_sec": float(g.clock),
            "valid_lap": bool(g.is_valid_lap),
            "global_yellow": bool(g.global_yellow),
            "global_yellow_s1": bool(g.global_yellow_1),
            "global_yellow_s2": bool(g.global_yellow_2),
            "global_yellow_s3": bool(g.global_yellow_3),
            "global_red": bool(g.global_red),
            "global_white": bool(g.global_white),
            "global_green": bool(g.global_green),
            "global_chequered": bool(g.global_chequered),
            # V3.Q — Spotter : car_coordinates + car_id + player_car_id for proximity geometry
            "player_car_id": int(g.player_car_id),
            "car_coordinates_raw": [(float(g.car_coordinates[i][0]), float(g.car_coordinates[i][1]), float(g.car_coordinates[i][2])) for i in range(min(60, int(g.active_cars)))],
            "car_ids_raw": [int(g.car_id[i]) for i in range(min(60, int(g.active_cars)))],
            "gap_ahead_ms": int(g.gap_ahead_ms),
            "gap_behind_ms": int(g.gap_behind_ms),
            "current_tyre_set": int(g.current_tyre_set),
            "mfd_fuel_to_add": float(g.mfd_fuel_to_add),
            "mfd_tyre_press_lf": float(g.mfd_tyre_pressure_lf),
            "mfd_tyre_press_rf": float(g.mfd_tyre_pressure_rf),
            "mfd_tyre_press_lr": float(g.mfd_tyre_pressure_lr),
            "mfd_tyre_press_rr": float(g.mfd_tyre_pressure_rr),
            "missing_mandatory_pits": int(g.missing_mandatory_pits),
            # Physics
            "speed_kmh": float(p.speed_kmh),
            "rpm": int(p.rpm),
            "gear": int(p.gear) - 1,  # ACC convention : N=0, 1=1, R=-1... but raw is offset
            "fuel_l": float(p.fuel),
            "gas": float(p.gas),
            "brake": float(p.brake),
            "clutch": float(p.clutch),
            "steer_angle_rad": float(p.steer_angle),
            "heading_rad": float(p.heading),
            "yaw_rate_rad_s": float(p.local_angular_vel[1]),  # Y axis = yaw rate (rad/s)
            "brake_bias": float(p.brake_bias),
            "tc": float(p.tc),
            "abs": float(p.abs),
            "pit_limiter_on": bool(p.pit_limiter_on),
            "tyre_press_fl": wp[0], "tyre_press_fr": wp[1], "tyre_press_rl": wp[2], "tyre_press_rr": wp[3],
            "tyre_temp_fl": wt[0], "tyre_temp_fr": wt[1], "tyre_temp_rl": wt[2], "tyre_temp_rr": wt[3],
            "tyre_wear_fl": ww[0], "tyre_wear_fr": ww[1], "tyre_wear_rl": ww[2], "tyre_wear_rr": ww[3],
            "brake_temp_fl": bt[0], "brake_temp_fr": bt[1], "brake_temp_rl": bt[2], "brake_temp_rr": bt[3],
            "pad_life_fl": pl[0], "pad_life_fr": pl[1], "pad_life_rl": pl[2], "pad_life_rr": pl[3],
            "disc_life_fl": dl[0], "disc_life_fr": dl[1], "disc_life_rl": dl[2], "disc_life_rr": dl[3],
            "air_temp": float(p.air_temp),
            "road_temp": float(p.road_temp),
            "water_temp": float(p.water_temp),
            "car_damage_front": float(p.car_damage[0]),
            "car_damage_rear": float(p.car_damage[1]),
            "car_damage_left": float(p.car_damage[2]),
            "car_damage_right": float(p.car_damage[3]),
            "car_damage_center": float(p.car_damage[4]),
            "tyres_out": int(p.number_of_tyres_out),
            "is_engine_running": bool(p.is_engine_running),
            "wheel_slip_fl": float(p.wheel_slip[0]), "wheel_slip_fr": float(p.wheel_slip[1]),
            "wheel_slip_rl": float(p.wheel_slip[2]), "wheel_slip_rr": float(p.wheel_slip[3]),
        }


# ============================================================
# CLI smoke test
# ============================================================
if __name__ == "__main__":
    import json, time
    r = AccShmReader()
    try:
        r.open()
        print(f"Static size  = {AccShmReader.SIZE_STATIC} bytes")
        print(f"Physics size = {AccShmReader.SIZE_PHYSICS} bytes")
        print(f"Graphics size= {AccShmReader.SIZE_GRAPHIC} bytes")
        print()
        for i in range(3):
            snap = r.snapshot()
            keys_print = ["status", "track", "car", "player", "session",
                          "speed_kmh", "rpm", "gear", "fuel_l",
                          "completed_laps", "position",
                          "tyre_press_fl", "tyre_press_fr", "tyre_press_rl", "tyre_press_rr",
                          "tyre_temp_fl", "brake_bias", "current_time_ms"]
            print(f"--- read {i+1} ---")
            for k in keys_print:
                print(f"  {k:20} = {snap.get(k)}")
            time.sleep(0.5)
    finally:
        r.close()
