# Export job URLs

Fetch apply URLs for jobs that match a freehire search filter and write them to a text file.

Each export line is `posted_at<TAB>created_at<TAB>url`. By default only
**yesterday in America/New_York** is kept — `posted_at` must fall on that day.
`--new-only` also drops an earlier `created_at` (a source `updated_at` bump);
a later `created_at` (ingest after midnight) is kept.
The file starts with `# date:` / `# timezone:`. `day_register.py` turns that
day's list into a register (new postings first, then `posted_at` bumps,
paragraphs of 10, count at the end).

No sign-in, API key, local database, or other freehire code is required. The script uses only the Python 3 standard library.

Filter parameter names match the freehire **job search API / SPA** (`GET /api/v1/jobs/search`, see `docs/API.md` § Filtering jobs).

## Requirements

- Python 3.9+ (no extra packages), **or** [uv](https://docs.astral.sh/uv/) to run without a separate Python install
- Network access to `https://freehire.me` (or the host given with `--base-url`)

## Quick start

```bash
cd export-job-urls
python export_job_urls.py
```

### With uv (no Python on PATH)

[uv](https://docs.astral.sh/uv/) downloads a Python interpreter on first run and needs no extra packages — useful on Windows when `python` is missing or only the Microsoft Store stub is installed:

```bash
cd export-job-urls
uv run export_job_urls.py
uv run day_register.py
```

Optional: pin a version on first run — `uv run --python 3.12 export_job_urls.py`. Every flag and filter below works the same; substitute `uv run` for `python` in any example.

With no filters on the command line, the built-in defaults are used:

- `work_mode=remote`
- `countries=us`
- `category=software_engineering,backend,frontend,fullstack,mobile`
- `semantic_ratio=0`
- **calendar date = yesterday in America/New_York** (`--date`; pass `--date all` to skip). `--new-only` also drops a `posted_at` bump whose `created_at` is an earlier day; it needs a calendar day (`--date all --new-only` is refused)
- `posted_within_days=3` is only the API fetch window, then the date cut is applied
- After the API match, a job whose `location` names a country **outside** `countries` is dropped (role-cluster geography can tag a Spain listing `us`). Bare `Remote` / a US city / empty location is kept

Output file: `exports/job_urls_YYYY-MM-DD_HHMMSS.txt` (header + `posted_at<TAB>created_at<TAB>url`).

### Daily register

```bash
python day_register.py
```

Default: **previous calendar day in America/New_York** (EST/EDT), same search
filters and date cut as `export_job_urls.py` — including the location post-filter
that drops a posting whose `location` names a country outside `countries=us`.
The list is **new postings first**, then `posted_at` bumps — Slack and the
register file both use that order, so the first paragraphs are the real new
jobs. `--new-only` still drops those bumps entirely. The register file starts
with `# date:` and `# timezone:`.

```bash
python day_register.py --date 2026-08-15
python day_register.py --from exports/job_urls_2026-08-17_000819.txt
python day_register.py --from exports/job_urls_20260818_000819.txt --date 2026-08-15
```

`--from` without `--date` uses the export's `# date:` header, and without
`--tz` / `--tz-offset` uses `# timezone:` as well. A file of bare URLs (no
`posted_at` column) is treated as already being that day's list.

A second run for the same date keeps **one file**: it diffs against URLs
already recorded for that day and appends only the new ones (`# count` is
updated). A URL already in the file stays there even if a later fetch
drops it. When Slack is on, each 10-URL block is posted and then written;
a failed block leaves the earlier ones recorded, and a retry sends only
what is still missing.

| File                                   | Role                                                               |
| -------------------------------------- | ------------------------------------------------------------------ |
| `day-register/register/YYYY-MM-DD.txt` | The day’s list: `# date` / `# timezone`, URLs in 10s, `# count: N` |

No new URLs → the file is left as it is, and Slack is not called.

### Slack (`#plan_list`)

`day_register.py` can post **10-URL paragraphs** to Slack. After each
message succeeds it updates the register, so a later run only posts URLs
that are not yet in the file.
The script never takes a channel name: the Incoming Webhook URL is bound to
one channel when you create it. Make that channel `#plan_list`. A webhook URL
in `.env` is enough; no other Slack config.

#### 1. Channel

In Slack: **Channels → Create → Create a channel**, name `plan_list`.
For a private channel, you must already be a member before you can attach a webhook.

#### 2. Slack app + Incoming Webhook

1. Open [https://api.slack.com/apps](https://api.slack.com/apps) → **Create New App**.
2. Choose **Blank app** (not AI agent / Starter app / From a manifest).
3. Name it (e.g. `day-register`), pick the workspace, create it.
4. Left sidebar: **Features → Incoming Webhooks** → **Activate Incoming Webhooks** On.
5. **Add New Webhook to Workspace** → select `plan_list` → **Allow**.
6. Copy the URL under **Webhook URLs for Your Workspace**:
   `https://hooks.slack.com/services/T…/B…/…`
   Anyone with this URL can post to that channel. Do not commit it or paste it in chat.

A workspace admin may have to allow app installs.

#### 3. Test the webhook (Windows PowerShell)

Slack’s docs use Unix single quotes; those break in cmd/PowerShell
(`Could not resolve host: application`, Slack `invalid_payload`). Use `curl.exe`
and double quotes around the header:

```powershell
curl.exe -X POST -H "Content-Type: application/json" --data '{"text":"Hello, World!"}' "https://hooks.slack.com/services/T.../B.../..."
```

`#plan_list` should show `Hello, World!` from the app. Hover the message → **⋮** →
**Delete message** to remove a test post (Incoming Webhooks cannot delete; a
workspace that only allows “delete own messages” needs an admin).

#### 4. Put the URL in `.env`

In `export-job-urls/`, copy `.env.example` to `.env` (gitignored) and paste the
real URL. No extra packages; `day_register.py` reads this file itself.

```
DAY_REGISTER_SLACK_WEBHOOK=https://hooks.slack.com/services/T.../B.../...
```

Precedence: `--slack-webhook` → process env → `.env`.

#### 5. Post the day’s list

```bash
cd export-job-urls
python day_register.py --slack
```

`--slack` posts one 10-URL paragraph, writes those URLs, then the next
paragraph, and **exits non-zero if the webhook is missing** so a forgotten
`.env` cannot look like a successful post. A Slack failure leaves
already-posted blocks in the file; a retry sends the rest. A timeout after
Slack accepted a message can still re-post that one block. If `.env` (or
the env var) is already set, `python day_register.py` with no flags also
posts when there is something new. `--no-slack` writes the file only. A
re-run that finds no new URLs skips Slack even with `--slack`.

What appears in `#plan_list`: one Slack message per paragraph of 10 new URLs.
No header. Truly new postings go out first; `posted_at` bumps follow. The
day’s file on disk holds the full list in that same order.

About 178 URLs on the first run → 18 messages. To build from an
already-exported dump without calling the API again:

```bash
python day_register.py --from exports/job_urls_2026-08-25_000819.txt --date 2026-08-25 --slack
```

`--from` a register that is already on disk for that date posts nothing — those
URLs are already seen. A file of bare URLs (no `posted_at`) is treated as that
day's list.

## Filters (same as the website / API)

### 1) Paste a URL from the site

Pass a freehire search URL from the browser, or an API search URL:

```bash
python export_job_urls.py --from-url "https://freehire.me/?work_mode=remote&category=software_engineering,backend,frontend,fullstack,mobile,devops,data_engineering,ai_engineering&countries=us&posted_within_days=3"
```

```bash
python export_job_urls.py --from-url "https://freehire.me/api/v1/jobs/search?work_mode=remote&countries=us&limit=20"
```

`limit` / `offset` are ignored; the script paginates through the full result set.

### 2) Raw query string

```bash
python export_job_urls.py --query "skills=go,rust&skills_mode=and&work_mode=remote&regions=eu"
# short form
python export_job_urls.py -Q "seniority=senior&work_mode=remote&countries=us"
```

### 3) CLI flags (1:1 with API params)

Hyphens map to underscores. Example: `--work-mode` → `work_mode`.

```bash
python export_job_urls.py \
  --work-mode remote \
  --category backend,frontend,fullstack \
  --countries us \
  --posted-within-days 3 \
  --skills go,rust \
  --skills-mode and \
  --company-type-exclude outstaff \
  --sort posted_at
```

String facets (same set as `StringFacets`):

`source`, `company_slug`, `regions`, `work_mode`, `employment_type`, `education_level`,
`seniority`, `category`, `domains`, `countries`, `cities`, `company_type`, `company_size`,
`salary_currency`, `salary_period`, `skills`, `is_tech`, `role`, `ai_archetype`, `reality`,
`collections`, `relocation`, `english_level`, `posting_language`

Each facet also supports `--<facet>-mode and|or` and `--<facet>-exclude VAL`.

Other search params:

`--q`, `--sort`, `--order`, `--semantic-ratio`, `--visa-sponsorship`,
`--salary-min`, `--salary-max`, `--experience-years-min`, `--posted-within-days`

Precedence: **CLI flags >** `--query` **>** `--from-url`.
If **no** filter flags / `--query` / `--from-url` are set, `DEFAULT_FILTERS` at the top of the script is used.
Passing any filter replaces that default set entirely (it does not merge onto remote/US/3-day).

Print the resolved filters without calling the API:

```bash
python export_job_urls.py --from-url "https://freehire.me/?work_mode=remote&countries=us" --print-filters
```

## Use anywhere (copy this folder)

1. Copy the whole `export-job-urls/` folder (`export_job_urls.py` and `day_register.py`).
2. Run `python export_job_urls.py ...` or `python day_register.py` from that location (or `uv run export_job_urls.py` / `uv run day_register.py` if you use uv).

Each script writes next to itself, not the current working directory, and creates its folder on first run:

- `export_job_urls.py` → `exports/` (timestamped dumps)
- `day_register.py` → `day-register/register/`

Generated files are gitignored; `.gitkeep` keeps the empty directories in version control.

## Other options

| Option              | Meaning                                                                                                  |
| ------------------- | -------------------------------------------------------------------------------------------------------- |
| `-o PATH`           | Output path (relative paths are under `exports/`)                                                        |
| `--base-url URL`    | API origin (default `https://freehire.me`)                                                               |
| `--page-size N`     | Page size 1–100 (default 100)                                                                            |
| `--keep-utm`        | Keep `?utm_source=freehire.me`                                                                           |
| `--print-filters`   | Print resolved filters, date, timezone, and `new_only`, then exit                                        |
| `--date YYYY-MM-DD` | Keep this calendar day (default: yesterday, NY; `all` = no cut)                                          |
| `--new-only`        | Drop a `posted_at` bump (`created_at` before that day). Default keeps bumps but lists them after new postings. Needs a calendar day — refused with `--date all` |
| `--tz NAME`         | IANA timezone for `--date` (default `America/New_York`)                                                  |
| `--tz-offset H`     | Hours east of UTC; overrides `--tz`                                                                      |

`day_register.py` extras:

| Option              | Meaning                                                                       |
| ------------------- | ----------------------------------------------------------------------------- |
| `--date YYYY-MM-DD` | Register this day (default: `# date:` from `--from`, else yesterday)          |
| `--new-only`        | Drop a `posted_at` bump. Default keeps bumps after the new postings          |
| `--tz NAME`         | IANA timezone (default: `# timezone:` from `--from`, else `America/New_York`) |
| `--tz-offset H`     | Hours east of UTC; overrides `--tz` and the export header                     |
| `--from PATH`       | Read an export file instead of calling the API                                |
| `--block-size N`    | URLs per paragraph (default 10)                                               |
| `--slack`           | Post to Slack; fail if no webhook                                             |
| `--no-slack`        | Do not post even if `DAY_REGISTER_SLACK_WEBHOOK` is set                       |
| `--slack-webhook`   | Incoming Webhook URL (default: `$DAY_REGISTER_SLACK_WEBHOOK`)                 |

## Notes

- No auth — public search API.
- Duplicate apply URLs are written once (stderr reports when dedup drops any).
- Repeated query keys in `--from-url` / `-Q` (e.g. `skills=go&skills=rust`) are merged into a comma list.
- Tracking UTM is stripped by default; ATS links work without it.
- API window limit: `offset + limit ≤ 10000`. Both scripts warn when the
  newest 10000 matches do not reach back to `--date`.

## Git (fork + `feat/export-job-urls`)

If you work from a fork and track `strelov1/freehire` as `upstream`, see
**[git-fork-workflow.md](git-fork-workflow.md)** for syncing `main`, rebasing
this branch, and pushing with `--force-with-lease`.
