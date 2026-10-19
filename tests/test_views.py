import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from plainnews.ai import Story, StoryItem
from plainnews.config import load_config
from plainnews.views import all_tab, format_tab, load_stories, rank, region_tab, story_from_dict

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc)


def make_config(extra=""):
    text = (
        "regions:\n  - {id: world, label: World}\n  - {id: uk, label: UK}\n"
        "  - {id: greece, label: Greece}\n  - {id: bristol, label: Bristol}\n"
        "label_priority: [bristol, greece, uk, world]\n" + extra +
        "sources:\n"
        '  - {name: A, region: uk, feed_url: "https://a.example/f", max_items_considered: 5}\n'
    )
    p = Path(tempfile.mkdtemp()) / "sources.yaml"
    p.write_text(text, encoding="utf-8")
    return load_config(p)


def st(region, headline, links, score, sources=None, minutes_ago=60, language="en", summary="", image=None,
       fallback=False):
    sources = sources or [f"src-{n}" for n in range(len(links))]
    when = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    return Story(region=region, language=language, headline=headline, link=links[0], summary=summary,
                 image_url=image,
                 items=[StoryItem(s, f"{headline} ({s})", link, False, when) for s, link in zip(sources, links)],
                 sources=list(sources), ai_score=score, score=score, published=when, fallback=fallback)


class RegionTabTests(unittest.TestCase):
    def test_top_stories_by_score_then_coverage_then_recency(self):
        stories = [
            st("uk", "low", ["u1"], 4),
            st("uk", "high", ["u2"], 9),
            st("uk", "tie-old", ["u3"], 6, minutes_ago=500),
            st("uk", "tie-new", ["u4"], 6, minutes_ago=10),
            st("uk", "tie-wide", ["u5", "u6"], 6, sources=["A", "B"]),
            st("world", "other region", ["w1"], 10),
        ]
        self.assertEqual([s.headline for s in region_tab(stories, "uk", 4)],
                         ["high", "tie-wide", "tie-new", "tie-old"])

    def test_unscored_stories_come_last_in_their_given_order(self):
        stories = [st("uk", "raw-1", ["u1"], None, fallback=True), st("uk", "scored", ["u2"], 2),
                   st("uk", "raw-2", ["u3"], None, fallback=True)]
        self.assertEqual([s.headline for s in rank(stories)], ["scored", "raw-1", "raw-2"])

    def test_limit_and_empty_region(self):
        stories = [st("uk", f"s{n}", [f"u{n}"], n) for n in range(1, 9)]
        self.assertEqual(len(region_tab(stories, "uk", 5)), 5)
        self.assertEqual(region_tab(stories, "bristol", 5), [])


class AllTabTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()

    def test_same_event_in_two_regions_is_one_card_from_the_most_specific_region(self):
        world = st("world", "France curfews (world)", ["bbc-fr", "gdn-fr"], 10, sources=["BBC", "Guardian"],
                   summary="World summary.", image="https://img/w.jpg")
        uk = st("uk", "France curfews (uk)", ["bbc-fr", "mail-fr"], 8, sources=["BBC", "Daily Mail"],
                summary="UK summary.")
        cards = all_tab([world, uk], self.cfg)
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual((card.region, card.headline, card.summary), ("uk", "France curfews (uk)", "UK summary."))
        self.assertEqual(card.score, 10)                                       # the highest score wins
        self.assertEqual(card.sources, ["BBC", "Daily Mail", "Guardian"])      # every source, no repeats
        self.assertEqual([i.link for i in card.items], ["bbc-fr", "mail-fr", "gdn-fr"])
        self.assertEqual(card.image_url, "https://img/w.jpg")                  # borrowed: the uk version had none

    def test_bristol_beats_uk_and_matching_is_transitive(self):
        uk = st("uk", "uk version", ["a", "b"], 7)
        bristol = st("bristol", "bristol version", ["b", "c"], 5)
        world = st("world", "world version", ["c", "d"], 6)
        cards = all_tab([uk, bristol, world], self.cfg)
        self.assertEqual([(c.region, c.headline, c.score) for c in cards], [("bristol", "bristol version", 7)])
        self.assertEqual(sorted(i.link for i in cards[0].items), ["a", "b", "c", "d"])

    def test_languages_do_not_merge_without_a_shared_link(self):
        english = st("world", "France riots", ["bbc-fr"], 9)
        greek = st("greece", "Γαλλία: επεισόδια", ["skai-fr"], 9, language="el")
        self.assertEqual(len(all_tab([english, greek], self.cfg)), 2)          # known limit of link matching

    def test_unrelated_stories_stay_separate_and_rank_by_score(self):
        stories = [st("uk", "a", ["1"], 5), st("uk", "b", ["2"], 9), st("world", "c", ["3"], 7)]
        self.assertEqual([c.headline for c in all_tab(stories, self.cfg)], ["b", "c", "a"])

    def test_limit_comes_from_the_config(self):
        stories = [st("uk", f"s{n}", [f"l{n}"], 1 + n % 9) for n in range(30)]
        self.assertEqual(len(all_tab(stories, self.cfg)), 15)
        self.assertEqual(len(all_tab(stories, make_config("settings: {items_in_all: 4}\n"))), 4)
        self.assertEqual(len(all_tab(stories, self.cfg, limit=7)), 7)

    def test_unscored_stories_follow_every_scored_one(self):
        raw = [st("greece", f"raw{n}", [f"g{n}"], None, language="el", fallback=True) for n in range(3)]
        scored = [st("uk", "scored", ["u1"], 1)]
        cards = all_tab(raw + scored, self.cfg)
        self.assertEqual([c.headline for c in cards], ["scored", "raw0", "raw1", "raw2"])
        self.assertTrue(all(c.fallback for c in cards[1:]))

    def test_a_scored_version_makes_a_merged_card_scored(self):
        raw = st("greece", "raw version", ["x"], None, language="el", fallback=True)
        scored = st("world", "scored version", ["x"], 6)
        card = all_tab([raw, scored], self.cfg)[0]
        self.assertEqual((card.region, card.score, card.fallback), ("greece", 6, False))

    def test_inputs_are_not_modified(self):
        world = st("world", "w", ["x", "y"], 9, sources=["A", "B"])
        uk = st("uk", "u", ["x", "z"], 5, sources=["A", "C"])
        all_tab([world, uk], self.cfg)
        self.assertEqual((world.sources, uk.sources), (["A", "B"], ["A", "C"]))
        self.assertEqual((len(world.items), len(uk.items), uk.score), (2, 2, 5))


class LoadAndFormatTests(unittest.TestCase):
    def test_round_trip_through_the_saved_json(self):
        original = st("uk", "headline", ["l1", "l2"], 7, sources=["A", "B"], summary="Summary.")
        path = Path(tempfile.mkdtemp()) / "stories.json"
        path.write_text(json.dumps([original.to_dict()], ensure_ascii=False), encoding="utf-8")
        loaded = load_stories(path)
        self.assertEqual(loaded, [original])
        self.assertIsInstance(loaded[0].items[0], StoryItem)
        self.assertEqual(story_from_dict(original.to_dict()), original)

    def test_format_tab(self):
        text = format_tab("All", [st("uk", "A headline", ["l"], 8)], {"uk": "UK"})
        self.assertEqual(text, "== All (1) ==\n[ 8] UK       1 src  A headline")


if __name__ == "__main__":
    unittest.main()
