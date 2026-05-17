"""Tests primary_mindset injection (Phase B2 brief V3 17/05)."""
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("BONO_TTS_PROVIDER", "elevenlabs")


def test_practice_detected():
    from prompts.persona_bono import _resolve_primary_mindset
    out = _resolve_primary_mindset("Track: monza\nCar: bmw_m4_gt3\nSession: PRACTICE (status=LIVE)")
    assert "PRACTICE" in out
    # Mindset enrichi B.4 17/05 nuit : check au moins un mot clé du nouveau bloc
    assert any(k in out.lower() for k in ("chrono", "setup", "essayer", "observer", "coaching"))


def test_race_detected():
    from prompts.persona_bono import _resolve_primary_mindset
    out = _resolve_primary_mindset("Track: spa\nCar: ferrari_488_gt3_evo\nSession: RACE (status=LIVE)")
    assert "RACE" in out
    # B.4 mindset : focus stratégique
    assert any(k in out.lower() for k in ("undercut", "stratège", "pit timing", "gap"))


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
    # B.4 enrichi : un des mots clés racing strategy doit être dedans
    assert any(k in prompt.lower() for k in ("undercut", "stratège", "pit", "gap"))


if __name__ == "__main__":
    test_practice_detected()
    test_race_detected()
    test_qualify_detected()
    test_unknown_returns_empty()
    test_build_system_prompt_includes_mindset()
    print("ALL TESTS PASS")
