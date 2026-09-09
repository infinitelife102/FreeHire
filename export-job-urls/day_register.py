#!/usr/bin/env python3
"""Build a per-date job register from yesterday's postings.

Pulls the previous calendar day's jobs (same search filters as export_job_urls.py),
writes them in paragraphs of 10, and records the count at the end. Truly new
postings (the `--new-only` set) come first; posted_at bumps follow. `--new-only`
still drops those bumps entirely.

A later run for the same date keeps one file, YYYY-MM-DD.txt. It diffs
against URLs already in that day's register and appends only the new
ones. URLs already recorded stay even if a later fetch drops them. Slack
gets those new URLs, not the whole day again. Each Slack block is written
after it succeeds, so a failed block can be retried without re-posting
the ones that already landed.

Usage:
  python day_register.py
  python day_register.py --date 2026-08-15
  python day_register.py --from exports/job_urls_2026-08-17_000819.txt
  python day_register.py --from exports/job_urls_20260818_000819.txt --date 2026-08-15

Slack (#plan_list or whatever channel the webhook is bound to):
  put DAY_REGISTER_SLACK_WEBHOOK in .env (see README), or pass --slack-webhook
  python day_register.py --slack

`--with-jd` also writes `[{url, jd}, …]` to day-register/jd/YYYY-MM-DD.json
via GET /api/v1/agent/jobs/search. That file follows the register: a URL
already recorded keeps its body even when a later fetch drops it; only
gaps are filled. Slack still gets URLs only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import date, tzinfo
from pathlib import Path

from export_job_urls import (
    DEFAULT_BASE,
    DEFAULT_TZ_NAME,
    PAGE_SIZE,
    JobRow,
    add_filter_arguments,
    add_new_only_argument,
    collect_jobs,
    effective_tz,
    ensure_posted_window,
    filters_from_args,
    parse_export_header,
    parse_job_line,
    resolve_target_date,
    rows_for_calendar_day,
    warn_if_window_truncated,
)

SCRIPT_DIR = Path(__file__).resolve().parent
# This script's own tree — not exports/ (that folder belongs to export_job_urls.py).
DAY_DIR = SCRIPT_DIR / "day-register"
REGISTER_DIR = DAY_DIR / "register"
JD_DIR = DAY_DIR / "jd"

BLOCK_SIZE = 10
SLACK_WEBHOOK_ENV = "DAY_REGISTER_SLACK_WEBHOOK"
ENV_FILE = SCRIPT_DIR / ".env"
# Incoming webhooks allow one post per second; wait at least that between blocks.
SLACK_POST_GAP_SECONDS = 1.0


def load_dotenv(path: Path) -> dict[str, str]:
    """KEY=VALUE lines from a local .env. No interpolation; optional quotes."""
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def load_export_file(path: Path) -> tuple[list[JobRow], date | None, str | None]:
    if not path.is_file():
        raise SystemExit(f"export file not found: {path}")
    text = path.read_text(encoding="utf-8")
    header_day, header_tz = parse_export_header(text)
    rows: list[JobRow] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        row = parse_job_line(raw)
        if row is None or row.url in seen:
            continue
        seen.add(row.url)
        rows.append(row)
    return rows, header_day, header_tz


def url_blocks(rows: list[JobRow], block_size: int) -> list[str]:
    """Split rows into paragraphs of block_size URLs.

    The register file chunks the full day. Slack chunks only the rows being
    posted — the whole day on the first run, new URLs after that.
    """
    chunks: list[str] = []
    for i in range(0, len(rows), block_size):
        block = rows[i : i + block_size]
        chunks.append("\n".join(row.url for row in block))
    return chunks


def register_filename(day: date) -> str:
    return f"{day.isoformat()}.txt"


def jd_filename(day: date) -> str:
    return f"{day.isoformat()}.json"


def load_day_rows(directory: Path, day: date) -> list[JobRow]:
    """URLs already recorded in this day's YYYY-MM-DD.txt, if it exists."""
    path = directory / register_filename(day)
    if not path.is_file():
        return []
    rows, _, _ = load_export_file(path)
    return rows


def load_jd_file(path: Path) -> list[JobRow]:
    """`[{url, jd}, …]` already recorded for this day, if the file exists."""
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    rows: list[JobRow] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url.strip() or url in seen:
            continue
        jd = item.get("jd")
        if not isinstance(jd, str):
            jd = ""
        seen.add(url)
        rows.append(JobRow(url=url.strip(), posted_at="", description=jd))
    return rows


def new_rows(rows: list[JobRow], seen: set[str]) -> list[JobRow]:
    return [row for row in rows if row.url not in seen]


def plan_register(
    kept: list[JobRow],
    directory: Path,
    day: date,
) -> tuple[list[JobRow] | None, list[JobRow] | None, str]:
    """Decide what to write and what Slack should post. Does not write.

    Returns (rows to write or None to leave the file, rows Slack should post
    or None if nothing new, status line). An empty first write still creates
    the file, but does not post. The file is append-only: a URL already
    recorded is kept even when it is missing from kept.
    """
    register_path = directory / register_filename(day)
    prior = load_day_rows(directory, day)
    seen = {row.url for row in prior}
    fresh = new_rows(kept, seen)

    if not register_path.is_file():
        status = f"register {day.isoformat()}: {len(kept)} jobs → {register_path}"
        return kept, (kept or None), status

    if not fresh:
        return None, None, f"register {day.isoformat()}: 0 new (already had {len(seen)})"

    combined = prior + fresh
    status = (
        f"register {day.isoformat()}: {len(fresh)} new "
        f"(now {len(combined)}) → {register_path}"
    )
    return combined, fresh, status


def merge_descriptions(rows: list[JobRow], details: list[JobRow]) -> list[JobRow]:
    """Copy a missing description onto rows matched by apply URL.

    A row that already has a body keeps it. A URL the details miss stays
    as-is. Title/company/slug are left alone — format_jd only writes url+jd.
    """
    by_url = {row.url: row.description for row in details if row.description.strip()}
    merged: list[JobRow] = []
    for row in rows:
        desc = by_url.get(row.url)
        if desc is None or row.description.strip():
            merged.append(row)
            continue
        merged.append(row._replace(description=desc))
    return merged


def plan_jd(
    register_rows: list[JobRow],
    existing: list[JobRow],
    fetched: list[JobRow],
) -> list[JobRow]:
    """Same URLs as the register, in that order.

    Bodies already in the JD file stay. This run's fetch fills gaps only.
    A URL that this fetch dropped keeps its stored body.
    """
    return merge_descriptions(merge_descriptions(register_rows, existing), fetched)


def needs_description_fetch(rows: list[JobRow]) -> bool:
    """True when some row still lacks a body. An empty list does not fetch."""
    return bool(rows) and any(not row.description.strip() for row in rows)


def use_cached_descriptions(prior: list[JobRow], existing_jd: list[JobRow]) -> bool:
    """True when this day's register already has a JD body for every URL."""
    return bool(prior) and not needs_description_fetch(plan_jd(prior, existing_jd, []))


def search_filters(args: argparse.Namespace, target: date, tz: tzinfo) -> dict[str, str]:
    filters = ensure_posted_window(filters_from_args(args), target, tz)
    if "sort" not in filters:
        filters["sort"] = "posted_at"
    return filters


def format_jd(rows: list[JobRow]) -> str:
    """JSON array of `{url, jd}` for the day's register."""
    payload = [{"url": row.url, "jd": row.description} for row in rows]
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def format_register(
    rows: list[JobRow], block_size: int, day: date, tz_label: str
) -> str:
    chunks = url_blocks(rows, block_size)
    body = "\n\n".join(chunks)
    header = f"# date: {day.isoformat()}\n# timezone: {tz_label}\n\n"
    if body:
        body += "\n\n"
    return header + body + f"# count: {len(rows)}\n"


def post_slack(webhook: str, text: str) -> None:
    if not webhook.startswith(("https://", "http://")):
        raise SystemExit("Slack webhook must be an http(s) URL")
    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        webhook,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "freehire-day-register/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        raise SystemExit(
            f"Slack HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}"
        ) from e
    except urllib.error.URLError as e:
        raise SystemExit(f"Slack request failed: {e}") from e


def resolve_slack_webhook(
    explicit: str | None,
    *,
    required: bool,
    disabled: bool,
    env_file: Path | None = None,
) -> str | None:
    """CLI flag, then the process env, then export-job-urls/.env."""
    if disabled:
        return None
    webhook = (explicit or os.environ.get(SLACK_WEBHOOK_ENV) or "").strip()
    if not webhook:
        webhook = load_dotenv(env_file if env_file is not None else ENV_FILE).get(
            SLACK_WEBHOOK_ENV, ""
        ).strip()
    if webhook:
        return webhook
    if required:
        raise SystemExit(
            "--slack needs a webhook: put "
            f"{SLACK_WEBHOOK_ENV} in .env, set the env var, or pass --slack-webhook"
        )
    return None


def write_text(path: Path, text: str) -> None:
    """Write text via a same-directory temp file, then replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def post_and_record(
    prior: list[JobRow],
    fresh: list[JobRow],
    directory: Path,
    day: date,
    tz_label: str,
    block_size: int,
    webhook: str,
    *,
    post: Callable[[str, str], None] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> int:
    """Post fresh in blocks and persist after each success.

    Returns the number of Slack messages sent. A failed post leaves the
    register at the last successful block so a retry sends only the rest.
    """
    post_fn = post or post_slack
    sleep_fn = sleep or time.sleep
    acc = list(prior)
    path = directory / register_filename(day)
    chunks = [fresh[i : i + block_size] for i in range(0, len(fresh), block_size)]
    for i, block in enumerate(chunks):
        post_fn(webhook, "\n".join(row.url for row in block))
        acc.extend(block)
        write_text(path, format_register(acc, block_size, day, tz_label))
        if i + 1 < len(chunks):
            sleep_fn(SLACK_POST_GAP_SECONDS)
    return len(chunks)


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--date",
        default=None,
        metavar="YYYY-MM-DD",
        help="register this calendar day (default: # date: from --from, else yesterday in America/New_York)",
    )
    add_new_only_argument(p)
    p.add_argument(
        "--tz",
        default=None,
        metavar="NAME",
        help=(
            "IANA timezone for the calendar day "
            f"(default: # timezone: from --from, else {DEFAULT_TZ_NAME})"
        ),
    )
    p.add_argument(
        "--tz-offset",
        type=int,
        default=None,
        metavar="H",
        help="hours east of UTC; overrides --tz when set",
    )
    p.add_argument(
        "--from",
        dest="from_file",
        default=None,
        metavar="PATH",
        help="read an export file instead of calling the API (bare URLs count as this day's list)",
    )
    p.add_argument(
        "--block-size",
        type=int,
        default=BLOCK_SIZE,
        help=f"urls per paragraph (default: {BLOCK_SIZE})",
    )
    p.add_argument("--base-url", default=DEFAULT_BASE, help=f"API origin (default: {DEFAULT_BASE})")
    p.add_argument("--page-size", type=int, default=PAGE_SIZE, help=f"page size 1–100 (default: {PAGE_SIZE})")
    p.add_argument(
        "--slack",
        action="store_true",
        help="post new URLs to Slack (requires webhook; fails if missing)",
    )
    p.add_argument(
        "--no-slack",
        action="store_true",
        help=f"do not post even if {SLACK_WEBHOOK_ENV} is set",
    )
    p.add_argument(
        "--slack-webhook",
        default=None,
        metavar="URL",
        help=f"Incoming Webhook URL (default: ${SLACK_WEBHOOK_ENV})",
    )
    p.add_argument(
        "--with-jd",
        action="store_true",
        help=(
            "also write [{url, jd}, …] to day-register/jd/YYYY-MM-DD.json "
            "(append-only like the register; GET /api/v1/agent/jobs/search; "
            "Slack stays URLs only). With --from, still calls the agent search "
            "for URLs that have no stored body."
        ),
    )
    add_filter_arguments(p)
    args = p.parse_args()

    if not 1 <= args.block_size <= 100:
        raise SystemExit("--block-size must be between 1 and 100")
    if not 1 <= args.page_size <= 100:
        raise SystemExit("--page-size must be between 1 and 100")

    rows: list[JobRow] | None = None
    header_day: date | None = None
    header_tz: str | None = None
    if args.from_file:
        src = Path(args.from_file)
        if not src.is_absolute():
            src = (SCRIPT_DIR / src).resolve()
        rows, header_day, header_tz = load_export_file(src)
        print(f"loaded {len(rows)} rows from {src}", file=sys.stderr)

    tz, tz_label = effective_tz(args.tz, args.tz_offset, header_tz)
    if args.tz is None and args.tz_offset is None and header_tz:
        print(f"using # timezone: {tz_label} from export header", file=sys.stderr)

    if args.date is None and header_day is not None:
        target = header_day
        print(f"using # date: {target.isoformat()} from export header", file=sys.stderr)
    else:
        target = resolve_target_date(args.date, tz)
    if target is None:
        raise SystemExit("day_register needs a calendar day; do not pass --date all")

    if rows is None:
        filters = search_filters(args, target, tz)
        with_description = args.with_jd
        if args.with_jd and use_cached_descriptions(
            load_day_rows(REGISTER_DIR, target),
            load_jd_file(JD_DIR / jd_filename(target)),
        ):
            with_description = False
        rows, api_total = collect_jobs(
            args.base_url,
            filters,
            args.page_size,
            with_description=with_description,
        )
        warn_if_window_truncated(api_total, rows, target, tz)

    kept, undated = rows_for_calendar_day(
        rows,
        target,
        tz,
        keep_undated=args.from_file is not None,
        drop_bumps=args.new_only,
    )
    if undated == len(rows) and rows and args.from_file:
        print(
            f"export has no posted_at; keeping all {len(kept)} urls for {target.isoformat()}",
            file=sys.stderr,
        )
    elif undated:
        print(f"skipped {undated} rows with no usable posted_at", file=sys.stderr)

    register_path = REGISTER_DIR / register_filename(target)
    to_write, posted_rows, status = plan_register(kept, REGISTER_DIR, target)

    webhook: str | None = None
    if posted_rows is None:
        if args.slack:
            print("slack: skipped (no new urls)", file=sys.stderr)
    else:
        webhook = resolve_slack_webhook(
            args.slack_webhook, required=args.slack, disabled=args.no_slack
        )

    if webhook and posted_rows:
        prior = load_day_rows(REGISTER_DIR, target)
        n = post_and_record(
            prior,
            posted_rows,
            REGISTER_DIR,
            target,
            tz_label,
            args.block_size,
            webhook,
        )
        print(f"slack: posted {n} messages", file=sys.stderr)
    elif to_write is not None:
        write_text(
            register_path,
            format_register(to_write, args.block_size, target, tz_label),
        )
    print(status, file=sys.stderr)

    if args.with_jd:
        register_rows = load_day_rows(REGISTER_DIR, target)
        jd_path = JD_DIR / jd_filename(target)
        existing_jd = load_jd_file(jd_path)
        jd_rows = plan_jd(register_rows, existing_jd, kept)
        if needs_description_fetch(jd_rows):
            details, _ = collect_jobs(
                args.base_url,
                search_filters(args, target, tz),
                args.page_size,
                with_description=True,
            )
            jd_rows = merge_descriptions(jd_rows, details)
        urls_changed = [row.url for row in jd_rows] != [row.url for row in existing_jd]
        bodies_changed = [row.description for row in jd_rows] != [
            row.description for row in existing_jd
        ]
        if not jd_path.is_file() or urls_changed or bodies_changed:
            write_text(jd_path, format_jd(jd_rows))
        have_jd = sum(1 for row in jd_rows if row.description.strip())
        print(f"jd {target.isoformat()}: {have_jd}/{len(jd_rows)} → {jd_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
