import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from plainnews.config import load_config
from plainnews.fetch import fetch_all, fetch_feed, format_report

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc)  # a Monday

BBC_LIKE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><title>t</title>
<item><title>Fresh story &amp; more</title><link>http://pub.example/a</link>
<description>&lt;p&gt;First   sentence.&lt;/p&gt;</description>
<pubDate>Mon, 19 Oct 2026 08:00:00 GMT</pubDate>
<media:thumbnail width="240" height="135" url="http://img.example/240/a.jpg"/>
<media:thumbnail width="976" height="549" url="http://img.example/976/a.jpg"/>
<media:thumbnail width="480" height="270" url="http://img.example/480/a.jpg"/></item>
<item><title>Old story</title><link>http://pub.example/old</link>
<pubDate>Wed, 14 Oct 2026 08:00:00 GMT</pubDate></item>
<item><title>Undated story</title><link>http://pub.example/undated</link></item>
<item><title>Duplicate of fresh</title><link>http://pub.example/a</link>
<pubDate>Mon, 19 Oct 2026 07:00:00 GMT</pubDate></item>
</channel></rss>"""

GREEK = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><title>t</title>
<item><title>Ελληνικός τίτλος</title><link>/news/1</link>
<description><![CDATA[<p>Πρώτη πρόταση με <b>έντονα</b>.</p>]]></description>
<content:encoded><![CDATA[<img src="/img/a.jpg" width="600"><p>κείμενο</p>]]></content:encoded>
<pubDate>Mon, 19 Oct 2026 08:30:00 GMT</pubDate></item></channel></rss>"""

ENCLOSURE = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>With enclosure</title><link>https://pub.example/e</link>
<pubDate>Mon, 19 Oct 2026 08:00:00 GMT</pubDate>
<enclosure url="https://img.example/e.jpg" type="image/jpeg" length="1"/></item>
<item><title>Video enclosure</title><link>https://pub.example/v</link>
<pubDate>Mon, 19 Oct 2026 07:00:00 GMT</pubDate>
<enclosure url="https://img.example/v.mp4" type="video/mp4" length="1"/></item>
</channel></rss>"""

MANY = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>""" + "".join(
    f"<item><title>Story {n}</title><link>https://pub.example/{n}</link>"
    f"<pubDate>Mon, 19 Oct 2026 0{9 - n}:00:00 GMT</pubDate></item>" for n in range(1, 6)
) + "</channel></rss>"

ALL_STALE = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Ancient</title><link>https://pub.example/x</link>
<pubDate>Mon, 05 Oct 2026 08:00:00 GMT</pubDate></item></channel></rss>"""


class Handler(BaseHTTPRequestHandler):
    hits = {}
    flaky_state = {"n": 0}

    def log_message(self, *args):
        pass

    def do_GET(self):
        Handler.hits[self.path] = Handler.hits.get(self.path, 0) + 1
        routes = {"/bbc": BBC_LIKE, "/greek": GREEK, "/enclosure": ENCLOSURE,
                  "/many": MANY, "/stale": ALL_STALE, "/notfeed": "<html>nope</html>"}
        if self.path == "/flaky":
            Handler.flaky_state["n"] += 1
            if Handler.flaky_state["n"] == 1:
                self.send_response(500)
                self.end_headers()
                return
            body = MANY
        elif self.path in routes:
            body = routes[self.path]
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def make_config(base, sources):
    lines = ["regions:", "  - {id: uk, label: UK}", "  - {id: greece, label: Greece}",
             'topic_rule: "x"', "fetch: {retries: 3, backoff_seconds: 0.01, timeout_seconds: 5}",
             "sources:"]
    for name, path, extra in sources:
        lines.append(f'  - {{name: "{name}", region: uk, feed_url: "{base}{path}", {extra}}}')
    tmp = Path(tempfile.mkdtemp()) / "sources.yaml"
    tmp.write_text("\n".join(lines), encoding="utf-8")
    return load_config(tmp)


class FetchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Handler.hits.clear()
        Handler.flaky_state["n"] = 0

    def one(self, path, extra="max_items_considered: 10", language="en"):
        cfg = make_config(self.base, [("Test", path, extra)])
        source = cfg.sources[0]
        if language != "en":
            source = type(source)(**{**source.__dict__, "language": language})
        return fetch_feed(source, cfg, NOW)

    def test_media_thumbnail_age_filter_dedupe(self):
        result, items = self.one("/bbc")
        self.assertTrue(result.ok)
        self.assertEqual(result.items_in_feed, 4)
        titles = [i.title for i in items]
        self.assertEqual(titles, ["Fresh story & more", "Undated story"])  # old + duplicate dropped
        self.assertEqual(result.newest_age_hours, 1.0)
        fresh = items[0]
        self.assertEqual(fresh.snippet, "First sentence.")
        self.assertEqual(fresh.published, datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc))
        self.assertEqual(fresh.rank, 0)
        self.assertIsNone(items[1].published)
        # smallest image that is at least 300px wide, upgraded to https
        self.assertEqual(fresh.image_url, "https://img.example/480/a.jpg")
        self.assertEqual(result.with_images, 1)

    def test_greek_text_html_image_and_relative_links(self):
        result, items = self.one("/greek", language="el")
        self.assertTrue(result.ok)
        item = items[0]
        self.assertEqual(item.title, "Ελληνικός τίτλος")
        self.assertEqual(item.snippet, "Πρώτη πρόταση με έντονα .")
        self.assertEqual(item.link, f"{self.base}/news/1")
        # resolved against the article link, then upgraded from http to https
        self.assertEqual(item.image_url, self.base.replace("http://", "https://") + "/img/a.jpg")
        self.assertEqual(item.language, "el")

    def test_enclosure_image_only_for_images(self):
        _, items = self.one("/enclosure")
        self.assertEqual(items[0].image_url, "https://img.example/e.jpg")
        self.assertIsNone(items[1].image_url)

    def test_max_items_considered_keeps_feed_order(self):
        result, items = self.one("/many", extra="max_items_considered: 3")
        self.assertEqual([i.title for i in items], ["Story 1", "Story 2", "Story 3"])
        self.assertEqual([i.rank for i in items], [0, 1, 2])
        self.assertEqual(result.items_kept, 3)

    def test_not_a_feed(self):
        result, items = self.one("/notfeed")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "no items")
        self.assertEqual(items, [])

    def test_404_is_not_retried(self):
        result, _ = self.one("/missing")
        self.assertEqual(result.error, "HTTP 404")
        self.assertEqual(Handler.hits["/missing"], 1)

    def test_server_error_is_retried_then_succeeds(self):
        result, items = self.one("/flaky")
        self.assertTrue(result.ok)
        self.assertEqual(Handler.hits["/flaky"], 2)
        self.assertEqual(len(items), 5)

    def test_connection_refused(self):
        cfg = make_config("http://127.0.0.1:1", [("Dead", "/x", "max_items_considered: 5")])
        result, items = fetch_feed(cfg.sources[0], cfg, NOW)
        self.assertEqual(result.error, "connection error")
        self.assertEqual(items, [])

    def test_all_stale_reports_no_recent_items(self):
        result, items = self.one("/stale")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "no recent items")
        self.assertEqual(result.newest_age_hours, 14 * 24 + 1.0)
        self.assertEqual(items, [])

    def test_one_bad_feed_does_not_stop_the_others(self):
        cfg = make_config(self.base, [
            ("Good1", "/many", "max_items_considered: 2"),
            ("Broken", "/missing", "max_items_considered: 2"),
            ("Good2", "/enclosure", "max_items_considered: 2"),
        ])
        items, results = fetch_all(cfg, NOW)
        self.assertEqual([r.source for r in results], ["Good1", "Broken", "Good2"])
        self.assertEqual([r.ok for r in results], [True, False, True])
        self.assertEqual(len(items), 4)
        report = format_report(results)
        self.assertIn("FAIL", report)
        self.assertIn("HTTP 404", report)


if __name__ == "__main__":
    unittest.main()
