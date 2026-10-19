"""Everything the status log and the header note say, worked out from the run's results.

Pure functions: no network, no files. The page only prints what is computed here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class SourceRow:
    region: str
    name: str
    ok: bool
    detail: str
    stale: bool = False


@dataclass
class RegionRow:
    label: str
    text: str
    fallback: bool


@dataclass
class Status:
    warnings: list = field(default_factory=list)   # most important first
    note: str = ""                                  # short form, shown in the page header
    sources: list = field(default_factory=list)
    sources_ok: int = 0
    sources_total: int = 0
    regions: list = field(default_factory=list)
    images_line: str = ""
    empty_summaries: int = 0
    version_line: str = ""


def _half_up(x: float) -> int:
    return int(x + 0.5)


def age_text(hours) -> str:
    """'36 min', '5 h' or '3 days'; halves round up (Python's round() would send 2.5 to 2)."""
    if hours is None:
        return "no dates"
    if hours < 1:
        return f"{max(1, _half_up(hours * 60))} min"
    if hours < 48:
        return f"{_half_up(hours)} h"
    return f"{_half_up(hours / 24)} days"


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def commit_age_days(now: datetime, commit_time: datetime | None):
    return None if commit_time is None else max(0, (now - commit_time).days)


def compute_status(config, feed_results, ai_reports, image_counts, shown, now, version="unknown",
                   commit_hash=None, commit_time=None, extra_warnings=()) -> Status:
    labels = {r.id: r.label for r in config.regions}
    status = Status()

    # ---- sources
    failed = stale = 0
    warnings_failed, warnings_stale = [], []
    for r in feed_results:
        region = labels.get(r.region, r.region)
        if not r.ok:
            failed += 1
            detail = f"unavailable ({r.error})"
            warnings_failed.append(f"{r.source} ({region}) is unavailable: {r.error}.")
            status.sources.append(SourceRow(region, r.source, False, detail))
            continue
        is_stale = r.newest_age_hours is not None and r.newest_age_hours > config.stale_after_hours
        detail = f"{r.items_kept} of {r.items_in_feed} items, newest {age_text(r.newest_age_hours)} ago"
        if is_stale:
            stale += 1
            detail += " (stale)"
            warnings_stale.append(f"{r.source} ({region}) looks stale: its newest item is {age_text(r.newest_age_hours)} old.")
        status.sources.append(SourceRow(region, r.source, True, detail, is_stale))
    status.sources_total = len(feed_results)
    status.sources_ok = status.sources_total - failed

    # ---- summaries per region
    fallback_regions = 0
    warnings_ai = []
    for report in ai_reports:
        label = labels.get(report.region, report.region)
        if report.fallback:
            fallback_regions += 1
            warnings_ai.append(f"{label}: summaries unavailable ({report.error}); showing raw headlines.")
            text = f"{_plural(report.stories, 'story', 'stories')}, raw headlines only ({report.error})"
        else:
            text = _plural(report.stories, "story", "stories")
        status.regions.append(RegionRow(label, text, report.fallback))

    # ---- images and summaries over the stories actually shown
    status.images_line = (f"{image_counts.get('from_feed', 0)} from feeds, {image_counts.get('from_page', 0)} "
                          f"from article pages, {image_counts.get('missing', 0)} missing")
    unique = {s.link: s for s in shown}.values()
    status.empty_summaries = sum(1 for s in unique if not s.fallback and not s.summary)

    # ---- version and commit age
    days = commit_age_days(now, commit_time)
    parts = [f"v{version}" + (f" ({commit_hash})" if commit_hash else "")]
    warnings_commit = []
    if days is not None:
        parts.append("last commit today" if days == 0 else f"last commit {_plural(days, 'day')} ago")
        if days >= config.commit_warning_days:
            warnings_commit.append(
                f"Scheduled updates stop after 60 days without repository activity, and the last commit was "
                f"{_plural(days, 'day')} ago. Commit something to restart the clock.")
    status.version_line = " · ".join(parts)

    status.warnings = [*extra_warnings, *warnings_failed, *warnings_ai, *warnings_stale, *warnings_commit]

    # ---- the short note for the page header
    note = []
    if failed:
        note.append(f"{_plural(failed, 'source')} unavailable")
    if fallback_regions:
        note.append(f"{_plural(fallback_regions, 'region')} showing raw headlines")
    if stale:
        note.append(f"{stale} stale")
    if warnings_commit:
        note.append("commit needed to keep updates running")
    status.note = " · ".join(note)
    return status
