"""Fetch and normalise feeds.

A bad feed never stops the run: every failure is recorded in a FeedResult
(shown later in the page's status log) and the other sources carry on.

Run on its own to test the real feeds:  python -m plainnews.fetch
"""
from __future__ import annotations

import html
import json
import re
import sys
import time
import unicodedata
from calendar import timegm
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import feedparser
import requests
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import ROOT, Config, Source, load_config

_TAG_RE = re.compile(r"<[^>]+>")
_SENTENCE_RE = re.compile(r"(?<=[.!?\u2026;])\s+")
# zero-width characters, direction marks, the byte-order mark and soft hyphens: invisible, never wanted
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u00ad"), None)
_SPACE_RE = re.compile(r"\s+")
_IMG_RE = re.compile(r"""<img\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", re.I)
_IMAGE_EXT_RE = re.compile(r"\.(jpe?g|png|webp|gif|avif)(\?|$)", re.I)


@dataclass
class Item:
    title: str
    link: str
    published: datetime | None  # UTC; None if the feed gave no date
    source: str
    region: str
    language: str
    paywalled: bool
    rank: int  # position in the feed, 0 = first
    snippet: str = ""
    image_url: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["published"] = self.published.isoformat() if self.published else None
        return d


@dataclass
class FeedResult:
    source: str
    region: str
    feed_url: str
    ok: bool
    error: str | None = None
    items_in_feed: int = 0
    items_kept: int = 0
    newest_age_hours: float | None = None
    with_images: int = 0
    seconds: float = 0.0


class _Retryable(Exception):
    """A failure worth retrying: timeout, connection error, HTTP 429 or 5xx."""


# ---------- downloading ----------

def _download(url: str, cfg: dict) -> requests.Response:
    headers = {
        "User-Agent": cfg["user_agent"],
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    }
    backoff = cfg["backoff_seconds"]
    resp = None
    for attempt in Retrying(
        stop=stop_after_attempt(cfg["retries"]),
        wait=wait_exponential(multiplier=backoff, min=backoff, max=8),
        retry=retry_if_exception_type(_Retryable),
        reraise=True,
    ):
        with attempt:
            try:
                resp = requests.get(url, headers=headers, timeout=cfg["timeout_seconds"])
            except requests.Timeout:
                raise _Retryable("timeout") from None
            except requests.RequestException:
                raise _Retryable("connection error") from None
            if resp.status_code == 429 or resp.status_code >= 500:
                raise _Retryable(f"HTTP {resp.status_code}")
    return resp


# ---------- normalising ----------

def _clean_text(value) -> str:
    if not value:
        return ""
    text = html.unescape(_TAG_RE.sub(" ", str(value))).translate(_INVISIBLE)
    return _SPACE_RE.sub(" ", text).strip()


def _fold(text: str) -> str:
    """Lower-case and accent-free, so Greek accents and capitals never matter."""
    decomposed = unicodedata.normalize("NFD", text.casefold())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


def _prepare_phrases(phrases) -> tuple:
    return tuple(p for p in (_fold(str(x).strip()) for x in phrases) if p)


def _strip_boilerplate(text: str, phrases: tuple) -> str:
    """Remove every sentence that contains one of the boilerplate phrases."""
    if not text or not phrases:
        return text
    kept = [s for s in _SENTENCE_RE.split(text) if not any(p in _fold(s) for p in phrases)]
    return " ".join(kept)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-") + "\u2026"


def _published(entry) -> datetime | None:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if not t:
        return None
    return datetime.fromtimestamp(timegm(t), tz=timezone.utc)


def _width(media: dict) -> int | None:
    try:
        return int(media.get("width"))
    except (TypeError, ValueError):
        return None


def _is_image(media: dict) -> bool:
    if media.get("medium") == "image" or str(media.get("type", "")).startswith("image/"):
        return True
    return not media.get("medium") and not media.get("type") and bool(_IMAGE_EXT_RE.search(media.get("url", "")))


def _pick(candidates):
    """Smallest image that is at least 300px wide, else the largest, else the first."""
    candidates = [(u, w) for u, w in candidates if u]
    if not candidates:
        return None
    sized = [c for c in candidates if c[1]]
    if sized:
        big = [c for c in sized if c[1] >= 300]
        return (min(big, key=lambda c: c[1]) if big else max(sized, key=lambda c: c[1]))[0]
    return candidates[0][0]


def _html_image(entry, base: str) -> str | None:
    blobs = [c.get("value", "") for c in entry.get("content") or []]
    blobs += [entry.get("summary", "")]
    for blob in blobs:
        for match in _IMG_RE.finditer(blob or ""):
            src = html.unescape(match.group(1)).strip()
            if src and not src.startswith("data:"):
                return urljoin(base, src)
    return None


def _find_image(entry, base: str) -> str | None:
    thumbs = [(m.get("url"), _width(m)) for m in entry.get("media_thumbnail") or []]
    contents = [(m.get("url"), _width(m)) for m in entry.get("media_content") or [] if _is_image(m)]
    enclosures = [
        (e.get("href") or e.get("url"), None)
        for e in entry.get("enclosures") or []
        if str(e.get("type", "")).startswith("image/")
    ]
    url = _pick(thumbs) or _pick(contents) or _pick(enclosures) or _html_image(entry, base)
    if not url:
        return None
    url = urljoin(base, url)
    # An https page cannot show http images, so ask for the https version.
    return "https://" + url[len("http://"):] if url.startswith("http://") else url


def _normalise(entry, source: Source, rank: int, snippet_chars: int, phrases: tuple = ()) -> Item | None:
    title = _clean_text(entry.get("title"))
    link = (entry.get("link") or "").strip()
    if not title or not link:
        return None
    link = urljoin(source.feed_url, link)
    snippet = _clean_text(entry.get("summary") or entry.get("description"))
    snippet = _truncate(_strip_boilerplate(snippet, phrases), snippet_chars)
    return Item(
        title=title,
        link=link,
        published=_published(entry),
        source=source.name,
        region=source.region,
        language=source.language,
        paywalled=source.paywalled,
        rank=rank,
        snippet=snippet,
        image_url=_find_image(entry, link),
    )


# ---------- fetching ----------

def fetch_feed(source: Source, config: Config, now: datetime):
    """Fetch one source. Returns (FeedResult, items). Never raises."""
    started = time.monotonic()
    result = FeedResult(source=source.name, region=source.region, feed_url=source.feed_url, ok=False)
    items: list[Item] = []
    try:
        resp = _download(source.feed_url, config.fetch)
        if resp.status_code != 200:
            result.error = f"HTTP {resp.status_code}"
            return result, items

        entries = feedparser.parse(resp.content).entries
        result.items_in_feed = len(entries)
        if not entries:
            result.error = "no items"
            return result, items

        chars = int(config.fetch["snippet_chars"])
        phrases = _prepare_phrases(config.fetch["boilerplate"])
        normalised = [i for i in (_normalise(e, source, rank, chars, phrases) for rank, e in enumerate(entries)) if i]
        dates = [i.published for i in normalised if i.published]
        if dates:
            result.newest_age_hours = round((now - max(dates)).total_seconds() / 3600, 1)

        cutoff = now - timedelta(hours=config.max_age_hours)
        seen = set()
        for item in normalised:
            if item.link in seen or (item.published and item.published < cutoff):
                continue
            seen.add(item.link)
            items.append(item)
            if len(items) >= source.max_items_considered:
                break

        result.items_kept = len(items)
        result.with_images = sum(1 for i in items if i.image_url)
        if not items:
            result.error = "no recent items"
            return result, items
        result.ok = True
    except _Retryable as exc:
        result.error = str(exc)
    except Exception as exc:  # a broken feed must never stop the build
        result.error = f"unexpected: {type(exc).__name__}"
    finally:
        result.seconds = round(time.monotonic() - started, 1)
    return result, items


def fetch_all(config: Config, now: datetime | None = None):
    """Fetch every enabled source. Returns (items, results), in config order."""
    now = now or datetime.now(timezone.utc)
    sources = config.active_sources()
    workers = max(1, int(config.fetch["max_workers"]))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(lambda s: fetch_feed(s, config, now), sources))
    items = [item for _, source_items in outcomes for item in source_items]
    return items, [result for result, _ in outcomes]


# ---------- command line ----------

def format_report(results) -> str:
    lines = []
    for r in results:
        head = f"[{'OK  ' if r.ok else 'FAIL'}] {r.region:<8} {r.source:<20}"
        if r.ok:
            age = f"{r.newest_age_hours}h" if r.newest_age_hours is not None else "n/a"
            lines.append(f"{head} kept {r.items_kept}/{r.items_in_feed}  newest {age}  images {r.with_images}/{r.items_kept}")
        else:
            extra = f"  (newest {r.newest_age_hours}h old)" if r.newest_age_hours is not None else ""
            lines.append(f"{head} {r.error}{extra}")
    return "\n".join(lines)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    config = load_config()
    items, results = fetch_all(config)
    print(format_report(results))

    out = ROOT / "data"
    out.mkdir(exist_ok=True)
    (out / "items.json").write_text(
        json.dumps([i.to_dict() for i in items], ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "fetch_report.json").write_text(
        json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=1), encoding="utf-8")

    failed = sum(1 for r in results if not r.ok)
    print(f"\n{len(items)} items from {len(results) - failed}/{len(results)} sources -> data/items.json")
    return 0 if items else 1


if __name__ == "__main__":
    sys.exit(main())
