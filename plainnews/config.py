"""Load and validate sources.yaml."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "sources.yaml"

AI_DEFAULTS = {
    "provider": "gemini",
    "model": "",
    "api_key_env": "GEMINI_API_KEY",
    "summary_max_sentences": 2,
    "max_stories_per_region": 25,
    "max_calls_per_run": 12,
    "max_output_tokens": 8192,
    "timeout_seconds": 90,
    "retries": 3,
    "backoff_seconds": 2,
    "coverage_bonus_per_source": 1,
    "coverage_bonus_max": 2,
}
IMAGE_DEFAULTS = {
    "og_image_fallback": True,
    "max_page_fetches_per_run": 40,
    "timeout_seconds": 8,
}
# Sentences containing any of these (ignoring case and accents) are removed from snippets before the AI
# sees them: support-line notices, appeals, promotional lines and feed boilerplate.
DEFAULT_BOILERPLATE = (
    "samaritans", "helpline", "if you need help", "if you are affected by", "get in touch",
    "subscribe to", "subscribe now", "newsletter", "read more", "click here", "continue reading",
    "follow us", "all rights reserved", "appeared first on", "download the app",
    "διαβάστε περισσότερα", "δείτε περισσότερα", "μάθετε περισσότερα", "διαβάστε επίσης",
    "ακολουθήστε μας", "εγγραφείτε", "εμφανίστηκε πρώτα", "γραμμή βοήθειας",
)

FETCH_DEFAULTS = {
    "timeout_seconds": 20,
    "retries": 3,
    "backoff_seconds": 1,
    "max_workers": 6,
    "snippet_chars": 300,
    "boilerplate": list(DEFAULT_BOILERPLATE),
    "user_agent": "Mozilla/5.0 (compatible; plain-news/0.1)",
}


class ConfigError(Exception):
    """sources.yaml is missing or invalid. The message lists every problem."""


@dataclass(frozen=True)
class Region:
    id: str
    label: str


@dataclass(frozen=True)
class Source:
    name: str
    region: str
    language: str
    feed_url: str
    max_items_considered: int
    paywalled: bool = False
    enabled: bool = True


@dataclass(frozen=True)
class Config:
    site_title: str
    site_description: str
    timezone: str
    max_age_hours: int
    items_per_region: int
    items_in_all: int
    commit_warning_days: int
    default_language: str
    regions: tuple
    label_priority: tuple
    ai: dict
    extra_rules: str
    images: dict
    fetch: dict
    sources: tuple

    def active_sources(self):
        return [s for s in self.sources if s.enabled]


def _positive_int(errors, where, value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        errors.append(f"{where} must be a whole number of at least 1 (got {value!r})")
        return 1
    return value


def load_config(path=None) -> Config:
    path = Path(path) if path else DEFAULT_PATH
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"{path} not found") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")

    errors: list[str] = []
    site = raw.get("site") or {}
    s = raw.get("settings") or {}

    default_language = s.get("default_language", "en")
    if not isinstance(default_language, str) or not re.fullmatch(r"[a-z]{2,3}", default_language):
        errors.append(f"settings.default_language must be a language code like 'en' (got {default_language!r})")
        default_language = "en"

    max_age = _positive_int(errors, "settings.max_age_hours", s.get("max_age_hours", 48))
    per_region = _positive_int(errors, "settings.items_per_region", s.get("items_per_region", 5))
    in_all = _positive_int(errors, "settings.items_in_all", s.get("items_in_all", 15))
    warn_days = _positive_int(errors, "settings.commit_warning_days", s.get("commit_warning_days", 45))

    regions = []
    for i, r in enumerate(raw.get("regions") or []):
        if not isinstance(r, dict) or not r.get("id") or not r.get("label"):
            errors.append(f"regions[{i}] needs both 'id' and 'label'")
            continue
        regions.append(Region(str(r["id"]), str(r["label"])))
    if not regions:
        errors.append("regions: at least one region is required")
    region_ids = [r.id for r in regions]
    if len(set(region_ids)) != len(region_ids):
        errors.append("regions: ids must be unique")

    priority = raw.get("label_priority") or region_ids
    if not isinstance(priority, list) or set(priority) != set(region_ids) or len(priority) != len(set(priority)):
        errors.append("label_priority must list every region id exactly once")
        priority = region_ids

    extra_rules = raw.get("extra_rules") or ""
    if not isinstance(extra_rules, str):
        errors.append("extra_rules must be text (it may be left empty)")
        extra_rules = ""

    fetch = {**FETCH_DEFAULTS, **(raw.get("fetch") or {})}
    phrases = fetch["boilerplate"]
    if not isinstance(phrases, list) or not all(isinstance(x, str) and x.strip() for x in phrases):
        errors.append("fetch.boilerplate must be a list of non-empty phrases (an empty list turns it off)")
        fetch["boilerplate"] = []

    ai = {**AI_DEFAULTS, **(raw.get("ai") or {})}
    for key in ("summary_max_sentences", "max_stories_per_region", "max_calls_per_run",
                "max_output_tokens", "timeout_seconds", "retries"):
        _positive_int(errors, f"ai.{key}", ai[key])
    for key in ("coverage_bonus_per_source", "coverage_bonus_max"):
        if isinstance(ai[key], bool) or not isinstance(ai[key], int) or ai[key] < 0:
            errors.append(f"ai.{key} must be a whole number, 0 or more (got {ai[key]!r})")

    sources = []
    seen = set()
    for i, d in enumerate(raw.get("sources") or []):
        where = f"sources[{i}]" + (f" ({d.get('name')})" if isinstance(d, dict) and d.get("name") else "")
        if not isinstance(d, dict):
            errors.append(f"{where} must be a mapping")
            continue
        name = d.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{where}: 'name' is required")
            name = f"source {i}"
        region = d.get("region")
        if region not in region_ids:
            errors.append(f"{where}: region {region!r} is not one of {region_ids}")
        language = d.get("language", default_language)
        if not isinstance(language, str) or not re.fullmatch(r"[a-z]{2,3}", language):
            errors.append(f"{where}: language must be a code like 'en' or 'el' (got {language!r})")
        url = d.get("feed_url")
        if not isinstance(url, str) or not re.match(r"https?://\S+$", url):
            errors.append(f"{where}: feed_url must be an http(s) address")
        max_items = _positive_int(errors, f"{where}: max_items_considered", d.get("max_items_considered"))
        paywalled = d.get("paywalled", False)
        enabled = d.get("enabled", True)
        for key, val in (("paywalled", paywalled), ("enabled", enabled)):
            if not isinstance(val, bool):
                errors.append(f"{where}: {key} must be true or false (got {val!r})")
        if (name, region) in seen:
            errors.append(f"{where}: duplicate source for region {region!r}")
        seen.add((name, region))
        sources.append(Source(name, region, language, url, max_items, bool(paywalled), bool(enabled)))
    if not any(src.enabled for src in sources):
        errors.append("sources: at least one enabled source is required")

    if errors:
        raise ConfigError(f"{path.name} has {len(errors)} problem(s):\n - " + "\n - ".join(errors))

    return Config(
        site_title=str(site.get("title", "Plain news")),
        site_description=str(site.get("description", "")),
        timezone=str(s.get("timezone", "Europe/London")),
        max_age_hours=max_age,
        items_per_region=per_region,
        items_in_all=in_all,
        commit_warning_days=warn_days,
        default_language=default_language,
        regions=tuple(regions),
        label_priority=tuple(priority),
        ai=ai,
        extra_rules=extra_rules.strip(),
        images={**IMAGE_DEFAULTS, **(raw.get("images") or {})},
        fetch=fetch,
        sources=tuple(sources),
    )
