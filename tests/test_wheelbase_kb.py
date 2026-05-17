"""Tests fix 0.4 — wheelbase dynamique car_kb."""
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("BONO_TTS_PROVIDER", "elevenlabs")


def test_bmw_m4_wheelbase():
    from tools.acc_knowledge import get_wheelbase_m
    assert get_wheelbase_m("bmw_m4_gt3") == 2.85


def test_porsche_992_wheelbase():
    from tools.acc_knowledge import get_wheelbase_m
    assert get_wheelbase_m("porsche_992_gt3_r") == 2.46


def test_unknown_car_fallback():
    from tools.acc_knowledge import get_wheelbase_m, WHEELBASE_DEFAULT_M
    assert get_wheelbase_m("unknown_car") == WHEELBASE_DEFAULT_M


def test_case_insensitive():
    from tools.acc_knowledge import get_wheelbase_m
    assert get_wheelbase_m("BMW_M4_GT3") == 2.85
    assert get_wheelbase_m("BMW_m4_GT3") == 2.85


def test_wheelbases_count():
    from tools.acc_knowledge import WHEELBASES_M
    assert len(WHEELBASES_M) >= 14


def test_wheelbase_range_sane():
    """Tous les GT3 wheelbases sont dans 2.4 - 2.9m."""
    from tools.acc_knowledge import WHEELBASES_M
    for car, wb in WHEELBASES_M.items():
        assert 2.4 <= wb <= 2.9, f"{car}: {wb}m hors range plausible GT3"


if __name__ == "__main__":
    test_bmw_m4_wheelbase()
    test_porsche_992_wheelbase()
    test_unknown_car_fallback()
    test_case_insensitive()
    test_wheelbases_count()
    test_wheelbase_range_sane()
    print("ALL TESTS PASS")
