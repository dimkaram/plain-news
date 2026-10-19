import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

from plainnews.config import load_config
from plainnews.images import fetch_og_image, fill_missing

PAGES = {
    "/og-abs": '<html><head><meta property="og:image" content="http://img.example/a.jpg"></head><body>x</body></html>',
    "/og-rel": '<html><head><meta property="og:image" content="/img/rel.jpg"></head></html>',
    "/og-reversed": "<html><head><meta content='https://img.example/r.jpg' property='og:image'></head></html>",
    "/twitter-only": '<html><head><meta name="twitter:image" content="https://img.example/t.jpg"></head></html>',
    "/og-beats-twitter": '<html><head><meta name="twitter:image" content="https://img.example/t.jpg">'
                         '<meta property="og:image" content="https://img.example/o.jpg"></head></html>',
    "/no-meta": "<html><head><title>x</title></head><body>hello</body></html>",
    "/late-meta": '<html><head></head><body><meta property="og:image" content="https://img.example/late.jpg"></body></html>',
    "/data-uri": '<html><head><meta property="og:image" content="data:image/png;base64,AAAA"></head></html>',
    "/big": '<html><head><meta property="og:image" content="https://img.example/big.jpg"></head><body>'
            + "x" * 1_000_000 + "</body></html>",
}


class Handler(BaseHTTPRequestHandler):
    hits = {}

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        Handler.hits[self.path] = Handler.hits.get(self.path, 0) + 1
        if path == "/pdf":
            body, ctype = b"%PDF-1.4", "application/pdf"
        elif path in PAGES:
            body, ctype = PAGES[path].encode("utf-8"), "text/html; charset=utf-8"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


def make_config(extra_images=""):
    text = (
        "regions: [{id: uk, label: UK}]\n"
        'topic_rule: "x"\n'
        f"images: {{timeout_seconds: 5{extra_images}}}\n"
        "sources:\n"
        '  - {name: A, region: uk, feed_url: "https://a.example/f", max_items_considered: 5}\n'
    )
    p = Path(tempfile.mkdtemp()) / "sources.yaml"
    p.write_text(text, encoding="utf-8")
    return load_config(p)


def item(link, image=None, paywalled=False):
    return SimpleNamespace(link=link, image_url=image, paywalled=paywalled)


class ImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.secure_base = cls.base.replace("http://", "https://")
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.cfg = make_config()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Handler.hits.clear()

    def og(self, path):
        return fetch_og_image(self.base + path, self.cfg)

    def test_absolute_http_is_upgraded(self):
        self.assertEqual(self.og("/og-abs"), "https://img.example/a.jpg")

    def test_relative_image_resolved_against_page(self):
        self.assertEqual(self.og("/og-rel"), self.secure_base + "/img/rel.jpg")

    def test_attribute_order_and_quotes(self):
        self.assertEqual(self.og("/og-reversed"), "https://img.example/r.jpg")

    def test_twitter_image_is_a_fallback_and_og_wins(self):
        self.assertEqual(self.og("/twitter-only"), "https://img.example/t.jpg")
        self.assertEqual(self.og("/og-beats-twitter"), "https://img.example/o.jpg")

    def test_nothing_found_cases(self):
        for path in ("/no-meta", "/late-meta", "/data-uri", "/pdf", "/missing"):
            self.assertIsNone(self.og(path), path)

    def test_large_page_is_not_a_problem(self):
        self.assertEqual(self.og("/big"), "https://img.example/big.jpg")

    def test_connection_failure_returns_none(self):
        self.assertIsNone(fetch_og_image("http://127.0.0.1:1/x", self.cfg))

    def test_fill_missing_rules(self):
        had = item(self.base + "/og-abs", image="https://already.example/i.jpg")
        paywalled = item(self.base + "/og-rel", paywalled=True)
        wanted = item(self.base + "/twitter-only")
        dup_a = item(self.base + "/og-reversed")
        dup_b = item(self.base + "/og-reversed")
        broken = item(self.base + "/no-meta")
        stats = fill_missing([had, paywalled, wanted, dup_a, dup_b, broken], self.cfg)
        self.assertEqual(had.image_url, "https://already.example/i.jpg")
        self.assertIsNone(paywalled.image_url)
        self.assertEqual(wanted.image_url, "https://img.example/t.jpg")
        self.assertEqual(dup_a.image_url, "https://img.example/r.jpg")
        self.assertEqual(dup_b.image_url, "https://img.example/r.jpg")
        self.assertIsNone(broken.image_url)
        self.assertEqual(Handler.hits.get("/og-abs", 0), 0)      # already had an image
        self.assertEqual(Handler.hits.get("/og-rel", 0), 0)      # paywalled: link only
        self.assertEqual(Handler.hits["/og-reversed"], 1)        # duplicate link fetched once
        self.assertEqual((stats.already_had, stats.skipped_paywalled), (1, 1))
        self.assertEqual((stats.attempted, stats.found, stats.failed), (3, 2, 1))

    def test_cap_limits_page_fetches(self):
        cfg = make_config(", max_page_fetches_per_run: 2")
        items = [item(f"{self.base}/og-abs?n={n}") for n in range(5)]
        stats = fill_missing(items, cfg)
        self.assertEqual(stats.attempted, 2)
        self.assertEqual(stats.skipped_over_cap, 3)
        self.assertEqual([bool(i.image_url) for i in items], [True, True, False, False, False])
        self.assertEqual(sum(Handler.hits.values()), 2)

    def test_can_be_switched_off(self):
        cfg = make_config(", og_image_fallback: false")
        items = [item(self.base + "/og-abs")]
        stats = fill_missing(items, cfg)
        self.assertEqual(stats.attempted, 0)
        self.assertEqual(Handler.hits, {})
        self.assertIsNone(items[0].image_url)


if __name__ == "__main__":
    unittest.main()
