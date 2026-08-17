#!/usr/bin/env python3
"""Export job apply URLs from the public freehire search API into a text file.

Auth is not required — GET /api/v1/jobs/search is public.
This script is self-contained (stdlib only) and can be copied anywhere.

Filters match the freehire job-search API / SPA query vocabulary
(docs/API.md § Filtering jobs, internal/search StringFacets).

Usage:
  python export_job_urls.py
  python export_job_urls.py --work-mode remote --countries us --category backend,frontend
  python export_job_urls.py --from-url "https://freehire.me/?work_mode=remote&countries=us&posted_within_days=3"
  python export_job_urls.py --query "skills=go,rust&skills_mode=and&work_mode=remote"
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_BASE = "https://freehire.me"
PAGE_SIZE = 100  # API max is 100
MAX_WINDOW = 10_000  # API hard stop: offset + limit ≤ 10000

SCRIPT_DIR = Path(__file__).resolve().parent
# Timestamped URL lists land here (created on first run if missing).
OUTPUT_DIR = SCRIPT_DIR / "urls"

# Mirrors internal/search.StringFacets keys (equality facets).
STRING_FACETS = (
    "source",
    "company_slug",
    "regions",
    "work_mode",
    "employment_type",
    "education_level",
    "seniority",
    "category",
    "domains",
    "countries",
    "cities",
    "company_type",
    "company_size",
    "salary_currency",
    "salary_period",
    "skills",
    "is_tech",
    "role",
    "ai_archetype",
    "reality",
    "collections",
    "relocation",
    "english_level",
    "posting_language",
)

# Non-facet search params (same names as GET /jobs/search).
SEARCH_EXTRAS = (
    "q",
    "sort",
    "order",
    "semantic_ratio",
    "visa_sponsorship",
    "salary_min",
    "salary_max",
    "experience_years_min",
    "posted_within_days",
)

# Dropped when importing a SPA/API URL — pagination is owned by this script.
PAGINATION_KEYS = frozenset({"limit", "offset"})

# Used only when the caller passes no filters at all.
DEFAULT_FILTERS: dict[str, str] = {
    "work_mode": "remote",
    "category": "software_engineering,backend,frontend,fullstack,mobile,devops",
    "countries": "us",
    "posted_within_days": "3",
    "semantic_ratio": "0",
}


def default_output_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return OUTPUT_DIR / f"job_urls_{stamp}.txt"


def parse_query_string(raw: str) -> dict[str, str]:
    """Parse a query string or full URL into API filter params.

    Repeated keys (skills=go&skills=rust) are merged comma-joined — same OR
    semantics the API accepts for a single comma-separated value.
    """
    raw = raw.strip()
    if not raw:
        return {}
    if "://" in raw or raw.startswith("/"):
        parts = urllib.parse.urlsplit(raw)
        query = parts.query
        # SPA sometimes puts filters only in the fragment (unusual); prefer query.
        if not query and parts.fragment and "=" in parts.fragment:
            query = parts.fragment.lstrip("?")
    else:
        query = raw.lstrip("?")

    multi: dict[str, list[str]] = {}
    for key, value in urllib.parse.parse_qsl(query, keep_blank_values=False):
        if key in PAGINATION_KEYS:
            continue
        multi.setdefault(key, []).append(value)

    out: dict[str, str] = {}
    for key, values in multi.items():
        # Dedup while preserving order; join for OR-within-facet.
        seen: set[str] = set()
        merged: list[str] = []
        for v in values:
            for part in v.split(","):
                part = part.strip()
                if part and part not in seen:
                    seen.add(part)
                    merged.append(part)
        if merged:
            out[key] = ",".join(merged)
    return out


def filters_from_args(args: argparse.Namespace) -> dict[str, str]:
    """Build API query params from --from-url / --query / facet flags."""
    filters: dict[str, str] = {}

    if args.from_url:
        filters.update(parse_query_string(args.from_url))
    if args.query:
        filters.update(parse_query_string(args.query))

    for name in STRING_FACETS:
        val = getattr(args, name, None)
        if val:
            filters[name] = val
        mode = getattr(args, f"{name}_mode", None)
        if mode:
            filters[f"{name}_mode"] = mode
        exclude = getattr(args, f"{name}_exclude", None)
        if exclude:
            filters[f"{name}_exclude"] = exclude

    for name in SEARCH_EXTRAS:
        val = getattr(args, name, None)
        if val is not None and val != "":
            filters[name] = str(val)

    if not filters:
        return dict(DEFAULT_FILTERS)
    return filters


def add_filter_arguments(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group(
        "job filters",
        "Same param names as GET /api/v1/jobs/search and the freehire SPA. "
        "Comma-separate values to OR (skills=go,rust). "
        "Use --<facet>-mode and / --<facet>-exclude for AND / exclude.",
    )
    g.add_argument(
        "--from-url",
        metavar="URL",
        help="SPA or API URL; its query string becomes the filter set",
    )
    g.add_argument(
        "--query",
        "-Q",
        metavar="QS",
        help='raw query string, e.g. "work_mode=remote&countries=us&skills=go"',
    )

    for name in STRING_FACETS:
        flag = f"--{name.replace('_', '-')}"
        g.add_argument(flag, dest=name, default=None, metavar="VAL", help=f"facet `{name}`")
        g.add_argument(
            f"--{name.replace('_', '-')}-mode",
            dest=f"{name}_mode",
            choices=("and", "or"),
            default=None,
            help=f"`{name}_mode` (and = require all values)",
        )
        g.add_argument(
            f"--{name.replace('_', '-')}-exclude",
            dest=f"{name}_exclude",
            default=None,
            metavar="VAL",
            help=f"`{name}_exclude`",
        )

    g.add_argument("--q", dest="q", default=None, help="full-text query")
    g.add_argument("--sort", choices=("created_at", "posted_at", "salary_min", "salary_max"), default=None)
    g.add_argument("--order", choices=("asc", "desc"), default=None)
    g.add_argument("--semantic-ratio", dest="semantic_ratio", default=None, metavar="N")
    g.add_argument("--visa-sponsorship", dest="visa_sponsorship", choices=("true", "false"), default=None)
    g.add_argument("--salary-min", dest="salary_min", default=None, metavar="N")
    g.add_argument("--salary-max", dest="salary_max", default=None, metavar="N")
    g.add_argument("--experience-years-min", dest="experience_years_min", default=None, metavar="N")
    g.add_argument("--posted-within-days", dest="posted_within_days", default=None, metavar="N")


def fetch_page(base: str, filters: dict[str, str], limit: int, offset: int) -> dict[str, Any]:
    params = {**filters, "limit": str(limit), "offset": str(offset)}
    url = f"{base.rstrip('/')}/api/v1/jobs/search?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "freehire-export-job-urls/1.0"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def strip_freehire_utm(raw: str) -> str:
    parts = urllib.parse.urlsplit(raw)
    q = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    q = [(k, v) for k, v in q if not (k == "utm_source" and v == "freehire.me")]
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(q), parts.fragment)
    )


def collect_urls(
    base: str,
    filters: dict[str, str],
    page_size: int = PAGE_SIZE,
    *,
    keep_utm: bool = False,
) -> tuple[list[str], int]:
    urls: list[str] = []
    offset = 0
    total = None

    print(
        "filters: " + urllib.parse.urlencode(filters).replace("%2C", ","),
        file=sys.stderr,
    )

    while offset < MAX_WINDOW:
        limit = min(page_size, MAX_WINDOW - offset)
        if limit <= 0:
            break
        try:
            body = fetch_page(base, filters, limit, offset)
        except urllib.error.HTTPError as e:
            raise SystemExit(
                f"HTTP {e.code} at offset={offset}: {e.read().decode('utf-8', errors='replace')}"
            ) from e
        except urllib.error.URLError as e:
            raise SystemExit(f"request failed: {e}") from e

        meta = body.get("meta") or {}
        if total is None:
            total = int(meta.get("total") or 0)
            print(f"total matches: {total}", file=sys.stderr)

        data = body.get("data") or []
        if not data:
            break

        for job in data:
            u = job.get("url")
            if u:
                urls.append(u if keep_utm else strip_freehire_utm(u))

        offset += len(data)
        print(f"fetched {offset}/{total} rows (urls so far={len(urls)})", file=sys.stderr)

        if offset >= total:
            break
        time.sleep(0.2)

    seen: set[str] = set()
    unique: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)
    if len(unique) != len(urls):
        print(
            f"deduped apply urls: {len(urls)} → {len(unique)}",
            file=sys.stderr,
        )
    return unique, total or 0


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "-o",
        "--output",
        default=None,
        help="output text file (default: urls/job_urls_YYYYMMDD_HHMMSS.txt)",
    )
    p.add_argument("--base-url", default=DEFAULT_BASE, help=f"API origin (default: {DEFAULT_BASE})")
    p.add_argument("--page-size", type=int, default=PAGE_SIZE, help=f"page size 1–100 (default: {PAGE_SIZE})")
    p.add_argument(
        "--keep-utm",
        action="store_true",
        help="keep ?utm_source=freehire.me (stripped by default)",
    )
    p.add_argument(
        "--print-filters",
        action="store_true",
        help="print resolved filters and exit (no request)",
    )
    add_filter_arguments(p)
    args = p.parse_args()

    if not 1 <= args.page_size <= 100:
        raise SystemExit("--page-size must be between 1 and 100")

    filters = filters_from_args(args)
    if args.print_filters:
        for k, v in sorted(filters.items()):
            print(f"{k}={v}")
        return

    out = Path(args.output) if args.output else default_output_path()
    if not out.is_absolute():
        # Relative -o paths land in urls/, not the caller's cwd.
        out = OUTPUT_DIR / out

    urls, total = collect_urls(
        args.base_url, filters, args.page_size, keep_utm=args.keep_utm
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(urls))
        if urls:
            f.write("\n")

    print(f"wrote {len(urls)} urls (api total={total}) → {out}", file=sys.stderr)
    if total > MAX_WINDOW:
        print(
            f"warning: api window caps at {MAX_WINDOW}; {total - MAX_WINDOW} matches were not fetched",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
