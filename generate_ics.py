#!/usr/bin/env python3
# generate_ics.py

from __future__ import annotations

import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from playwright.sync_api import sync_playwright

URL = "https://danskhaandbold.dk/tv-program"
OUT_PATH = Path("docs/tv-program.ics")
TZID = "Europe/Copenhagen"
SCRIPT_VERSION = "2026-01-15-tv-program-textparser-v2"
DEFAULT_DURATION_MIN = 90

# -----------------------------
# Models
# -----------------------------
@dataclass(frozen=True)
class MatchEvent:
    start: datetime
    end: datetime
    summary: str
    location: str
    description: str
    uid_seed: str


# -----------------------------
# ICS helpers
# -----------------------------
def ics_escape(text: str) -> str:
    text = (text or "")
    text = text.replace("\\", "\\\\")
    text = text.replace(";", r"\;")
    text = text.replace(",", r"\,")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\n", r"\n")
    return text


def ics_fold_line(line: str, limit: int = 75) -> str:
    if len(line) <= limit:
        return line
    out = []
    while len(line) > limit:
        out.append(line[:limit])
        line = " " + line[limit:]
    out.append(line)
    return "\r\n".join(out)


def dt_to_ics_local(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S")


def dt_to_ics_utc(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


def stable_uid(seed: str) -> str:
    return f"{uuid.uuid5(uuid.NAMESPACE_URL, seed)}@danskhaandbold.tvprogram"


def build_ics(events: List[MatchEvent]) -> str:
    now_utc = datetime.utcnow()

    lines = [
        "BEGIN:VCALENDAR",
        "PRODID:-//ChatGPT//Handball ICS//DA",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-SCRIPT-VERSION:{SCRIPT_VERSION}",
        "BEGIN:VTIMEZONE",
        f"TZID:{TZID}",
        f"X-LIC-LOCATION:{TZID}",
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
    ]

    for ev in events:
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{stable_uid(ev.uid_seed)}",
                f"DTSTAMP:{dt_to_ics_utc(now_utc)}",
                f"DTSTART;TZID={TZID}:{dt_to_ics_local(ev.start)}",
                f"DTEND;TZID={TZID}:{dt_to_ics_local(ev.end)}",
                f"SUMMARY:{ics_escape(ev.summary)}",
                f"LOCATION:{ics_escape(ev.location)}",
                f"DESCRIPTION:{ics_escape(ev.description)}",
                "END:VEVENT",
            ]
        )

    lines.append("END:VCALENDAR")

    folded = [ics_fold_line(ln) for ln in lines]
    return "\r\n".join(folded) + "\r\n"


# -----------------------------
# Parsing helpers
# -----------------------------
WEEKDAYS_DA = (
    "mandag",
    "tirsdag",
    "onsdag",
    "torsdag",
    "fredag",
    "lørdag",
    "søndag",
)

MONTHS_DA: Dict[str, int] = {
    "jan": 1,
    "januar": 1,
    "feb": 2,
    "februar": 2,
    "mar": 3,
    "marts": 3,
    "apr": 4,
    "april": 4,
    "maj": 5,
    "jun": 6,
    "juni": 6,
    "jul": 7,
    "juli": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "september": 9,
    "okt": 10,
    "oktober": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

DATE_HEADER_RE = re.compile(
    r"^(?P<wd>(" + "|".join(WEEKDAYS_DA) + r"))\s+"
    r"(?P<day>\d{1,2})\.\s*"
    r"(?P<mon>[A-Za-zæøåÆØÅ\.]+)\s*$",
    re.IGNORECASE,
)

# Accept:
#   "18:00"
#   "18.00"
#   "kl. 18:00"
#   "kl 18:00"
TIME_RE = re.compile(r"^(?:kl\.?\s*)?(?P<h>\d{1,2})[:\.](?P<m>\d{2})$", re.IGNORECASE)

# Channel detection (only if present in the extracted text)
CHANNEL_RE = re.compile(
    r"\b("
    r"TV\s?2(?:\s*Sport|\s*News|\s*Charlie|\s*Echo|\s*Zulu|\s*Play)?|"
    r"TV3(?:\s*Sport)?|"
    r"DR1|DR2|DR3|"
    r"Viaplay|Eurosport(?:\s*\d+)?|Sport Live|MAX|DAZN"
    r")\b",
    re.IGNORECASE,
)

MATCH_RE = re.compile(r".+\s-\s.+")


def normalize_channel(s: str) -> str:
    m = CHANNEL_RE.search(s or "")
    if not m:
        return ""
    ch = m.group(1).strip()
    ch = re.sub(r"\bTV\s+2\b", "TV2", ch, flags=re.IGNORECASE)
    ch = re.sub(r"\s{2,}", " ", ch)
    return ch


def infer_year(month: int) -> int:
    now = datetime.now()
    if now.month in (11, 12) and month in (1, 2, 3):
        return now.year + 1
    return now.year


def parse_date_header(line: str) -> Optional[Tuple[int, int, int]]:
    line = (line or "").strip().lower()
    m = DATE_HEADER_RE.match(line)
    if not m:
        return None
    day = int(m.group("day"))
    mon_raw = m.group("mon").strip().lower().rstrip(".")
    mon = MONTHS_DA.get(mon_raw)
    if not mon:
        return None
    year = infer_year(mon)
    return year, mon, day


def parse_time(line: str) -> Optional[Tuple[int, int]]:
    line = (line or "").strip()
    m = TIME_RE.match(line)
    if not m:
        return None
    return int(m.group("h")), int(m.group("m"))


def clean_line(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def make_event(
    y: int,
    mo: int,
    d: int,
    hh: int,
    mm: int,
    match_title: str,
    competition: str,
    channel: str,
    extras: List[str],
) -> MatchEvent:
    start = datetime(y, mo, d, hh, mm)
    end = start + timedelta(minutes=DEFAULT_DURATION_MIN)

    summary = clean_line(match_title)
    location = clean_line(channel)  # may be empty if not present on site

    desc_lines: List[str] = []
    if competition:
        desc_lines.append(clean_line(competition))

    for ex in extras:
        ex = clean_line(ex)
        if not ex:
            continue
        if ex in (summary, location, competition):
            continue
        desc_lines.append(ex)

    description = "\n".join(desc_lines)

    uid_seed = f"{y:04d}-{mo:02d}-{d:02d}|{hh:02d}:{mm:02d}|{summary}|{location}"
    return MatchEvent(start=start, end=end, summary=summary, location=location, description=description, uid_seed=uid_seed)


def parse_events_from_lines(lines: List[str]) -> List[MatchEvent]:
    events: List[MatchEvent] = []

    current_date: Optional[Tuple[int, int, int]] = None
    pending_time: Optional[Tuple[int, int]] = None
    pending_match: Optional[str] = None
    pending_comp: Optional[str] = None
    pending_channel: Optional[str] = None
    pending_extras: List[str] = []

    def flush():
        nonlocal pending_time, pending_match, pending_comp, pending_channel, pending_extras
        if current_date and pending_time and pending_match:
            y, mo, d = current_date
            hh, mm = pending_time
            ev = make_event(
                y, mo, d, hh, mm,
                pending_match,
                pending_comp or "",
                pending_channel or "",
                pending_extras,
            )
            events.append(ev)

        pending_time = None
        pending_match = None
        pending_comp = None
        pending_channel = None
        pending_extras = []

    for raw in lines:
        line = clean_line(raw)
        if not line:
            continue

        dh = parse_date_header(line)
        if dh:
            flush()
            current_date = dh
            continue

        t = parse_time(line)
        if t:
            flush()
            pending_time = t
            continue

        if not current_date or not pending_time:
            continue

        ch = normalize_channel(line)
        if ch:
            pending_channel = ch
            # Some pages might put channel on same “card”; if so, consider it complete.
            # But we still allow more lines after.
            continue

        if pending_match is None:
            if MATCH_RE.match(line):
                pending_match = line
                continue

            # Support two-line teams with "-" on its own (older pattern)
            if line == "-":
                pending_extras.append(line)
                continue
            if pending_extras and pending_extras[-1] == "-" and len(pending_extras) >= 2:
                left = pending_extras[-2]
                right = line
                pending_extras = pending_extras[:-2]
                pending_match = f"{left} - {right}"
                continue

            # Otherwise ignore until we hit a match line
            continue

        if pending_comp is None:
            # First non-match line after match is competition
            if line != pending_match:
                pending_comp = line
            continue

        pending_extras.append(line)

    flush()

    # de-dup
    uniq: Dict[str, MatchEvent] = {}
    for ev in events:
        uniq[ev.uid_seed] = ev
    out = list(uniq.values())
    out.sort(key=lambda e: (e.start, e.summary))
    return out


# -----------------------------
# Playwright fetch
# -----------------------------
def get_main_text() -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(URL, wait_until="domcontentloaded")

        page.wait_for_timeout(2500)
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1200)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(500)
        except Exception:
            pass

        try:
            txt = page.locator("main").inner_text(timeout=3000)
        except Exception:
            txt = page.locator("body").inner_text(timeout=3000)

        browser.close()
        return txt


def main() -> int:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    text = get_main_text()
    lines = [ln.strip() for ln in (text or "").splitlines()]

    events = parse_events_from_lines(lines)

    if not events:
        diag = "\n".join(lines[:150])
        print("ERROR: No events were parsed. Dumping first 150 lines of main text:\n", file=sys.stderr)
        print(diag, file=sys.stderr)
        return 2

    ics = build_ics(events)
    OUT_PATH.write_text(ics, encoding="utf-8")

    # If channels are missing from the source text, warn but do not fail.
    missing_loc = sum(1 for e in events if not e.location)
    if missing_loc:
        print(f"WARNING: {missing_loc}/{len(events)} events have empty LOCATION (channel not found in extracted text).")

    print(f"Generated {len(events)} events -> {OUT_PATH.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
