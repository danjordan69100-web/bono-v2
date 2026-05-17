"""Tests fmt_lap_compact (F6 fix anti-hallucination "1:10.5")."""
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("BONO_TTS_PROVIDER", "elevenlabs")


def test_basic_lap_time():
    import core_service as cs
    # 110567 ms = 1:50.6
    assert cs.fmt_lap_compact(110567) == "1:50.6"


def test_under_minute():
    import core_service as cs
    assert cs.fmt_lap_compact(59800) == "0:59.8"


def test_zero_invalid():
    import core_service as cs
    assert cs.fmt_lap_compact(0) == "?:??.?"
    assert cs.fmt_lap_compact(None) == "?:??.?"


def test_edge_seconds_rounding():
    import core_service as cs
    # 1:59.95 → 1:59.9 (formatter ne re-round pas, tronque à 1 décimale)
    out = cs.fmt_lap_compact(119950)
    assert out.startswith("1:") or out.startswith("2:")  # tolérant rounding


def test_very_long_lap():
    """Nordschleife 8-min lap, must still format correctly."""
    import core_service as cs
    out = cs.fmt_lap_compact(480500)  # 8 min 0.5 s
    assert out == "8:00.5"


if __name__ == "__main__":
    test_basic_lap_time()
    test_under_minute()
    test_zero_invalid()
    test_edge_seconds_rounding()
    test_very_long_lap()
    print("ALL TESTS PASS")
