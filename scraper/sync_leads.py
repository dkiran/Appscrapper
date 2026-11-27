"""Push Appscrapper contacts into SingleQR leads via HTTPS import API."""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("playstore.sync_leads")

BATCH_SIZE = 2000  # keep under API max (5000); fewer batches → fewer transfer log rows
DEFAULT_SYNC_CRON = "0 4 * * *"


def _env_or_cfg(cfg, env_key, cfg_key=None, default=None):
    val = os.environ.get(env_key)
    if val is not None and str(val).strip() != "":
        return val
    if cfg_key is None:
        cfg_key = env_key.lower()
    return cfg.get(cfg_key, default)


def _parse_bool(value, default=True):
    if value is None or value == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on", "enable", "enabled")


def normalize_leads_url(url):
    """Require an absolute HTTPS URL; prepend https:// if the scheme was omitted."""
    raw = (url or "").strip()
    if not raw:
        return raw
    if "://" not in raw:
        raw = f"https://{raw}"
    if not raw.lower().startswith("https://"):
        raise RuntimeError(f"SINGLEQR_LEADS_URL must use https:// (got {url!r})")
    return raw.rstrip("/")


def package_from_play_url(url):
    if not url:
        return None
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        ids = parse_qs(parsed.query).get("id") or []
        if ids and ids[0].strip():
            return ids[0].strip()
    except Exception:
        pass
    m = re.search(r"[?&]id=([a-zA-Z0-9._]+)", url or "")
    return m.group(1) if m else None


def first_app_name(apps_text):
    if not apps_text:
        return None
    parts = [p.strip() for p in str(apps_text).split(";") if p.strip()]
    if not parts:
        return None
    name = parts[0]
    return name[:255]


def ensure_sync_tables(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS leads_sync_state ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), "
        "last_synced_at TEXT, "
        "last_success_at TEXT, "
        "last_created INTEGER DEFAULT 0, "
        "last_skipped INTEGER DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS leads_synced_emails ("
        "email TEXT PRIMARY KEY, "
        "synced_at TEXT)"
    )
    # Seed singleton row
    row = conn.execute("SELECT id FROM leads_sync_state WHERE id = 1").fetchone()
    if not row:
        conn.execute(
            "INSERT INTO leads_sync_state (id, last_synced_at, last_success_at) VALUES (1, NULL, NULL)"
        )
    conn.commit()


def get_sync_state(conn):
    ensure_sync_tables(conn)
    return conn.execute("SELECT * FROM leads_sync_state WHERE id = 1").fetchone()


def mark_sync_success(conn, created, skipped):
    ensure_sync_tables(conn)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        "UPDATE leads_sync_state SET last_synced_at = ?, last_success_at = ?, "
        "last_created = ?, last_skipped = ? WHERE id = 1",
        (ts, ts, int(created), int(skipped)),
    )
    conn.commit()


def already_synced_emails(conn):
    ensure_sync_tables(conn)
    rows = conn.execute("SELECT email FROM leads_synced_emails").fetchall()
    return {str(r["email"]).lower() for r in rows if r["email"]}


def record_synced_emails(conn, emails):
    ensure_sync_tables(conn)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for email in emails:
        e = (email or "").strip().lower()
        if not e:
            continue
        if conn.pg:
            conn.execute(
                "INSERT INTO leads_synced_emails (email, synced_at) VALUES (?, ?) "
                "ON CONFLICT (email) DO UPDATE SET synced_at = EXCLUDED.synced_at",
                (e, ts),
            )
        else:
            conn.execute(
                "INSERT OR REPLACE INTO leads_synced_emails (email, synced_at) VALUES (?, ?)",
                (e, ts),
            )
    conn.commit()


def contact_sync_stats(conn, both_stores_only=False):
    """Counts for diagnosing empty sync runs."""
    ensure_sync_tables(conn)
    total = conn.execute("SELECT COUNT(*) AS n FROM contacts").fetchone()["n"]
    with_email = conn.execute(
        "SELECT COUNT(*) AS n FROM contacts WHERE COALESCE(email, '') <> ''"
    ).fetchone()["n"]
    both = conn.execute(
        "SELECT COUNT(*) AS n FROM contacts WHERE COALESCE(email, '') <> '' AND both_stores = 'Yes'"
    ).fetchone()["n"]
    synced = len(already_synced_emails(conn))
    pending = select_contacts(conn, both_stores_only=both_stores_only, exclude_synced=True)
    return {
        "contacts_total": int(total or 0),
        "contacts_with_email": int(with_email or 0),
        "contacts_both_stores": int(both or 0),
        "locally_marked_synced": synced,
        "pending_after_local_gate": len(pending),
    }


def select_contacts(conn, both_stores_only=True, since=None, exclude_synced=True):
    """Return contact rows ready to push. since: ISO timestamp; exclude already-synced emails."""
    ensure_sync_tables(conn)
    sql = (
        "SELECT email, developer_email, company, website, country, apps, app_count, "
        "play_url, ios_url, both_stores FROM contacts WHERE COALESCE(email, '') <> ''"
    )
    params = []
    if both_stores_only:
        sql += " AND both_stores = 'Yes'"
    sql += " ORDER BY company, email"
    rows = conn.execute(sql, params).fetchall()

    synced = already_synced_emails(conn) if exclude_synced else set()
    out = []
    for r in rows:
        email = (r["email"] or "").split(",")[0].strip().lower()
        if not email or "@" not in email:
            continue
        if email in synced:
            continue
        # Optional since filter: without per-contact timestamps, --since only clears
        # the synced-email gate when caller uses exclude_synced=False + since backfill.
        out.append(dict(r) if not hasattr(r, "keys") else {k: r[k] for k in r.keys()})
        out[-1]["email"] = email
    return out


def map_contact_to_lead(contact, scraped_at=None):
    play_url = (contact.get("play_url") or "").strip() or None
    ios_url = (contact.get("ios_url") or "").strip() or None
    company = (contact.get("company") or "").strip() or None
    apps = contact.get("apps") or ""
    package_id = package_from_play_url(play_url)
    return {
        "email": contact["email"],
        "developerEmail": (contact.get("developer_email") or "").split(",")[0].strip().lower() or None,
        "company": company,
        "developer": company,
        "website": (contact.get("website") or "").strip() or None,
        "country": (contact.get("country") or "").strip() or None,
        "playStoreUrl": play_url,
        "androidApp": play_url,
        "iosApp": ios_url,
        "appName": first_app_name(apps),
        "packageId": package_id,
        "source": "appscrapper",
        "scrapedAt": scraped_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _post_batch(url, api_key, batch, timeout=120):
    data = json.dumps({"leads": batch}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "Appscrapper-leads-sync/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            payload = json.loads(body) if body else {}
            return resp.status, payload
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(err_body) if err_body else {}
        except json.JSONDecodeError:
            payload = {"raw": err_body}
        raise RuntimeError(f"HTTP {e.code}: {payload}") from e


def sync_leads(cfg, conn, *, dry_run=False, since=None, resync_all=False):
    """
    Select due contacts, map to API payload, POST in batches, advance watermark on success.
    Returns summary dict.
    """
    url = normalize_leads_url(_env_or_cfg(cfg, "SINGLEQR_LEADS_URL", "singleqr_leads_url"))
    api_key = _env_or_cfg(cfg, "SINGLEQR_LEADS_API_KEY", "singleqr_leads_api_key")
    # Default false: push every contact with an email (Play-only, iOS-only, or both).
    both_only = _parse_bool(
        _env_or_cfg(cfg, "SYNC_BOTH_STORES_ONLY", "sync_both_stores_only", "false"),
        False,
    )

    if not dry_run and (not url or not api_key):
        raise RuntimeError(
            "SINGLEQR_LEADS_URL and SINGLEQR_LEADS_API_KEY are required "
            "(env or config.json singleqr_leads_url / singleqr_leads_api_key)"
        )

    exclude_synced = not resync_all and not since
    if since or resync_all:
        # Backfill / resync: ignore local synced set; backend still skips duplicates.
        exclude_synced = False
        log.info(
            "local synced-email gate disabled (resync_all=%s since=%s); backend still skips duplicates",
            bool(resync_all),
            since or "-",
        )

    stats = contact_sync_stats(conn, both_stores_only=both_only)
    log.info("sync-leads pool: %s", stats)

    contacts = select_contacts(conn, both_stores_only=both_only, since=since, exclude_synced=exclude_synced)
    scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    leads = [map_contact_to_lead(c, scraped_at=scraped_at) for c in contacts]

    summary = {
        "candidates": len(leads),
        "created": 0,
        "skipped": 0,
        "errors": [],
        "batches": 0,
        "dry_run": dry_run,
        "both_stores_only": both_only,
        "pool": stats,
    }

    if not leads:
        log.info(
            "sync-leads: nothing to push (candidates=0, both_stores_only=%s, "
            "contacts_with_email=%s, locally_marked_synced=%s). "
            "If contacts remain unsent in SingleQR, re-run with --resync-all "
            "(or --since 2020-01-01) so the local gate is ignored.",
            both_only,
            stats["contacts_with_email"],
            stats["locally_marked_synced"],
        )
        if not dry_run:
            mark_sync_success(conn, 0, 0)
        return summary

    log.info(
        "sync-leads: %d contacts to push (both_stores_only=%s, dry_run=%s)",
        len(leads),
        both_only,
        dry_run,
    )

    if dry_run:
        summary["sample"] = leads[:3]
        return summary

    endpoint = url.rstrip("/")
    if not endpoint.endswith("/import-scraped"):
        # Allow base like https://api.example.com/api/v1/admin/leads/import-scraped
        pass

    for i in range(0, len(leads), BATCH_SIZE):
        batch = leads[i : i + BATCH_SIZE]
        status, payload = _post_batch(endpoint, api_key, batch)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            data = payload if isinstance(payload, dict) else {}
        created = int(data.get("created") or 0)
        skipped = int(data.get("skipped") or data.get("skippedCount") or 0)
        errs = data.get("errors") or []
        summary["created"] += created
        summary["skipped"] += skipped
        summary["batches"] += 1
        if errs:
            summary["errors"].extend(errs[:20])
        emails = [row["email"] for row in batch]
        record_synced_emails(conn, emails)
        log.info(
            "sync-leads batch %d: http=%s created=%d skipped=%d errors=%d",
            summary["batches"],
            status,
            created,
            skipped,
            len(errs),
        )

    mark_sync_success(conn, summary["created"], summary["skipped"])
    log.info(
        "sync-leads done: created=%d skipped=%d batches=%d",
        summary["created"],
        summary["skipped"],
        summary["batches"],
    )
    return summary
