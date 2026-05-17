"""Bono v2 — Playback service.
- Pulls TTS MP3 fetch requests from Core via ZMQ
- Synthesizes via ElevenLabs Flash v2.5 (primary, ~75ms) or Fish Audio (fallback)
- Plays on Casque device via miniaudio native (primary, 0ms overhead) or PS MediaPlayer (fallback)
- Publishes playback state (idle/playing/stopped) back to Core

Cache key : SHA1(text + voice_id + provider + mood) → mp3 in tts_cache/
Barge-in : Core sends `play.stop` → miniaudio device stop + kill PS subprocess
"""
import sys
import os
import hashlib
import time
import threading
import subprocess
import tempfile
import io
from pathlib import Path
import httpx
import orjson

sys.path.insert(0, str(Path(__file__).parent))
from config import (
    ZMQ_PLAYBACK_REQ, ZMQ_EVENTS, TTS_CACHE_DIR, FISH_API, FISH_MODEL, get_secrets
)
from zmq_bus import make_pull, make_pub, send_json
from loguru import logger

SECRETS = get_secrets()
FISH_KEY = SECRETS.get("fish_key", "")
FISH_VOICE = SECRETS.get("fish_voice_id", "")
ELEVENLABS_KEY = SECRETS.get("elevenlabs_key", "")
ELEVENLABS_VOICE = SECRETS.get("elevenlabs_voice_id", "")
ELEVENLABS_MODEL = SECRETS.get("elevenlabs_model", "eleven_flash_v2_5")  # 75ms latency
TTS_PROVIDER = os.environ.get("BONO_TTS_PROVIDER", "elevenlabs").lower()  # elevenlabs|fish

# V2.3 fix : per-cache-key lock to prevent race condition when 2 threads synth same text simultaneously
_cache_locks: dict[str, threading.Lock] = {}
_cache_locks_master = threading.Lock()


def _get_cache_lock(key: str) -> threading.Lock:
    with _cache_locks_master:
        if key not in _cache_locks:
            _cache_locks[key] = threading.Lock()
        return _cache_locks[key]


def _atomic_write_bytes(path: Path, data: bytes):
    """Write bytes atomically : write to .tmp then os.replace. Prevents partial writes / corruption."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(str(tmp), str(path))

_play_lock = threading.Lock()
_current_proc: subprocess.Popen | None = None


CACHE_MAX_SIZE_MB = 500  # LRU eviction threshold (GLM #4 audit)


def cache_key(text: str, voice_id: str, mood: str = "calm", provider: str = "fish") -> Path:
    h = hashlib.sha1(f"{provider}|{voice_id}|{mood}|{text}".encode("utf-8")).hexdigest()[:24]
    return TTS_CACHE_DIR / f"{h}.mp3"


def cache_evict_if_needed():
    """LRU eviction : si cache > CACHE_MAX_SIZE_MB, supprime les MP3 les plus anciens."""
    try:
        files = sorted(TTS_CACHE_DIR.glob("*.mp3"), key=lambda p: p.stat().st_atime)
        total_mb = sum(f.stat().st_size for f in files) / (1024 * 1024)
        if total_mb < CACHE_MAX_SIZE_MB:
            return
        evicted = 0
        for f in files:
            if total_mb < CACHE_MAX_SIZE_MB * 0.8:
                break
            sz = f.stat().st_size / (1024 * 1024)
            f.unlink()
            total_mb -= sz
            evicted += 1
        if evicted:
            logger.info(f"[cache] LRU evicted {evicted} files, now {total_mb:.1f} MB")
    except Exception as e:
        logger.warning(f"[cache] evict err : {e}")


def fish_synthesize(text: str, voice_id: str, mood: str = "calm") -> bytes | None:
    """Returns MP3 bytes via Fish Audio. Uses cache if present. V2.3 : per-key lock + atomic write."""
    path = cache_key(text, voice_id, mood, provider="fish")
    lock = _get_cache_lock(str(path))
    with lock:
        # Double-check after lock acquired (another thread may have synth meanwhile)
        if path.exists() and path.stat().st_size > 100:
            return path.read_bytes()
        if not FISH_KEY:
            logger.error("No FishAudioApiKey")
            return None
        try:
            t0 = time.time()
            r = httpx.post(
                FISH_API,
                headers={"Authorization": f"Bearer {FISH_KEY}", "Content-Type": "application/json"},
                json={"text": text, "reference_id": voice_id, "format": "mp3", "normalize": True, "latency": "normal", "model": FISH_MODEL},
                timeout=20,
            )
            if r.status_code != 200:
                logger.error(f"Fish HTTP {r.status_code}: {r.text[:200]}")
                return None
            mp3 = r.content
            _atomic_write_bytes(path, mp3)
            cache_evict_if_needed()
            logger.info(f"Fish synth + cached ({len(mp3)}B, {int((time.time()-t0)*1000)}ms) : '{text[:50]}'")
            return mp3
        except Exception as e:
            logger.error(f"Fish synth fail : {e}")
            return None


def elevenlabs_synthesize(text: str, voice_id: str, mood: str = "calm", streaming: bool = False) -> bytes | None:
    """Returns MP3 bytes via ElevenLabs Flash v2.5 (~75ms first-byte latency).
    V2.3 : per-cache-key lock + atomic write to prevent corruption on concurrent same-text synth.
    streaming=True : use /stream endpoint (collect chunks). TTFT ~75ms, total similar but chunked transport.
    Mood maps to stability/style :
      - calm   : stab=0.75 style=0.0 (precise, low energy)
      - urgent : stab=0.55 style=0.15 (more expressive)
    """
    path = cache_key(text, voice_id, mood, provider="elevenlabs")
    lock = _get_cache_lock(str(path))
    with lock:
        # Double-check after lock acquired
        if path.exists() and path.stat().st_size > 100:
            return path.read_bytes()
        if not ELEVENLABS_KEY or not voice_id:
            logger.error("No ElevenLabsApiKey or voice_id")
            return None
        stability = 0.55 if mood == "urgent" else 0.75
        style = 0.15 if mood == "urgent" else 0.0
        endpoint_suffix = "/stream" if streaming else ""
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}{endpoint_suffix}"
        try:
            t0 = time.time()
            first_byte_at = None
            if streaming:
                chunks = []
                with httpx.stream(
                    "POST", url,
                    headers={"xi-api-key": ELEVENLABS_KEY, "Content-Type": "application/json", "Accept": "audio/mpeg"},
                    json={
                        "text": text,
                        "model_id": ELEVENLABS_MODEL,
                        "voice_settings": {"stability": stability, "similarity_boost": 0.95, "style": style, "use_speaker_boost": True},
                        "output_format": "mp3_44100_128",
                    },
                    timeout=15,
                ) as r:
                    if r.status_code != 200:
                        body = r.read().decode("utf-8", errors="ignore")
                        logger.error(f"ElevenLabs stream HTTP {r.status_code}: {body[:200]}")
                        return None
                    for chunk in r.iter_bytes(chunk_size=4096):
                        if first_byte_at is None and chunk:
                            first_byte_at = (time.time() - t0) * 1000
                        chunks.append(chunk)
                mp3 = b"".join(chunks)
            else:
                r = httpx.post(url,
                    headers={"xi-api-key": ELEVENLABS_KEY, "Content-Type": "application/json", "Accept": "audio/mpeg"},
                    json={
                        "text": text,
                        "model_id": ELEVENLABS_MODEL,
                        "voice_settings": {"stability": stability, "similarity_boost": 0.95, "style": style, "use_speaker_boost": True},
                        "output_format": "mp3_44100_128",
                    },
                    timeout=15,
                )
                if r.status_code != 200:
                    logger.error(f"ElevenLabs HTTP {r.status_code}: {r.text[:200]}")
                    return None
                mp3 = r.content
            _atomic_write_bytes(path, mp3)
            cache_evict_if_needed()
            total_ms = int((time.time() - t0) * 1000)
            ttfb_str = f"TTFB={int(first_byte_at)}ms " if first_byte_at else ""
            logger.info(f"ElevenLabs synth + cached ({len(mp3)}B, {ttfb_str}total={total_ms}ms, {ELEVENLABS_MODEL}, stream={streaming}) : '{text[:50]}'")
            return mp3
        except Exception as e:
            logger.error(f"ElevenLabs synth fail : {e}")
            return None


def synthesize(text: str, voice_override: str | None = None, mood: str = "calm") -> bytes | None:
    """Provider-aware TTS dispatch with automatic fallback.
    Order : preferred provider → fallback provider. Uses BONO_TTS_PROVIDER env var."""
    if TTS_PROVIDER == "elevenlabs":
        voice = voice_override or ELEVENLABS_VOICE
        mp3 = elevenlabs_synthesize(text, voice, mood)
        if mp3:
            return mp3
        logger.warning("[tts] ElevenLabs fail, fallback Fish")
        return fish_synthesize(text, voice_override or FISH_VOICE, mood)
    else:  # fish primary
        voice = voice_override or FISH_VOICE
        mp3 = fish_synthesize(text, voice, mood)
        if mp3:
            return mp3
        logger.warning("[tts] Fish fail, fallback ElevenLabs")
        return elevenlabs_synthesize(text, voice_override or ELEVENLABS_VOICE, mood)


_stop_event = threading.Event()


def play_mp3_blocking(mp3_bytes: bytes, device: str = "Casque") -> bool:
    """Plays MP3 via miniaudio (Python native, ~0ms boot vs PowerShell ~300ms).
    Falls back to PowerShell MediaPlayer if miniaudio fails.
    Blocking call. _stop_event.set() interrupts (barge-in).

    Real duration measured via miniaudio.decode() metadata (not bitrate estimation).
    """
    global _current_proc
    with _play_lock:
        _stop_event.clear()
        try:
            import miniaudio
            # Decode mp3 to PCM frames first (gives true duration via metadata)
            decoded = miniaudio.decode(mp3_bytes, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=2, sample_rate=44100)
            duration_s = decoded.num_frames / decoded.sample_rate
            est_ms = int(duration_s * 1000) + 200  # 200ms safety tail
            t0 = time.time()
            stream = miniaudio.stream_memory(mp3_bytes)
            # V2.3 fix : explicit dev.stop() + dev.close() before return (clean WASAPI release)
            dev = miniaudio.PlaybackDevice()
            interrupted = False
            try:
                dev.start(stream)
                while (time.time() - t0) * 1000 < est_ms:
                    if _stop_event.is_set():
                        interrupted = True
                        elapsed = int((time.time() - t0) * 1000)
                        logger.info(f"[barge-in] miniaudio interrupted at {elapsed}/{est_ms}ms")
                        break
                    time.sleep(0.02)  # 20ms poll for barge-in reactivity
            finally:
                try: dev.stop()
                except Exception: pass
                try: dev.close()
                except Exception: pass
            if interrupted:
                return False
            elapsed = int((time.time() - t0) * 1000)
            logger.info(f"[playback] miniaudio done ({len(mp3_bytes)}B, duration={duration_s:.2f}s, elapsed={elapsed}ms)")
            return True
        except Exception as e:
            logger.warning(f"[playback] miniaudio fail ({e}), fallback PowerShell MediaPlayer")
        # Fallback PowerShell
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp.write(mp3_bytes); tmp.close()
        try:
            ps_path = tmp.name.replace("\\", "\\\\")
            ps_script = (
                "Add-Type -AssemblyName presentationCore;"
                "$p = New-Object System.Windows.Media.MediaPlayer;"
                "$p.Volume = 1.0;"
                f'$p.Open([uri]"{ps_path}");'
                "Start-Sleep -Milliseconds 200; $p.Play();"
                "$tries=0; while (-not $p.NaturalDuration.HasTimeSpan -and $tries -lt 30) { Start-Sleep -Milliseconds 50; $tries++ };"
                "$d = if ($p.NaturalDuration.HasTimeSpan) { $p.NaturalDuration.TimeSpan.TotalSeconds } else { 6 };"
                "Start-Sleep -Milliseconds ([int]($d * 1000) + 300);"
                "$p.Stop(); $p.Close()"
            )
            _current_proc = subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
                creationflags=0x08000000
            )
            logger.info(f"[playback] fallback PS PID={_current_proc.pid}")
            _current_proc.wait()
            _current_proc = None
            return True
        except Exception as e:
            logger.error(f"play_mp3 fallback fail : {e}")
            return False
        finally:
            try: os.unlink(tmp.name)
            except Exception: pass


def stop_playback():
    """Barge-in : signal miniaudio stop + kill PowerShell fallback if any."""
    global _current_proc
    _stop_event.set()
    if _current_proc:
        try:
            _current_proc.kill()
            logger.info(f"[barge-in] killed fallback PID={_current_proc.pid}")
        except Exception: pass
        _current_proc = None


# Message arbitration / priority queue (consensus 3 IAs 16/05 — GPT "LE plus gros manque structurel") :
# Severity warn > info. Auto-events critique (yellow/fuel_critical/tyre_cliff) > PB > lap_completed > PTT response.
# Architecture : on collecte les messages dans une priority queue locale, on draine entre chaque play_mp3_blocking,
# on choisit le plus prioritaire encore valide (TTL OK + turn pas invalidated).
import heapq

# (priority, ts_received, payload_dict). Plus petit = plus prioritaire.
_pending_msgs: list = []
_pending_seq = 0

PRIORITY_BY_EVENT = {
    "spotter_close": -2,           # SAFETY MAX absolute (collision imminent)
    "damage_warning": -1,          # SAFETY (just after collision)
    "engine_overheat": -1,         # SAFETY engine
    "fuel_critical": 0,            # SAFETY/CRITICAL
    "tyre_cliff": 0,
    "yellow_flag": 0,
    "track_limits_warning": 0,
    "last_lap": 0,                 # CRITICAL race finish
    "closing_rate": 1,             # WARN race tactical
    "oversteer_detected": 1,
    "traffic_on_push_lap": 1,
    "brake_temp_high": 2,
    "rain_incoming_10min": 3,
    "fuel_save": 4,
    "fuel_warning": 5,             # WARNING
    "position_change": 5,          # race
    "chequered_flag": 5,
    "rain_incoming_30min": 6,
    "understeer_detected": 7,
    "brake_temp_cold": 8,
    "opponent_pit_detected": 9,
    "personal_best": 10,           # INFO important
    "pace_dictation": 11,
    "pit_window_open": 12,
    "warmup_complete": 13,
    "pre_race_grid_check": 2,      # Pre-race grid : critique avant départ
    "session_brief": 14,           # intro slow priority (peut attendre)
    "race_intro": 14,
    "out_lap_started": 14,
    "laps_remaining_5": 14,
    "laps_remaining_2": 12,
    "laps_remaining_1": 8,
    "lap_completed": 15,           # INFO normal
}


def _enqueue_msg(payload: dict):
    """Push into priority queue. Critical events go to head."""
    global _pending_seq
    auto_evt = payload.get("auto_event", "")
    prio = PRIORITY_BY_EVENT.get(auto_evt, 20)  # PTT response = 20 (low prio, but won't drop)
    _pending_seq += 1
    heapq.heappush(_pending_msgs, (prio, _pending_seq, payload))


def _drain_next_valid_msg(invalidated_turns: set) -> dict | None:
    """Pop the most prioritary msg that still has valid TTL + non-invalidated turn."""
    now = time.time()
    while _pending_msgs:
        prio, seq, payload = heapq.heappop(_pending_msgs)
        turn_id = payload.get("turn_id")
        if turn_id is not None and turn_id in invalidated_turns:
            logger.info(f"[playback] drop invalidated turn#{turn_id} from queue")
            continue
        deadline_ts = payload.get("deadline_ts")
        if deadline_ts is not None and now > deadline_ts:
            age = now - payload.get("issued_at", deadline_ts - 10)
            logger.info(f"[playback] drop stale TTL turn#{turn_id} age={age:.1f}s prio={prio}")
            continue
        return payload
    return None


def main():
    logger.add("logs/playback_service.log", rotation="10 MB", retention=3)
    logger.info("=== Bono v2 playback_service start ===")
    logger.info(f"TTS provider={TTS_PROVIDER} | EL voice={ELEVENLABS_VOICE[:8]}... model={ELEVENLABS_MODEL} | Fish voice={FISH_VOICE[:8]}...")
    import zmq
    pull = make_pull(ZMQ_PLAYBACK_REQ)
    # Non-blocking recv pour pouvoir draine la queue entre les plays
    pull.setsockopt(zmq.RCVTIMEO, 100)  # 100ms poll
    pub_events = make_pub(ZMQ_EVENTS)
    pub_events.setsockopt(zmq.SNDTIMEO, 200)
    pub_events.setsockopt(zmq.SNDHWM, 50)
    time.sleep(0.3)
    invalidated_turns: set[int] = set()
    logger.info(f"ZMQ PULL {ZMQ_PLAYBACK_REQ} | PUB {ZMQ_EVENTS} | priority queue active")
    try:
        while True:
            # 1. Drain socket : enqueue tout ce qui arrive
            try:
                while True:
                    parts = pull.recv_multipart(flags=zmq.NOBLOCK)
                    if len(parts) < 2:
                        continue
                    topic = parts[0].decode()
                    payload = orjson.loads(parts[1])
                    if topic == "play.stop":
                        stop_playback()
                        inv = payload.get("invalidate_turn")
                        if inv is not None:
                            invalidated_turns.add(int(inv))
                            if len(invalidated_turns) > 200:
                                invalidated_turns = set(list(invalidated_turns)[-100:])
                        try: send_json(pub_events, "play.status", {"state": "stopped", "invalidated_turn": inv})
                        except Exception: pass
                    elif topic == "play.text":
                        _enqueue_msg(payload)
            except zmq.error.Again:
                pass  # socket vide
            # 2. Si queue non-vide : pop le + prioritaire et play
            payload = _drain_next_valid_msg(invalidated_turns)
            if payload is None:
                # rien à jouer → wait 100ms next recv
                try:
                    parts = pull.recv_multipart()  # blocking
                    if len(parts) < 2: continue
                    topic = parts[0].decode()
                    payload2 = orjson.loads(parts[1])
                    if topic == "play.text":
                        _enqueue_msg(payload2)
                    elif topic == "play.stop":
                        stop_playback()
                        inv = payload2.get("invalidate_turn")
                        if inv is not None:
                            invalidated_turns.add(int(inv))
                except zmq.error.Again:
                    pass
                continue
            # 3. Play the chosen payload
            topic = "play.text"
            turn_id = payload.get("turn_id")
            if topic == "play.text":
                # Check turn_id validity (cancellation policy — GPT audit)
                if turn_id is not None and turn_id in invalidated_turns:
                    logger.info(f"[playback] turn#{turn_id} invalidated, skip")
                    continue
                # TTL deadline check (GPT-5.4 reco P1) : drop if stale (race conditions changed)
                deadline_ts = payload.get("deadline_ts")
                if deadline_ts is not None and time.time() > deadline_ts:
                    age = time.time() - payload.get("issued_at", deadline_ts - 10)
                    logger.info(f"[playback] turn#{turn_id} TTL expired (age={age:.1f}s), drop stale message")
                    try: send_json(pub_events, "play.status", {"state": "ttl_expired", "turn_id": turn_id, "age_s": round(age, 1)})
                    except Exception: pass
                    continue
                text = payload.get("text", "").strip()
                voice_override = payload.get("voice")  # None → use default from provider
                mood = payload.get("mood", "calm")
                if not text:
                    continue
                try:
                    send_json(pub_events, "play.status", {"state": "synthesizing", "text": text[:80], "turn_id": turn_id, "provider": TTS_PROVIDER})
                except Exception: pass
                # Fix F7 17/05 : instrumentation TTS_ms + playback_ms cross-service.
                # Avant ce fix : pipeline_timings.tts_ms=0 et playback_ms=NULL hardcodés.
                # Maintenant : on mesure, on émet via play.status final pour que core_service
                # mette à jour la ligne pipeline_timings du turn_id.
                _t_tts0 = time.time()
                mp3 = synthesize(text, voice_override=voice_override, mood=mood)
                _tts_ms = int((time.time() - _t_tts0) * 1000)
                if not mp3:
                    try: send_json(pub_events, "play.status", {"state": "synth_fail", "text": text[:80], "turn_id": turn_id, "tts_ms": _tts_ms})
                    except Exception: pass
                    continue
                # Re-check invalidation after synth (could be cancelled during fish call)
                if turn_id is not None and turn_id in invalidated_turns:
                    logger.info(f"[playback] turn#{turn_id} invalidated post-synth, skip play")
                    continue
                # Re-check TTL after synth (could have expired during synth wait)
                if deadline_ts is not None and time.time() > deadline_ts:
                    age = time.time() - payload.get("issued_at", deadline_ts - 10)
                    logger.info(f"[playback] turn#{turn_id} TTL expired post-synth (age={age:.1f}s), skip play")
                    try: send_json(pub_events, "play.status", {"state": "ttl_expired_post_synth", "turn_id": turn_id, "age_s": round(age, 1)})
                    except Exception: pass
                    continue
                try: send_json(pub_events, "play.status", {"state": "playing", "text": text[:80], "size": len(mp3), "turn_id": turn_id})
                except Exception: pass
                _t_play0 = time.time()
                ok = play_mp3_blocking(mp3, device=payload.get("device", "Casque"))
                _playback_ms = int((time.time() - _t_play0) * 1000)
                # Émission play.done avec timings — core_service écoutera pour MAJ pipeline_timings.
                try:
                    send_json(pub_events, "play.status", {
                        "state": "done" if ok else "play_fail",
                        "text": text[:80], "turn_id": turn_id,
                        "tts_ms": _tts_ms, "playback_ms": _playback_ms,
                        "provider": TTS_PROVIDER,
                    })
                except Exception: pass
            # NB: play.stop est traité dans la phase 1 (drain), pas ici (la queue ne contient que play.text)
    except KeyboardInterrupt:
        logger.info("=== Bono v2 playback_service stop ===")


if __name__ == "__main__":
    main()
