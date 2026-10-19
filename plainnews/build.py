"""Run the whole pipeline and write the site.

    python -m plainnews.build                      fetch feeds, summarise, build site/
    python -m plainnews.build --from-saved         rebuild site/ from the last saved run
                                                   (no feeds, no AI calls, no page lookups)
    python -m plainnews.build --from-saved --with-images
                                                   the same, plus article-page image lookups
    python -m plainnews.build --out DIR            write somewhere other than site/

A run that cannot fetch a single item fails without touching the output folder, so the previously
published page stays live. A run where only the AI step fails still builds a page, from raw headlines.
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict, fields
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from .ai import GroupReport, Story, format_ai_report, run_ai
from .config import ROOT, Config, load_config
from .fetch import FeedResult, fetch_all, format_report
from .images import fill_missing
from .render import local_timezone, render_page, write_site
from .status import compute_status
from .views import all_tab, load_stories, region_tab


class BuildError(Exception):
    """The build cannot produce a useful page; nothing is written."""


# ------------------------------------------------------------ version and git

def read_version(root: Path = ROOT) -> str:
    try:
        return (root / "VERSION").read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def git_info(root: Path = ROOT):
    """(short commit id, commit time) of the checked-out commit, or (None, None) if git cannot say."""
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%h %ct"], cwd=root, capture_output=True, text=True,
                             timeout=15, check=True).stdout.split()
        return out[0], datetime.fromtimestamp(int(out[1]), tz=timezone.utc)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None, None


# ------------------------------------------------------------------- choosing

def pick_tabs(stories: list, config: Config) -> list:
    """[(id, label, stories, show_region)]: All first, then every configured region, in order."""
    tabs = [("all", "All", all_tab(stories, config), True)]
    for region in config.regions:
        tabs.append((region.id, region.label, region_tab(stories, region.id, config.items_per_region), False))
    return tabs


def shown_stories(tabs: list) -> list:
    return [story for _, _, stories, _ in tabs for story in stories]


def ensure_images(tabs: list, config: Config) -> dict:
    """Look up article-page images for shown stories that have none. Returns counts for the status log."""
    shown = shown_stories(tabs)
    by_link = {}
    for story in shown:
        by_link.setdefault(story.link, []).append(story)
    from_feed = sum(1 for group in by_link.values() if group[0].image_url)

    wanted = []
    for link, group in by_link.items():          # All-tab stories come first, so the request cap favours them
        if not group[0].image_url:
            lead = next((i for i in group[0].items if i.link == link), None)
            wanted.append(SimpleNamespace(link=link, image_url=None, paywalled=bool(lead and lead.paywalled)))
    fill_missing(wanted, config)

    found = {w.link: w.image_url for w in wanted if w.image_url}
    for link, url in found.items():
        for story in by_link[link]:
            story.image_url = story.image_url or url
    return {"from_feed": from_feed, "from_page": len(found), "missing": len(by_link) - from_feed - len(found)}


# ------------------------------------------------------------------- building

def build_site(config: Config, stories: list, feed_results: list, ai_reports: list, out_dir, now: datetime,
               look_up_images: bool = True) -> dict:
    tabs = pick_tabs(stories, config)
    if look_up_images:
        counts = ensure_images(tabs, config)
    else:
        unique = {s.link: s for s in shown_stories(tabs)}
        counts = {"from_feed": sum(1 for s in unique.values() if s.image_url), "from_page": 0,
                  "missing": sum(1 for s in unique.values() if not s.image_url)}

    _, tz_warning = local_timezone(config.timezone)
    commit_hash, commit_time = git_info()
    version = read_version()
    status = compute_status(config, feed_results, ai_reports, counts, shown_stories(tabs), now, version=version,
                            commit_hash=commit_hash, commit_time=commit_time,
                            extra_warnings=[tz_warning] if tz_warning else [])
    html = render_page(config, tabs, status, now, version=version)
    written = write_site(out_dir, html, config)
    return {"tabs": tabs, "status": status, "files": written, "image_counts": counts}


def save_run(stories: list, feed_results: list, ai_reports: list, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    dump = lambda name, rows: (directory / name).write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    dump("stories.json", [s.to_dict() for s in stories])
    dump("fetch_report.json", [asdict(r) for r in feed_results])
    dump("ai_report.json", [asdict(r) for r in ai_reports])


def _load_rows(path: Path, cls) -> list:
    if not path.exists():
        return []
    known = {f.name for f in fields(cls)}
    return [cls(**{k: v for k, v in row.items() if k in known}) for row in json.loads(path.read_text(encoding="utf-8"))]


def run(config: Config, out_dir, now: datetime | None = None, provider=None, data_dir: Path | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    items, feed_results = fetch_all(config, now)
    print(format_report(feed_results))
    if not items:
        raise BuildError("no items could be fetched from any source, so nothing was built")

    result = run_ai(items, config, provider=provider, now=now)
    print()
    print(format_ai_report(result.reports))
    if data_dir is not None:
        save_run(result.stories, feed_results, result.reports, data_dir)
    return build_site(config, result.stories, feed_results, result.reports, out_dir, now)


def run_saved(config: Config, out_dir, data_dir: Path, now: datetime | None = None, with_images: bool = False) -> dict:
    now = now or datetime.now(timezone.utc)
    stories_path = data_dir / "stories.json"
    if not stories_path.exists():
        raise BuildError("no saved run found; run  python -m plainnews.ai  or  python -m plainnews.build  first")
    stories = load_stories(stories_path)
    feeds = _load_rows(data_dir / "fetch_report.json", FeedResult)
    reports = _load_rows(data_dir / "ai_report.json", GroupReport)
    return build_site(config, stories, feeds, reports, out_dir, now, look_up_images=with_images)


# ------------------------------------------------------------------ command line

def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    out_dir = ROOT / "site"
    if "--out" in argv:
        out_dir = Path(argv[argv.index("--out") + 1])
    config = load_config()
    try:
        if "--from-saved" in argv:
            built = run_saved(config, out_dir, ROOT / "data", with_images="--with-images" in argv)
        else:
            built = run(config, out_dir, data_dir=ROOT / "data")
    except BuildError as exc:
        print(f"Build failed: {exc}")
        return 1

    status = built["status"]
    print(f"\nSite written to {out_dir}: {', '.join(built['files'])}")
    print(f"Sources {status.sources_ok}/{status.sources_total} working · images: {status.images_line}")
    for warning in status.warnings:
        print(f"WARNING: {warning}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
