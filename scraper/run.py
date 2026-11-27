import json
import logging
import os
import random
import time
from datetime import datetime, timedelta, timezone

from . import db
from google_play_scraper.exceptions import NotFoundError

from .ios import best_match, clean_url, similar
from .categories import category_terms
from .country import country_from_address
from .play_countries import PLAY_STORE_COUNTRIES

log = logging.getLogger("playstore")


def _is_http_400(exc):
    msg = str(exc).lower()
    return "400" in msg and ("bad request" in msg or "http error" in msg)


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


def _ios_check(cfg, fetcher, sleep, title, company, website, region, package=None):
    """Returns (url, match). match is 'none' if checked with no result, 'unchecked' if the lookup failed."""
    try:
        cands = fetcher.ios_lookup(title, region)
    except Exception as e:
        log.warning("  App Store lookup failed for %r: %s", title, e)
        return "", "unchecked"
    finally:
        sleep(cfg.get("ios_delay_seconds", 3))
    m, why = best_match(title, company, website, cands, bundle_id=package)
    return (clean_url(m.get("trackViewUrl")) if m else ""), (why if m else "none")


def db_target(cfg):
    """DATABASE_URL env or config database_url (PostgreSQL) wins; otherwise the local SQLite db_path."""
    return os.environ.get("DATABASE_URL") or cfg.get("database_url") or cfg["db_path"]


def resolve_countries(value, *, for_app_store=False):
    """Return ISO country codes to search.

    ALL → Google Play supported storefronts only (never invalid territories).
    Explicit lists are lowercased and intersected with Play-supported codes so
    typos / dead regions are dropped. for_app_store is kept for call-site clarity;
    the same Play list is used (iTunes HTTP 400s are still skipped if any remain).
    """
    del for_app_store  # same Play-supported list for both discovery modes
    play = {c.lower() for c in PLAY_STORE_COUNTRIES}
    if value in (None, "", "ALL"):
        return sorted(play)
    wanted = [str(c).lower() for c in value]
    return [c for c in wanted if c in play]


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


# The Play library raises these when it cannot read a result page (a parse problem for that one query),
# which is not a sign that Google is blocking us.
_PARSE_ERRORS = (TypeError, IndexError, KeyError, AttributeError)


def scrape(cfg, fetcher, sleep=time.sleep):
    """Never re-fetches a stored app and never repeats a finished search; never drops a scraped app
    (App Store / developer-country conditions only filter the contacts file)."""
    log.info("run started (discovery=%s)", cfg.get("discovery", "ios"))
    conn = db.connect(db_target(cfg))
    known = db.known_packages(conn, cfg.get("refresh_after_days"))
    cap = cfg.get("max_new_apps_per_run") or float("inf")
    deadline = time.monotonic() + 60 * cfg["max_runtime_minutes"] if cfg.get("max_runtime_minutes") else None
    stats = {"saved": 0, "resolved": 0}
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
        recheck = cfg.get("ios_recheck_per_run")  # null/0 = no cap
        for r in db.pending_ios(conn, recheck or None):
            url, why = _ios_check(cfg, fetcher, sleep, r["app"], r["company"], r["website"], r["crawl_region"] or "us",
                                  package=r["package"])
            if why != "unchecked":
                db.set_ios(conn, r["package"], url, why)
                log.info("  App Store check %s: %s", r["package"], why)

    mode = cfg.get("discovery", "ios")  # "ios" (App Store first), "play" (Google Play first) or "both"
    play_countries = resolve_countries(cfg.get("countries"))
    ios_countries = resolve_countries(cfg.get("countries"), for_app_store=True)
    try:
        if cfg.get("resolve_ios_only", True) and mode in ("ios", "both"):
            try:
                _resolve_ios_only(cfg, fetcher, conn, known, sleep, fail, ok, stats)
            except _Stop:
                log.warning("App Store-only resolver paused after repeated errors; continuing with discovery")
                ok()  # reset the failure streak so discovery gets a fresh start
        if mode in ("ios", "both"):
            ios_todo = due_searches(conn, ios_countries, ["ios:" + t for t in terms], cfg.get("search_repeat_days"))
            log.info(
                "iOS-first: %d searches due (%d terms x %d Play-supported countries)",
                len(ios_todo),
                len(terms),
                len(ios_countries),
            )
            _run_ios_first(cfg, fetcher, conn, ios_todo, known, cap, deadline, sleep, fail, ok, stats)
        if mode in ("play", "both"):
            todo = due_searches(conn, play_countries, terms, cfg.get("search_repeat_days"))
            log.info("Play-first: %d searches due (%d terms x %d countries)", len(todo), len(terms), len(play_countries))
            _run_searches(cfg, fetcher, conn, todo, known, cap, deadline, sleep, fail, ok, stats)
    except _Stop:
        pass
    contacts = db.refresh_contacts(conn, cfg.get("require_ios", False), cfg.get("developer_countries"))
    total = conn.execute("SELECT COUNT(*) AS n FROM apps").fetchone()["n"]
    if cfg.get("export_csv"):
        db.export_csv(conn, cfg["csv_path"])
        db.export_contacts(conn, cfg.get("contacts_path", "data/contacts.csv"))
    log.info("contacts table: %d unique companies/emails", contacts)
    log.info("run done: saved=%d resolved=%d failed=%d total_apps=%d", stats["saved"], stats["resolved"], failed, total)
    conn.close()
    return {"saved": stats["saved"], "failed": failed, "total": total}


def _run_searches(cfg, fetcher, conn, todo, known, cap, deadline, sleep, fail, ok, stats):
    budget = cap - stats["saved"]
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
                        cfg, fetcher, sleep, row["app"], row["company"], row["website"], country, package=pkg)
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


def _resolve_ios_only(cfg, fetcher, conn, known, sleep, fail, ok, stats):
    """For App Store apps with no same-ID Android app: search Google Play by name and accept a result only
    if the website matches, or the name and developer both match closely. Result is recorded either way."""
    # null/0 = resolve all pending ios-only apps this run (dedupe via known packages)
    limit = cfg.get("resolve_per_run") or None
    rows = db.pending_resolve(conn, limit)
    if rows:
        log.info("resolving %d App Store-only apps by name ...", len(rows))
    for r in rows:
        region = r["crawl_region"] or "us"
        try:
            try:
                hits = fetcher.search(r["app"], region, cfg["lang"], 5)
            except _PARSE_ERRORS as e:
                log.info("  Play results unreadable for %r (%s); marked resolve-unreadable, not retried", r["app"], e)
                db.set_match(conn, r["package"], "resolve-unreadable")
                stats["unreadable"] = stats.get("unreadable", 0) + 1
                ok()  # the server answered; this is not a block
                sleep(cfg["delay_seconds"])
                continue
            sleep(cfg["delay_seconds"])
            cands = sorted((h for h in hits if h.get("appId") and similar(r["app"], h.get("title")) >= 0.75),
                           key=lambda h: -similar(r["app"], h.get("title")))[:2]
            ios_rec = {"trackName": r["app"], "sellerName": r["company"], "sellerUrl": r["website"],
                       "trackViewUrl": r["ios_url"]}
            found = None
            for h in cands:
                try:
                    d = fetcher.details(h["appId"], region, cfg["lang"])
                except _PARSE_ERRORS + (NotFoundError,):
                    sleep(cfg["delay_seconds"])
                    continue  # unreadable or missing candidate: try the next one
                sleep(cfg["delay_seconds"])
                m, _ = best_match(d.get("title"), d.get("developer"), d.get("developerWebsite"), [ios_rec])
                if m:
                    found = (h["appId"], d)
                    break
            ok()
        except Exception as e:
            log.warning("  resolve failed for %r: %s", r["app"], e)
            fail()
            continue
        if found:
            pkg, d = found
            row = to_row(pkg, d, region)
            row["ios_url"], row["ios_match"] = clean_url(r["ios_url"]), "name-match"
            db.upsert(conn, row)
            if pkg != r["package"]:
                db.delete_app(conn, r["package"])
            known.add(pkg)
            stats["resolved"] += 1
            log.info("  MATCH %s -> Android %s", r["app"], pkg)
        else:
            db.set_match(conn, r["package"], "no-android-found")


def _ios_only_row(r, region):
    return {"package": r["bundleId"], "app": r.get("trackName") or "",
            "company": r.get("sellerName") or r.get("artistName") or "", "website": r.get("sellerUrl") or "",
            "category": r.get("primaryGenreName") or "", "url": "", "support_email": "", "developer_email": "",
            "developer_address": "", "country": "", "crawl_region": region, "fetched_at": db.now(),
            "ios_url": clean_url(r.get("trackViewUrl")), "ios_match": "ios-only"}


def _run_ios_first(cfg, fetcher, conn, todo, known, cap, deadline, sleep, fail, ok, stats):
    """Search the App Store (up to 200 apps per request, official API), then open each app's Google Play page
    directly by bundle ID. Found = on both stores. Not found = stored as App Store only (nothing is dropped)."""
    known_ios = {clean_url(r["ios_url"]) for r in conn.execute("SELECT ios_url FROM apps") if r["ios_url"]}
    for country, key in todo:
        term = key[len("ios:"):]
        if cap - stats["saved"] <= 0:
            log.info("per-run limit reached; the rest continues next run")
            return
        if deadline and time.monotonic() > deadline:
            log.info("run time limit reached; the rest continues next run")
            return
        log.info("App Store search %r in %s ...", term, country)
        try:
            results = fetcher.ios_search(term, country, cfg.get("ios_results_per_query", 200))
        except Exception as e:
            log.warning("App Store search failed term=%r country=%s: %s", term, country, e)
            if _is_http_400(e):
                # Unsupported storefront — mark done and keep going (do not burn failure streak).
                db.mark_search_done(conn, key, country)
                sleep(cfg.get("ios_delay_seconds", 3))
                continue
            db.search_failed(conn, key, country)
            fail()
            sleep(cfg.get("ios_delay_seconds", 3))
            continue
        ok()
        sleep(cfg.get("ios_delay_seconds", 3))
        log.info("  %d apps", len(results))
        complete, bad = True, False
        for r in results:
            bundle = r.get("bundleId")
            if not bundle or bundle in known or clean_url(r.get("trackViewUrl")) in known_ios:
                continue
            if cap - stats["saved"] <= 0:
                complete = False
                break
            try:
                try:
                    d = fetcher.details(bundle, country, cfg["lang"])
                except NotFoundError:
                    d = None
                if d is None:
                    row = _ios_only_row(r, country)
                else:
                    row = to_row(bundle, d, country)
                    row["ios_url"], row["ios_match"] = clean_url(r.get("trackViewUrl")), "bundle-id"
                db.upsert(conn, row)
                known.add(bundle)
                known_ios.add(row["ios_url"])
                stats["saved"] += 1
                ok()
                log.info("  %s %s (%s)", "BOTH" if d else "iOS-only", bundle, r.get("trackName"))
            except Exception as e:
                log.warning("Google Play check failed %s: %s", bundle, e)
                bad = True
                fail()
            sleep(cfg["delay_seconds"])
        if complete and not bad:
            db.mark_search_done(conn, key, country)
        if cfg.get("export_csv"):
            db.export_csv(conn, cfg["csv_path"])


def load_config(path):
    with open(path) as f:
        return json.load(f)
