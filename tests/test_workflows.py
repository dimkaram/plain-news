import re
import unittest
from pathlib import Path

import yaml

SECRET_REF = re.compile(r"\$\{\{\s*secrets\.")        # a real secret reference, not the word in a comment
WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def load(name):
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    data["on"] = data.pop(True, data.get("on"))        # YAML 1.1 reads a bare `on:` as the boolean True
    return text, data


class PublishWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text, self.wf = load("publish.yml")

    def test_runs_twice_a_day_in_uk_time_and_on_demand(self):
        schedule = self.wf["on"]["schedule"]
        self.assertEqual([(s["cron"], s["timezone"]) for s in schedule],
                         [("15 7 * * *", "Europe/London"), ("45 17 * * *", "Europe/London")])
        self.assertIn("workflow_dispatch", self.wf["on"])

    def test_least_privilege_permissions(self):
        self.assertEqual(self.wf["permissions"], {"contents": "read"})
        self.assertEqual(self.wf["jobs"]["deploy"]["permissions"],
                         {"actions": "read", "pages": "write", "id-token": "write"})
        self.assertNotIn("permissions", self.wf["jobs"]["build"])                  # the build only reads

    def test_the_key_comes_only_from_the_secret_and_only_in_the_build_step(self):
        self.assertEqual(len(SECRET_REF.findall(self.text)), 1)
        build = self.wf["jobs"]["build"]["steps"]
        step = next(s for s in build if s.get("name") == "Build the site")
        self.assertEqual(step["env"], {"GEMINI_API_KEY": "${{ secrets.GEMINI_API_KEY }}"})
        self.assertNotRegex(self.text, r"AIza[0-9A-Za-z_-]{20,}")                    # no key pasted into the file

    def test_deploy_only_after_a_successful_build_and_runs_do_not_overlap(self):
        self.assertEqual(self.wf["jobs"]["deploy"]["needs"], "build")
        self.assertEqual(self.wf["concurrency"], {"group": "pages", "cancel-in-progress": False})
        self.assertEqual(self.wf["jobs"]["deploy"]["environment"]["name"], "github-pages")

    def test_runs_are_time_limited(self):
        self.assertEqual((self.wf["jobs"]["build"]["timeout-minutes"], self.wf["jobs"]["deploy"]["timeout-minutes"]),
                         (15, 10))

    def test_publishes_the_folder_the_build_writes(self):
        upload = next(s for s in self.wf["jobs"]["build"]["steps"] if "upload-pages-artifact" in s.get("uses", ""))
        self.assertEqual(upload["with"]["path"], "site")
        run = [s["run"] for s in self.wf["jobs"]["build"]["steps"] if "run" in s]
        self.assertIn("python -m plainnews.build", run)


class TestsWorkflowTests(unittest.TestCase):
    def test_runs_on_push_and_pull_request_with_no_secrets_and_read_only_access(self):
        text, wf = load("tests.yml")
        self.assertEqual(set(wf["on"]), {"push", "pull_request"})
        self.assertEqual(wf["permissions"], {"contents": "read"})
        self.assertEqual(SECRET_REF.findall(text), [])
        self.assertIn("python -W error::ResourceWarning -m unittest",
                      [s.get("run") for s in wf["jobs"]["tests"]["steps"]])


class BothWorkflowsTests(unittest.TestCase):
    def test_only_first_party_actions_are_used_with_a_major_version(self):
        for name in ("publish.yml", "tests.yml"):
            text, _ = load(name)
            uses = re.findall(r"uses:\s*(\S+)", text)
            self.assertTrue(uses)
            for ref in uses:
                self.assertRegex(ref, r"^actions/[a-z-]+@v\d+$", f"{name}: {ref}")


if __name__ == "__main__":
    unittest.main()
