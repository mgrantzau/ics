# generate_ics.py
# Endelig stabil version:
# - JS-rendering via Playwright
# - Kun 1 event pr. kamp
# - LOCATION = kanal
# - DESCRIPTION = øvrig info

import os
import re
import uuid
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

URL = "https://danskhaandbold.dk/tv-program"
TZID = "Europe/Copenhagen"
DURATION_MIN = 90
SCRIPT_VERSION = "2025-12-15-playwright-final"

MONTHS = {
    "jan.": 1, "feb.": 2, "mar.": 3, "apr.": 4, "maj": 5, "jun.": 6,
    "jul.": 7, "aug.": 8, "sep.": 9, "okt.": 10, "nov.": 11, "dec.": 12
}

CHANNEL_RE = re.compile(
    r"(TV2\s*Sport|TV2\s*Play|TV2\b|DR1\b|DR2\b|TV3\s*Sport)",
    re.IGNORECASE
)

FOOTER_RE = re.compile(
    r"(Besøg Landsholdshoppen|CVR nummer|DanskHåndbold 2025|GDPR|Boozt\.com)",
    re.IGNORECASE
)

def norm(s: str) -> str:
    return " ".join(s.replace("\u00a0", " ").split()).strip()

def esc(s: str) -> str:
    return (s.replace("\\", "\\\\")
            .replace("\n", "\\n")
            .replace(",", "\\,")
            .replace(";", "\\;"))

def ics_dt(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S")

def uid(start: datetime, summary: str) -> str:
    key = f"{start.isoformat()}|{summary}"
    return f"{uuid.uuid5(uuid.NAMESPACE_URL, key)}@chatgpt.local"

def fetch_lines() -> list[str]:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(URL, wait_until="networkidle", timeout=60000)
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(1000)
        html = page.content()
        browser.close()

    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text("\n")
    return [norm(x) for x in text.splitlines() if norm(x)]

def extract_channel(block: list[str]) -> str:
    for line in block:
        if "afspilles" in line.lower():
            m = CHANNEL_RE.search(line)
            if m:
                return norm(m.group(1))
    for line in block:
        m = CHANNEL_RE.search(line)
        if m:
            return norm(m.group(1))
    return ""

def extract_match(block: list[str]) -> tuple[str, str]:
    # "Hold A - Hold B"
    for line in block:
        if re.search(r".+\s[–-]\s.+", line):
            notes = "\n".join(x for x in block if x != line)
            return line, notes

    # "Hold A", "-", "Hold B"
    for i in range(1, len(block) - 1):
        if block[i] == "-":
            summary = f"{block[i-1]} - {block[i+1]}"
            notes = "\n".join(
                block[j] for j in range(len(block))
                if j not in (i-1, i, i+1)
            )
            return summary, notes

    return "", "\n".join(block)

def main():
    lines = fetch_lines()

    date_re = re.compile(
        r"^(mandag|tirsdag|onsdag|torsdag|fredag|lørdag|søndag)\s+(\d{1,2})\.\s+([a-zæøå]+\.)$",
        re.IGNORECASE
    )
    time_re = re.compile(r"^kl\.\s*(\d{1,2}):(\d{2})$", re.IGNORECASE)

    events = {}
    current_date = None
    year = datetime.now().year
    last_month = None

    i = 0
    while i < len(lines):
        if date_re.match(lines[i]):
            _, day, mon = date_re.match(lines[i]).groups()
            month = MONTHS[mon.lower()]
            if last_month and month < last_month:
                year += 1
            last_month = month
            current_date = datetime(year, month, int(day))
            i += 1
            continue

        if current_date and time_re.match(lines[i]):
            h, m = map(int, time_re.match(lines[i]).groups())
            start = current_date.replace(hour=h, minute=m)
            end = start + timedelta(minutes=DURATION_MIN)

            block = []
            j = i + 1
            while j < len(lines):
                if date_re.match(lines[j]) or time_re.match(lines[j]):
                    break
                if FOOTER_RE.search(lines[j]):
                    break
                block.append(lines[j])
                j += 1

            summary, notes = extract_match(block)
            if summary:
                channel = extract_channel(block)
                events[(start, summary)] = (end, channel, notes)

            i = j
            continue

        i += 1

    out = [
        "BEGIN:VCALENDAR",
        "PRODID:-//ChatGPT//Handball ICS//DA",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-SCRIPT-VERSION:{SCRIPT_VERSION}",
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

    dtstamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    for (start, summary), (end, channel, notes) in sorted(events.items()):
        out.extend([
            "BEGIN:VEVENT",
            f"UID:{uid(start, summary)}",
            f"DTSTAMP:{dtstamp}",
            f"DTSTART;TZID={TZID}:{ics_dt(start)}",
            f"DTEND;TZID={TZID}:{ics_dt(end)}",
            f"SUMMARY:{esc(summary)}",
        ])
        if channel:
            out.append(f"LOCATION:{esc(channel)}")
        if notes:
            out.append(f"DESCRIPTION:{esc(notes)}")
        out.append("END:VEVENT")

    out.append("END:VCALENDAR")

    os.makedirs("docs", exist_ok=True)
    with open("docs/tv-program.ics", "w", encoding="utf-8") as f:
        f.write("\r\n".join(out) + "\r\n")

    print(f"[{SCRIPT_VERSION}] Generated {len(events)} events")

if __name__ == "__main__":
    main()
