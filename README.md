# Plain news

Static, non-personalised news summaries by region, aggregated from multiple sources.

Several times a day (four by default) a GitHub Action reads a set of news feeds, asks an AI model to merge duplicate stories and write
a short summary of each, ranks the stories, and publishes a single static web page on GitHub Pages. The page
shows the top stories overall (**All**) and for each region. Regions are just labels you choose, so they can
be countries, cities or topics.

**What it is not:** it does not personalise, track or profile anyone, it has no server or database, it
does not translate (each summary is in its source's own language), it does not copy article text, and it
sends no emails or notifications.

## Contents

1. [Quick start](#quick-start)
2. [How it works](#how-it-works)
3. [Configuration](#configuration)
4. [Sources and regions](#sources-and-regions)
5. [Schedule](#schedule)
6. [The AI model](#the-ai-model)
7. [Keeping it running](#keeping-it-running)
8. [Running it locally](#running-it-locally)
9. [The status log](#the-status-log)
10. [Privacy, publishers and limits](#privacy-publishers-and-limits)
11. [Troubleshooting](#troubleshooting)
12. [Licence](#licence)

## Quick start

You need a GitHub account and a Google account. Everything runs on free tiers.

1. **Fork or clone** this repository to your own account. It must be public for free GitHub Pages.
2. **Turn on scheduled runs if you forked it.** GitHub disables scheduled workflows in forks by default. Open
   the **Actions** tab and enable workflows.
3. **Turn on Pages:** *Settings, Pages, Build and deployment, Source: GitHub Actions.*
4. **Get a Gemini key** in [Google AI Studio](https://aistudio.google.com). Create a **new project** for it
   and check that the project's billing tier does not say Paid or Tier 1. A project with no billing linked should
   not be able to spend money: if it runs out of free quota it simply gets errors.
5. **Store the key as a secret:** *Settings, Secrets and variables, Actions, New repository secret.* Name it
   `GEMINI_API_KEY` exactly and paste the key. Never put the key in a file.
6. **Edit `sources.yaml`:** set `site.title` and `site.repo_url`, choose your regions and sources, and set
   `ai.model` (see [The AI model](#the-ai-model)).
7. **Run it:** *Actions, Publish, Run workflow.* When both jobs are green, the **deploy** job shows your
   page address, normally `https://YOUR-NAME.github.io/REPOSITORY/`.
8. **Optional, on an Android phone:** open the page in Chrome and choose *Add to Home screen*.

If the secret is missing or wrong, the build still succeeds: every region falls back to raw headlines and the
page says so in its header. If no feed can be reached at all, the build fails and the previous page stays live.

## How it works

1. **Fetch:** every enabled source's feed is downloaded, with retries. A dead feed is skipped and logged. Items
   older than `settings.max_age_hours` are dropped.
2. **Clean:** titles and snippets lose zero-width characters, and sentences that contain boilerplate phrases
   (support-line notices, appeals, promotional lines) are removed from snippets.
3. **Summarise:** for each region and language, one AI call receives the titles and snippets, never full
   articles. It groups items that report the same event, picks a lead item, writes a short summary and scores
   the story from 1 to 10. A required answer format is enforced by the model's API.
4. **Check and rank:** code checks every answer. Headlines are always the publishers' own titles. Summaries
   are dropped if they contain links or markup, numbers not found in the source text, or nothing beyond the
   headline, and a cut-off ending is trimmed back to the last complete sentence. Each extra source covering a
   story adds to its score (see `ai.coverage_bonus_per_source`).
5. **Choose:** each region tab shows its top stories. The **All** tab shows one card per event: stories from
   different regions that share an article link are merged, and the version that scored highest leads.
6. **Images:** a card uses the feed's image, then an image inside the feed text, then the article page's
   preview image (`og:image`, read from the start of the page only).
7. **Publish:** the page, a manifest and icons are written to `site/` and deployed to GitHub Pages.

If a step fails, the previous page stays live and GitHub emails you about the failed run.

## Configuration

Everything is in `sources.yaml`. Changing sources, regions or settings needs no code change. Defaults apply
when a setting is left out.

### Page and run settings

| Setting | Default | What it does |
|---|---|---|
| `site.title` | `Plain news` | Title of the page, the manifest and the footer. |
| `site.description` | (empty) | Used for the page's search and sharing information only. |
| `site.repo_url` | (empty) | Adds a "Source code" link to the footer. Leave empty to hide the link. |
| `site.footer_note` | (empty) | An optional extra line in the footer. |
| `settings.timezone` | `Europe/London` | Timezone for the date and update time shown on the page. |
| `settings.max_age_hours` | `48` | Items older than this are ignored. |
| `settings.items_per_region` | `5` | Stories shown on each region tab. |
| `settings.items_in_all` | `15` | Stories shown on the All tab. |
| `settings.default_language` | `en` | Language of sources that do not set one. Also the page language. |
| `settings.stale_after_hours` | `24` | A source whose newest item is older than this is flagged as stale. |
| `settings.commit_warning_days` | `45` | The status log warns when the last commit is older than this. |
| `settings.update_times` | `07:15`, `12:15`, `17:45`, `21:15` | When the page should be refreshed, as `HH:MM` times in `settings.timezone`. See [Schedule](#schedule). |
| `settings.update_window_minutes` | `120` | An update stays due for this long after its time, so a late check still catches up. |
| `settings.max_attempts_per_update` | `4` | Runs tried for one update time before waiting for the next one, so a persistent fault cannot flood you with failure emails. |

### AI settings

| Setting | Default | What it does |
|---|---|---|
| `ai.provider` | `gemini` | The only provider built in. |
| `ai.model` | (none: you must set it) | The model name. List the ones your key can use with `python -m plainnews.ai --list-models`. |
| `ai.api_key_env` | `GEMINI_API_KEY` | Name of the environment variable that holds the key. |
| `ai.summary_max_sentences` | `2` | Summaries are "up to" this many sentences. Thin snippets give shorter or empty summaries. |
| `ai.max_stories_per_region` | `25` | The most stories the AI may return for a region. |
| `ai.min_stories_share` | `0.5` | The model must return at least this share of the items sent (up to the maximum), so it cannot stop early. Set `0` to switch the minimum off. |
| `ai.max_calls_per_run` | `12` | Hard cap on AI calls per run. An unusable answer is asked for again once. |
| `ai.max_output_tokens` | `8192` | Longest answer allowed per call. |
| `ai.timeout_seconds` | `90` | How long to wait for one answer. |
| `ai.retries` | `3` | Attempts per call for timeouts, connection errors and HTTP 429 or 5xx. |
| `ai.backoff_seconds` | `2` | Base wait between those attempts, doubling each time. |
| `ai.coverage_bonus_per_source` | `1` | Added to a story's score for each extra source covering it. |
| `ai.coverage_bonus_max` | `2` | The largest total bonus. Scores never exceed 10. |
| `extra_rules` | (empty) | Extra instructions for the AI, applied to every region. Empty means plain news with no topic filtering. For example: `Ignore sport.` |

### Fetching and images

| Setting | Default | What it does |
|---|---|---|
| `fetch.timeout_seconds` | `20` | How long to wait for a feed. |
| `fetch.retries` | `3` | Attempts per feed for timeouts, connection errors and HTTP 429 or 5xx. |
| `fetch.backoff_seconds` | `1` | Base wait between those attempts, doubling each time. |
| `fetch.max_workers` | `6` | Feeds downloaded at the same time. |
| `fetch.snippet_chars` | `300` | How much of each snippet the AI sees. |
| `fetch.user_agent` | `Mozilla/5.0 (compatible; plain-news/0.1)` | Sent with every request. |
| `fetch.boilerplate` | a built-in list in English and Greek | Sentences containing any of these phrases are removed from snippets (ignoring case and accents). Setting it replaces the built-in list; `[]` turns the filter off. |
| `images.og_image_fallback` | `true` | Read the article page's preview image when the feed has none. |
| `images.max_page_fetches_per_run` | `40` | Most article pages fetched per run. |
| `images.timeout_seconds` | `8` | How long to wait for an article page. |

## Sources and regions

### Regions

```yaml
regions:
  - id: world
    label: World
  - id: uk
    label: UK
label_priority: [uk, world]
```

- The order of `regions` is the order of the tabs. The **All** tab is always first.
- `label_priority` lists every region id once. When two versions of a story tie on score, the region listed first
  is shown.
- Regions are only labels, so you can use categories (`science`, `economy`) instead of places. The AI is told
  to score stories "for a reader following *label* news".

### Sources

The `sources` list holds one entry per feed:

```yaml
sources:
  - name: BBC News
    region: uk
    feed_url: https://feeds.bbci.co.uk/news/uk/rss.xml
    max_items_considered: 15
```

| Field | Default | What it does |
|---|---|---|
| `sources[].name` | required | Shown on the page and in the status log. |
| `sources[].region` | required | Must be one of the region ids. |
| `sources[].language` | `settings.default_language` | A language code such as `en` or `el`. Summaries are written in this language, and stories are only merged within one language. |
| `sources[].feed_url` | required | An RSS or Atom address starting with `http://` or `https://`. |
| `sources[].max_items_considered` | required | How many of the feed's items the AI sees. Newest-first feeds (large news sites) need more, and editor-ranked ones fewer. |
| `sources[].paywalled` | `false` | Marks the source on the card. A paywalled source never leads a story when a free one is available, and its pages are not fetched for images. |
| `sources[].enabled` | `true` | `false` keeps the source in the file but skips it. |

**To add a source:** add an entry, then run `python -m plainnews.fetch` to see whether it works, how many items it
gives and how many have images. **To remove one:** delete the entry, or set `enabled: false`. A source may appear
in more than one region.

## Schedule

GitHub's own scheduler can fire late or not at all, so the page is not started by a timer set to exact times.
Instead a small workflow, **Check schedule**, runs every half hour and asks one question: has an update time passed
in the last `settings.update_window_minutes` without a successful **Publish** run since? If so, it starts
**Publish**. A late or lost check is simply caught by the next one.

```yaml
schedule:
  - cron: "17 * * * *"
  - cron: "47 * * * *"
```

- **When:** set the update times in `settings.update_times`, as local times in `settings.timezone`. Clock changes
  are handled by tested code, not by GitHub. The defaults are 07:15, 12:15, 17:45 and 21:15 UK time.
- **Delay:** an update starts at the first check after its time, so up to about half an hour late, plus the build.
- **Failures:** a failed run is retried at the next checks, up to `settings.max_attempts_per_update` times per update
  time, then it waits for the next update time.
- **Run now:** *Actions, Publish, Run workflow.* A manual run counts as the update for any update time before it.
- **What the check thinks:** `GITHUB_REPOSITORY=owner/name python -m plainnews.updates --dry-run` says whether an
  update is due and why, without starting anything.
- **Cost:** about 48 short checks a day, each under a minute. Standard runners are free for public repositories, and
  the checks make no AI calls: only **Publish** does.
- **Permissions:** the check workflow may start workflows (`actions: write`) and read the repository, nothing else.
  It uses the built-in token, so there is nothing to create or store.
- **Avoid the start of an hour** if you change the check times: GitHub can delay scheduled runs when it is busy,
    and :00 is the busiest time.

## The AI model

The model is set by `ai.model`, and the key comes from the `GEMINI_API_KEY` secret.

- **Free tier:** at the time of writing (October 2026) Google's free tier covers its Flash and Flash-Lite models.
  Models, limits and the tier itself change, so check your project's numbers on AI Studio's **Rate Limit** page.
  A run needs about one call per region, so a handful of calls a day.
- **Choose a model:** run `python -m plainnews.ai --list-models` with your key set, and pick a recent Flash-Lite
  or Flash model that is not marked as a preview.
- **When a model is retired,** the status log shows a failure (often HTTP 404) and the regions fall back to raw
  headlines. List the models again and change `ai.model`.
- **A free key is safest:** use a project with no billing. Never put the key in a file or in a workflow.
- **To use another provider,** add it in `plainnews/ai.py`: a provider is a class with one `generate` method.

## Keeping it running

GitHub disables scheduled workflows in a public repository after 60 days without repository activity. Do not
count on the scheduled runs themselves as activity. It is the **Check schedule** workflow's own schedule that GitHub switches off, and then nothing starts the page. The
status log warns you when the last commit is older than `settings.commit_warning_days`.

To restart the clock, make any commit. A routine way is to edit the number in the `VERSION` file and push it. The
page shows that version and the commit date in its status log and footer.

If a scheduled workflow has been disabled, open the **Actions** tab and re-enable it.

## Running it locally

You need Python 3.12 or newer.

```
python -m pip install -r requirements.txt       # or: conda env create -f environment.yml
python -m unittest                              # the tests
python -m plainnews.fetch                       # fetch every feed and report how each one did
python -m plainnews.ai --list-models            # models your key can use (key required)
python -m plainnews.ai                          # fetch and summarise; saves the result in data/
python -m plainnews.ai --debug                  # also show what the model left out
python -m plainnews.views                       # the tab choices from the last saved run
python -m plainnews.images                      # try the article-page image lookup
python -m plainnews.build                       # the whole pipeline; writes site/
python -m plainnews.build --from-saved          # rebuild site/ from data/, with no network use
python -m plainnews.build --from-saved --with-images
```

Set the key without saving it in your shell history:

```
read -rs GEMINI_API_KEY
export GEMINI_API_KEY
```

To look at the page, run `cd site` and `python -m http.server 8000`, then open `http://localhost:8000`. The
folders `site/` and `data/` are ignored by git.

## The status log

The page ends with a collapsed **Status** section. If anything needs attention, a short note also appears in the
page header and links to it.

- **Unavailable:** a feed could not be fetched, and the reason (for example HTTP 403) is shown.
- **Stale:** a feed works but its newest item is older than `settings.stale_after_hours`.
- **Raw headlines:** the AI step failed for a region, so its cards show headlines and links without summaries.
- **Left without a summary:** the snippets added nothing to the headline, or a check removed the summary.
- **Images:** how many came from feeds, from article pages, and how many cards have none.
- **Last commit:** the age of the latest commit, with the 60-day warning described above.

## Privacy, publishers and limits

**Privacy.** The page sets no cookies, runs no scripts from other sites, loads no fonts and has no analytics.
It stores nothing about the people who read it, and nothing on the page affects what appears next time. Two things
remain: thumbnails are loaded straight from the publishers' servers, so a publisher can see the visitor's network
address when a thumbnail loads (the page asks browsers not to send a referrer), and the page is hosted by GitHub
Pages, which has its own policies.

**Publishers.** Headlines, images and links belong to their publishers, and the page links to the original
articles. The MIT licence covers this project's code only. Check each publisher's terms for using its feeds
before you publish a page publicly.

**AI summaries can be wrong.** The checks above remove many problems but cannot prove a summary is correct. The
footer says so, and the headline and links are always there to follow.

**Known limits**

- A story covered in both English and Greek can appear twice on the All tab, because stories are only matched by
  shared links.
- Many sources return the newest items first rather than the most important, which is why `max_items_considered`
  matters, and why the model, not the feed order, decides what ranks highly.
- Feeds move and disappear without notice. The status log is how you will find out.
- **The default sources are a choice, not a verdict.** The Greek set has no source left of centre-left, the Daily
  Mail sits further right than "centre-right", and NBC News adds a US flavour to World. Edit `sources.yaml` to
  suit your own view.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| The page shows raw headlines only | The key is missing or wrong, the model name is retired, or the free quota ran out. The status log gives the reason. |
| The deploy job fails | Pages is not set to **GitHub Actions** as its source. |
| The page is not updating | Open the **Check schedule** runs in the **Actions** tab. Each prints its decision. If it never runs, workflows are disabled (a fork, or 60 days without activity): enable them. If it keeps saying an update is not due, check `settings.update_times` and `settings.timezone`. |
| A source shows HTTP 403 | The publisher blocks cloud servers, or the feed address changed. Try the address in a browser, or replace the source. |
| A region has no cards | Its sources gave no items newer than `settings.max_age_hours`, or all of them failed. |

## Licence

MIT. See `LICENSE`.
