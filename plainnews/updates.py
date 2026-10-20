"""Decide whether the page is due for an update, and start the Publish workflow if so.

GitHub's own scheduler can fire late or not at all, so instead of asking it to start a build at exact
times, a small "Check schedule" workflow runs every half hour and asks one question here: has an
update time (settings.update_times, in settings.timezone) passed recently without a successful
Publish run since? If so it starts Publish. A late or lost check is caught by the next one.

The time logic is pure and tested, including the weekends when the clocks change.

    python -m plainnews.updates --dry-run     say whether an update is due, without starting anything
    python -m plainnews.updates               the same, and start Publish if one is due

Needs GITHUB_REPOSITORY (owner/name). Starting a run also needs GITHUB_TOKEN with permission to run
workflows; the dry run works on a public repository without a token.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import Config, load_config

WORKFLOW_FILE = "publish.yml"
ACTIVE = {"queued", "in_progress", "waiting", "pending", "requested"}
DEFAULT_API = "https://api.github.com"
RETRY_WAIT = 2          # seconds; doubles on each retry


class UpdateError(Exception):
    """The check could not do its job (bad settings, or GitHub's API said no)."""


class _Retryable(UpdateError):
    pass


# ----------------------------------------------------------------- the time logic

def parse_times(values) -> list[time]:
    out = []
    for value in values:
        try:
            hour, minute = value.split(":")
            out.append(time(int(hour), int(minute)))
        except (ValueError, AttributeError):
            raise UpdateError(f"not an HH:MM time: {value!r}") from None
    return out


def update_slots(now: datetime, times: list, tz, window_minutes: int) -> list:
    """Every update time (as a UTC moment) on the local days that could still be inside the window."""
    local_today = now.astimezone(tz).date()
    days_back = window_minutes // 1440 + 1
    slots = set()
    for offset in range(days_back + 1):
        day = local_today - timedelta(days=offset)
        for t in times:
            # A time that does not exist (the hour the clocks skip) or exists twice (the hour they repeat)
            # still gives one slot per day: zoneinfo takes the first reading.
            slots.add(datetime.combine(day, t).replace(tzinfo=tz).astimezone(timezone.utc))
    return sorted(slots)


def longest_gap_minutes(times: list) -> int:
    """The longest wait between consecutive update times, counting the wrap from the last one to the first."""
    minutes = sorted(t.hour * 60 + t.minute for t in times)
    if len(minutes) < 2:
        return 1440
    gaps = [b - a for a, b in zip(minutes, minutes[1:])] + [minutes[0] + 1440 - minutes[-1]]
    return max(gaps)


def latest_slot(now: datetime, times: list, tz, window_minutes: int):
    """The most recent update time that has passed, if it is still within the window; else None."""
    window = timedelta(minutes=window_minutes)
    eligible = [s for s in update_slots(now, times, tz, window_minutes) if s <= now and now - s <= window]
    return max(eligible) if eligible else None


# ------------------------------------------------------------------ the decision

@dataclass
class Decision:
    due: bool
    reason: str
    slot: datetime | None = None


def _when(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def decide(now: datetime, config: Config, runs: list, tz) -> Decision:
    """runs: recent Publish runs as GitHub's API returns them (created_at, status, conclusion)."""
    slot = latest_slot(now, parse_times(config.update_times), tz, config.update_window_minutes)
    if slot is None:
        return Decision(False, f"no update time passed in the last {config.update_window_minutes} minutes")
    label = slot.astimezone(tz).strftime("%H:%M")

    if any(r.get("status") in ACTIVE for r in runs):
        return Decision(False, "a Publish run is already queued or running", slot)
    since = [r for r in runs if _when(r["created_at"]) >= slot]
    if any(r.get("conclusion") == "success" for r in since):
        return Decision(False, f"the {label} update is done: a Publish run succeeded after it", slot)
    if len(since) >= config.max_attempts_per_update:
        return Decision(False, f"the {label} update is not done, but {len(since)} runs have already been tried "
                               f"(limit {config.max_attempts_per_update})", slot)
    tried = f"{len(since)} earlier attempt(s) failed" if since else "no run since"
    return Decision(True, f"the {label} update is due ({tried})", slot)


# ------------------------------------------------------------------- GitHub's API

def _headers(token):
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _call(method: str, url: str, token, **kwargs):
    for attempt in Retrying(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=RETRY_WAIT, min=RETRY_WAIT, max=10),
                            retry=retry_if_exception_type(_Retryable), reraise=True):
        with attempt:
            try:
                resp = requests.request(method, url, headers=_headers(token), timeout=30, **kwargs)
            except requests.RequestException:
                raise _Retryable("could not reach GitHub's API") from None
            if resp.status_code == 429 or resp.status_code >= 500:
                raise _Retryable(f"GitHub's API said HTTP {resp.status_code}")
    return resp


def fetch_runs(api: str, repo: str, token) -> list:
    resp = _call("GET", f"{api}/repos/{repo}/actions/workflows/{WORKFLOW_FILE}/runs?per_page=30", token)
    if resp.status_code != 200:
        raise UpdateError(f"could not list Publish runs: HTTP {resp.status_code}")
    return resp.json().get("workflow_runs", [])


def dispatch(api: str, repo: str, token, ref: str) -> None:
    if not token:
        raise UpdateError("GITHUB_TOKEN is needed to start a run")
    resp = _call("POST", f"{api}/repos/{repo}/actions/workflows/{WORKFLOW_FILE}/dispatches", token, json={"ref": ref})
    if resp.status_code != 204:
        raise UpdateError(f"could not start Publish: HTTP {resp.status_code}")


def run_check(config: Config, repo: str, token, api: str = DEFAULT_API, ref: str = "main",
              now: datetime | None = None, dry_run: bool = False) -> Decision:
    now = now or datetime.now(timezone.utc)
    try:
        tz = ZoneInfo(config.timezone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise UpdateError(f"no timezone data for {config.timezone!r} (pip install tzdata)") from None
    decision = decide(now, config, fetch_runs(api, repo, token), tz)
    if decision.due and not dry_run:
        dispatch(api, repo, token, ref)
    return decision


# ------------------------------------------------------------------- command line

def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        print("Set GITHUB_REPOSITORY to owner/name, for example dimkaram/plain-news.")
        return 2
    config = load_config()
    now = datetime.now(timezone.utc)
    try:
        london = now.astimezone(ZoneInfo(config.timezone)).strftime("%Y-%m-%d %H:%M:%S %Z")
    except (ZoneInfoNotFoundError, ValueError, OSError):
        london = "(no timezone data)"
    print(f"event:      {os.environ.get('GITHUB_EVENT_NAME', 'local')} {os.environ.get('SCHEDULE', '')}".rstrip())
    print(f"now (UTC):  {now:%Y-%m-%d %H:%M:%S}")
    print(f"now ({config.timezone}): {london}")
    try:
        decision = run_check(config, repo, os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_API_URL", DEFAULT_API),
                             os.environ.get("GITHUB_REF_NAME", "main"), now, dry_run="--dry-run" in argv)
    except UpdateError as exc:
        print(f"Check failed: {exc}")
        return 1
    gap = longest_gap_minutes(parse_times(config.update_times))
    if config.update_window_minutes < gap:
        print(f"note:       update_window_minutes ({config.update_window_minutes}) is shorter than the longest gap between "
              f"update times ({gap}), so a missed update can lapse before the next check arrives")
    print(f"decision:   {decision.reason}")
    if decision.due:
        print("dry run: nothing started" if "--dry-run" in argv else "started the Publish workflow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
