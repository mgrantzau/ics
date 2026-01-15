import re
import uuid
from datetime import datetime, timedelta, date
from typing import Optional, List, Dict

from playwright.sync_api import sync_playwright

# --------------------
# CONFIG
# --------------------
URL = "https://danskhaandbold.dk/tv-program"
OUT_FILE = "docs/tv-program.ics"
TZ = "Europe/Copenhagen"
DEFAULT_DURATION_MIN = 90

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "maj": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dec": 12,
}

DATE_RE = re.compile(r"(\d{1,2})\.\s*([a-zæøå]+)", re.IGNORECASE)
TIME_RE = re.compile(r"kl\.\s*(\d{1,2}):(\d{2})", re.IGNORECASE)


def parse_date_line(line: str, assumed_year: int) -> Optional[date]:
    m = DATE_RE.search(line.lower())
    if not m:
        return None
    day = int(m.group(1))
    month = MONTHS.get(m.group(2)[:3])
    if not month:
        return None
    return date(assumed_year, month, day)


def parse_time_line(line: str) -> Optional[tuple[int, int]]:
    m = TIME_RE.search(line.lower())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def ics_escape(text: str) -> str:
    return (
        (text or "")
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


def scrape_event_blocks_text() -> List[str]:
    """
    Returns a list of raw text blocks; each block corresponds to one program card.
    IMPORTANT: all extraction happens while Playwright is still running.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(URL, wait_until="networkidle")

        # allow client-side hydration
        page.wait_for_timeout(2500)

        # scroll to trigger lazy-loading
        last_count = -1
        for _ in range(30):
            count = page.evaluate(
                "() => document.querySelectorAll('main .odd\\\\:bg-charcoal\\\\/5').length"
            )
            if count == last_count:
                break
            last_count = count
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1200)

        # Extract *strings* (not element handles) before closing
        blocks: List[str] = page.eval_on_selector_all(
            "main .odd\\:bg-charcoal\\/5",
            "els => els.map(e => e.innerText)"
        )

        browser.close()
        return blocks


def parse_blocks_to_events(blocks: List[str]) -> List[Dict]:
    events: List[Dict] = []
    year = datetime.now().year

    for block in blocks:
        lines = [l.strip() for l in block.split("\n") if l.strip()]

        current_date: Optional[date] = None
        current_time: Optional[tuple[int, int]] = None

        # The cards typically contain:
        #  - weekday + date line (e.g. "torsdag 15. jan.")
        #  - time line (e.g. "kl. 18:00")
        #  - match line (e.g. "Spanien - Serbien")
        #  - tournament line (e.g. "EM ...")
        #  - channel line (e.g. "TV2 Sport")
        #
        # But to be resilient, we scan sequentially.
        for i, line in enumerate(lines):
            d = parse_date_line(line, year)
            if d:
                current_date = d
                continue

            t = parse_time_line(line)
            if t:
                current_time = t
                continue

            # Identify match line by presence of " - " (with spaces) to reduce false positives
            if " - " in line and current_date and current_time:
                summary = line

                # Next non-empty line = tournament/competition (best effort)
                description = ""
                location = ""

                if i + 1 < len(lines):
                    description = lines[i + 1].strip()

                # Channel often after description
                if i + 2 < len(lines):
                    location = lines[i + 2].strip()

                start_dt = datetime(
                    current_date.year,
                    current_date.month,
                    current_date.day,
                    current_time[0],
                    current_time[1],
                )
                end_dt = start_dt + timedelta(minutes=DEFAULT_DURATION_MIN)

                events.append(
                    {
                        "summary": summary,
                        "start": start_dt,
                        "end": end_dt,
                        "location": location,
                        "description": description,
                    }
                )

                # reset time so we don't accidentally reuse it if structure is weird
                current_time = None

    if not events:
        raise RuntimeError("No events were parsed. The site structure may have changed.")

    return events


def write_ics(events: List[Dict]) -> None:
    now_utc = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "PRODID:-//ChatGPT//Handball ICS//DA",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-SCRIPT-VERSION:2026-01-15-dom-parser-final-v2",
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
    ]

    for e in events:
        # Stable UID: start time + match title only
        uid_basis = f"{e['start'].isoformat()}|{e['summary']}"
        uid = uuid.uuid5(uuid.NAMESPACE_URL, uid_basis)

        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{uid}@danskhaandbold.tvprogram",
                f"DTSTAMP:{now_utc}",
                f"DTSTART;TZID={TZ}:{e['start'].strftime('%Y%m%dT%H%M%S')}",
                f"DTEND;TZID={TZ}:{e['end'].strftime('%Y%m%dT%H%M%S')}",
                f"SUMMARY:{ics_escape(e['summary'])}",
                f"LOCATION:{ics_escape(e['location'])}",
                f"DESCRIPTION:{ics_escape(e['description'])}",
                "END:VEVENT",
            ]
        )

    lines.append("END:VCALENDAR")

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    blocks = scrape_event_blocks_text()
    events = parse_blocks_to_events(blocks)
    write_ics(events)
    print(f"Generated {len(events)} events → {OUT_FILE}")
