import csv
import os
import sqlite3
from datetime import datetime, timezone

CSV_COLUMNS = ["Email", "App", "Company", "Website", "Category",
               "Play Store URL", "Developer email", "Developer address", "Country",
               "App Store URL", "iOS match", "Both Stores"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS apps (
  package TEXT PRIMARY KEY,
  app TEXT, company TEXT, website TEXT, category TEXT, url TEXT,
  support_email TEXT, developer_email TEXT, developer_address TEXT, country TEXT,
  crawl_region TEXT, fetched_at TEXT, ios_url TEXT, ios_match TEXT
);
CREATE TABLE IF NOT EXISTS searches (
  term TEXT, country TEXT, done_at TEXT, attempts INTEGER DEFAULT 0, PRIMARY KEY (term, country)
);
"""


def connect(path):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(apps)")}
    if "support_email" not in cols:  # older DB: developer_email used to hold the support email
        conn.execute("ALTER TABLE apps ADD COLUMN support_email TEXT")
        conn.execute("UPDATE apps SET support_email = developer_email, developer_email = ''")
        conn.commit()
    scols = {r["name"] for r in conn.execute("PRAGMA table_info(searches)")}
    if "attempts" not in scols:
        conn.execute("ALTER TABLE searches ADD COLUMN attempts INTEGER DEFAULT 0")
    for col in ("ios_url", "ios_match"):
        if col not in cols:
            conn.execute(f"ALTER TABLE apps ADD COLUMN {col} TEXT")
    conn.commit()
    return conn


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def upsert(conn, row):
    cols = list(row)
    placeholders = ",".join("?" * len(cols))
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "package")
    conn.execute(
        f"INSERT INTO apps ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(package) DO UPDATE SET {updates}",
        [row[c] for c in cols])
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
    return {(r["term"], r["country"]): r["done_at"] or "" for r in conn.execute("SELECT term, country, done_at FROM searches")}


def mark_search_done(conn, term, country):
    conn.execute("INSERT OR REPLACE INTO searches VALUES (?,?,?,0)", (term, country, now()))
    conn.commit()


def search_failed(conn, term, country, max_attempts=3):
    """Count a failed search; after max_attempts it is treated as done (retried after the repeat window)
    so a country Play doesn't serve can't be retried every run forever."""
    r = conn.execute("SELECT attempts FROM searches WHERE term=? AND country=?", (term, country)).fetchone()
    attempts = ((r["attempts"] or 0) if r else 0) + 1
    if attempts >= max_attempts:
        mark_search_done(conn, term, country)
        return True
    conn.execute("INSERT OR REPLACE INTO searches VALUES (?,?,'',?)", (term, country, attempts))
    conn.commit()
    return False


def pending_ios(conn, limit):
    """Stored apps whose App Store check never ran or failed."""
    return conn.execute("SELECT package, app, company, website, crawl_region FROM apps "
                        "WHERE ios_match IS NULL OR ios_match = 'unchecked' LIMIT ?", (limit,)).fetchall()


def set_ios(conn, package, url, match):
    conn.execute("UPDATE apps SET ios_url=?, ios_match=? WHERE package=?", (url, match, package))
    conn.commit()


def export_csv(conn, path):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    rows = conn.execute("SELECT * FROM apps ORDER BY company, app").fetchall()
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        for r in rows:
            # Email = App support email; Developer email = other emails found in developer/about text.
            w.writerow([r["support_email"], r["app"], r["company"], r["website"], r["category"],
                        r["url"], r["developer_email"], r["developer_address"], r["country"], r["ios_url"] or "", r["ios_match"] or "", "Yes" if (r["url"] and r["ios_url"]) else "No"])
    return len(rows)


CONTACT_COLUMNS = ["Email", "Developer email", "Company", "Website", "Country", "Apps", "App count", "Play Store URL", "App Store URL", "Both Stores"]


def export_contacts(conn, path, require_ios=False, countries=None):
    """One row per company+support email, so a campaign doesn't mail the same person once per app."""
    where = "WHERE ios_url IS NOT NULL AND ios_url != ''" if require_ios else ""
    groups = {}
    for r in conn.execute(f"SELECT * FROM apps {where} ORDER BY app"):
        if countries and (r["country"] or "").lower() not in {c.lower() for c in countries}:
            continue
        key = ((r["support_email"] or r["developer_email"] or "").split(",")[0].strip().lower(),
               (r["company"] or "").lower())
        if not key[0]:
            continue
        groups.setdefault(key, []).append(r)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CONTACT_COLUMNS)
        for rows in groups.values():
            both = [x for x in rows if x["url"] and x["ios_url"]]
            r = both[0] if both else rows[0]  # prefer an app that is on both stores as the sample
            ios = next((x["ios_url"] for x in rows if x["ios_url"]), "")
            w.writerow([r["support_email"], r["developer_email"], r["company"], r["website"], r["country"],
                        "; ".join(x["app"] for x in rows[:10]), len(rows), r["url"] or "", ios,
                        "Yes" if both else "No"])
    return len(groups)
