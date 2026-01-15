#!/usr/bin/env python3
# generate_ics.py
#
# Generates docs/tv-program.ics from https://danskhaandbold.dk/tv-program
#
# Strategy (robust vs Nuxt changes):
# - Use Playwright to render the page (Nuxt/CSR).
# - Scroll to force lazy-loaded items to render.
# - Parse the DESKTOP rows (md:grid) which contain:
#     date, time, match title (home-away), competition, and channel icon/alt.
#
# Output:
# - SUMMARY: "<Home> - <Away>"
# - LOCATION: "<Channel name>" (from img alt, mapped)
# - DESCRIPTION: "<Competition>" (and optionally other notes)
#
# Exit codes:
# 2 = parsed 0 events (likely selector/site structure change)

from __future__ import annotations

import hashlib
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, date
from typing import List, Optional, Tuple

from playwright.sync_api import sync_playwright

# -----------------------------
# Config
# -----------------------------

SOURCE_URL = "https://danskhaandbold.dk/tv-program"
OUT_PATH = os.path.join("docs", "tv-program.ics")

TZID = "Europe/Copenhagen"
PRODID = "-//ChatGPT//Handball ICS//DA"
SCRIPT_VERSION = "2026-01-15-dom-parser-v2"

DEFAULT_DURATION_MIN = 90  # keep consistent with your existing feed

# Danish month abbreviations used on the site
DK_MONTHS = {
    "jan.": 1,
    "feb.": 2,
    "mar.": 3,
    "apr.": 4,
    "maj": 5,
    "jun.": 6,
    "jul.": 7,
    "aug.": 8,
    "sep.": 9,
    "okt.": 10,
    "nov.": 11,
    "dec.": 12,
}

# Map channel image alt text -> clean channel label for LOCATION
CHANNEL_MAP = {
    "afspilles på TV2": "TV2",
    "afspilles på TV2 Sport": "TV2 Sport",
    "afspilles på TV2 Sport X": "TV2 Sport X",
    "afspilles på TV2 Play": "TV2 Play",
    "afspilles på DR1": "DR1",
    "afspilles på DR2": "DR2",
    "afspilles på DR": "DR",
    "afspilles på TV3 Sport": "TV3 Sport",
    "afspilles på TV3": "TV3",
    "afspilles på Viaplay": "Viaplay",
}

# -----------------------------
# Data model
# -----------------------------

@dataclass
class TvEvent:
    start: datetime
    end: datetime
    summary: str
    location: str
    description: str
    uid: str


# -----------------------------
# Helpers
# -----------------------------

def ics_escape(text: str) -> str:
    """Escape per RFC5545 for TEXT fields."""
    if text is None:
        return ""
    text = text.replace("\\", "\\\\")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\n", "\\n")
    text = text.replace(",", "\\,")
    text = text.replace(";", "\\;")
    return text


def fold_ics_line(line: str, limit: int = 75) -> List[str]:
    """
    Fold lines to 75 octets (roughly chars for ASCII-ish content).
    Continuation lines start with a single space.
    """
    if len(line) <= limit:
        return [line]
    out = []
    while len(line) > limit:
        out.append(line[:limit])
        line = " " + line[limit:]
    out.append(line)
    return out


def make_uid(stamp_basis: str) -> str:
    """
    Stable UID from content basis, so updates replace existing items
    rather than creating duplicates (depends on calendar client).
    """
    h = hashlib.sha1(stamp_basis.encode("utf-8")).hexdigest()
    # Use a UUIDv5-like stable namespace feel
    stable = str(uuid.UUID(h[:32]))
    return f"{stable}@danskhaandbold.tvprogram"


def parse_danish_date(date_str: str, today: Optional[date] = None) -> date:
    """
    Input examples: "torsdag 15. jan."  / "fredag 16. jan."
    Year is not present; infer the nearest reasonable year.
    Rule:
      - Use current year, but if month/day already passed by > ~6 months,
        allow next year (handles Dec->Jan boundary).
    """
    if today is None:
        today = datetime.now().date()

    s = date_str.strip().lower()
    # Extract "15. jan." part
    m = re.search(r"(\d{1,2})\.\s*([a-zæøå]+\.)|(\d{1,2})\.\s*([a-zæøå]+)", s)
    if not m:
        # fallback: search for "15." and "jan."
        m2 = re.search(r"(\d{1,2})\.\s*([a-zæøå]+\.?)", s)
        if not m2:
            raise ValueError(f"Could not parse date from: {date_str}")
        day = int(m2.group(1))
        mon_key = m2.group(2)
    else:
        # handle different groupings
        day = int(m.group(1) or m.group(3))
        mon_key = (m.group(2) or m.group(4) or "").strip()

    if not mon_key.endswith(".") and mon_key not in DK_MONTHS:
        mon_key = mon_key + "."

    if mon_key not in DK_MONTHS:
        raise ValueError(f"Unknown month token '{mon_key}' in '{date_str}'")

    month = DK_MONTHS[mon_key]

    # Try current year then maybe next/prev boundary correction
    y = today.year
    candidate = date(y, month, day)

    # If candidate is far in the past relative to today, assume next year
    if candidate < today - timedelta(days=183):
        candidate = date(y + 1, month, day)
    # If candidate is far in the future relative to today, assume previous year
    elif candidate > today + timedelta(days=183):
        candidate = date(y - 1, month, day)

    return candidate


def parse_time_str(time_str: str) -> Tuple[int, int]:
    """
    Input example: "kl. 18:00"
    """
    s = time_str.strip().lower()
    m = re.search(r"(\d{1,2}):(\d{2})", s)
    if not m:
        raise ValueError(f"Could not parse time from: {time_str}")
    return int(m.group(1)), int(m.group(2))


def normalize_whitespace(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def clean_channel_from_alt(alt: str) -> str:
    alt = normalize_whitespace(alt)
    if alt in CHANNEL_MAP:
        return CHANNEL_MAP[alt]
    # fallback: try stripping prefix
    m = re.search(r"afspilles på\s+(.+)$", alt, flags=re.IGNORECASE)
    if m:
        return normalize_whitespace(m.group(1))
    return alt or ""


# -----------------------------
# DOM extraction (Playwright)
# -----------------------------

JS_EXTRACT = r"""
() => {
  // We parse only the desktop rows: ".main-grid.hidden.rounded.py-5.md:grid"
  // Each row contains:
  //  - date <p> (md:col-span-3)
  //  - time <p> (md:col-span-2 ... )
  //  - details <div> (md:col-span-5 ... ) with:
  //      <p class="font-semibold">MATCH</p>
  //      <p class="text-sm">COMPETITION</p>
  //  - channel <img alt="afspilles på ...">
  const rows = Array.from(document.querySelectorAll("main .main-grid.hidden.rounded.py-5.md\\:grid"));
  return rows.map(row => {
    const ps = Array.from(row.querySelectorAll("p"));
    // The first two <p> are typically date and time (based on layout)
    const dateText = ps[0]?.innerText?.trim() ?? "";
    const timeText = ps[1]?.innerText?.trim() ?? "";

    const details = row.querySelector("div.my-auto.md\\:col-span-5, div.my-auto.md\\:col-span-5.lg\\:col-span-4");
    let matchTitle = "";
    let competition = "";
    if (details) {
      const m = details.querySelector("p.font-semibold");
      const c = details.querySelector("p.text-sm");
      matchTitle = m?.innerText?.trim() ?? "";
      competition = c?.innerText?.trim() ?? "";
    }

    const img = row.querySelector("img[alt]");
    const alt = img?.getAttribute("alt") ?? "";
    return { dateText, timeText, matchTitle, competition, channelAlt: alt };
  });
}
"""


def scroll_to_load(page) -> None:
    """
    The tv-program page lazy-loads additional rows as you scroll.
    We scroll until the number of rendered desktop rows stops increasing.
    """
    page.wait_for_timeout(2500)

    last = 0
    stable = 0

    for _ in range(40):  # max scrolls
        count = page.evaluate(r"""
            () => document.querySelectorAll("main .main-grid.hidden.rounded.py-5.md\\:grid").length
        """)
        if count == last:
            stable += 1
        else:
            stable = 0
            last = count

        if stable >= 2:
            break

        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1500)


def extract_rows(page) -> List[dict]:
    rows = page.evaluate(JS_EXTRACT)
    # Filter obvious empties
    out = []
    for r in rows:
        if not r:
            continue
        if normalize_whitespace(r.get("matchTitle", "")) == "":
            continue
        out.append(r)
    return out


# -----------------------------
# Build events
# -----------------------------

def rows_to_events(rows: List[dict]) -> List[TvEvent]:
    events: List[TvEvent] = []
    today = datetime.now().date()

    for r in rows:
        date_text = normalize_whitespace(r.get("dateText", ""))
        time_text = normalize_whitespace(r.get("timeText", ""))
        match_title = normalize_whitespace(r.get("matchTitle", ""))
        competition = normalize_whitespace(r.get("competition", ""))
        channel_alt = normalize_whitespace(r.get("channelAlt", ""))

        # Parse datetime
        try:
            d = parse_danish_date(date_text, today=today)
            hh, mm = parse_time_str(time_text)
        except Exception:
            # Skip if parsing fails; keep script resilient
            continue

        start_dt = datetime(d.year, d.month, d.day, hh, mm)
        end_dt = start_dt + timedelta(minutes=DEFAULT_DURATION_MIN)

        # Channel label
        location = clean_channel_from_alt(channel_alt)

        # Ensure SUMMARY is always the match title (not the competition)
        summary = match_title

        # Keep all other info in DESCRIPTION
        description = competition

        # Stable UID basis
        uid_basis = f"{start_dt.isoformat()}|{summary}|{location}|{description}"
        uid = make_uid(uid_basis)

        events.append(
            TvEvent(
                start=start_dt,
                end=end_dt,
                summary=summary,
                location=location,
                description=description,
                uid=uid,
            )
        )

    # Deduplicate by UID (in case DOM contains duplicates)
    seen = set()
    uniq = []
    for e in events:
        if e.uid in seen:
            continue
        seen.add(e.uid)
        uniq.append(e)

    # Sort
    uniq.sort(key=lambda e: e.start)
    return uniq


# -----------------------------
# ICS writer
# -----------------------------

def format_dt(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S")


def build_ics(events: List[TvEvent]) -> str:
    now_utc = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    lines: List[str] = []
    lines.append("BEGIN:VCALENDAR")
    lines.append(f"PRODID:{PRODID}")
    lines.append("VERSION:2.0")
    lines.append("CALSCALE:GREGORIAN")
    lines.append("METHOD:PUBLISH")
    lines.append(f"X-SCRIPT-VERSION:{SCRIPT_VERSION}")

    # Timezone block (matches what you already have)
    lines.extend([
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
    ])

    for ev in events:
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{ev.uid}")
        lines.append(f"DTSTAMP:{now_utc}")
        lines.append(f"DTSTART;TZID={TZID}:{format_dt(ev.start)}")
        lines.append(f"DTEND;TZID={TZID}:{format_dt(ev.end)}")
        lines.append(f"SUMMARY:{ics_escape(ev.summary)}")
        if ev.location:
            lines.append(f"LOCATION:{ics_escape(ev.location)}")
        if ev.description:
            lines.append(f"DESCRIPTION:{ics_escape(ev.description)}")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")

    # Fold lines
    folded: List[str] = []
    for ln in lines:
        folded.extend(fold_ics_line(ln))
    return "\r\n".join(folded) + "\r\n"


# -----------------------------
# Main
# -----------------------------

def main() -> int:
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="da-DK",
            timezone_id=TZID,
        )
        page = context.new_page()
        page.goto(SOURCE_URL, wait_until="networkidle", timeout=120_000)

        # Ensure lazy-loaded items appear
        scroll_to_load(page)

        rows = extract_rows(page)

        browser.close()

    events = rows_to_events(rows)

    if not events:
        # Dump some text to help debugging in GitHub Actions logs
        # (kept short)
        print("ERROR: No events were parsed. The site structure may have changed.", file=sys.stderr)
        return 2

    ics_text = build_ics(events)

    with open(OUT_PATH, "w", encoding="utf-8", newline="") as f:
        f.write(ics_text)

    print(f"Generated {len(events)} events -> {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
