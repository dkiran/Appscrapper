import csv
import os

import pytest
from scraper.country import country_from_address
from scraper.run import scrape

PG = os.environ.get("TEST_DATABASE_URL")  # set to run the same tests against PostgreSQL


@pytest.fixture(autouse=True)
def clean_pg():
    if PG:
        import psycopg
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute("DROP TABLE IF EXISTS apps, searches, contacts")
    yield


CFG = lambda tmp: {"discovery": "play", "database_url": PG, "export_csv": True, "db_path": str(tmp / "a.db"), "csv_path": str(tmp / "a.csv"),
                   "search_terms": ["x"], "countries": ["us"], "lang": "en",
                   "results_per_query": 5, "max_new_apps_per_run": 10,
                   "delay_seconds": 0, "refresh_after_days": 30,
                   "check_ios": False, "contacts_path": str(tmp / "c.csv")}


class Fake:
    ios = []
    def __init__(self): self.detail_calls = 0
    def ios_lookup(self, t, c): return self.ios
    def search(self, t, c, l, n): return [{"appId": "com.a"}, {"appId": "com.b"}]
    def details(self, p, c, l):
        self.detail_calls += 1
        if p == "com.b":
            raise RuntimeError("blocked")
        return {"title": "A", "developer": "Acme", "developerWebsite": "https://acme.io",
                "genre": "Tools", "url": "https://play.google.com/store/apps/details?id=com.a",
                "developerEmail": "support@acme.io", "developerEmails": ["ceo@acme.io"], "developerAddress": "1 Main St\nNairobi, Kenya"}


def test_country():
    assert country_from_address("1 Main St, Mumbai, India") == "India"
    assert country_from_address("PO Box 1, CA 94000, USA") == "United States"
    assert country_from_address("") == ""


def test_scrape_exports_columns_and_skips_fresh(tmp_path):
    f = Fake()
    r = scrape(CFG(tmp_path), f, sleep=lambda s: None)
    assert r["saved"] == 1 and r["failed"] == 1
    rows = list(csv.DictReader(open(tmp_path / "a.csv")))
    assert list(rows[0]) == ["Email", "App", "Company", "Website", "Category",
                             "Play Store URL", "Developer email", "Developer address", "Country",
                             "App Store URL", "iOS match", "Both Stores"]
    assert rows[0]["Country"] == "Kenya" and rows[0]["Email"] == "support@acme.io"
    assert rows[0]["Developer email"] == "ceo@acme.io"
    calls = f.detail_calls
    scrape(CFG(tmp_path), f, sleep=lambda s: None)  # com.a fresh -> only com.b retried
    assert f.detail_calls == calls + 1


def test_page_email_extraction():
    from scraper.fetch import email_strings
    blob = [["desc: mail me at x@y.com"], [["a@b.com"], ["Support@B.com", "c@d.org"]]]
    assert [e for _, e in email_strings(blob)] == ["a@b.com", "Support@B.com", "c@d.org"]


def test_categories_terms():
    from scraper.categories import category_terms
    assert category_terms(["FINANCE"]) == ["Finance"] and len(category_terms("ALL")) == 49


def _contacts(tmp_path):
    return list(csv.DictReader(open(tmp_path / "c.csv")))


def test_nothing_dropped_filters_only_contacts(tmp_path):
    cfg = CFG(tmp_path); cfg.update(check_ios=True, require_ios=True, ios_delay_seconds=0,
                                    developer_countries=["India"])
    r = scrape(cfg, Fake(), sleep=lambda s: None)
    assert r["saved"] == 1  # saved although no App Store match and developer is in Kenya
    rows = list(csv.DictReader(open(tmp_path / "a.csv")))
    assert len(rows) == 1 and rows[0]["App Store URL"] == "" and rows[0]["Both Stores"] == "No"
    assert _contacts(tmp_path) == []  # contacts filtered by require_ios / developer_countries


def test_ios_match_and_require(tmp_path):
    from scraper.ios import best_match
    cand = {"trackName": "A", "trackViewUrl": "https://apps.apple.com/app/id1", "sellerName": "Acme Ltd",
            "sellerUrl": "https://www.acme.io/about"}
    assert best_match("A", "Acme", "https://acme.io", [cand])[1] == "website+title"
    assert best_match("A", "Other Co", "https://x.com", [cand])[0] is None
    assert best_match("Alpha", "Acme", "https://acme.io", [cand])[0] is None
    cfg = CFG(tmp_path); cfg.update(check_ios=True, require_ios=True, ios_delay_seconds=0)
    f = Fake(); f.ios = [cand]
    scrape(cfg, f, sleep=lambda s: None)
    rows = list(csv.DictReader(open(tmp_path / "a.csv")))
    assert rows[0]["App Store URL"].startswith("https://apps.apple.com") and rows[0]["Both Stores"] == "Yes"
    c = _contacts(tmp_path)
    assert len(c) == 1 and c[0]["Both Stores"] == "Yes" and c[0]["App Store URL"].startswith("https://apps.apple.com")


def test_no_repeat_and_ios_recheck(tmp_path):
    class Flaky(Fake):
        def ios_lookup(self, t, c):
            if self.down: raise RuntimeError("apple down")
            return super().ios_lookup(t, c)
        def details(self, p, c, l):
            self.detail_calls += 1
            return {"title": p, "developer": "D", "developerEmail": "s@d.io", "developerAddress": "Pune, India"}
        def search(self, t, c, l, n): self.searches += 1; return [{"appId": "com.a"}]
    f = Flaky(); f.down = True; f.searches = 0
    cfg = CFG(tmp_path); cfg.update(check_ios=True, ios_delay_seconds=0)
    scrape(cfg, f, sleep=lambda s: None)
    assert f.detail_calls == 1 and f.searches == 1
    f.down = False; f.ios = [{"trackName": "com.a", "trackViewUrl": "https://apps.apple.com/x", "sellerName": "D"}]
    scrape(cfg, f, sleep=lambda s: None)  # search done + app known -> only the failed iOS check is retried
    assert f.detail_calls == 1 and f.searches == 1
    assert list(csv.DictReader(open(tmp_path / "a.csv")))[0]["App Store URL"].endswith("/x")


def test_all_countries_and_repeat_window(tmp_path):
    from scraper import db
    from scraper.run import resolve_countries, db_target
    assert "us" in resolve_countries("ALL") and len(resolve_countries("ALL")) > 200

    class Good(Fake):
        searches = 0
        def search(self, t, c, l, n): Good.searches += 1; return [{"appId": "com.a"}]
        def details(self, p, c, l):
            self.detail_calls += 1
            return {"title": "A", "developer": "D", "developerEmail": "s@d.io", "developerAddress": "Pune, India"}
    cfg = CFG(tmp_path); cfg.update(search_repeat_days=30)
    f = Good(); scrape(cfg, f, sleep=lambda s: None)
    assert (Good.searches, f.detail_calls) == (1, 1)
    scrape(cfg, f, sleep=lambda s: None)
    assert (Good.searches, f.detail_calls) == (1, 1)  # finished and inside the repeat window
    con = db.connect(db_target(cfg)); con.execute("UPDATE searches SET done_at='2000-01-01'"); con.commit(); con.close()
    scrape(cfg, f, sleep=lambda s: None)
    assert (Good.searches, f.detail_calls) == (2, 1)  # search repeats, stored app is not fetched again


def test_stops_after_consecutive_failures_and_jitters(tmp_path):
    delays = []

    class Blocked(Fake):
        def search(self, t, c, l, n): return [{"appId": f"com.{i}"} for i in range(20)]
        def details(self, p, c, l): self.detail_calls += 1; raise RuntimeError("429")
    f = Blocked(); cfg = CFG(tmp_path); cfg["max_consecutive_failures"] = 3; cfg["delay_seconds"] = 4
    r = scrape(cfg, f, sleep=delays.append)
    assert f.detail_calls == 3 and r["failed"] == 3  # stopped early instead of hammering 20 apps
    assert delays and all(2.8 <= d <= 6.0 for d in delays) and len(set(delays)) > 1


def test_order_is_category_across_all_countries_first(tmp_path):
    from scraper import db
    from scraper.run import due_searches
    conn = db.connect(PG or str(tmp_path / "o.db"))
    order = due_searches(conn, ["us", "in", "ke"], ["Business", "Finance"], 30)
    assert order == [("us", "Business"), ("in", "Business"), ("ke", "Business"),
                     ("us", "Finance"), ("in", "Finance"), ("ke", "Finance")]


def test_database_is_the_store_and_import(tmp_path):
    from scraper import db
    from scraper.run import db_target
    cfg = CFG(tmp_path); cfg["export_csv"] = False
    scrape(cfg, Fake(), sleep=lambda s: None)
    assert not (tmp_path / "a.csv").exists()  # no CSV unless asked for
    conn = db.connect(db_target(cfg))
    r = conn.execute("SELECT app, support_email, both_stores FROM apps").fetchall()
    assert [(x["app"], x["support_email"], x["both_stores"]) for x in r] == [("A", "support@acme.io", "No")]
    assert conn.execute("SELECT COUNT(*) AS n FROM contacts").fetchone()["n"] == 1
    # import from an old SQLite file
    import sqlite3
    old = tmp_path / "old.db"
    o = sqlite3.connect(old)
    o.execute("CREATE TABLE apps (package TEXT PRIMARY KEY, app TEXT, url TEXT, support_email TEXT)")
    o.execute("INSERT INTO apps VALUES ('com.old','Old','https://play/x','o@o.io')"); o.commit(); o.close()
    assert db.import_sqlite(str(old), conn) == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM apps").fetchone()["n"] == 2
    assert db.export_csv(conn, str(tmp_path / "x.csv")) == 2


def test_every_search_is_paced(tmp_path):
    delays = []

    class AllKnown(Fake):
        def search(self, t, c, l, n): return []
    cfg = CFG(tmp_path); cfg.update(search_terms=["a", "b", "c"], countries=["us"], delay_seconds=4)
    scrape(cfg, AllKnown(), sleep=delays.append)
    assert len(delays) == 3 and all(d > 0 for d in delays)


class IosFake:
    """App Store returns 3 apps: one on both stores (same ID), one iOS-only, one whose Play check fails."""
    def __init__(self): self.play_calls = []; self.ios_searches = 0; self.broken = True
    def ios_search(self, term, country, limit):
        self.ios_searches += 1
        return [{"bundleId": "com.both", "trackName": "Both", "trackViewUrl": "https://apps.apple.com/app/id1?uo=4",
                 "sellerName": "Acme"},
                {"bundleId": "com.ios.only", "trackName": "Only", "trackViewUrl": "https://apps.apple.com/app/id2?uo=4",
                 "sellerName": "Zed", "sellerUrl": "https://zed.io", "primaryGenreName": "Finance"},
                {"bundleId": "com.flaky", "trackName": "Flaky", "trackViewUrl": "https://apps.apple.com/app/id3",
                 "sellerName": "Fl"}]
    def details(self, p, c, l):
        from google_play_scraper.exceptions import NotFoundError
        self.play_calls.append(p)
        if p == "com.ios.only": raise NotFoundError("404")
        if p == "com.flaky" and self.broken: raise RuntimeError("429")
        return {"title": p, "developer": "Acme", "developerEmail": "s@acme.io", "developerEmails": ["d@acme.io"],
                "developerAddress": "Pune, India", "url": f"https://play.google.com/store/apps/details?id={p}"}


def test_ios_first_discovery(tmp_path):
    cfg = CFG(tmp_path); cfg.update(discovery="ios", search_terms=["Business"], countries=["us"], check_ios=True)
    f = IosFake()
    r = scrape(cfg, f, sleep=lambda s: None)
    assert r["saved"] == 2 and r["failed"] == 1
    rows = {x["App"]: x for x in csv.DictReader(open(tmp_path / "a.csv"))}
    assert rows["com.both"]["Both Stores"] == "Yes" and rows["com.both"]["iOS match"] == "bundle-id"
    assert rows["com.both"]["App Store URL"] == "https://apps.apple.com/app/id1"  # tracking query removed
    o = rows["Only"]
    assert o["Both Stores"] == "No" and o["Play Store URL"] == "" and o["iOS match"] == "ios-only" and o["Website"] == "https://zed.io"
    # flaky Play check -> not stored, search not marked done, so it is retried; stored apps are not re-checked
    f.broken = False; f.play_calls.clear()
    r2 = scrape(cfg, f, sleep=lambda s: None)
    assert f.play_calls == ["com.flaky"] and r2["saved"] == 1 and f.ios_searches == 2
    r3 = scrape(cfg, f, sleep=lambda s: None)
    assert f.ios_searches == 2 and r3["saved"] == 0  # search finished: nothing repeated


def test_bundle_id_beats_name_matching():
    from scraper.ios import best_match
    cand = {"bundleId": "com.Acme.App", "trackName": "Totally Different", "sellerName": "Someone"}
    assert best_match("X", "Y", "", [cand], bundle_id="com.acme.app")[1] == "bundle-id"


def test_resolve_ios_only_by_name(tmp_path):
    from scraper import db
    from scraper.run import db_target
    cfg = CFG(tmp_path); cfg.update(discovery="ios", search_terms=[], countries=[], resolve_per_run=10)
    conn = db.connect(db_target(cfg))
    for pkg, name, site in [("net.zed.Only", "Only", "https://www.zed.io"), ("com.q.Nope", "Nope", "https://q.io")]:
        db.upsert(conn, {"package": pkg, "app": name, "company": "Zed Ltd", "website": site, "url": "",
                         "ios_url": f"https://apps.apple.com/app/{name}", "ios_match": "ios-only",
                         "crawl_region": "us", "fetched_at": db.now()})

    class R(Fake):
        searches = 0
        def search(self, t, c, l, n):
            R.searches += 1
            return [{"appId": "com.zed.android", "title": "Only", "developer": "Zed"}] if t == "Only" else \
                   [{"appId": "com.other", "title": "Something Else", "developer": "X"}]
        def details(self, p, c, l):
            self.detail_calls += 1
            return {"title": "Only", "developer": "Zed Ltd", "developerWebsite": "https://zed.io",
                    "developerEmail": "hi@zed.io", "developerEmails": [], "developerAddress": "Dubai, UAE"}
    f = R()
    r = scrape(cfg, f, sleep=lambda s: None)
    rows = {x["package"]: x for x in conn.execute("SELECT * FROM apps").fetchall()}
    assert "net.zed.Only" not in rows and rows["com.zed.android"]["ios_match"] == "name-match"
    assert rows["com.zed.android"]["both_stores"] == "Yes" and rows["com.zed.android"]["support_email"] == "hi@zed.io"
    assert rows["com.q.Nope"]["ios_match"] == "no-android-found" and rows["com.q.Nope"]["both_stores"] == "No"
    scrape(cfg, f, sleep=lambda s: None)
    assert R.searches == 2  # each app searched once only


def test_dashboard_stats_and_server(tmp_path, monkeypatch):
    import json, threading, urllib.request, urllib.error, base64
    from datetime import date
    import dashboard
    from scraper import db
    from scraper.run import db_target
    cfg = CFG(tmp_path)
    scrape(cfg, Fake(), sleep=lambda s: None)
    conn = db.connect(db_target(cfg))
    s = dashboard.collect_stats(conn)
    assert s["total"] == 1 and s["today"] == 1 and len(s["days"]) == 30 and s["days"][-1]["day"] == date.today().isoformat()
    assert s["app_store_only"] == 0 and s["play_only"] == 1 and s["with_email"] == 1 and s["companies"] == 1
    conn.close()
    srv = dashboard.make_server(db_target(cfg), "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    assert json.load(urllib.request.urlopen(base + "/api/stats"))["total"] == 1
    assert b"Scraper monitor" in urllib.request.urlopen(base + "/").read()
    head = urllib.request.urlopen(urllib.request.Request(base + "/", method="HEAD"))
    assert head.status == 200 and head.read() == b""
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    with pytest.raises(urllib.error.HTTPError) as eh:
        urllib.request.urlopen(urllib.request.Request(base + "/", method="HEAD"))
    assert eh.value.code == 401
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(base + "/api/stats")
    assert e.value.code == 401
    req = urllib.request.Request(base + "/api/stats", headers={"Authorization": "Basic " + base64.b64encode(b"u:pw").decode()})
    assert json.load(urllib.request.urlopen(req))["total"] == 1
    srv.shutdown()


def test_logs_reach_database_and_dashboard(tmp_path):
    import json, logging, threading, urllib.request
    import dashboard
    from scraper import db
    from scraper.dblog import DbLogHandler
    from scraper.run import db_target
    cfg = CFG(tmp_path); cfg.update(categories=["FINANCE", "MEDICAL"], search_terms=[], countries=["us", "in"])
    h = DbLogHandler(db_target(cfg)); lg = logging.getLogger("playstore"); lg.addHandler(h); lg.setLevel(logging.INFO)
    try:
        scrape(cfg, Fake(), sleep=lambda s: None)
        lg.warning("custom <b>warning</b>")
    finally:
        lg.removeHandler(h); h.close()
    conn = db.connect(db_target(cfg))
    allrows = dashboard.collect_logs(conn)
    assert any("run started" in r["message"] for r in allrows) and allrows[-1]["message"] == "custom <b>warning</b>"
    assert [r["level"] for r in dashboard.collect_logs(conn, issues=True)][-1] == "WARNING"
    assert dashboard.collect_logs(conn, after=allrows[-1]["id"]) == []
    st = dashboard.collect_stats(conn, cfg=cfg)
    assert st["ios_total"] == 0 and st["android_total"] == 1 and st["both"] == 0
    assert st["categories"] == {"done": 0, "total": 2} and st["countries"]["total"] == 2  # Fake always has a failing app
    srv = dashboard.make_server(db_target(cfg), "127.0.0.1", 0, cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    got = json.load(urllib.request.urlopen(base + "/api/logs?after=0&issues=1"))
    assert got and all(x["level"] != "INFO" for x in got)
    assert json.load(urllib.request.urlopen(base + "/api/stats"))["categories"]["total"] == 2
    srv.shutdown()
