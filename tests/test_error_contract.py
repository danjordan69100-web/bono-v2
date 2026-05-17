"""Tests uniform error contract for all 7 racing tools (V2.2)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_err_helper():
    from tools.racing_tools import _err
    r = _err("test_code", "human msg", retryable=True, extra=42)
    assert r["ok"] is False
    assert r["error_code"] == "test_code"
    assert r["human_message"] == "human msg"
    assert r["retryable"] is True
    assert r["extra"] == 42


def test_ok_helper():
    from tools.racing_tools import _ok
    r = _ok({"foo": "bar"})
    assert r["ok"] is True
    assert r["foo"] == "bar"


def test_query_telemetry_no_shm():
    from tools.racing_tools import set_snapshot_provider, query_telemetry, clear_turn_snapshot
    clear_turn_snapshot()
    set_snapshot_provider(lambda: {})
    r = query_telemetry()
    assert r["ok"] is False
    assert "shm" in r["error_code"]
    assert "human_message" in r
    assert r["retryable"] is True


def test_query_telemetry_with_shm():
    from tools.racing_tools import set_snapshot_provider, query_telemetry, clear_turn_snapshot
    clear_turn_snapshot()
    set_snapshot_provider(lambda: {"shm_ok": True, "status": "LIVE", "speed_kmh": 200, "rpm": 7000})
    r = query_telemetry()
    assert r["ok"] is True
    assert r["speed_kmh"] == 200


def test_setup_update_unknown_field():
    from tools.racing_tools import bono_engineer_setup_update_acc
    r = bono_engineer_setup_update_acc(field="nope", delta=1)
    assert r["ok"] is False
    assert r["error_code"] == "unknown_field"


def test_setup_update_no_car_track():
    from tools.racing_tools import set_snapshot_provider, bono_engineer_setup_update_acc, clear_turn_snapshot
    clear_turn_snapshot()
    set_snapshot_provider(lambda: {})
    r = bono_engineer_setup_update_acc(field="brake_bias", delta=1)
    assert r["ok"] is False
    assert r["error_code"] == "no_car_or_track_in_shm"


def test_all_tools_have_consistent_contract():
    """Every tool when given empty SHM returns the uniform error contract."""
    from tools.racing_tools import (set_snapshot_provider, clear_turn_snapshot,
                                     query_telemetry, query_fuel_strategy, query_tire_state,
                                     query_opponents, query_weather, query_session_state)
    clear_turn_snapshot()
    set_snapshot_provider(lambda: {})
    tools = [
        query_telemetry, lambda: query_fuel_strategy(0), query_tire_state,
        query_opponents, query_weather, query_session_state,
    ]
    for tool in tools:
        r = tool()
        assert "ok" in r
        assert r["ok"] is False
        assert "error_code" in r
        assert "human_message" in r
        assert "retryable" in r


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  {name} PASS")
            except AssertionError as e:
                print(f"  {name} FAIL: {e}")
