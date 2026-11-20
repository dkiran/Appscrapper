"""Match a Google Play app to its App Store listing using Apple's public iTunes Search API results."""
import re
from difflib import SequenceMatcher
from urllib.parse import urlparse

_SUFFIX = re.compile(r"\b(pvt|private|ltd|limited|llc|inc|corp|co|gmbh|sa|ag|llp|plc|technologies|technology|software|apps?|labs?)\b\.?")


def _norm(s):
    s = re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())
    return re.sub(r"\s+", " ", _SUFFIX.sub(" ", s)).strip()


def _domain(url):
    if not url:
        return ""
    host = urlparse(url if "//" in url else "//" + url).netloc.lower()
    host = host.split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _root(domain):
    parts = domain.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov", "ac"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def clean_url(url):
    """App Store links without tracking query strings, so the same app always compares equal."""
    return (url or "").split("?")[0]


def best_match(title, developer, website, candidates, bundle_id=None):
    """candidates: iTunes result dicts; bundle_id: the Play package name. Returns (candidate, reason) or (None, '')."""
    t, dev, site = _norm(title), _norm(developer), _root(_domain(website))
    best = (None, "", 0)
    for c in candidates or []:
        if bundle_id and (c.get("bundleId") or "").lower() == bundle_id.lower():
            return c, "bundle-id"  # same ID on both stores: certain
        ct = _norm(c.get("trackName"))
        cdev = _norm(c.get("sellerName") or c.get("artistName"))
        csite = _root(_domain(c.get("sellerUrl")))
        title_sim = SequenceMatcher(None, t, ct).ratio() if t and ct else 0
        dev_sim = SequenceMatcher(None, dev, cdev).ratio() if dev and cdev else 0
        same_site = bool(site and csite and site == csite)
        if same_site and title_sim >= 0.6:
            score, why = 3 + title_sim, "website+title"
        elif title_sim >= 0.85 and dev_sim >= 0.8:
            score, why = 2 + title_sim, "title+developer"
        else:
            continue
        if score > best[2]:
            best = (c, why, score)
    return best[0], best[1]


def similar(a, b):
    """0..1 similarity of two app/company names after dropping legal suffixes and punctuation."""
    a, b = _norm(a), _norm(b)
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0
