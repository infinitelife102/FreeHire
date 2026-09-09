#!/usr/bin/env python3
"""Unit tests for the date-cut helpers (stdlib only)."""

from __future__ import annotations

import argparse
import io
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from day_register import (
    SLACK_POST_GAP_SECONDS,
    SLACK_WEBHOOK_ENV,
    format_jd,
    format_register,
    load_day_rows,
    load_dotenv,
    load_export_file,
    load_jd_file,
    merge_descriptions,
    needs_description_fetch,
    new_rows,
    plan_jd,
    plan_register,
    post_and_record,
    register_filename,
    resolve_slack_webhook,
    url_blocks,
    use_cached_descriptions,
    write_text,
)
from export_job_urls import (
    DEFAULT_FILTERS,
    MAX_WINDOW,
    STRING_FACETS,
    JobRow,
    _USEastern,
    add_filter_arguments,
    add_new_only_argument,
    calendar_day,
    collect_jobs,
    countries_named_in,
    days_needed_for,
    effective_tz,
    ensure_posted_window,
    filters_from_args,
    format_job_line,
    location_names_other_country,
    order_new_first,
    parse_export_header,
    parse_job_line,
    parse_query_string,
    requested_country_codes,
    require_date_for_new_only,
    resolve_tz,
    rows_for_calendar_day,
    utc_offset_from_label,
    warn_if_window_truncated,
)


SPA_SEARCH_URL = (
    "https://freehire.me/?work_mode=remote"
    "&category=software_engineering,backend,frontend,fullstack,mobile"
    "&countries=us&requires_clearance=false"
)


class FilterVocabTests(unittest.TestCase):
    def test_defaults_match_current_spa_search(self) -> None:
        self.assertEqual(DEFAULT_FILTERS, parse_query_string(SPA_SEARCH_URL))

    def test_from_url_args_use_the_spa_defaults(self) -> None:
        p = argparse.ArgumentParser()
        add_filter_arguments(p)
        args = p.parse_args(["--from-url", SPA_SEARCH_URL])
        self.assertEqual(filters_from_args(args), DEFAULT_FILTERS)

    def test_cli_flags_for_new_params(self) -> None:
        p = argparse.ArgumentParser()
        add_filter_arguments(p)
        args = p.parse_args(
            [
                "--relocation",
                "not_supported",
                "--requires-clearance",
                "false",
                "--role-type",
                "people_manager",
                "--experience-years-max",
                "5",
                "--open-within-days",
                "7",
            ]
        )
        filters = filters_from_args(args)
        self.assertEqual(filters["relocation"], "not_supported")
        self.assertEqual(filters["requires_clearance"], "false")
        self.assertEqual(filters["role_type"], "people_manager")
        self.assertEqual(filters["experience_years_max"], "5")
        self.assertEqual(filters["open_within_days"], "7")

    def test_string_facets_cover_new_api_keys(self) -> None:
        self.assertIn("requires_clearance", STRING_FACETS)
        self.assertIn("role_type", STRING_FACETS)

    def test_imported_url_drops_retired_semantic_ratio(self) -> None:
        got = parse_query_string(SPA_SEARCH_URL + "&semantic_ratio=0&limit=20")
        self.assertNotIn("semantic_ratio", got)
        self.assertNotIn("limit", got)
        self.assertEqual(got, DEFAULT_FILTERS)

    def test_defaults_leave_the_posted_window_to_ensure_posted_window(self) -> None:
        self.assertNotIn("posted_within_days", DEFAULT_FILTERS)
        tz = timezone.utc
        target = datetime.now(tz).date() - timedelta(days=1)
        out = ensure_posted_window(dict(DEFAULT_FILTERS), target, tz)
        self.assertEqual(out["posted_within_days"], str(days_needed_for(target, tz)))


class LocationCountryFilterTests(unittest.TestCase):
    keep_us = frozenset({"us"})

    def test_drops_named_foreign_country(self) -> None:
        self.assertTrue(location_names_other_country("Spain", self.keep_us))
        self.assertTrue(location_names_other_country("Sydney, Australia", self.keep_us))
        self.assertTrue(location_names_other_country("Vietnam", self.keep_us))
        self.assertTrue(location_names_other_country("China", self.keep_us))

    def test_keeps_remote_empty_and_us_city(self) -> None:
        for loc in ("", "Remote", "New York, NY", "United States", "Remote - US"):
            self.assertFalse(
                location_names_other_country(loc, self.keep_us), loc
            )

    def test_keeps_when_us_is_also_named(self) -> None:
        self.assertFalse(
            location_names_other_country("Spain / United States", self.keep_us)
        )

    def test_keeps_ambiguous_us_place_names(self) -> None:
        self.assertFalse(location_names_other_country("Georgia", self.keep_us))
        self.assertFalse(location_names_other_country("Indiana", self.keep_us))

    def test_keeps_new_mexico_not_mexico(self) -> None:
        for loc in ("New Mexico", "Remote - New Mexico", "Albuquerque, New Mexico"):
            self.assertFalse(location_names_other_country(loc, self.keep_us), loc)
        self.assertEqual(countries_named_in("New Mexico"), {"us"})
        self.assertTrue(location_names_other_country("Mexico", self.keep_us))
        self.assertTrue(location_names_other_country("Mexico City", self.keep_us))

    def test_no_filter_without_requested_countries(self) -> None:
        self.assertFalse(location_names_other_country("Spain", frozenset()))
        self.assertEqual(requested_country_codes({}), frozenset())
        self.assertEqual(requested_country_codes({"countries": "us"}), frozenset({"us"}))

    def test_requested_country_aliases(self) -> None:
        self.assertEqual(requested_country_codes({"countries": "usa"}), frozenset({"us"}))
        self.assertEqual(requested_country_codes({"countries": "United States"}), frozenset({"us"}))
        self.assertEqual(requested_country_codes({"countries": "UK"}), frozenset({"gb"}))
        self.assertEqual(requested_country_codes({"countries": "de"}), frozenset({"de"}))
        self.assertEqual(requested_country_codes({"countries": "us,ca"}), frozenset({"us", "ca"}))
        self.assertFalse(
            location_names_other_country(
                "United States", requested_country_codes({"countries": "usa"})
            )
        )
        self.assertFalse(
            location_names_other_country(
                "United Kingdom", requested_country_codes({"countries": "uk"})
            )
        )

    def test_countries_named_in(self) -> None:
        self.assertEqual(countries_named_in("Sydney, Australia"), {"au"})
        self.assertEqual(countries_named_in("Remote"), set())
        self.assertEqual(countries_named_in("United States"), {"us"})

    def test_collect_jobs_drops_foreign_location(self) -> None:
        page = {
            "meta": {"total": 3},
            "data": [
                {
                    "url": "https://jobs.ashbyhq.com/bjakcareer/spain",
                    "location": "Spain",
                    "posted_at": "2026-08-27T06:09:08Z",
                },
                {
                    "url": "https://jobs.ashbyhq.com/bjakcareer/sydney",
                    "location": "Sydney, Australia",
                    "posted_at": "2026-08-27T06:13:12Z",
                },
                {
                    "url": "https://jobs.ashbyhq.com/acme/us",
                    "location": "Remote",
                    "posted_at": "2026-08-27T12:00:00Z",
                    "created_at": "2026-08-27T11:00:00Z",
                },
            ],
        }
        with patch("export_job_urls.fetch_page", return_value=page):
            with patch("sys.stderr", io.StringIO()):
                rows, total = collect_jobs(
                    "https://freehire.me", {"countries": "us"}, page_size=100
                )
        self.assertEqual(total, 3)
        self.assertEqual([r.url for r in rows], ["https://jobs.ashbyhq.com/acme/us"])
        self.assertEqual(rows[0].created_at, "2026-08-27T11:00:00Z")

    def test_collect_jobs_keeps_apply_url_as_returned(self) -> None:
        page = {
            "meta": {"total": 1},
            "data": [
                {
                    "url": "https://www.whatjobs.com/job/123?utm_source=freehire.me",
                    "location": "Remote",
                    "posted_at": "2026-08-27T12:00:00Z",
                }
            ],
        }
        with patch("export_job_urls.fetch_page", return_value=page):
            with patch("sys.stderr", io.StringIO()):
                rows, _ = collect_jobs(
                    "https://freehire.me", {"countries": "us"}, page_size=100
                )
        self.assertEqual(
            rows[0].url,
            "https://www.whatjobs.com/job/123?utm_source=freehire.me",
        )

    def test_collect_jobs_keeps_full_description_from_agent_search(self) -> None:
        page = {
            "meta": {"total": 1},
            "data": [
                {
                    "url": "https://jobs.ashbyhq.com/acme/us",
                    "location": "Remote",
                    "posted_at": "2026-08-27T12:00:00Z",
                    "created_at": "2026-08-27T11:00:00Z",
                    "title": "Backend Engineer",
                    "company": "Acme",
                    "public_slug": "backend-engineer-acme-1a2b",
                    "description": "Build APIs in Go.\n\nRequirements:\n- 5 years",
                }
            ],
        }
        with patch("export_job_urls.fetch_page", return_value=page) as fetch:
            with patch("sys.stderr", io.StringIO()):
                rows, total = collect_jobs(
                    "https://freehire.me",
                    {"countries": "us"},
                    page_size=100,
                    with_description=True,
                    description_format="text",
                )
        self.assertEqual(total, 1)
        self.assertEqual(rows[0].title, "Backend Engineer")
        self.assertEqual(rows[0].company, "Acme")
        self.assertEqual(rows[0].public_slug, "backend-engineer-acme-1a2b")
        self.assertIn("Build APIs in Go", rows[0].description)
        kwargs = fetch.call_args.kwargs
        self.assertEqual(kwargs["path"], "/api/v1/agent/jobs/search")
        self.assertEqual(kwargs["extra"], {"description_format": "text"})


class ParseExportHeaderTests(unittest.TestCase):
    def test_reads_date_and_timezone(self) -> None:
        day, tz_label = parse_export_header(
            "# date: 2026-08-17\n# timezone: America/New_York\n\n2026-08-17T12:00:00Z\thttps://x.test/a\n"
        )
        self.assertEqual(day, date(2026, 8, 17))
        self.assertEqual(tz_label, "America/New_York")

    def test_stops_at_first_data_line(self) -> None:
        day, _ = parse_export_header("https://x.test/a\n# date: 2026-08-17\n")
        self.assertIsNone(day)

    def test_rejects_bad_date(self) -> None:
        with self.assertRaises(SystemExit):
            parse_export_header("# date: 17-08-2026\n")


class RowsForCalendarDayTests(unittest.TestCase):
    tz = timezone.utc

    def test_keeps_matching_day(self) -> None:
        rows = [
            JobRow("https://x.test/a", "2026-08-17T12:00:00Z"),
            JobRow("https://x.test/b", "2026-08-16T12:00:00Z"),
        ]
        kept, undated = rows_for_calendar_day(rows, date(2026, 8, 17), self.tz)
        self.assertEqual([r.url for r in kept], ["https://x.test/a"])
        self.assertEqual(undated, 0)

    def test_drops_undated_unless_legacy(self) -> None:
        rows = [JobRow("https://x.test/a", ""), JobRow("https://x.test/b", "")]
        kept, undated = rows_for_calendar_day(rows, date(2026, 8, 15), self.tz)
        self.assertEqual(kept, [])
        self.assertEqual(undated, 2)

        kept, undated = rows_for_calendar_day(
            rows, date(2026, 8, 15), self.tz, keep_undated=True
        )
        self.assertEqual([r.url for r in kept], ["https://x.test/a", "https://x.test/b"])
        self.assertEqual(undated, 2)

    def test_keeps_posted_at_bump_by_default(self) -> None:
        rows = [
            JobRow(
                "https://job-boards.greenhouse.io/specterops/jobs/8621096002",
                "2026-08-31T14:08:48Z",
                "2026-07-06T16:49:29Z",
            ),
            JobRow(
                "https://x.test/new",
                "2026-08-31T12:00:00Z",
                "2026-08-31T11:00:00Z",
            ),
        ]
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            kept, undated = rows_for_calendar_day(rows, date(2026, 8, 31), self.tz)
        self.assertEqual(
            [r.url for r in kept],
            [
                "https://x.test/new",
                "https://job-boards.greenhouse.io/specterops/jobs/8621096002",
            ],
        )
        self.assertEqual(undated, 0)
        self.assertEqual(buf.getvalue(), "")

    def test_lists_new_postings_before_bumps(self) -> None:
        # Bump has an earlier posted_at; without new-first it would sort first.
        rows = [
            JobRow(
                "https://x.test/bump",
                "2026-08-31T08:00:00Z",
                "2026-07-06T16:49:29Z",
            ),
            JobRow(
                "https://x.test/new-late",
                "2026-08-31T20:00:00Z",
                "2026-08-31T19:00:00Z",
            ),
            JobRow(
                "https://x.test/new-early",
                "2026-08-31T10:00:00Z",
                "2026-08-31T09:00:00Z",
            ),
            JobRow(
                "https://x.test/bump-late",
                "2026-08-31T22:00:00Z",
                "2026-06-01T00:00:00Z",
            ),
        ]
        kept, undated = rows_for_calendar_day(rows, date(2026, 8, 31), self.tz)
        self.assertEqual(
            [r.url for r in kept],
            [
                "https://x.test/new-early",
                "https://x.test/new-late",
                "https://x.test/bump",
                "https://x.test/bump-late",
            ],
        )
        self.assertEqual(undated, 0)
        ordered = order_new_first(list(reversed(rows)), date(2026, 8, 31), self.tz)
        self.assertEqual([r.url for r in ordered], [r.url for r in kept])

    def test_drops_posted_at_bump_when_new_only(self) -> None:
        rows = [
            JobRow(
                "https://job-boards.greenhouse.io/specterops/jobs/8621096002",
                "2026-08-31T14:08:48Z",
                "2026-07-06T16:49:29Z",
            ),
            JobRow(
                "https://x.test/new",
                "2026-08-31T12:00:00Z",
                "2026-08-31T11:00:00Z",
            ),
        ]
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            kept, undated = rows_for_calendar_day(
                rows, date(2026, 8, 31), self.tz, drop_bumps=True
            )
        self.assertEqual([r.url for r in kept], ["https://x.test/new"])
        self.assertEqual(undated, 0)
        self.assertIn("dropped 1 jobs whose created_at is before 2026-08-31", buf.getvalue())

    def test_keeps_when_created_at_is_after_target(self) -> None:
        # posted evening, first ingest after midnight — not a Greenhouse bump.
        rows = [
            JobRow(
                "https://x.test/late",
                "2026-08-31T03:30:00Z",
                "2026-09-01T05:00:00Z",
            )
        ]
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            kept, undated = rows_for_calendar_day(
                rows, date(2026, 8, 31), self.tz, drop_bumps=True
            )
        self.assertEqual([r.url for r in kept], ["https://x.test/late"])
        self.assertEqual(undated, 0)
        self.assertEqual(buf.getvalue(), "")

    def test_keeps_legacy_row_without_created_at(self) -> None:
        rows = [JobRow("https://x.test/a", "2026-08-17T12:00:00Z")]
        kept, undated = rows_for_calendar_day(rows, date(2026, 8, 17), self.tz)
        self.assertEqual([r.url for r in kept], ["https://x.test/a"])
        self.assertEqual(undated, 0)

    def test_parse_job_line_bare_url(self) -> None:
        row = parse_job_line("https://x.test/a")
        self.assertEqual(row, JobRow(url="https://x.test/a", posted_at=""))

    def test_parse_and_format_three_column_line(self) -> None:
        raw = "2026-08-31T14:08:48Z\t2026-07-06T16:49:29Z\thttps://x.test/a"
        row = parse_job_line(raw)
        self.assertEqual(
            row,
            JobRow(
                url="https://x.test/a",
                posted_at="2026-08-31T14:08:48Z",
                created_at="2026-07-06T16:49:29Z",
            ),
        )
        self.assertEqual(format_job_line(row), raw)

    def test_parse_job_line_two_column_legacy(self) -> None:
        row = parse_job_line("2026-08-31T14:08:48Z\thttps://x.test/a")
        self.assertEqual(
            row, JobRow(url="https://x.test/a", posted_at="2026-08-31T14:08:48Z")
        )

    def test_parse_job_line_empty_created_at_is_not_a_bump(self) -> None:
        raw = "2026-08-31T12:00:00Z\t\thttps://x.test/a"
        row = parse_job_line(raw)
        self.assertEqual(
            row,
            JobRow(url="https://x.test/a", posted_at="2026-08-31T12:00:00Z", created_at=""),
        )
        kept, undated = rows_for_calendar_day(
            [row], date(2026, 8, 31), self.tz, drop_bumps=True
        )
        self.assertEqual([r.url for r in kept], ["https://x.test/a"])
        self.assertEqual(undated, 0)
        self.assertEqual(format_job_line(row), raw)

    def test_new_only_flag_defaults_off(self) -> None:
        p = argparse.ArgumentParser()
        add_new_only_argument(p)
        self.assertFalse(p.parse_args([]).new_only)
        self.assertTrue(p.parse_args(["--new-only"]).new_only)

    def test_new_only_refuses_date_all(self) -> None:
        require_date_for_new_only(False, None)
        require_date_for_new_only(True, date(2026, 8, 31))
        with self.assertRaises(SystemExit) as ctx:
            require_date_for_new_only(True, None)
        self.assertIn("--date all", str(ctx.exception))


class WarnIfWindowTruncatedTests(unittest.TestCase):
    def test_silent_when_under_cap(self) -> None:
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            warn_if_window_truncated(10, [], date(2026, 8, 15), timezone.utc)
        self.assertEqual(buf.getvalue(), "")

    def test_names_missed_day_when_oldest_fetched_is_newer(self) -> None:
        rows = [JobRow("https://x.test/a", "2026-08-18T12:00:00Z")]
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            warn_if_window_truncated(MAX_WINDOW + 1, rows, date(2026, 8, 15), timezone.utc)
        text = buf.getvalue()
        self.assertIn("api window caps", text)
        self.assertIn("2026-08-15 was not fetched", text)


class USEasternTests(unittest.TestCase):
    tz = _USEastern()

    def test_fall_back_morning_stays_on_november_first(self) -> None:
        # 04:30 UTC is 00:30 EDT — still 1 November, not 23:30 the night before.
        self.assertEqual(calendar_day("2026-11-01T04:30:00Z", self.tz), date(2026, 11, 1))
        local = datetime(2026, 11, 1, 4, 30, tzinfo=timezone.utc).astimezone(self.tz)
        self.assertEqual(local.hour, 0)
        self.assertEqual(local.minute, 30)

    def test_fall_back_after_2am_is_standard_time(self) -> None:
        # 06:30 UTC is 01:30 EST (after the 2am EDT → 1am EST fallback).
        local = datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc).astimezone(self.tz)
        self.assertEqual(local.date(), date(2026, 11, 1))
        self.assertEqual(local.hour, 1)
        self.assertEqual(local.tzname(), "EST")

    def test_spring_forward_before_2am_is_standard_time(self) -> None:
        # 06:30 UTC is 01:30 EST (2am EST does not exist; clocks jump to 3am EDT).
        local = datetime(2026, 3, 8, 6, 30, tzinfo=timezone.utc).astimezone(self.tz)
        self.assertEqual(local.date(), date(2026, 3, 8))
        self.assertEqual(local.hour, 1)
        self.assertEqual(local.tzname(), "EST")

    def test_spring_forward_after_jump_is_daylight_time(self) -> None:
        local = datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc).astimezone(self.tz)
        self.assertEqual(local.hour, 3)
        self.assertEqual(local.tzname(), "EDT")


class EffectiveTzTests(unittest.TestCase):
    def test_cli_offset_wins_over_header(self) -> None:
        tz, label = effective_tz(None, 9, "America/New_York")
        self.assertEqual(label, "UTC+9")
        self.assertEqual(tz.utcoffset(None), timedelta(hours=9))

    def test_cli_name_wins_over_header(self) -> None:
        _, label = effective_tz("UTC+0", None, "America/New_York")
        self.assertEqual(label, "UTC+0")

    def test_header_used_when_cli_omitted(self) -> None:
        _, label = effective_tz(None, None, "UTC-4")
        self.assertEqual(label, "UTC-4")

    def test_default_when_nothing_set(self) -> None:
        _, label = effective_tz(None, None, None)
        self.assertEqual(label, "America/New_York")

    def test_utc_offset_label(self) -> None:
        self.assertEqual(utc_offset_from_label("UTC+9"), 9)
        self.assertEqual(utc_offset_from_label("UTC-5"), -5)
        self.assertEqual(utc_offset_from_label("UTC"), 0)
        self.assertIsNone(utc_offset_from_label("America/New_York"))
        tz, label = resolve_tz("UTC+9", None)
        self.assertEqual(label, "UTC+9")
        self.assertEqual(tz.utcoffset(None), timedelta(hours=9))

    def test_header_utc_keeps_row_that_new_york_would_drop(self) -> None:
        rows = [JobRow("https://x.test/a", "2026-08-17T00:30:00Z")]
        tz, _ = effective_tz(None, None, "UTC")
        kept, _ = rows_for_calendar_day(rows, date(2026, 8, 17), tz)
        self.assertEqual([r.url for r in kept], ["https://x.test/a"])
        ny, _ = effective_tz(None, None, None)
        kept_ny, _ = rows_for_calendar_day(rows, date(2026, 8, 17), ny)
        self.assertEqual(kept_ny, [])


class LoadExportFileTests(unittest.TestCase):
    def test_returns_header_timezone(self) -> None:
        text = (
            "# date: 2026-08-17\n"
            "# timezone: UTC\n"
            "\n"
            "2026-08-17T00:30:00Z\thttps://x.test/a\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "export.txt"
            path.write_text(text, encoding="utf-8")
            rows, day, tz_label = load_export_file(path)
        self.assertEqual([r.url for r in rows], ["https://x.test/a"])
        self.assertEqual(day, date(2026, 8, 17))
        self.assertEqual(tz_label, "UTC")


class SlackRegisterTests(unittest.TestCase):
    def test_url_blocks_match_file_paragraphs(self) -> None:
        rows = [JobRow(f"https://x.test/{i}", "") for i in range(12)]
        blocks = url_blocks(rows, 10)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0].count("\n"), 9)
        self.assertEqual(blocks[1], "https://x.test/10\nhttps://x.test/11")

    def test_slack_posts_url_blocks_only(self) -> None:
        rows = [JobRow("https://x.test/a", ""), JobRow("https://x.test/b", "")]
        self.assertEqual(url_blocks(rows, 10), ["https://x.test/a\nhttps://x.test/b"])
        self.assertEqual(url_blocks([], 10), [])

    def test_resolve_slack_webhook_env_and_flags(self) -> None:
        with patch.dict("os.environ", {SLACK_WEBHOOK_ENV: "https://hooks.example/x"}, clear=False):
            self.assertEqual(
                resolve_slack_webhook(None, required=False, disabled=False),
                "https://hooks.example/x",
            )
            self.assertIsNone(resolve_slack_webhook(None, required=False, disabled=True))
        self.assertEqual(
            resolve_slack_webhook("https://hooks.example/cli", required=True, disabled=False),
            "https://hooks.example/cli",
        )
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop(SLACK_WEBHOOK_ENV, None)
            with self.assertRaises(SystemExit):
                resolve_slack_webhook(None, required=True, disabled=False, env_file=Path("/no/.env"))

    def test_dotenv_file_supplies_webhook(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                f'# comment\n{SLACK_WEBHOOK_ENV}="https://hooks.example/file"\n',
                encoding="utf-8",
            )
            self.assertEqual(
                load_dotenv(path)[SLACK_WEBHOOK_ENV],
                "https://hooks.example/file",
            )
            with patch.dict("os.environ", {}, clear=False):
                os.environ.pop(SLACK_WEBHOOK_ENV, None)
                self.assertEqual(
                    resolve_slack_webhook(None, required=True, disabled=False, env_file=path),
                    "https://hooks.example/file",
                )


class RegisterFileTests(unittest.TestCase):
    def test_register_filename_is_one_file_per_day(self) -> None:
        self.assertEqual(register_filename(date(2026, 9, 1)), "2026-09-01.txt")

    def test_load_day_rows_reads_only_the_day_file(self) -> None:
        day = date(2026, 9, 1)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "2026-09-01.txt").write_text(
                "# date: 2026-09-01\nhttps://x.test/a\n", encoding="utf-8"
            )
            (directory / "2026-09-01(1).txt").write_text(
                "https://x.test/b\n", encoding="utf-8"
            )
            (directory / "2026-08-31.txt").write_text(
                "https://x.test/other\n", encoding="utf-8"
            )
            self.assertEqual(
                [row.url for row in load_day_rows(directory, day)],
                ["https://x.test/a"],
            )
            self.assertEqual(load_day_rows(directory, date(2026, 9, 2)), [])

    def test_new_rows_keep_order_and_drop_seen(self) -> None:
        rows = [
            JobRow("https://x.test/a", ""),
            JobRow("https://x.test/c", ""),
            JobRow("https://x.test/b", ""),
        ]
        fresh = new_rows(rows, {"https://x.test/a", "https://x.test/b"})
        self.assertEqual([r.url for r in fresh], ["https://x.test/c"])

    def test_format_register_header_blocks_and_count(self) -> None:
        text = format_register(
            [JobRow("https://x.test/c", "")],
            10,
            date(2026, 9, 1),
            "America/New_York",
        )
        self.assertEqual(
            text,
            "# date: 2026-09-01\n# timezone: America/New_York\n\n"
            "https://x.test/c\n\n# count: 1\n",
        )

    def test_format_jd_is_url_and_jd_json(self) -> None:
        text = format_jd(
            [
                JobRow(
                    url="https://x.test/a",
                    posted_at="2026-09-01T12:00:00Z",
                    title="Backend Engineer",
                    company="Acme",
                    description="Build APIs in Go.",
                )
            ]
        )
        self.assertEqual(
            json.loads(text),
            [{"url": "https://x.test/a", "jd": "Build APIs in Go."}],
        )

    def test_merge_descriptions_matches_by_url(self) -> None:
        rows = [JobRow("https://x.test/a", "2026-09-01T12:00:00Z")]
        details = [
            JobRow(
                url="https://x.test/a",
                posted_at="2026-09-01T12:00:00Z",
                title="Backend Engineer",
                company="Acme",
                description="Build APIs.",
            )
        ]
        merged = merge_descriptions(rows, details)
        self.assertEqual(merged[0].description, "Build APIs.")
        self.assertEqual(merged[0].title, "")

    def test_merge_descriptions_keeps_row_when_url_missing(self) -> None:
        rows = [JobRow("https://x.test/a", "2026-09-01T12:00:00Z", description="keep")]
        merged = merge_descriptions(rows, [JobRow("https://x.test/b", "", description="other")])
        self.assertEqual(merged, rows)

    def test_merge_descriptions_does_not_overwrite_stored_body(self) -> None:
        rows = [JobRow("https://x.test/a", "", description="stored")]
        details = [JobRow("https://x.test/a", "", description="fresh")]
        merged = merge_descriptions(rows, details)
        self.assertEqual(merged[0].description, "stored")

    def test_needs_description_fetch_skips_empty_list(self) -> None:
        self.assertFalse(needs_description_fetch([]))
        self.assertFalse(
            needs_description_fetch([JobRow("https://x.test/a", "", description="body")])
        )
        self.assertTrue(needs_description_fetch([JobRow("https://x.test/a", "")]))

    def test_plan_jd_keeps_stored_and_fills_gaps(self) -> None:
        register = [
            JobRow("https://x.test/a", ""),
            JobRow("https://x.test/b", ""),
            JobRow("https://x.test/c", ""),
        ]
        existing = [JobRow("https://x.test/a", "", description="old A")]
        fetched = [
            JobRow("https://x.test/b", "", description="new B"),
            JobRow("https://x.test/d", "", description="dropped"),
        ]
        planned = plan_jd(register, existing, fetched)
        self.assertEqual(
            [(row.url, row.description) for row in planned],
            [
                ("https://x.test/a", "old A"),
                ("https://x.test/b", "new B"),
                ("https://x.test/c", ""),
            ],
        )

    def test_use_cached_descriptions_requires_every_register_url(self) -> None:
        prior = [JobRow("https://x.test/a", ""), JobRow("https://x.test/b", "")]
        self.assertFalse(use_cached_descriptions([], []))
        self.assertFalse(
            use_cached_descriptions(prior, [JobRow("https://x.test/a", "", description="A")])
        )
        self.assertTrue(
            use_cached_descriptions(
                prior,
                [
                    JobRow("https://x.test/a", "", description="A"),
                    JobRow("https://x.test/b", "", description="B"),
                ],
            )
        )

    def test_load_jd_file_reads_url_and_jd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "2026-09-01.json"
            self.assertEqual(load_jd_file(path), [])
            write_text(
                path,
                json.dumps([{"url": "https://x.test/a", "jd": "Build APIs."}]) + "\n",
            )
            rows = load_jd_file(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].url, "https://x.test/a")
            self.assertEqual(rows[0].description, "Build APIs.")

    def test_write_text_replaces_and_leaves_no_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "2026-09-01.txt"
            write_text(path, "first\n")
            write_text(path, "second\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "second\n")
            leftovers = [p.name for p in Path(tmp).iterdir() if p.name != path.name]
            self.assertEqual(leftovers, [])


class PlanRegisterTests(unittest.TestCase):
    day = date(2026, 9, 1)
    tz = "America/New_York"

    def _rows(self, *urls: str) -> list[JobRow]:
        return [JobRow(url, "") for url in urls]

    def _seed(self, directory: Path, *urls: str) -> str:
        text = format_register(self._rows(*urls), 10, self.day, self.tz)
        write_text(directory / register_filename(self.day), text)
        return text

    def test_first_plan_returns_kept_and_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            kept = self._rows("https://x.test/a", "https://x.test/b")
            to_write, posted, status = plan_register(kept, directory, self.day)
            self.assertEqual([r.url for r in posted or []], ["https://x.test/a", "https://x.test/b"])
            self.assertEqual([r.url for r in to_write or []], ["https://x.test/a", "https://x.test/b"])
            self.assertIn("2 jobs", status)
            self.assertFalse((directory / "2026-09-01.txt").exists())

    def test_first_plan_empty_returns_none_to_post(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            to_write, posted, status = plan_register([], directory, self.day)
            self.assertEqual(to_write, [])
            self.assertIsNone(posted)
            self.assertIn("0 jobs", status)
            self.assertFalse((directory / "2026-09-01.txt").exists())

    def test_rerun_same_urls_skips_write_and_slack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            before = self._seed(directory, "https://x.test/a")
            to_write, posted, status = plan_register(
                self._rows("https://x.test/a"), directory, self.day
            )
            self.assertIsNone(to_write)
            self.assertIsNone(posted)
            self.assertIn("0 new", status)
            self.assertEqual((directory / "2026-09-01.txt").read_text(encoding="utf-8"), before)

    def test_rerun_appends_only_fresh_and_leaves_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            before = self._seed(directory, "https://x.test/a")
            to_write, posted, status = plan_register(
                self._rows("https://x.test/a", "https://x.test/c", "https://x.test/b"),
                directory,
                self.day,
            )
            self.assertEqual([r.url for r in posted or []], ["https://x.test/c", "https://x.test/b"])
            self.assertEqual(
                [r.url for r in to_write or []],
                ["https://x.test/a", "https://x.test/c", "https://x.test/b"],
            )
            self.assertIn("2 new", status)
            self.assertEqual((directory / "2026-09-01.txt").read_text(encoding="utf-8"), before)

    def test_rerun_keeps_urls_missing_from_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._seed(directory, "https://x.test/a")
            to_write, posted, status = plan_register(
                self._rows("https://x.test/b"), directory, self.day
            )
            self.assertEqual([r.url for r in posted or []], ["https://x.test/b"])
            self.assertEqual(
                [r.url for r in to_write or []],
                ["https://x.test/a", "https://x.test/b"],
            )
            self.assertIn("1 new", status)


class PostAndRecordTests(unittest.TestCase):
    day = date(2026, 9, 1)
    tz = "America/New_York"

    def _rows(self, *urls: str) -> list[JobRow]:
        return [JobRow(url, "") for url in urls]

    def test_persists_after_each_successful_block(self) -> None:
        posts: list[str] = []
        sleeps: list[float] = []

        def post(_webhook: str, text: str) -> None:
            posts.append(text)

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            n = post_and_record(
                [],
                self._rows(*[f"https://x.test/{i}" for i in range(12)]),
                directory,
                self.day,
                self.tz,
                10,
                "https://hooks.example/x",
                post=post,
                sleep=sleeps.append,
            )
            self.assertEqual(n, 2)
            self.assertEqual(len(posts), 2)
            self.assertEqual(posts[0].count("\n"), 9)
            self.assertEqual(posts[1], "https://x.test/10\nhttps://x.test/11")
            self.assertEqual(sleeps, [SLACK_POST_GAP_SECONDS])
            self.assertEqual(
                [r.url for r in load_day_rows(directory, self.day)],
                [f"https://x.test/{i}" for i in range(12)],
            )

    def test_failed_block_leaves_only_successful_ones(self) -> None:
        def post(_webhook: str, text: str) -> None:
            if "https://x.test/10" in text:
                raise SystemExit("Slack HTTP 500: boom")

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            prior = self._rows("https://x.test/old")
            write_text(
                directory / register_filename(self.day),
                format_register(prior, 10, self.day, self.tz),
            )
            with self.assertRaises(SystemExit):
                post_and_record(
                    prior,
                    self._rows(*[f"https://x.test/{i}" for i in range(12)]),
                    directory,
                    self.day,
                    self.tz,
                    10,
                    "https://hooks.example/x",
                    post=post,
                    sleep=lambda _: None,
                )
            self.assertEqual(
                [r.url for r in load_day_rows(directory, self.day)],
                ["https://x.test/old"] + [f"https://x.test/{i}" for i in range(10)],
            )

    def test_failed_first_block_leaves_prior_untouched(self) -> None:
        def post(_webhook: str, _text: str) -> None:
            raise SystemExit("Slack request failed")

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            prior = self._rows("https://x.test/old")
            before = format_register(prior, 10, self.day, self.tz)
            write_text(directory / register_filename(self.day), before)
            with self.assertRaises(SystemExit):
                post_and_record(
                    prior,
                    self._rows("https://x.test/new"),
                    directory,
                    self.day,
                    self.tz,
                    10,
                    "https://hooks.example/x",
                    post=post,
                    sleep=lambda _: None,
                )
            self.assertEqual(
                (directory / register_filename(self.day)).read_text(encoding="utf-8"),
                before,
            )


if __name__ == "__main__":
    unittest.main()
