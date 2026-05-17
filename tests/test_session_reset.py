"""Tests reset_latches_on_session_transition + _reset_state_for_new_session (F2 + F10 17/05)."""
import sys
import os
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("BONO_TTS_PROVIDER", "elevenlabs")


def _import_core():
    """Import core_service (handles already-imported case)."""
    import core_service as cs  # noqa
    return cs


def test_blacklist_resets_all_except_transients():
    """F10 invariant : un nouvel event ajouté hors NEVER_RESET doit être reset par défaut."""
    cs = _import_core()
    cs._event_latch.clear()
    cs._state.last_session_signature = "monza|bmw_m4_gt3|RACE|1"
    # Simule plusieurs events fired (latched)
    now = time.time()
    cs._event_latch["fuel_warning"] = {"active": True, "first_seen_ts": 0, "last_fire_ts": now, "last_dedup_key": "", "last_false_ts": 0}
    cs._event_latch["yellow_flag"] = {"active": True, "first_seen_ts": 0, "last_fire_ts": now, "last_dedup_key": "", "last_false_ts": 0}
    cs._event_latch["my_new_invented_event"] = {"active": True, "first_seen_ts": 0, "last_fire_ts": now, "last_dedup_key": "", "last_false_ts": 0}
    # Transition vers nouveau combo
    cs.reset_latches_on_session_transition({"session": "PRACTICE", "track": "spa", "car": "bmw_m4_gt3", "is_in_pit": False})
    # yellow_flag (transient blacklisté) doit rester
    assert "yellow_flag" in cs._event_latch
    # fuel_warning + nouvel event invented doivent être reset (blacklist behavior)
    assert "fuel_warning" not in cs._event_latch
    assert "my_new_invented_event" not in cs._event_latch


def test_state_flags_reset_on_session_change():
    """F2 invariant : flags _announced reset en transition session."""
    cs = _import_core()
    cs._state.last_session_signature = "monza|bmw_m4_gt3|QUALIFY|1"
    cs._state.session_briefed = True
    cs._state.race_intro_announced = True
    cs._state.chequered_announced = True
    cs._state.last_position = 5
    cs._state.last_lap_count = 12
    cs._state.current_lap_sectors = [33000, 28000, 32000]
    cs._state.laps_remaining_announced = {5, 2}
    # Transition
    cs.reset_latches_on_session_transition({"session": "RACE", "track": "monza", "car": "bmw_m4_gt3", "is_in_pit": False})
    assert cs._state.session_briefed is False
    assert cs._state.race_intro_announced is False
    assert cs._state.chequered_announced is False
    assert cs._state.last_position == -1
    assert cs._state.last_lap_count == 0
    assert cs._state.current_lap_sectors == []
    assert cs._state.laps_remaining_announced == set()


def test_no_reset_on_same_signature():
    """Pas de reset si signature identique (idempotence)."""
    cs = _import_core()
    # signature courante doit matcher l'état complet du _state.session_id (réel ou 0 par défaut)
    sid_part = f"|{cs._state.session_id or 0}"
    cs._state.last_session_signature = f"monza|bmw_m4_gt3|RACE{sid_part}"
    cs._state.session_briefed = True
    cs.reset_latches_on_session_transition({"session": "RACE", "track": "monza", "car": "bmw_m4_gt3", "is_in_pit": False})
    # Pas de reset : signature identique
    assert cs._state.session_briefed is True


def test_pit_exit_resets_stint_latches():
    """Pit-exit reset les latches stint (fuel_warning, tyre_cliff, brake_temp)."""
    cs = _import_core()
    cs._event_latch.clear()
    cs._state.last_in_pit_at_reset = True  # était en pit
    cs._state.last_session_signature = "monza|bmw_m4_gt3|RACE|1"
    now = time.time()
    cs._event_latch["fuel_warning"] = {"active": True, "first_seen_ts": 0, "last_fire_ts": now, "last_dedup_key": "", "last_false_ts": 0}
    cs._event_latch["tyre_cliff"] = {"active": True, "first_seen_ts": 0, "last_fire_ts": now, "last_dedup_key": "", "last_false_ts": 0}
    # Sort du pit
    cs.reset_latches_on_session_transition({"session": "RACE", "track": "monza", "car": "bmw_m4_gt3", "is_in_pit": False})
    assert "fuel_warning" not in cs._event_latch
    assert "tyre_cliff" not in cs._event_latch


if __name__ == "__main__":
    test_blacklist_resets_all_except_transients()
    test_state_flags_reset_on_session_change()
    test_no_reset_on_same_signature()
    test_pit_exit_resets_stint_latches()
    print("ALL TESTS PASS")
