"""Tests PSI conversion ACC (Phase C prereq brief V3 17/05)."""
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("BONO_TTS_PROVIDER", "elevenlabs")


def test_click_to_psi_cold():
    from tools.acc_knowledge import click_to_psi_cold
    assert click_to_psi_cold(0) == 20.3
    assert click_to_psi_cold(45) == 24.8
    assert click_to_psi_cold(50) == 25.3


def test_click_to_psi_hot():
    from tools.acc_knowledge import click_to_psi_hot
    # delta default 2.0
    assert click_to_psi_hot(45) == 26.8
    assert click_to_psi_hot(50) == 27.3
    # custom delta
    assert click_to_psi_hot(45, cold_to_hot_delta=1.5) == 26.3


def test_psi_target_to_click():
    from tools.acc_knowledge import psi_hot_target_to_click
    assert psi_hot_target_to_click(26.8) == 45
    assert psi_hot_target_to_click(27.0) == 47
    assert psi_hot_target_to_click(26.6) == 43


def test_reversibility():
    """psi_target → click → psi_hot doit revenir au target."""
    from tools.acc_knowledge import psi_hot_target_to_click, click_to_psi_hot
    for target in (26.6, 26.8, 27.0, 27.2):
        click = psi_hot_target_to_click(target)
        back = click_to_psi_hot(click)
        assert abs(back - target) < 0.15, f"Target {target} → click {click} → back {back}"


def test_realistic_bounds():
    """Setup ACC range : click 0-100. PSI hot reste plausible 20-30 PSI."""
    from tools.acc_knowledge import click_to_psi_hot
    assert 22 < click_to_psi_hot(0) < 23
    assert 31 < click_to_psi_hot(100) < 33


if __name__ == "__main__":
    test_click_to_psi_cold()
    test_click_to_psi_hot()
    test_psi_target_to_click()
    test_reversibility()
    test_realistic_bounds()
    print("ALL TESTS PASS")
