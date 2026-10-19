import json
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from plainnews.ai import AIError, LLMResponse
from plainnews.build import (BuildError, build_site, ensure_images, git_info, pick_tabs, read_version, run,
                             run_saved, shown_stories)
from plainnews.config import load_config

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc)


def feed(*items):
    body = "".join(
        f"<item><title>{t}</title><link>{l}</link><description>{d}</description>"
        f"<pubDate>Mon, 19 Oct 2026 0{8 - n}:00:00 GMT</pubDate>{extra}</item>"
        for n, (t, l, d, extra) in enumerate(items))
    return f'<?xml version="1.0"?><rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><title>t</title>{body}</channel></rss>'


class Handler(BaseHTTPRequestHandler):
    hits = {}
    routes = {}

    def log_message(self, *args):
        pass

    def do_GET(self):
        Handler.hits[self.path] = Handler.hits.get(self.path, 0) + 1
        if self.path in Handler.routes:
            body, ctype = Handler.routes[self.path]
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()


class StoryEachProvider:
    """A stand-in model: one story per item, scored by position, with the snippet as the summary."""

    def __init__(self, fail=False):
        self.fail, self.calls = fail, 0

    def generate(self, system, user, schema=None):
        self.calls += 1
        if self.fail:
            raise AIError("HTTP 429")
        rows = json.loads(user.split("\n", 1)[1])
        stories = [{"items": [r["id"]], "lead": r["id"], "summary": r["snippet"], "score": 9 - n % 8}
                   for n, r in enumerate(rows)]
        return LLMResponse(json.dumps({"stories": stories}), 100, 50)


class BuildTests(unittest.TestCase):
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
        b = self.base
        rich = "The council confirmed an interim replacement would be appointed after the meeting."
        Handler.hits = {}
        Handler.routes = {
            "/feed-a": (feed(("Mayor resigns after scandal", f"{b}/art/a1", rich, ""),
                              ("Rain expected tomorrow", f"{b}/art/a2", "", '<media:thumbnail url="https://img.example/feed.jpg" width="400"/>')),
                        "application/xml"),
            "/feed-b": (feed(("Ελληνικός τίτλος", f"{b}/art/b1", "", "")), "application/xml"),
            "/art/a1": ('<html><head><meta property="og:image" content="https://img.example/a1.jpg"></head><body>x</body></html>', "text/html"),
            "/art/a2": ("<html><head></head></html>", "text/html"),
            "/art/b1": ('<html><head><meta property="og:image" content="https://img.example/b1.jpg"></head></html>', "text/html"),
        }
        self.tmp = Path(tempfile.mkdtemp())

    def config(self, dead=True, repo=""):
        text = (
            "site: {title: Test news%s}\n" % (f', repo_url: "{repo}"' if repo else "") +
            "regions:\n  - {id: alpha, label: Alpha}\n  - {id: beta, label: Beta}\n"
            "label_priority: [beta, alpha]\n"
            "fetch: {retries: 1, backoff_seconds: 0.01, timeout_seconds: 5}\n"
            "images: {timeout_seconds: 5}\n"
            "ai: {model: test-model}\n"
            "sources:\n"
            f'  - {{name: A, region: alpha, feed_url: "{self.base}/feed-a", max_items_considered: 5}}\n'
            f'  - {{name: B, region: beta, language: el, feed_url: "{self.base}/feed-b", max_items_considered: 5}}\n'
            + (f'  - {{name: Dead, region: beta, feed_url: "{self.base}/dead", max_items_considered: 5}}\n' if dead else ""))
        p = self.tmp / "sources.yaml"
        p.write_text(text, encoding="utf-8")
        return load_config(p)

    def test_full_run_builds_the_site(self):
        out, data = self.tmp / "site", self.tmp / "data"
        built = run(self.config(), out, now=NOW, provider=StoryEachProvider(), data_dir=data)
        html = (out / "index.html").read_text(encoding="utf-8")
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["icon-192.png", "icon-512.png", "index.html", "manifest.webmanifest"])
        for fragment in ("Mayor resigns after scandal", "Ελληνικός τίτλος", 'id="t-alpha"', 'id="t-beta"',
                         "interim replacement would be appointed after the meeting."):
            self.assertIn(fragment, html)
        # a failed source is reported both in the header and in the status log
        self.assertIn("1 source unavailable", html)
        self.assertIn("Dead (Beta) is unavailable: HTTP 404.", html)
        self.assertIn("Sources: 2 of 3 working", html)
        # images: one from the feed, two from article pages (the third article page has none)
        self.assertIn('src="https://img.example/feed.jpg"', html)
        self.assertIn('src="https://img.example/a1.jpg"', html)
        self.assertIn('src="https://img.example/b1.jpg"', html)
        self.assertEqual(built["image_counts"], {"from_feed": 1, "from_page": 2, "missing": 0})
        self.assertEqual(sorted(p.name for p in data.iterdir()), ["ai_report.json", "fetch_report.json", "stories.json"])

    def test_the_all_tab_comes_first_and_only_it_shows_regions(self):
        built = run(self.config(), self.tmp / "site", now=NOW, provider=StoryEachProvider())
        self.assertEqual([(t[0], t[3]) for t in built["tabs"]], [("all", True), ("alpha", False), ("beta", False)])
        self.assertEqual(len(built["tabs"][0][2]), 3)

    def test_an_ai_failure_still_builds_a_page_from_raw_headlines(self):
        out = self.tmp / "site"
        built = run(self.config(), out, now=NOW, provider=StoryEachProvider(fail=True))
        html = (out / "index.html").read_text(encoding="utf-8")
        self.assertIn("Mayor resigns after scandal", html)
        self.assertIn("2 regions showing raw headlines", html)
        self.assertIn("summaries unavailable (HTTP 429)", html)
        self.assertNotIn("interim replacement", html)                      # no summaries without the model
        self.assertEqual(built["status"].empty_summaries, 0)               # raw headlines are not "empty summaries"

    def test_a_run_that_fetches_nothing_fails_and_leaves_the_old_site_alone(self):
        out = self.tmp / "site"
        out.mkdir()
        (out / "index.html").write_text("previous page", encoding="utf-8")
        Handler.routes.pop("/feed-a")
        Handler.routes.pop("/feed-b")
        with self.assertRaises(BuildError):
            run(self.config(dead=True), out, now=NOW, provider=StoryEachProvider())
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["index.html"])
        self.assertEqual((out / "index.html").read_text(encoding="utf-8"), "previous page")

    def test_rebuild_from_a_saved_run_makes_no_requests(self):
        data = self.tmp / "data"
        run(self.config(), self.tmp / "first", now=NOW, provider=StoryEachProvider(), data_dir=data)
        Handler.hits = {}
        out = self.tmp / "second"
        built = run_saved(self.config(), out, data, now=NOW)
        self.assertEqual(Handler.hits, {})                                  # no feeds, no article pages
        html = (out / "index.html").read_text(encoding="utf-8")
        self.assertIn("Mayor resigns after scandal", html)
        self.assertIn("Sources: 2 of 3 working", html)                      # the saved fetch report is reused
        # images found during the first run were not saved, so the rebuild shows only feed images
        self.assertEqual(built["image_counts"]["from_page"], 0)
        run_saved(self.config(), self.tmp / "third", data, now=NOW, with_images=True)
        self.assertGreater(Handler.hits.get("/art/a1", 0), 0)               # --with-images does look up pages

    def test_rebuild_without_a_saved_run_explains_what_to_do(self):
        with self.assertRaises(BuildError) as ctx:
            run_saved(self.config(), self.tmp / "site", self.tmp / "missing", now=NOW)
        self.assertIn("python -m plainnews.ai", str(ctx.exception))

    def test_repo_link_in_the_footer_comes_from_the_config(self):
        out = self.tmp / "site"
        run(self.config(repo="https://github.com/me/news"), out, now=NOW, provider=StoryEachProvider())
        self.assertIn('<a href="https://github.com/me/news">Source code</a>', (out / "index.html").read_text(encoding="utf-8"))

    def test_a_story_in_two_tabs_costs_one_page_lookup(self):
        config = self.config(dead=False)
        built = run(config, self.tmp / "site", now=NOW, provider=StoryEachProvider())
        shown = shown_stories(built["tabs"])
        self.assertGreater(len(shown), len({s.link for s in shown}))        # some stories appear in two tabs
        self.assertEqual(Handler.hits.get("/art/a1"), 1)                    # ...yet the page was fetched once


class VersionAndGitTests(unittest.TestCase):
    def test_version_file(self):
        folder = Path(tempfile.mkdtemp())
        self.assertEqual(read_version(folder), "unknown")
        (folder / "VERSION").write_text("1.2.3\n", encoding="utf-8")
        self.assertEqual(read_version(folder), "1.2.3")

    def test_git_info(self):
        folder = Path(tempfile.mkdtemp())
        self.assertEqual(git_info(folder), (None, None))                     # not a repository
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_COMMITTER_DATE": "2026-09-01T12:00:00Z",
               "GIT_AUTHOR_DATE": "2026-09-01T12:00:00Z", "PATH": __import__("os").environ["PATH"]}
        run_git = lambda *a: subprocess.run(["git", *a], cwd=folder, env=env, check=True, capture_output=True)
        run_git("init", "-q")
        (folder / "f.txt").write_text("x", encoding="utf-8")
        run_git("add", ".")
        run_git("-c", "commit.gpgsign=false", "commit", "-q", "-m", "first")
        short, when = git_info(folder)
        self.assertRegex(short, r"^[0-9a-f]{7,}$")
        self.assertEqual(when, datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
