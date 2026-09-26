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


# Alle kendte kanaler/platforme.
#
# Det er vigtigt, at vi genkender OGSÅ kanaler, som ikke skal
# med i kalenderen. Ellers risikerer parseren at fortsætte til
# en senere TV2-kanal og knytte den til den forkerte kamp.

CHANNEL_PATTERNS = [
    r"Ekstra\s*Bladet\+",
    r"TV2\s*Sport\s*X",
    r"TV2\s*Sport",
    r"TV2\s*Play",
    r"TV3\s*Sport",
    r"TV3\s*Max",
    r"TV3\+",
    r"Sport\s*Live",
    r"Pluto\s*TV",
    r"DR\s*TV",
    r"DR\s*\d",
    r"Viaplay",
    r"Eurosport",
    r"Discovery\+",
    r"MAX",
    r"TV3",
    r"TV2",
]


CHANNEL_RE = re.compile(
    r"(?i)\b(?:afspilles\s+på\s+)?("
    + "|".join(CHANNEL_PATTERNS)
    + r")\b"
)


# Kun disse kanaler/platforme kommer med i kalenderen.

ALLOWED_CHANNELS = {
    "tv2",
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


def parse_date_line(
    line: str,
    assumed_year: int,
) -> Optional[date]:

    m = DATE_RE.search(
        line.lower()
    )

    if not m:
        return None

    day = int(
        m.group(1)
    )

    mon_key = (
        m.group(2)[:3]
        .lower()
    )

    month = MONTHS.get(
        mon_key
    )

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

    m = TIME_RE.search(
        line.lower()
    )

    if not m:
        return None

    return (
        int(m.group(1)),
        int(m.group(2)),
    )


def looks_like_weekday_date_header(
    text: str,
) -> bool:

    low = (
        text.lower()
        .strip()
    )

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


def looks_like_match(
    text: str,
) -> bool:

    text = (
        text or ""
    ).strip()

    if not text:
        return False

    if " - " not in text:
        return False

    if looks_like_weekday_date_header(
        text
    ):
        return False

    return True


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
    text: str,
) -> str:

    if not text:
        return ""

    text = re.sub(
        r"^\s*afspilles\s+på\s+",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()

    text = re.sub(
        r"\bTV\s*2\b",
        "TV2",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"\bTV\s*3\b",
        "TV3",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"\s{2,}",
        " ",
        text,
    ).strip()

    return text


def normalize_channel_key(
    text: str,
) -> str:

    text = (
        text or ""
    ).strip().lower()

    text = re.sub(
        r"\s{2,}",
        " ",
        text,
    )

    text = text.replace(
        "tv 2",
        "tv2",
    )

    text = text.replace(
        "tv 3",
        "tv3",
    )

    text = text.replace(
        "dr 1",
        "dr1",
    )

    text = text.replace(
        "dr 2",
        "dr2",
    )

    return text


def is_allowed_channel(
    channel: str,
) -> bool:

    return (
        normalize_channel_key(
            channel
        )
        in ALLOWED_CHANNELS
    )


# --------------------
# SCRAPER
# --------------------

def scrape_cards_payload() -> List[dict]:

    """
    Hent hele den synlige TV-programside.

    Håndbold.dk har ikke længere den tidligere <main>-struktur,
    så BODY bruges som datakilde.

    Selve afgrænsningen mellem kampene sker efterfølgende i
    parseren og ikke via hjemmesidens styling-klasser.
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

        print(
            f"Page title: {page.title()}"
        )

        page.wait_for_timeout(
            5000
        )

        # Scroll siden for at sikre, at lazy-loaded indhold
        # bliver hentet.

        previous_height = 0

        for _ in range(15):

            height = page.evaluate(
                """
                document.body
                    ? document.body.scrollHeight
                    : 0
                """
            )

            if not height:
                break

            page.evaluate(
                """
                window.scrollTo(
                    0,
                    document.body.scrollHeight
                )
                """
            )

            page.wait_for_timeout(
                1000
            )

            if height == previous_height:
                break

            previous_height = height

        body_count = (
            page.locator("body")
            .count()
        )

        main_count = (
            page.locator("main")
            .count()
        )

        print(
            f"BODY elements: {body_count}"
        )

        print(
            f"MAIN elements: {main_count}"
        )

        if body_count == 0:

            browser.close()

            raise RuntimeError(
                "The TV programme page "
                "did not contain a BODY element."
            )

        body_text = (
            page.locator("body")
            .inner_text(
                timeout=10000
            )
        )

        print(
            f"Body text length: "
            f"{len(body_text)}"
        )

        payload = [
            {
                "text": body_text,
                "alts": [],
                "aria": [],
            }
        ]

        browser.close()

        return payload


# --------------------
# NORMALISERING
# --------------------

def normalize_lines(
    text: str,
    alts: List[str],
    aria: List[str],
) -> List[str]:

    lines = [
        line.strip()
        for line
        in (text or "").split("\n")
        if line.strip()
    ]

    for alt in (
        alts or []
    ):

        if (
            alt
            and alt not in lines
        ):
            lines.append(
                alt
            )

    for label in (
        aria or []
    ):

        if (
            label
            and label not in lines
        ):
            lines.append(
                label
            )

    return lines


# --------------------
# KAMP-AFGRÆNSNING
# --------------------

def collect_match_block(
    lines: List[str],
    match_index: int,
) -> List[str]:

    """
    Returnerer kun linjerne, der tilhører den aktuelle kamp.

    Vi stopper ved:
      - næste tidspunkt
      - næste dato
      - næste kamp

    Dermed kan en kanal fra en senere kamp aldrig blive brugt
    på den aktuelle kamp.
    """

    block: List[str] = []

    for line in lines[
        match_index + 1:
    ]:

        stripped = (
            line or ""
        ).strip()

        if not stripped:
            continue

        if looks_like_weekday_date_header(
            stripped
        ):
            break

        if parse_time_line(
            stripped
        ):
            break

        if looks_like_match(
            stripped
        ):
            break

        block.append(
            stripped
        )

    return block


def find_channel_in_block(
    block: List[str],
) -> str:

    for line in block:

        match = CHANNEL_RE.search(
            line
        )

        if match:

            return clean_channel(
                match.group(1)
            )

    return ""


def find_description_in_block(
    block: List[str],
) -> str:

    for line in block:

        stripped = (
            line or ""
        ).strip()

        if not stripped:
            continue

        if CHANNEL_RE.search(
            stripped
        ):
            continue

        if (
            stripped.lower()
            .startswith(
                "afspilles på"
            )
        ):
            continue

        return stripped

    return ""


# --------------------
# PARSER
# --------------------

def parse_payload_to_events(
    payload: List[dict],
) -> List[Event]:

    year = datetime.now().year

    events: List[Event] = []

    skipped_not_allowed = 0
    skipped_missing_channel = 0
    total_matches_found = 0

    skipped_channel_counts = {}

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

        current_date: Optional[
            date
        ] = None

        current_time: Optional[
            tuple[int, int]
        ] = None

        for index, line in enumerate(
            lines
        ):

            parsed_date = (
                parse_date_line(
                    line,
                    year,
                )
            )

            if parsed_date:

                current_date = (
                    parsed_date
                )

                continue

            parsed_time = (
                parse_time_line(
                    line
                )
            )

            if parsed_time:

                current_time = (
                    parsed_time
                )

                continue

            if not looks_like_match(
                line
            ):
                continue

            if not current_date:
                continue

            if not current_time:
                continue

            total_matches_found += 1

            summary = (
                line.strip()
            )

            block = collect_match_block(
                lines,
                index,
            )

            channel = (
                find_channel_in_block(
                    block
                )
            )

            description = (
                find_description_in_block(
                    block
                )
            )

            if not channel:

                skipped_missing_channel += 1

                print(
                    "SKIP missing channel: "
                    f"{summary}"
                )

                current_time = None

                continue

            channel_key = (
                normalize_channel_key(
                    channel
                )
            )

            if not is_allowed_channel(
                channel
            ):

                skipped_not_allowed += 1

                skipped_channel_counts[
                    channel_key
                ] = (
                    skipped_channel_counts
                    .get(
                        channel_key,
                        0,
                    )
                    + 1
                )

                print(
                    "SKIP channel "
                    f"{channel}: "
                    f"{summary}"
                )

                current_time = None

                continue

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
                    minutes=(
                        DEFAULT_DURATION_MIN
                    )
                )
            )

            events.append(
                Event(
                    summary=summary,
                    start=start_dt,
                    end=end_dt,
                    location=channel,
                    description=description,
                )
            )

            current_time = None

    print(
        f"Total matches found: "
        f"{total_matches_found}"
    )

    print(
        f"Allowed events: "
        f"{len(events)}"
    )

    print(
        "Skipped "
        "(not allowed channel): "
        f"{skipped_not_allowed}"
    )

    print(
        "Skipped "
        "(missing channel): "
        f"{skipped_missing_channel}"
    )

    if skipped_channel_counts:

        print(
            "Skipped channels:"
        )

        for (
            channel,
            count
        ) in sorted(
            skipped_channel_counts.items()
        ):

            print(
                f"  {channel}: {count}"
            )

    if not events:

        raise RuntimeError(
            "No allowed events were parsed. "
            "The site structure or channel "
            "representation may have changed."
        )

    parse_payload_to_events.skipped_not_allowed = (
        skipped_not_allowed
    )

    parse_payload_to_events.skipped_missing_channel = (
        skipped_missing_channel
    )

    parse_payload_to_events.total_matches_found = (
        total_matches_found
    )

    return events


# --------------------
# ICS
# --------------------

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
        (
            "X-SCRIPT-VERSION:"
            "2026-09-26-match-boundaries-v9"
        ),

        "BEGIN:VTIMEZONE",
        "TZID:Europe/Copenhagen",
        (
            "X-LIC-LOCATION:"
            "Europe/Copenhagen"
        ),

        "BEGIN:DAYLIGHT",
        "TZOFFSETFROM:+0100",
        "TZOFFSETTO:+0200",
        "TZNAME:CEST",
        "DTSTART:19700329T020000",
        (
            "RRULE:FREQ=YEARLY;"
            "BYMONTH=3;"
            "BYDAY=-1SU"
        ),
        "END:DAYLIGHT",

        "BEGIN:STANDARD",
        "TZOFFSETFROM:+0200",
        "TZOFFSETTO:+0100",
        "TZNAME:CET",
        "DTSTART:19701025T030000",
        (
            "RRULE:FREQ=YEARLY;"
            "BYMONTH=10;"
            "BYDAY=-1SU"
        ),
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
                    "@danskhaandbold."
                    "tvprogram"
                ),

                (
                    f"DTSTAMP:"
                    f"{now_utc}"
                ),

                (
                    f"DTSTART;TZID="
                    f"{TZID}:"
                    f"{event.start.strftime('%Y%m%dT%H%M%S')}"
                ),

                (
                    f"DTEND;TZID="
                    f"{TZID}:"
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


# --------------------
# MAIN
# --------------------

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

    total_matches_found = getattr(
        parse_payload_to_events,
        "total_matches_found",
        0,
    )

    print(
        f"Generated {len(events)} "
        f"events → {OUT_FILE}"
    )

    print(
        f"Total matches found: "
        f"{total_matches_found}"
    )

    print(
        "Skipped "
        "(not allowed channel): "
        f"{skipped_not_allowed}"
    )

    print(
        "Skipped "
        "(missing channel): "
        f"{skipped_missing_channel}"
    )

    missing_location = sum(
        1
        for event in events
        if not event.location.strip()
    )

    print(
        f"Missing LOCATION: "
        f"{missing_location} / "
        f"{len(events)}"
    )
