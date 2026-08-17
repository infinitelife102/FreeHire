# Export job URLs

Fetch apply URLs for jobs that match a freehire search filter and write them to a text file.

No sign-in, API key, local database, or other freehire code is required. The script uses only the Python 3 standard library.

Filter parameter names match the freehire **job search API / SPA** (`GET /api/v1/jobs/search`, see `docs/API.md` § Filtering jobs).

## Requirements

- Python 3.9+ (no extra packages)
- Network access to `https://freehire.me` (or the host given with `--base-url`)

## Quick start

```bash
cd export-job-urls
python export_job_urls.py
```

With no filters on the command line, the built-in defaults are used (remote, US, last 3 days, categories: software_engineering, backend, frontend, fullstack, mobile, devops).

Output file: `urls/job_urls_YYYYMMDD_HHMMSS.txt`.

## Filters (same as the website / API)

### 1) Paste a URL from the site

Pass a freehire search URL from the browser, or an API search URL:

```bash
python export_job_urls.py --from-url "https://freehire.me/?work_mode=remote&category=software_engineering,backend,frontend,fullstack,mobile,devops&countries=us&posted_within_days=3"
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

Precedence: **CLI flags > `--query` > `--from-url`**.  
If **no** filter flags / `--query` / `--from-url` are set, `DEFAULT_FILTERS` at the top of the script is used.  
Passing any filter replaces that default set entirely (it does not merge onto remote/US/3-day).

Print the resolved filters without calling the API:

```bash
python export_job_urls.py --from-url "https://freehire.me/?work_mode=remote&countries=us" --print-filters
```

## Use anywhere (copy this folder)

1. Copy the whole `export-job-urls/` folder (or just `export_job_urls.py`).
2. Run `python export_job_urls.py ...` from that location.

The output file is always written under `urls/` next to the script (timestamped name), regardless of the current working directory. The folder is created automatically on first run. Generated `job_urls_*.txt` files are gitignored; `.gitkeep` keeps the empty `urls/` directory in version control.

## Other options

| Option | Meaning |
| --- | --- |
| `-o PATH` | Output path (relative paths are under `urls/`) |
| `--base-url URL` | API origin (default `https://freehire.me`) |
| `--page-size N` | Page size 1–100 (default 100) |
| `--keep-utm` | Keep `?utm_source=freehire.me` |
| `--print-filters` | Print resolved filters and exit |

## Notes

- No auth — public search API.
- Duplicate apply URLs are written once (stderr reports when dedup drops any).
- Repeated query keys in `--from-url` / `-Q` (e.g. `skills=go&skills=rust`) are merged into a comma list.
- Tracking UTM is stripped by default; ATS links work without it.
- API window limit: `offset + limit ≤ 10000`.
