import json
import logging
import time

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


def scrape(cfg, fetcher, sleep=time.sleep):
    """Never re-fetches a stored app and never repeats a finished search; never drops a scraped app
    (App Store / developer-country conditions only filter the contacts file)."""
    conn = db.connect(cfg["db_path"])
    known = db.known_packages(conn, cfg.get("refresh_after_days"))
    budget = cfg["max_new_apps_per_run"]
    saved = failed = 0
    terms = list(cfg.get("search_terms") or []) + category_terms(cfg.get("categories"))

    if cfg.get("check_ios"):  # finish App Store checks that never ran or failed earlier
        for r in db.pending_ios(conn, cfg.get("ios_recheck_per_run", 50)):
            url, why = _ios_check(cfg, fetcher, sleep, r["app"], r["company"], r["website"], r["crawl_region"] or "us")
            if why != "unchecked":
                db.set_ios(conn, r["package"], url, why)
                log.info("  App Store check %s: %s", r["package"], why)

    todo = [(c, t) for c in cfg["countries"] for t in terms if not db.search_done(conn, t, c)]
    if not todo:
        log.info("every configured search is already done; add search_terms/categories/countries for more")
    for country, term in todo:
        if budget <= 0:
            log.info("per-run limit reached; the rest continues next run")
            break
        log.info("searching %r in %s ...", term, country)
        try:
            hits = fetcher.search(term, country, cfg["lang"], cfg["results_per_query"])
        except Exception as e:
            log.warning("search failed term=%r country=%s: %s", term, country, e)
            failed += 1
            sleep(cfg["delay_seconds"])
            continue
        log.info("  %d results", len(hits))
        search_failed = False
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
                saved += 1
                budget -= 1
                log.info("  saved %s (%s) email=%s dev_email=%s ios=%s", pkg, d.get("title"),
                         d.get("developerEmail") or "-", ", ".join(d.get("developerEmails") or []) or "-",
                         row.get("ios_match", "-"))
            except Exception as e:
                log.warning("details failed %s: %s", pkg, e)
                failed += 1
                search_failed = True
            sleep(cfg["delay_seconds"])
        if complete and not search_failed:
            db.mark_search_done(conn, term, country)
        db.export_csv(conn, cfg["csv_path"])
    total = db.export_csv(conn, cfg["csv_path"])
    contacts = db.export_contacts(conn, cfg.get("contacts_path", "data/contacts.csv"),
                                  cfg.get("require_ios", False), cfg.get("developer_countries"))
    log.info("contacts file: %d unique companies/emails", contacts)
    log.info("run done: saved=%d failed=%d total_in_csv=%d", saved, failed, total)
    conn.close()
    return {"saved": saved, "failed": failed, "total": total}


def load_config(path):
    with open(path) as f:
        return json.load(f)
