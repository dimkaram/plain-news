import tempfile
import unittest
from pathlib import Path

from plainnews.config import ConfigError, load_config


class RealConfigTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()

    def test_counts_and_order(self):
        self.assertEqual(len(self.cfg.sources), 16)
        self.assertEqual(len(self.cfg.active_sources()), 15)
        self.assertEqual([r.id for r in self.cfg.regions], ["world", "uk", "greece", "bristol"])
        self.assertEqual(self.cfg.label_priority, ("bristol", "greece", "uk", "world"))

    def test_defaults_applied(self):
        bbc = next(s for s in self.cfg.sources if s.name == "BBC News" and s.region == "uk")
        self.assertEqual(bbc.language, "en")
        self.assertFalse(bbc.paywalled)
        self.assertTrue(bbc.enabled)

    def test_greece_is_greek_and_kathimerini_disabled(self):
        greece = [s for s in self.cfg.sources if s.region == "greece"]
        self.assertTrue(all(s.language == "el" for s in greece))
        kath = next(s for s in greece if s.name == "Kathimerini")
        self.assertFalse(kath.enabled)
        self.assertEqual(sorted(s.name for s in greece if s.enabled), ["ERT News", "Skai", "To Vima"])

    def test_runtime_defaults(self):
        self.assertEqual(self.cfg.fetch["retries"], 3)
        self.assertEqual(self.cfg.fetch["snippet_chars"], 300)
        self.assertTrue(self.cfg.fetch["boilerplate"])        # never empty unless you turn it off
        self.assertTrue(self.cfg.images["og_image_fallback"])
        self.assertEqual(self.cfg.items_in_all, 15)

    def test_ai_settings(self):
        ai = self.cfg.ai
        self.assertEqual(ai["provider"], "gemini")
        self.assertEqual(ai["summary_max_sentences"], 2)
        self.assertEqual(ai["max_stories_per_region"], 25)
        self.assertEqual(ai["max_calls_per_run"], 12)
        self.assertEqual(ai["coverage_bonus_max"], 2)       # default, not set in the file
        self.assertEqual(ai["min_stories_share"], 0.5)      # default, not set in the file
        self.assertEqual(self.cfg.extra_rules, "")           # plain news: no rules by default


class InvalidConfigTests(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "sources.yaml"
            p.write_text(text, encoding="utf-8")
            return load_config(p)

    def test_reports_every_problem(self):
        bad = """
regions:
  - {id: uk, label: UK}
label_priority: [uk]
topic_rule: "x"
sources:
  - {name: A, region: nowhere, feed_url: "https://a.example/feed", max_items_considered: 5}
  - {name: B, region: uk, feed_url: "ftp://b.example", max_items_considered: 0}
  - {name: C, region: uk, feed_url: "https://c.example/feed", max_items_considered: 5, enabled: "yes"}
  - {name: C, region: uk, feed_url: "https://c.example/feed", max_items_considered: 5}
"""
        with self.assertRaises(ConfigError) as ctx:
            self.load(bad)
        msg = str(ctx.exception)
        for fragment in ("nowhere", "feed_url must be", "at least 1", "enabled must be", "duplicate"):
            self.assertIn(fragment, msg)

    def test_missing_file(self):
        with self.assertRaises(ConfigError):
            load_config("/nonexistent/sources.yaml")

    def test_boilerplate_defaults_and_validation(self):
        base = """
regions: [{id: uk, label: UK}]
sources:
  - {name: A, region: uk, feed_url: "https://a.example/f", max_items_considered: 5}
"""
        cfg = self.load(base)
        self.assertIn("samaritans", cfg.fetch["boilerplate"])
        self.assertIn("εμφανίστηκε πρώτα", cfg.fetch["boilerplate"])
        self.assertEqual(self.load(base + "fetch: {boilerplate: []}\n").fetch["boilerplate"], [])
        with self.assertRaises(ConfigError) as ctx:
            self.load(base + "fetch: {boilerplate: oops}\n")
        self.assertIn("fetch.boilerplate", str(ctx.exception))

    def test_bad_ai_settings_are_reported(self):
        text = """
regions: [{id: uk, label: UK}]
ai: {summary_max_sentences: 0, coverage_bonus_max: -1, min_stories_share: 1.5}
extra_rules: 5
sources:
  - {name: A, region: uk, feed_url: "https://a.example/f", max_items_considered: 5}
"""
        with self.assertRaises(ConfigError) as ctx:
            self.load(text)
        msg = str(ctx.exception)
        for fragment in ("ai.summary_max_sentences", "ai.coverage_bonus_max", "ai.min_stories_share",
                         "extra_rules must be text"):
            self.assertIn(fragment, msg)

    def test_needs_an_enabled_source(self):
        text = """
regions: [{id: uk, label: UK}]
topic_rule: "x"
sources:
  - {name: A, region: uk, feed_url: "https://a.example/f", max_items_considered: 5, enabled: false}
"""
        with self.assertRaises(ConfigError) as ctx:
            self.load(text)
        self.assertIn("at least one enabled", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
