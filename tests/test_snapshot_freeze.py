"""Tests contextvars-based turn snapshot freeze (V2.3 D1 fix)."""
import sys
import contextvars
import concurrent.futures as cf
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_no_turn_returns_live():
    from tools.racing_tools import set_snapshot_provider, _snap, clear_turn_snapshot
    clear_turn_snapshot()
    set_snapshot_provider(lambda: {"live": "X"})
    s = _snap()
    assert s.get("live") == "X"


def test_turn_freeze_main_thread():
    from tools.racing_tools import set_snapshot_provider, set_turn_snapshot, clear_turn_snapshot, _snap
    set_snapshot_provider(lambda: {"live": "X"})
    token = set_turn_snapshot({"frozen": "TURN1"})
    try:
        s = _snap()
        assert s.get("frozen") == "TURN1"
        # Live snapshot changes shouldn't affect frozen
        set_snapshot_provider(lambda: {"live": "Y"})
        assert _snap().get("frozen") == "TURN1"
    finally:
        clear_turn_snapshot(token)


def test_turn_freeze_propagates_to_threadpool():
    """CRITICAL : contextvars propagates to ThreadPoolExecutor unlike threading.local()."""
    from tools.racing_tools import set_snapshot_provider, set_turn_snapshot, clear_turn_snapshot, _snap
    set_snapshot_provider(lambda: {"live": "X"})
    token = set_turn_snapshot({"frozen": "TURN1"})
    try:
        ctx = contextvars.copy_context()
        results = []
        with cf.ThreadPoolExecutor(max_workers=4) as ex:
            futs = [ex.submit(ctx.run, _snap) for _ in range(4)]
            for f in cf.as_completed(futs):
                results.append(f.result())
        for r in results:
            assert r.get("frozen") == "TURN1", f"worker saw live snapshot instead of frozen: {r}"
    finally:
        clear_turn_snapshot(token)


def test_clear_restores_live():
    from tools.racing_tools import set_snapshot_provider, set_turn_snapshot, clear_turn_snapshot, _snap
    set_snapshot_provider(lambda: {"live": "Z"})
    token = set_turn_snapshot({"frozen": "T"})
    clear_turn_snapshot(token)
    assert _snap().get("live") == "Z"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  {name} PASS")
            except AssertionError as e:
                print(f"  {name} FAIL: {e}")
