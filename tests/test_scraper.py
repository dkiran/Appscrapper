import csv
from scraper.country import country_from_address
from scraper.run import scrape

CFG = lambda tmp: {"db_path": str(tmp / "a.db"), "csv_path": str(tmp / "a.csv"),
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
    import sqlite3
    from scraper.run import resolve_countries
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
    con = sqlite3.connect(cfg["db_path"]); con.execute("UPDATE searches SET done_at='2000-01-01'"); con.commit()
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
    import sqlite3
    from scraper import db
    from scraper.run import due_searches
    conn = db.connect(str(tmp_path / "o.db"))
    order = due_searches(conn, ["us", "in", "ke"], ["Business", "Finance"], 30)
    assert order == [("us", "Business"), ("in", "Business"), ("ke", "Business"),
                     ("us", "Finance"), ("in", "Finance"), ("ke", "Finance")]
