"""Tests fix 0.2 — runtime_state.llm_busy SQL flag cross-process."""
import sys
import os
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("BONO_TTS_PROVIDER", "elevenlabs")


def test_set_and_get():
    import memory
    memory.set_llm_busy(True)
    assert memory.get_llm_busy() is True
    memory.set_llm_busy(False)
    assert memory.get_llm_busy() is False


def test_stale_fail_open():
    """Si updated_at > 30s, retourne False (fail-open)."""
    import memory
    memory.set_llm_busy(True)
    # Force stale
    with memory._lock:
        c = memory.get_conn()
        c.execute("UPDATE runtime_state SET updated_at=? WHERE id=1", (time.time() - 60,))
        c.commit(); c.close()
    assert memory.get_llm_busy() is False, "Stale entry should fail-open"
    # Cleanup
    memory.set_llm_busy(False)


if __name__ == "__main__":
    test_set_and_get()
    test_stale_fail_open()
    print("ALL TESTS PASS")
