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

WEEKDAY_WORDS = {"mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"}

# VIGTIGT: længste/most-specific først, ellers bliver "TV2 Sport" til "TV2"
CHANNEL_PATTERNS = [
    r"TV2\s*Sport\s*X",
    r"TV2\s*Sport",
    r"TV2\s*Play",
    r"TV3\s*Sport",
    r"TV3\s*Max",
    r"DR\s*TV",
    r"DR\s*\d",
    r"Viaplay",
    r"Eurosport",
    r"MAX",
    r"Discovery\+",
    r"TV3",
    r"TV2",
]

CHANNEL_RE = re.compile(
    r"(?i)\b(?:afspilles\s+på\s+)?(" + "|".join(CHANNEL_PATTERNS) + r")\b"
)

# Whitelist: kun disse kanaler må komme med i feedet (case-insensitive, normaliseret)
ALLOWED_CHANNELS = {
    "tv2 sport",
    "tv2 sport x",
    "tv2 play",
    "dr1",
    "dr2",
    "dr tv",
}


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


def looks_like_weekday_date_header(s: str) -> bool:
    low = s.lower().strip()
    if not low:
        return False
    words = low.split()
    if not words:
        return False
    if words[0] not in WEEKDAY_WORDS:
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


def clean_channel(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"^\s*afspilles\s+på\s+", "", s, flags=re.IGNORECASE).strip()
    # Normaliser små mellemrumvarianter
    s = re.sub(r"\bTV\s*2\b", "TV2", s, flags=re.IGNORECASE)
    s = re.sub(r"\bTV\s*3\b", "TV3", s, flags=re.IGNORECASE)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s


def normalize_channel_key(s: str) -> str:
    """
    Normaliser kanalnavn til en stabil nøgle til sammenligning.
    """
    s = (s or "").strip().lower()
    s = re.sub(r"\s{2,}", " ", s)
    s = s.replace("tv 2", "tv2").replace("tv 3", "tv3")
    return s


def is_allowed_channel(channel: str) -> bool:
    return normalize_channel_key(channel) in ALLOWED_CHANNELS


def scrape_cards_payload() -> List[dict]:
    """
    Returnerer pr card:
      - text: innerText
      - alts: img alt-tekster
      - aria: aria-labels
    Alt udtrækkes mens browseren er åben.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(URL, wait_until="networkidle")
        page.wait_for_timeout(2500)

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

        payload: List[dict] = page.eval_on_selector_all(
            "main .odd\\:bg-charcoal\\/5",
            """
            (els) => els.map(el => {
              const text = (el.innerText || "").trim();

              const alts = Array.from(el.querySelectorAll("img"))
                .map(img => (img.getAttribute("alt") || "").trim())
                .filter(Boolean);

              const aria = Array.from(el.querySelectorAll("[aria-label]"))
                .map(n => (n.getAttribute("aria-label") || "").trim())
                .filter(Boolean);

              const uniq = (arr) => Array.from(new Set(arr));
              return { text, alts: uniq(alts), aria: uniq(aria) };
            })
            """,
        )

        browser.close()
        return payload


def normalize_lines(text: str, alts: List[str], aria: List[str]) -> List[str]:
    lines = [l.strip() for l in (text or "").split("\n") if l.strip()]
    for a in (alts or []):
        if a and a not in lines:
            lines.append(a)
    for a in (aria or []):
        if a and a not in lines:
            lines.append(a)
    return lines


def find_channel(lines_after_match: List[str]) -> str:
    for r in lines_after_match:
        m = CHANNEL_RE.search(r.strip())
        if m:
            return clean_channel(m.group(1))
    return ""


def find_description(lines_after_match: List[str]) -> str:
    for r in lines_after_match:
        rr = r.strip()
        if not rr:
            continue
        if looks_like_weekday_date_header(rr):
            continue
        if parse_time_line(rr):
            continue
        if " - " in rr:  # næste kamp
            break
        if CHANNEL_RE.search(rr):
            continue
        return rr
    return ""


def parse_payload_to_events(payload: List[dict]) -> List[Event]:
    year = datetime.now().year
    events: List[Event] = []

    skipped_not_allowed = 0
    skipped_missing_channel = 0

    for card in payload:
        lines = normalize_lines(card.get("text", ""), card.get("alts", []), card.get("aria", []))
        if not lines:
            continue

        current_date: Optional[date] = None
        current_time: Optional[tuple[int, int]] = None

        for i, line in enumerate(lines):
            d = parse_date_line(line, year)
            if d:
                current_date = d
                continue

            t = parse_time_line(line)
            if t:
                current_time = t
                continue

            if " - " in line and current_date and current_time:
                summary = line.strip()
                rest = lines[i + 1 :]

                channel = find_channel(rest)

                # ---- WHITELIST FILTER ----
                if not channel.strip():
                    skipped_missing_channel += 1
                    current_time = None
                    continue

                if not is_allowed_channel(channel):
                    skipped_not_allowed += 1
                    current_time = None
                    continue

                desc = find_description(rest)

                start_dt = datetime(
                    current_date.year, current_date.month, current_date.day,
                    current_time[0], current_time[1]
                )
                end_dt = start_dt + timedelta(minutes=DEFAULT_DURATION_MIN)

                events.append(Event(summary=summary, start=start_dt, end=end_dt, location=channel, description=desc))

                current_time = None

    if not events:
        raise RuntimeError("No events were parsed (or all were filtered). The site structure may have changed.")

    # gem counts til print
    parse_payload_to_events.skipped_not_allowed = skipped_not_allowed  # type: ignore[attr-defined]
    parse_payload_to_events.skipped_missing_channel = skipped_missing_channel  # type: ignore[attr-defined]

    return events


def write_ics(events: List[Event]) -> None:
    now_utc = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "PRODID:-//ChatGPT//Handball ICS//DA",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-SCRIPT-VERSION:2026-01-15-dom-parser-final-v6",
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
    payload = scrape_cards_payload()
    events = parse_payload_to_events(payload)
    write_ics(events)

    skipped_not_allowed = getattr(parse_payload_to_events, "skipped_not_allowed", 0)
    skipped_missing_channel = getattr(parse_payload_to_events, "skipped_missing_channel", 0)

    print(f"Generated {len(events)} events → {OUT_FILE}")
    print(f"Skipped (not allowed channel): {skipped_not_allowed}")
    print(f"Skipped (missing channel): {skipped_missing_channel}")

    missing_loc = sum(1 for e in events if not e.location.strip())
    print(f"Missing LOCATION: {missing_loc} / {len(events)}")
