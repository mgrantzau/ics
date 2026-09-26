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
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "maj": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "okt": 10,
    "nov": 11,
    "dec": 12,
}

DATE_RE = re.compile(
    r"(\d{1,2})\.\s*([a-zæøå]+)",
    re.IGNORECASE,
)

TIME_RE = re.compile(
    r"kl\.\s*(\d{1,2}):(\d{2})",
    re.IGNORECASE,
)

WEEKDAY_WORDS = {
    "mandag",
    "tirsdag",
    "onsdag",
    "torsdag",
    "fredag",
    "lørdag",
    "søndag",
}

# VIGTIGT:
# Længste/most-specific først, ellers bliver "TV2 Sport" til "TV2".
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
    r"(?i)\b(?:afspilles\s+på\s+)?("
    + "|".join(CHANNEL_PATTERNS)
    + r")\b"
)

# Whitelist:
# Kun disse kanaler må komme med i feedet.
ALLOWED_CHANNELS = {
    "tv2 sport",
    "tv2 sport x",
    "tv2 play",
    "dr1",
    "dr2",
    "dr tv",
    "tv2",
}


@dataclass
class Event:
    summary: str
    start: datetime
    end: datetime
    location: str
    description: str


def parse_date_line(
    line: str,
    assumed_year: int,
) -> Optional[date]:

    m = DATE_RE.search(line.lower())

    if not m:
        return None

    day = int(m.group(1))
    mon_key = m.group(2)[:3].lower()

    month = MONTHS.get(mon_key)

    if not month:
        return None

    return date(
        assumed_year,
        month,
        day,
    )


def parse_time_line(
    line: str,
) -> Optional[tuple[int, int]]:

    m = TIME_RE.search(line.lower())

    if not m:
        return None

    return (
        int(m.group(1)),
        int(m.group(2)),
    )


def looks_like_weekday_date_header(
    s: str,
) -> bool:

    low = s.lower().strip()

    if not low:
        return False

    words = low.split()

    if not words:
        return False

    if words[0] not in WEEKDAY_WORDS:
        return False

    return bool(
        DATE_RE.search(low)
    )


def ics_escape(
    text: str,
) -> str:

    return (
        (text or "")
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


def clean_channel(
    s: str,
) -> str:

    if not s:
        return ""

    s = re.sub(
        r"^\s*afspilles\s+på\s+",
        "",
        s,
        flags=re.IGNORECASE,
    ).strip()

    s = re.sub(
        r"\bTV\s*2\b",
        "TV2",
        s,
        flags=re.IGNORECASE,
    )

    s = re.sub(
        r"\bTV\s*3\b",
        "TV3",
        s,
        flags=re.IGNORECASE,
    )

    s = re.sub(
        r"\s{2,}",
        " ",
        s,
    ).strip()

    return s


def normalize_channel_key(
    s: str,
) -> str:

    """
    Normaliser kanalnavn til en stabil nøgle til sammenligning.
    """

    s = (s or "").strip().lower()

    s = re.sub(
        r"\s{2,}",
        " ",
        s,
    )

    s = s.replace(
        "tv 2",
        "tv2",
    )

    s = s.replace(
        "tv 3",
        "tv3",
    )

    s = s.replace(
        "dr 1",
        "dr1",
    )

    s = s.replace(
        "dr 2",
        "dr2",
    )

    return s


def is_allowed_channel(
    channel: str,
) -> bool:

    return (
        normalize_channel_key(channel)
        in ALLOWED_CHANNELS
    )


def scrape_cards_payload() -> List[dict]:

    """
    Hent TV-programsiden.

    Vi bruger bevidst BODY i stedet for en CSS/styling-baseret
    card-selector. Samtidig udskriver vi diagnostik, så et CI-run
    viser præcis hvad GitHub Actions modtager fra hjemmesiden.
    """

    with sync_playwright() as p:

        browser = p.chromium.launch()

        page = browser.new_page(
            viewport={
                "width": 1440,
                "height": 1200,
            },
            user_agent=(
                "Mozilla/5.0 "
                "(Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/140.0.0.0 "
                "Safari/537.36"
            ),
        )

        response = page.goto(
            URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        status = (
            response.status
            if response
            else "no response"
        )

        print(
            f"HTTP status: {status}"
        )

        print(
            f"Final URL: {page.url}"
        )

        try:
            title = page.title()
        except Exception:
            title = "<unable to read title>"

        print(
            f"Page title: {title}"
        )

        # Giv JavaScript tid til at bygge siden.
        page.wait_for_timeout(5000)

        # Scroll gennem siden for eventuelt lazy-loaded indhold.
        previous_height = 0

        for _ in range(15):

            try:
                height = page.evaluate(
                    "document.body ? "
                    "document.body.scrollHeight : 0"
                )
            except Exception:
                height = 0

            if not height:
                break

            if height == previous_height:
                # Scroll én ekstra gang, men vi behøver ikke
                # fortsætte 30 gange på en stabil side.
                page.evaluate(
                    "window.scrollTo("
                    "0, document.body.scrollHeight"
                    ")"
                )

                page.wait_for_timeout(1000)

                break

            previous_height = height

            page.evaluate(
                "window.scrollTo("
                "0, document.body.scrollHeight"
                ")"
            )

            page.wait_for_timeout(1000)

        # --------------------
        # DIAGNOSTIK
        # --------------------

        body_count = page.locator(
            "body"
        ).count()

        main_count = page.locator(
            "main"
        ).count()

        print(
            f"BODY elements: {body_count}"
        )

        print(
            f"MAIN elements: {main_count}"
        )

        if body_count == 0:

            html = page.content()

            print(
                f"HTML length: {len(html)}"
            )

            print(
                "----- HTML PREVIEW -----"
            )

            print(
                html[:5000]
            )

            print(
                "----- END HTML PREVIEW -----"
            )

            browser.close()

            raise RuntimeError(
                "The TV programme page did not contain a BODY element."
            )

        body = page.locator(
            "body"
        )

        try:
            body_text = body.inner_text(
                timeout=10000
            )
        except Exception as exc:

            html = page.content()

            print(
                "Unable to read BODY innerText."
            )

            print(
                f"Error: {exc}"
            )

            print(
                f"HTML length: {len(html)}"
            )

            print(
                "----- HTML PREVIEW -----"
            )

            print(
                html[:5000]
            )

            print(
                "----- END HTML PREVIEW -----"
            )

            browser.close()

            raise

        print(
            f"Body text length: {len(body_text)}"
        )

        print(
            "----- BODY PREVIEW -----"
        )

        print(
            body_text[:5000]
        )

        print(
            "----- END BODY PREVIEW -----"
        )

        # --------------------
        # PAYLOAD
        # --------------------

        payload: List[dict] = (
            page.eval_on_selector_all(
                "body",
                """
                (els) => els.map(el => {
                    const text =
                        (el.innerText || "").trim();

                    const alts =
                        Array.from(
                            el.querySelectorAll("img")
                        )
                        .map(img =>
                            (
                                img.getAttribute("alt")
                                || ""
                            ).trim()
                        )
                        .filter(Boolean);

                    const aria =
                        Array.from(
                            el.querySelectorAll(
                                "[aria-label]"
                            )
                        )
                        .map(n =>
                            (
                                n.getAttribute(
                                    "aria-label"
                                )
                                || ""
                            ).trim()
                        )
                        .filter(Boolean);

                    const uniq = (arr) =>
                        Array.from(
                            new Set(arr)
                        );

                    return {
                        text,
                        alts: uniq(alts),
                        aria: uniq(aria)
                    };
                })
                """,
            )
        )

        print(
            f"Payload elements: {len(payload)}"
        )

        browser.close()

        if not payload:

            raise RuntimeError(
                "Could not retrieve page BODY "
                "from TV programme page."
            )

        return payload


def normalize_lines(
    text: str,
    alts: List[str],
    aria: List[str],
) -> List[str]:

    lines = [
        line.strip()
        for line in (text or "").split("\n")
        if line.strip()
    ]

    for alt in (alts or []):

        if (
            alt
            and alt not in lines
        ):
            lines.append(
                alt
            )

    for label in (aria or []):

        if (
            label
            and label not in lines
        ):
            lines.append(
                label
            )

    return lines


def find_channel(
    lines_after_match: List[str],
) -> str:

    for row in lines_after_match:

        m = CHANNEL_RE.search(
            row.strip()
        )

        if m:
            return clean_channel(
                m.group(1)
            )

    return ""


def find_description(
    lines_after_match: List[str],
) -> str:

    for row in lines_after_match:

        rr = row.strip()

        if not rr:
            continue

        if looks_like_weekday_date_header(
            rr
        ):
            continue

        if parse_time_line(
            rr
        ):
            continue

        # Næste kamp
        if " - " in rr:
            break

        if CHANNEL_RE.search(
            rr
        ):
            continue

        return rr

    return ""


def parse_payload_to_events(
    payload: List[dict],
) -> List[Event]:

    year = datetime.now().year

    events: List[Event] = []

    skipped_not_allowed = 0
    skipped_missing_channel = 0

    for card in payload:

        lines = normalize_lines(
            card.get(
                "text",
                "",
            ),
            card.get(
                "alts",
                [],
            ),
            card.get(
                "aria",
                [],
            ),
        )

        if not lines:
            continue

        current_date: Optional[date] = None

        current_time: Optional[
            tuple[int, int]
        ] = None

        for i, line in enumerate(
            lines
        ):

            d = parse_date_line(
                line,
                year,
            )

            if d:
                current_date = d
                continue

            t = parse_time_line(
                line
            )

            if t:
                current_time = t
                continue

            if (
                " - " in line
                and current_date
                and current_time
            ):

                summary = line.strip()

                rest = lines[
                    i + 1:
                ]

                channel = find_channel(
                    rest
                )

                # --------------------
                # WHITELIST FILTER
                # --------------------

                if not channel.strip():

                    skipped_missing_channel += 1

                    current_time = None

                    continue

                if not is_allowed_channel(
                    channel
                ):

                    skipped_not_allowed += 1

                    current_time = None

                    continue

                desc = find_description(
                    rest
                )

                start_dt = datetime(
                    current_date.year,
                    current_date.month,
                    current_date.day,
                    current_time[0],
                    current_time[1],
                )

                end_dt = (
                    start_dt
                    + timedelta(
                        minutes=DEFAULT_DURATION_MIN
                    )
                )

                events.append(
                    Event(
                        summary=summary,
                        start=start_dt,
                        end=end_dt,
                        location=channel,
                        description=desc,
                    )
                )

                current_time = None

    if not events:

        print(
            "Parsed events: 0"
        )

        print(
            "Skipped (not allowed channel): "
            f"{skipped_not_allowed}"
        )

        print(
            "Skipped (missing channel): "
            f"{skipped_missing_channel}"
        )

        raise RuntimeError(
            "No events were parsed "
            "(or all were filtered). "
            "See BODY PREVIEW above for diagnostics."
        )

    parse_payload_to_events.skipped_not_allowed = (
        skipped_not_allowed
    )

    parse_payload_to_events.skipped_missing_channel = (
        skipped_missing_channel
    )

    return events


def write_ics(
    events: List[Event],
) -> None:

    now_utc = (
        datetime.utcnow()
        .strftime(
            "%Y%m%dT%H%M%SZ"
        )
    )

    lines = [
        "BEGIN:VCALENDAR",
        "PRODID:-//ChatGPT//Handball ICS//DA",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-SCRIPT-VERSION:2026-09-26-body-diagnostic-v8",

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

    for event in events:

        uid_basis = (
            f"{event.start.isoformat()}"
            f"|{event.summary}"
        )

        uid = uuid.uuid5(
            uuid.NAMESPACE_URL,
            uid_basis,
        )

        lines.extend(
            [
                "BEGIN:VEVENT",

                (
                    f"UID:{uid}"
                    "@danskhaandbold.tvprogram"
                ),

                f"DTSTAMP:{now_utc}",

                (
                    f"DTSTART;TZID={TZID}:"
                    f"{event.start.strftime('%Y%m%dT%H%M%S')}"
                ),

                (
                    f"DTEND;TZID={TZID}:"
                    f"{event.end.strftime('%Y%m%dT%H%M%S')}"
                ),

                (
                    "SUMMARY:"
                    f"{ics_escape(event.summary)}"
                ),

                (
                    "LOCATION:"
                    f"{ics_escape(event.location)}"
                ),

                (
                    "DESCRIPTION:"
                    f"{ics_escape(event.description)}"
                ),

                "END:VEVENT",
            ]
        )

    lines.append(
        "END:VCALENDAR"
    )

    with open(
        OUT_FILE,
        "w",
        encoding="utf-8",
    ) as file:

        file.write(
            "\n".join(
                lines
            )
        )


if __name__ == "__main__":

    payload = (
        scrape_cards_payload()
    )

    events = (
        parse_payload_to_events(
            payload
        )
    )

    write_ics(
        events
    )

    skipped_not_allowed = getattr(
        parse_payload_to_events,
        "skipped_not_allowed",
        0,
    )

    skipped_missing_channel = getattr(
        parse_payload_to_events,
        "skipped_missing_channel",
        0,
    )

    print(
        f"Generated {len(events)} events "
        f"→ {OUT_FILE}"
    )

    print(
        "Skipped (not allowed channel): "
        f"{skipped_not_allowed}"
    )

    print(
        "Skipped (missing channel): "
        f"{skipped_missing_channel}"
    )

    missing_loc = sum(
        1
        for event in events
        if not event.location.strip()
    )

    print(
        f"Missing LOCATION: "
        f"{missing_loc} / {len(events)}"
    )
