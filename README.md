# Play Store app scraper

Scheduled scraper that collects Google Play apps into SQLite and exports
`data/apps.csv` with columns:
`Email, App, Company, Website, Category, Play Store URL, Developer email, Developer address, Country`.

```bash
pip install -r requirements.txt
python main.py --once      # run one scrape now
python main.py             # stay running, scrape on config.json "schedule_cron" (default 02:00 UTC daily)
pytest                     # offline tests
```

System cron alternative: `0 2 * * * cd /path/playstore-scraper && python main.py --once`

## Config (`config.json`)
- `search_terms` + `countries`: how apps are discovered (Play has no "list all apps" endpoint).
- `max_new_apps_per_run`, `delay_seconds`: throttle. Keep them conservative to avoid blocks.
- Nothing is scraped twice: stored apps are never fetched again, and a finished (term, country) search is skipped on later runs, so each scheduled run works through the *next* searches. When all are done it says so; add terms/categories/countries for more. `refresh_after_days` (default `null` = never) can be set to re-fetch old apps.
- Nothing scraped is dropped: `apps.csv` holds every app. `require_ios` and `developer_countries` only filter `contacts.csv`. Apps whose App Store check failed or never ran are re-checked at the start of later runs (`ios_recheck_per_run`).

## Notes
- `Email` = the "App support" email. `Developer email` = a different email in the page's "About the developer" block (blank if none, or if it equals the support email). Run `python main.py --inspect <package>` to see every bare email on a page and its position in the data if a result looks wrong.
- `Country` is parsed from the developer address (blank if it can't be recognised); the store region crawled is kept in the DB as `crawl_region`.
- Scraping Play pages is against Google's Terms of Service and may be rate-limited or blocked; use at your own risk, and handle collected emails in line with applicable privacy/anti-spam law.
- Not verified against live Play Store (network was blocked where this was built); only the offline tests have been run.

## Category-wise and country-wise
- `python main.py --list-categories` prints every category id (32 app + 17 game categories).
- `"categories"` in `config.json`: `"ALL"`, `"APPS"`, `"GAMES"` or a list such as `["FINANCE", "MEDICAL"]`. Each category name is searched as a term, in addition to `search_terms`. The Category column is always the app's real category.
- `"countries"`: Play Store regions searched (two-letter codes, e.g. `["us", "in", "ke"]`); results differ by region.
- `"developer_countries"`: e.g. `["India", "Kenya"]` limits `contacts.csv` to developers whose address ends in that country (empty = all).
- Raise `max_new_apps_per_run` and `results_per_query` for wider coverage; search returns only a limited number of hits per term, so more terms = more apps.

## Both stores + campaign list
- `"check_ios": true` looks each app up in Apple's public iTunes Search API and fills `App Store URL` and `iOS match` (`website+title` or `title+developer`; weaker matches are rejected). `"require_ios": true` limits `contacts.csv` to apps found on both stores. Lookups are throttled by `ios_delay_seconds` (Apple allows roughly 20 requests/minute).
- `data/contacts.csv` has one row per company and support email (with its apps listed), so a campaign sends each company one email rather than one per app. Use this file for outreach, not `apps.csv`.
- Matching is a best guess by name, developer and website; spot-check a sample before mailing.

## Before you email anyone
Cold B2B email is lawful in many places but regulated: include a real sender identity and postal address, a working unsubscribe link that you honour, and no misleading subject lines (CAN-SPAM). The EU/UK (GDPR/PECR), Canada (CASL) and India (DPDP) have stricter or different rules; prefer role addresses (support@, contact@) over personal ones, keep a suppression list, and get advice for the markets you target. Scraping Google Play also breaches its Terms of Service.

## Filtering for a campaign
Both CSVs end with `Play Store URL`/`App Store URL` (blank when the app isn't on that store) and a `Both Stores` column (`Yes`/`No`). In `contacts.csv` filter on `Both Stores = Yes` for apps on both stores, or on whichever URL column is non-blank for Google-Play-only / App-Store-only lists. `require_ios` is `false` by default, so `contacts.csv` contains everyone and you filter in your email tool; set it `true` to limit the file to both-store developers.

## Daily full coverage
Defaults: `"categories": "ALL"` (all 49 categories), `"countries": "ALL"` (every ISO country code; ones Play doesn't serve fail 3 times and are then skipped until the repeat window), no per-run app limit, and a 10-hour time cap (`max_runtime_minutes`). Each daily run does the due searches in order and stops at the time cap; the next run continues. A finished search comes due again after `search_repeat_days` (30) to pick up new apps, but apps already stored are never fetched again. Expect the first full pass (about 49 x 249 searches plus every app and App Store lookup) to take many days; lower `countries` to a list like `["us","gb","in"]` for a faster start.

## Staying under the radar
Delays are randomised (about 0.7x to 1.5x of `delay_seconds`, default 4s), a run is capped at `max_new_apps_per_run` (1500) and `max_runtime_minutes`, and it stops itself after `max_consecutive_failures` (10) failed requests in a row, e.g. a 429 block, then continues on the next scheduled run. Raise volume gradually only if runs finish without failures.

## Order of work
Searches run one category at a time across **every** country, in the order listed in `"categories"` (default: Business, Finance, Shopping, Food & Drink, Travel & Local, Lifestyle, Education, Health & Fitness), then the next category. So after the first category every country has been covered once. Put a single category in the list to cover only that one.
