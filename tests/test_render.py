import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

from plainnews.ai import GroupReport, Story, StoryItem
from plainnews.config import load_config
from plainnews.fetch import FeedResult
from plainnews.render import (ICONS, STATIC, build_card, local_timezone, manifest, render_page, safe_url,
                              time_text, write_site)
from plainnews.status import compute_status

NOW = datetime(2026, 10, 19, 7, 0, tzinfo=timezone.utc)      # 08:00 in London (BST)


def make_config(site="", model="gemini-test"):
    text = (
        f"site: {{title: \"Plain news\", description: \"News by region.\"{site}}}\n"
        "regions:\n  - {id: world, label: World}\n  - {id: uk, label: UK}\n  - {id: greece, label: Greece}\n"
        f"ai: {{model: {model}}}\n"
        "sources:\n"
        '  - {name: A, region: uk, feed_url: "https://a.example/f", max_items_considered: 5}\n'
    )
    p = Path(tempfile.mkdtemp()) / "sources.yaml"
    p.write_text(text, encoding="utf-8")
    return load_config(p)


def story(region, headline, link, image=None, summary="A summary.", language="en", minutes_ago=62,
          sources=(("BBC", True),), paywalled=False):
    when = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    items = [StoryItem(name, f"{headline} ({name})", f"{link}/{name}" if n else link, paywalled, when)
             for n, (name, _) in enumerate(sources)]
    return Story(region=region, language=language, headline=headline, link=link, summary=summary, image_url=image,
                 items=items, sources=[s for s, _ in sources], ai_score=7, score=7, published=when)


class Collect(HTMLParser):
    VOID = {"meta", "link", "img", "br", "hr", "input"}

    def __init__(self):
        super().__init__()
        self.tags, self.stack, self.problems, self.text = [], [], [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.problems.append(f"unexpected </{tag}> (open: {self.stack[-3:]})")
            if tag in self.stack:
                while self.stack and self.stack.pop() != tag:
                    pass
        else:
            self.stack.pop()

    def handle_data(self, data):
        self.text.append(data)

    def attrs(self, tag):
        return [a for t, a in self.tags if t == tag]


def parse(html):
    c = Collect()
    c.feed(html)
    c.close()
    return c


def status_for(config, **kw):
    return compute_status(config, kw.pop("feeds", [FeedResult("BBC", "uk", "https://x.example", True,
                                                                items_in_feed=30, items_kept=10, newest_age_hours=1.0)]),
                          kw.pop("reports", [GroupReport("uk", "en", stories=3, ok=True)]),
                          {"from_feed": 2, "from_page": 1, "missing": 0}, kw.pop("shown", []), NOW,
                          version="0.1.0", **kw)


def page(config=None, tabs=None, status=None, **kw):
    config = config or make_config()
    status = status or status_for(config)
    tabs = tabs if tabs is not None else [
        ("all", "All", [story("uk", "Alpha headline", "https://a.example/1", image="https://img.example/a.jpg")], True),
        ("world", "World", [story("world", "World headline", "https://w.example/1")], False),
        ("uk", "UK", [], False),
        ("greece", "Greece", [story("greece", "Ελληνικός τίτλος", "https://g.example/1", language="el")], False)]
    return render_page(config, tabs, status, NOW, version="0.1.0"), config


class PageStructureTests(unittest.TestCase):
    def test_tabs_follow_the_given_order_and_every_panel_exists(self):
        html, _ = page()
        c = parse(html)
        self.assertEqual(c.problems, [])
        buttons = [a for a in c.attrs("button") if a.get("role") == "tab"]
        self.assertEqual([b["id"] for b in buttons], ["t-all", "t-world", "t-uk", "t-greece"])
        self.assertEqual([b["aria-selected"] for b in buttons], ["true", "false", "false", "false"])
        panels = [a for a in c.attrs("section") if a.get("role") == "tabpanel"]
        self.assertEqual([p["id"] for p in panels], ["p-all", "p-world", "p-uk", "p-greece"])
        self.assertNotIn("is-inactive", panels[0]["class"])
        self.assertTrue(all("is-inactive" in p["class"] for p in panels[1:]))
        self.assertIn("No stories in this section.", html)

    def test_region_label_only_on_the_all_tab(self):
        html, _ = page()
        all_panel = html.split('id="p-all"')[1].split('id="p-world"')[0]
        world_panel = html.split('id="p-world"')[1].split('id="p-uk"')[0]
        self.assertIn("UK · <time", all_panel)
        self.assertNotIn("World · <time", world_panel)
        self.assertIn('<p class="kicker"><time', world_panel)

    def test_header_date_updated_time_and_title(self):
        html, _ = page()
        self.assertIn("<h1>Plain news</h1>", html)
        self.assertIn("Monday 19 October 2026 · Updated 08:00", html)       # London time, not UTC
        self.assertIn('<html lang="en">', html)

    def test_no_external_requests_apart_from_publishers(self):
        html, _ = page()
        c = parse(html)
        self.assertEqual([a for a in c.attrs("script") if "src" in a], [])                 # no external scripts
        self.assertEqual([a["href"] for a in c.attrs("link") if a.get("rel") == "stylesheet"], [])
        self.assertNotIn("@import", html)
        self.assertNotIn("url(", html)
        self.assertIn('<meta name="referrer" content="no-referrer">', html)
        for a in c.attrs("img"):
            self.assertTrue(a["src"].startswith("https://img.example"))


class CardTests(unittest.TestCase):
    def test_image_and_text_only_cards(self):
        html, _ = page()
        c = parse(html)
        imgs = c.attrs("img")
        self.assertEqual(len(imgs), 1)
        self.assertEqual((imgs[0]["loading"], imgs[0]["referrerpolicy"], imgs[0]["alt"]), ("lazy", "no-referrer", ""))
        self.assertEqual(html.count('class="top noimg"'), 2)           # the two cards without an image have no gap

    def test_greek_card_is_marked_with_its_language(self):
        html, _ = page()
        self.assertEqual(html.count(' lang="el"'), 1)
        self.assertIn("Ελληνικός τίτλος", html)

    def test_sources_lead_first_one_link_each_and_paywall_marker(self):
        s = story("uk", "Merged", "https://x.example/lead", sources=(("BBC", True), ("Sky", True), ("Guardian", True)),
                  paywalled=True)
        s.items.append(StoryItem("Sky", "dup", "https://x.example/sky-2", False, None))     # a second Sky item: shown once
        card = build_card(s, {"uk": "UK"}, timezone.utc, NOW)
        self.assertEqual([x["name"] for x in card["sources"]], ["BBC", "Sky", "Guardian"])
        self.assertTrue(all(x["paywalled"] for x in card["sources"][:3]))
        html, _ = page(tabs=[("all", "All", [s], True)])
        self.assertEqual(html.count('class="pw"'), 3)

    def test_the_lead_source_is_listed_first_even_if_it_is_not_the_first_item(self):
        s = story("uk", "Merged", "https://x.example/b", sources=(("BBC", True), ("Sky", True)))
        s.items[0].link, s.items[1].link = "https://x.example/a", "https://x.example/b"
        s.link = "https://x.example/b"
        card = build_card(s, {}, timezone.utc, NOW)
        self.assertEqual([x["name"] for x in card["sources"]], ["Sky", "BBC"])

    def test_text_is_escaped(self):
        evil = story("uk", '<script>alert("x")</script>', "https://a.example/1", summary="<img src=x onerror=alert(1)>")
        html, _ = page(tabs=[("all", "All", [evil], True)])
        self.assertNotIn("<script>alert", html)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;", html)
        c = parse(html)
        self.assertEqual(len([t for t, _ in c.tags if t == "script"]), 2)             # only our own two scripts

    def test_unsafe_links_and_images_never_reach_the_page(self):
        bad_link = story("uk", "Bad link", "javascript:alert(1)")
        bad_link.items = [StoryItem("BBC", "t", "javascript:alert(2)", False, None)]
        bad_image = story("uk", "Bad image", "https://a.example/ok", image="javascript:alert(3)")
        rescued = story("uk", "Rescued", "javascript:alert(4)")
        rescued.items = [StoryItem("BBC", "t", "https://a.example/rescued", False, None)]
        html, _ = page(tabs=[("all", "All", [bad_link, bad_image, rescued], True)])
        self.assertNotIn("javascript:", html)
        self.assertNotIn("Bad link", html)                                            # no usable link: card dropped
        self.assertIn("Bad image", html)
        self.assertEqual(parse(html).attrs("img"), [])
        self.assertIn('href="https://a.example/rescued"', html)                       # an item link replaces a bad lead link

    def test_times_are_in_the_configured_timezone(self):
        london = timezone(timedelta(hours=1))
        same_day = datetime(2026, 10, 19, 6, 2, tzinfo=timezone.utc)
        yesterday = datetime(2026, 10, 18, 6, 2, tzinfo=timezone.utc)
        self.assertEqual(time_text(same_day, london, NOW), "07:02")
        self.assertEqual(time_text(yesterday, london, NOW), "18 Oct 07:02")
        html, _ = page(tabs=[("all", "All", [story("uk", "Alpha", "https://a.example/1", minutes_ago=62)], True)])
        self.assertIn('<time datetime="2026-10-19T05:58:00+00:00">06:58</time>', html)     # 05:58 UTC is 06:58 BST

    def test_a_story_without_a_date_has_no_time(self):
        s = story("uk", "Undated", "https://a.example/1")
        s.published = None
        self.assertNotIn("<time", page(tabs=[("world", "World", [s], False)])[0])


class StatusAndFooterTests(unittest.TestCase):
    def test_status_is_closed_by_default_and_the_header_note_appears_only_with_problems(self):
        config = make_config()
        html, _ = page(config)
        details = [a for a in parse(html).attrs("details")]
        self.assertEqual(len(details), 1)
        self.assertNotIn("open", details[0])
        self.assertNotIn('class="note"', html)
        self.assertIn("Sources: 1 of 1 working", html)
        bad = status_for(config, feeds=[FeedResult("Bristol Post", "uk", "https://x", False, error="HTTP 403")])
        html, _ = page(config, status=bad)
        self.assertIn('<a class="note" href="#status">1 source unavailable</a>', html)
        self.assertIn("Bristol Post (UK) is unavailable: HTTP 403.", html)
        self.assertIn("unavailable (HTTP 403)", html)

    def test_footer_names_the_model_and_hides_the_repo_link_until_it_is_set(self):
        html, _ = page(make_config(model="gemini-3.5-flash-lite"))
        self.assertIn("AI model (gemini-3.5-flash-lite)", html)
        self.assertIn("Thumbnails load directly from the publishers' servers.", html)
        self.assertIn("This page itself sets no cookies, runs no trackers and stores nothing about you.", html)
        self.assertNotIn("Source code", html)
        html, _ = page(make_config(', repo_url: "https://github.com/me/plain-news", footer_note: "Contact <me>"'))
        self.assertIn('<a href="https://github.com/me/plain-news">Source code</a> · MIT licence', html)
        self.assertIn("Contact &lt;me&gt;", html)
        self.assertIn("Plain news v0.1.0", html)

    def test_version_and_status_line(self):
        html, _ = page()
        self.assertIn("Updated Monday 19 October 2026, 08:00 (UK time) · v0.1.0", html)


class SiteFilesTests(unittest.TestCase):
    def test_manifest_and_files(self):
        config = make_config()
        out = Path(tempfile.mkdtemp()) / "site"
        written = write_site(out, page(config)[0], config)
        self.assertEqual(sorted(written), sorted(["index.html", "manifest.webmanifest", *ICONS]))
        data = json.loads((out / "manifest.webmanifest").read_text(encoding="utf-8"))
        self.assertEqual((data["name"], data["display"], data["start_url"]), ("Plain news", "standalone", "./"))
        self.assertEqual([(i["sizes"], i["src"]) for i in data["icons"]],
                         [("192x192", "icon-192.png"), ("512x512", "icon-512.png")])
        for name in ICONS:
            self.assertEqual((out / name).read_bytes(), (STATIC / name).read_bytes())
        self.assertEqual(manifest(config)["short_name"], "Plain news")

    def test_greek_text_survives_the_round_trip_as_utf8(self):
        config = make_config()
        out = Path(tempfile.mkdtemp())
        write_site(out, page(config)[0], config)
        self.assertIn("Ελληνικός τίτλος", (out / "index.html").read_text(encoding="utf-8"))


class HelperTests(unittest.TestCase):
    def test_safe_url(self):
        for good in ("https://a.example/x", "http://a.example", " https://a.example/y "):
            self.assertEqual(safe_url(good), good.strip())
        for bad in ("javascript:alert(1)", "data:text/html,x", "//a.example/x", "/relative", "https://", "", None, 5):
            self.assertIsNone(safe_url(bad), repr(bad))

    def test_missing_timezone_data_falls_back_to_utc(self):
        tz, warning = local_timezone("Nowhere/Land")
        self.assertEqual((tz, "Nowhere/Land" in warning), (timezone.utc, True))
        self.assertEqual(local_timezone("Europe/London")[1], None)


if __name__ == "__main__":
    unittest.main()
