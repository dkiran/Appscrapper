import json
import logging
import os
import random
import time
from datetime import datetime, timedelta, timezone

import pycountry

from . import db
from .ios import best_match
from .categories import category_terms
from .country import country_from_address

log = logging.getLogger("playstore")


def to_row(package, d, region):
    address = (d.get("developerAddress") or "").replace("\n", ", ")
    support = (d.get("developerEmail") or "").strip()
    return {
        "package": package,
        "app": d.get("title") or "",
        "company": d.get("developer") or "",
        "website": d.get("developerWebsite") or "",
        "category": d.get("genre") or "",
        "url": d.get("url") or f"https://play.google.com/store/apps/details?id={package}",
        "support_email": support,
        "developer_email": ", ".join(d.get("developerEmails") or []),
        "developer_address": address,
        "country": country_from_address(address),
        "crawl_region": region,
        "fetched_at": db.now(),
    }


def _ios_check(cfg, fetcher, sleep, title, company, website, region):
    """Returns (url, match). match is 'none' if checked with no result, 'unchecked' if the lookup failed."""
    try:
        cands = fetcher.ios_lookup(title, region)
    except Exception as e:
        log.warning("  App Store lookup failed for %r: %s", title, e)
        return "", "unchecked"
    finally:
        sleep(cfg.get("ios_delay_seconds", 3))
    m, why = best_match(title, company, website, cands)
    return (m.get("trackViewUrl") if m else ""), (why if m else "none")


def db_target(cfg):
    """DATABASE_URL env or config database_url (PostgreSQL) wins; otherwise the local SQLite db_path."""
    return os.environ.get("DATABASE_URL") or cfg.get("database_url") or cfg["db_path"]


def resolve_countries(value):
    if value in (None, "", "ALL"):
        return sorted(c.alpha_2.lower() for c in pycountry.countries)
    return [c.lower() for c in value]


def due_searches(conn, countries, terms, repeat_days):
    """Searches never finished first (term by term, each across all countries), then finished ones older than repeat_days, oldest first."""
    state = db.searches_state(conn)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=repeat_days)).isoformat() if repeat_days else None
    todo = []
    for t in terms:  # one term/category across every country before the next term
        for c in countries:
            done = state.get((t, c), "")
            if not done or (cutoff and done < cutoff):
                todo.append((done, c, t))
    todo.sort(key=lambda x: x[0])  # stable: unfinished ("") keep config order
    return [(c, t) for _, c, t in todo]


class _Stop(Exception):
    pass


def scrape(cfg, fetcher, sleep=time.sleep):
    """Never re-fetches a stored app and never repeats a finished search; never drops a scraped app
    (App Store / developer-country conditions only filter the contacts file)."""
    conn = db.connect(db_target(cfg))
    known = db.known_packages(conn, cfg.get("refresh_after_days"))
    budget = cfg.get("max_new_apps_per_run") or float("inf")
    deadline = time.monotonic() + 60 * cfg["max_runtime_minutes"] if cfg.get("max_runtime_minutes") else None
    stats = {"saved": 0}
    failed = streak = 0
    limit = cfg.get("max_consecutive_failures") or float("inf")
    raw_sleep = sleep

    def sleep(sec):  # randomised so requests don't arrive at a perfectly regular rhythm
        raw_sleep(sec * random.uniform(0.7, 1.5))

    def fail():
        nonlocal failed, streak
        failed += 1
        streak += 1
        if streak >= limit:
            log.warning("%d failures in a row - stopping this run (possible block). Next run continues.", streak)
            raise _Stop()

    def ok():
        nonlocal streak
        streak = 0

    terms = list(cfg.get("search_terms") or []) + category_terms(cfg.get("categories"))

    if cfg.get("check_ios"):  # finish App Store checks that never ran or failed earlier
        for r in db.pending_ios(conn, cfg.get("ios_recheck_per_run", 50)):
            url, why = _ios_check(cfg, fetcher, sleep, r["app"], r["company"], r["website"], r["crawl_region"] or "us")
            if why != "unchecked":
                db.set_ios(conn, r["package"], url, why)
                log.info("  App Store check %s: %s", r["package"], why)

    countries = resolve_countries(cfg.get("countries"))
    todo = due_searches(conn, countries, terms, cfg.get("search_repeat_days"))
    log.info("%d searches due (%d categories/terms x %d countries)", len(todo), len(terms), len(countries))
    try:
        _run_searches(cfg, fetcher, conn, todo, known, budget, deadline, sleep, fail, ok, stats)
    except _Stop:
        pass
    contacts = db.refresh_contacts(conn, cfg.get("require_ios", False), cfg.get("developer_countries"))
    total = conn.execute("SELECT COUNT(*) AS n FROM apps").fetchone()["n"]
    if cfg.get("export_csv"):
        db.export_csv(conn, cfg["csv_path"])
        db.export_contacts(conn, cfg.get("contacts_path", "data/contacts.csv"))
    log.info("contacts table: %d unique companies/emails", contacts)
    log.info("run done: saved=%d failed=%d total_apps=%d", stats["saved"], failed, total)
    conn.close()
    return {"saved": stats["saved"], "failed": failed, "total": total}


def _run_searches(cfg, fetcher, conn, todo, known, budget, deadline, sleep, fail, ok, stats):
    for country, term in todo:
        if budget <= 0:
            log.info("per-run limit reached; the rest continues next run")
            break
        if deadline and time.monotonic() > deadline:
            log.info("run time limit reached; the rest continues next run")
            break
        log.info("searching %r in %s ...", term, country)
        try:
            hits = fetcher.search(term, country, cfg["lang"], cfg["results_per_query"])
        except Exception as e:
            log.warning("search failed term=%r country=%s: %s", term, country, e)
            db.search_failed(conn, term, country)
            fail()
            sleep(cfg["delay_seconds"])
            continue
        log.info("  %d results", len(hits))
        sleep(cfg["delay_seconds"])  # pace the searches too, even when every result is already stored
        search_failed = False
        ok()
        complete = True
        for h in hits:
            pkg = h.get("appId")
            if not pkg or pkg in known:
                continue
            if budget <= 0:
                complete = False
                break
            try:
                d = fetcher.details(pkg, country, cfg["lang"])
                row = to_row(pkg, d, country)
                if cfg.get("check_ios"):
                    row["ios_url"], row["ios_match"] = _ios_check(
                        cfg, fetcher, sleep, row["app"], row["company"], row["website"], country)
                db.upsert(conn, row)
                known.add(pkg)
                stats["saved"] += 1
                budget -= 1
                ok()
                log.info("  saved %s (%s) email=%s dev_email=%s ios=%s", pkg, d.get("title"),
                         d.get("developerEmail") or "-", ", ".join(d.get("developerEmails") or []) or "-",
                         row.get("ios_match", "-"))
            except Exception as e:
                log.warning("details failed %s: %s", pkg, e)
                search_failed = True
                fail()
            sleep(cfg["delay_seconds"])
        if complete and not search_failed:
            db.mark_search_done(conn, term, country)
        if cfg.get("export_csv"):
            db.export_csv(conn, cfg["csv_path"])


def load_config(path):
    with open(path) as f:
        return json.load(f)
