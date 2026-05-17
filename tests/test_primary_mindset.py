"""Tests primary_mindset injection (Phase B2 brief V3 17/05)."""
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("BONO_TTS_PROVIDER", "elevenlabs")


def test_practice_detected():
    from prompts.persona_bono import _resolve_primary_mindset
    out = _resolve_primary_mindset("Track: monza\nCar: bmw_m4_gt3\nSession: PRACTICE (status=LIVE)")
    assert "PRACTICE" in out and "exploration" in out.lower()


def test_race_detected():
    from prompts.persona_bono import _resolve_primary_mindset
    out = _resolve_primary_mindset("Track: spa\nCar: ferrari_488_gt3_evo\nSession: RACE (status=LIVE)")
    assert "RACE" in out and "PROACTIVE" in out


def test_qualify_detected():
    from prompts.persona_bono import _resolve_primary_mindset
    out = _resolve_primary_mindset("Session: QUALIFY (status=LIVE)")
    assert "QUALIFY" in out


def test_unknown_returns_empty():
    from prompts.persona_bono import _resolve_primary_mindset
    assert _resolve_primary_mindset("") == ""
    assert _resolve_primary_mindset("ACC SHM not live yet.") == ""


def test_build_system_prompt_includes_mindset():
    from prompts.persona_bono import build_system_prompt
    prompt = build_system_prompt("Track: monza\nCar: bmw_m4_gt3\nSession: RACE (status=LIVE)", tools_schemas=[])
    assert "[PRIMARY MINDSET: RACE]" in prompt
    assert "PROACTIVE" in prompt


if __name__ == "__main__":
    test_practice_detected()
    test_race_detected()
    test_qualify_detected()
    test_unknown_returns_empty()
    test_build_system_prompt_includes_mindset()
    print("ALL TESTS PASS")
