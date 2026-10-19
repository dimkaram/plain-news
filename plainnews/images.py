"""Find a preview image for stories whose feed did not supply one.

Step 3 of the image lookup: read the article page's og:image tag. It reads
only the start of the page (up to the end of <head>), never the article text,
skips paywalled sources, is capped per run, and never retries. A failure just
means the card stays text-only.
"""
from __future__ import annotations

import codecs
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin

import requests

from .config import Config, load_config

MAX_PAGE_BYTES = 200_000
CHUNK_BYTES = 16_384
_OG_NAMES = ("og:image", "og:image:secure_url")
_TWITTER_NAMES = ("twitter:image", "twitter:image:src")


@dataclass
class ImageStats:
    already_had: int = 0
    attempted: int = 0
    found: int = 0
    failed: int = 0
    skipped_paywalled: int = 0
    skipped_over_cap: int = 0


class _MetaImageParser(HTMLParser):
    """Collects og:image / twitter:image from <meta> tags; stops caring at <body>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.og = None
        self.twitter = None
        self.done = False

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        if tag == "body":
            self.done = True
        elif tag == "meta":
            a = {k.lower(): (v or "").strip() for k, v in attrs}
            key = (a.get("property") or a.get("name") or "").lower()
            content = a.get("content", "")
            if not content:
                return
            if key in _OG_NAMES and not self.og:
                self.og = content
            elif key in _TWITTER_NAMES and not self.twitter:
                self.twitter = content
            if self.og:
                self.done = True

    def handle_endtag(self, tag):
        if tag == "head":
            self.done = True


def _clean(url: str | None, base: str) -> str | None:
    if not url or url.startswith("data:"):
        return None
    url = urljoin(base, url)
    if not url.startswith(("http://", "https://")):
        return None
    return "https://" + url[len("http://"):] if url.startswith("http://") else url


def fetch_og_image(url: str, config: Config) -> str | None:
    """Return the page's preview image URL, or None. Never raises."""
    try:
        with requests.get(
            url,
            headers={"User-Agent": config.fetch["user_agent"], "Accept": "text/html,*/*"},
            timeout=config.images["timeout_seconds"],
            stream=True,
        ) as resp:
            if resp.status_code != 200:
                return None
            if "html" not in resp.headers.get("Content-Type", "html").lower():
                return None
            parser = _MetaImageParser()
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            read = 0
            for chunk in resp.iter_content(chunk_size=CHUNK_BYTES):
                read += len(chunk)
                parser.feed(decoder.decode(chunk))
                if parser.done or read >= MAX_PAGE_BYTES:
                    break
            return _clean(parser.og or parser.twitter, resp.url)
    except Exception:
        return None


def fill_missing(items, config: Config) -> ImageStats:
    """Fill image_url on items that have none. `items` should be in priority order.

    Works on anything with .link, .image_url and .paywalled (feed items now,
    selected stories later). Only the first `max_page_fetches_per_run` pages
    are fetched; the rest stay text-only.
    """
    stats = ImageStats()
    settings = config.images
    wanted = []
    seen = set()
    for item in items:
        if item.image_url:
            stats.already_had += 1
        elif item.paywalled:
            stats.skipped_paywalled += 1
        elif item.link not in seen:
            seen.add(item.link)
            wanted.append(item)

    if not settings["og_image_fallback"]:
        return stats

    cap = int(settings["max_page_fetches_per_run"])
    stats.skipped_over_cap = max(0, len(wanted) - cap)
    wanted = wanted[:cap]
    stats.attempted = len(wanted)

    with ThreadPoolExecutor(max_workers=4) as pool:
        found = list(pool.map(lambda it: fetch_og_image(it.link, config), wanted))

    for item, url in zip(wanted, found):
        if url:
            item.image_url = url
            stats.found += 1
        else:
            stats.failed += 1
    # Other items for the same article link get the same image.
    by_link = {it.link: it.image_url for it in wanted if it.image_url}
    for item in items:
        if not item.image_url and item.link in by_link:
            item.image_url = by_link[item.link]
    return stats


def main() -> int:
    """Live test: fetch the feeds, then look up images for items that lack one."""
    from .fetch import fetch_all

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    config = load_config()
    items, _ = fetch_all(config)
    missing = [i for i in items if not i.image_url]
    print(f"{len(items)} items fetched, {len(missing)} without a feed image")
    stats = fill_missing(missing, config)

    groups = {}
    for i in missing:
        groups.setdefault((i.region, i.source), []).append(i)
    for (region, source), group in groups.items():
        got = [i for i in group if i.image_url]
        print(f"{region:<8} {source:<20} page image found for {len(got)}/{len(group)}")
        if got:
            print(f"         e.g. {got[0].image_url}")
    print(f"\nattempted {stats.attempted}, found {stats.found}, failed {stats.failed}, "
          f"left over (cap) {stats.skipped_over_cap}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
