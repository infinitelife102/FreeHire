#!/usr/bin/env python3
"""Build a per-date job register from yesterday's postings.

Pulls the previous calendar day's jobs (same search filters as export_job_urls.py),
writes them in paragraphs of 10, and records the count at the end.

Also creates an empty leftover file for that date so unfinished URLs can be
moved out of the register without inventing a path later.

Usage:
  python day_register.py
  python day_register.py --date 2026-08-15
  python day_register.py --from exports/job_urls_2026-08-17_000819.txt
  python day_register.py --from exports/job_urls_20260818_000819.txt --date 2026-08-15
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from export_job_urls import (
    DEFAULT_BASE,
    DEFAULT_TZ_NAME,
    PAGE_SIZE,
    JobRow,
    add_filter_arguments,
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
LEFTOVER_DIR = DAY_DIR / "leftover"

BLOCK_SIZE = 10


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


def format_register(rows: list[JobRow], block_size: int, day: date, tz_label: str) -> str:
    chunks: list[str] = []
    for i in range(0, len(rows), block_size):
        block = rows[i : i + block_size]
        chunks.append("\n".join(row.url for row in block))
    body = "\n\n".join(chunks)
    header = f"# date: {day.isoformat()}\n# timezone: {tz_label}\n\n"
    if body:
        body += "\n\n"
    return header + body + f"# count: {len(rows)}\n"


def leftover_header(day: date, tz_label: str) -> str:
    return (
        f"# leftover {day.isoformat()} ({tz_label}) — paste URLs from the "
        "register that you did not finish\n"
    )


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def write_register(path: Path, text: str) -> None:
    write_text(path, text)


def ensure_leftover(path: Path, day: date, tz_label: str) -> str:
    """Create the leftover file if it is missing. Never overwrite content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if any(line.strip() and not line.lstrip().startswith("#") for line in existing.splitlines()):
            return "kept (already has urls)"
        return "kept (already exists)"
    write_text(path, leftover_header(day, tz_label))
    return "created"


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
        "--keep-utm",
        action="store_true",
        help="keep ?utm_source=freehire.me (stripped by default)",
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
        filters = ensure_posted_window(filters_from_args(args), target, tz)
        if "sort" not in filters:
            filters["sort"] = "posted_at"
        rows, api_total = collect_jobs(
            args.base_url, filters, args.page_size, keep_utm=args.keep_utm
        )
        warn_if_window_truncated(api_total, rows, target, tz)

    kept, undated = rows_for_calendar_day(rows, target, tz, keep_undated=args.from_file is not None)
    if undated == len(rows) and rows and args.from_file:
        print(
            f"export has no posted_at; keeping all {len(kept)} urls for {target.isoformat()}",
            file=sys.stderr,
        )
    elif undated:
        print(f"skipped {undated} rows with no usable posted_at", file=sys.stderr)

    register_path = REGISTER_DIR / f"{target.isoformat()}.txt"
    leftover_path = LEFTOVER_DIR / f"{target.isoformat()}.txt"
    write_register(register_path, format_register(kept, args.block_size, target, tz_label))
    leftover_state = ensure_leftover(leftover_path, target, tz_label)

    print(
        f"register {target.isoformat()}: {len(kept)} jobs → {register_path}",
        file=sys.stderr,
    )
    print(f"leftover: {leftover_state} → {leftover_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
