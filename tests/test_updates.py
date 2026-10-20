import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from plainnews import updates
from plainnews.config import ConfigError, load_config
from plainnews.updates import (UpdateError, decide, latest_slot, longest_gap_minutes, main, parse_times, run_check,
                               update_slots)

LONDON = ZoneInfo("Europe/London")
TIMES = [time(7, 15), time(12, 15), time(17, 45), time(21, 15)]


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def make_config(settings=""):
    text = ("regions: [{id: uk, label: UK}]\n" + (f"settings: {{{settings}}}\n" if settings else "") +
            'sources:\n  - {name: A, region: uk, feed_url: "https://a.example/f", max_items_considered: 5}\n')
    p = Path(tempfile.mkdtemp()) / "sources.yaml"
    p.write_text(text, encoding="utf-8")
    return load_config(p)


def run(created, status="completed", conclusion="success"):
    return {"created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": status, "conclusion": conclusion}


class SlotTests(unittest.TestCase):
    def test_summer_time_slots_are_an_hour_before_the_clock(self):
        slots = update_slots(utc(2026, 10, 20, 12), TIMES, LONDON, 120)
        today = [s for s in slots if s.date() == datetime(2026, 10, 20).date()]
        self.assertEqual([s.strftime("%H:%M") for s in today], ["06:15", "11:15", "16:45", "20:15"])   # BST = UTC+1

    def test_the_clocks_going_back_weekend_keeps_four_slots_a_day_at_the_right_hours(self):
        expected = {  # the UK changes at 01:00 UTC on 25 October 2026
            (2026, 10, 24): ["06:15", "11:15", "16:45", "20:15"],
            (2026, 10, 25): ["07:15", "12:15", "17:45", "21:15"],
            (2026, 10, 26): ["07:15", "12:15", "17:45", "21:15"],
        }
        slots = update_slots(utc(2026, 10, 26, 12), TIMES, LONDON, 3000)
        for (y, m, d), hours in expected.items():
            self.assertEqual([s.strftime("%H:%M") for s in slots if (s.year, s.month, s.day) == (y, m, d)], hours)

    def test_the_same_in_spring(self):
        slots = update_slots(utc(2027, 3, 29, 12), TIMES, LONDON, 3000)   # clocks go forward on 28 March 2027
        by_day = {}
        for s in slots:
            by_day.setdefault(s.day, []).append(s.strftime("%H:%M"))
        self.assertEqual(by_day[27], ["07:15", "12:15", "17:45", "21:15"])
        self.assertEqual(by_day[28], ["06:15", "11:15", "16:45", "20:15"])

    def test_a_time_inside_the_skipped_hour_still_gives_exactly_one_slot_that_day(self):
        slots = update_slots(utc(2027, 3, 28, 12), [time(1, 30)], LONDON, 60)
        self.assertEqual(len([s for s in slots if s.date() == datetime(2027, 3, 28).date()]), 1)

    def test_a_time_inside_the_repeated_hour_gives_one_slot_that_day(self):
        slots = update_slots(utc(2026, 10, 25, 12), [time(1, 30)], LONDON, 60)
        self.assertEqual(len([s for s in slots if s.date() == datetime(2026, 10, 25).date()]), 1)

    def test_latest_slot_inside_the_window(self):
        self.assertEqual(latest_slot(utc(2026, 10, 20, 6, 50), TIMES, LONDON, 120), utc(2026, 10, 20, 6, 15))
        self.assertEqual(latest_slot(utc(2026, 10, 20, 8, 14), TIMES, LONDON, 120), utc(2026, 10, 20, 6, 15))

    def test_nothing_due_before_the_first_slot_or_after_the_window(self):
        self.assertIsNone(latest_slot(utc(2026, 10, 20, 6, 0), TIMES, LONDON, 120))     # overnight gap
        self.assertIsNone(latest_slot(utc(2026, 10, 20, 8, 16), TIMES, LONDON, 120))    # 2h01 after the slot

    def test_a_window_that_crosses_midnight_uses_yesterdays_slot(self):
        self.assertEqual(latest_slot(utc(2026, 12, 2, 0, 45), [time(23, 30)], LONDON, 180), utc(2026, 12, 1, 23, 30))

    def test_a_long_window_can_reach_back_more_than_a_day(self):
        self.assertEqual(latest_slot(utc(2026, 10, 22, 6, 0), [time(7, 15)], LONDON, 3000), utc(2026, 10, 21, 6, 15))

    def test_times_are_parsed_or_rejected(self):
        self.assertEqual(parse_times(["07:15", "21:05"]), [time(7, 15), time(21, 5)])
        for bad in (["7pm"], ["25:00"], [None]):
            with self.assertRaises(UpdateError):
                parse_times(bad)


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()
        self.now = utc(2026, 10, 20, 6, 50)            # 07:50 in London; the 07:15 update (06:15 UTC) has passed

    def d(self, runs, now=None):
        return decide(now or self.now, self.cfg, runs, LONDON)

    def test_due_when_nothing_has_run_since_the_slot(self):
        result = self.d([run(utc(2026, 10, 19, 21, 15))])
        self.assertTrue(result.due)
        self.assertIn("07:15", result.reason)

    def test_a_late_check_still_catches_up_inside_the_window(self):
        self.assertTrue(self.d([], utc(2026, 10, 20, 8, 10)).due)                    # 09:10 London, 115 minutes late

    def test_not_due_after_a_successful_run_since_the_slot_whoever_started_it(self):
        result = self.d([run(utc(2026, 10, 20, 6, 54))])
        self.assertFalse(result.due)
        self.assertIn("07:15 update is done", result.reason)

    def test_a_success_before_the_slot_does_not_count(self):
        self.assertTrue(self.d([run(utc(2026, 10, 20, 6, 14))]).due)

    def test_not_due_while_a_run_is_queued_or_running(self):
        for status in ("queued", "in_progress", "waiting", "pending"):
            self.assertFalse(self.d([run(utc(2026, 10, 20, 5), status, None)]).due, status)

    def test_failures_are_retried_up_to_the_limit_then_stop(self):
        fails = lambda n: [run(utc(2026, 10, 20, 6, 20 + i), conclusion="failure") for i in range(n)]
        self.assertTrue(self.d(fails(3)).due)
        stopped = self.d(fails(4))
        self.assertFalse(stopped.due)
        self.assertIn("limit 4", stopped.reason)
        self.assertFalse(self.d([run(utc(2026, 10, 20, 6, 20), conclusion="cancelled")] * 4).due)

    def test_old_failures_do_not_count_against_a_new_slot(self):
        old = [run(utc(2026, 10, 19, 21, 20), conclusion="failure")] * 5
        self.assertTrue(self.d(old).due)

    def test_the_attempt_limit_is_a_setting(self):
        cfg = make_config("max_attempts_per_update: 1")
        self.assertFalse(decide(self.now, cfg, [run(utc(2026, 10, 20, 6, 20), conclusion="failure")], LONDON).due)

    def test_not_due_outside_every_window(self):
        cfg = make_config("update_window_minutes: 120")
        result = decide(utc(2026, 10, 20, 9, 30), cfg, [], LONDON)                   # 10:30 London, 3h15 after the slot
        self.assertFalse(result.due)
        self.assertIn("no update time passed", result.reason)

    def test_the_default_window_covers_the_longest_gap_so_a_missed_update_never_lapses(self):
        self.assertEqual(self.cfg.update_window_minutes, 600)
        self.assertEqual(longest_gap_minutes(parse_times(self.cfg.update_times)), 600)         # 21:15 to 07:15
        for hour in range(24):                                  # at any hour of the day, with nothing published
            self.assertTrue(decide(utc(2026, 10, 20, hour, 30), self.cfg, [], LONDON).due, hour)

    def test_the_real_failure_of_20_october_would_now_have_been_caught(self):
        runs = [run(utc(2026, 10, 20, 6, 54))]                                       # the manual run after the 07:15 update
        late_noon = decide(utc(2026, 10, 20, 13, 52), self.cfg, runs, LONDON)         # the first check after 12:15, 2h37 late
        self.assertTrue(late_noon.due)
        self.assertIn("12:15", late_noon.reason)
        runs.append(run(utc(2026, 10, 20, 13, 55)))
        late_evening = decide(utc(2026, 10, 20, 18, 46), self.cfg, runs, LONDON)      # the first check after 17:45, 2h01 late
        self.assertTrue(late_evening.due)
        self.assertIn("17:45", late_evening.reason)

    def test_one_check_publishes_once_even_when_several_updates_were_missed(self):
        runs = [run(utc(2026, 10, 20, 6, 54))]
        first = decide(utc(2026, 10, 20, 18, 46), self.cfg, runs, LONDON)             # 12:15 and 17:45 both missed
        self.assertTrue(first.due)
        self.assertIn("17:45", first.reason)                                          # only the latest one counts
        runs.append(run(utc(2026, 10, 20, 18, 50)))
        self.assertFalse(decide(utc(2026, 10, 20, 19, 17), self.cfg, runs, LONDON).due)  # and then it is done

    def test_the_times_come_from_the_config(self):
        cfg = make_config('update_times: ["09:00"]')
        self.assertFalse(decide(self.now, cfg, [], LONDON).due)                      # 07:50 London: nothing at 07:15 now
        self.assertTrue(decide(utc(2026, 10, 20, 8, 30), cfg, [], LONDON).due)       # 09:30 London

    def test_config_rejects_bad_times(self):
        for bad in ('update_times: ["7:15"]', 'update_times: []', 'update_times: ["07:15", "07:15"]',
                    "update_window_minutes: 0", "max_attempts_per_update: 0"):
            with self.assertRaises(ConfigError, msg=bad):
                make_config(bad)
        self.assertEqual(make_config().update_times, ("07:15", "12:15", "17:45", "21:15"))


class GapTests(unittest.TestCase):
    def test_longest_gap(self):
        self.assertEqual(longest_gap_minutes(TIMES), 600)
        self.assertEqual(longest_gap_minutes([time(7, 0), time(19, 0)]), 720)
        self.assertEqual(longest_gap_minutes([time(7, 15)]), 1440)
        self.assertEqual(longest_gap_minutes([time(23, 0), time(1, 0)]), 22 * 60)


class GitHubHandler(BaseHTTPRequestHandler):
    runs, get_status, post_status, posts = [], 200, 204, []

    def log_message(self, *args):
        pass

    def _send(self, status, body=b""):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.get_status != 200:
            return self._send(self.get_status)
        GitHubHandler.gets = getattr(GitHubHandler, "gets", 0) + 1
        self._send(200, json.dumps({"workflow_runs": GitHubHandler.runs}).encode())

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        GitHubHandler.posts.append({"path": self.path, "body": body, "auth": self.headers.get("Authorization")})
        self._send(GitHubHandler.post_status)


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), GitHubHandler)
        cls.api = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        GitHubHandler.runs, GitHubHandler.posts = [], []
        GitHubHandler.get_status = GitHubHandler.post_status = None
        GitHubHandler.get_status, GitHubHandler.post_status = 200, 204
        patcher = mock.patch.object(updates, "RETRY_WAIT", 0.01)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = make_config()
        self.now = utc(2026, 10, 20, 6, 50)

    def check(self, **kw):
        return run_check(self.cfg, "o/r", kw.pop("token", "secret-token"), self.api, now=self.now, **kw)

    def test_a_due_update_starts_publish_on_the_default_branch(self):
        result = self.check(ref="main")
        self.assertTrue(result.due)
        self.assertEqual(len(GitHubHandler.posts), 1)
        post = GitHubHandler.posts[0]
        self.assertEqual(post["path"], "/repos/o/r/actions/workflows/publish.yml/dispatches")
        self.assertEqual((post["body"], post["auth"]), ({"ref": "main"}, "Bearer secret-token"))

    def test_nothing_is_started_when_not_due_or_on_a_dry_run(self):
        GitHubHandler.runs = [run(utc(2026, 10, 20, 6, 54))]
        self.assertFalse(self.check().due)
        GitHubHandler.runs = []
        self.assertTrue(self.check(dry_run=True).due)
        self.assertEqual(GitHubHandler.posts, [])

    def test_a_dry_run_needs_no_token_but_starting_one_does(self):
        self.assertTrue(self.check(token=None, dry_run=True).due)
        with self.assertRaises(UpdateError):
            self.check(token=None)

    def test_api_errors_are_reported_and_server_errors_retried(self):
        GitHubHandler.get_status = 404
        with self.assertRaises(UpdateError) as ctx:
            self.check()
        self.assertIn("HTTP 404", str(ctx.exception))
        GitHubHandler.get_status = 500
        with self.assertRaises(UpdateError) as ctx:
            self.check()
        self.assertIn("HTTP 500", str(ctx.exception))
        GitHubHandler.get_status, GitHubHandler.post_status = 200, 403
        with self.assertRaises(UpdateError) as ctx:
            self.check()
        self.assertIn("could not start Publish: HTTP 403", str(ctx.exception))

    def test_a_missing_timezone_database_is_explained(self):
        cfg = make_config("timezone: Nowhere/Land")
        with self.assertRaises(UpdateError) as ctx:
            run_check(cfg, "o/r", None, self.api, now=self.now, dry_run=True)
        self.assertIn("tzdata", str(ctx.exception))

    def test_main_prints_the_facts_and_never_the_token(self):
        env = {"GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "secret-token", "GITHUB_API_URL": self.api,
               "GITHUB_EVENT_NAME": "schedule", "SCHEDULE": "17 * * * *"}
        buffer = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), contextlib.redirect_stdout(buffer):
            code = main(["--dry-run"])
        out = buffer.getvalue()
        self.assertEqual(code, 0)
        for fragment in ("event:      schedule 17 * * * *", "now (UTC):", "now (Europe/London):", "decision:"):
            self.assertIn(fragment, out)
        self.assertNotIn("secret-token", out)

    def test_main_warns_when_the_window_is_shorter_than_the_longest_gap(self):
        GitHubHandler.runs = []
        env = {"GITHUB_REPOSITORY": "o/r", "GITHUB_API_URL": self.api}
        for settings, expect in (("update_window_minutes: 120", True), ("", False)):
            buffer = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch("plainnews.updates.load_config", return_value=make_config(settings)), \
                    contextlib.redirect_stdout(buffer):
                main(["--dry-run"])
            self.assertEqual("longest gap" in buffer.getvalue(), expect, settings)

    def test_main_needs_a_repository(self):
        with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main([]), 2)


if __name__ == "__main__":
    unittest.main()
