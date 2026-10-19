#!/usr/bin/env python3
"""Check candidate RSS/Atom feed URLs for plain-news.

Usage:
    python scripts/check_feeds.py              # test every candidate below
    python scripts/check_feeds.py URL [URL..]  # test specific URLs only
"""
import sys
import time
from calendar import timegm

import feedparser
import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; plain-news-feedcheck/0.1)",
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
}
TIMEOUT = 20

CANDIDATES = {
    "UK / BBC News": ["https://feeds.bbci.co.uk/news/uk/rss.xml"],
    "UK / Sky News": ["https://feeds.skynews.com/feeds/rss/uk.xml"],
    "UK / Guardian": ["https://www.theguardian.com/uk/rss"],
    "UK / Daily Mail": [
        "https://www.dailymail.co.uk/news/index.rss",
        "https://www.dailymail.co.uk/articles.rss",
    ],
    "Greece / Kathimerini": [
        "https://www.kathimerini.gr/feed/",
        "https://www.kathimerini.gr/rss",
        "https://www.kathimerini.gr/rss.xml",
    ],
    "Greece / Skai (fallback)": [
        "https://www.skai.gr/feed",
        "https://www.skai.gr/rss",
    ],
    "Greece / To Vima": ["https://www.tovima.gr/feed"],
    "Greece / Ta Nea (alt)": ["https://www.tanea.gr/feed"],
    "Greece / ERT News": [
        "https://www.ertnews.gr/feed/",
        "https://www.ertnews.gr/rss",
        "https://www.ertnews.gr/feed/rss",
    ],
    "Bristol / BBC Bristol": ["https://feeds.bbci.co.uk/news/england/bristol/rss.xml"],
    "Bristol / Bristol24/7": [
        "https://www.bristol247.com/feed/",
        "https://www.bristol247.com/news-and-features/news/feed/",
    ],
    "Bristol / Bristol Post": [
        "https://www.bristolpost.co.uk/?service=rss",
        "https://www.bristolpost.co.uk/news/?service=rss",
    ],
    "World / BBC": ["https://feeds.bbci.co.uk/news/world/rss.xml"],
    "World / Guardian": ["https://www.theguardian.com/world/rss"],
    "World / Al Jazeera English": ["https://www.aljazeera.com/xml/rss/all.xml"],
    "World / DW English": [
        "https://rss.dw.com/rdf/rss-en-all",
        "https://rss.dw.com/xml/rss-en-world",
    ],
    "World / NBC News": [
        "https://feeds.nbcnews.com/nbcnews/public/news",
        "https://feeds.nbcnews.com/nbcnews/public/world",
    ],
}


def has_image(entry) -> bool:
    if entry.get("media_thumbnail") or entry.get("media_content"):
        return True
    for enc in entry.get("enclosures", []) or []:
        if str(enc.get("type", "")).startswith("image/"):
            return True
    for link in entry.get("links", []) or []:
        if link.get("rel") == "enclosure" and str(link.get("type", "")).startswith("image/"):
            return True
    return False


def entry_time(entry):
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return timegm(t) if t else None


def check(url: str) -> dict:
    result = {"url": url, "ok": False}
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException as exc:
        result["error"] = f"request failed: {type(exc).__name__}"
        return result

    result["status"] = resp.status_code
    result["final_url"] = resp.url
    if resp.status_code != 200:
        result["error"] = f"HTTP {resp.status_code}"
        return result

    feed = feedparser.parse(resp.content)
    entries = feed.entries
    result["items"] = len(entries)
    if not entries:
        result["error"] = "parsed but no items (not a feed, or empty)"
        return result

    times = [t for t in (entry_time(e) for e in entries) if t]
    if times:
        result["newest_age_h"] = round((time.time() - max(times)) / 3600, 1)
    result["with_images"] = sum(1 for e in entries if has_image(e))
    result["first_title"] = (entries[0].get("title") or "").strip()[:90]
    result["ok"] = True
    return result


def report(label: str, res: dict) -> None:
    mark = "OK  " if res["ok"] else "FAIL"
    print(f"  [{mark}] {res['url']}")
    if res.get("final_url") and res["final_url"] != res["url"]:
        print(f"         redirected to: {res['final_url']}")
    if res["ok"]:
        age = res.get("newest_age_h")
        age_txt = f"{age}h old" if age is not None else "no dates"
        print(
            f"         items={res['items']}  newest={age_txt}  "
            f"images={res['with_images']}/{res['items']}"
        )
        print(f"         first: {res['first_title']}")
    else:
        print(f"         {res.get('error', 'unknown error')}")


def main(argv) -> None:
    if argv:
        for url in argv:
            report(url, check(url))
        return

    for label, urls in CANDIDATES.items():
        print(label)
        for url in urls:
            report(label, check(url))
        print()


if __name__ == "__main__":
    main(sys.argv[1:])
