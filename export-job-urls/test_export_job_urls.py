#!/usr/bin/env python3
"""Unit tests for the date-cut helpers (stdlib only)."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from day_register import (
    SLACK_WEBHOOK_ENV,
    load_dotenv,
    load_export_file,
    resolve_slack_webhook,
    slack_texts,
    url_blocks,
)
from export_job_urls import (
    MAX_WINDOW,
    JobRow,
    _USEastern,
    calendar_day,
    effective_tz,
    parse_export_header,
    parse_job_line,
    resolve_tz,
    rows_for_calendar_day,
    utc_offset_from_label,
    warn_if_window_truncated,
)


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

    def test_parse_job_line_bare_url(self) -> None:
        row = parse_job_line("https://x.test/a")
        self.assertEqual(row, JobRow(url="https://x.test/a", posted_at=""))


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

    def test_slack_texts_header_then_each_block(self) -> None:
        rows = [JobRow("https://x.test/a", ""), JobRow("https://x.test/b", "")]
        texts = slack_texts(rows, 10, date(2026, 8, 25), "America/New_York")
        self.assertEqual(texts[0], "register 2026-08-25 (America/New_York) — 2 jobs")
        self.assertEqual(texts[1], "https://x.test/a\nhttps://x.test/b")

    def test_slack_texts_header_only_when_empty(self) -> None:
        texts = slack_texts([], 10, date(2026, 8, 25), "America/New_York")
        self.assertEqual(texts, ["register 2026-08-25 (America/New_York) — 0 jobs"])

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


if __name__ == "__main__":
    unittest.main()
