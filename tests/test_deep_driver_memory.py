"""Tests fix B.5 — deep_driver_memory function."""
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("BONO_TTS_PROVIDER", "elevenlabs")


def test_function_exists():
    import memory
    assert hasattr(memory, "deep_driver_memory")
    assert callable(memory.deep_driver_memory)


def test_no_history_returns_proper():
    """combo nouveau → no history dict propre."""
    import memory
    memory.init_db()
    result = memory.deep_driver_memory("monza_inventee_test_xyz", "voiture_inventee_xyz")
    assert result.get("has_history") is False


def test_with_history_includes_deep_fields():
    """Si has_history, must inclure deep_memory fields."""
    import memory
    memory.init_db()
    # On utilise monza/bmw_m4_gt3 qui existe dans la DB
    result = memory.deep_driver_memory("monza", "bmw_m4_gt3")
    if result.get("has_history"):
        # Si on a un historique, les champs deep doivent être présents
        for key in ("setup_changes_applied_here", "recurring_issues",
                    "pb_trend_over_time", "tyre_pressure_optimal_observed_psi"):
            assert key in result, f"Missing key {key}"
        assert result.get("has_deep_memory") is True


if __name__ == "__main__":
    test_function_exists()
    test_no_history_returns_proper()
    test_with_history_includes_deep_fields()
    print("ALL TESTS PASS")
