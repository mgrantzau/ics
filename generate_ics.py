# generate_ics.py
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

URL = "https://danskhaandbold.dk/tv-program"
TZID = "Europe/Copenhagen"
DURATION_MIN = 90

MONTHS = {
    "jan.": 1, "feb.": 2, "mar.": 3, "apr.": 4, "maj": 5, "jun.": 6,
    "jul.": 7, "aug.": 8, "sep.": 9, "okt.": 10, "nov.": 11, "dec.": 12
}

def ics_dt(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S")

def esc(s: str) -> str:
    # RFC5545 escaping
    return (s.replace("\\", "\\\\")
             .replace("\r\n", "\n")
             .replace("\r", "\n")
             .replace("\n", "\\n")
             .replace(",", "\\,")
             .replace(";", "\\;"))

def main():
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; mgrantzau-ics/1.0; +https://github.com/mgrantzau/ics)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "da,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    r = requests.get(URL, headers=headers, timeout=45)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text("\n")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # Example: "tirsdag 16. dec."
    date_re = re.compile(
        r"^(mandag|tirsdag|onsdag|torsdag|fredag|lørdag|søndag)\s+(\d{1,2})\.\s+([a-zæøå]+\.)$",
        re.IGNORECASE
    )
    # Example: "kl. 18:30"
    time_re = re.compile(r"^kl\.\s*(\d{1,2}):(\d{2})$", re.IGNORECASE)
    # Example: "afspilles på TV2 Sport"
    on_re = re.compile(r"^afspilles på\s+(.+)$", re.IGNORECASE)

    # Kamp-linje (tolerant): "Hold A - Hold B" eller "Hold A – Hold B"
    match_re = re.compile(r".+\s[–-]\s.+")

    events = []
    current_date = None

    # Year rollover heuristic (dec -> jan)
    now = datetime.now()
    year_hint = now.year
    last_month = None

    i = 0
    while i < len(lines):
        mdate = date_re.match(lines[i])
        if mdate:
            day = int(mdate.group(2))
            mon_abbr = mdate.group(3).lower()
            mon = MONTHS.get(mon_abbr)
            if mon is None:
                i += 1
                continue

            if last_month is not None and mon < last_month:
                year_hint += 1
            last_month = mon

            current_date = datetime(year_hint, mon, day)
            i += 1
            continue

        mtime = time_re.match(lines[i])
        if current_date and mtime:
            hh = int(mtime.group(1))
            mm = int(mtime.group(2))

            start = datetime(current_date.year, current_date.month, current_date.day, hh, mm, 0)
            end = start + timedelta(minutes=DURATION_MIN)

            summary = ""
            location = ""
            notes_parts = []

            # Scan frem til næste dato/tid og find:
            # - første kamp-linje => SUMMARY
            # - "afspilles på ..." => LOCATION
            j = i + 1
            while j < len(lines):
                if date_re.match(lines[j]) or time_re.match(lines[j]):
                    break

                mon_line = on_re.match(lines[j])
                if mon_line:
                    location = mon_line.group(1).strip()
                    j += 1
                    break

                if (not summary) and match_re.match(lines[j]):
                    summary = lines[j]
                else:
                    notes_parts.append(lines[j])

                j += 1

            notes = "\n".join([p for p in notes_parts if p]).strip()

            # Kun opret event hvis vi fandt en kamp-linje
            if summary:
                events.append((start, end, summary, location, notes))

            # Spring forbi hele blokken vi lige har behandlet
            i = j
            continue

        i += 1

    dtstamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    vtimezone = """BEGIN:VTIMEZONE
TZID:Europe/Copenhagen
X-LIC-LOCATION:Europe/Copenhagen
BEGIN:DAYLIGHT
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
TZNAME:CEST
DTSTART:19700329T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
TZNAME:CET
DTSTART:19701025T030000
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
END:STANDARD
END:VTIMEZONE
"""

    out = []
    out.append("BEGIN:VCALENDAR")
    out.append("PRODID:-//ChatGPT//Handball ICS//DA")
    out.append("VERSION:2.0")
    out.append("CALSCALE:GREGORIAN")
    out.append("METHOD:PUBLISH")
    out.append(vtimezone.strip())

    for start, end, summary, location, notes in events:
        out.append("BEGIN:VEVENT")
        out.append(f"UID:{uuid.uuid4()}@chatgpt.local")
        out.append(f"DTSTAMP:{dtstamp}")
        out.append(f"DTSTART;TZID={TZID}:{ics_dt(start)}")
        out.append(f"DTEND;TZID={TZID}:{ics_dt(end)}")
        out.append(f"SUMMARY:{esc(summary)}")
        if location:
            out.append(f"LOCATION:{esc(location)}")
        if notes:
            out.append(f"DESCRIPTION:{esc(notes)}")
        out.append("END:VEVENT")

    out.append("END:VCALENDAR")
    ics = "\r\n".join(out) + "\r\n"

    os.makedirs("docs", exist_ok=True)
    with open("docs/tv-program.ics", "w", encoding="utf-8", newline="") as f:
        f.write(ics)

    print(f"Generated {len(events)} events -> docs/tv-program.ics")

if __name__ == "__main__":
    main()
