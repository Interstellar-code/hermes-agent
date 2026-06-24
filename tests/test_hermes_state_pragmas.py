from hermes_state import SessionDB


def test_sessiondb_sets_busy_timeout_and_wal_autocheckpoint(_isolate_hermes_home):
    db = SessionDB()
    try:
        wal_autocheckpoint = db._conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
        busy_timeout = db._conn.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        db.close()

    assert wal_autocheckpoint == 100
    assert busy_timeout == 5000
