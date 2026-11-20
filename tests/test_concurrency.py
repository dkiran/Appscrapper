"""Many connections opening (as dashboard requests do) while another writes must never deadlock."""
import os
import threading

import pytest

from scraper import db

PG = os.environ.get("TEST_DATABASE_URL")


@pytest.mark.skipif(not PG, reason="needs PostgreSQL (set TEST_DATABASE_URL)")
def test_concurrent_connect_and_write_do_not_deadlock():
    import psycopg
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS apps, searches, contacts, logs")
    db.connect(PG).close()
    errors = []

    def opener():
        try:
            for _ in range(25):
                conn = db.connect(PG)
                conn.execute("SELECT COUNT(*) AS n FROM apps").fetchone()
                conn.close()
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    def writer():
        try:
            conn = db.connect(PG)
            for i in range(300):
                db.upsert(conn, {"package": f"p{i}", "app": "x", "url": "u", "fetched_at": db.now()})
                conn.execute("INSERT INTO logs (ts, level, message) VALUES (?,?,?)", (db.now(), "INFO", "m"))
                conn.commit()
            conn.close()
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=opener) for _ in range(8)] + [threading.Thread(target=writer)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
