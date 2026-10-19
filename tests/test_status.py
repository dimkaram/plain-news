import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from plainnews.ai import GroupReport
from plainnews.config import load_config
from plainnews.fetch import FeedResult
from plainnews.status import age_text, commit_age_days, compute_status

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc)


def make_config(extra=""):
    text = ("regions:\n  - {id: uk, label: UK}\n  - {id: bristol, label: Bristol}\n" + extra +
            'sources:\n  - {name: A, region: uk, feed_url: "https://a.example/f", max_items_considered: 5}\n')
    p = Path(tempfile.mkdtemp()) / "sources.yaml"
    p.write_text(text, encoding="utf-8")
    return load_config(p)


def feed(source, region="uk", ok=True, error=None, kept=10, total=30, age=1.0):
    return FeedResult(source=source, region=region, feed_url="https://x.example", ok=ok, error=error,
                      items_in_feed=total, items_kept=kept if ok else 0, newest_age_hours=age if ok else None)


def story(link, summary="Text.", fallback=False):
    return SimpleNamespace(link=link, summary=summary, fallback=fallback)


class AgeTextTests(unittest.TestCase):
    def test_units(self):
        self.assertEqual([age_text(h) for h in (None, 0.01, 0.6, 1.0, 5.4, 47.9, 72)],
                         ["no dates", "1 min", "36 min", "1 h", "5 h", "48 h", "3 days"])

    def test_commit_age(self):
        self.assertIsNone(commit_age_days(NOW, None))
        self.assertEqual(commit_age_days(NOW, NOW - timedelta(days=23, hours=5)), 23)
        self.assertEqual(commit_age_days(NOW, NOW + timedelta(hours=1)), 0)


class StatusTests(unittest.TestCase):
    def status(self, feeds, reports=(), shown=(), config=None, counts=None, **kw):
        return compute_status(config or make_config(), feeds, list(reports), counts or {}, list(shown), NOW, **kw)

    def test_all_well(self):
        s = self.status([feed("BBC News"), feed("Sky News", age=2.5)],
                        [GroupReport("uk", "en", stories=25, ok=True)], [story("l1")],
                        counts={"from_feed": 10, "from_page": 3, "missing": 1}, version="0.1.0",
                        commit_hash="a1b2c3d", commit_time=NOW - timedelta(days=23))
        self.assertEqual((s.warnings, s.note), ([], ""))
        self.assertEqual((s.sources_ok, s.sources_total), (2, 2))
        self.assertEqual(s.sources[1].detail, "10 of 30 items, newest 3 h ago")      # 2.5 rounds to 3 hours
        self.assertEqual(s.regions[0].text, "25 stories")
        self.assertEqual(s.images_line, "10 from feeds, 3 from article pages, 1 missing")
        self.assertEqual(s.version_line, "v0.1.0 (a1b2c3d) · last commit 23 days ago")

    def test_failures_stale_fallback_and_commit_warnings_in_priority_order(self):
        cfg = make_config("settings: {stale_after_hours: 24, commit_warning_days: 45}\n")
        s = self.status(
            [feed("Bristol Post", "bristol", ok=False, error="HTTP 403"), feed("Daily Mail", age=30), feed("BBC")],
            [GroupReport("uk", "en", stories=3, fallback=True, error="HTTP 429")], config=cfg,
            commit_hash=None, commit_time=NOW - timedelta(days=50))
        self.assertEqual([w.split(" ")[0] for w in s.warnings], ["Bristol", "UK:", "Daily", "Scheduled"])
        self.assertIn("Bristol Post (Bristol) is unavailable: HTTP 403.", s.warnings[0])
        self.assertIn("showing raw headlines", s.warnings[1] + " " + s.note)
        self.assertIn("50 days ago", s.warnings[3])
        self.assertEqual(s.note, "1 source unavailable · 1 region showing raw headlines · 1 stale · "
                                 "commit needed to keep updates running")
        self.assertEqual((s.sources_ok, s.sources_total), (2, 3))
        self.assertTrue(s.sources[1].stale and s.sources[1].detail.endswith("(stale)"))
        self.assertEqual(s.sources[0].detail, "unavailable (HTTP 403)")
        self.assertTrue(s.regions[0].fallback)

    def test_story_plural(self):
        one = self.status([feed("A")], [GroupReport("uk", "en", stories=1, ok=True)])
        self.assertEqual(one.regions[0].text, "1 story")
        raw = self.status([feed("A")], [GroupReport("uk", "en", stories=2, fallback=True, error="HTTP 429")])
        self.assertEqual(raw.regions[0].text, "2 stories, raw headlines only (HTTP 429)")

    def test_plurals_in_the_note(self):
        s = self.status([feed("A", ok=False, error="x"), feed("B", ok=False, error="y")])
        self.assertEqual(s.note, "2 sources unavailable")

    def test_exactly_at_the_commit_threshold_warns_and_just_under_does_not(self):
        cfg = make_config("settings: {commit_warning_days: 45}\n")
        at = self.status([feed("A")], config=cfg, commit_time=NOW - timedelta(days=45))
        under = self.status([feed("A")], config=cfg, commit_time=NOW - timedelta(days=44, hours=23))
        self.assertTrue(at.warnings and not under.warnings)

    def test_unknown_commit_gives_no_warning_and_no_commit_text(self):
        s = self.status([feed("A")], version="0.1.0")
        self.assertEqual((s.warnings, s.version_line), ([], "v0.1.0"))

    def test_empty_summaries_are_counted_once_per_story_and_ignore_raw_headlines(self):
        shown = [story("a", ""), story("a", ""), story("b", "Text."), story("c", "", fallback=True)]
        self.assertEqual(self.status([feed("A")], shown=shown).empty_summaries, 1)

    def test_extra_warnings_come_first(self):
        s = self.status([feed("A", ok=False, error="x")], extra_warnings=["Times shown in UTC."])
        self.assertEqual(s.warnings[0], "Times shown in UTC.")


if __name__ == "__main__":
    unittest.main()
