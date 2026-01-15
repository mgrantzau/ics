#!/usr/bin/env python3
# generate_ics.py
#
# Scrapes https://danskhaandbold.dk/tv-program (rendered) and generates docs/tv-program.ics
# Output rules:
#   - SUMMARY    = "Hold A - Hold B"  (or best possible fallback)
#   - LOCATION   = TV-kanal (fx "TV2 Sport", "DR2")
#   - DESCRIPTION= Turnering/round + evt. ekstra info
#
# Robust against the common formatting issue where competition/round ends up as SUMMARY.

from __future__ import annotations

import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


URL = "https://danskhaandbold.dk/tv-program"
OUT_PATH = Path("docs/tv-program.ics")
TZID = "Europe/Copenhagen"
SCRIPT_VERSION = "2026-01-15-tv-program-final-title-fix"


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
# Helpers: text & ICS
# -----------------------------
def _strip(s: str) -> str:
    return re.sub(r"[ \t]+", " ", (s or "").strip())


def ics_escape(text: str) -> str:
    # RFC5545 escaping for TEXT
    # Backslash, semicolon, comma, newline
    text = text.replace("\\", "\\\\")
    text = text.replace(";", r"\;")
    text = text.replace(",", r"\,")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\n", r"\n")
    return text


def ics_fold_line(line: str, limit: int = 75) -> str:
    # Fold lines at 75 octets; we approximate by characters (OK for ASCII-heavy content).
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
    # We write DTSTAMP in UTC. We don't convert timezone-aware here; just treat as UTC timestamp.
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
        uid = stable_uid(ev.uid_seed)
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"DTSTAMP:{dt_to_ics_utc(now_utc)}",
                f"DTSTART;TZID={TZID}:{dt_to_ics_local(ev.start)}",
                f"DTEND;TZID={TZID}:{dt_to_ics_local(ev.end)}",
                f"SUMMARY:{ics_escape(ev.summary)}",
                f"LOCATION:{ics_escape(ev.location)}" if ev.location else "LOCATION:",
                f"DESCRIPTION:{ics_escape(ev.description)}" if ev.description else "DESCRIPTION:",
                "END:VEVENT",
            ]
        )

    lines.append("END:VCALENDAR")

    # Fold
    folded = []
    for ln in lines:
        folded.append(ics_fold_line(ln))
    return "\r\n".join(folded) + "\r\n"


# -----------------------------
# Parsing logic
# -----------------------------
CHANNEL_RE = re.compile(
    r"\b("
    r"TV\s?2(?:\s*Sport|\s*News|\s*Charlie|\s*Echo|\s*Zulu)?|"
    r"TV3(?:\s*Sport)?|"
    r"DR1|DR2|DR\s?Ramasjang|DR\s?TV|"
    r"Eurosport(?:\s*\d+)?|"
    r"Viaplay|"
    r"Sport Live|"
    r"MAX|"
    r"Discovery\+|"
    r"DAZN"
    r")\b",
    re.IGNORECASE,
)

TIME_RE = re.compile(r"\b(\d{1,2})\.(\d{2})\b")  # "18.00"
DATE_RE = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b")  # "16/1/2026" or "16-01-2026"
ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")  # "2026-01-16"


def normalize_channel(text: str) -> str:
    m = CHANNEL_RE.search(text)
    if not m:
        return ""
    ch = m.group(1).strip()
    # Simple normalization: TV2 -> TV2, TV 2 -> TV2
    ch = re.sub(r"\bTV\s+2\b", "TV2", ch, flags=re.IGNORECASE)
    ch = re.sub(r"\s{2,}", " ", ch)
    return ch


def parse_datetime_from_text(blob: str) -> Tuple[Optional[datetime], Optional[str]]:
    """
    Tries to find a date+time in Danish-ish formats.
    Returns (datetime, date_key_str) where date_key_str is YYYY-MM-DD used for UID seeding.
    """
    blob = blob.replace("\u00a0", " ")

    # Date (prefer ISO-like)
    ymd = None
    m = ISO_DATE_RE.search(blob)
    if m:
        y, mo, d = map(int, m.groups())
        ymd = (y, mo, d)
    else:
        m = DATE_RE.search(blob)
        if m:
            d, mo, y = m.groups()
            d = int(d)
            mo = int(mo)
            y = int(y)
            if y < 100:
                y += 2000
            ymd = (y, mo, d)

    # Time
    tm = None
    m = TIME_RE.search(blob)
    if m:
        hh, mm = m.groups()
        tm = (int(hh), int(mm))

    if not ymd or not tm:
        return None, None

    dt = datetime(ymd[0], ymd[1], ymd[2], tm[0], tm[1])
    date_key = f"{ymd[0]:04d}-{ymd[1]:02d}-{ymd[2]:02d}"
    return dt, date_key


def extract_teams_and_competition(lines: List[str]) -> Tuple[str, str]:
    """
    Given text lines from a single program item, decide:
      - teams string for SUMMARY (preferred)
      - competition/round for DESCRIPTION
    Heuristics:
      - Team line often contains " - " OR is two lines with a "-" line between.
      - Competition is usually the remaining 'heading' line(s).
    """
    cleaned = [ln for ln in (_strip(x) for x in lines) if ln]
    if not cleaned:
        return "", ""

    # Remove obvious noise fragments
    noise_prefixes = (
        "Vises på",
        "Kanal",
        "TV",
        "all-day",
        "starts",
        "ends",
    )
    cleaned = [ln for ln in cleaned if not any(ln.lower().startswith(p.lower()) for p in noise_prefixes)]

    teams = ""
    comp = ""

    # 1) One-line teams "A - B"
    for ln in cleaned:
        if " - " in ln and len(ln.split(" - ", 1)[0]) >= 2 and len(ln.split(" - ", 1)[1]) >= 2:
            # Guard against "Taber semifinale 1 - Taber semifinale 2" (still valid teams)
            teams = ln
            break

    # 2) Three-line pattern: A, "-", B
    if not teams:
        for i in range(len(cleaned) - 2):
            if cleaned[i + 1] == "-" and cleaned[i] and cleaned[i + 2]:
                teams = f"{cleaned[i]} - {cleaned[i + 2]}"
                break

    # 3) If we didn't find teams but we have two adjacent likely team lines, use them
    if not teams:
        # pick first two non-channel, non-time lines as fallback
        candidates = []
        for ln in cleaned:
            if CHANNEL_RE.search(ln):
                continue
            if TIME_RE.search(ln) and len(ln) <= 5:
                continue
            if DATE_RE.search(ln) or ISO_DATE_RE.search(ln):
                continue
            candidates.append(ln)
        if len(candidates) >= 2:
            teams = f"{candidates[0]} - {candidates[1]}"

    # Competition: choose a line that is NOT teams and not channel/time/date
    def is_meta(ln: str) -> bool:
        return bool(CHANNEL_RE.search(ln) or DATE_RE.search(ln) or ISO_DATE_RE.search(ln) or (TIME_RE.search(ln) and len(ln) <= 5))

    comp_candidates = []
    for ln in cleaned:
        if is_meta(ln):
            continue
        if teams and ln == teams:
            continue
        # avoid adding parts already in teams
        if teams and ln in teams:
            continue
        comp_candidates.append(ln)

    # If teams exist, comp is first candidate that doesn't look like a team line
    if teams:
        # Prefer something with commas / "runde" / "liga" / "EM" / "VM" etc.
        prefer_re = re.compile(r"\b(runde|liga|lig|turnering|final|semi|kvart|champions|europe|em|vm)\b", re.IGNORECASE)
        preferred = [c for c in comp_candidates if prefer_re.search(c)]
        comp = preferred[0] if preferred else (comp_candidates[0] if comp_candidates else "")
    else:
        # No teams: treat first non-meta as summary and rest as description
        comp = comp_candidates[0] if comp_candidates else ""

    return _strip(teams), _strip(comp)


def clean_summary(summary: str, competition: str) -> Tuple[str, str]:
    """
    Final correction:
    If SUMMARY looks like a competition and DESCRIPTION looks like teams, swap them.
    """
    s = _strip(summary)
    c = _strip(competition)

    # Competition-like patterns
    compy = re.compile(r"\b(EM|VM|liga|final|runde|turnering|champions|gruppespil|qual|kval|pokal)\b", re.IGNORECASE)
    teamy = re.compile(r".+ - .+")

    if compy.search(s) and teamy.match(c):
        return c, s

    return s, c


def default_end_time(start: datetime) -> datetime:
    # Use 90 minutes default, matches what you have been using.
    return start + timedelta(minutes=90)


# -----------------------------
# Scrape with Playwright
# -----------------------------
def scrape_program_text_blocks() -> List[str]:
    """
    Returns a list of text blocks, one per TV-program item.
    We do not rely on brittle CSS class names; instead we scan for repeating card-like elements.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(URL, wait_until="domcontentloaded")

        # Try to wait for something meaningful on the page.
        try:
            page.wait_for_timeout(1500)
        except PlaywrightTimeoutError:
            pass

        # Some sites load content asynchronously; give it a little more time.
        page.wait_for_timeout(1500)

        # Extract candidate blocks:
        # We pick elements with substantial text and containing a channel or time.
        blocks: List[str] = page.evaluate(
            """
            () => {
              const elements = Array.from(document.querySelectorAll("main, section, article, div, li"));
              const seen = new Set();
              const out = [];

              function norm(s){ return (s||"").replace(/\\s+/g," ").trim(); }

              for (const el of elements) {
                const txt = norm(el.innerText);
                if (!txt) continue;

                // Heuristic: item must contain time like 18.00 or 20.45 AND some known channel token
                const hasTime = /\\b\\d{1,2}\\.\\d{2}\\b/.test(txt);
                const hasChannel = /\\b(TV\\s?2|TV3|DR1|DR2|Viaplay|Eurosport|Sport Live|MAX|DAZN)\\b/i.test(txt);

                // Keep medium blocks (avoid huge footers)
                if ((hasTime || hasChannel) && txt.length >= 20 && txt.length <= 400) {
                  // de-dup by exact text
                  if (!seen.has(txt)) {
                    seen.add(txt);
                    out.push(txt);
                  }
                }
              }

              return out;
            }
            """
        )

        browser.close()

    return [b for b in blocks if b and len(b) < 800]


def build_events_from_blocks(blocks: Iterable[str]) -> List[MatchEvent]:
    events: List[MatchEvent] = []
    for block in blocks:
        # Split into lines to do better extraction
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]

        # Date+time
        start_dt, date_key = parse_datetime_from_text(block)
        if not start_dt or not date_key:
            # If we can't parse date/time, skip
            continue

        # Channel
        channel = normalize_channel(block)

        # Teams + competition
        teams, competition = extract_teams_and_competition(lines)

        # If teams weren't found, fallback to a safer summary
        summary = teams if teams else (competition if competition else "Håndboldkamp")

        # Now apply swap-fix if needed
        summary, competition = clean_summary(summary, competition)

        # Description: keep competition + any extra lines that aren't duplicative
        # If competition is empty but we have leftover, keep something.
        description = competition

        # Add extra detail lines (excluding duplicates and meta)
        extra = []
        for ln in lines:
            ln = _strip(ln)
            if not ln:
                continue
            if ln == summary or ln == competition:
                continue
            if CHANNEL_RE.search(ln):
                continue
            if DATE_RE.search(ln) or ISO_DATE_RE.search(ln):
                continue
            if TIME_RE.search(ln) and len(ln) <= 5:
                continue
            # Avoid adding team fragments already present
            if summary and ln in summary:
                continue
            extra.append(ln)

        # Keep description compact and stable
        if extra:
            if description:
                description = description + "\n" + "\n".join(extra[:4])
            else:
                description = "\n".join(extra[:4])

        # End time
        end_dt = default_end_time(start_dt)

        # UID seed should be stable across runs for same match
        uid_seed = f"{date_key}|{start_dt.strftime('%H:%M')}|{summary}|{channel}"

        events.append(
            MatchEvent(
                start=start_dt,
                end=end_dt,
                summary=summary,
                location=channel,
                description=description,
                uid_seed=uid_seed,
            )
        )

    # Deduplicate by uid_seed (in case scraping returns same block twice)
    unique = {}
    for ev in events:
        unique[ev.uid_seed] = ev
    events = list(unique.values())

    # Sort
    events.sort(key=lambda e: (e.start, e.summary))
    return events


def main() -> int:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    blocks = scrape_program_text_blocks()
    events = build_events_from_blocks(blocks)

    if not events:
        print("ERROR: No events were parsed. The site structure may have changed.", file=sys.stderr)
        return 2

    ics = build_ics(events)
    OUT_PATH.write_text(ics, encoding="utf-8")

    print(f"Generated {len(events)} events -> {OUT_PATH.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
