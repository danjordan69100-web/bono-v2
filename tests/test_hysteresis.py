"""Tests hysteresis_check (V2.3 B1 + D1 reset_delay debounce)."""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_basic_confirm_delay():
    from core_service import hysteresis_check, _event_latch
    _event_latch.clear()
    # condition True, no confirm needed
    assert hysteresis_check("t1", True, confirm_delay_s=0, cooldown_s=0) is True


def test_confirm_delay_not_yet():
    from core_service import hysteresis_check, _event_latch
    _event_latch.clear()
    r = hysteresis_check("t2", True, confirm_delay_s=1.0, cooldown_s=10)
    assert r is False  # confirming


def test_confirm_delay_after_wait():
    from core_service import hysteresis_check, _event_latch
    _event_latch.clear()
    hysteresis_check("t3", True, confirm_delay_s=0.2, cooldown_s=10)
    time.sleep(0.25)
    r = hysteresis_check("t3", True, confirm_delay_s=0.2, cooldown_s=10)
    assert r is True


def test_latching_blocks_refire():
    from core_service import hysteresis_check, _event_latch
    _event_latch.clear()
    assert hysteresis_check("t4", True, confirm_delay_s=0, cooldown_s=0) is True
    # Refire same condition while latched : False
    assert hysteresis_check("t4", True, confirm_delay_s=0, cooldown_s=0) is False


def test_exit_condition_releases_latch():
    from core_service import hysteresis_check, _event_latch
    _event_latch.clear()
    assert hysteresis_check("t5", True, exit_condition=False, confirm_delay_s=0, cooldown_s=0) is True
    # Exit met : doesn't fire, but releases latch
    assert hysteresis_check("t5", False, exit_condition=True, confirm_delay_s=0, cooldown_s=0) is False
    # Re-entry now possible
    assert hysteresis_check("t5", True, exit_condition=False, confirm_delay_s=0, cooldown_s=0) is True


def test_dedup_key_refires_in_active():
    from core_service import hysteresis_check, _event_latch
    _event_latch.clear()
    assert hysteresis_check("t6", True, dedup_key="band=A", confirm_delay_s=0, cooldown_s=0) is True
    # Same key : no refire
    assert hysteresis_check("t6", True, dedup_key="band=A", confirm_delay_s=0, cooldown_s=0) is False
    # New key : refire
    assert hysteresis_check("t6", True, dedup_key="band=B", confirm_delay_s=0, cooldown_s=0) is True


def test_reset_delay_debounce_flicker():
    """V2.3 D1 Gemini reco : brief False flicker during confirm should not reset accumulator."""
    from core_service import hysteresis_check, _event_latch
    _event_latch.clear()
    # Start confirming
    hysteresis_check("flicker", True, confirm_delay_s=1.0, cooldown_s=10, reset_delay_s=0.5)
    time.sleep(0.3)
    # Brief False (noise)
    hysteresis_check("flicker", False, confirm_delay_s=1.0, cooldown_s=10, reset_delay_s=0.5)
    time.sleep(0.1)
    # Back True : accumulator preserved
    hysteresis_check("flicker", True, confirm_delay_s=1.0, cooldown_s=10, reset_delay_s=0.5)
    time.sleep(0.7)
    # Total confirm = ~1.1s, should fire despite flicker
    r = hysteresis_check("flicker", True, confirm_delay_s=1.0, cooldown_s=10, reset_delay_s=0.5)
    assert r is True


def test_reset_delay_true_reset_after_extended_false():
    """If False persists longer than reset_delay_s, accumulator IS reset."""
    from core_service import hysteresis_check, _event_latch
    _event_latch.clear()
    hysteresis_check("reset", True, confirm_delay_s=1.0, cooldown_s=10, reset_delay_s=0.2)
    time.sleep(0.1)
    hysteresis_check("reset", False, confirm_delay_s=1.0, cooldown_s=10, reset_delay_s=0.2)
    time.sleep(0.3)  # > reset_delay 0.2s
    hysteresis_check("reset", False, confirm_delay_s=1.0, cooldown_s=10, reset_delay_s=0.2)
    # Now back True : accumulator was reset, need full 1s
    hysteresis_check("reset", True, confirm_delay_s=1.0, cooldown_s=10, reset_delay_s=0.2)
    time.sleep(0.5)
    r = hysteresis_check("reset", True, confirm_delay_s=1.0, cooldown_s=10, reset_delay_s=0.2)
    assert r is False  # only ~0.5s into new confirm window


if __name__ == "__main__":
    # Manual run
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  {name} PASS")
            except AssertionError as e:
                print(f"  {name} FAIL: {e}")
            except Exception as e:
                print(f"  {name} ERROR: {e}")
