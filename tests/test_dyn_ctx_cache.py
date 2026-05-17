"""Tests cache dynamic_context (Phase A2 brief V3 17/05)."""
import sys
import os
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("BONO_TTS_PROVIDER", "elevenlabs")


def _snap_live(**override):
    base = {
        "shm_ok": True, "status": "LIVE", "read_at": time.time(),
        "track": "monza", "car": "bmw_m4_gt3", "session": "RACE",
        "completed_laps": 3, "position": 5,
        "best_time_ms": 105432, "last_time_ms": 107821,
        "speed_kmh": 220, "rpm": 7800, "gear": 5,
        "fuel_l": 45.0, "fuel_estimated_laps": 12.3,
    }
    base.update(override)
    return base


def test_cache_hit_returns_same_content():
    import core_service as cs
    cs._dyn_ctx_cache.clear()
    snap = _snap_live()
    c1 = cs.build_dynamic_context_cached(snap)
    c2 = cs.build_dynamic_context_cached(snap)
    assert c1 == c2
    assert len(cs._dyn_ctx_cache) == 1


def test_cache_miss_on_different_lap():
    import core_service as cs
    cs._dyn_ctx_cache.clear()
    cs.build_dynamic_context_cached(_snap_live(completed_laps=3))
    cs.build_dynamic_context_cached(_snap_live(completed_laps=4))
    assert len(cs._dyn_ctx_cache) == 2


def test_cache_expires_after_ttl():
    import core_service as cs
    cs._dyn_ctx_cache.clear()
    snap = _snap_live()
    cs.build_dynamic_context_cached(snap)
    # Force le cache à vieillir
    key = list(cs._dyn_ctx_cache.keys())[0]
    cs._dyn_ctx_cache[key] = (time.time() - 10.0, "STALE CONTENT")  # 10s = > TTL 5s
    fresh = cs.build_dynamic_context_cached(snap)
    assert fresh != "STALE CONTENT"
    assert "Track: monza" in fresh


def test_cache_skipped_when_shm_off():
    import core_service as cs
    cs._dyn_ctx_cache.clear()
    cs.build_dynamic_context_cached({"shm_ok": False})
    assert len(cs._dyn_ctx_cache) == 0


if __name__ == "__main__":
    test_cache_hit_returns_same_content()
    test_cache_miss_on_different_lap()
    test_cache_expires_after_ttl()
    test_cache_skipped_when_shm_off()
    print("ALL TESTS PASS")
