"""Bono v2 — Core service (orchestrateur).

Threads :
- shm_loop : 30 Hz SHM watcher → publishes snapshot via ZMQ PUB (CONFLATE)
- audio_loop : ZMQ PULL audio.ptt → Deepgram transcribe → LLM → publish play.text
- events_loop : detect auto-events on snapshot delta (lap_completed, fuel_low, etc.) → fire
- fastapi : HTTP dashboard + state introspection

State partagé : `_state.snapshot` (latest SHM dict) + `_state.session_id` + `_state.last_exchange`.
"""
import sys
import os
import time
import threading
import asyncio
import json
import io
import wave
from pathlib import Path
from typing import Optional

import zmq
import orjson
import httpx
from loguru import logger
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import uvicorn
from anthropic import Anthropic

sys.path.insert(0, str(Path(__file__).parent))
from config import (
    ZMQ_AUDIO_INPUT, ZMQ_TELEMETRY_PUB, ZMQ_PLAYBACK_REQ, ZMQ_EVENTS,
    SHM_POLL_HZ, ANTHROPIC_MODELS, LLM_MAX_TOKENS, LLM_TEMPERATURE_RACE,
    LLM_TEMPERATURE_PADDOCK, DEEPGRAM_MODEL, DEEPGRAM_LANGUAGE,
    HTTP_PORT, get_secrets,
)
from zmq_bus import make_pub, make_pull, make_push, send_json, send_bytes
from shm.acc_ctypes import AccShmReader
from prompts.persona_bono import build_system_prompt
from tools.registry import get_all_schemas, dispatch
from tools.racing_tools import set_snapshot_provider, set_turn_snapshot, clear_turn_snapshot  # also triggers tool registration via import
import memory


# ============================================================
# Shared state
# ============================================================
class State:
    snapshot: dict = {}
    snapshot_lock = threading.Lock()  # Gemini #2 audit : SHM atomic snapshot
    session_id: Optional[int] = None
    last_exchange: dict = {}
    started_at = time.time()
    metrics = {"ptt_count": 0, "llm_calls": 0, "tts_calls": 0, "events_fired": 0,
               "errors": 0, "avg_stt_ms": 0, "avg_llm_ms": 0, "dropped_zmq": 0,
               "total_cost_usd_session": 0.0, "killswitch_triggered": False}
    last_event_ts: dict[str, float] = {}  # cooldown tracking
    last_lap_count = 0
    # V3.B : track last lap for lap_history logging
    last_logged_lap = 0
    # V3.F : sector tracking (live S1/S2/S3 from sector_index transitions)
    last_sector_index = 0
    current_lap_sectors: list = []  # [s1_ms, s2_ms, s3_ms] for current lap, reset on new lap
    # V3.O : driving trace accumulator (resets each sector)
    sector_speed_min: float = 1e9
    sector_speed_min_pos: float = 0.0
    sector_brake_max: float = 0.0
    sector_throttle_release_pos: float = 0.0
    sector_was_on_throttle: bool = False
    sector_slip_max: float = 0.0
    sector_tc_active_frames: int = 0
    sector_abs_active_frames: int = 0
    sector_total_frames: int = 0
    # turn_id system (GPT audit) : nouveau PTT/event invalide les précédents en attente
    current_turn_id: int = 0
    invalidated_turns: set = set()
    # V2.3 dev : fake mode for testing auto-events + tools without real ACC
    fake_mode: bool = False
    fake_snapshot: dict | None = None
    # Bug 2 fix complet : flag pour éviter de spammer update_session_meta() à chaque tick SHM (30 Hz)
    session_meta_filled: bool = False
    # V4 tracking — pour opponent pit detection, U/O patterns, rain history, sector deltas
    last_seen_opponents: dict = None  # {car_id: (last_ts, last_pos_xz)}
    last_known_rain_in_10: int = 0
    last_known_rain_in_30: int = 0
    last_uo_event_ts: float = 0.0
    last_brake_temp_event_ts: float = 0.0
    last_fuel_save_event_ts: float = 0.0
    last_pace_dictation_lap: int = 0
    # V5 batch1 — tracking "vrai Bono" : pre-session, out-lap, position, gaps, race countdown
    session_briefed: bool = False
    out_lap_announced_lap: int = -1
    warmup_complete_announced: bool = False
    last_position: int = -1
    last_gap_behind_ms: int = 0
    last_gap_behind_ts: float = 0.0
    chequered_announced: bool = False
    laps_remaining_announced: set = None  # {5, 2, 1}
    last_damage_total: float = 0.0
    last_water_temp_event_ts: float = 0.0
    race_intro_announced: bool = False
    last_in_pit_lane: bool = False
    last_lap_announced: bool = False
    pre_race_check_announced: bool = False
    # Session-transition latch reset (consensus 3 IAs 17/05) : les hysteresis_check sont
    # "latched once" — un fuel_warning RACE doit pouvoir refire si on retourne en PRACTICE
    # puis re-RACE. On track la combinaison (session_type, session_id) et reset _event_latch
    # quand elle change. Auto-events transients (yellow_flag, traffic, spotter) restent
    # gérés par leur propre exit_condition.
    last_session_signature: Optional[str] = None
    # Pit-exit latch reset : driver retourne en piste après pit stop → reset des warnings
    # actionnables one-shot (fuel_warning, tyre_cliff, brake_temp_high) pour qu'ils puissent
    # refire si conditions toujours vraies dans le nouveau stint.
    last_in_pit_at_reset: bool = False


_state = State()
_state.last_seen_opponents = {}
_state.laps_remaining_announced = set()
_state_lock = threading.Lock()  # for current_turn_id

# Phase A1 (brief V3 17/05) : mutex GLOBAL pour serialiser tous les appels Anthropic.
# Prévient race condition entre handle_ptt (réactif) et futur strategist_service (proactif).
# Sans ce lock, 2 LLM calls concurrents = double facturation, risque message coupé,
# explosion latence (Anthropic API a une rate limit per-account, pas per-thread).
_llm_global_lock = threading.Lock()


# Anthropic pricing per million tokens (haiku/sonnet/opus 4.5/4.6/4.7)
ANTHROPIC_COSTS = {
    "claude-haiku-4-5-20251001": (1.0, 5.0),   # in / out per M tokens
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4-7": (15.0, 75.0),
}
DAILY_COST_LIMIT_USD = float(os.environ.get("BONO_DAILY_COST_LIMIT", "5.0"))


def compute_cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    prices = ANTHROPIC_COSTS.get(model, (1.0, 5.0))
    return (tokens_in / 1_000_000) * prices[0] + (tokens_out / 1_000_000) * prices[1]


def can_afford_call(model: str = "claude-haiku-4-5-20251001") -> tuple[bool, str]:
    """Cost kill-switch (consensus 3 IA). Returns (can_proceed, reason)."""
    cutoff = time.time() - 86400
    try:
        conn = memory.get_conn()
        spent = conn.execute("SELECT COALESCE(SUM(cost_usd),0) AS t FROM costs WHERE ts>=?", (cutoff,)).fetchone()["t"]
        conn.close()
    except Exception:
        spent = 0
    if spent >= DAILY_COST_LIMIT_USD:
        return False, f"daily_budget_exceeded ({spent:.2f}/{DAILY_COST_LIMIT_USD:.2f} USD)"
    if model == "claude-opus-4-7" and spent >= DAILY_COST_LIMIT_USD * 0.8:
        return False, f"opus_blocked_at_80pct ({spent:.2f}/{DAILY_COST_LIMIT_USD:.2f})"
    return True, "ok"


def new_turn_id() -> int:
    with _state_lock:
        _state.current_turn_id += 1
        # Invalidate all previous turns (cancellation policy)
        if _state.current_turn_id > 1:
            _state.invalidated_turns.add(_state.current_turn_id - 1)
        # Prune set if too big
        if len(_state.invalidated_turns) > 100:
            _state.invalidated_turns = set(t for t in _state.invalidated_turns if t >= _state.current_turn_id - 50)
        return _state.current_turn_id


def is_turn_valid(turn_id: int) -> bool:
    with _state_lock:
        return turn_id == _state.current_turn_id and turn_id not in _state.invalidated_turns


def get_snapshot() -> dict:
    """Thread-safe snapshot read (Gemini #2 audit : atomic copy).
    V2.3 dev : if fake_mode active, returns fake_snapshot for auto-event/tools testing."""
    if _state.fake_mode and _state.fake_snapshot is not None:
        return dict(_state.fake_snapshot)
    with _state.snapshot_lock:
        return dict(_state.snapshot)  # shallow copy is enough (values are primitives)


set_snapshot_provider(get_snapshot)


# ============================================================
# SHM loop : 30 Hz read → publish snapshot
# ============================================================
def shm_loop(stop_event: threading.Event):
    logger.info(f"[shm] loop start @ {SHM_POLL_HZ}Hz")
    pub = make_pub(ZMQ_TELEMETRY_PUB, conflate=True)
    pub.setsockopt(zmq.SNDTIMEO, 100)
    pub.setsockopt(zmq.SNDHWM, 10)
    time.sleep(0.3)
    reader = AccShmReader()
    period = 1.0 / SHM_POLL_HZ
    try:
        reader.open()
    except Exception as e:
        logger.error(f"[shm] open fail : {e}")
        return
    try:
        while not stop_event.is_set():
            try:
                snap = reader.snapshot()
                snap["read_at"] = time.time()
                # V3.O — driving trace per-sector accumulator
                if snap.get("shm_ok") and snap.get("status") == "LIVE" and snap.get("speed_kmh", 0) > 1:
                    sec_idx = snap.get("current_sector", 0)
                    speed = snap.get("speed_kmh", 0)
                    brake_v = snap.get("brake", 0)  # 0-1
                    gas_v = snap.get("gas", 0)
                    norm_pos = snap.get("normalized_position", 0)
                    slip_arr = [snap.get(f"wheel_slip_{p}", 0) for p in ("fl","fr","rl","rr")]
                    slip_max_now = max(slip_arr) if slip_arr else 0
                    tc_active = snap.get("tc", 0) > 0
                    abs_active = snap.get("abs", 0) > 0
                    # Detect sector transition → log previous sector's trace
                    if sec_idx != _state.last_sector_index and _state.last_sector_index >= 0:
                        # Fix 17/05 B7 : capturer le SECTEUR TIME qui vient de finir pour log_lap_metric.
                        # ACC SHM expose `last_sector_ms` qui contient le temps du dernier secteur complété.
                        # On l'append à current_lap_sectors (reset à new lap dans detect_auto_events).
                        # Avant ce fix : current_lap_sectors restait vide → log_lap_metric écrivait s1/s2/s3=0
                        # alors que driving_trace avait toute la data détaillée (désync 2 tables).
                        last_sec_ms = snap.get("last_sector_ms", 0)
                        if last_sec_ms and last_sec_ms > 0 and len(_state.current_lap_sectors) < 3:
                            _state.current_lap_sectors.append(int(last_sec_ms))
                        # Sector just changed : log accumulator for the previous sector
                        if _state.session_id and _state.sector_total_frames > 0:
                            try:
                                memory.log_driving_trace(
                                    session_id=_state.session_id,
                                    lap_num=snap.get("completed_laps", 0),
                                    sector_index=_state.last_sector_index,
                                    speed_min_kmh=round(_state.sector_speed_min, 1) if _state.sector_speed_min < 1e8 else 0,
                                    speed_min_at_position=round(_state.sector_speed_min_pos, 4),
                                    brake_max_pct=round(_state.sector_brake_max * 100, 1),
                                    throttle_release_position=round(_state.sector_throttle_release_pos, 4),
                                    wheel_slip_max=round(_state.sector_slip_max, 3),
                                    tc_active_pct=round(_state.sector_tc_active_frames / max(_state.sector_total_frames, 1) * 100, 1),
                                    abs_active_pct=round(_state.sector_abs_active_frames / max(_state.sector_total_frames, 1) * 100, 1),
                                    track=snap.get("track", ""),
                                    car=snap.get("car", ""),
                                )
                            except Exception as e:
                                logger.debug(f"[trace] log fail : {e}")
                        # Reset accumulator for new sector
                        _state.sector_speed_min = 1e9
                        _state.sector_speed_min_pos = 0.0
                        _state.sector_brake_max = 0.0
                        _state.sector_throttle_release_pos = 0.0
                        _state.sector_was_on_throttle = False
                        _state.sector_slip_max = 0.0
                        _state.sector_tc_active_frames = 0
                        _state.sector_abs_active_frames = 0
                        _state.sector_total_frames = 0
                        _state.last_sector_index = sec_idx
                    # Update accumulator for current sector
                    if speed < _state.sector_speed_min:
                        _state.sector_speed_min = speed
                        _state.sector_speed_min_pos = norm_pos
                    if brake_v > _state.sector_brake_max:
                        _state.sector_brake_max = brake_v
                    # Throttle release : was on throttle, now off
                    if gas_v > 0.5:
                        _state.sector_was_on_throttle = True
                    elif _state.sector_was_on_throttle and gas_v < 0.2 and _state.sector_throttle_release_pos == 0.0:
                        _state.sector_throttle_release_pos = norm_pos
                        _state.sector_was_on_throttle = False
                    if slip_max_now > _state.sector_slip_max:
                        _state.sector_slip_max = slip_max_now
                    if tc_active: _state.sector_tc_active_frames += 1
                    if abs_active: _state.sector_abs_active_frames += 1
                    _state.sector_total_frames += 1
                # Atomic store (Gemini #2)
                with _state.snapshot_lock:
                    _state.snapshot = snap
                try:
                    send_json(pub, "tele.snapshot", snap)
                except zmq.error.Again:
                    _state.metrics["dropped_zmq"] += 1
                # Bug 2 fix complet : back-fill session track/car/type dès qu'on a SHM valide
                # (sinon track/car=unknown si Dan ne complète pas un seul lap)
                if not _state.session_meta_filled and _state.session_id and snap.get("shm_ok"):
                    tr = snap.get("track", "")
                    cr = snap.get("car", "")
                    st = (snap.get("session") or "").upper()
                    if tr and cr and tr.lower() not in ("", "unknown") and cr.lower() not in ("", "unknown"):
                        try:
                            memory.update_session_meta(_state.session_id, track=tr, car=cr, session_type=st)
                            _state.session_meta_filled = True
                            logger.info(f"[shm] session meta back-filled: track={tr} car={cr} type={st}")
                        except Exception as e:
                            logger.warning(f"[shm] update_session_meta fail: {e}")
                detect_auto_events(snap)
            except Exception as e:
                logger.warning(f"[shm] read err : {e}")
                with _state.snapshot_lock:
                    _state.snapshot = {"shm_ok": False, "error": str(e)}
            time.sleep(period)
    finally:
        reader.close()
        logger.info("[shm] loop stop")


# ============================================================
# Auto-event detector (state delta + hysteresis + edge detection + latching)
# Anti-spam : enter threshold, exit threshold, latched state, confirm delay, dedup key.
# ============================================================
# Per-event latching state:
#   active = currently above threshold (latched until exit threshold met)
#   first_seen_ts = when entry threshold was first crossed (confirm delay accumulator)
#   last_fire_ts = last actual fire time (cooldown gate)
#   last_dedup_key = unique identifier (lap, threshold_band, ...) to suppress repeat fire
_event_latch: dict[str, dict] = {}


def can_fire(event_type: str, cooldown_s: float = 30.0) -> bool:
    """Legacy cooldown-only gate. Kept for backward compat with calls that don't need full hysteresis."""
    now = time.time()
    last = _state.last_event_ts.get(event_type, 0)
    if now - last < cooldown_s: return False
    _state.last_event_ts[event_type] = now
    return True


def hysteresis_check(event_type: str, condition: bool,
                     confirm_delay_s: float = 0.0, exit_condition: bool | None = None,
                     dedup_key: str = "", cooldown_s: float = 30.0,
                     reset_delay_s: float = 0.5) -> bool:
    """Returns True iff the event should fire NOW under hysteresis rules.

    V2.3 Gemini reco : `reset_delay_s` debounce on confirm-window. When `condition`
    flickers False briefly during the confirm_delay accumulation, we don't immediately
    reset `first_seen_ts` — we tolerate up to reset_delay_s of False before truly resetting.
    This protects against noisy SHM signals (wheel slip, RPM at engine cutoff).

    Args:
      event_type: unique identifier of the event family
      condition: True if entry threshold currently met
      confirm_delay_s: condition must hold continuously this many seconds before fire (default 0 = immediate)
      exit_condition: explicit exit threshold; if None, uses (not condition). Latching only releases when exit_condition True.
      dedup_key: extra uniqueness key (e.g. f"lap={lap}|band={band}"). Same key won't refire while latched.
      cooldown_s: minimum seconds between fires even if dedup_key differs
      reset_delay_s: debounce window on confirm-side (default 0.5s — handle telemetry noise)
    """
    now = time.time()
    st = _event_latch.setdefault(event_type, {
        "active": False, "first_seen_ts": 0.0, "last_fire_ts": 0.0,
        "last_dedup_key": "", "last_false_ts": 0.0,
    })
    # Exit detection (release latch)
    if st["active"]:
        ex = exit_condition if exit_condition is not None else (not condition)
        if ex:
            st["active"] = False
            st["first_seen_ts"] = 0.0
            st["last_false_ts"] = 0.0
            logger.debug(f"[hyst] {event_type} EXIT")
        else:
            # Still latched, no refire unless dedup_key changes AND cooldown elapsed
            if dedup_key and dedup_key != st["last_dedup_key"] and (now - st["last_fire_ts"]) >= cooldown_s:
                st["last_fire_ts"] = now
                st["last_dedup_key"] = dedup_key
                return True
            return False
    # Not latched : check entry threshold with debounce
    if not condition:
        # Track when we last saw False — only reset first_seen_ts after reset_delay_s of continuous False
        if st["first_seen_ts"] > 0.0:
            if st["last_false_ts"] == 0.0:
                st["last_false_ts"] = now
            elif (now - st["last_false_ts"]) >= reset_delay_s:
                # True debounce expired : reset accumulator
                st["first_seen_ts"] = 0.0
                st["last_false_ts"] = 0.0
        return False
    # condition True : clear False-tracker
    st["last_false_ts"] = 0.0
    if st["first_seen_ts"] == 0.0:
        st["first_seen_ts"] = now
    # Confirm delay (anti-flicker)
    if now - st["first_seen_ts"] < confirm_delay_s:
        return False
    # Cooldown gate
    if (now - st["last_fire_ts"]) < cooldown_s:
        return False
    # Fire and latch
    st["active"] = True
    st["last_fire_ts"] = now
    st["last_dedup_key"] = dedup_key
    return True


# Fix 17/05 F10 : on inverse la logique. Avant : whitelist `_LATCHES_RESET_ON_SESSION_CHANGE` qui
# devait être maintenue à jour à chaque nouveau hysteresis_check ajouté → invariant fragile.
# Maintenant : blacklist `_LATCHES_NEVER_RESET` (les events transients dont l'exit_condition gère
# le re-fire correctement). Tout le reste est reset par défaut en transition session.
# Quand on ajoute un nouvel event, par défaut il sera reset proprement → invariant solide.
_LATCHES_NEVER_RESET = {
    # Transients : exit_condition gère déjà le re-fire (yellow tombe → re-fire si yellow revient).
    "yellow_flag", "traffic_on_push_lap",
    # Edge-triggered idempotent : géré par dedup_key (lap_num), pas de latch persistant.
    "lap_completed", "personal_best",
    # Position changes : event par delta, pas de latch.
    "position_change",
}

_LATCHES_RESET_ON_PIT_EXIT = {
    # Re-fire après pit stop : conditions ont pu changer (fuel/tyres neufs ou pas).
    "fuel_warning", "fuel_critical", "tyre_cliff",
    "brake_temp_high", "brake_temp_critical",
}

# Fix F2 : State flags `_announced` qu'on doit reset à chaque vraie nouvelle session
# (changement de track/car/session_type côté SHM, pas juste session_id Bono).
# Sans ça, un `session_briefed=True` du combo précédent fait skip le brief de la nouvelle course.
_STATE_FLAGS_RESET_ON_SESSION_CHANGE = [
    ("session_briefed", False),
    ("out_lap_announced_lap", -1),
    ("warmup_complete_announced", False),
    ("chequered_announced", False),
    ("race_intro_announced", False),
    ("last_lap_announced", False),
    ("pre_race_check_announced", False),
    ("session_meta_filled", False),
    ("last_lap_count", 0),
    ("last_logged_lap", 0),
    ("last_position", -1),
    ("last_gap_behind_ms", 0),
    ("last_damage_total", 0.0),
    ("current_lap_sectors", None),   # spécial : reset à []
    ("laps_remaining_announced", None),  # spécial : reset à set()
]


def _reset_state_for_new_session():
    """Centralisé : reset les State flags one-shot d'une session.
    Avant ce fix (audit 17/05) : flags éparpillés, certains restaient True du précédent run
    → brief skip silencieux. Cf. post-mortem `feature added without invariant update`."""
    for attr, default in _STATE_FLAGS_RESET_ON_SESSION_CHANGE:
        if attr == "current_lap_sectors":
            _state.current_lap_sectors = []
        elif attr == "laps_remaining_announced":
            _state.laps_remaining_announced = set()
        else:
            setattr(_state, attr, default)
    logger.info("[state] reset session-scoped flags (one-shot announces, lap counters, position)")


def reset_latches_on_session_transition(snap: dict):
    """Detect transition (track/car/session_type/sid) → clear latches + reset State flags.
    Called at top of detect_auto_events on every tick.

    Fix F10 17/05 : blacklist (NEVER_RESET) au lieu de whitelist → invariant solide quand
    on ajoute de nouveaux events. Tout latch sauf transients est reset par défaut.

    Fix F2 17/05 : reset aussi les State flags `_announced` (session_briefed, race_intro, etc.)
    via _reset_state_for_new_session(). Sans ça, le brief de la nouvelle course est skippé
    silencieusement car le flag du combo précédent reste True.

    La signature inclut TRACK + CAR (pas juste session_id) car Bono garde le même session_id
    pendant tout son uptime ; la vraie "nouvelle session" se voit via SHM track/car change.
    """
    session_type = (snap.get("session") or "").upper()
    track = (snap.get("track") or "").lower()
    car = (snap.get("car") or "").lower()
    sid = _state.session_id or 0
    signature = f"{track}|{car}|{session_type}|{sid}"
    if signature != _state.last_session_signature:
        if _state.last_session_signature is not None:
            cleared = 0
            for ev in list(_event_latch.keys()):
                if ev not in _LATCHES_NEVER_RESET:
                    _event_latch.pop(ev, None)
                    cleared += 1
            logger.info(f"[latch] session transition {_state.last_session_signature} → {signature} : reset {cleared} latches")
            _reset_state_for_new_session()
        _state.last_session_signature = signature

    # Pit-exit transition : driver was in pit, no longer is → reset stint latches
    in_pit = bool(snap.get("is_in_pit", False))
    if _state.last_in_pit_at_reset and not in_pit:
        cleared = 0
        for ev in _LATCHES_RESET_ON_PIT_EXIT:
            if ev in _event_latch:
                _event_latch.pop(ev, None)
                cleared += 1
        if cleared > 0:
            logger.info(f"[latch] pit-exit detected : reset {cleared} stint latches")
    _state.last_in_pit_at_reset = in_pit


def fmt_fuel_laps_fr(laps: float) -> str:
    """Format fuel_laps remaining → speakable FR.
    1.0 → '1 tour', 2.0 → '2 tours', 2.5 → '2 tours et demi',
    0.4 → 'moins d'un tour', 0 → 'aucun tour'.
    """
    if laps is None or laps < 0.05:
        return "aucun tour"
    if laps < 0.6:
        return "moins d'un tour"
    whole = int(laps)
    frac = laps - whole
    if 0.4 <= frac <= 0.6:
        return f"{whole} tour{'s' if whole>1 else ''} et demi"
    if frac < 0.4:
        return f"{whole} tour{'s' if whole>1 else ''}"
    return f"{whole+1} tour{'s' if (whole+1)>1 else ''}"


def fmt_delta_fr(delta_s: float) -> str:
    """Format lap-time delta → speakable FR motorsport.
    0.05 → 'cinq centièmes', 0.10 → 'un dixième', 0.30 → 'trois dixièmes',
    0.50 → 'une demi-seconde', 0.478 → 'environ une demi-seconde',
    1.0 → 'une seconde', 2.3 → 'deux secondes trois'.
    """
    a = abs(delta_s)
    if a < 0.025:
        return "même temps"
    if a < 0.075:
        return "cinq centièmes"
    if a < 0.15:
        return "un dixième"
    tenths = int(round(a * 10))
    if a < 0.45:
        names = {2:"deux dixièmes",3:"trois dixièmes",4:"quatre dixièmes"}
        return names.get(tenths, f"{tenths} dixièmes")
    if a < 0.55:
        return "une demi-seconde"
    if a < 0.95:
        return f"{tenths} dixièmes"
    # >= 1s
    secs = int(a)
    rem_tenths = int(round((a - secs) * 10))
    if rem_tenths == 0:
        return f"{secs} seconde{'s' if secs>1 else ''}"
    return f"{secs} seconde{'s' if secs>1 else ''} {rem_tenths}"


def fmt_lap_compact(ms: int) -> str:
    """Format lap_time_ms → compact non-ambigu pour LLM context.
    Convention F1/GT3 : mm:ss.f. Anti-hallucination "1:10.5" (Gemini diag 17/05) :
    le LLM lit "1:50.567" sans conversion possible. Raw seconds (110.5s) =
    risque "un dix point cinq".
    110567 ms -> '1:50.6'
     59800 ms -> '0:59.8'
       450 ms -> '0:00.5'
    """
    if not ms or ms <= 0:
        return "?:??.?"
    total_s = ms / 1000.0
    mins = int(total_s // 60)
    secs = total_s - mins * 60
    return f"{mins}:{secs:04.1f}"


def format_lap_time_fr(ms: int) -> str:
    """Format lap_time_ms vers texte TTS-friendly motorsport-style (FR).
    Convention F1/GT3 : mm:ss.fff. Sortie text speakable.
    110615 ms -> '1 minute 50 secondes 6'
     59800 ms -> '59 secondes 8'
       450 ms -> '0 seconde 4' (edge case)
    """
    if not ms or ms <= 0:
        return "temps invalide"
    total_s = ms / 1000.0
    mins = int(total_s // 60)
    secs = total_s - mins * 60
    # On garde 1 décimale (style radio F1 "one fifty point six")
    secs_int = int(secs)
    tenths = int(round((secs - secs_int) * 10))
    if tenths == 10:
        secs_int += 1
        tenths = 0
        if secs_int == 60:
            mins += 1
            secs_int = 0
    if mins > 0:
        return f"{mins} minute{'s' if mins>1 else ''} {secs_int} seconde{'s' if secs_int>1 else ''} {tenths}"
    return f"{secs_int} seconde{'s' if secs_int>1 else ''} {tenths}"


def fire_event(event_type: str, severity: str, message: str, payload: dict | None = None, ttl_s: float = 8.0):
    """Fire event to playback queue with TTL (drop if stale at play time).
    Fix 0.1 brief V3 17/05 nuit : silence_zones DROP au lieu d'EXTEND TTL.
    Avant : event info hard-braking → TTL étendu à 10s → joué APRÈS le freinage, mais
    obsolète (info "pace_drift" pendant attaque T1 = no value 5s après).
    Maintenant : event info en zone critique = DROP silencieux. Events warn/critical
    passent toujours (yellow_flag, fuel_critical, brake_temp_critical, tyre_cliff)."""
    _state.metrics["events_fired"] += 1
    snap = _state.snapshot if not _state.fake_mode else (_state.fake_snapshot or {})
    if snap and severity == "info":
        try:
            brake = snap.get("brake", 0) or 0
            speed = snap.get("speed_kmh", 0) or 0
            steer = abs(snap.get("steer_angle_rad", 0) or 0)
            rpm = snap.get("rpm", 0) or 0
            # 1. Hard braking entry (corner approach)
            if brake > 0.6 and speed > 150:
                logger.info(f"[event] silence_drop type={event_type} reason=hard_braking_entry brake={brake:.2f} speed={speed:.0f}")
                return
            # 2. Mid-corner (low speed + significant steering)
            if 60 < speed < 130 and steer > 0.3:
                logger.info(f"[event] silence_drop type={event_type} reason=mid_corner speed={speed:.0f} steer={steer:.2f}")
                return
            # 3. High RPM push-lap straight (concentration max)
            if rpm > 7800 and speed > 220:
                logger.info(f"[event] silence_drop type={event_type} reason=high_rpm_straight rpm={rpm} speed={speed:.0f}")
                return
        except Exception as e:
            logger.debug(f"[event] silence_zone check err: {e}")
    logger.info(f"[event] {severity.upper()} {event_type}: {message}")
    if _state.session_id:
        event_id = memory.log_event(_state.session_id, event_type, severity, payload or {"msg": message})
        # Fix F3 17/05 : corrélation event <-> état véhicule pour post-session debrief
        try:
            current_snap = _state.fake_snapshot if _state.fake_mode else _state.snapshot
            if current_snap and current_snap.get("shm_ok"):
                memory.log_system_snapshot(_state.session_id, event_id, current_snap)
        except Exception as e:
            logger.debug(f"[event] system_snapshot log fail : {e}")
    turn = new_turn_id()
    now = time.time()
    ok = send_play_text({"text": message, "mood": "calm", "auto_event": event_type, "turn_id": turn,
                         "issued_at": now, "ttl_s": ttl_s, "deadline_ts": now + ttl_s})
    if not ok:
        _state.metrics["dropped_zmq"] += 1
        logger.warning(f"[event] DROPPED {event_type} (push_play not available or queue full)")


def detect_auto_events(snap: dict):
    """Hysteresis-based auto events. Each event has explicit entry/exit thresholds + confirm delay + dedup key.

    Anti-spam contract (GPT-5.4 reco P1) :
    - tyre_cliff       : enter >70% wear, exit <65%, confirm 2s, dedup by 5% band
    - fuel_critical    : enter <3 laps, exit >3.5 laps, confirm 1s, dedup per session_lap
    - yellow_flag      : enter True, exit False (single state), 1s confirm (anti-flicker)
    - lap_completed/PB : edge-triggered on lap count delta (not condition-based)
    """
    if not snap.get("shm_ok"): return
    if snap.get("status") != "LIVE":  # GPT-5.4 reco : exclude REPLAY (was 'LIVE' OR 'REPLAY' before)
        return
    # Session/pit transition reset (3 IAs consensus 17/05) : avant tout check, on libère
    # les latches one-shot si on a changé de session ou sorti du pit. Sinon warnings muet
    # toute la séquence Practice → Race.
    reset_latches_on_session_transition(snap)
    # gate : must be in session with real signals
    if snap.get("speed_kmh", 0) < 1 and snap.get("rpm", 0) < 100: return

    # V2.3 session-aware filtering — events fire only when relevant for session type
    session_type = (snap.get("session") or "").upper()
    is_race = session_type == "RACE"
    is_quali = session_type == "QUALIFY"
    is_practice = session_type == "PRACTICE"
    is_hotlap = session_type in ("HOTLAP", "HOTSTINT")

    # --- Lap completed / personal_best : EDGE-TRIGGERED on lap count delta ---
    # PB fired in all chrono-relevant sessions. lap_completed verbose only in Practice/Hotlap.
    laps = snap.get("completed_laps", 0)
    if laps > _state.last_lap_count and laps > 0:
        last_time = snap.get("last_time_ms", 0)
        best = snap.get("best_time_ms", 0)
        valid_lap = snap.get("valid_lap", True)
        # V3.B : log lap to history (every lap, valid or not — we filter at query time)
        if _state.session_id and laps > _state.last_logged_lap:
            try:
                s1, s2, s3 = (_state.current_lap_sectors + [0, 0, 0])[:3]
                tyre_press_avg = round(sum(snap.get(f"tyre_press_{p}", 0) for p in ("fl","fr","rl","rr")) / 4, 2) if snap.get("tyre_press_fl", 0) > 0 else 0
                tyre_temp_avg = round(sum(snap.get(f"tyre_temp_{p}", 0) for p in ("fl","fr","rl","rr")) / 4, 1) if snap.get("tyre_temp_fl", 0) > 0 else 0
                tyre_wear_max = max(snap.get(f"tyre_wear_{p}", 0) for p in ("fl","fr","rl","rr")) if snap.get("tyre_wear_fl", 0) > 0 else 0
                memory.log_lap_metric(
                    session_id=_state.session_id,
                    lap_num=laps,
                    lap_time_ms=last_time,
                    s1_ms=s1, s2_ms=s2, s3_ms=s3,
                    position=snap.get("position", 0),
                    gap_ahead_ms=snap.get("gap_ahead_ms", 0),
                    gap_behind_ms=snap.get("gap_behind_ms", 0),
                    fuel_l=snap.get("fuel_l", 0.0),
                    fuel_per_lap=snap.get("fuel_per_lap", 0.0),
                    tyre_press_avg=tyre_press_avg,
                    tyre_temp_avg=tyre_temp_avg,
                    tyre_wear_max=tyre_wear_max,
                    valid_lap=bool(valid_lap),
                    track=snap.get("track", ""),
                    car=snap.get("car", ""),
                )
                # Bug 2 fix : back-fill session meta avec track/car détectés via SHM
                memory.update_session_meta(_state.session_id,
                                           track=snap.get("track", ""),
                                           car=snap.get("car", ""),
                                           session_type=session_type)
                _state.last_logged_lap = laps
                _state.current_lap_sectors = []  # reset for next lap
            except Exception as e:
                logger.warning(f"[lap_history] log fail : {e}")
        if last_time > 0 and last_time < 600000 and valid_lap:
            # PB always relevant (PRACTICE/QUALI/HOTLAP/RACE)
            if best > 0 and last_time <= best and can_fire(f"personal_best:{laps}", 5):
                # PB : ton plus court en race, complet en practice/quali (chrono focus).
                if is_race:
                    msg_pb = f"Personal best, {format_lap_time_fr(last_time)}. Garde le rythme."
                else:
                    msg_pb = f"Personal best, {format_lap_time_fr(last_time)}, beau tour."
                # TTL généreux pour PB : message peut attendre derrière un PTT en cours (~10-15s synthèse)
                fire_event("personal_best", "info", msg_pb, {"last_ms": last_time, "best_ms": best, "lap": laps, "session": session_type}, ttl_s=20)
            # lap_completed verbose only in Practice / Hotlap (chrono focus). In Race = brief tactical only.
            elif (is_practice or is_hotlap) and can_fire(f"lap_completed:{laps}", 5):
                # Delta vs best speakable (cinq centièmes / un dixième / une demi-seconde / etc)
                if best > 0 and last_time > best:
                    delta_s = (last_time - best) / 1000.0
                    msg = f"Tour {laps}, {format_lap_time_fr(last_time)}, plus {fmt_delta_fr(delta_s)}."
                else:
                    msg = f"Tour {laps}, {format_lap_time_fr(last_time)}."
                fire_event("lap_completed", "info", msg, {"lap": laps, "time_ms": last_time, "session": session_type}, ttl_s=12)
        _state.last_lap_count = laps

    # --- Fuel low : RACE only (Practice/Quali assez fuel généralement) ---
    fuel_laps = snap.get("fuel_estimated_laps", 0)
    if is_race and fuel_laps > 0:
        # Fuel anticipation (consensus 3 IAs 16/05) : warning à 5 laps (anticipatif), critique à 2 (urgent).
        # Hysteresis distincts → 2 events séparés, dedup par band pour ne pas re-firer même zone.
        # Anticipation : 5 laps restants
        if hysteresis_check("fuel_warning",
                             condition=(fuel_laps < 5 and fuel_laps >= 3),
                             exit_condition=(fuel_laps > 5.5 or fuel_laps < 2.5),
                             confirm_delay_s=1.5,
                             dedup_key="band=5-3",
                             cooldown_s=120):
            fire_event("fuel_warning", "info", f"Plus que {fmt_fuel_laps_fr(fuel_laps)} d'essence, prépare le pit.",
                       {"laps_left": fuel_laps, "session": session_type}, ttl_s=15)
        # Critique : <2 laps = box maintenant
        band = "2-1" if fuel_laps >= 1 else "<1"
        if hysteresis_check("fuel_critical",
                             condition=(fuel_laps < 2),
                             exit_condition=(fuel_laps > 2.5),
                             confirm_delay_s=1.0,
                             dedup_key=f"band={band}",
                             cooldown_s=60):
            fire_event("fuel_critical", "warn", f"Carburant critique, {fmt_fuel_laps_fr(fuel_laps)} restant. Box maintenant.",
                       {"laps_left": fuel_laps, "band": band, "session": session_type}, ttl_s=25)

    # --- Yellow flag : critical for ALL session types (PB invalidation in quali, safety in race) ---
    # Quali tone = "Yellow S2, lift." (terse). Race tone = "Yellow sector 2, lift."
    yellow = bool(snap.get("global_yellow"))
    if hysteresis_check("yellow_flag",
                         condition=yellow,
                         exit_condition=(not yellow),
                         confirm_delay_s=1.0,
                         dedup_key="",
                         cooldown_s=20):
        # Détection secteur (V3.AA) : si un sub-yellow est actif, précise
        ys = [snap.get("global_yellow_s1"), snap.get("global_yellow_s2"), snap.get("global_yellow_s3")]
        sector_label = None
        try:
            active = [i+1 for i, v in enumerate(ys) if v]
            if len(active) == 1:
                sector_label = f"secteur {active[0]}"
        except Exception:
            sector_label = None
        if is_quali:
            msg = f"Yellow {sector_label}, lift." if sector_label else "Yellow, lift."
        else:
            msg = f"Drapeau jaune {sector_label}, lève." if sector_label else "Drapeau jaune, lève."
        fire_event("yellow_flag", "warn", msg, {"session": session_type, "sector": sector_label}, ttl_s=15)

    # --- V3.E Track limits warning : count via penalty/track_status (ACC SHM field 'penalty') ---
    # ACC penalty values : 0=none, 1=DriveThrough, 2=StopAndGo10, 3=StopAndGo20, 4=StopAndGo30, 5=Disqualified, etc.
    penalty = snap.get("penalty", 0)
    if penalty and penalty > 0:
        # First detection of any penalty
        if hysteresis_check("track_limits_warning",
                             condition=True,
                             exit_condition=(penalty == 0),
                             confirm_delay_s=0.5,
                             dedup_key=f"penalty_code={penalty}",
                             cooldown_s=30):
            penalty_label = {1: "drive-through", 2: "stop-and-go 10 secondes", 3: "stop-and-go 20 secondes", 4: "stop-and-go 30 secondes", 5: "disqualifié"}.get(penalty, f"pénalité {penalty}")
            msg = f"Pénalité {penalty_label}. Nettoie tes track limits." if is_race else f"Limites de piste, {penalty_label}."
            fire_event("track_limits_warning", "warn", msg, {"penalty_code": penalty, "label": penalty_label, "session": session_type}, ttl_s=15)

    # --- V3.E Blue flag : car ahead pace much faster (closing > 1s/lap) — RACE only, lapped traffic context ---
    # Heuristique : si gap_behind d'un faster car < 1.5s ET driver est sur push lap → blue flag
    # Pour V3.E v1, on utilise simple : si gap_ahead très petit et speed_kmh élevée = situation "fast car ahead in flying lap" ou "lapped car about to be passed"
    # Plus avancé en V3.J avec driver_history detection.
    # V1 minimal : alert si dans quali, gap_ahead < 0.5s = "traffic on push lap, careful"
    if is_quali:
        gap_ahead_ms = snap.get("gap_ahead_ms", 0)
        if 0 < gap_ahead_ms < 500:  # < 0.5s
            if hysteresis_check("traffic_on_push_lap",
                                 condition=True,
                                 exit_condition=(gap_ahead_ms == 0 or gap_ahead_ms > 1500),
                                 confirm_delay_s=0.3,
                                 dedup_key=f"close",
                                 cooldown_s=15):
                fire_event("traffic_on_push_lap", "warn", "Traffic devant, gère.", {"gap_ms": gap_ahead_ms, "session": "QUALIFY"}, ttl_s=4)

    # --- V3.E Pit window open : RACE only, when fuel_estimated_laps ~ pit_loss_in_laps ---
    if is_race:
        fuel_laps = snap.get("fuel_estimated_laps", 0)
        # Pit window = fuel just enough to reach ideal pit lap (5-10 laps before fuel critical)
        # Simple : if fuel_laps in [5, 10] window → open opportunity
        if 5 < fuel_laps < 10:
            if hysteresis_check("pit_window_open",
                                 condition=True,
                                 exit_condition=(fuel_laps < 4 or fuel_laps > 11),
                                 confirm_delay_s=2.0,
                                 dedup_key=f"band={int(fuel_laps)}",
                                 cooldown_s=180):
                fire_event("pit_window_open", "info", f"Fenêtre pit ouverte, encore {fmt_fuel_laps_fr(fuel_laps)} d'essence.", {"fuel_laps": fuel_laps, "session": "RACE"}, ttl_s=8)

    # --- Tyre cliff : RACE primary (Practice = mention only if max>80%, Quali rarely cliff in 2-3 laps) ---
    wears = [snap.get("tyre_wear_fl", 0), snap.get("tyre_wear_fr", 0), snap.get("tyre_wear_rl", 0), snap.get("tyre_wear_rr", 0)]
    max_wear = max(wears) if wears else 0
    band = f"{int(max_wear * 20)}/20"
    # Cliff threshold session-aware : 0.70 race, 0.80 practice (less alarming), skip quali
    cliff_threshold = 0.70 if is_race else (0.85 if is_practice else None)
    if cliff_threshold is not None and hysteresis_check("tyre_cliff",
                         condition=(max_wear > cliff_threshold),
                         exit_condition=(max_wear < cliff_threshold - 0.05),
                         confirm_delay_s=2.0,
                         dedup_key=f"band={band}",
                         cooldown_s=90):
        wear_pct = int(max_wear * 100)
        if is_race:
            msg = f"Pneus à {wear_pct} pourcent, fin de vie. Gestion impérative."
        else:
            msg = f"Pneus à {wear_pct} pourcent, attention au cliff."
        fire_event("tyre_cliff", "warn", msg, {"wear": wears, "max_wear": round(max_wear, 3), "band": band, "session": session_type}, ttl_s=20)

    # --- Phase D-light brief V3 17/05 : 3 observers strategist (RACE only) ---
    # Architecture : pas de service séparé (trop ambitieux pour un soir). On utilise hysteresis_check
    # + détection delta lap. Mutex LLM global (_llm_global_lock) garantit pas de race avec PTT.
    if is_race:
        # OBSERVER 1 — pace_drift : si avg 3 derniers laps > best + 2%, c'est un drift.
        if _state.session_id and laps >= 4:
            try:
                recent = memory.get_recent_laps(_state.session_id, n=5)
                valid_recent = [l for l in recent if l.get("valid_lap") and l.get("lap_time_ms", 0) > 0]
                if len(valid_recent) >= 3:
                    times = [l["lap_time_ms"] for l in valid_recent[-3:]]
                    avg_recent = sum(times) / len(times)
                    best_session = snap.get("best_time_ms", 0)
                    if best_session > 0:
                        drift_pct = (avg_recent - best_session) / best_session
                        band = f"drift_{int(drift_pct*100)}_lap{laps}"
                        if hysteresis_check("pace_drift",
                                             condition=(drift_pct > 0.02),
                                             exit_condition=(drift_pct < 0.012),
                                             confirm_delay_s=0,
                                             dedup_key=band, cooldown_s=120):
                            delta_s = (avg_recent - best_session) / 1000.0
                            fire_event("pace_drift", "info",
                                       f"Tu perds {fmt_delta_fr(delta_s)} vs ton meilleur sur les trois derniers tours, vérifie ton rythme.",
                                       {"avg_3_ms": int(avg_recent), "best_ms": best_session, "drift_pct": round(drift_pct, 3), "session": session_type}, ttl_s=12)
            except Exception as e:
                logger.debug(f"[observer.pace_drift] {e}")

        # OBSERVER 2 — undercut_window : car ahead à <2s ET pit_window_open (fuel_laps 5-10)
        # Si on a fuel pour pit et qu'on est proche du car ahead = bonne chance d'undercut.
        gap_a = snap.get("gap_ahead_ms", 0) or 0
        fuel_laps_remaining = snap.get("fuel_estimated_laps", 0) or 0
        if 0 < gap_a < 2000 and 5 < fuel_laps_remaining < 10:
            if hysteresis_check("undercut_window",
                                 condition=True,
                                 exit_condition=(gap_a > 3500 or fuel_laps_remaining < 4 or fuel_laps_remaining > 11),
                                 confirm_delay_s=2.0,
                                 dedup_key=f"gap_{int(gap_a/500)}_fuel_{int(fuel_laps_remaining)}",
                                 cooldown_s=180):
                fire_event("undercut_window", "info",
                           f"Fenêtre undercut ouverte : voiture devant à {gap_a/1000:.1f}s et tu as {int(fuel_laps_remaining)} tours d'essence. Pit dans 2-3 tours possible.",
                           {"gap_ahead_ms": gap_a, "fuel_laps": fuel_laps_remaining, "session": session_type}, ttl_s=15)

        # OBSERVER 3 — fuel_save_required : si fuel_laps < laps_restants_estimes course
        # Pas trivial sans laps_total race. Heuristique : si fuel_laps < 80% des laps restants estimés par session_time_left/avg_lap_time
        session_time_left_s = snap.get("session_time_left_s", 0) or 0
        last_time_ms = snap.get("last_time_ms", 0) or 0
        if session_time_left_s > 60 and last_time_ms > 30000 and fuel_laps_remaining > 0:
            laps_remaining_session = session_time_left_s / (last_time_ms / 1000.0)
            fuel_deficit = laps_remaining_session - fuel_laps_remaining
            if fuel_deficit > 0.5:
                save_needed_pct = round(fuel_deficit / max(laps_remaining_session, 1) * 100, 1)
                band = f"deficit_{int(fuel_deficit)}_lap{laps}"
                if hysteresis_check("fuel_save_required",
                                     condition=(fuel_deficit > 0.5),
                                     exit_condition=(fuel_deficit < 0.2),
                                     confirm_delay_s=3.0,
                                     dedup_key=band, cooldown_s=240):
                    fire_event("fuel_save_required", "warn",
                               f"Fuel save nécessaire, déficit estimé {fuel_deficit:.1f} tour. Lift et coast aux droites longues.",
                               {"fuel_laps": fuel_laps_remaining, "laps_remaining_estimated": round(laps_remaining_session, 1),
                                "deficit_laps": round(fuel_deficit, 2), "save_pct": save_needed_pct, "session": session_type}, ttl_s=18)

    # --- spotter_close auto-event RETIRÉ 17/05 ---
    # Consensus unanime Gemini 3.1 Pro + GPT-5.4 + GLM 5.1 (audit Bono V5 17/05) :
    # "anti-pattern absolu — LLM/TTS pipeline a 5s+ latence, ne peut PAS faire spotter à 10m".
    # Le tool `query_proximity_map` reste exposé (racing_tools.py l.1088) pour les questions
    # stratégiques hors action chaude (ex: "qui est derrière moi ?" en sortie de ligne droite).
    # Pour vrai spotter temps-réel, utiliser CrewChief natif (latence ~200ms via clips audio).

    # --- V4 Brake temperature warning (GLM reco) : freins trop chauds/froids GT3 typique 400-650°C ---
    try:
        bt = [snap.get(f"brake_temp_{p}", 0) for p in ("fl","fr","rl","rr")]
        bt_max = max(bt) if bt else 0
        bt_avg = sum(bt) / 4 if bt else 0
        now = time.time()
        # Fix anti-spam 17/05 nuit : cooldown 45s -> 180s + threshold 700 -> 720 (vrai surchauffe)
        # Session Zolder 17/05 18:00-18:24 : 26 brake_temp_high en 28 min (1 toutes les ~60s).
        # Spam confirmé. Freins restent chauds = pas besoin de répéter chaque tour.
        if bt_max > 720 and (now - _state.last_brake_temp_event_ts) > 180:
            fire_event("brake_temp_high", "warn", f"Freins chauds, {int(bt_max)} degrés, attention au fade.",
                       {"max_c": round(bt_max, 0), "avg_c": round(bt_avg, 0), "session": session_type}, ttl_s=15)
            _state.last_brake_temp_event_ts = now
        elif bt_avg < 200 and snap.get("completed_laps", 0) <= 1 and (now - _state.last_brake_temp_event_ts) > 60:
            fire_event("brake_temp_cold", "info", f"Freins encore froids, environ {int(bt_avg)} degrés. Build temperature.",
                       {"max_c": round(bt_max, 0), "avg_c": round(bt_avg, 0), "session": session_type}, ttl_s=10)
            _state.last_brake_temp_event_ts = now
    except Exception as e:
        logger.debug(f"[brake_temp] err: {e}")

    # --- V4 Understeer / Oversteer detection (GLM reco) : steering vs yaw_rate ---
    # Heuristic GT3 : at >80km/h, on a turn (|steer| > 0.1 rad), si yaw_rate / expected_yaw < 0.5 → understeer
    # Si yaw_rate / expected_yaw > 1.5 → oversteer
    try:
        v = snap.get("speed_kmh", 0)
        steer = snap.get("steer_angle_rad", 0)
        yaw = snap.get("yaw_rate_rad_s", 0)
        now = time.time()
        # V5 audit : U/O detect en PRACTICE/HOTLAP only (race = trop bavard, quali = ne sert pas)
        # Fix anti-spam 17/05 nuit : cooldown 30s -> 150s + dedup par lap.
        # Session Zolder 17/05 : 45 understeer_detected sur 28 min = 1.6/min. Spam massif.
        # Le pattern "BMW sous-vire chaque virage" = pas besoin de répéter. 1 fois par lap suffit.
        _current_lap = snap.get("completed_laps", 0)
        _last_uo_lap = getattr(_state, "_last_uo_lap", -1)
        if (v > 80 and abs(steer) > 0.15
            and (now - _state.last_uo_event_ts) > 150
            and _current_lap != _last_uo_lap
            and (is_practice or is_hotlap)):
            # wheelbase dynamique selon car_kb (Fix 0.4 brief V3 17/05 nuit)
            # Avant : hardcoded 2.85m (BMW M4). Porsche 992 = 2.46m, Mercedes AMG = 2.66m.
            # Erreur ±15% sur expected_yaw selon la voiture → understeer/oversteer mal calibré.
            try:
                from tools.acc_knowledge import get_wheelbase_m
                _wheelbase = get_wheelbase_m(snap.get("car", ""))
            except Exception:
                _wheelbase = 2.7
            import math
            expected_yaw = (v / 3.6) * math.tan(steer) / _wheelbase
            if abs(expected_yaw) > 0.05:
                ratio = yaw / expected_yaw if expected_yaw != 0 else 1.0
                if ratio < 0.4 and ratio > -0.5:  # understeer
                    fire_event("understeer_detected", "info",
                               "La voiture sous-vire, baisse l'angle ou ouvre plus tôt.",
                               {"ratio": round(ratio, 2), "speed_kmh": round(v, 0), "session": session_type}, ttl_s=8)
                    _state.last_uo_event_ts = now
                    _state._last_uo_lap = _current_lap  # dedup par lap (fix anti-spam 17/05 nuit)
                elif ratio > 1.7:  # oversteer
                    fire_event("oversteer_detected", "warn",
                               "La voiture sur-vire, ferme un peu en sortie.",
                               {"ratio": round(ratio, 2), "speed_kmh": round(v, 0), "session": session_type}, ttl_s=8)
                    _state.last_uo_event_ts = now
                    _state._last_uo_lap = _current_lap
    except Exception as e:
        logger.debug(f"[uo] err: {e}")

    # --- V4 Rain incoming (Gemini reco) : rain_intensity_in_10min / in_30min ---
    try:
        r10 = snap.get("rain_intensity_in_10min", 0)
        r30 = snap.get("rain_intensity_in_30min", 0)
        r_now = snap.get("rain_intensity", 0)
        # Edge-trigger : si r10 augmente vs last known
        if r10 > _state.last_known_rain_in_10 and r10 > 0 and r_now == 0:
            fire_event("rain_incoming_10min", "warn",
                       f"Pluie attendue dans 10 minutes, intensité {r10}. Prépare-toi.",
                       {"intensity_10min": r10, "intensity_30min": r30, "session": session_type}, ttl_s=20)
        elif r30 > _state.last_known_rain_in_30 and r30 > 0 and r10 == 0:
            fire_event("rain_incoming_30min", "info",
                       f"Pluie possible dans 30 minutes, intensité {r30}. Monitore.",
                       {"intensity_10min": r10, "intensity_30min": r30, "session": session_type}, ttl_s=20)
        _state.last_known_rain_in_10 = r10
        _state.last_known_rain_in_30 = r30
    except Exception as e:
        logger.debug(f"[rain] err: {e}")

    # --- V4 Fuel save strategy (GPT+GLM reco) : si race + fuel insuffisant calculé pour fin → lift and coast ---
    # MVP : si en race ET fuel_estimated_laps < (laps_remaining_race) → fuel save needed
    # Simple proxy : utilise fuel_per_lap + fuel_l vs laps_remaining (si dispo via SHM)
    try:
        if is_race and snap.get("fuel_estimated_laps", 0) > 0:
            # Fix 0.5 brief V3 17/05 nuit : avg_lap dynamique.
            # Avant : hardcoded 90s → erreur sur Monza 108s (-17%) ou Spa 130s (-31%).
            # Priorité : (1) moyenne 3 derniers laps valides session, (2) track_kb avg_lap_time_s,
            #            (3) fallback 90s.
            _avg_lap_s = 90.0
            try:
                if _state.session_id:
                    _recent = memory.get_recent_laps(_state.session_id, n=3)
                    _valid = [l for l in _recent if l.get("valid_lap") and l.get("lap_time_ms", 0) > 0]
                    if _valid:
                        _avg_lap_s = sum(l["lap_time_ms"] for l in _valid) / len(_valid) / 1000
                if _avg_lap_s == 90.0:
                    from tools.acc_knowledge import get_track_knowledge
                    _track_kb = get_track_knowledge(snap.get("track", "")) or {}
                    _kb_lap = _track_kb.get("avg_lap_time_s") or _track_kb.get("typical_lap_s")
                    if _kb_lap and _kb_lap > 30:
                        _avg_lap_s = float(_kb_lap)
            except Exception:
                _avg_lap_s = 90.0
            laps_left_race = (snap.get("session_time_left_ms", 0) or 0) / 1000 / max(_avg_lap_s, 30)
            fuel_laps_est = snap.get("fuel_estimated_laps", 0)
            now = time.time()
            if laps_left_race > 2 and fuel_laps_est > 0 and fuel_laps_est < laps_left_race - 0.5:
                deficit = laps_left_race - fuel_laps_est
                if deficit > 0.5 and (now - _state.last_fuel_save_event_ts) > 180:
                    fire_event("fuel_save", "warn",
                               f"Économise du carburant, déficit de {deficit:.1f} tour. Lift and coast T1 et Ascari.",
                               {"deficit_laps": round(deficit, 2), "fuel_laps_est": round(fuel_laps_est, 1), "session": session_type}, ttl_s=20)
                    _state.last_fuel_save_event_ts = now
    except Exception as e:
        logger.debug(f"[fuel_save] err: {e}")

    # --- V4 Opponent pit detection (GLM reco) : voiture qui disparaît du radius coords puis réapparaît ---
    # Heuristic : on track les car_ids vus avec leur dernière position. Si une voiture proche disparaît
    # (radius > 200m d'un coup ou plus dans la liste), on suppose qu'elle a pité.
    try:
        if is_race and _state.last_seen_opponents is not None:
            player_id = snap.get("player_car_id", -1)
            coords = snap.get("car_coordinates_raw", []) or []
            ids = snap.get("car_ids_raw", []) or []
            now = time.time()
            current_ids = set()
            player_pos = None
            for i, cid in enumerate(ids):
                if cid == player_id:
                    player_pos = coords[i]
                else:
                    current_ids.add(cid)
                    _state.last_seen_opponents[cid] = (now, coords[i])
            # Sweep : voitures vues il y a <30s qui ne sont plus dans current_ids → probable pit
            for cid, (last_ts, last_pos) in list(_state.last_seen_opponents.items()):
                if cid in current_ids: continue
                if now - last_ts > 30: continue  # déjà ancien, on lâche
                if now - last_ts < 8: continue  # V5 audit : 8s = confiance avant alerte (vs 3s spam)
                # Distance dernière vue à player → si on était proche (<200m), probable pit
                if player_pos:
                    px, _, pz = player_pos
                    lx, _, lz = last_pos
                    d = ((px - lx) ** 2 + (pz - lz) ** 2) ** 0.5
                    if d < 200 and can_fire(f"opponent_pit:{cid}:{int(now/30)}", 60):
                        fire_event("opponent_pit_detected", "info",
                                   f"Voiture {cid} possiblement aux stands, opportunité.",
                                   {"car_id": cid, "last_distance_m": round(d, 0), "session": session_type}, ttl_s=10)
                # Cleanup vieux
                if now - last_ts > 60:
                    _state.last_seen_opponents.pop(cid, None)
    except Exception as e:
        logger.debug(f"[opponent_pit] err: {e}")

    # ===== V5 BATCH 1+2+3 — "VRAI BONO" : session brief, out-lap, position, gaps, race countdown =====

    # --- V5 Session brief auto (au premier snap valide après détection track+car) ---
    if not _state.session_briefed and snap.get("track") and snap.get("car") and \
       snap.get("track") not in ("", "unknown") and snap.get("car") not in ("", "unknown"):
        try:
            track_name = snap.get("track", "")
            car_name = snap.get("car", "")
            fuel_l = snap.get("fuel_l", 0)
            session_str = session_type.lower() if session_type else "session"
            msg = f"Bono ready. {car_name} à {track_name}, session {session_str}. Carburant à {int(fuel_l)} litres. Quand tu es prêt, build temperature."
            fire_event("session_brief", "info", msg,
                       {"track": track_name, "car": car_name, "session": session_type, "fuel_l": fuel_l}, ttl_s=15)
            _state.session_briefed = True
        except Exception as e:
            logger.debug(f"[session_brief] err: {e}")

    # --- V5 Out-lap announcement (new lap from pit / start session) ---
    in_pit = bool(snap.get("is_in_pit_lane", False) or snap.get("in_pit", False))
    cur_lap = snap.get("completed_laps", 0)
    if _state.last_in_pit_lane and not in_pit and cur_lap != _state.out_lap_announced_lap:
        # Sortie du pit → out lap
        if (is_practice or is_hotlap or is_quali):
            fire_event("out_lap_started", "info",
                       "Out lap, tyres froids, build temperature progressif.",
                       {"lap": cur_lap, "session": session_type}, ttl_s=10)
            _state.out_lap_announced_lap = cur_lap
    _state.last_in_pit_lane = in_pit

    # --- V5 Warmup complete (3 laps + tyres in window 75-90°C) ---
    if not _state.warmup_complete_announced and cur_lap >= 3 and (is_practice or is_hotlap):
        tyres_temp = [snap.get(f"tyre_temp_{p}", 0) for p in ("fl","fr","rl","rr")]
        if tyres_temp and all(75 <= t <= 90 for t in tyres_temp):
            fire_event("warmup_complete", "info",
                       "Pneus à température, tu peux pousser maintenant.",
                       {"lap": cur_lap, "tyre_temp_avg": round(sum(tyres_temp)/4, 1), "session": session_type}, ttl_s=10)
            _state.warmup_complete_announced = True

    # --- V5b Pre-race grid check (1 min countdown avant départ) ---
    # Trigger : session=RACE, immobile, lap 0, pas pit, pas encore briefed pour cette race
    if is_race and not _state.pre_race_check_announced and snap.get("completed_laps", 0) == 0:
        v_now = snap.get("speed_kmh", 0)
        in_pit_now = bool(snap.get("is_in_pit_lane", False) or snap.get("in_pit", False))
        if v_now < 5 and not in_pit_now:
            try:
                # Read setup state + compute fuel needed
                from tools.racing_tools import query_setup_state as _qss, query_fuel_for_session_plan as _qfp
                ss = _qss()
                # Duration estimée : session_time_left_ms ou défaut 30 min
                time_left_s = (snap.get("session_time_left_ms", 0) or 0) / 1000.0
                duration_min = max(15, time_left_s / 60.0) if time_left_s > 0 else 30
                track = snap.get("track", "")
                fp = _qfp("race", duration_min, track) if track else None
                # Compose synthesis
                bits = []
                if ss.get("ok"):
                    s_data = ss.get("data", ss)
                    fuel_setup = s_data.get("fuel", "?")
                    tc1 = s_data.get("tC1", "?")
                    abs_ = s_data.get("abs", "?")
                    bb = s_data.get("brakeBias", "?")
                    bits.append(f"Carburant {fuel_setup} litres, TC {tc1}, ABS {abs_}, brake bias {bb}")
                if fp and fp.get("ok"):
                    fdata = fp.get("data", fp)
                    fuel_needed = fdata.get("fuel_recommended_l", 0)
                    laps_est = fdata.get("laps_with_in_out", 0)
                    bits.append(f"Pour {int(duration_min)} minutes il te faut {fuel_needed} litres, environ {int(laps_est)} tours")
                    # Compare avec setup
                    if ss.get("ok"):
                        fuel_setup_val = ss.get("data", ss).get("fuel", 0) or 0
                        if isinstance(fuel_setup_val, (int, float)) and fuel_needed > fuel_setup_val + 2:
                            bits.append(f"DÉFICIT {fuel_needed - fuel_setup_val} litres, ajoute avant départ")
                        elif isinstance(fuel_setup_val, (int, float)) and fuel_needed <= fuel_setup_val + 2:
                            bits.append("Carburant OK pour finir")
                msg = "Pre-race check. " + ". ".join(bits) + "." if bits else "Pre-race check, pas assez de data."
                fire_event("pre_race_grid_check", "info", msg,
                           {"duration_min": duration_min, "track": track, "session": session_type}, ttl_s=25)
                _state.pre_race_check_announced = True
            except Exception as e:
                logger.debug(f"[pre_race_check] err: {e}")

    # --- V5 Race intro (premier lap race) ---
    if is_race and not _state.race_intro_announced and cur_lap >= 1:
        pos = snap.get("position", 0)
        if pos > 0:
            fire_event("race_intro", "info",
                       f"Race is live, P {pos} start. Focus sur les premiers virages.",
                       {"position": pos, "session": session_type}, ttl_s=12)
            _state.race_intro_announced = True

    # --- V5 Position change (race only) ---
    if is_race and _state.last_position > 0:
        cur_pos = snap.get("position", 0)
        if cur_pos > 0 and cur_pos != _state.last_position:
            if cur_pos < _state.last_position:
                msg = f"P {cur_pos}, dépassement réussi, garde le rythme."
            else:
                msg = f"P {cur_pos}, position perdue, défends bien."
            if can_fire(f"position_change:{cur_pos}", 8):
                fire_event("position_change", "info", msg,
                           {"new_pos": cur_pos, "old_pos": _state.last_position, "session": session_type}, ttl_s=10)
    if snap.get("position", 0) > 0:
        _state.last_position = snap.get("position")

    # --- V5 Closing rate (race only) : voiture derrière qui se rapproche vite ---
    if is_race:
        gap_behind = snap.get("gap_behind_ms", 0) or 0
        now = time.time()
        if gap_behind > 0 and gap_behind < 5000 and _state.last_gap_behind_ts > 0:
            dt = now - _state.last_gap_behind_ts
            if dt > 5 and dt < 30:  # delta sain
                gap_delta = gap_behind - _state.last_gap_behind_ms
                # closing fast = >0.3s de réduction sur intervalle court
                if gap_delta < -300:  # voiture derrière s'est rapprochée de 0.3s+
                    if can_fire("closing_rate", 15):
                        fire_event("closing_rate", "warn",
                                   f"Voiture derrière se rapproche, défends.",
                                   {"gap_behind_ms": gap_behind, "delta_ms": gap_delta, "session": session_type}, ttl_s=10)
        if gap_behind > 0:
            _state.last_gap_behind_ms = gap_behind
            _state.last_gap_behind_ts = now

    # --- V5 Last lap & laps remaining (race) ---
    if is_race:
        try:
            # ACC SHM expose 'session_laps_total' ou similaire ; en attendant on utilise time_left
            time_left_ms = snap.get("session_time_left_ms", 0) or 0
            if time_left_ms > 0 and last_time > 0:
                laps_left_est = (time_left_ms / 1000.0) / (last_time / 1000.0)
                # Last lap
                if laps_left_est < 1.2 and not _state.last_lap_announced:
                    fire_event("last_lap", "warn",
                               "Last lap mate, bring it home clean.",
                               {"session": session_type}, ttl_s=15)
                    _state.last_lap_announced = True
                # Laps remaining 5 / 2 / 1
                for threshold in (5, 2, 1):
                    if threshold not in _state.laps_remaining_announced and abs(laps_left_est - threshold) < 0.5:
                        fire_event(f"laps_remaining_{threshold}", "info",
                                   f"{threshold} tours restants.",
                                   {"laps_left": round(laps_left_est, 1), "session": session_type}, ttl_s=10)
                        _state.laps_remaining_announced.add(threshold)
        except Exception as e:
            logger.debug(f"[last_lap] err: {e}")

    # --- V5 Chequered flag ---
    if snap.get("global_chequered", False) and not _state.chequered_announced:
        fire_event("chequered_flag", "info",
                   "Drapeau à damier, bon boulot mate.",
                   {"session": session_type}, ttl_s=15)
        _state.chequered_announced = True

    # --- V5 Damage warning ---
    # Fix anti-spam 17/05 nuit : cooldown 60s + threshold delta 5 -> 15 + dedup.
    # Session Zolder 17/05 : 7 damage_warning en 0.6s d'intervalle moyen = burst chronique
    # (1 contact = 5-8 events car damage progresse ticks SHM consécutifs).
    try:
        damage = (snap.get("car_damage_front", 0) + snap.get("car_damage_rear", 0) +
                  snap.get("car_damage_left", 0) + snap.get("car_damage_right", 0) +
                  snap.get("car_damage_center", 0))
        now = time.time()
        _last_damage_ts = getattr(_state, "_last_damage_event_ts", 0)
        # Delta threshold relevé à 15 (vrai contact > effleurement) + cooldown 60s anti-burst
        if (damage > _state.last_damage_total + 15
            and damage > 10
            and (now - _last_damage_ts) > 60):
            delta = damage - _state.last_damage_total
            fire_event("damage_warning", "warn",
                       f"Contact détecté, dommages augmentés. Vérifie ton ressenti voiture.",
                       {"damage_total": round(damage, 1), "delta": round(delta, 1), "session": session_type}, ttl_s=15)
            _state._last_damage_event_ts = now
        _state.last_damage_total = damage
    except Exception: pass

    # --- V5 Engine overheat ---
    try:
        water_temp = snap.get("water_temp", 0) or 0
        now = time.time()
        if water_temp > 105 and (now - _state.last_water_temp_event_ts) > 60:
            fire_event("engine_overheat", "warn",
                       f"Eau moteur à {int(water_temp)} degrés, surchauffe imminente. Lift and coast.",
                       {"water_temp": round(water_temp, 1), "session": session_type}, ttl_s=20)
            _state.last_water_temp_event_ts = now
    except Exception: pass

    # --- V4 Pace dictation (Gemini reco) : "Target 1:51.2, need half a tenth S2" ---
    # En quali/practice/hotlap, après 3 tours, on a un best. Bono dicte une cible.
    try:
        laps_now = snap.get("completed_laps", 0)
        best_ms = snap.get("best_time_ms", 0)
        if (is_practice or is_quali or is_hotlap) and laps_now >= 3 and best_ms > 0 and \
           laps_now != _state.last_pace_dictation_lap and can_fire(f"pace_dict:{laps_now}", 90):
            # Suggérer un target = best - 0.3s (push viable)
            target_ms = best_ms - 300
            fire_event("pace_dictation", "info",
                       f"Target ce tour, {format_lap_time_fr(target_ms)}. Trois dixièmes à prendre.",
                       {"target_ms": target_ms, "best_ms": best_ms, "lap": laps_now, "session": session_type}, ttl_s=10)
            _state.last_pace_dictation_lap = laps_now
    except Exception as e:
        logger.debug(f"[pace_dict] err: {e}")


# ============================================================
# Audio PTT loop : ZMQ PULL → Deepgram → Anthropic → Playback
# ============================================================
# Globals : pre-built clients (avoid per-PTT TLS handshake)
_dg_client = None
_whisper_model = None
_whisper_lock = threading.Lock()


def _get_dg_client(deepgram_key: str):
    """Lazy singleton DeepgramClient (reuses connection pool)."""
    global _dg_client
    if _dg_client is None and deepgram_key:
        from deepgram import DeepgramClient
        _dg_client = DeepgramClient(api_key=deepgram_key)
    return _dg_client


def _get_whisper_model():
    """Lazy load faster-whisper large-v3-turbo on GPU (RTX 5070 8GB)."""
    global _whisper_model
    if _whisper_model is None:
        with _whisper_lock:
            if _whisper_model is None:
                try:
                    from faster_whisper import WhisperModel
                    # GPU CUDA if available, fallback CPU. compute_type float16 for GPU 8GB.
                    try:
                        _whisper_model = WhisperModel("large-v3-turbo", device="cuda", compute_type="float16")
                        logger.info("[whisper] large-v3-turbo loaded on GPU (cuda/float16)")
                    except Exception as e_gpu:
                        logger.warning(f"[whisper] GPU load fail ({e_gpu}), fallback CPU int8")
                        _whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
                        logger.info("[whisper] base loaded on CPU (int8)")
                except Exception as e:
                    logger.error(f"[whisper] init fail completely : {e}")
                    return None
    return _whisper_model


def _stt_whisper_fallback(wav_bytes: bytes) -> str:
    """Faster-whisper local fallback. Used if Deepgram fails/timeouts."""
    model = _get_whisper_model()
    if model is None:
        return ""
    # Write wav_bytes to temp file (faster_whisper accepts path or numpy)
    import tempfile
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(wav_bytes)
            tmp_path = tmp.name
        t0 = time.time()
        segments, info = model.transcribe(tmp_path, language=None, beam_size=1, vad_filter=True)
        transcript = " ".join(seg.text.strip() for seg in segments).strip()
        dt = int((time.time() - t0) * 1000)
        logger.info(f"[whisper] transcribed '{transcript[:60]}...' ({dt}ms, lang={info.language})")
        return transcript
    except Exception as e:
        logger.error(f"[whisper] transcribe fail : {e}")
        return ""
    finally:
        try: os.unlink(tmp_path)
        except Exception: pass


def audio_loop(stop_event: threading.Event):
    logger.info("[audio] loop start")
    pull = make_pull(ZMQ_AUDIO_INPUT)
    push_play = make_push(ZMQ_PLAYBACK_REQ)
    push_play.setsockopt(zmq.SNDTIMEO, 1000)
    push_play.setsockopt(zmq.SNDHWM, 20)
    # V2.3 fix : expose globally for /say /ask /stop_speak endpoints (avoid duplicate PUSH bind/connect bug)
    register_push_play_global(push_play)
    time.sleep(0.3)
    secrets = get_secrets()
    deepgram_key = secrets["deepgram"]
    anthropic_key = secrets["anthropic"]
    if not deepgram_key:
        logger.error("[audio] No DEEPGRAM_API_KEY — STT disabled")
    else:
        # Warm Deepgram client (saves TLS handshake on first PTT)
        _get_dg_client(deepgram_key)
        logger.info("[audio] DeepgramClient pre-warmed")
    if not anthropic_key:
        logger.error("[audio] No ANTHROPIC_API_KEY — LLM disabled")
    anthropic_client = Anthropic(api_key=anthropic_key) if anthropic_key else None

    # Whisper warm in background (~2-5s GPU load)
    def _warm_whisper():
        try:
            _get_whisper_model()
        except Exception as e:
            logger.warning(f"[whisper] background warm fail : {e}")
    threading.Thread(target=_warm_whisper, daemon=True).start()

    while not stop_event.is_set():
        if not pull.poll(500): continue
        parts = pull.recv_multipart()
        if len(parts) < 2: continue
        topic = parts[0].decode()
        meta = orjson.loads(parts[1])
        if topic == "audio.ptt":
            wav_bytes = parts[2] if len(parts) >= 3 else b""
            handle_ptt(wav_bytes, meta, deepgram_key, anthropic_client, push_play)
        elif topic == "audio.ptt.stream_start":
            # V2.3 D4 — Deepgram Live WS consumer placeholder.
            # Live WS connection management requires async runtime here. For now we acknowledge
            # the stream_start marker but rely on the post-release WAV via 'audio.ptt' (line above).
            # Full Live WS implementation requires refactor : separate async loop in dedicated thread
            # that consumes audio.ptt.chunk and forwards to DG WS, then resolves final transcript
            # which can be matched by client_turn_seed with the WAV PTT. TODO V2.4.
            pass
        elif topic == "audio.ptt.chunk":
            pass  # buffered for future Live WS consumer
        elif topic == "audio.ptt.stream_end":
            pass  # would finalize Live WS transcript
    logger.info("[audio] loop stop")


def handle_ptt(wav_bytes: bytes, meta: dict, deepgram_key: str, anthropic_client: Anthropic, push_play):
    _state.metrics["ptt_count"] += 1
    t0 = time.time()
    # Generate new turn_id (cancels previous pending turns)
    turn_id = new_turn_id()
    logger.info(f"[ptt] turn#{turn_id} received {len(wav_bytes)}B (dur={meta.get('duration_s'):.2f}s rms={meta.get('rms'):.4f})")
    # Cost kill-switch (consensus 3 IA)
    ok, reason = can_afford_call(ANTHROPIC_MODELS["fast"])
    if not ok:
        _state.metrics["killswitch_triggered"] = True
        logger.warning(f"[ptt] killswitch : {reason}")
        try:
            _now = time.time()
            send_json(push_play, "play.text", {"text": f"Budget LLM journalier dépassé, je dois me taire.", "mood": "calm", "turn_id": turn_id,
                                                "issued_at": _now, "ttl_s": 12.0, "deadline_ts": _now + 12.0})
        except Exception: pass
        return
    # 1. STT — Deepgram primary, faster-whisper fallback
    transcript = ""
    stt_provider = "none"
    try:
        dg = _get_dg_client(deepgram_key)
        if dg is None:
            raise RuntimeError("no DeepgramClient available")
        # Fix 17/05 : SDK v3.11 utilise `listen.rest.v("1").transcribe_file(source, options)`, pas `listen.v1.media.*` (API v4+).
        # Le code original déclenchait `'ListenRouter' object has no attribute 'v1'` à chaque PTT
        # → 100% fallback Whisper (5-6s STT au lieu de <1s Deepgram).
        from deepgram import PrerecordedOptions
        source = {"buffer": wav_bytes}
        options = PrerecordedOptions(model=DEEPGRAM_MODEL, language=DEEPGRAM_LANGUAGE, smart_format=True, punctuate=True)
        resp = dg.listen.rest.v("1").transcribe_file(source, options)
        transcript = (resp.results.channels[0].alternatives[0].transcript or "").strip()
        stt_provider = "deepgram"
    except Exception as e:
        logger.warning(f"[ptt] Deepgram fail ({e}), fallback faster-whisper")
        _state.metrics["errors"] += 1
        # Fallback : faster-whisper local
        transcript = _stt_whisper_fallback(wav_bytes)
        stt_provider = "whisper_local" if transcript else "none"
        if not transcript:
            logger.error("[ptt] STT entirely failed (Deepgram + Whisper both)")
            return
    t_stt = (time.time() - t0) * 1000
    _state.metrics["avg_stt_ms"] = int(0.7 * _state.metrics["avg_stt_ms"] + 0.3 * t_stt)
    logger.info(f"[ptt] STT[{stt_provider}] '{transcript}' ({t_stt:.0f}ms)")
    # Fix F3 17/05 : audio_metrics table was 0-row. Branchée maintenant après STT pour suivre
    # qualité audio (RMS/peak/duration), corrélation avec quality du transcript et lang detected.
    if _state.session_id:
        try:
            memory.log_audio_metric(
                session_id=_state.session_id, turn_id=turn_id,
                duration_s=float(meta.get("duration_s", 0) or 0),
                rms=float(meta.get("rms", 0) or 0),
                peak=float(meta.get("peak", 0) or 0),
                sample_rate=int(meta.get("sample_rate", 16000) or 16000),
                bytes_=len(wav_bytes),
                transcript_len=len(transcript),
                lang_detected=meta.get("lang_detected"),
            )
        except Exception as e:
            logger.debug(f"[ptt] log_audio_metric fail : {e}")
    if not transcript or len(transcript) < 2:
        return
    # V2.3 latest-turn-wins (GPT-5.4 P1) : if a newer PTT arrived while we were STT'ing, abort here
    if not is_turn_valid(turn_id):
        logger.info(f"[ptt] turn#{turn_id} superseded post-STT, abort before LLM")
        return

    # 2. LLM with tool use
    if not anthropic_client:
        return
    # V2.3 fix : freeze snapshot via contextvar (propagates to ThreadPoolExecutor, unlike threading.local)
    snap = get_snapshot() or {}
    snap["_turn_id"] = turn_id
    snap["_turn_started_at"] = time.time()
    # Bug 3 fix complet : propager STT timing/provider pour log_pipeline_timing
    snap["_t_stt_ms"] = int(t_stt)
    snap["_stt_provider"] = stt_provider
    # Fix P0.1 brief V3 17/05 nuit : injecter session_id dans le snap pour que tools
    # (gap_trend_engine, strategy_projection, query_sector_performance, query_driver_memory)
    # puissent retrouver l'historique session courante sans repasser par _state global.
    snap["_session_id"] = _state.session_id
    snap["_session_signature"] = _state.last_session_signature
    token = set_turn_snapshot(snap)
    try:
        _handle_ptt_llm(transcript, turn_id, t0, push_play, anthropic_client, snap, tools_used=[])
    finally:
        clear_turn_snapshot(token)


def _handle_ptt_llm(transcript: str, turn_id: int, t0: float, push_play, anthropic_client, snap: dict, tools_used: list):
    """LLM+tool dispatch+playback section, isolated for clean turn_snapshot management.

    "Speak as we go" : when LLM streams text WITHOUT tool calls in this hop, we push completed
    sentences to TTS progressively (every "." or "?" or "!" with min 15 chars). For hops with
    tool_use, we still wait for final_msg (tools require complete response).
    """
    dynamic_ctx = build_dynamic_context_cached(snap)  # Phase A2 brief V3 : TTL 5s cache
    sys_prompt = build_system_prompt(dynamic_ctx, tools_schemas=get_all_schemas())
    is_french = any(c in transcript for c in "éèêàçâîôûùœ") or any(w in transcript.lower().split() for w in ("je","tu","mon","mes","ça","c'est","pneu","pneus","essence","carburant","tours","écart"))
    model_id = ANTHROPIC_MODELS["deep"] if any(w in transcript.lower() for w in ("explique","analyse","détail","compare","why","explain","debrief")) else ANTHROPIC_MODELS["fast"]
    temperature = LLM_TEMPERATURE_RACE if snap.get("speed_kmh", 0) > 30 else LLM_TEMPERATURE_PADDOCK

    # Add recent history (last 3 exchanges)
    history_msgs = []
    if _state.session_id:
        for ex in memory.get_recent_exchanges(_state.session_id, n=3):
            if ex["driver_msg"]:
                history_msgs.append({"role": "user", "content": ex["driver_msg"]})
            if ex["bono_msg"]:
                history_msgs.append({"role": "assistant", "content": ex["bono_msg"]})

    messages = history_msgs + [{"role": "user", "content": transcript}]
    # Note: tools_used is provided as arg (passed empty from handle_ptt) — appended in-place
    final_text = ""
    tokens_in_total = 0; tokens_out_total = 0
    # Prompt caching (consensus audit IA) : system + tools = ~2000 tokens cachables, économise 90% tokens-in
    # Mark system+tools with cache_control ephemeral, valid 5min sliding TTL
    system_blocks = [{"type": "text", "text": sys_prompt, "cache_control": {"type": "ephemeral"}}]
    cached_tools = get_all_schemas()
    if cached_tools:
        # Marquer le dernier tool avec cache_control = cache tous les tools précédents
        cached_tools = [dict(t) for t in cached_tools]
        cached_tools[-1] = {**cached_tools[-1], "cache_control": {"type": "ephemeral"}}
    # V2.3 D5 — Speak-as-we-go : push partial sentences to TTS as they stream from LLM
    # Only effective on the FINAL hop (no tool_use). For hops with tools we still wait for full response.
    # Default OFF in V2.3 : edge cases when tool_use arrives AFTER initial text streams = risk of
    # partial fabrication (LLM "starts" answer then decides to call tool). Activate after stability test.
    enable_speak_as_we_go = os.environ.get("BONO_SPEAK_AS_WE_GO", "0") == "1"
    sentence_endings = ".!?"
    MIN_PARTIAL_CHARS = 25  # don't push fragments below this
    pushed_partials_buffer = ""

    def _try_flush_sentence(accumulated: str) -> tuple[str, str | None]:
        """If accumulated text ends with a sentence boundary and is long enough, split off the ready sentence.
        Returns (remaining, sentence_to_push|None)."""
        if not accumulated or len(accumulated) < MIN_PARTIAL_CHARS:
            return accumulated, None
        # Find last sentence boundary in accumulated
        last_boundary = -1
        for i, ch in enumerate(accumulated):
            if ch in sentence_endings:
                last_boundary = i
        if last_boundary < 0:
            return accumulated, None
        ready = accumulated[:last_boundary + 1].strip()
        remaining = accumulated[last_boundary + 1:].lstrip()
        if len(ready) < MIN_PARTIAL_CHARS:
            return accumulated, None
        return remaining, ready

    # Fix 17/05 B6 : tracker du texte complet à travers tous les hops (chain tools).
    # Auparavant `final_text` ne contenait que le dernier hop, et avec SPEAK_AS_WE_GO la soustraction
    # pouvait vider la chaîne → bono_msg='' en DB. Maintenant on accumule TOUT le texte LLM
    # produit dans la conversation pour le sauver tel quel.
    _full_response_accumulated = ""
    try:
        for hop in range(3):
            # V2.3 latest-turn-wins (GPT-5.4 P1) : abort if newer PTT arrived since
            if not is_turn_valid(turn_id):
                logger.info(f"[ptt] turn#{turn_id} superseded at hop {hop}, abort LLM loop")
                return
            t_llm0 = time.time()
            # PHASE A2 (GPT-5.4 reco P3) : streaming + parallel tools
            # streaming response = TTFT ~400ms vs total 1500ms ; parallel tools = -300-500ms si 2+ tools
            tool_uses_local = []
            text_chunks = []
            in_tok = 0; out_tok = 0
            # Speak-as-we-go tracking (per-hop)
            accumulated_partial = ""
            already_pushed_partial = ""
            seen_tool_use_in_response = False
            # Fix 17/05 instrumentation : capture TTFT (Time-To-First-Token) — auparavant llm_ttft_ms=0 partout.
            _ttft_ms_hop = 0
            _first_delta_ts = None
            # Phase A1 brief V3 : mutex global LLM. Sérialise vs futur strategist proactif.
            # Notes : on tient le lock pendant tout le streaming (cohérent — 1 conversation à la fois).
            # Si strategist veut parler en parallèle, il attendra ici (acceptable : strategist = async).
            # Fix 0.2 17/05 nuit : flag SQL cross-process pour que strategist (autre process) skip.
            try: memory.set_llm_busy(True)
            except Exception: pass
            with _llm_global_lock, anthropic_client.messages.stream(
                model=model_id, max_tokens=LLM_MAX_TOKENS, temperature=temperature,
                system=system_blocks, tools=cached_tools, messages=messages,
                extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
            ) as stream:
                for event in stream:
                    # text deltas
                    if hasattr(event, "type"):
                        if event.type == "content_block_start":
                            # If this block is tool_use, mark and disable partial push (must wait final)
                            blk = getattr(event, "content_block", None)
                            if blk and getattr(blk, "type", None) == "tool_use":
                                seen_tool_use_in_response = True
                        elif event.type == "content_block_delta":
                            d = getattr(event.delta, "text", "") if hasattr(event, "delta") else ""
                            if d:
                                # TTFT instrumentation : premier delta texte = TTFT
                                if _first_delta_ts is None:
                                    _first_delta_ts = time.time()
                                    _ttft_ms_hop = int((_first_delta_ts - t_llm0) * 1000)
                                text_chunks.append(d)
                                # V2.3 D5 — speak as we go : push complete sentences early
                                if enable_speak_as_we_go and not seen_tool_use_in_response:
                                    accumulated_partial += d
                                    accumulated_partial, ready = _try_flush_sentence(accumulated_partial)
                                    if ready and is_turn_valid(turn_id):
                                        already_pushed_partial += ready + " "
                                        _now = time.time()
                                        try:
                                            send_json(push_play, "play.text", {"text": ready, "mood": "calm", "turn_id": turn_id, "issued_at": _now, "ttl_s": 8.0, "deadline_ts": _now + 8.0, "partial": True})
                                            logger.info(f"[ptt] turn#{turn_id} SPEAK-AS-WE-GO partial: '{ready[:60]}'")
                                        except Exception: pass
                        elif event.type == "message_delta":
                            # usage info arrives here in streaming
                            try:
                                if hasattr(event, "usage"):
                                    out_tok = getattr(event.usage, "output_tokens", 0)
                            except Exception: pass
                # final message has tool_use blocks + complete usage
                final_msg = stream.get_final_message()
                tool_uses_local = [b for b in final_msg.content if getattr(b, "type", None) == "tool_use"]
                # text already collected via deltas; ensure complete
                text_blocks = [b.text for b in final_msg.content if getattr(b, "type", None) == "text"]
                if not text_chunks and text_blocks:
                    text_chunks = ["".join(text_blocks)]
                in_tok = final_msg.usage.input_tokens
                out_tok = final_msg.usage.output_tokens
            t_llm = (time.time() - t_llm0) * 1000
            _state.metrics["llm_calls"] += 1
            _state.metrics["avg_llm_ms"] = int(0.7 * _state.metrics["avg_llm_ms"] + 0.3 * t_llm)
            tokens_in_total += in_tok; tokens_out_total += out_tok
            text_str_full = "".join(text_chunks).strip()  # Full LLM response (toujours conservé pour DB)
            # V2.3 D5 : if we pushed partials, remaining text is what wasn't yet pushed (pour TTS final)
            # Fix 17/05 (B6 audit) : SÉPARER text_str_full (DB) du text_str (TTS).
            # Avant ce fix : si TOUTE la phrase passait en partials, text_str devenait '' après soustraction
            # → bono_msg='' en DB (5/8 turns session 49 affectés). Bono parlait mais l'historique
            # ne savait pas ce qu'il avait dit → contexte multi-turn cassé.
            if enable_speak_as_we_go and already_pushed_partial:
                remaining_text = text_str_full
                if already_pushed_partial.strip() in text_str_full:
                    remaining_text = text_str_full.replace(already_pushed_partial.strip(), "", 1).strip()
                text_str = remaining_text  # what's left to send to TTS at end of turn
            else:
                text_str = text_str_full
            # Trace full text across tool hops (pour DB save B6 fix)
            if text_str_full:
                _full_response_accumulated = (_full_response_accumulated + " " + text_str_full).strip() if _full_response_accumulated else text_str_full
            if tool_uses_local:
                # Append assistant message with tool_use blocks
                messages.append({"role": "assistant", "content": final_msg.content})
                # PARALLEL tool dispatch — contextvars propagate auto (V2.3 fix vs threading.local)
                # We explicitly use copy_context() to ensure each worker sees the frozen snapshot.
                import concurrent.futures as _cf
                import contextvars as _cv
                tool_results_content: list = [None] * len(tool_uses_local)
                _ctx_for_workers = _cv.copy_context()  # captures turn_snapshot_var current value
                def _run_in_ctx(name, inp):
                    return _ctx_for_workers.run(dispatch, name, inp)
                with _cf.ThreadPoolExecutor(max_workers=min(4, len(tool_uses_local))) as ex:
                    fut_map = {}
                    for idx, tu in enumerate(tool_uses_local):
                        tools_used.append(tu.name)
                        fut = ex.submit(_run_in_ctx, tu.name, dict(tu.input))
                        fut_map[fut] = (idx, tu)
                    for fut in _cf.as_completed(fut_map, timeout=10):
                        idx, tu = fut_map[fut]
                        try:
                            result = fut.result()
                        except Exception as e:
                            result = {"ok": False, "error_code": "tool_exception", "human_message": str(e)}
                        tool_results_content[idx] = {"type": "tool_result", "tool_use_id": tu.id, "content": json.dumps(result)}
                        logger.info(f"[ptt] tool_use {tu.name}({tu.input}) -> {str(result)[:120]}")
                messages.append({"role": "user", "content": tool_results_content})
                continue  # next hop with tool_results
            else:
                final_text = text_str
                break
    except Exception as e:
        logger.error(f"[ptt] LLM fail : {e}")
        _state.metrics["errors"] += 1
        return
    finally:
        # Fix 0.2 17/05 nuit : libère le flag SQL cross-process meme si exception/return early
        try: memory.set_llm_busy(False)
        except Exception: pass
    total_ms = int((time.time() - t0) * 1000)
    # Cost tracking (consensus 3 IA — was missing)
    cost = compute_cost_usd(model_id, tokens_in_total, tokens_out_total)
    _state.metrics["total_cost_usd_session"] += cost
    memory.log_cost(model_id, tokens_in_total, tokens_out_total, cost)
    _state.last_exchange = {"transcript": transcript, "response": final_text, "tools_used": tools_used,
                            "model": model_id, "total_ms": total_ms, "ts": time.time(),
                            "cost_usd": round(cost, 5), "turn_id": turn_id}
    logger.info(f"[ptt] turn#{turn_id} response '{final_text[:80]}' tools={tools_used} total={total_ms}ms cost=${cost:.5f}")
    if _state.session_id:
        # Fix 17/05 B6 : utiliser le texte LLM COMPLET (accumulé sur tous les hops), pas final_text
        # qui peut être vide après soustraction SPEAK_AS_WE_GO. Fallback sur final_text si l'accumulateur
        # est vide (cas tools-only sans output texte).
        full_msg_for_db = _full_response_accumulated if _full_response_accumulated else final_text
        memory.log_exchange(_state.session_id, transcript, full_msg_for_db, model_id, tools_used, tokens_in_total, tokens_out_total, total_ms)
        # Bug 3 fix : log pipeline_timings per turn (stt/llm/tools/cost breakdown)
        try:
            stt_ms_val = int(snap.get("_t_stt_ms", 0)) if isinstance(snap, dict) else 0
            memory.log_pipeline_timing(
                session_id=_state.session_id, turn_id=turn_id,
                stt_ms=stt_ms_val, stt_provider=snap.get("_stt_provider", "deepgram") if isinstance(snap, dict) else "deepgram",
                # Fix 17/05 instrumentation : ttft du premier hop (avant tool calls) = vrai TTFT perçu
                llm_ttft_ms=_ttft_ms_hop if '_ttft_ms_hop' in locals() else 0,
                llm_total_ms=total_ms, llm_model=model_id,
                tts_ms=0, tts_provider="elevenlabs", playback_ms=0,
                total_e2e_ms=total_ms,
                tools_used=tools_used or [], tools_parallel=len(tools_used or []),
                tokens_in=tokens_in_total, tokens_out=tokens_out_total,
                cost_usd=cost, cache_hit=False,
            )
        except Exception as _e:
            logger.warning(f"[ptt] log_pipeline_timing fail (non-fatal): {_e}")

    # Check turn_id still valid before pushing to playback (GPT audit : cancellation)
    if not is_turn_valid(turn_id):
        logger.info(f"[ptt] turn#{turn_id} invalidated → skip playback")
        return

    # 3. Push to playback (with HWM/SNDTIMEO already on push_play)
    if final_text:
        try:
            # V2.3 cross-check 3 IA : TTL 12s (était 8s trop strict).
            # Pic LLM possible (Haiku peut spike 3s+), TTS varie, queue audio occupée — 8s coupe le playback à mi-phrase.
            # 12s = marge raisonnable. Bono dropé > 12s = vraiment obsolète.
            _now = time.time()
            send_json(push_play, "play.text", {"text": final_text, "mood": "calm", "turn_id": turn_id,
                                                "issued_at": _now, "ttl_s": 12.0, "deadline_ts": _now + 12.0})
            _state.metrics["tts_calls"] += 1
        except zmq.error.Again:
            _state.metrics["dropped_zmq"] += 1
            logger.warning(f"[ptt] turn#{turn_id} DROPPED (playback queue full)")
            try:
                # V2.3 GLM reco : reconnect socket if persistent block
                logger.warning("[ptt] zmq.Again persistent — consider playback service health check")
            except Exception: pass


# Phase A2 brief V3 (17/05) : cache dynamic_context avec TTL 5s.
# build_dynamic_context fait des queries SQL (driving_trace, recent_laps, driver_history) à chaque appel
# (3 queries × ~5ms = 15ms + format). Multiplié par turn LLM + auto-events qui rebuilent =
# coût SQL inutile + tokens recomputés à l'identique. TTL 5s : la télémétrie ACC bouge à 30Hz
# mais le LLM context bouge en pratique au lap suivant (~2 min). 5s = juste assez frais.
_dyn_ctx_cache: dict[str, tuple[float, str]] = {}  # key -> (ts, content)
_dyn_ctx_lock = threading.Lock()
_DYN_CTX_TTL_S = 5.0


def build_dynamic_context_cached(snap: dict) -> str:
    """Wrapper cached. Key = signature minimale du snap (lap, position, session)."""
    if not snap or not snap.get("shm_ok") or snap.get("status") == "OFF":
        # No cache for OFF state — short response inline
        return build_dynamic_context(snap)
    key = f"{snap.get('track','?')}|{snap.get('car','?')}|{snap.get('session','?')}|{snap.get('completed_laps',0)}|{snap.get('position',0)}|{int(snap.get('fuel_l',0))}"
    now = time.time()
    with _dyn_ctx_lock:
        cached = _dyn_ctx_cache.get(key)
        if cached and (now - cached[0]) < _DYN_CTX_TTL_S:
            return cached[1]
    # Cache miss : rebuild (hors lock pour ne pas bloquer)
    content = build_dynamic_context(snap)
    with _dyn_ctx_lock:
        _dyn_ctx_cache[key] = (now, content)
        # Garbage collect entries > 60s (évite leak mémoire si SHM change beaucoup)
        if len(_dyn_ctx_cache) > 50:
            stale = [k for k, (ts, _) in _dyn_ctx_cache.items() if now - ts > 60]
            for k in stale: _dyn_ctx_cache.pop(k, None)
    return content


def build_dynamic_context(snap: dict) -> str:
    if not snap.get("shm_ok") or snap.get("status") == "OFF":
        return "ACC SHM not live yet. No real telemetry available — be honest if asked."
    # Staleness budget (consensus 3 IAs piège #1) : si le snap a >2s, indiquer prudemment au LLM
    snap_age_s = None
    if snap.get("read_at"):
        snap_age_s = time.time() - snap["read_at"]
        if snap_age_s > 5:
            return f"WARNING: telemetry stale ({snap_age_s:.1f}s old). Data unreliable, ask driver to confirm before specific advice."
    # Fat context (3 IAs consensus 17/05) : pré-injecter tout ce que le LLM demanderait
    # via tool calls → 80% des tool calls disparaissent + élimine hallucination "1:10.5".
    # Temps en format compact `mm:ss.f` (string pré-formatée, jamais raw seconds).
    best_ms = snap.get("best_time_ms", 0) or 0
    last_ms = snap.get("last_time_ms", 0) or 0
    parts = [
        f"Track: {snap.get('track')}",
        f"Car: {snap.get('car')}",
        f"Session: {snap.get('session')} (status={snap.get('status')})",
        f"Lap: {snap.get('completed_laps')} / Position: {snap.get('position')}",
        f"Best lap: {fmt_lap_compact(best_ms) if best_ms > 0 else 'none yet'}",
        f"Last lap: {fmt_lap_compact(last_ms) if last_ms > 0 else 'none yet'}",
    ]
    if snap.get("speed_kmh", 0) > 5:
        parts.append(f"Live driving: speed={snap.get('speed_kmh'):.0f}km/h rpm={snap.get('rpm')} gear={snap.get('gear')}")
    if snap.get("fuel_estimated_laps", 0) > 0:
        fuel_per_lap = snap.get("fuel_per_lap", 0)
        fuel_part = f"Fuel: {snap.get('fuel_l'):.1f}L (~{snap.get('fuel_estimated_laps'):.1f} laps)"
        if fuel_per_lap and fuel_per_lap > 0:
            fuel_part += f" burn={fuel_per_lap:.2f}L/lap"
        parts.append(fuel_part)
    # Track conditions (track_grip, air_temp, road_temp, rain) — fat context
    cond_bits = []
    if snap.get("air_temp", 0) > 0:
        cond_bits.append(f"air={snap.get('air_temp'):.0f}°C")
    if snap.get("road_temp", 0) > 0:
        cond_bits.append(f"track={snap.get('road_temp'):.0f}°C")
    if snap.get("track_grip_status") is not None:
        cond_bits.append(f"grip={snap.get('track_grip_status')}")
    rain = snap.get("rain_intensity", 0) or 0
    if rain > 0.01:
        cond_bits.append(f"rain={rain:.2f}")
    if cond_bits:
        parts.append("Conditions: " + " ".join(cond_bits))
    # Tyres avg (le LLM demande souvent "tyre temps?")
    tyre_temps = [snap.get(f"tyre_temp_{p}", 0) for p in ("fl","fr","rl","rr")]
    if any(t > 0 for t in tyre_temps):
        parts.append(f"Tyres °C: FL{tyre_temps[0]:.0f} FR{tyre_temps[1]:.0f} RL{tyre_temps[2]:.0f} RR{tyre_temps[3]:.0f}")
    tyre_press = [snap.get(f"tyre_press_{p}", 0) for p in ("fl","fr","rl","rr")]
    if any(p > 0 for p in tyre_press):
        parts.append(f"Tyres psi: FL{tyre_press[0]:.1f} FR{tyre_press[1]:.1f} RL{tyre_press[2]:.1f} RR{tyre_press[3]:.1f}")
    # Gap ahead/behind (course context)
    ga = snap.get("gap_ahead_ms", 0) or 0
    gb = snap.get("gap_behind_ms", 0) or 0
    if ga or gb:
        parts.append(f"Gaps: ahead={ga/1000:+.2f}s behind={gb/1000:+.2f}s" if (ga and gb)
                     else (f"Gap ahead: {ga/1000:+.2f}s" if ga else f"Gap behind: {gb/1000:+.2f}s"))
    # Driving trace structuré (consensus 3 IAs 16/05 : "data déjà loggée mais ignorée")
    # On agrège les N derniers samples par secteur → top loss corners + brake anomalies.
    # Coûte ~1 req SQL + ~150 tokens. À ne PAS faire dans le hot path PTT si latence p95 > 8s.
    try:
        if _state.session_id:
            recent_trace = memory.get_driving_trace_recent(_state.session_id, n=15)
            if recent_trace:
                # Groupe par secteur
                by_s = {0: [], 1: [], 2: []}
                for t in recent_trace:
                    si = t.get("sector_index", 0)
                    if si in by_s:
                        by_s[si].append(t)
                summary_bits = []
                for si, ts in by_s.items():
                    if not ts: continue
                    avg_speed_min = sum(t.get("speed_min_kmh", 0) for t in ts) / len(ts)
                    max_slip = max(t.get("wheel_slip_max", 0) for t in ts)
                    max_brake = max(t.get("brake_max_pct", 0) for t in ts)
                    summary_bits.append(f"S{si+1}: speed_min_avg={avg_speed_min:.0f}km/h slip_max={max_slip:.2f} brake_max={max_brake:.0f}%")
                if summary_bits:
                    parts.append("Driving trace (last 15 samples par secteur):\n  " + "\n  ".join(summary_bits))
            # Recent lap times (pace trend)
            recent_laps = memory.get_recent_laps(_state.session_id, n=5)
            if recent_laps:
                # Format compact mm:ss.f (anti-hallucination "1:10.5" — Gemini 17/05).
                # JAMAIS raw seconds : "110.567s" → LLM dit "cent dix point cinq" au lieu de "1:50.6".
                lap_strs = [f"L{l['lap_num']} {fmt_lap_compact(l['lap_time_ms'])}" for l in recent_laps if l.get('lap_time_ms', 0) > 0]
                if lap_strs:
                    parts.append("Recent laps: " + ", ".join(lap_strs))
        # Cross-session history (driver_history_patterns) si on a track/car
        track = snap.get("track")
        car = snap.get("car")
        if track and car and track != "unknown" and car != "unknown":
            try:
                hist = memory.driver_history_patterns(track, car)
                if hist.get("has_history") and hist.get("n_laps_history", 0) >= 5:
                    # Format compact pour le LLM (jamais raw seconds dans le prompt).
                    parts.append(f"Driver history at {track}/{car}: best {fmt_lap_compact(hist['best_lap_ms'])} avg {fmt_lap_compact(hist['avg_lap_ms'])}, weakness {hist.get('weakness_sector','?')} (+{hist.get('weakness_avg_delta_ms',0)/1000:.2f}s avg). Last {hist['n_laps_history']} laps.")
            except Exception: pass
            # Fix P0.4 brief V3 17/05 nuit : injection KB car/track mini-bloc dans le system prompt.
            # Auparavant la KB n'etait accessible que via tool call (query_setup_context). Maintenant
            # un mini-bloc 4-6 lignes (strengths/weaknesses/character) est inclus pour que le LLM
            # reponde direct sans tool round-trip pour les questions evidentes.
            try:
                from tools.acc_knowledge import get_car_knowledge, get_track_knowledge
                car_kb = get_car_knowledge(car) or {}
                track_kb = get_track_knowledge(track) or {}
                kb_bits = []
                if car_kb:
                    bb = car_kb.get("bb_range_pct", [])
                    strengths = car_kb.get("strengths", "")
                    weaknesses = car_kb.get("weaknesses", "")
                    if isinstance(strengths, list): strengths = ", ".join(strengths[:3])
                    if isinstance(weaknesses, list): weaknesses = ", ".join(weaknesses[:3])
                    if bb or strengths or weaknesses:
                        kb_bits.append(f"Car {car}: BB {bb} | strengths={strengths[:80]} | weak={weaknesses[:80]}")
                if track_kb:
                    df = track_kb.get("downforce_pref", "")
                    brake_zones = track_kb.get("brake_events", "")
                    if isinstance(brake_zones, list): brake_zones = ", ".join(str(b)[:30] for b in brake_zones[:3])
                    if df or brake_zones:
                        kb_bits.append(f"Track {track}: DF={df} | brake_events={brake_zones[:100]}")
                if kb_bits:
                    parts.append("KB: " + " | ".join(kb_bits))
            except Exception as _kb_err:
                logger.debug(f"[ctx] kb inject fail: {_kb_err}")
    except Exception as e:
        logger.debug(f"[ctx] driving trace inject fail: {e}")
    return "\n".join(parts)


# ============================================================
# FastAPI dashboard + state introspection
# ============================================================
app = FastAPI(title="Bono v2 Dashboard")

# Bearer auth (audit GLM #9) : protège les endpoints de mutation (/ask /say /stop_speak)
BONO_TOKEN = os.environ.get("BONO_API_TOKEN", "").strip()
_auth_scheme = HTTPBearer(auto_error=False)


def require_token(creds: HTTPAuthorizationCredentials = Depends(_auth_scheme)):
    """If BONO_API_TOKEN is set in env, require Bearer match. Else allow (localhost dev)."""
    if not BONO_TOKEN:
        return  # auth disabled
    if not creds or creds.credentials != BONO_TOKEN:
        raise HTTPException(status_code=401, detail="invalid_token")


# V2.3 fix : global push_play socket for HTTP endpoints (/say /ask /stop_speak).
# Previously each endpoint created its own PUSH.connect(5557) — INVALID because audio_loop BINDs 5557.
# ZMQ PUSH cannot connect to another PUSH bound endpoint; messages silently routed nowhere.
# Solution : audio_loop registers its `push_play` socket globally; endpoints use it via lock.
_push_play_global = None
_push_play_lock = threading.Lock()


def register_push_play_global(sock):
    """Called by audio_loop after binding its push socket. Endpoints reuse it for /say /ask."""
    global _push_play_global
    _push_play_global = sock


def send_play_text(payload: dict) -> bool:
    """Thread-safe send via the audio_loop's bound PUSH socket. Returns True if queued."""
    if _push_play_global is None:
        logger.warning("[send_play_text] push_play not registered yet")
        return False
    with _push_play_lock:
        try:
            send_json(_push_play_global, "play.text", payload)
            return True
        except zmq.error.Again:
            logger.warning("[send_play_text] queue saturated")
            return False
        except Exception as e:
            logger.error(f"[send_play_text] err : {e}")
            return False


# V2.3 dev mode — Fake SHM injection for testing auto-events + tools in-game logic
@app.post("/dev/inject_shm")
async def dev_inject_shm(req: Request, _auth: None = Depends(require_token)):
    """DEV : inject a fake SHM snapshot to test auto-events + tools without real ACC.
    Body : { ...any snapshot fields you want to fake... }
    Common scenarios :
      - fuel_critical: {"shm_ok":true,"status":"LIVE","session":"RACE","speed_kmh":200,"rpm":7000,"fuel_l":3,"fuel_per_lap":1.5,"fuel_estimated_laps":2,"completed_laps":10}
      - yellow_flag : {"shm_ok":true,"status":"LIVE","speed_kmh":150,"rpm":6000,"global_yellow":true}
      - tyre_cliff  : {"shm_ok":true,"status":"LIVE","speed_kmh":200,"rpm":7000,"tyre_wear_fl":0.72,"tyre_wear_fr":0.71,"tyre_wear_rl":0.68,"tyre_wear_rr":0.69}
      - lap_completed/PB: bump completed_laps + set last_time_ms < best_time_ms
    """
    body = await req.json()
    _state.fake_mode = True
    _state.fake_snapshot = body
    logger.info(f"[dev] fake SHM injected : {list(body.keys())[:10]}")
    return {"status": "fake_injected", "fields": list(body.keys()), "fake_mode": True}


@app.post("/dev/clear_fake")
async def dev_clear_fake(_auth: None = Depends(require_token)):
    """Disable fake mode, return to real SHM."""
    _state.fake_mode = False
    _state.fake_snapshot = None
    logger.info("[dev] fake SHM cleared, back to real ACC SHM")
    return {"status": "cleared", "fake_mode": False}


@app.post("/dev/trigger_detect")
async def dev_trigger_detect(_auth: None = Depends(require_token)):
    """Manually invoke detect_auto_events() once with current snapshot (real or fake).
    Useful to fire test events synchronously without waiting for SHM 30Hz loop."""
    snap = get_snapshot() or {}
    before = _state.metrics["events_fired"]
    detect_auto_events(snap)
    after = _state.metrics["events_fired"]
    return {"events_fired_delta": after - before, "snap_speed_kmh": snap.get("speed_kmh"), "snap_status": snap.get("status")}


# V2.3 M3 — Perf endpoints (live ring buffer + per-session SQL)
_perf_ring: list[dict] = []
_perf_ring_lock = threading.Lock()
_PERF_RING_MAX = 600  # 10 min @ 1Hz


def perf_ring_push(metrics: dict):
    """Called from core_service or external monitor to append latest perf snapshot."""
    with _perf_ring_lock:
        _perf_ring.append({"ts": time.time(), **metrics})
        if len(_perf_ring) > _PERF_RING_MAX:
            del _perf_ring[:len(_perf_ring) - _PERF_RING_MAX]

DASH_HTML = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><title>Bono v2</title>
<style>
body{font-family:ui-monospace,monospace;background:#0a0a0a;color:#bbb;margin:1rem}
h1{color:#f60;margin:0 0 .5rem}
.row{display:flex;gap:1rem;flex-wrap:wrap;margin-bottom:.5rem}
.card{background:#111;border:1px solid #333;padding:.6rem;border-radius:6px;flex:1;min-width:240px}
.k{color:#777}.v{color:#0fa;font-weight:600}
pre{margin:0;font-size:.85rem;white-space:pre-wrap}
.live{color:#0f0}.off{color:#f00}.warn{color:#fc0}
</style></head><body>
<h1>Bono v2 — Live</h1>
<div id="root">loading...</div>
<script>
async function tick(){
  try{const r=await fetch('/state');const s=await r.json();
  const ok=s.snapshot&&s.snapshot.shm_ok;
  const st=s.snapshot?.status||'?';
  const cls=st==='LIVE'?'live':(st==='OFF'?'off':'warn');
  document.getElementById('root').innerHTML=`
   <div class="row">
    <div class="card"><div class="k">SHM</div><div class="v ${cls}">${st}</div>
      <pre>track=${s.snapshot?.track||'?'} car=${s.snapshot?.car||'?'} session=${s.snapshot?.session||'?'}</pre></div>
    <div class="card"><div class="k">Drive</div>
      <pre>speed=${(s.snapshot?.speed_kmh||0).toFixed(0)} kmh
rpm=${s.snapshot?.rpm||0} gear=${s.snapshot?.gear??'-'}
fuel=${(s.snapshot?.fuel_l||0).toFixed(1)} L (~${(s.snapshot?.fuel_estimated_laps||0).toFixed(1)} laps)
lap=${s.snapshot?.completed_laps||0} pos=${s.snapshot?.position||0}</pre></div>
    <div class="card"><div class="k">Tyres (PSI / °C)</div>
      <pre>FL=${(s.snapshot?.tyre_press_fl||0).toFixed(1)}  ${(s.snapshot?.tyre_temp_fl||0).toFixed(0)}
FR=${(s.snapshot?.tyre_press_fr||0).toFixed(1)}  ${(s.snapshot?.tyre_temp_fr||0).toFixed(0)}
RL=${(s.snapshot?.tyre_press_rl||0).toFixed(1)}  ${(s.snapshot?.tyre_temp_rl||0).toFixed(0)}
RR=${(s.snapshot?.tyre_press_rr||0).toFixed(1)}  ${(s.snapshot?.tyre_temp_rr||0).toFixed(0)}</pre></div>
   </div>
   <div class="row">
    <div class="card"><div class="k">Last exchange</div>
      <pre>TU   : ${s.last_exchange?.transcript||'-'}
BONO : ${s.last_exchange?.response||'-'}
tools: ${(s.last_exchange?.tools_used||[]).join(', ')||'-'}
model: ${s.last_exchange?.model||'-'}
total: ${s.last_exchange?.total_ms||'-'}ms</pre></div>
    <div class="card"><div class="k">Metrics</div>
      <pre>${JSON.stringify(s.metrics,null,2)}</pre></div>
   </div>`;
  }catch(e){document.getElementById('root').innerHTML='Err: '+e}
}
tick();setInterval(tick,1000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return DASH_HTML


@app.get("/state")
def get_state():
    return {
        "uptime_s": round(time.time() - _state.started_at, 1),
        "session_id": _state.session_id,
        "snapshot": _state.snapshot,
        "last_exchange": _state.last_exchange,
        "metrics": _state.metrics,
        "memory_stats": memory.stats(),
    }


@app.get("/health")
def health():
    return {"ok": True, "uptime_s": round(time.time() - _state.started_at, 1)}


@app.get("/snapshot")
def snapshot():
    return _state.snapshot


# ============================================================
# Monitoring endpoints (for Claude Code agent + remote debug)
# ============================================================
@app.get("/logs")
def logs(lines: int = 50, service: str = "core"):
    """Tail recent logs from core/input/playback. Service in {core,input,playback}."""
    valid = {"core", "input", "playback"}
    if service not in valid:
        return JSONResponse({"error": "service must be one of " + ",".join(valid)}, 400)
    log_path = Path(__file__).parent / "logs" / f"{service}_service.log"
    if not log_path.exists():
        return {"lines": [], "path": str(log_path), "exists": False}
    try:
        with open(log_path, encoding="utf-8", errors="ignore") as f:
            all_lines = f.readlines()
        return {"service": service, "total_lines": len(all_lines), "returned": min(lines, len(all_lines)),
                "lines": [l.rstrip("\n") for l in all_lines[-lines:]]}
    except Exception as e:
        return JSONResponse({"error": str(e)}, 500)


@app.get("/events")
def events_recent(limit: int = 20, session_id: int | None = None):
    """Recent auto-events from SQLite (lap_completed, fuel_critical, etc.)"""
    conn = memory.get_conn()
    if session_id:
        rows = conn.execute("SELECT * FROM events WHERE session_id=? ORDER BY ts DESC LIMIT ?", (session_id, limit)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return {"count": len(rows), "events": [dict(r) for r in rows]}


@app.get("/exchanges")
def exchanges_recent(limit: int = 20, session_id: int | None = None):
    """Recent PTT exchanges (driver → bono) from SQLite."""
    conn = memory.get_conn()
    if session_id:
        rows = conn.execute("SELECT * FROM exchanges WHERE session_id=? ORDER BY ts DESC LIMIT ?", (session_id, limit)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM exchanges ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return {"count": len(rows), "exchanges": [dict(r) for r in rows]}


@app.get("/sessions")
def sessions_list(limit: int = 10):
    """Recent sessions metadata."""
    conn = memory.get_conn()
    rows = conn.execute("SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return {"count": len(rows), "sessions": [dict(r) for r in rows]}


@app.get("/costs")
def costs_summary(period_h: int = 24):
    """LLM cost summary over last N hours."""
    cutoff = time.time() - period_h * 3600
    conn = memory.get_conn()
    rows = conn.execute(
        "SELECT model, COUNT(*) AS n, SUM(tokens_in) AS tin, SUM(tokens_out) AS tout, SUM(cost_usd) AS cost FROM costs WHERE ts>=? GROUP BY model",
        (cutoff,)
    ).fetchall()
    total = conn.execute("SELECT SUM(cost_usd) AS t FROM costs WHERE ts>=?", (cutoff,)).fetchone()
    conn.close()
    return {"period_h": period_h, "total_usd": round(total["t"] or 0, 4), "by_model": [dict(r) for r in rows]}


@app.post("/ask")
async def ask_manual(req: Request, _auth: None = Depends(require_token)):
    """Inject a text question into the LLM pipeline (no STT, no audio). Useful for debug / Claude Code agent.
    Body : {"text": "...", "speak": bool}
    """
    body = await req.json()
    text = (body.get("text") or "").strip()
    speak = bool(body.get("speak", True))
    if not text:
        return JSONResponse({"error": "text required"}, 400)
    # Build LLM input directly
    secrets = get_secrets()
    if not secrets["anthropic"]:
        return JSONResponse({"error": "no anthropic key"}, 500)
    client = Anthropic(api_key=secrets["anthropic"])
    snap = get_snapshot() or {}
    sys_prompt = build_system_prompt(build_dynamic_context(snap), tools_schemas=get_all_schemas())
    system_blocks = [{"type": "text", "text": sys_prompt, "cache_control": {"type": "ephemeral"}}]
    cached_tools = get_all_schemas()
    if cached_tools:
        cached_tools = [dict(t) for t in cached_tools]
        cached_tools[-1] = {**cached_tools[-1], "cache_control": {"type": "ephemeral"}}
    messages = [{"role": "user", "content": text}]
    tools_used: list = []
    final_text = ""
    tokens_in = tokens_out = 0
    t0 = time.time()
    try:
        for _ in range(3):
            resp = client.messages.create(
                model=ANTHROPIC_MODELS["fast"], max_tokens=LLM_MAX_TOKENS, temperature=0.7,
                system=system_blocks, tools=cached_tools, messages=messages,
                extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
            )
            tokens_in += resp.usage.input_tokens; tokens_out += resp.usage.output_tokens
            tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
            text_blocks = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
            if tool_uses:
                messages.append({"role": "assistant", "content": resp.content})
                trc = []
                for tu in tool_uses:
                    tools_used.append(tu.name)
                    result = dispatch(tu.name, dict(tu.input))
                    trc.append({"type": "tool_result", "tool_use_id": tu.id, "content": json.dumps(result)})
                messages.append({"role": "user", "content": trc})
                continue
            final_text = "".join(text_blocks).strip()
            break
    except Exception as e:
        return JSONResponse({"error": str(e)}, 500)
    total_ms = int((time.time() - t0) * 1000)
    if speak and final_text:
        # V2.3 fix : use global push_play (audio_loop's bound socket) instead of new PUSH.connect
        _now = time.time()
        ok = send_play_text({"text": final_text, "mood": "calm", "issued_at": _now, "ttl_s": 12.0, "deadline_ts": _now + 12.0})
        if not ok:
            logger.warning("[/ask] play push failed")
    return {"question": text, "response": final_text, "tools_used": tools_used,
            "tokens_in": tokens_in, "tokens_out": tokens_out, "total_ms": total_ms,
            "spoken": speak and bool(final_text)}


@app.post("/say")
async def say(req: Request, _auth: None = Depends(require_token)):
    """Force Bono to speak arbitrary text (no LLM). Useful for TTS test.
    Body : {"text": "..."}
    """
    body = await req.json()
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "text required"}, 400)
    # V2.3 fix : use global push_play instead of fresh PUSH.connect (buggy)
    _now = time.time()
    ok = send_play_text({"text": text, "mood": "calm", "issued_at": _now, "ttl_s": 12.0, "deadline_ts": _now + 12.0})
    if ok:
        return {"status": "queued", "text": text}
    return JSONResponse({"error": "play_push_failed", "msg": "push socket not registered yet (core booting?) or queue full"}, 503)


@app.post("/stop_speak")
def stop_speak(_auth: None = Depends(require_token)):
    """Barge-in : invalidate current turn + send stop + flush queue (Gemini #1 audit)."""
    # Invalidate the current turn (any pending playback for old turns will be dropped)
    with _state_lock:
        invalidated = _state.current_turn_id
        _state.invalidated_turns.add(invalidated)
    try:
        ctx = zmq.Context.instance()
        sock = ctx.socket(zmq.PUSH)
        sock.setsockopt(zmq.SNDTIMEO, 500)
        sock.connect(ZMQ_PLAYBACK_REQ)
        send_json(sock, "play.stop", {"ts": time.time(), "invalidate_turn": invalidated})
        sock.close()
        return {"status": "stop_sent", "invalidated_turn": invalidated}
    except Exception as e:
        return JSONResponse({"error": str(e)}, 500)


@app.get("/tools")
def list_tools():
    """List all registered Anthropic tools + schemas."""
    return {"count": len(get_all_schemas()), "tools": get_all_schemas()}


# V2.3 M3 — Perf monitoring endpoints
@app.get("/perf/live")
def perf_live(last_n: int = 60):
    """Returns last N seconds of perf metrics from ring buffer."""
    with _perf_ring_lock:
        data = list(_perf_ring[-last_n:])
    if not data:
        return {"count": 0, "data": [], "msg": "no_metrics_pushed_yet"}
    # Aggregate quick stats
    keys_num = ["cpu_pct", "ram_pct", "gpu_util_pct", "gpu_temp_c", "vram_used_mb"]
    agg = {}
    for k in keys_num:
        vals = [d.get(k) for d in data if isinstance(d.get(k), (int, float))]
        if vals:
            agg[k] = {"min": min(vals), "max": max(vals), "avg": round(sum(vals)/len(vals), 2)}
    return {"count": len(data), "agg": agg, "data": data[-30:]}  # last 30 raw points


@app.get("/perf/session/{session_id}")
def perf_session(session_id: int):
    """Returns aggregated pipeline timings + audio metrics for a session."""
    with memory._lock:
        c = memory.get_conn()
        try:
            sess = c.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not sess:
                return JSONResponse({"error": "session_not_found"}, 404)
            timings = c.execute("SELECT * FROM pipeline_timings WHERE session_id=? ORDER BY ts", (session_id,)).fetchall()
            audio = c.execute("SELECT * FROM audio_metrics WHERE session_id=? ORDER BY ts", (session_id,)).fetchall()
            exch_count = c.execute("SELECT COUNT(*) AS n FROM exchanges WHERE session_id=?", (session_id,)).fetchone()["n"]
            evt_count = c.execute("SELECT COUNT(*) AS n FROM events WHERE session_id=?", (session_id,)).fetchone()["n"]
            total_cost = c.execute("SELECT COALESCE(SUM(cost_usd),0) AS t FROM costs WHERE ts BETWEEN ? AND ?", (sess["started_at"], sess["ended_at"] or time.time())).fetchone()["t"]
        finally:
            c.close()
    # Build aggregates
    if timings:
        e2e_vals = [t["total_e2e_ms"] for t in timings if t["total_e2e_ms"]]
        stt_vals = [t["stt_ms"] for t in timings if t["stt_ms"]]
        llm_vals = [t["llm_total_ms"] for t in timings if t["llm_total_ms"]]
        tts_vals = [t["tts_ms"] for t in timings if t["tts_ms"]]
        def _agg(vs):
            if not vs: return {}
            s = sorted(vs)
            return {"n": len(vs), "min": s[0], "max": s[-1], "avg": round(sum(vs)/len(vs), 0), "p50": s[len(s)//2], "p95": s[int(len(s)*0.95)] if len(s) > 1 else s[-1]}
        agg = {"e2e_ms": _agg(e2e_vals), "stt_ms": _agg(stt_vals), "llm_ms": _agg(llm_vals), "tts_ms": _agg(tts_vals)}
    else:
        agg = {}
    return {
        "session": dict(sess),
        "stats": {"exchanges": exch_count, "events": evt_count, "total_cost_usd": round(total_cost, 4), "pipeline_samples": len(timings), "audio_samples": len(audio)},
        "agg_latency": agg,
    }


@app.get("/perf/dashboard", response_class=HTMLResponse)
def perf_dashboard():
    """Standalone perf dashboard with live charts."""
    return """<!doctype html><html><head><meta charset='utf-8'><title>Bono Perf</title>
<style>body{font-family:ui-monospace;background:#0a0a0a;color:#bbb;padding:1rem}h1{color:#f60}
.card{background:#111;border:1px solid #333;padding:.6rem;border-radius:6px;margin:.5rem 0}
.k{color:#777}.v{color:#0fa;font-weight:600;font-size:1.5em}
canvas{background:#0a0a0a;border:1px solid #222}</style></head><body>
<h1>Bono v2 — Perf Live</h1>
<div id="agg" class="card">loading...</div>
<canvas id="chart" width="900" height="240"></canvas>
<script>
async function tick(){
  try{
    const r=await fetch('/perf/live?last_n=120');const d=await r.json();
    if(!d.count){document.getElementById('agg').innerHTML='No metrics yet — start bono_perf_monitor.py';return;}
    const a=d.agg;
    document.getElementById('agg').innerHTML=
      `<div class="k">CPU ${a.cpu_pct?.avg||'-'}% (max ${a.cpu_pct?.max||'-'}%)</div>`+
      `<div class="k">RAM ${a.ram_pct?.avg||'-'}% (max ${a.ram_pct?.max||'-'}%)</div>`+
      `<div class="k">GPU ${a.gpu_util_pct?.avg||'-'}% (temp ${a.gpu_temp_c?.max||'-'}C max)</div>`+
      `<div class="k">VRAM ${a.vram_used_mb?.avg||'-'} MB (max ${a.vram_used_mb?.max||'-'} MB)</div>`+
      `<div class="k">Samples: ${d.count}</div>`;
    // Draw chart
    const c=document.getElementById('chart').getContext('2d');
    c.fillStyle='#0a0a0a';c.fillRect(0,0,900,240);
    const pts=d.data;
    const drawLine=(key,color,scale=1)=>{
      c.strokeStyle=color;c.beginPath();
      pts.forEach((p,i)=>{
        const v=(p[key]||0)*scale;
        const x=i*30;const y=240-(v*2);
        if(i===0)c.moveTo(x,y);else c.lineTo(x,y);
      });c.stroke();
    };
    drawLine('cpu_pct','#f60');
    drawLine('gpu_util_pct','#0fa');
    drawLine('gpu_temp_c','#f00',1);
  }catch(e){console.error(e);}
}
tick();setInterval(tick,1000);
</script></body></html>"""


_ws_clients: set[WebSocket] = set()


@app.websocket("/ws/telemetry")
async def ws_telemetry(ws: WebSocket):
    """WebSocket push : snapshot + last_exchange every 10 Hz. Standard Bono v2 live feed."""
    await ws.accept()
    _ws_clients.add(ws)
    logger.info(f"[ws] client connected ({len(_ws_clients)} total)")
    try:
        while True:
            payload = {"t": time.time(), "snapshot": _state.snapshot, "last_exchange": _state.last_exchange, "metrics": _state.metrics}
            await ws.send_json(payload)
            await asyncio.sleep(0.1)  # 10 Hz
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"[ws] err : {e}")
    finally:
        _ws_clients.discard(ws)
        logger.info(f"[ws] client disconnected ({len(_ws_clients)} left)")


@app.get("/processes")
def processes_status():
    """List Bono v2 child processes."""
    import psutil
    procs = []
    for p in psutil.process_iter(["pid", "name", "cmdline", "memory_info", "create_time", "status"]):
        try:
            cmd = " ".join(p.info["cmdline"] or [])
            if "bono_v2" in cmd:
                procs.append({
                    "pid": p.info["pid"],
                    "name": Path(p.info["cmdline"][-1]).name if p.info["cmdline"] else "?",
                    "ram_mb": int(p.info["memory_info"].rss / 1024 / 1024),
                    "uptime_s": int(time.time() - p.info["create_time"]),
                    "status": p.info["status"],
                })
        except Exception: pass
    return {"count": len(procs), "processes": procs}


# ============================================================
# Main
# ============================================================
def play_status_listener(stop_event: threading.Event):
    """Fix F7 17/05 : écoute les events play.status du playback_service via ZMQ SUB.
    Quand un event state=done arrive avec tts_ms + playback_ms, met à jour la ligne
    correspondante de pipeline_timings (avant : tts_ms=0, playback_ms=NULL hardcodés)."""
    try:
        from zmq_bus import make_sub, recv_json
        sub = make_sub(ZMQ_EVENTS, topic="play.status")
        logger.info(f"[play_status] subscribed to {ZMQ_EVENTS}")
        while not stop_event.is_set():
            msg = recv_json(sub, timeout_ms=1000)
            if not msg:
                continue
            try:
                topic, payload = msg
                if not isinstance(payload, dict): continue
                state = payload.get("state")
                turn_id = payload.get("turn_id")
                if state == "done" and turn_id is not None and _state.session_id:
                    tts_ms = int(payload.get("tts_ms", 0) or 0)
                    playback_ms = int(payload.get("playback_ms", 0) or 0)
                    provider = payload.get("provider", "")
                    if tts_ms or playback_ms:
                        try:
                            memory.update_pipeline_timing_tts(_state.session_id, turn_id, tts_ms, playback_ms, provider)
                        except Exception as e:
                            logger.debug(f"[play_status] pipeline update fail : {e}")
            except Exception as e:
                logger.debug(f"[play_status] handle fail : {e}")
    except Exception as e:
        logger.warning(f"[play_status] listener crashed : {e}")


def main():
    logger.add("logs/core_service.log", rotation="10 MB", retention=3)
    logger.info("=== Bono v2 core_service start ===")
    secrets = get_secrets()
    logger.info(f"Secrets : deepgram={'OK' if secrets['deepgram'] else 'MISS'} anthropic={'OK' if secrets['anthropic'] else 'MISS'} fish={'OK' if secrets['fish_key'] else 'MISS'}")

    # Start session in DB — Bug 5 fix : resume si session récente <30min encore ouverte
    # + auto-close des sessions stales >2h (effet bord de get_or_resume_session)
    _state.session_id, _resumed = memory.get_or_resume_session(track="unknown", car="unknown", session_type="boot")
    logger.info(f"Session #{_state.session_id} {'resumed' if _resumed else 'started'} in DB")

    stop = threading.Event()
    t_shm = threading.Thread(target=shm_loop, args=(stop,), daemon=True)
    t_audio = threading.Thread(target=audio_loop, args=(stop,), daemon=True)
    t_playstatus = threading.Thread(target=play_status_listener, args=(stop,), daemon=True)
    t_shm.start(); t_audio.start(); t_playstatus.start()
    try:
        uvicorn.run(app, host="127.0.0.1", port=HTTP_PORT, log_level="warning")
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("=== Bono v2 core_service stop ===")
        stop.set()
        if _state.session_id:
            memory.end_session(_state.session_id, summary="boot session")


if __name__ == "__main__":
    main()
