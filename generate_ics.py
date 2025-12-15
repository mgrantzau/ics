# generate_ics.py
# Endelig version: 1 event pr. kamp, stabil UID, LOCATION = kanal, DESCRIPTION = øvrige noter.
# Håndterer at kampnavn og "afspilles på" kan være splittet over flere linjer.
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

# Kendte kanaler (udvid efter behov)
CHANNEL_ANYWHERE_RE = re.compile(
    r"(TV2\s*Sport|TV2\s*Play|TV2\b|DR1\b|DR2\b|TV3\s*Sport)",
    re.IGNORECASE
)

# Stop ved footer/irrelevant indhold (så vi ikke sluger hele siden)
FOOTER_STOP_RE = re.compile(
    r"(Besøg Landsholdshoppen|CVR nummer|danskhaandbold@danskhaandbold\.dk|DanskHåndbold 2025|GDPR|Boozt\.com)",
    re.IGNORECASE
)

def norm(s: str) -> str:
    return " ".join(s.replace("\u00a0", " ").split()).strip()

def ics_dt(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S")

def esc(s: str) -> str:
    return (s.replace("\\", "\\\\")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .replace("\n", "\\n")
            .replace(",", "\\,")
            .replace(";", "\\;"))

def stable_uid(start: datetime, summary: str) -> str:
    key = f"{start.strftime('%Y%m%dT%H%M')};{summary.strip()}"
    return f"{uuid.uuid5(uuid.NAMESPACE_URL, key)}@chatgpt.local"

def extract_channel(block: list[str]) -> str:
    """
    Finder kanal i blokken, uanset om den står som:
      - "afspilles på TV2 Sport"
      - "afspilles", "på", "TV2 Sport"
      - eller kun som substring i en linje
    """
    # 1) "afspilles på <...>" samme linje
    for ln in block:
        m = re.search(r"afspilles\s*p[åa]\s*(.+)$", ln, flags=re.IGNORECASE)
        if m:
            m2 = CHANNEL_ANYWHERE_RE.search(m.group(1))
            if m2:
                return norm(m2.group(1))

    # 2) split "afspilles" + "på/pa" + "<kanal>"
    for i, ln in enumerate(block):
        low = ln.lower()
        if low == "afspilles":
            if i + 1 < len(block) and block[i + 1].lower() in ("på", "pa"):
                if i + 2 < len(block):
                    m = CHANNEL_ANYWHERE_RE.search(block[i + 2])
                    if m:
                        return norm(m.group(1))
        if low in ("afspilles på", "afspilles pa"):
            if i + 1 < len(block):
                m = CHANNEL_ANYWHERE_RE.search(block[i + 1])
                if m:
                    return norm(m.group(1))

    # 3) fallback: kanal hvor som helst
    for ln in block:
        m = CHANNEL_ANYWHERE_RE.search(ln)
        if m:
            return norm(m.group(1))

    return ""

def extract_summary_and_notes(block: list[str]) -> tuple[str, str]:
    """
    SUMMARY skal være kampen.
    Håndterer:
      - "Hold A - Hold B" i én linje (eller med –)
      - "Hold A", "-", "Hold B" i tre linjer
    DESCRIPTION = alt andet (minus "afspilles/på/kanal")
    """
    one_line_match = re.compile(r".+\s[–-]\s.+")
    summary = ""

    # 1) én-linjers kamp
    for ln in block:
        if one_line_match.match(ln):
            summary = ln
            break

    # 2) 3-linjers kamp: team, '-', team
    if not summary:
        for i in range(1, len(block) - 1):
            if block[i] == "-" and block[i - 1] and block[i + 1]:
                summary = f"{block[i - 1]} - {block[i + 1]}"
                break

    # Hvis vi ikke kan lave en kamp-summary, så drop eventet (undgår dubletter)
    if not summary:
        return "", "\n".join(block).strip()

    # noter: alt undtagen summary-linjen, og undtagen split-linjer omkring "afspilles på"
    skip_set = {"afspilles", "på", "pa", "afspilles på", "afspilles pa"}
    notes_parts = []
    used_three_line_indices = set()

    # markér 3-linjers komponenter som "brugte", hvis vi lavede summary af dem
    if " - " in summary:
        parts = summary.split(" - ", 1)
        if len(parts) == 2:
            team1, team2 = parts[0].strip(), parts[1].strip()
            for i in range(1, len(block) - 1):
                if block[i] == "-" and block[i - 1] == team1 and block[i + 1] == team2:
                    used_three_line_indices = {i - 1, i, i + 1}
                    break

    for idx, ln in enumerate(block):
        if idx in used_three_line_indices:
            continue
        if ln == summary:
            continue
        if ln.lower() in skip_set:
            continue
        notes_parts.append(ln)

    notes = "\n".join([x for x in notes_parts if x]).strip()
    return summary, notes

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
    lines = [norm(ln) for ln in text.splitlines() if norm(ln)]

    date_re = re.compile(
        r"^(mandag|tirsdag|onsdag|torsdag|fredag|lørdag|søndag)\s+(\d{1,2})\.\s+([a-zæøå]+\.)$",
        re.IGNORECASE
    )
    time_re = re.compile(r"^kl\.\s*(\d{1,2}):(\d{2})$", re.IGNORECASE)

    events = {}  # (start, summary) -> (end, location, notes)
    current_date = None

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

            block = []
            j = i + 1
            while j < len(lines):
                if date_re.match(lines[j]) or time_re.match(lines[j]):
                    break
                if FOOTER_STOP_RE.search(lines[j]):
                    break
                block.append(lines[j])
                # sikkerhed mod at sluge for meget
                if len(block) >= 50:
                    break
                j += 1

            location = extract_channel(block)
            summary, notes = extract_summary_and_notes(block)

            # drop ikke-kamp events -> ingen "Pokalturnering ..." dubletter
            if summary:
                # hvis kanal-linjen står alene i notes, fjern den (vi har den i LOCATION)
                if location:
                    notes = "\n".join([ln for ln in notes.split("\n") if norm(ln).lower() != location.lower()]).strip()

                events[(start, summary)] = (end, location, notes)

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

    for (start, summary), (end, location, notes) in sorted(events.items(), key=lambda x: (x[0][0], x[0][1])):
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
    ics = "\r\n".join(out) + "\r\n"

    os.makedirs("docs", exist_ok=True)
    with open("docs/tv-program.ics", "w", encoding="utf-8", newline="") as f:
        f.write(ics)

    print(f"Generated {len(events)} events -> docs/tv-program.ics")

if __name__ == "__main__":
    main()
