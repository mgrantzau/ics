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
SCRIPT_VERSION = "2026-01-15-dom-parser-v1"
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
# Date/time parsing
# -----------------------------
MONTHS_DA: Dict[str, int] = {
    "jan": 1, "januar": 1,
    "feb": 2, "februar": 2,
    "mar": 3, "marts": 3,
    "apr": 4, "april": 4,
    "maj": 5,
    "jun": 6, "juni": 6,
    "jul": 7, "juli": 7,
    "aug": 8, "august": 8,
    "sep": 9, "september": 9,
    "okt": 10, "oktober": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

DATE_RE = re.compile(r"^\s*(?:mandag|tirsdag|onsdag|torsdag|fredag|lørdag|søndag)\s+(\d{1,2})\.\s*([A-Za-zæøåÆØÅ\.]+)\s*$", re.IGNORECASE)
TIME_RE = re.compile(r"^\s*(?:kl\.?\s*)?(\d{1,2})[:\.](\d{2})\s*$", re.IGNORECASE)
ALT_CHANNEL_RE = re.compile(r"afspilles på\s+(.+)$", re.IGNORECASE)

def clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def infer_year(month: int) -> int:
    now = datetime.now()
    if now.month in (11, 12) and month in (1, 2, 3):
        return now.year + 1
    return now.year

def parse_date(date_str: str) -> Optional[Tuple[int, int, int]]:
    m = DATE_RE.match(clean(date_str))
    if not m:
        return None
    day = int(m.group(1))
    mon_raw = m.group(2).strip().lower().rstrip(".")
    mon = MONTHS_DA.get(mon_raw)
    if not mon:
        return None
    year = infer_year(mon)
    return year, mon, day

def parse_time(time_str: str) -> Optional[Tuple[int, int]]:
    m = TIME_RE.match(clean(time_str))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))

def parse_channel_from_alt(alt: str) -> str:
    alt = clean(alt)
    m = ALT_CHANNEL_RE.search(alt)
    if not m:
        return ""
    ch = clean(m.group(1))
    # normalize common variants
    ch = re.sub(r"\bTV\s+2\b", "TV2", ch, flags=re.IGNORECASE)
    return ch


# -----------------------------
# DOM extraction
# -----------------------------
def extract_rows(page) -> List[Dict[str, str]]:
    """
    Returns a list of dicts:
      date, time, match, comp, channel
    Pull from BOTH mobile and desktop layouts. We dedup later.
    """

    # Desktop rows: <div class="main-grid hidden ... md:grid"> ... </div>
    # Mobile cards:  <div class="flex flex-col ... md:hidden"> ... </div>
    # We'll look for containers that have both a date and time "kl." somewhere.

    js = r"""
    () => {
      const out = [];

      function text(el) { return (el && el.innerText ? el.innerText : "").trim(); }

      // Candidate blocks: each event is wrapped in a div with odd:bg-charcoal/5
      const blocks = Array.from(document.querySelectorAll("main .odd\\:bg-charcoal\\/5"));

      for (const block of blocks) {
        // Prefer desktop row if present
        const desktop = block.querySelector(".main-grid.md\\:grid");
        const mobile  = block.querySelector(".md\\:hidden");

        const node = desktop || mobile;
        if (!node) continue;

        // date/time: easiest is find first <p> that looks like weekday dd. mon.
        let dateStr = "";
        let timeStr = "";

        const ps = Array.from(node.querySelectorAll("p"));
        for (const p of ps) {
          const t = text(p);
          if (!dateStr && /(mandag|tirsdag|onsdag|torsdag|fredag|lørdag|søndag)\s+\d{1,2}\.\s*/i.test(t)) {
            dateStr = t;
            continue;
          }
          if (!timeStr && /(^|\b)kl\.?\s*\d{1,2}[:.]\d{2}\b/i.test(t)) {
            timeStr = t;
            continue;
          }
        }

        // match + competition:
        // desktop: inside a div: <p class="font-semibold">MATCH</p> <p class="text-sm">COMP</p>
        // mobile:  match is composed as "A - B" in a font-semibold row, comp is centered text-sm above
        let matchStr = "";
        let compStr = "";

        // desktop path
        const matchP = node.querySelector("p.font-semibold");
        if (matchP) matchStr = text(matchP);

        const compP = node.querySelector("p.text-sm");
        if (compP) compStr = text(compP);

        // channel from img alt="afspilles på ..."
        let channel = "";
        const img = node.querySelector('img[alt*="afspilles på"]');
        if (img && img.getAttribute("alt")) channel = img.getAttribute("alt").trim();

        out.push({ date: dateStr, time: timeStr, match: matchStr, comp: compStr, channel });
      }

      return out;
    }
    """
    return page.evaluate(js)


def make_event(y: int, mo: int, d: int, hh: int, mm: int, match_title: str, comp: str, channel: str) -> MatchEvent:
    start = datetime(y, mo, d, hh, mm)
    end = start + timedelta(minutes=DEFAULT_DURATION_MIN)

    summary = clean(match_title)
    description = clean(comp)

    location = parse_channel_from_alt(channel) if channel else ""
    uid_seed = f"{y:04d}-{mo:02d}-{d:02d}|{hh:02d}:{mm:02d}|{summary}|{location}"
    return MatchEvent(start=start, end=end, summary=summary, location=location, description=description, uid_seed=uid_seed)


def main() -> int:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(URL, wait_until="domcontentloaded")

        # allow hydration
        page.wait_for_timeout(2500)

        rows = extract_rows(page)
        browser.close()

    events: List[MatchEvent] = []
    for r in rows:
        d = parse_date(r.get("date", ""))
        t = parse_time(r.get("time", ""))
        match = clean(r.get("match", ""))
        comp = clean(r.get("comp", ""))
        channel = r.get("channel", "")

        if not d or not t or not match:
            continue

        y, mo, day = d
        hh, mm = t
        events.append(make_event(y, mo, day, hh, mm, match, comp, channel))

    # Dedup
    uniq: Dict[str, MatchEvent] = {}
    for ev in events:
        uniq[ev.uid_seed] = ev

    out = list(uniq.values())
    out.sort(key=lambda e: (e.start, e.summary))

    if not out:
        print("ERROR: No events were parsed. The site structure may have changed.", file=sys.stderr)
        # print a small diagnostic
        print(f"Rows extracted: {len(rows)}", file=sys.stderr)
        if rows:
            print("First extracted row:", rows[0], file=sys.stderr)
        return 2

    ics = build_ics(out)
    OUT_PATH.write_text(ics, encoding="utf-8")

    missing_loc = sum(1 for e in out if not e.location)
    if missing_loc:
        print(f"WARNING: {missing_loc}/{len(out)} events have empty LOCATION (no channel alt found).")

    print(f"Generated {len(out)} events -> {OUT_PATH.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
