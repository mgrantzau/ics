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

# --------------------
# HELPERS
# --------------------
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
        text.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


# --------------------
# SCRAPING
# --------------------
def scrape_events() -> List[Dict]:
    events = []
    year = datetime.now().year
    current_date: Optional[date] = None

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(URL, wait_until="networkidle")

        # allow hydration
        page.wait_for_timeout(2500)

        # scroll to load lazy content
        last_count = 0
        for _ in range(25):
            count = page.evaluate(
                "() => document.querySelectorAll('main .odd\\\\:bg-charcoal\\\\/5').length"
            )
            if count == last_count:
                break
            last_count = count
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1200)

        rows = page.query_selector_all("main .odd\\:bg-charcoal\\/5")
        browser.close()

    for row in rows:
        lines = [l.strip() for l in row.inner_text().split("\n") if l.strip()]

        for line in lines:
            d = parse_date_line(line, year)
            if d:
                current_date = d
                continue

            t = parse_time_line(line)
            if t and current_date:
                hour, minute = t
                continue

            if "-" in line and current_date and t:
                summary = line
                start_dt = datetime(
                    current_date.year,
                    current_date.month,
                    current_date.day,
                    hour,
                    minute,
                )
                end_dt = start_dt + timedelta(minutes=DEFAULT_DURATION_MIN)

                # try to find channel + competition
                channel = ""
                description = ""

                idx = lines.index(line)
                if idx + 1 < len(lines):
                    description = lines[idx + 1]
                if idx + 2 < len(lines):
                    channel = lines[idx + 2]

                events.append(
                    {
                        "summary": summary,
                        "start": start_dt,
                        "end": end_dt,
                        "location": channel,
                        "description": description,
                    }
                )

    if not events:
        raise RuntimeError("No events parsed – site structure may have changed")

    return events


# --------------------
# ICS WRITER
# --------------------
def write_ics(events: List[Dict]):
    now_utc = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "PRODID:-//ChatGPT//Handball ICS//DA",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-SCRIPT-VERSION:2026-01-15-dom-parser-final",
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
        # STABLE UID: start time + summary ONLY
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


# --------------------
# MAIN
# --------------------
if __name__ == "__main__":
    events = scrape_events()
    write_ics(events)
    print(f"Generated {len(events)} events → {OUT_FILE}")
