#!/usr/bin/env python3
"""Export job apply URLs from the public freehire search API into a text file.

Auth is not required — GET /api/v1/jobs/search is public.
This script is self-contained (stdlib only) and can be copied anywhere.

Each output line is `posted_at<TAB>created_at<TAB>url` (either timestamp may be empty).
By default only the previous calendar day in America/New_York is kept
(`posted_at` on that day). New postings are listed first; `posted_at` bumps
follow. `--new-only` also drops an earlier `created_at` (a source
`updated_at` bump). The file starts with `# date:` / `# timezone:`.
Filters match the freehire job-search API / SPA query vocabulary
(docs/API.md § Filtering jobs, internal/search StringFacets).

Usage:
  python export_job_urls.py
  python export_job_urls.py --work-mode remote --countries us --category backend,frontend
  python export_job_urls.py --from-url "https://freehire.me/?work_mode=remote&countries=us&requires_clearance=false"
  python export_job_urls.py --query "skills=go,rust&skills_mode=and&work_mode=remote"
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, NamedTuple

DEFAULT_BASE = "https://freehire.me"
PAGE_SIZE = 100  # API max is 100
MAX_WINDOW = 10_000  # API hard stop: offset + limit ≤ 10000
SEARCH_PATH = "/api/v1/jobs/search"
# Same filters as SEARCH_PATH; each hit carries the full posting body.
AGENT_SEARCH_PATH = "/api/v1/agent/jobs/search"

SCRIPT_DIR = Path(__file__).resolve().parent
# Timestamped URL dumps from this script only (not the day-register folder).
EXPORT_DIR = SCRIPT_DIR / "exports"

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
    # true-or-absent boolean; the API treats "false" as "not marked" (see
    # search.RequiresClearanceParam), so the value must be forwarded as-is.
    "requires_clearance",
    "role",
    "ai_archetype",
    "role_type",
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
    "visa_sponsorship",
    "salary_min",
    "salary_max",
    "experience_years_min",
    "experience_years_max",
    "posted_within_days",
    "open_within_days",
)

# Dropped when importing a SPA/API URL. Pagination is owned by this script.
# semantic_ratio was removed from GET /jobs/search with hybrid search.
DROPPED_IMPORT_KEYS = frozenset({"limit", "offset", "semantic_ratio"})

# Used only when the caller passes no filters at all.
# Search facets only. posted_within_days is not a default — ensure_posted_window
# sizes the API fetch from --date (yesterday morning still covers 00:00), then
# the calendar cut keeps that day.
# Category is the product-engineering IC set the daily bid is built from
# (software_engineering plus backend/frontend/fullstack/mobile). devops /
# data_engineering / ai_engineering stay commented out — different bid —
# as do SRE/QA/security/architecture and the science/analytics/training ML
# buckets. Hybrid/onsite stay out — those are not bid-from-anywhere roles.
# Clearance matches the SPA search the daily list is built from
# (requires_clearance=false). Relocation is left unset so the API
# does not filter on it.
DEFAULT_FILTERS: dict[str, str] = {
    "work_mode": "remote",
    "category": (
        "software_engineering,backend,frontend,fullstack,mobile"
        # ",devops,data_engineering,ai_engineering"
    ),
    "countries": "us",
    "requires_clearance": "false",
}

DEFAULT_TZ_NAME = "America/New_York"

# Country names only (not cities). "Sydney, Australia" drops via Australia;
# bare "Remote" / "New York" / empty names no foreign country and is kept.
# "georgia" and "palestine" are omitted — they collide with US places.
# "new mexico" is the US state; it maps to us so "mexico" cannot claim that span.
# Longer names claim their span first so "united states" wins over "us".
_COUNTRY_NAME_TO_CODE: dict[str, str] = {
    "united states of america": "us",
    "united states": "us",
    "new mexico": "us",
    "estados unidos": "us",
    "vereinigte staaten": "us",
    "u.s.a.": "us",
    "u.s.": "us",
    "usa": "us",
    "us": "us",
    "puerto rico": "us",
    "united kingdom": "gb",
    "great britain": "gb",
    "reino unido": "gb",
    "vereinigtes königreich": "gb",
    "england": "gb",
    "britain": "gb",
    "u.k.": "gb",
    "uk": "gb",
    "united arab emirates": "ae",
    "emiratos árabes unidos": "ae",
    "emirados árabes unidos": "ae",
    "vereinigte arabische emirate": "ae",
    "uae": "ae",
    "saudi arabia": "sa",
    "arabia saudita": "sa",
    "arábia saudita": "sa",
    "saudi-arabien": "sa",
    "south korea": "kr",
    "corea del sur": "kr",
    "coreia do sul": "kr",
    "südkorea": "kr",
    "korea": "kr",
    "hong kong": "hk",
    "new zealand": "nz",
    "south africa": "za",
    "czech republic": "cz",
    "dominican republic": "do",
    "the netherlands": "nl",
    "netherlands": "nl",
    "países bajos": "nl",
    "niederlande": "nl",
    "sri lanka": "lk",
    "costa rica": "cr",
    "el salvador": "sv",
    "north macedonia": "mk",
    "bosnia and herzegovina": "ba",
    "ivory coast": "ci",
    "côte d'ivoire": "ci",
    "cote d'ivoire": "ci",
    "brunei darussalam": "bn",
    "palestinian territories": "ps",
    "state of palestine": "ps",
    "australia": "au",
    "austria": "at",
    "germany": "de",
    "deutschland": "de",
    "alemania": "de",
    "alemanha": "de",
    "france": "fr",
    "francia": "fr",
    "frança": "fr",
    "frankreich": "fr",
    "spain": "es",
    "españa": "es",
    "espanha": "es",
    "spanien": "es",
    "italy": "it",
    "italia": "it",
    "italien": "it",
    "sweden": "se",
    "sverige": "se",
    "suecia": "se",
    "schweden": "se",
    "poland": "pl",
    "polonia": "pl",
    "polónia": "pl",
    "polen": "pl",
    "ireland": "ie",
    "irland": "ie",
    "portugal": "pt",
    "switzerland": "ch",
    "suiza": "ch",
    "suíça": "ch",
    "schweiz": "ch",
    "belgium": "be",
    "belgien": "be",
    "denmark": "dk",
    "danmark": "dk",
    "dänemark": "dk",
    "finland": "fi",
    "suomi": "fi",
    "finnland": "fi",
    "norway": "no",
    "norge": "no",
    "norwegen": "no",
    "ukraine": "ua",
    "canada": "ca",
    "singapore": "sg",
    "japan": "jp",
    "japón": "jp",
    "japão": "jp",
    "india": "in",
    "argentina": "ar",
    "brazil": "br",
    "brasil": "br",
    "mexico": "mx",
    "méxico": "mx",
    "israel": "il",
    "china": "cn",
    "greece": "gr",
    "grecia": "gr",
    "grécia": "gr",
    "griechenland": "gr",
    "philippines": "ph",
    "filipinas": "ph",
    "philippinen": "ph",
    "colombia": "co",
    "cyprus": "cy",
    "taiwan": "tw",
    "malaysia": "my",
    "malasia": "my",
    "romania": "ro",
    "rumanía": "ro",
    "roménia": "ro",
    "rumänien": "ro",
    "hungary": "hu",
    "hungría": "hu",
    "hungria": "hu",
    "ungarn": "hu",
    "bulgaria": "bg",
    "bulgarien": "bg",
    "thailand": "th",
    "tailandia": "th",
    "indonesia": "id",
    "indonesien": "id",
    "vietnam": "vn",
    "turkey": "tr",
    "türkiye": "tr",
    "turquía": "tr",
    "turquia": "tr",
    "türkei": "tr",
    "egypt": "eg",
    "egipto": "eg",
    "egito": "eg",
    "ägypten": "eg",
    "lebanon": "lb",
    "qatar": "qa",
    "kuwait": "kw",
    "bahrain": "bh",
    "oman": "om",
    "jordan": "jo",
    "iraq": "iq",
    "iran": "ir",
    "morocco": "ma",
    "algeria": "dz",
    "tunisia": "tn",
    "pakistan": "pk",
    "bangladesh": "bd",
    "nepal": "np",
    "peru": "pe",
    "perú": "pe",
    "chile": "cl",
    "uruguay": "uy",
    "ecuador": "ec",
    "bolivia": "bo",
    "paraguay": "py",
    "venezuela": "ve",
    "panama": "pa",
    "guatemala": "gt",
    "nigeria": "ng",
    "kenya": "ke",
    "ghana": "gh",
    "ethiopia": "et",
    "czechia": "cz",
    "tschechien": "cz",
    "serbia": "rs",
    "croatia": "hr",
    "slovenia": "si",
    "slovakia": "sk",
    "lithuania": "lt",
    "latvia": "lv",
    "estonia": "ee",
    "luxembourg": "lu",
    "iceland": "is",
    "malta": "mt",
    "monaco": "mc",
    "albania": "al",
    "montenegro": "me",
    "kosovo": "xk",
    "mauritius": "mu",
    "andorra": "ad",
    "angola": "ao",
    "brunei": "bn",
    "cameroon": "cm",
    "honduras": "hn",
    "cambodia": "kh",
    "laos": "la",
    "liechtenstein": "li",
    "libya": "ly",
    "myanmar": "mm",
    "burma": "mm",
    "mongolia": "mn",
    "macao": "mo",
    "macau": "mo",
    "mozambique": "mz",
    "nicaragua": "ni",
    "rwanda": "rw",
    "san marino": "sm",
    "senegal": "sn",
    "tanzania": "tz",
    "uganda": "ug",
    "yemen": "ye",
    "zambia": "zm",
    "zimbabwe": "zw",
    "russia": "ru",
    "uzbekistan": "uz",
    "kazakhstan": "kz",
    "kyrgyzstan": "kg",
    "tajikistan": "tj",
    "turkmenistan": "tm",
    "belarus": "by",
    "moldova": "md",
    "armenia": "am",
    "azerbaijan": "az",
}

_COUNTRY_NAME_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])"), code)
    for name, code in sorted(_COUNTRY_NAME_TO_CODE.items(), key=lambda kv: -len(kv[0]))
]


def requested_country_codes(filters: dict[str, str]) -> frozenset[str]:
    """ISO codes from the `countries` search filter, or empty when unset.

    Aliases use the same table as location matching (`usa`, `United States`,
    `uk` → us/gb). An unrecognized token is kept as-is so a raw ISO code
    like `de` still matches a location that resolved to that code.
    """
    raw = filters.get("countries") or ""
    codes: set[str] = set()
    for part in raw.split(","):
        token = part.strip().casefold()
        if not token:
            continue
        codes.add(_COUNTRY_NAME_TO_CODE.get(token, token))
    return frozenset(codes)


def location_names_other_country(location: str, keep: frozenset[str]) -> bool:
    """True when location names a country outside `keep` and none inside it.

    Bare Remote / a US city / an empty string name no country and stay.
    "Spain / United States" names a keep-country and stays.
    """
    if not keep or not location.strip():
        return False
    named = countries_named_in(location)
    if not named:
        return False
    return named.isdisjoint(keep)


def countries_named_in(location: str) -> set[str]:
    """ISO codes of country names that appear as whole words in location.

    Patterns are longest-first; a shorter name inside an already-claimed span
    is skipped ("New Mexico" is us, not mx).
    """
    text = location.casefold()
    found: set[str] = set()
    claimed: list[tuple[int, int]] = []
    for pattern, code in _COUNTRY_NAME_PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span()
            if any(start < claimed_end and end > claimed_start for claimed_start, claimed_end in claimed):
                continue
            claimed.append((start, end))
            found.add(code)
    return found


class JobRow(NamedTuple):
    url: str
    posted_at: str  # RFC3339 from the API, or "" when the row has none
    created_at: str = ""  # first ingest time; empty on legacy two-column exports
    title: str = ""
    company: str = ""
    public_slug: str = ""
    description: str = ""  # full body only when fetched via AGENT_SEARCH_PATH


def default_output_path(day: date | None = None) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if day is None:
        return EXPORT_DIR / f"job_urls_{stamp}.txt"
    return EXPORT_DIR / f"job_urls_{day.isoformat()}_{stamp[9:]}.txt"


def format_job_line(row: JobRow) -> str:
    """One export line: posted_at, created_at, apply URL."""
    return f"{row.posted_at}\t{row.created_at}\t{row.url}"


def parse_job_line(raw: str) -> JobRow | None:
    """Parse one export line (`posted_at\\tcreated_at\\turl`, `posted_at\\turl`, or a bare URL)."""
    line = raw.strip()
    if not line or line.startswith("#"):
        return None
    if "\t" not in line:
        return JobRow(url=line, posted_at="")
    posted_at, rest = line.split("\t", 1)
    posted_at = posted_at.strip()
    if "\t" in rest:
        created_at, url = rest.split("\t", 1)
        created_at = created_at.strip()
        url = url.strip()
        if not url:
            return None
        return JobRow(url=url, posted_at=posted_at, created_at=created_at)
    url = rest.strip()
    if not url:
        return None
    return JobRow(url=url, posted_at=posted_at)


class _USEastern(tzinfo):
    """America/New_York using US DST rules (2007–). Used when zoneinfo has no tzdata.

    fromutc is required: datetime.astimezone / datetime.now(tz) convert through
    UTC, and the default tzinfo.fromutc treats that UTC wall clock as local,
    which shifts the November fallback by five hours and can move the calendar
    day (2026-11-01 04:30 UTC is 00:30 EDT, not 23:30 EDT the night before).
    """

    def utcoffset(self, dt: datetime | None) -> timedelta:
        return timedelta(hours=-4) if self.dst(dt) else timedelta(hours=-5)

    def dst(self, dt: datetime | None) -> timedelta:
        if dt is None:
            return timedelta(0)
        # fold=1 is the second 1am on the November fallback day (standard time).
        if dt.fold == 1:
            return timedelta(0)
        return timedelta(hours=1) if _us_eastern_is_dst(dt) else timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        return "EDT" if self.dst(dt) else "EST"

    def fromutc(self, dt: datetime) -> datetime:
        if dt.tzinfo is not self:
            raise ValueError("fromutc: dt.tzinfo is not self")
        utc = dt.replace(tzinfo=None)
        start = datetime.combine(_nth_sunday(utc.year, 3, 2), datetime.min.time()).replace(
            hour=7
        )
        end = datetime.combine(_nth_sunday(utc.year, 11, 1), datetime.min.time()).replace(
            hour=6
        )
        is_dst = start <= utc < end
        local = utc + timedelta(hours=-4 if is_dst else -5)
        fold = 0
        if not is_dst:
            repeat = datetime.combine(_nth_sunday(utc.year, 11, 1), datetime.min.time()).replace(
                hour=1
            )
            if repeat <= local < repeat.replace(hour=2):
                fold = 1
        return local.replace(tzinfo=self, fold=fold)


def _nth_sunday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    first_sun = 1 + (6 - first.weekday()) % 7
    return date(year, month, first_sun + 7 * (n - 1))


def _us_eastern_is_dst(dt: datetime) -> bool:
    """True when *local* Eastern wall time is in DST (2am March–2am November)."""
    local = dt.replace(tzinfo=None) if dt.tzinfo else dt
    start = datetime.combine(_nth_sunday(local.year, 3, 2), datetime.min.time()).replace(hour=2)
    end = datetime.combine(_nth_sunday(local.year, 11, 1), datetime.min.time()).replace(hour=2)
    return start <= local < end


def tz_from_offset(hours: int) -> timezone:
    if not -14 <= hours <= 14:
        raise SystemExit("--tz-offset must be between -14 and 14")
    return timezone(timedelta(hours=hours))


def utc_offset_from_label(label: str) -> int | None:
    """Parse a `UTC+9` / `UTC-5` / `UTC` label. None if not that shape."""
    if label == "UTC":
        return 0
    if not label.startswith("UTC") or len(label) < 4 or label[3] not in "+-":
        return None
    try:
        hours = int(label[3:])
    except ValueError:
        return None
    if not -14 <= hours <= 14:
        return None
    return hours


def resolve_tz(name: str | None, offset: int | None) -> tuple[tzinfo, str]:
    if offset is not None:
        return tz_from_offset(offset), f"UTC{offset:+d}"
    label = name or DEFAULT_TZ_NAME
    parsed = utc_offset_from_label(label)
    if parsed is not None:
        return tz_from_offset(parsed), label
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(label), label
    except Exception:
        if label in {"America/New_York", "US/Eastern"}:
            return _USEastern(), label
        raise SystemExit(
            f"unknown timezone {label!r}: pass --tz-offset or use America/New_York"
        )


def effective_tz(
    name: str | None,
    offset: int | None,
    header_label: str | None = None,
) -> tuple[tzinfo, str]:
    """CLI offset, then CLI name, then an export `# timezone:`, then New York."""
    if offset is not None:
        return resolve_tz(None, offset)
    if name:
        return resolve_tz(name, None)
    if header_label:
        return resolve_tz(header_label, None)
    return resolve_tz(DEFAULT_TZ_NAME, None)


def parse_posted_at(raw: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def calendar_day(posted_at: str, tz: tzinfo) -> date | None:
    dt = parse_posted_at(posted_at)
    if dt is None:
        return None
    return dt.astimezone(tz).date()


def yesterday(tz: tzinfo) -> date:
    return datetime.now(tz).date() - timedelta(days=1)


def parse_date_arg(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as e:
        raise SystemExit(f"--date must be YYYY-MM-DD or all, got {raw!r}") from e


def days_needed_for(target: date, tz: tzinfo) -> int:
    """Minimum posted_within_days so a morning run still covers target 00:00."""
    today = datetime.now(tz).date()
    delta = (today - target).days
    if delta < 0:
        raise SystemExit(f"--date {target.isoformat()} is in the future")
    return delta + 1


def ensure_posted_window(filters: dict[str, str], target: date, tz: tzinfo) -> dict[str, str]:
    need = str(days_needed_for(target, tz))
    have = filters.get("posted_within_days")
    out = dict(filters)
    if have is None or not have.isdigit() or int(have) < int(need):
        if have is not None and have != need:
            print(
                f"posted_within_days={have} is too narrow for {target.isoformat()}; "
                f"using {need}",
                file=sys.stderr,
            )
        out["posted_within_days"] = need
    return out


def is_posted_at_bump(row: JobRow, target: date, tz: tzinfo) -> bool:
    """True when created_at is a day before target (a source updated_at bump).

    A created_at on or after target is not a bump — ingest can cross midnight.
    A missing created_at (legacy two-column export) is not a bump.
    """
    created_day = calendar_day(row.created_at, tz)
    return created_day is not None and created_day < target


def order_new_first(rows: list[JobRow], target: date, tz: tzinfo) -> list[JobRow]:
    """Truly new postings first, then posted_at bumps. Each group by posted_at, url."""
    new: list[JobRow] = []
    bumps: list[JobRow] = []
    for row in rows:
        if is_posted_at_bump(row, target, tz):
            bumps.append(row)
        else:
            new.append(row)
    def by_posted(r: JobRow) -> tuple[str, str]:
        return (r.posted_at, r.url)

    new.sort(key=by_posted)
    bumps.sort(key=by_posted)
    return new + bumps


def filter_by_day(
    rows: list[JobRow],
    target: date,
    tz: tzinfo,
    *,
    drop_bumps: bool = False,
) -> tuple[list[JobRow], int]:
    """Keep rows whose posted_at falls on target.

    With drop_bumps (CLI `--new-only`), an earlier created_at is a source
    `updated_at` bump (Greenhouse), not a new posting, and is dropped. A
    created_at on or after target is kept — ingest can cross midnight without
    making the job a bump. A missing created_at (legacy two-column export) is
    not a bump; posted_at alone decides. Default keeps every row on that day
    and lists new postings first, then bumps.
    """
    kept: list[JobRow] = []
    undated = 0
    bumped = 0
    for row in rows:
        day = calendar_day(row.posted_at, tz)
        if day is None:
            undated += 1
            continue
        if day != target:
            continue
        if drop_bumps and is_posted_at_bump(row, target, tz):
            bumped += 1
            continue
        kept.append(row)
    kept = order_new_first(kept, target, tz)
    if bumped:
        print(
            f"dropped {bumped} jobs whose created_at is before {target.isoformat()} "
            f"(posted_at bump)",
            file=sys.stderr,
        )
    return kept, undated


def resolve_target_date(raw: str | None, tz: tzinfo) -> date | None:
    """None means no calendar cut (`--date all`). Empty/default is yesterday."""
    if raw is None or raw == "":
        return yesterday(tz)
    if raw == "all":
        return None
    return parse_date_arg(raw)


def require_date_for_new_only(new_only: bool, target: date | None) -> None:
    """`--new-only` compares created_at to --date; `--date all` has no day."""
    if new_only and target is None:
        raise SystemExit("--new-only needs a calendar day; do not pass --date all")


def date_file_header(day: date, tz_label: str) -> str:
    return f"# date: {day.isoformat()}\n# timezone: {tz_label}\n\n"


def parse_export_header(text: str) -> tuple[date | None, str | None]:
    """Read `# date:` / `# timezone:` from the leading comment block of an export."""
    day: date | None = None
    tz_label: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if not line.startswith("#"):
            break
        key, _, value = line[1:].partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "date" and value:
            try:
                day = date.fromisoformat(value)
            except ValueError as e:
                raise SystemExit(f"invalid # date: {value!r}") from e
        elif key == "timezone" and value:
            tz_label = value
    return day, tz_label


def rows_for_calendar_day(
    rows: list[JobRow],
    target: date,
    tz: tzinfo,
    *,
    keep_undated: bool = False,
    drop_bumps: bool = False,
) -> tuple[list[JobRow], int]:
    """Keep rows whose posted_at falls on target.

    drop_bumps (`--new-only`) also drops an earlier created_at bump.
    Without it, new postings are listed first and bumps follow.
    When keep_undated is set and every row lacks a usable posted_at (a legacy
    bare-URL export), the whole list is treated as already being that day.
    """
    kept, undated = filter_by_day(rows, target, tz, drop_bumps=drop_bumps)
    if keep_undated and rows and undated == len(rows):
        return list(rows), undated
    return kept, undated


def warn_if_window_truncated(
    total: int,
    rows: list[JobRow],
    target: date | None,
    tz: tzinfo,
) -> None:
    if total <= MAX_WINDOW:
        return
    print(
        f"warning: api window caps at {MAX_WINDOW}; {total - MAX_WINDOW} matches were not fetched",
        file=sys.stderr,
    )
    if target is None:
        return
    oldest: date | None = None
    for row in rows:
        day = calendar_day(row.posted_at, tz)
        if day is not None and (oldest is None or day < oldest):
            oldest = day
    if oldest is not None and oldest > target:
        print(
            f"warning: newest {MAX_WINDOW} matches only reach back to {oldest.isoformat()}; "
            f"{target.isoformat()} was not fetched",
            file=sys.stderr,
        )


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
        if key in DROPPED_IMPORT_KEYS:
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


def add_new_only_argument(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--new-only",
        action="store_true",
        help=(
            "keep only first-seen postings: drop a row whose created_at is "
            "before --date (a Greenhouse posted_at bump). Default keeps every "
            "row whose posted_at falls on that day and lists new postings first"
        ),
    )


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
    g.add_argument("--visa-sponsorship", dest="visa_sponsorship", choices=("true", "false"), default=None)
    g.add_argument("--salary-min", dest="salary_min", default=None, metavar="N")
    g.add_argument("--salary-max", dest="salary_max", default=None, metavar="N")
    g.add_argument("--experience-years-min", dest="experience_years_min", default=None, metavar="N")
    g.add_argument("--experience-years-max", dest="experience_years_max", default=None, metavar="N")
    g.add_argument("--posted-within-days", dest="posted_within_days", default=None, metavar="N")
    g.add_argument("--open-within-days", dest="open_within_days", default=None, metavar="N")


def _job_str(job: dict[str, Any], key: str) -> str:
    value = job.get(key) or ""
    return value if isinstance(value, str) else str(value)


def fetch_page(
    base: str,
    filters: dict[str, str],
    limit: int,
    offset: int,
    *,
    path: str = SEARCH_PATH,
    extra: dict[str, str] | None = None,
) -> dict[str, Any]:
    params = {**filters, "limit": str(limit), "offset": str(offset)}
    if extra:
        params.update(extra)
    url = f"{base.rstrip('/')}/{path.lstrip('/')}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "freehire-export-job-urls/1.0"},
    )
    timeout = 90 if path.rstrip("/") == AGENT_SEARCH_PATH.rstrip("/") else 60
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def collect_jobs(
    base: str,
    filters: dict[str, str],
    page_size: int = PAGE_SIZE,
    *,
    with_description: bool = False,
    description_format: str = "text",
) -> tuple[list[JobRow], int]:
    rows: list[JobRow] = []
    offset = 0
    total = None
    keep_countries = requested_country_codes(filters)
    dropped_other_country = 0

    path = AGENT_SEARCH_PATH if with_description else SEARCH_PATH
    extra = {"description_format": description_format} if with_description else None

    print(
        "filters: " + urllib.parse.urlencode(filters).replace("%2C", ","),
        file=sys.stderr,
    )
    if with_description:
        print(f"descriptions: {path} format={description_format}", file=sys.stderr)

    while offset < MAX_WINDOW:
        limit = min(page_size, MAX_WINDOW - offset)
        if limit <= 0:
            break
        try:
            body = fetch_page(base, filters, limit, offset, path=path, extra=extra)
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
            if not u:
                continue
            loc = job.get("location") or ""
            if not isinstance(loc, str):
                loc = str(loc)
            if location_names_other_country(loc, keep_countries):
                dropped_other_country += 1
                continue
            posted = _job_str(job, "posted_at")
            created = _job_str(job, "created_at")
            description = _job_str(job, "description") if with_description else ""
            rows.append(
                JobRow(
                    url=u,
                    posted_at=posted,
                    created_at=created,
                    title=_job_str(job, "title"),
                    company=_job_str(job, "company"),
                    public_slug=_job_str(job, "public_slug"),
                    description=description,
                )
            )

        offset += len(data)
        print(f"fetched {offset}/{total} rows (jobs so far={len(rows)})", file=sys.stderr)

        if offset >= total:
            break
        time.sleep(0.2)

    seen: set[str] = set()
    unique: list[JobRow] = []
    for row in rows:
        if row.url not in seen:
            seen.add(row.url)
            unique.append(row)
    if len(unique) != len(rows):
        print(
            f"deduped apply urls: {len(rows)} → {len(unique)}",
            file=sys.stderr,
        )
    if dropped_other_country:
        print(
            f"dropped {dropped_other_country} jobs whose location names a "
            f"country outside countries={','.join(sorted(keep_countries))}",
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
        help="output text file (default: exports/job_urls_YYYY-MM-DD_HHMMSS.txt)",
    )
    p.add_argument("--base-url", default=DEFAULT_BASE, help=f"API origin (default: {DEFAULT_BASE})")
    p.add_argument("--page-size", type=int, default=PAGE_SIZE, help=f"page size 1–100 (default: {PAGE_SIZE})")
    p.add_argument(
        "--print-filters",
        action="store_true",
        help="print resolved filters and exit (no request)",
    )
    p.add_argument(
        "--date",
        default=None,
        metavar="YYYY-MM-DD",
        help="keep only this calendar day (default: yesterday in America/New_York; `all` = no date cut)",
    )
    add_new_only_argument(p)
    p.add_argument(
        "--tz",
        default=DEFAULT_TZ_NAME,
        metavar="NAME",
        help=f"IANA timezone for --date (default: {DEFAULT_TZ_NAME})",
    )
    p.add_argument(
        "--tz-offset",
        type=int,
        default=None,
        metavar="H",
        help="hours east of UTC; overrides --tz when set",
    )
    add_filter_arguments(p)
    args = p.parse_args()

    if not 1 <= args.page_size <= 100:
        raise SystemExit("--page-size must be between 1 and 100")

    tz, tz_label = resolve_tz(args.tz, args.tz_offset)
    target = resolve_target_date(args.date, tz)
    require_date_for_new_only(args.new_only, target)
    filters = filters_from_args(args)
    if target is not None:
        filters = ensure_posted_window(filters, target, tz)
        if "sort" not in filters:
            filters["sort"] = "posted_at"

    if args.print_filters:
        for k, v in sorted(filters.items()):
            print(f"{k}={v}")
        print(f"date={target.isoformat() if target else 'all'}")
        print(f"timezone={tz_label}")
        print(f"new_only={'true' if args.new_only else 'false'}")
        return

    out = Path(args.output) if args.output else default_output_path(target)
    if not out.is_absolute():
        # Relative -o paths land in exports/, not the caller's cwd.
        out = EXPORT_DIR / out

    rows, total = collect_jobs(args.base_url, filters, args.page_size)
    warn_if_window_truncated(total, rows, target, tz)
    if target is not None:
        rows, undated = filter_by_day(rows, target, tz, drop_bumps=args.new_only)
        if undated:
            print(f"skipped {undated} rows with no usable posted_at", file=sys.stderr)

    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        if target is not None:
            f.write(date_file_header(target, tz_label))
        f.write("\n".join(format_job_line(row) for row in rows))
        if rows:
            f.write("\n")

    if target is not None:
        print(
            f"wrote {len(rows)} jobs for {target.isoformat()} ({tz_label}) "
            f"(api total={total}) → {out}",
            file=sys.stderr,
        )
    else:
        print(f"wrote {len(rows)} jobs (api total={total}) → {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
