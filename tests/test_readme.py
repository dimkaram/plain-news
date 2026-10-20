"""The README and the code must agree about every setting: names, and the defaults the README states."""
import re
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path

from plainnews.config import AI_DEFAULTS, FETCH_DEFAULTS, IMAGE_DEFAULTS, Source, load_config

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
ROWS = [line for line in README.splitlines() if line.startswith("|")]


def minimal_config():
    p = Path(tempfile.mkdtemp()) / "sources.yaml"
    p.write_text("regions: [{id: uk, label: UK}]\nsources:\n"
                 '  - {name: A, region: uk, feed_url: "https://a.example/f", max_items_considered: 5}\n',
                 encoding="utf-8")
    return load_config(p)


def documented_keys():
    """Every `section.key` or `sources[].field` named in backticks in the README."""
    return set(re.findall(r"`((?:site|settings|ai|images|fetch)\.[a-z_]+|sources\[\]\.[a-z_]+)`", README))


def expected_defaults():
    cfg = minimal_config()
    expected = {
        "site.title": cfg.site_title, "site.description": cfg.site_description, "site.repo_url": cfg.repo_url,
        "site.footer_note": cfg.footer_note,
        "settings.timezone": cfg.timezone, "settings.max_age_hours": cfg.max_age_hours,
        "settings.items_per_region": cfg.items_per_region, "settings.items_in_all": cfg.items_in_all,
        "settings.default_language": cfg.default_language, "settings.stale_after_hours": cfg.stale_after_hours,
        "settings.commit_warning_days": cfg.commit_warning_days,
        "settings.update_times": cfg.update_times, "settings.update_window_minutes": cfg.update_window_minutes,
        "settings.max_attempts_per_update": cfg.max_attempts_per_update,
    }
    expected.update({f"ai.{k}": v for k, v in AI_DEFAULTS.items()})
    expected.update({f"images.{k}": v for k, v in IMAGE_DEFAULTS.items()})
    expected.update({f"fetch.{k}": v for k, v in FETCH_DEFAULTS.items()})
    return expected


class ReadmeMatchesCodeTests(unittest.TestCase):
    def test_every_setting_is_documented_in_exactly_one_table_row(self):
        for key in expected_defaults():
            rows = [r for r in ROWS if r.startswith(f"| `{key}`")]
            self.assertEqual(len(rows), 1, f"{key} should appear in exactly one table row")

    def test_the_readme_names_no_setting_that_does_not_exist(self):
        known = set(expected_defaults()) | {f"sources[].{f.name}" for f in fields(Source)}
        self.assertEqual(sorted(documented_keys() - known), [])

    def test_every_source_field_is_documented(self):
        for f in fields(Source):
            self.assertTrue(any(r.startswith(f"| `sources[].{f.name}`") for r in ROWS), f.name)

    def test_the_defaults_stated_in_the_readme_are_the_real_ones(self):
        for key, default in expected_defaults().items():
            if default in ("", None) or isinstance(default, (list, tuple)):
                continue                                              # empty and list defaults are described in words
            row = next(r for r in ROWS if r.startswith(f"| `{key}`"))
            cell = row.split("|")[2].strip()
            shown = str(default).lower() if isinstance(default, bool) else str(default)
            self.assertIn(shown, cell.replace("`", ""), f"{key}: README says {cell!r}, code says {default!r}")

    def test_top_level_settings_are_documented(self):
        for name in ("regions", "label_priority", "extra_rules", "sources"):
            self.assertRegex(README, rf"`{name}`")

    def test_the_commands_in_the_readme_exist(self):
        for module in re.findall(r"python -m (plainnews\.[a-z]+)", README):
            self.assertTrue((ROOT / (module.replace(".", "/") + ".py")).exists(), module)

    def test_the_workflow_snippet_matches_the_real_schedule(self):
        workflow = (ROOT / ".github" / "workflows" / "check-schedule.yml").read_text(encoding="utf-8")
        for cron in ('cron: "17 * * * *"', 'cron: "47 * * * *"'):
            self.assertIn(cron, README)
            self.assertIn(cron, workflow)

    def test_the_default_update_times_are_stated_in_the_readme(self):
        row = next(r for r in ROWS if r.startswith("| `settings.update_times`"))
        for t in minimal_config().update_times:
            self.assertIn(t, row)


if __name__ == "__main__":
    unittest.main()
