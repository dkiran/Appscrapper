"""Play Store app scraper. `python main.py --once` runs now; without it, continuous loop or cron."""
import argparse
import logging
import os
import random
import time

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from scraper.fetch import PlayFetcher
from scraper.run import load_config, scrape


class _SyncArgs:
    def __init__(self, dry_run=False, since=None, resync_all=False):
        self.dry_run = dry_run
        self.since = since
        self.resync_all = resync_all


def _run_sync_leads(cfg, args):
    from scraper import db
    from scraper.run import db_target
    from scraper.sync_leads import sync_leads

    conn = db.connect(db_target(cfg))
    try:
        # Refresh contacts from apps so Play-only / iOS-only emails are included.
        db.refresh_contacts(conn, cfg.get("require_ios", False), cfg.get("developer_countries"))
        summary = sync_leads(
            cfg,
            conn,
            dry_run=args.dry_run,
            since=getattr(args, "since", None),
            resync_all=bool(getattr(args, "resync_all", False) or getattr(args, "since", None)),
        )
        logging.info("sync-leads summary: %s", summary)
        if args.dry_run and summary.get("sample"):
            for row in summary["sample"]:
                logging.info("sample lead: %s", row)
        return summary
    finally:
        conn.close()


def _scrape_then_maybe_sync(cfg):
    result = scrape(cfg, PlayFetcher())
    if cfg.get("sync_leads_after_scrape"):
        try:
            _run_sync_leads(cfg, _SyncArgs())
        except Exception:
            logging.exception("sync-leads after scrape failed (scrape results were kept)")
    return result or {}


def _sync_configured(cfg):
    return bool(os.environ.get("SINGLEQR_LEADS_URL") or cfg.get("singleqr_leads_url"))


def _is_continuous(cfg):
    mode = str(cfg.get("schedule_mode") or "").strip().lower()
    cron = cfg.get("schedule_cron")
    return mode in ("continuous", "loop") or cron in (None, "", "continuous", "loop")


def _pause_seconds(minutes, jitter=0.2):
    base = max(1, float(minutes or 1) * 60)
    return base * random.uniform(1.0 - jitter, 1.0 + jitter)


def _run_continuous(cfg):
    """Scrape forever: request pacing is inside scrape(); pause between runs to avoid blocks."""
    pause_m = cfg.get("loop_pause_minutes", 45)
    block_m = cfg.get("block_cooldown_minutes", 120)
    logging.info(
        "continuous scrape: delay_seconds=%s ios_delay_seconds=%s "
        "loop_pause_minutes=%s block_cooldown_minutes=%s. Ctrl+C to stop.",
        cfg.get("delay_seconds"),
        cfg.get("ios_delay_seconds"),
        pause_m,
        block_m,
    )
    while True:
        try:
            result = _scrape_then_maybe_sync(cfg)
        except Exception:
            logging.exception("scrape loop iteration failed; cooling down")
            result = {"stopped_early": True}
        wait_m = block_m if result.get("stopped_early") else pause_m
        wait_s = _pause_seconds(wait_m)
        logging.info(
            "continuous scrape: sleeping %.0f min before next run (stopped_early=%s)",
            wait_s / 60.0,
            bool(result.get("stopped_early")),
        )
        time.sleep(wait_s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--once", action="store_true", help="run one scrape and exit (use with system cron)")
    ap.add_argument("--inspect", metavar="PACKAGE", help="print where emails sit in a page's data, then exit")
    ap.add_argument("--export", action="store_true", help="write apps.csv and contacts.csv from the database, then exit")
    ap.add_argument("--import-sqlite", metavar="FILE", help="copy an old SQLite file (e.g. data/apps.db) into the database, then exit")
    ap.add_argument("--list-categories", action="store_true", help="print all category ids and exit")
    ap.add_argument("--sync-leads", action="store_true",
                    help="push new contacts to SingleQR leads API, then exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --sync-leads: map and count only, do not POST")
    ap.add_argument("--resync-all", action="store_true",
                    help="with --sync-leads: ignore local synced-email gate and push all contacts with email "
                         "(SingleQR still skips duplicates)")
    ap.add_argument("--since", metavar="ISO",
                    help="with --sync-leads: same as --resync-all (legacy backfill flag)")
    args = ap.parse_args()
    if args.list_categories:
        from scraper.categories import ALL_CATEGORIES
        for k, v in ALL_CATEGORIES.items():
            print(f"{k:22} {v}")
        return
    if args.inspect:
        for key, path, email in PlayFetcher().inspect(args.inspect):
            print(key, path, email)
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    if args.sync_leads:
        _run_sync_leads(cfg, args)
        return
    if not (args.export or args.import_sqlite):
        from scraper.dblog import DbLogHandler
        from scraper.run import db_target as _target
        logging.getLogger().addHandler(DbLogHandler(_target(cfg)))  # same lines as the console, for the dashboard
    if args.export or args.import_sqlite:
        from scraper import db
        from scraper.run import db_target
        conn = db.connect(db_target(cfg))
        if args.import_sqlite:
            print("imported", db.import_sqlite(args.import_sqlite, conn), "apps")
            db.refresh_contacts(conn, cfg.get("require_ios", False), cfg.get("developer_countries"))
        if args.export:
            print(db.export_csv(conn, cfg["csv_path"]), "apps ->", cfg["csv_path"])
            print(db.export_contacts(conn, cfg["contacts_path"]), "contacts ->", cfg["contacts_path"])
        return
    if args.once:
        _scrape_then_maybe_sync(cfg)
        return
    if _is_continuous(cfg):
        try:
            _run_continuous(cfg)
        except (KeyboardInterrupt, SystemExit):
            pass
        return
    job = lambda: _scrape_then_maybe_sync(cfg)
    if cfg.get("run_on_start"):
        job()
    sched = BlockingScheduler(timezone=cfg["timezone"])
    sched.add_job(job, CronTrigger.from_crontab(cfg["schedule_cron"], timezone=cfg["timezone"]),
                  max_instances=1, coalesce=True)
    sync_cron = cfg.get("sync_cron") or "0 4 * * *"
    if _sync_configured(cfg):
        # Separate daily sync (default 04:00 UTC) so contacts land even if scrape overruns.
        sched.add_job(
            lambda: _run_sync_leads(cfg, _SyncArgs()),
            CronTrigger.from_crontab(sync_cron, timezone=cfg["timezone"]),
            id="sync_leads",
            max_instances=1,
            coalesce=True,
        )
        logging.info("scheduled sync-leads: %s (%s)", sync_cron, cfg["timezone"])
    logging.info("scheduled scrape: %s (%s). Ctrl+C to stop.", cfg["schedule_cron"], cfg["timezone"])
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
