"""Tests fix 0.1 — silence_zones DROP (not EXTEND) brief V3 17/05 nuit."""
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("BONO_TTS_PROVIDER", "elevenlabs")


def _setup_cs(brake=0.0, speed=100, steer=0.0, rpm=5000):
    import core_service as cs
    cs._state.snapshot = {
        "brake": brake, "speed_kmh": speed,
        "steer_angle_rad": steer, "rpm": rpm,
    }
    cs._state.fake_mode = False
    return cs


def test_hard_braking_drops_info():
    """brake>0.6 + speed>150 → info DROP."""
    cs = _setup_cs(brake=0.7, speed=180)
    fires = []
    cs._state.session_id = None
    cs.send_play_text = lambda p: fires.append(p) or True
    cs.fire_event("pace_drift", "info", "test pace drift")
    # Si DROP, send_play_text n'est pas appelé. Si EXTEND, oui (mais sans session_id ça skip log_event).
    # On vérifie que ttl_s n'a PAS été modifié (DROP early return)
    assert fires == [], f"Expected DROP, got {len(fires)} plays"


def test_mid_corner_drops_info():
    cs = _setup_cs(brake=0.0, speed=90, steer=0.5)
    fires = []
    cs._state.session_id = None
    cs.send_play_text = lambda p: fires.append(p) or True
    cs.fire_event("pace_drift", "info", "mid-corner test")
    assert fires == []


def test_high_rpm_straight_drops_info():
    cs = _setup_cs(brake=0.0, speed=250, rpm=8200)
    fires = []
    cs._state.session_id = None
    cs.send_play_text = lambda p: fires.append(p) or True
    cs.fire_event("pace_drift", "info", "high rpm test")
    assert fires == []


def test_warn_passes_in_zone():
    """warn/critical doivent passer même en zone."""
    cs = _setup_cs(brake=0.9, speed=200)
    fires = []
    cs._state.session_id = None
    cs.send_play_text = lambda p: fires.append(p) or True
    cs.fire_event("yellow_flag", "warn", "yellow")
    # send_play_text est censé être appelé (warn = priority)
    assert len(fires) >= 1, f"Expected warn to pass, got {fires}"


def test_normal_zone_passes_info():
    cs = _setup_cs(brake=0.1, speed=100, steer=0.05)
    fires = []
    cs._state.session_id = None
    cs.send_play_text = lambda p: fires.append(p) or True
    cs.fire_event("personal_best", "info", "PB")
    assert len(fires) >= 1, f"Expected info in normal zone to pass, got {fires}"


if __name__ == "__main__":
    test_hard_braking_drops_info()
    test_mid_corner_drops_info()
    test_high_rpm_straight_drops_info()
    test_warn_passes_in_zone()
    test_normal_zone_passes_info()
    print("ALL TESTS PASS")
