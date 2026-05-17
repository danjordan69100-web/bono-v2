"""Bono v2 — Input service.
- Polls Fanatec wheel btn 8 (pygame, 50 Hz)
- Captures mic via sounddevice on PTT down → PCM 16kHz mono
- PUSHs WAV bytes to Core via ZMQ on PTT up
- Heartbeat ZMQ every 5s

Immune to ACC focus (process séparé, sounddevice WASAPI shared, pygame DirectInput).
"""
import sounddevice as sd
import numpy as np
import pygame
import time
import io
import os
import wave
import sys
import threading
from pathlib import Path

# Local imports
sys.path.insert(0, str(Path(__file__).parent))
from config import (
    ZMQ_AUDIO_INPUT, ZMQ_TELEMETRY_PUB, SAMPLE_RATE, CHANNELS,
    PTT_BUTTON, PTT_MAX_DURATION_S, PTT_JOYSTICK_POLL_HZ, AUDIO_MIC_DEVICE,
)
from zmq_bus import make_push, send_bytes
from loguru import logger


def main():
    logger.add("logs/input_service.log", rotation="10 MB", retention=3)
    logger.info("=== Bono v2 input_service start ===")
    logger.info(f"PTT btn={PTT_BUTTON} max={PTT_MAX_DURATION_S}s rate={SAMPLE_RATE}Hz")

    # ZMQ PUSH for audio chunks (with HWM + SNDTIMEO — Gemini/GPT audit)
    import zmq
    push = make_push(ZMQ_AUDIO_INPUT)
    push.setsockopt(zmq.SNDTIMEO, 2000)
    push.setsockopt(zmq.SNDHWM, 5)
    logger.info(f"ZMQ PUSH bound : {ZMQ_AUDIO_INPUT} (HWM=5, SNDTIMEO=2s)")

    # pygame joystick init
    pygame.init()
    pygame.joystick.init()
    joys = []
    for i in range(pygame.joystick.get_count()):
        j = pygame.joystick.Joystick(i)
        j.init()
        if "FANATEC" in j.get_name().upper() and j.get_numbuttons() > PTT_BUTTON:
            joys.append(j)
            logger.info(f"Wheel [{i}] {j.get_name()!r} ({j.get_numbuttons()} buttons)")
    if not joys:
        logger.error("No FANATEC wheel found, exit")
        sys.exit(1)

    # Mic audio state
    recording = False
    audio_chunks = []
    record_start = 0.0
    stream = None
    lock = threading.Lock()

    # V2.3 D4 — STT streaming live :
    # En plus du buffer complet (envoyé post-release), on stream les chunks 100ms en temps réel
    # via topic 'audio.ptt.chunk' pour que core_service les forward à Deepgram Live WebSocket.
    # Le post-release envoi reste comme fallback robuste si Live WS down.
    STREAM_CHUNK_MS = 100
    STREAM_CHUNK_SAMPLES = int(SAMPLE_RATE * STREAM_CHUNK_MS / 1000)  # 1600 samples @ 16kHz
    _stream_buffer = []  # accumulator per stream-chunk
    _stream_seq = 0
    # V2.3 fix critique 3 IA unanime : OFF par défaut tant que consumer core_service n'est pas
    # implémenté (Deepgram Live WS async). Sinon DEADLOCK garanti : HWM=5 PUSH satures en 600ms
    # de capture → input_service bloque sur send() au 6ème chunk. À réactiver V2.4 quand consumer prêt.
    enable_streaming = os.environ.get("BONO_STT_STREAMING", "0") == "1"

    def audio_cb(indata, frames, time_info, status):
        nonlocal _stream_buffer, _stream_seq
        with lock:
            if recording:
                audio_chunks.append(indata.copy())
                if enable_streaming:
                    _stream_buffer.append(indata.copy())
                    # Each 100ms (1600 samples @ 16kHz) → send chunk
                    total = sum(c.shape[0] for c in _stream_buffer)
                    if total >= STREAM_CHUNK_SAMPLES:
                        try:
                            import numpy as _np
                            audio_arr = _np.concatenate(_stream_buffer, axis=0)
                            int16 = (audio_arr * 32767).clip(-32768, 32767).astype(_np.int16)
                            pcm_bytes = int16.tobytes()
                            _stream_seq += 1
                            meta = {"seq": _stream_seq, "ts": time.time(), "sample_rate": SAMPLE_RATE, "channels": CHANNELS, "is_pcm": True}
                            send_bytes(push, "audio.ptt.chunk", meta, pcm_bytes)
                        except Exception as e:
                            pass  # ignore stream chunk failures, post-release is fallback
                        _stream_buffer = []

    def start_record():
        nonlocal recording, audio_chunks, record_start, stream, _stream_buffer, _stream_seq
        with lock:
            if recording: return
            audio_chunks = []
            _stream_buffer = []
            _stream_seq = 0
            # Send stream start marker
            if enable_streaming:
                try:
                    send_bytes(push, "audio.ptt.stream_start", {"ts": time.time(), "sample_rate": SAMPLE_RATE}, b"")
                except Exception: pass
            record_start = time.time()
            recording = True
        try:
            stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS,
                                    dtype="float32", callback=audio_cb,
                                    device=AUDIO_MIC_DEVICE)
            stream.start()
            logger.info(f"PTT DOWN — recording started")
        except Exception as e:
            logger.error(f"start_record fail : {e}")
            recording = False

    def stop_and_send():
        nonlocal recording, stream
        with lock:
            if not recording: return
            recording = False
            dur = time.time() - record_start
        try:
            if stream:
                stream.stop(); stream.close(); stream = None
        except Exception: pass
        if dur < 0.3:
            logger.info(f"PTT UP — too short ({dur:.2f}s) skip")
            return
        audio = np.concatenate(audio_chunks, axis=0) if audio_chunks else np.zeros((0, CHANNELS), dtype="float32")
        if len(audio) == 0:
            logger.warning("Empty audio buffer")
            return
        rms = float(np.sqrt(np.mean(audio**2)))
        peak = float(np.max(np.abs(audio)))
        logger.info(f"PTT UP — {dur:.2f}s RMS={rms:.4f} peak={peak:.3f}")
        if rms < 0.001:
            logger.warning("Audio quasi-silent, send anyway (let Deepgram decide)")
        # Build WAV
        wav_buf = io.BytesIO()
        int16 = (audio * 32767).clip(-32768, 32767).astype(np.int16)
        with wave.open(wav_buf, "wb") as w:
            w.setnchannels(CHANNELS); w.setsampwidth(2); w.setframerate(SAMPLE_RATE)
            w.writeframes(int16.tobytes())
        wav_bytes = wav_buf.getvalue()
        # Input-side basic validation (GLM audit) : reject if too short OR essentially silent
        if dur < 0.5:
            logger.info(f"  dropped : too short ({dur:.2f}s < 0.5s)"); return
        if rms < 0.002:
            logger.info(f"  dropped : silent (RMS={rms:.5f} < 0.002)"); return
        # Send stream end marker first (so Live STT can finalize)
        if enable_streaming:
            try:
                send_bytes(push, "audio.ptt.stream_end", {"ts": time.time(), "total_chunks": _stream_seq}, b"")
            except Exception: pass
        # ZMQ PUSH WAV complet (fallback robuste si Live WS down ou échec)
        meta = {"ts": time.time(), "duration_s": dur, "rms": rms, "peak": peak,
                "sample_rate": SAMPLE_RATE, "channels": CHANNELS, "bytes": len(wav_bytes),
                "client_turn_seed": int(time.time() * 1000) % 1000000,
                "streaming_used": enable_streaming, "stream_chunks_sent": _stream_seq}
        try:
            import zmq as _zmq
            send_bytes(push, "audio.ptt", meta, wav_bytes)
            logger.info(f"ZMQ PUSHED {len(wav_bytes)} bytes to Core (stream chunks: {_stream_seq})")
        except Exception as e:
            logger.warning(f"ZMQ push fail (HWM full?) : {e}")

    # Polling loop
    prev = False
    poll_period = 1.0 / PTT_JOYSTICK_POLL_HZ
    try:
        while True:
            pygame.event.pump()
            pressed = False
            for j in joys:
                try:
                    if j.get_button(PTT_BUTTON):
                        pressed = True; break
                except Exception: pass
            if pressed and not prev:
                start_record()
            elif (not pressed) and prev:
                stop_and_send()
            prev = pressed
            if recording and (time.time() - record_start) > PTT_MAX_DURATION_S:
                logger.warning(f"Auto-stop {PTT_MAX_DURATION_S}s max")
                stop_and_send()
                prev = False
            # heartbeat retiré (audit P1 : non-consommé par core)
            time.sleep(poll_period)
    except KeyboardInterrupt:
        logger.info("=== Bono v2 input_service stop (KeyboardInterrupt) ===")
    finally:
        try: pygame.quit()
        except Exception: pass


if __name__ == "__main__":
    main()
