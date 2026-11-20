"""Play Store app scraper. `python main.py --once` runs now; without it, runs on the cron schedule."""
import argparse
import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from scraper.fetch import PlayFetcher
from scraper.run import load_config, scrape


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--once", action="store_true", help="run one scrape and exit (use with system cron)")
    ap.add_argument("--inspect", metavar="PACKAGE", help="print where emails sit in a page's data, then exit")
    ap.add_argument("--export", action="store_true", help="write apps.csv and contacts.csv from the database, then exit")
    ap.add_argument("--import-sqlite", metavar="FILE", help="copy an old SQLite file (e.g. data/apps.db) into the database, then exit")
    ap.add_argument("--list-categories", action="store_true", help="print all category ids and exit")
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
    job = lambda: scrape(cfg, PlayFetcher())
    if args.once:
        job()
        return
    if cfg.get("run_on_start"):
        job()
    sched = BlockingScheduler(timezone=cfg["timezone"])
    sched.add_job(job, CronTrigger.from_crontab(cfg["schedule_cron"], timezone=cfg["timezone"]),
                  max_instances=1, coalesce=True)
    logging.info("scheduled: %s (%s). Ctrl+C to stop.", cfg["schedule_cron"], cfg["timezone"])
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
