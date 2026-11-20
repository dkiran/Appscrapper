"""Logging handler that also writes log lines to the database so the dashboard can show them."""
import logging

from . import db


class DbLogHandler(logging.Handler):
    def __init__(self, target, keep=20000):
        super().__init__(logging.INFO)
        self.target, self.keep, self.conn, self.n = target, keep, None, 0

    def emit(self, record):
        try:
            if self.conn is None:
                self.conn = db.connect(self.target)
            self.conn.execute("INSERT INTO logs (ts, level, message) VALUES (?,?,?)",
                              (db.now(), record.levelname, record.getMessage()[:2000]))
            self.n += 1
            if self.n % 500 == 0:  # keep the table small
                self.conn.execute("DELETE FROM logs WHERE id < (SELECT MAX(id) FROM logs) - ?", (self.keep,))
            self.conn.commit()
        except Exception:
            self.conn = None  # reconnect next time; logging must never break a scrape

    def close(self):
        try:
            if self.conn:
                self.conn.close()
        finally:
            super().close()
