# generate_ics.py
# Fix v3:
# - Ingen dubletter: turneringslinjer med "-" bliver ikke behandlet som kamp
# - LOCATION findes via både tekst og HTML-fallback nær tidspunktet (Playwright-render)
# - DESCRIPTION = øvrige info

import os
import re
import uuid
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

URL = "https://danskhaandbold.dk/tv-program"
TZID = "Europe/Copenhagen"
DURATION_MIN = 90
SCRIPT_VERSION = "2025-12-15-playwright-final-v3"

MONTHS = {
    "jan.": 1, "feb.": 2, "mar.": 3, "apr.": 4, "maj": 5, "jun.": 6,
    "jul.": 7, "aug.": 8, "sep.": 9, "okt.": 10, "nov.": 11, "dec.": 12
}

DATE_RE = re.compile(
    r"^(mandag|tirsdag|onsdag|torsdag|fredag|lørdag|søndag)\s+(\d{1,2})\.\s+([a-zæøå]+\.)$",
    re.IGNORECASE
)
TIME_RE = re.compile(r"^kl\.\s*(\d{1,2}):(\d{2})$", re.IGNORECASE)

CHANNEL_RE = re.compile(
    r"(TV2\s*Sport|TV2\s*Play|TV2\b|DR1\b|DR2\b|TV3\s*Sport)",
    re.IGNORECASE
)

# Linjer der typisk er turnering/liga og IKKE et kampnavn
COMPETITION_RE = re.compile(
    r"(Pokalturnering|Bambuni|Champions League|Golden League|Final4|gruppespil|Herrer|Kvinder)",
    re.IGNORECASE
)

# Kampnavne er sjældent fyldt med tal/år eller 1/4 osv.
BAD_MATCH_TOKENS_RE = re.compile(r"(\b20\d{2}\b|\d+/\d+|\b1/2\b|\b1/4\b|\b1/8\b)", re.IGNORECASE)

FOOTER_STOP_RE = re.compile(
    r"(Besøg Landsholdshoppen|CVR nummer|danskhaandbold@danskhaandbold\.dk|DanskHåndbold 2025|GDPR|Boozt\.com)",
    re.IGNORECASE
)

def norm(s: str) -> str:
    return " ".join(s.replace("\u00a0", " ").split()).strip()

def esc(s: str) -> str:
    return (s.replace("\\", "\\\\")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .replace("\n", "\\n")
            .replace(",", "\\,")
            .replace(";", "\\;"))

def ics_dt(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S")

def stable_uid(start: datetime, summary: str) -> str:
    key = f"{start.strftime('%Y%m%dT%H%M')};{summary.strip()}"
    return f"{uuid.uuid5(uuid.NAMESPACE_URL, key)}@chatgpt.local"

def fetch_rendered_html_and_lines() -> tuple[str, list[str]]:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(URL, wait_until="networkidle", timeout=60000)

        # hjælper ved lazy-load
        page.mouse.wheel(0, 5000)
        page.wait_for_timeout(800)
        page.mouse.wheel(0, 5000)
        page.wait_for_timeout(800)

        html = page.content()
        browser.close()

    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text("\n")
    lines = [norm(ln) for ln in text.splitlines() if norm(ln)]
    return html, lines

def looks_like_real_match_line(line: str) -> bool:
    """
    Sand hvis linjen ligner "Hold A - Hold B" (eller med –),
    og ikke ligner turnering/liga (med år, 1/4-finaler osv.).
    """
    if not re.search(r".+\s[–-]\s.+", line):
        return False
    if COMPETITION_RE.search(line):
        return False
    if BAD_MATCH_TOKENS_RE.search(line):
        return False
    return True

def extract_channel_from_block(block: list[str]) -> str:
    # 1) "afspilles på <...>" samme linje
    for ln in block:
        m = re.search(r"afspilles\s*p[åa]\s*(.+)$", ln, flags=re.IGNORECASE)
        if m:
            m2 = CHANNEL_RE.search(m.group(1))
            if m2:
                return norm(m2.group(1))

    # 2) split: "afspilles", "på", "<kanal>"
    for i, ln in enumerate(block):
        low = ln.lower()
        if low == "afspilles":
            if i + 1 < len(block) and block[i + 1].lower() in ("på", "pa"):
                if i + 2 < len(block):
                    m = CHANNEL_RE.search(block[i + 2])
                    if m:
                        return norm(m.group(1))

        if low in ("afspilles på", "afspilles pa"):
            if i + 1 < len(block):
                m = CHANNEL_RE.search(block[i + 1])
                if m:
                    return norm(m.group(1))

    # 3) fallback: kanal hvor som helst i blokken
    for ln in block:
        m = CHANNEL_RE.search(ln)
        if m:
            return norm(m.group(1))

    return ""

def extract_channel_from_html_near_time(rendered_html: str, hh: int, mm: int) -> str:
    """
    Fallback: find kanal i HTML tæt på tidspunktet HH:MM.
    Det fanger cases hvor kanal ikke kommer med i soup.get_text().
    """
    anchor = f"{hh:02d}:{mm:02d}"
    pos = rendered_html.find(anchor)
    if pos == -1:
        return ""
    window = rendered_html[pos:pos + 25000]
    m = CHANNEL_RE.search(window)
    return norm(m.group(1)) if m else ""

def extract_match_summary_and_notes(block: list[str]) -> tuple[str, str]:
    """
    Returnerer (SUMMARY, NOTES).
    SUMMARY oprettes kun hvis vi kan finde et rigtigt kampnavn.
    """
    summary = ""

    # 1) én-linjers kamp
    for ln in block:
        if looks_like_real_match_line(ln):
            summary = ln
            break

    used_three = set()

    # 2) tre-linjers kamp: team, "-", team
    if not summary:
        for i in range(1, len(block) - 1):
            if block[i] == "-" and block[i - 1] and block[i + 1]:
                candidate = f"{block[i - 1]} - {block[i + 1]}"
                if looks_like_real_match_line(candidate):
                    summary = candidate
                    used_three = {i - 1, i, i + 1}
                    break

    if not summary:
        return "", "\n".join(block).strip()

    # NOTES: alt andet end selve match-navnet (+ fjern "afspilles/på/kanal")
    channel = extract_channel_from_block(block)
    skip_set = {"afspilles", "på", "pa", "afspilles på", "afspilles pa"}

    notes_parts = []
    for idx, ln in enumerate(block):
        if ln == summary:
            continue
        if idx in used_three:
            continue
        if ln.lower() in skip_set:
            continue
        if channel and norm(ln).lower() == channel.lower():
            continue
        notes_parts.append(ln)

    notes = "\n".join([x for x in notes_parts if x]).strip()
    return summary, notes

def build_ics(events: list[tuple[datetime, datetime, str, str, str]]) -> str:
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
    out.append(f"X-SCRIPT-VERSION:{SCRIPT_VERSION}")
    out.append(vtimezone.strip())

    for start, end, summary, location, notes in events:
        out.append("BEGIN:VEVENT")
        out.append(f"UID:{stable_uid(start, summary)}")
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
    return "\r\n".join(out) + "\r\n"

def main():
    html, lines = fetch_rendered_html_and_lines()

    events_map = {}  # (start, summary) -> (end, location, notes)
    current_date = None

    now = datetime.now()
    year_hint = now.year
    last_month = None

    i = 0
    while i < len(lines):
        mdate = DATE_RE.match(lines[i])
        if mdate:
            day = int(mdate.group(2))
            mon_abbr = mdate.group(3).lower()
            month = MONTHS.get(mon_abbr)
            if month is None:
                i += 1
                continue

            if last_month is not None and month < last_month:
                year_hint += 1
            last_month = month

            current_date = datetime(year_hint, month, day)
            i += 1
            continue

        mtime = TIME_RE.match(lines[i])
        if current_date and mtime:
            hh = int(mtime.group(1))
            mm = int(mtime.group(2))

            start = datetime(current_date.year, current_date.month, current_date.day, hh, mm, 0)
            end = start + timedelta(minutes=DURATION_MIN)

            block = []
            j = i + 1
            while j < len(lines):
                if DATE_RE.match(lines[j]) or TIME_RE.match(lines[j]):
                    break
                if FOOTER_STOP_RE.search(lines[j]):
                    break
                block.append(lines[j])
                if len(block) >= 80:
                    break
                j += 1

            summary, notes = extract_match_summary_and_notes(block)
            if summary:
                location = extract_channel_from_block(block)
                if not location:
                    location = extract_channel_from_html_near_time(html, hh, mm)

                # Dedup ekstra hårdt: hvis vi allerede har et event på samme tid,
                # behold den med "Hold A - Hold B" (denne) og overskriv ikke med noget dårligere.
                events_map[(start, summary)] = (end, location, notes)

            i = j
            continue

        i += 1

    events = []
    for (start, summary), (end, location, notes) in sorted(events_map.items(), key=lambda x: (x[0][0], x[0][1])):
        events.append((start, end, summary, location, notes))

    ics = build_ics(events)

    os.makedirs("docs", exist_ok=True)
    with open("docs/tv-program.ics", "w", encoding="utf-8", newline="") as f:
        f.write(ics)

    print(f"[{SCRIPT_VERSION}] Generated {len(events)} events -> docs/tv-program.ics")

if __name__ == "__main__":
    main()
