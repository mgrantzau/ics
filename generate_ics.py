import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, date
from typing import Optional, List

from playwright.sync_api import sync_playwright

# --------------------
# CONFIG
# --------------------
URL = "https://danskhaandbold.dk/tv-program"
OUT_FILE = "docs/tv-program.ics"
TZID = "Europe/Copenhagen"
DEFAULT_DURATION_MIN = 90

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "maj": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dec": 12,
}

DATE_RE = re.compile(r"(\d{1,2})\.\s*([a-zæøå]+)", re.IGNORECASE)
TIME_RE = re.compile(r"kl\.\s*(\d{1,2}):(\d{2})", re.IGNORECASE)

# Kanal-heuristik: udvid listen hvis der dukker nye op
CHANNEL_RE = re.compile(
    r"^(?:TV\s?2|TV2|TV\s?3|TV3|DR\d?|DR\s?TV|Viaplay|TV2\s?Play|TV\s?2\s?Play|"
    r"TV2\s?Sport\s?X|TV2\s?Sport|TV3\s?Sport|TV3\s?Max|TV2\s?Charlie|TV2\s?News|"
    r"Eurosport|MAX|Discovery\+)\b",
    re.IGNORECASE
)

WEEKDAY_WORDS = {"mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"}


@dataclass
class Event:
    summary: str
    start: datetime
    end: datetime
    location: str
    description: str


def parse_date_line(line: str, assumed_year: int) -> Optional[date]:
    m = DATE_RE.search(line.lower())
    if not m:
        return None
    day = int(m.group(1))
    mon_key = m.group(2)[:3].lower()
    month = MONTHS.get(mon_key)
    if not month:
        return None
    return date(assumed_year, month, day)


def parse_time_line(line: str) -> Optional[tuple[int, int]]:
    m = TIME_RE.search(line.lower())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def looks_like_channel(s: str) -> bool:
    s = (s or "").strip()
    if not s:
        return False
    return bool(CHANNEL_RE.search(s))


def looks_like_weekday_date_header(s: str) -> bool:
    # fx "torsdag 15. jan." -> indeholder ugedag + datoformat
    low = s.lower()
    if not any(w in low.split()[:2] for w in WEEKDAY_WORDS):
        return False
    return bool(DATE_RE.search(low))


def ics_escape(text: str) -> str:
    return (
        (text or "")
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


def scrape_blocks_text() -> List[str]:
    """
    Returnerer en liste af tekstblokke (1 blok pr. program-card).
    Al tekst udtrækkes mens browseren stadig kører (ingen event-loop fejl).
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(URL, wait_until="networkidle")
        page.wait_for_timeout(2500)

        # Scroll for lazy load
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

        blocks: List[str] = page.eval_on_selector_all(
            "main .odd\\:bg-charcoal\\/5",
            "els => els.map(e => e.innerText)"
        )

        browser.close()
        return blocks


def parse_blocks_to_events(blocks: List[str]) -> List[Event]:
    year = datetime.now().year
    events: List[Event] = []

    for block in blocks:
        lines = [l.strip() for l in block.split("\n") if l.strip()]
        if not lines:
            continue

        current_date: Optional[date] = None
        current_time: Optional[tuple[int, int]] = None

        for i, line in enumerate(lines):
            # opdater date/time når vi ser dem
            d = parse_date_line(line, year)
            if d:
                current_date = d
                continue

            t = parse_time_line(line)
            if t:
                current_time = t
                continue

            # match-linje: kræv " - " og at vi har date+time sat
            if " - " in line and current_date and current_time:
                summary = line.strip()

                # Kig fremad i resten af blokken for description + channel
                rest = lines[i + 1 :]

                channel = ""
                desc = ""

                # Find første kanal-linje
                for r in rest:
                    if looks_like_channel(r):
                        channel = r.strip()
                        break

                # Find første "beskrivelse" (ikke kanal, ikke datoheader, ikke tid)
                for r in rest:
                    rr = r.strip()
                    if not rr:
                        continue
                    if looks_like_channel(rr):
                        continue
                    if looks_like_weekday_date_header(rr):
                        continue
                    if parse_time_line(rr):
                        continue
                    if " - " in rr:  # næste kamp starter
                        break
                    # typisk turnering/række
                    desc = rr
                    break

                start_dt = datetime(
                    current_date.year, current_date.month, current_date.day,
                    current_time[0], current_time[1]
                )
                end_dt = start_dt + timedelta(minutes=DEFAULT_DURATION_MIN)

                events.append(
                    Event(
                        summary=summary,
                        start=start_dt,
                        end=end_dt,
                        location=channel,
                        description=desc,
                    )
                )

                # nulstil tid så vi ikke utilsigtet genbruger den
                current_time = None

    if not events:
        raise RuntimeError("No events were parsed. The site structure may have changed.")

    return events


def write_ics(events: List[Event]) -> None:
    now_utc = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "PRODID:-//ChatGPT//Handball ICS//DA",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-SCRIPT-VERSION:2026-01-15-dom-parser-final-v3",
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
        # Stabil UID: starttid + kampnavn (så opdateringer ikke laver dubletter)
        uid_basis = f"{e.start.isoformat()}|{e.summary}"
        uid = uuid.uuid5(uuid.NAMESPACE_URL, uid_basis)

        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{uid}@danskhaandbold.tvprogram",
                f"DTSTAMP:{now_utc}",
                f"DTSTART;TZID={TZID}:{e.start.strftime('%Y%m%dT%H%M%S')}",
                f"DTEND;TZID={TZID}:{e.end.strftime('%Y%m%dT%H%M%S')}",
                f"SUMMARY:{ics_escape(e.summary)}",
                f"LOCATION:{ics_escape(e.location)}",
                f"DESCRIPTION:{ics_escape(e.description)}",
                "END:VEVENT",
            ]
        )

    lines.append("END:VCALENDAR")

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    blocks = scrape_blocks_text()
    events = parse_blocks_to_events(blocks)
    write_ics(events)
    print(f"Generated {len(events)} events → {OUT_FILE}")
