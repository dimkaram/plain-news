"""Choose what each tab shows.

Each region tab shows that region's top stories. The All tab shows one card per
event: stories from different regions that share an article link are the same
event, so they are merged into a single card.

Run on its own to look at the choice made from the last saved AI run
(data/stories.json), without calling any model:
    python -m plainnews.views
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import datetime

from .ai import Story, StoryItem
from .config import ROOT, Config, load_config


def _timestamp(iso: str | None) -> float:
    return datetime.fromisoformat(iso).timestamp() if iso else 0.0


def _rank_key(entry):
    """Scored stories first: highest score, then wider coverage, then newest. Unscored (raw headline)
    stories come after all of those, in the order they were given."""
    index, story = entry
    if story.score is None:
        return (1, 0, 0, 0.0, index)
    return (0, -story.score, -len(story.sources), -_timestamp(story.published), index)


def rank(stories) -> list:
    return [story for _, story in sorted(enumerate(stories), key=_rank_key)]


def region_tab(stories, region: str, limit: int) -> list:
    return rank([s for s in stories if s.region == region])[:limit]


def _components(stories) -> list:
    """Groups of stories connected by a shared article link (transitively)."""
    parent = list(range(len(stories)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    owner = {}
    for index, story in enumerate(stories):
        for item in story.items:
            if item.link in owner:
                parent[find(index)] = find(owner[item.link])
            else:
                owner[item.link] = index
    groups = {}
    for index in range(len(stories)):
        groups.setdefault(find(index), []).append(index)
    return list(groups.values())


def _merge(versions: list, config: Config) -> Story:
    """One card from several versions of the same event: the most specific region's headline, summary and
    label; every source link; the highest score."""
    priority = {region: n for n, region in enumerate(config.label_priority)}
    ranked = [s for _, s in sorted(enumerate(versions), key=lambda e: (priority.get(e[1].region, 99), _rank_key(e)))]
    lead = ranked[0]

    items, seen = [], set()
    for version in ranked:
        for item in version.items:
            if item.link not in seen:
                seen.add(item.link)
                items.append(item)
    sources = list(dict.fromkeys(name for v in ranked for name in v.sources))
    scores = [v.score for v in ranked if v.score is not None]
    dates = [v.published for v in ranked if v.published]
    return replace(
        lead,
        items=items,
        sources=sources,
        score=max(scores) if scores else None,
        image_url=next((v.image_url for v in ranked if v.image_url), None),
        published=max(dates, key=_timestamp) if dates else None,
        fallback=lead.fallback and not scores,
    )


def all_tab(stories, config: Config, limit: int | None = None) -> list:
    limit = config.items_in_all if limit is None else limit
    stories = list(stories)
    merged = [_merge([stories[i] for i in group], config) for group in _components(stories)]
    return rank(merged)[:limit]


# ------------------------------------------------------------- command line

def story_from_dict(d: dict) -> Story:
    return Story(**{**d, "items": [StoryItem(**i) for i in d["items"]]})


def load_stories(path) -> list:
    return [story_from_dict(d) for d in json.loads(path.read_text(encoding="utf-8"))]


def format_tab(title: str, stories, labels: dict) -> str:
    lines = [f"== {title} ({len(stories)}) =="]
    for story in stories:
        score = "-" if story.score is None else str(story.score)
        lines.append(f"[{score:>2}] {labels.get(story.region, story.region):<8} {len(story.sources)} src  {story.headline[:72]}")
    return "\n".join(lines)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    config = load_config()
    path = ROOT / "data" / "stories.json"
    if not path.exists():
        print("No saved run found. Run  python -m plainnews.ai  first.")
        return 1
    stories = load_stories(path)
    labels = {r.id: r.label for r in config.regions}
    print(format_tab("All", all_tab(stories, config), labels))
    for region in config.regions:
        print()
        print(format_tab(region.label, region_tab(stories, region.id, config.items_per_region), labels))
    return 0


if __name__ == "__main__":
    sys.exit(main())
