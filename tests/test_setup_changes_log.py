"""Tests setup_changes table + log_setup_change (Phase B1 brief V3 17/05)."""
import sys
import os
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("BONO_TTS_PROVIDER", "elevenlabs")


def test_setup_changes_table_exists():
    import memory
    memory.init_db()
    db_path = memory.DB_DIR / "bono.db"
    conn = sqlite3.connect(db_path)
    c = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='setup_changes'")
    row = c.fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "setup_changes"


def test_setup_changes_columns():
    import memory
    memory.init_db()
    db_path = memory.DB_DIR / "bono.db"
    conn = sqlite3.connect(db_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(setup_changes)")]
    conn.close()
    for required in ("created_at", "field", "old_value", "new_value", "source", "success", "reverted_at"):
        assert required in cols, f"Column {required} missing"


def test_log_setup_change_writes():
    import memory
    memory.init_db()
    before_count = 0
    db_path = memory.DB_DIR / "bono.db"
    conn = sqlite3.connect(db_path)
    before_count = conn.execute("SELECT COUNT(*) FROM setup_changes").fetchone()[0]
    conn.close()
    memory.log_setup_change(session_id=None, turn_id=None, track="monza", car="bmw_m4_gt3",
                             setup_file="test.json", field="brakeBias",
                             old_value=56, new_value=57, source="test", success=True)
    conn = sqlite3.connect(db_path)
    after = conn.execute("SELECT COUNT(*) FROM setup_changes").fetchone()[0]
    # Cleanup test row
    conn.execute("DELETE FROM setup_changes WHERE source='test' AND field='brakeBias'")
    conn.commit()
    conn.close()
    assert after == before_count + 1


if __name__ == "__main__":
    test_setup_changes_table_exists()
    test_setup_changes_columns()
    test_log_setup_change_writes()
    print("ALL TESTS PASS")
