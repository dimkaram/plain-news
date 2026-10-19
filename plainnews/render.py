"""Turn the chosen stories and the run's status into the static site.

Everything on the page is escaped by the template engine, and only http(s) links and images are ever
written into it. The page makes no requests of its own beyond the publishers' images.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jinja2 import Environment, FileSystemLoader

PACKAGE = Path(__file__).resolve().parent
TEMPLATES = PACKAGE / "templates"
STATIC = PACKAGE / "static"
ICONS = {"icon-192.png": "192x192", "icon-512.png": "512x512"}
PAPER = "#f6f2ea"


def safe_url(url) -> str | None:
    """Only absolute http(s) addresses may reach the page (never javascript:, data: and the like)."""
    if not isinstance(url, str):
        return None
    parts = urlparse(url.strip())
    return url.strip() if parts.scheme in ("http", "https") and parts.netloc else None


def local_timezone(name: str):
    """The configured timezone, or UTC with a warning if the system has no timezone data."""
    try:
        return ZoneInfo(name), None
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return timezone.utc, f"Timezone data for {name} was not found, so times are shown in UTC."


def time_text(moment: datetime, tz: tzinfo, now: datetime) -> str:
    """The clock time shown before (or without) JavaScript: '07:02', or '18 Oct 07:02' for another day."""
    local = moment.astimezone(tz)
    if local.date() == now.astimezone(tz).date():
        return local.strftime("%H:%M")
    return f"{local.day} {local.strftime('%b %H:%M')}"


def build_card(story, labels: dict, tz: tzinfo, now: datetime) -> dict | None:
    link = safe_url(story.link) or next((u for u in (safe_url(i.link) for i in story.items) if u), None)
    if link is None:
        return None

    sources = {}
    for item in sorted(story.items, key=lambda i: 0 if i.link == story.link else 1):   # the lead's source first
        url = safe_url(item.link)
        if url and item.source not in sources:
            sources[item.source] = {"name": item.source, "url": url, "paywalled": bool(item.paywalled)}
    if not sources:
        sources[story.sources[0] if story.sources else "Source"] = {"name": story.sources[0] if story.sources else "Source",
                                                                     "url": link, "paywalled": False}

    moment = datetime.fromisoformat(story.published) if story.published else None
    return {
        "headline": story.headline,
        "link": link,
        "summary": story.summary,
        "image": safe_url(story.image_url),
        "lang": story.language,
        "region_label": labels.get(story.region, story.region),
        "time_iso": moment.astimezone(timezone.utc).isoformat() if moment else "",
        "time_text": time_text(moment, tz, now) if moment else "",
        "sources": list(sources.values()),
    }


def build_tabs(tab_stories: list, labels: dict, tz: tzinfo, now: datetime) -> list:
    """tab_stories: [(id, label, stories, show_region)] in display order."""
    tabs = []
    for tab_id, label, stories, show_region in tab_stories:
        cards = [c for c in (build_card(s, labels, tz, now) for s in stories) if c]
        tabs.append({"id": tab_id, "label": label, "cards": cards, "show_region": show_region})
    return tabs


def manifest(config) -> dict:
    return {
        "name": config.site_title,
        "short_name": config.site_title[:12],
        "description": config.site_description,
        "lang": config.default_language,
        "start_url": "./",
        "scope": "./",
        "display": "standalone",
        "background_color": PAPER,
        "theme_color": PAPER,
        "icons": [{"src": name, "sizes": ICONS[name], "type": "image/png", "purpose": "any maskable"} for name in ICONS],
    }


def render_page(config, tab_stories: list, status, now: datetime, version: str = "") -> str:
    tz, _ = local_timezone(config.timezone)
    labels = {r.id: r.label for r in config.regions}
    local = now.astimezone(tz)
    date_line = f"{local.strftime('%A')} {local.day} {local.strftime('%B')} {local.year}"
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True, keep_trailing_newline=True)
    return env.get_template("index.html.j2").render(
        lang=config.default_language,
        site_title=config.site_title,
        description=config.site_description,
        css=(TEMPLATES / "style.css").read_text(encoding="utf-8"),
        js=(TEMPLATES / "page.js").read_text(encoding="utf-8"),
        date_line=date_line,
        date_line_plain=date_line,
        updated=local.strftime("%H:%M"),
        note=status.note,
        tabs=build_tabs(tab_stories, labels, tz, now),
        status=status,
        model=str(config.ai.get("model", "")).strip(),
        repo_url=safe_url(config.repo_url) or "",
        footer_note=config.footer_note,
        version=f"v{version}" if version else "",
    )


def write_site(out_dir, html: str, config) -> list:
    """Write index.html, the manifest and the icons. Returns the file names written."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(html, encoding="utf-8")
    (out / "manifest.webmanifest").write_text(json.dumps(manifest(config), indent=2, ensure_ascii=False) + "\n",
                                              encoding="utf-8")
    for name in ICONS:
        shutil.copyfile(STATIC / name, out / name)
    return ["index.html", "manifest.webmanifest", *ICONS]
