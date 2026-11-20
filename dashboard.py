"""Read-only monitoring page: totals and day-wise records saved. Counts only, no emails or personal data.

    python dashboard.py                 # http://127.0.0.1:8080  (uses DATABASE_URL, else config.json's SQLite file)
    HOST=0.0.0.0 PORT=8080 DASHBOARD_PASSWORD=secret python dashboard.py
"""
import base64
import hmac
import json
import os
import time
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scraper import db
from scraper.categories import category_terms
from scraper.run import db_target, load_config, resolve_countries

DAYS = 30


def _n(conn, where=""):
    return conn.execute(f"SELECT COUNT(*) AS n FROM apps {where}").fetchone()["n"]


def collect_logs(conn, after=0, limit=300, issues=False):
    where = "level IN ('WARNING','ERROR','CRITICAL')" if issues else "1=1"
    if after:
        rows = conn.execute(f"SELECT id, ts, level, message FROM logs WHERE id > ? AND {where} ORDER BY id LIMIT ?", (after, limit)).fetchall()
    else:
        rows = list(reversed(conn.execute(f"SELECT id, ts, level, message FROM logs WHERE {where} ORDER BY id DESC LIMIT ?", (limit,)).fetchall()))
    return [{"id": r["id"], "ts": r["ts"], "level": r["level"], "message": r["message"]} for r in rows]


def coverage(conn, cfg):
    """Configured categories/countries and how many have at least one finished search."""
    cfg = cfg or {}
    terms = list(cfg.get("search_terms") or []) + category_terms(cfg.get("categories"))
    countries = resolve_countries(cfg.get("countries"))
    done = [(r["term"], r["country"]) for r in conn.execute("SELECT term, country FROM searches WHERE done_at <> ''")]
    done_terms = {t[4:] if t.startswith("ios:") else t for t, _ in done}
    done_countries = {c for _, c in done}
    return {"categories": {"done": len(done_terms & set(terms)), "total": len(terms)},
            "countries": {"done": len(done_countries & set(countries)), "total": len(countries)}}


def collect_stats(conn, today=None, cfg=None):
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
        "ios_total": _n(conn, "WHERE COALESCE(ios_url,'') <> ''"),
        "android_total": _n(conn, "WHERE COALESCE(url,'') <> ''"),
        **coverage(conn, cfg),
        "last_log": (conn.execute("SELECT ts FROM logs ORDER BY id DESC LIMIT 1").fetchone() or {"ts": None})["ts"],
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

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        if not self._authorised():
            if self.headers.get("Authorization"):
                time.sleep(1)  # slow down password guessing
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="scraper"')
            self.end_headers()
            return
        path, _, query = self.path.partition("?")
        q = dict(kv.split("=", 1) for kv in query.split("&") if "=" in kv)
        if path in ("/api/stats", "/api/logs"):
            try:
                conn = db.connect(self.server.target)
                if path == "/api/stats":
                    data = collect_stats(conn, cfg=self.server.cfg)
                else:
                    data = collect_logs(conn, int(q.get("after", 0) or 0), 300, q.get("issues") == "1")
                body, code = json.dumps(data).encode(), 200
                conn.close()
            except Exception as e:  # keep the page alive if the DB is briefly unavailable
                body, code = json.dumps({"error": str(e)}).encode(), 503
            ctype = "application/json"
        elif path in ("/", "/index.html"):
            body, code, ctype = PAGE.encode(), 200, "text/html; charset=utf-8"
        else:
            body, code, ctype = b"not found", 404, "text/plain"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head:
            self.wfile.write(body)


def make_server(target, host="127.0.0.1", port=8080, cfg=None):
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.target, srv.cfg = target, cfg or {}
    return srv


if __name__ == "__main__":
    cfg = load_config(os.environ.get("CONFIG", "config.json"))
    target = db_target(cfg)
    host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8080"))
    print(f"dashboard on http://{host}:{port}" + ("  (password protected)" if os.environ.get("DASHBOARD_PASSWORD") else ""))
    make_server(target, host, port, cfg).serve_forever()
