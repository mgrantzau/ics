#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

TZ = ZoneInfo("Europe/Copenhagen")
OUT_PATH = Path("docs/tv-program.ics")
SOURCE_URL = "https://danskhaandbold.dk/tv-program"

DEFAULT_DURATION_MIN = 90
SCRIPT_VERSION = "2026-01-15-dom-parser-stable-uid-v1"

# Danish month mapping (short forms used on the site)
DA_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "maj": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "okt": 10,
    "nov": 11,
    "dec": 12,
}

DATE_RE = re.compile(r"^(mandag|tirsdag|onsdag|torsdag|fredag|lørdag|søndag)\s+(\d{1,2})\.\s+([a-zæøå]{3})\.$", re.IGNORECASE)
TIME_RE = re.compile(r"^kl\.\s*(\d{1,2}):(\d{2})$", re.IGNORECASE)


@dataclass(frozen=True)
class Event:
    start: datetime
    end: datetime
    summary: str
    location: str
    description: str
    uid: str


def ics_escape(text: str) -> str:
    """
    Escape per RFC5545 for TEXT values.
    - Backslash, semicolon, comma, newline
    """
    if text is None:
        return ""
    text = text.replace("\\", "\\\\")
    text = text.replace(";", "\\;")
    text = text.replace(",", "\\,")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
    return text


def normalize_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def dtstamp_utc() -> str:
    return datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")


def format_local(dt: datetime) -> str:
    # DTSTART;TZID=Europe/Copenhagen:YYYYMMDDTHHMMSS (seconds optional; keep HHMM)
    return dt.strftime("%Y%m%dT%H%M%S")


def vtimezone_block() -> str:
    # Minimal Europe/Copenhagen rules (works well for Apple/Fantastical)
    return "\n".join([
        "BEGIN:VTIMEZONE",
        "TZID:Europe/Copenhagen",
        "X-LIC-LOCATION:Europe/Copenhagen",
        "BEGIN:DAYLIGHT",
        "TZOFFSETFROM:+0100",
        "TZOFFSETTO:+0200",
        "TZNAME:CEST",
        "DTSTART:19700329T020000",
        "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU",
        "END:DAYLIGHT",
        "BEGIN:STANDARD",
        "TZOFFSETFROM:+0200",
        "TZOFFSETTO:+0100",
        "TZNAME:CET",
        "DTSTART:19701025T030000",
        "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU",
        "END:STANDARD",
        "END:VTIMEZONE",
    ])


def make_uid(start_dt: datetime, summary: str) -> str:
    """
    Stable UID across runs:
    - depends ONLY on DTSTART + SUMMARY
    This prevents duplicates when location/description changes.
    """
    uid_basis = f"{start_dt.isoformat()}|{normalize_space(summary)}"
    return f"{uuid.uuid5(uuid.NAMESPACE_URL, uid_basis)}@danskhaandbold.tvprogram"


def parse_date_line(line: str, assumed_year: int) -> datetime.date | None:
    m = DATE_RE.match(normalize_space(line).lower())
    if not m:
        return None
    day = int(m.group(2))
    mon_txt = m.group(3).lower()
    if mon_txt not in DA_MONTHS:
        return None
    month = DA_MONTHS[mon_txt]
    return datetime(assumed_year, month, day, tzinfo=TZ).date()


def parse_time_line(line: str) -> tuple[int, int] | None:
    m = TIME_RE.match(normalize_space(line))
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh, mm


def get_assumed_year() -> int:
    """
    The site shows day+month but not year.
    We assume current year, but if month is "early" and we are late in year,
    the date might refer to next year. We'll fix per-event below with rollover logic.
    """
    now = datetime.now(TZ)
    return now.year


def fix_year_rollover(d: datetime.date) -> datetime.date:
    """
    If we are in December and events show Jan/Feb etc, they may be next year.
    Simple rule: if date is > 180 days in the past relative to "now", bump year.
    """
    now = datetime.now(TZ).date()
    if (now - d).days > 180:
        # move to next year
        try:
            return d.replace(year=d.year + 1)
        except ValueError:
            # Feb 29 edge case (unlikely here)
            return d.replace(year=d.year + 1, day=28)
    return d


def scrape_main_text(page) -> list[str]:
    # Pull visible main text; robust if DOM structure changes.
    return page.inner_text("main").splitlines()


def scroll_to_load_more(page, max_scrolls: int = 30, wait_ms: int = 1200) -> None:
    """
    The page can lazy-load more items as you scroll.
    We scroll until the number of event blocks stabilizes.
    """
    # Wait for initial render
    page.wait_for_timeout(2000)

    last_count = -1
    stable_rounds = 0

    for _ in range(max_scrolls):
        count = page.evaluate(r"""
            () => document.querySelectorAll("main .odd\\:bg-charcoal\\/5").length
        """)
        if count == last_count:
            stable_rounds += 1
        else:
            stable_rounds = 0
            last_count = count

        if stable_rounds >= 2:
            break

        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(wait_ms)


def extract_rows(page) -> list[dict]:
    """
    Prefer structured DOM blocks if present.
    Each row block is expected to contain:
      - time line "kl. HH:MM"
      - title/summary (teams)
      - description/competition (line below title)
      - channel line maybe contains TV2/DR/TV3 etc
    """
    # The site used these blocks previously; keep as first choice
    js = r"""
    () => {
      const blocks = Array.from(document.querySelectorAll("main .odd\\:bg-charcoal\\/5"));
      return blocks.map(b => {
        const t = b.innerText.split("\n").map(x => x.trim()).filter(Boolean);
        return { lines: t };
      });
    }
    """
    blocks = page.evaluate(js)
    rows = []
    for b in blocks:
        lines = b.get("lines") or []
        rows.append({"lines": lines})
    return rows


def parse_events_from_rows(rows: list[dict]) -> list[Event]:
    """
    Parse events from structured row blocks.
    We still need the date context; the date appears as a separate text line in the page,
    not always inside each block. Therefore we also read main text and parse sequentially,
    because it's the most reliable way to track date -> time -> summary -> description -> channel.
    """
    # Fallback: parse from main text sequentially (more stable)
    # We'll do that as primary; rows are only used to sanity-check "channel" patterns if needed.
    return []


def parse_events_from_main_lines(lines: list[str]) -> list[Event]:
    assumed_year = get_assumed_year()
    current_date = None

    events: list[Event] = []

    i = 0
    while i < len(lines):
        line = normalize_space(lines[i])
        low = line.lower()

        # date line
        d = parse_date_line(low, assumed_year)
        if d:
            current_date = fix_year_rollover(d)
            i += 1
            continue

        # time line
        tm = parse_time_line(line)
        if tm and current_date:
            hh, mm = tm
            start_dt = datetime(current_date.year, current_date.month, current_date.day, hh, mm, tzinfo=TZ)
            end_dt = start_dt + timedelta(minutes=DEFAULT_DURATION_MIN)

            # Next lines usually:
            # summary
            # description
            # (optional) "afspilles på X" OR just channel line
            summary = ""
            description = ""
            location = ""

            # summary
            if i + 1 < len(lines):
                summary = normalize_space(lines[i + 1])

            # description
            if i + 2 < len(lines):
                description = normalize_space(lines[i + 2])

            # channel / location:
            # We scan the next few lines for a channel-ish value, stopping if we hit a new "kl." or weekday date.
            channel = ""
            scan_limit = min(len(lines), i + 8)
            for j in range(i + 3, scan_limit):
                cand = normalize_space(lines[j])

                # stop if next event/date begins
                if TIME_RE.match(cand) or DATE_RE.match(cand.lower()):
                    break

                # accept patterns:
                # "afspilles på TV2 Sport"
                # or plain "TV2 Sport"
                m = re.search(r"afspilles på\s+(.+)$", cand, flags=re.IGNORECASE)
                if m:
                    channel = normalize_space(m.group(1))
                    break

                # plain channel line heuristics
                if re.match(r"^(tv2|tv3|dr)\b", cand.strip().lower()):
                    channel = cand
                    break

            location = channel

            # Guard: require at least summary + a competition/description-ish line
            if summary and description and "program" not in summary.lower():
                uid = make_uid(start_dt, summary)
                events.append(Event(
                    start=start_dt,
                    end=end_dt,
                    summary=summary,
                    location=location,
                    description=description,
                    uid=uid,
                ))

            i += 1
            continue

        i += 1

    # Deduplicate by UID (in case parsing repeats)
    uniq = {}
    for e in events:
        uniq[e.uid] = e
    return list(uniq.values())


def render_ics(events: list[Event]) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "PRODID:-//ChatGPT//Handball ICS//DA",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-SCRIPT-VERSION:{SCRIPT_VERSION}",
        vtimezone_block(),
    ]

    stamp = dtstamp_utc()

    # Sort by start time for stable output (nice for diffs)
    events = sorted(events, key=lambda e: e.start)

    for e in events:
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{e.uid}",
            f"DTSTAMP:{stamp}",
            f"DTSTART;TZID=Europe/Copenhagen:{format_local(e.start)}",
            f"DTEND;TZID=Europe/Copenhagen:{format_local(e.end)}",
            f"SUMMARY:{ics_escape(e.summary)}",
        ])
        if e.location:
            lines.append(f"LOCATION:{ics_escape(e.location)}")
        if e.description:
            lines.append(f"DESCRIPTION:{ics_escape(e.description)}")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\n".join(lines) + "\n"


def main() -> int:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        try:
            page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=60000)
        except PlaywrightTimeoutError:
            print("ERROR: Timeout loading the page.")
            browser.close()
            return 2

        # Scroll to load more (important)
        try:
            scroll_to_load_more(page)
        except Exception:
            # If scrolling fails, still try parsing what we have
            pass

        # Parse using main text (most robust)
        main_lines = scrape_main_text(page)
        events = parse_events_from_main_lines(main_lines)

        browser.close()

    if not events:
        print("ERROR: No events were parsed. The site structure may have changed.")
        print("Dumping first 120 lines of main text:")
        for ln in main_lines[:120]:
            print(ln)
        return 2

    ics = render_ics(events)
    OUT_PATH.write_text(ics, encoding="utf-8")

    print(f"Generated {len(events)} events -> {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
