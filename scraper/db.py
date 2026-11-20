"""Storage: PostgreSQL when given a postgres:// URL, otherwise a local SQLite file. SQL uses ? placeholders."""
import csv
import os
import sqlite3
from datetime import datetime, timezone

CSV_COLUMNS = ["Email", "App", "Company", "Website", "Category",
               "Play Store URL", "Developer email", "Developer address", "Country",
               "App Store URL", "iOS match", "Both Stores"]
CONTACT_COLUMNS = ["Email", "Developer email", "Company", "Website", "Country", "Apps", "App count",
                   "Play Store URL", "App Store URL", "Both Stores"]

APP_COLUMNS = {"app": "TEXT", "company": "TEXT", "website": "TEXT", "category": "TEXT", "url": "TEXT",
               "support_email": "TEXT", "developer_email": "TEXT", "developer_address": "TEXT",
               "country": "TEXT", "crawl_region": "TEXT", "fetched_at": "TEXT",
               "ios_url": "TEXT", "ios_match": "TEXT", "both_stores": "TEXT"}
SCHEMA = [
    "CREATE TABLE IF NOT EXISTS apps (package TEXT PRIMARY KEY, " + ", ".join(f"{k} {v}" for k, v in APP_COLUMNS.items()) + ")",
    "CREATE TABLE IF NOT EXISTS searches (term TEXT, country TEXT, done_at TEXT, attempts INTEGER DEFAULT 0, PRIMARY KEY (term, country))",
    "CREATE TABLE IF NOT EXISTS contacts (email TEXT, developer_email TEXT, company TEXT, website TEXT, country TEXT, "
    "apps TEXT, app_count INTEGER, play_url TEXT, ios_url TEXT, both_stores TEXT)",
    "CREATE INDEX IF NOT EXISTS apps_both_idx ON apps (both_stores)",
]


class Conn:
    def __init__(self, raw, pg):
        self.raw, self.pg = raw, pg

    def execute(self, sql, params=()):
        if self.pg:
            return self.raw.execute(sql.replace("?", "%s"), tuple(params) or None)
        return self.raw.execute(sql, params)

    def commit(self):
        self.raw.commit()

    def close(self):
        self.raw.close()

    def columns(self, table):
        if self.pg:
            rows = self.execute("SELECT column_name AS name FROM information_schema.columns "
                                "WHERE table_name = ? AND table_schema = current_schema()", (table,))
        else:
            rows = self.execute(f"PRAGMA table_info({table})")
        return {r["name"] for r in rows}


def is_postgres(target):
    return str(target).startswith(("postgres://", "postgresql://"))


def connect(target):
    """target: postgres:// URL or a SQLite file path."""
    if is_postgres(target):
        import psycopg
        from psycopg.rows import dict_row
        conn = Conn(psycopg.connect(target, row_factory=dict_row), True)
    else:
        d = os.path.dirname(target)
        if d:
            os.makedirs(d, exist_ok=True)
        raw = sqlite3.connect(target)
        raw.row_factory = sqlite3.Row
        conn = Conn(raw, False)
    for stmt in SCHEMA[:3]:
        conn.execute(stmt)
    cols = conn.columns("apps")
    if "support_email" not in cols:  # very old DB: developer_email used to hold the support email
        conn.execute("ALTER TABLE apps ADD COLUMN support_email TEXT")
        conn.execute("UPDATE apps SET support_email = developer_email, developer_email = ''")
    for col, typ in APP_COLUMNS.items():
        if col not in cols:
            conn.execute(f"ALTER TABLE apps ADD COLUMN {col} {typ}")
    if "attempts" not in conn.columns("searches"):
        conn.execute("ALTER TABLE searches ADD COLUMN attempts INTEGER DEFAULT 0")
    conn.execute("UPDATE apps SET both_stores = CASE WHEN COALESCE(url,'') <> '' AND COALESCE(ios_url,'') <> '' "
                 "THEN 'Yes' ELSE 'No' END WHERE both_stores IS NULL")
    conn.execute(SCHEMA[3])
    conn.commit()
    return conn


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _both(url, ios_url):
    return "Yes" if (url and ios_url) else "No"


def upsert(conn, row):
    row = dict(row)
    if "ios_url" in row or "url" in row:
        row["both_stores"] = _both(row.get("url"), row.get("ios_url"))
    cols = list(row)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "package")
    conn.execute(
        f"INSERT INTO apps ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
        f"ON CONFLICT(package) DO UPDATE SET {updates}", [row[c] for c in cols])
    conn.commit()


def known_packages(conn, refresh_after_days=None):
    """Packages that must not be fetched again. With refresh_after_days unset, that is every stored app."""
    if not refresh_after_days:
        return {r["package"] for r in conn.execute("SELECT package FROM apps")}
    cutoff = datetime.now(timezone.utc).timestamp() - refresh_after_days * 86400
    out = set()
    for r in conn.execute("SELECT package, fetched_at FROM apps"):
        try:
            if datetime.fromisoformat(r["fetched_at"]).timestamp() >= cutoff:
                out.add(r["package"])
        except (TypeError, ValueError):
            pass
    return out


def searches_state(conn):
    """{(term, country): done_at ISO string, or '' if never completed}"""
    return {(r["term"], r["country"]): r["done_at"] or ""
            for r in conn.execute("SELECT term, country, done_at FROM searches")}


def _put_search(conn, term, country, done_at, attempts):
    conn.execute("INSERT INTO searches (term, country, done_at, attempts) VALUES (?,?,?,?) "
                 "ON CONFLICT(term, country) DO UPDATE SET done_at=excluded.done_at, attempts=excluded.attempts",
                 (term, country, done_at, attempts))
    conn.commit()


def mark_search_done(conn, term, country):
    _put_search(conn, term, country, now(), 0)


def search_failed(conn, term, country, max_attempts=3):
    """Count a failed search; after max_attempts it is treated as done (retried after the repeat window)
    so a country Play doesn't serve can't be retried every run forever."""
    r = conn.execute("SELECT attempts FROM searches WHERE term=? AND country=?", (term, country)).fetchone()
    attempts = ((r["attempts"] or 0) if r else 0) + 1
    if attempts >= max_attempts:
        mark_search_done(conn, term, country)
        return True
    _put_search(conn, term, country, "", attempts)
    return False


def pending_ios(conn, limit):
    """Stored apps whose App Store check never ran or failed."""
    return conn.execute("SELECT package, app, company, website, crawl_region FROM apps "
                        "WHERE ios_match IS NULL OR ios_match = 'unchecked' LIMIT ?", (limit,)).fetchall()


def set_ios(conn, package, url, match):
    conn.execute("UPDATE apps SET ios_url=?, ios_match=?, both_stores=CASE WHEN COALESCE(url,'') <> '' "
                 "AND COALESCE(?,'') <> '' THEN 'Yes' ELSE 'No' END WHERE package=?", (url, match, url, package))
    conn.commit()


def pending_resolve(conn, limit):
    """App Store apps with no same-ID Android app, those with a website first (best for confirming a match)."""
    return conn.execute("SELECT package, app, company, website, ios_url, crawl_region FROM apps "
                        "WHERE ios_match = 'ios-only' ORDER BY CASE WHEN COALESCE(website,'') <> '' THEN 0 ELSE 1 END, "
                        "fetched_at LIMIT ?", (limit,)).fetchall()


def set_match(conn, package, match):
    conn.execute("UPDATE apps SET ios_match=? WHERE package=?", (match, package))
    conn.commit()


def delete_app(conn, package):
    conn.execute("DELETE FROM apps WHERE package=?", (package,))
    conn.commit()


def refresh_contacts(conn, require_ios=False, countries=None):
    """Rebuild the `contacts` table: one row per company + support email, so a campaign doesn't mail
    the same person once per app."""
    want = {c.lower() for c in countries or []}
    groups = {}
    where = "WHERE COALESCE(ios_url,'') <> ''" if require_ios else ""
    for r in conn.execute(f"SELECT * FROM apps {where} ORDER BY app"):
        if want and (r["country"] or "").lower() not in want:
            continue
        key = ((r["support_email"] or r["developer_email"] or "").split(",")[0].strip().lower(),
               (r["company"] or "").lower())
        if key[0]:
            groups.setdefault(key, []).append(r)
    conn.execute("DELETE FROM contacts")
    for rows in groups.values():
        both = [x for x in rows if x["both_stores"] == "Yes"]
        r = both[0] if both else rows[0]  # prefer an app that is on both stores as the sample
        ios = next((x["ios_url"] for x in rows if x["ios_url"]), "")
        conn.execute("INSERT INTO contacts VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (r["support_email"], r["developer_email"], r["company"], r["website"], r["country"],
                      "; ".join(x["app"] for x in rows[:10]), len(rows), r["url"] or "", ios,
                      "Yes" if both else "No"))
    conn.commit()
    return len(groups)


def _write_csv(path, header, rows):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return len(rows)


def export_csv(conn, path):
    """Optional: dump the apps table to CSV (the database is the main store)."""
    rows = conn.execute("SELECT * FROM apps ORDER BY company, app").fetchall()
    return _write_csv(path, CSV_COLUMNS, [
        [r["support_email"], r["app"], r["company"], r["website"], r["category"], r["url"], r["developer_email"],
         r["developer_address"], r["country"], r["ios_url"] or "", r["ios_match"] or "", r["both_stores"] or "No"]
        for r in rows])


def export_contacts(conn, path):
    rows = conn.execute("SELECT * FROM contacts ORDER BY company").fetchall()
    return _write_csv(path, CONTACT_COLUMNS, [
        [r["email"], r["developer_email"], r["company"], r["website"], r["country"], r["apps"], r["app_count"],
         r["play_url"], r["ios_url"], r["both_stores"]] for r in rows])


def import_sqlite(src_path, conn):
    """Copy apps and finished searches from an old SQLite file into the current database."""
    src = sqlite3.connect(src_path)
    src.row_factory = sqlite3.Row
    have = conn.columns("apps")
    n = 0
    for r in src.execute("SELECT * FROM apps"):
        row = {k: r[k] for k in r.keys() if k in have}
        if row.get("package"):
            upsert(conn, row)
            n += 1
    try:
        for r in src.execute("SELECT term, country, done_at FROM searches"):
            if r["done_at"]:
                _put_search(conn, r["term"], r["country"], r["done_at"], 0)
    except sqlite3.OperationalError:
        pass
    src.close()
    return n
