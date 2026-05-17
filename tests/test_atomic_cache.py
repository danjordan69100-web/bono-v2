"""Tests atomic cache write (V2.3 D1 race condition fix)."""
import sys, os, time, tempfile, threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_atomic_write_basic():
    from playback_service import _atomic_write_bytes
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "test.mp3"
        _atomic_write_bytes(p, b"hello")
        assert p.read_bytes() == b"hello"


def test_atomic_write_overwrite():
    from playback_service import _atomic_write_bytes
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "test.mp3"
        _atomic_write_bytes(p, b"first")
        _atomic_write_bytes(p, b"second")
        assert p.read_bytes() == b"second"


def test_no_partial_files_remaining():
    """After atomic write, no .tmp files should remain."""
    from playback_service import _atomic_write_bytes
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "test.mp3"
        _atomic_write_bytes(p, b"x" * 10000)
        tmp_files = list(Path(td).glob("*.tmp*"))
        assert not tmp_files, f"Found leftover tmp files: {tmp_files}"


def test_cache_lock_per_key():
    from playback_service import _get_cache_lock
    l1 = _get_cache_lock("key_A")
    l2 = _get_cache_lock("key_A")
    l3 = _get_cache_lock("key_B")
    assert l1 is l2, "Same key should return same lock"
    assert l1 is not l3, "Different keys should have different locks"


def test_concurrent_atomic_write_no_corruption():
    """Multiple threads writing same key shouldn't corrupt the final file."""
    from playback_service import _atomic_write_bytes, _get_cache_lock
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "shared.mp3"
        # Each thread writes a different payload
        payloads = [b"a" * 1000, b"b" * 1000, b"c" * 1000]
        results = []

        def worker(payload):
            lock = _get_cache_lock(str(p))
            with lock:
                _atomic_write_bytes(p, payload)
                results.append(p.read_bytes())

        ths = [threading.Thread(target=worker, args=(payloads[i],)) for i in range(3)]
        for t in ths: t.start()
        for t in ths: t.join()

        # Final file should equal one of the payloads, fully intact (not partial)
        final = p.read_bytes()
        assert final in payloads, f"Final file content is corrupted, not matching any payload"
        assert len(final) == 1000, f"Wrong size: {len(final)}"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  {name} PASS")
            except AssertionError as e:
                print(f"  {name} FAIL: {e}")
