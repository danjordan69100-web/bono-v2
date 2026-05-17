"""ZeroMQ helpers for Bono v2 IPC.

Topics:
  - tele.snapshot    : Core publishes SHM snapshot (~30 Hz)
  - tele.event       : Core publishes detected event (lap_completed, fuel_low, etc.)
  - audio.ptt        : Input publishes captured PTT audio (WAV bytes)
  - play.request     : Core requests playback (Fish synthesize + MediaPlayer)
  - play.status      : Playback publishes its state (idle/playing/stopped)
"""
import zmq
import orjson
from typing import Optional


def make_pub(endpoint: str, conflate: bool = False, hwm: int = 100) -> zmq.Socket:
    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.PUB)
    sock.setsockopt(zmq.SNDHWM, hwm)
    if conflate:
        sock.setsockopt(zmq.CONFLATE, 1)
    sock.bind(endpoint)
    return sock


def make_sub(endpoint: str, topic: str = "", conflate: bool = False, hwm: int = 100) -> zmq.Socket:
    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.SUB)
    sock.setsockopt(zmq.RCVHWM, hwm)
    if conflate:
        sock.setsockopt(zmq.CONFLATE, 1)
    sock.setsockopt_string(zmq.SUBSCRIBE, topic)
    sock.connect(endpoint)
    return sock


def make_push(endpoint: str, hwm: int = 5, sndtimeo_ms: int = 2000) -> zmq.Socket:
    """PUSH socket with bounded queue. Default HWM=5, SNDTIMEO=2s (drop-blocking policy).
    Saturation → sock.send raises zmq.Again → caller must handle (drop oldest)."""
    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.PUSH)
    sock.setsockopt(zmq.SNDHWM, hwm)
    sock.setsockopt(zmq.SNDTIMEO, sndtimeo_ms)
    sock.bind(endpoint)
    return sock


def make_pull(endpoint: str, hwm: int = 5) -> zmq.Socket:
    """PULL socket with bounded queue. Default HWM=5."""
    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.PULL)
    sock.setsockopt(zmq.RCVHWM, hwm)
    sock.connect(endpoint)
    return sock


def send_json(sock: zmq.Socket, topic: str, payload: dict):
    sock.send_multipart([topic.encode(), orjson.dumps(payload)])


def recv_json(sock: zmq.Socket, timeout_ms: int = -1) -> Optional[tuple]:
    """Returns (topic_str, payload_dict) or None on timeout."""
    if timeout_ms >= 0:
        if not sock.poll(timeout_ms):
            return None
    parts = sock.recv_multipart()
    if len(parts) >= 2:
        return parts[0].decode(), orjson.loads(parts[1])
    return None


def send_bytes(sock: zmq.Socket, kind: str, meta: dict, raw: bytes):
    """Multi-part : [topic, json meta, raw bytes]"""
    sock.send_multipart([kind.encode(), orjson.dumps(meta), raw])


def recv_bytes(sock: zmq.Socket, timeout_ms: int = -1) -> Optional[tuple]:
    if timeout_ms >= 0:
        if not sock.poll(timeout_ms):
            return None
    parts = sock.recv_multipart()
    if len(parts) >= 3:
        return parts[0].decode(), orjson.loads(parts[1]), parts[2]
    return None
