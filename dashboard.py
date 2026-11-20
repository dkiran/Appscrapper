"""Read-only monitoring page: totals and day-wise records saved. Counts only, no emails or personal data.

    python dashboard.py                 # http://127.0.0.1:8080  (uses DATABASE_URL, else config.json's SQLite file)
    HOST=0.0.0.0 PORT=8080 DASHBOARD_PASSWORD=secret python dashboard.py
"""
import base64
import hmac
import json
import os
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scraper import db
from scraper.run import db_target, load_config

DAYS = 30


def _n(conn, where=""):
    return conn.execute(f"SELECT COUNT(*) AS n FROM apps {where}").fetchone()["n"]


def collect_stats(conn, today=None):
    today = today or date.today()
    per_day = {r["day"]: (r["saved"], r["both"]) for r in conn.execute(
        "SELECT SUBSTR(fetched_at, 1, 10) AS day, COUNT(*) AS saved, "
        "SUM(CASE WHEN both_stores = 'Yes' THEN 1 ELSE 0 END) AS both FROM apps "
        "WHERE fetched_at IS NOT NULL AND fetched_at <> '' GROUP BY SUBSTR(fetched_at, 1, 10)")}
    days = []
    for i in range(DAYS - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        saved, both = per_day.get(d, (0, 0))
        days.append({"day": d, "saved": int(saved or 0), "both": int(both or 0)})
    last = conn.execute("SELECT MAX(fetched_at) AS t FROM apps").fetchone()["t"]
    return {
        "total": _n(conn),
        "both": _n(conn, "WHERE both_stores = 'Yes'"),
        "app_store_only": _n(conn, "WHERE COALESCE(url,'') = ''"),
        "play_only": _n(conn, "WHERE COALESCE(ios_url,'') = '' AND COALESCE(url,'') <> ''"),
        "unresolved": _n(conn, "WHERE ios_match = 'ios-only'"),
        "with_email": _n(conn, "WHERE COALESCE(support_email,'') <> ''"),
        "companies": conn.execute("SELECT COUNT(*) AS n FROM contacts").fetchone()["n"],
        "searches_done": conn.execute("SELECT COUNT(*) AS n FROM searches WHERE done_at <> ''").fetchone()["n"],
        "today": days[-1]["saved"],
        "last_saved": last,
        "days": days,
        "older": sum(v[0] for k, v in per_day.items() if k < days[0]["day"]),
    }


PAGE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html"), encoding="utf-8").read()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _authorised(self):
        pw = os.environ.get("DASHBOARD_PASSWORD")
        if not pw:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                given = base64.b64decode(h[6:]).decode().split(":", 1)[-1]
                return hmac.compare_digest(given.encode(), pw.encode())
            except Exception:
                return False
        return False

    def do_GET(self):
        if not self._authorised():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="scraper"')
            self.end_headers()
            return
        if self.path.split("?")[0] == "/api/stats":
            try:
                conn = db.connect(self.server.target)
                body, code = json.dumps(collect_stats(conn)).encode(), 200
                conn.close()
            except Exception as e:  # keep the page alive if the DB is briefly unavailable
                body, code = json.dumps({"error": str(e)}).encode(), 503
            ctype = "application/json"
        elif self.path.split("?")[0] in ("/", "/index.html"):
            body, code, ctype = PAGE.encode(), 200, "text/html; charset=utf-8"
        else:
            body, code, ctype = b"not found", 404, "text/plain"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def make_server(target, host="127.0.0.1", port=8080):
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.target = target
    return srv


if __name__ == "__main__":
    target = db_target(load_config(os.environ.get("CONFIG", "config.json")))
    host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8080"))
    print(f"dashboard on http://{host}:{port}" + ("  (password protected)" if os.environ.get("DASHBOARD_PASSWORD") else ""))
    make_server(target, host, port).serve_forever()
