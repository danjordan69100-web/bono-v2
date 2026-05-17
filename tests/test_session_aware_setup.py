"""Tests session-aware setup selection (Fix bugs A/B/C/D 17/05 nuit)."""
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("BONO_TTS_PROVIDER", "elevenlabs")


def test_match_race_picks_race_file():
    from tools.racing_tools import match_setup_file_for_session
    files = [
        "C:/x/Monza_BMWM4GT3_Wet.json",
        "C:/x/Monza_BMWM4GT3_Race.json",
        "C:/x/Monza_BMWM4GT3_Practice.json",
        "C:/x/Monza_BMWM4GT3_Qualif.json",
    ]
    assert match_setup_file_for_session(files, session_type="RACE", is_wet=False).endswith("_Race.json")


def test_match_quali_picks_qualif_file():
    from tools.racing_tools import match_setup_file_for_session
    files = [
        "C:/x/Spa_Ferrari488_Qualif.json",
        "C:/x/Spa_Ferrari488_Race.json",
        "C:/x/Spa_Ferrari488_Wet.json",
    ]
    assert match_setup_file_for_session(files, session_type="QUALIFY", is_wet=False).endswith("_Qualif.json")


def test_wet_overrides_session():
    from tools.racing_tools import match_setup_file_for_session
    files = [
        "C:/x/Imola_Porsche_Race.json",
        "C:/x/Imola_Porsche_Wet.json",
        "C:/x/Imola_Porsche_Practice.json",
    ]
    # is_wet=True doit prendre le Wet même si session_type=RACE
    assert match_setup_file_for_session(files, session_type="RACE", is_wet=True).endswith("_Wet.json")


def test_fallback_to_mtime_if_no_match():
    from tools.racing_tools import match_setup_file_for_session
    # Pas de session type connu, pas de match suffix → fallback mtime (le test passe le 1er fichier qui existe)
    files = ["C:/x/random_setup.json"]
    # Mock os.path.getmtime to avoid file access
    import os
    orig = os.path.getmtime
    os.path.getmtime = lambda p: 1.0
    try:
        result = match_setup_file_for_session(files, session_type="UNKNOWN", is_wet=False)
        assert result == "C:/x/random_setup.json"
    finally:
        os.path.getmtime = orig


def test_session_pressure_offset_quali():
    from tools.acc_knowledge import SESSION_PRESSURE_OFFSET_PSI
    assert SESSION_PRESSURE_OFFSET_PSI["QUALIFY"] < 0  # quali start cold lower
    assert SESSION_PRESSURE_OFFSET_PSI["RACE"] == 0.0


def test_wet_pressure_offset_significant():
    from tools.acc_knowledge import WET_PRESSURE_OFFSET_PSI
    assert WET_PRESSURE_OFFSET_PSI < -1.5  # at least -1.5 PSI for wet (sous-gonflage)


def test_get_session_tyre_target_psi():
    from tools.acc_knowledge import get_car_knowledge, get_session_tyre_target_psi
    kb = get_car_knowledge("bmw_m4_gt3")
    sec = get_session_tyre_target_psi(kb, "RACE", is_wet=False)
    wet = get_session_tyre_target_psi(kb, "RACE", is_wet=True)
    assert sec.get("FL", 0) > wet.get("FL", 0)
    assert wet["FL"] < 25.5
    # Quali < race < practice
    quali = get_session_tyre_target_psi(kb, "QUALIFY", is_wet=False)
    assert quali["FL"] < sec["FL"]


def test_get_session_bb_target_pct():
    from tools.acc_knowledge import get_car_knowledge, get_session_bb_target_pct
    kb = get_car_knowledge("ferrari_488_gt3_evo")
    race_bb = get_session_bb_target_pct(kb, None, "RACE", is_wet=False)
    quali_bb = get_session_bb_target_pct(kb, None, "QUALIFY", is_wet=False)
    wet_bb = get_session_bb_target_pct(kb, None, "RACE", is_wet=True)
    assert quali_bb > race_bb  # +1% en quali
    assert wet_bb < race_bb    # transfer arrière en wet


if __name__ == "__main__":
    test_match_race_picks_race_file()
    test_match_quali_picks_qualif_file()
    test_wet_overrides_session()
    test_fallback_to_mtime_if_no_match()
    test_session_pressure_offset_quali()
    test_wet_pressure_offset_significant()
    test_get_session_tyre_target_psi()
    test_get_session_bb_target_pct()
    print("ALL TESTS PASS")
