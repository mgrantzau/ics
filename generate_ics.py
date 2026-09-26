import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, date
from typing import Optional, List

from playwright.sync_api import sync_playwright


# ============================================================
# CONFIG
# ============================================================

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


# ============================================================
# CHANNELS
# ============================================================

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
    r"(?i)(?:afspilles\s+på\s+)?("
    + "|".join(CHANNEL_PATTERNS)
    + r")"
)


ALLOWED_CHANNELS = {
    "tv2",
    "tv2 sport",
    "tv2 sport x",
    "tv2 play",
    "dr1",
    "dr2",
    "dr tv",
}


# ============================================================
# DATA
# ============================================================

@dataclass
class Event:
    summary: str
    start: datetime
    end: datetime
    location: str
    description: str


# ============================================================
# BASIC PARSING
# ============================================================

def parse_date_line(
    line: str,
    assumed_year: int,
) -> Optional[date]:

    match = DATE_RE.search(
        (line or "").lower()
    )

    if not match:
        return None

    day = int(
        match.group(1)
    )

    month_key = (
        match.group(2)[:3]
        .lower()
    )

    month = MONTHS.get(
        month_key
    )

    if not month:
        return None

    try:
        return date(
            assumed_year,
            month,
            day,
        )

    except ValueError:
        return None


def parse_time_line(
    line: str,
) -> Optional[tuple[int, int]]:

    match = TIME_RE.search(
        (line or "").lower()
    )

    if not match:
        return None

    return (
        int(match.group(1)),
        int(match.group(2)),
    )


def looks_like_weekday_date_header(
    text: str,
) -> bool:

    text = (
        text or ""
    ).strip().lower()

    if not text:
        return False

    words = text.split()

    if not words:
        return False

    if words[0] not in WEEKDAY_WORDS:
        return False

    return bool(
        DATE_RE.search(text)
    )


# ============================================================
# MATCH DETECTION
# ============================================================

def looks_like_competition_text(
    text: str,
) -> bool:

    """
    Frasorterer tekster, som indeholder ' - ', men som tydeligt
    er turnerings-/rundeangivelser og ikke holdnavne.

    Eksempel:
        Pokalturnering Kvinder 2025 - 1/4-finaler
    """

    low = (
        text or ""
    ).strip().lower()

    competition_words = [
        "finaler",
        "finale",
        "semifinale",
        "semifinaler",
        "kvartfinale",
        "kvartfinaler",
        "1/4-final",
        "1/8-final",
        "gruppespil",
        "indl. grupper",
        "indledende grupper",
        "pokalturnering",
    ]

    if any(
        word in low
        for word in competition_words
    ):
        return True

    # Årstal efterfulgt af bindestreg og rundeangivelse.
    if re.search(
        r"\b20\d{2}\s+-\s+\d+/\d+",
        low,
    ):
        return True

    return False


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

    if looks_like_competition_text(
        text
    ):
        return False

    left, right = text.split(
        " - ",
        1,
    )

    left = left.strip()
    right = right.strip()

    if not left or not right:
        return False

    # En kamp skal have reel tekst på begge sider.
    if len(left) < 2 or len(right) < 2:
        return False

    return True


# ============================================================
# CHANNEL HELPERS
# ============================================================

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


# ============================================================
# ICS HELPERS
# ============================================================

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


# ============================================================
# SCRAPER
# ============================================================

def scrape_ordered_payload() -> List[str]:

    """
    Hent TV-programmet i DOM-rækkefølge.

    Tekstnoder og IMG alt-attributter placeres i samme sekvens,
    så kanal-logoets alt-tekst bliver ved den kamp, det tilhører.
    """

    with sync_playwright() as playwright:

        browser = (
            playwright.chromium.launch()
        )

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

        # Lazy-loaded content
        previous_height = 0

        for _ in range(15):

            height = page.evaluate(
                """
                () => document.body
                    ? document.body.scrollHeight
                    : 0
                """
            )

            if not height:
                break

            page.evaluate(
                """
                () => window.scrollTo(
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

        ordered_lines: List[str] = (
            page.evaluate(
                """
                () => {
                    const root = document.body;

                    if (!root) {
                        return [];
                    }

                    const result = [];

                    const pushText = (value) => {
                        if (!value) {
                            return;
                        }

                        const parts = value
                            .split(/\\n+/)
                            .map(value => value.trim())
                            .filter(Boolean);

                        for (const part of parts) {
                            result.push(part);
                        }
                    };

                    const walker =
                        document.createTreeWalker(
                            root,
                            NodeFilter.SHOW_ELEMENT |
                            NodeFilter.SHOW_TEXT
                        );

                    let node;

                    while (
                        (node = walker.nextNode())
                    ) {

                        if (
                            node.nodeType ===
                            Node.TEXT_NODE
                        ) {

                            const parent =
                                node.parentElement;

                            if (!parent) {
                                continue;
                            }

                            const tag =
                                parent.tagName;

                            if (
                                tag === "SCRIPT" ||
                                tag === "STYLE" ||
                                tag === "NOSCRIPT"
                            ) {
                                continue;
                            }

                            const style =
                                window.getComputedStyle(
                                    parent
                                );

                            if (
                                style.display === "none" ||
                                style.visibility === "hidden"
                            ) {
                                continue;
                            }

                            pushText(
                                node.textContent
                            );

                            continue;
                        }

                        if (
                            node.nodeType ===
                            Node.ELEMENT_NODE
                        ) {

                            const el = node;

                            if (
                                el.tagName === "IMG"
                            ) {

                                const alt =
                                    (
                                        el.getAttribute(
                                            "alt"
                                        ) || ""
                                    ).trim();

                                if (alt) {
                                    result.push(
                                        alt
                                    );
                                }
                            }
                        }
                    }

                    return result;
                }
                """
            )
        )

        browser.close()

        cleaned: List[str] = []

        for line in ordered_lines:

            line = (
                line or ""
            ).strip()

            if not line:
                continue

            # Kun direkte dubletter fjernes her.
            if (
                cleaned
                and cleaned[-1] == line
            ):
                continue

            cleaned.append(
                line
            )

        print(
            f"Ordered DOM lines: "
            f"{len(cleaned)}"
        )

        if not cleaned:

            raise RuntimeError(
                "No ordered DOM content "
                "could be extracted."
            )

        print(
            "----- RELEVANT DOM PREVIEW -----"
        )

        preview_count = 0

        for line in cleaned:

            relevant = (
                looks_like_weekday_date_header(line)
                or parse_time_line(line)
                or looks_like_match(line)
                or "afspilles" in line.lower()
            )

            if not relevant:
                continue

            print(line)

            preview_count += 1

            if preview_count >= 80:
                break

        print(
            "----- END DOM PREVIEW -----"
        )

        return cleaned


# ============================================================
# MATCH BLOCK
# ============================================================

def collect_match_block(
    lines: List[str],
    match_index: int,
) -> List[str]:

    """
    Samler kun data frem til næste kamp/dato/tid.
    """

    block: List[str] = []

    for line in lines[
        match_index + 1:
    ]:

        line = (
            line or ""
        ).strip()

        if not line:
            continue

        if looks_like_weekday_date_header(
            line
        ):
            break

        if parse_time_line(
            line
        ):
            break

        if looks_like_match(
            line
        ):
            break

        block.append(
            line
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

        line = (
            line or ""
        ).strip()

        if not line:
            continue

        if CHANNEL_RE.search(
            line
        ):
            continue

        if (
            line.lower()
            .startswith("afspilles på")
        ):
            continue

        if (
            line.lower()
            .startswith("logo")
        ):
            continue

        return line

    return ""


# ============================================================
# EVENT KEY
# ============================================================

def event_key(
    event_date: date,
    event_time: tuple[int, int],
    summary: str,
) -> tuple:

    """
    Stabil deduplikeringsnøgle.

    Samme kamp på samme dato og tidspunkt må kun behandles én gang,
    selv om hjemmesiden indeholder flere responsive versioner.
    """

    return (
        event_date.isoformat(),
        event_time[0],
        event_time[1],
        re.sub(
            r"\s+",
            " ",
            summary.strip().lower(),
        ),
    )


# ============================================================
# PARSER
# ============================================================

def parse_lines_to_events(
    lines: List[str],
) -> List[Event]:

    year = datetime.now().year

    events: List[Event] = []

    current_date: Optional[
        date
    ] = None

    current_time: Optional[
        tuple[int, int]
    ] = None

    total_candidates = 0
    unique_matches = 0

    skipped_duplicates = 0
    skipped_not_allowed = 0
    skipped_missing_channel = 0

    skipped_channel_counts = {}

    seen_matches = set()

    for index, line in enumerate(
        lines
    ):

        if looks_like_weekday_date_header(
            line
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

        total_candidates += 1

        summary = (
            line.strip()
        )

        key = event_key(
            current_date,
            current_time,
            summary,
        )

        if key in seen_matches:

            skipped_duplicates += 1

            print(
                "SKIP duplicate: "
                f"{summary}"
            )

            current_time = None

            continue

        seen_matches.add(
            key
        )

        unique_matches += 1

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

            if block:

                print(
                    "  Block: "
                    + " | ".join(
                        block[:10]
                    )
                )

            current_time = None

            continue

        if not is_allowed_channel(
            channel
        ):

            skipped_not_allowed += 1

            channel_key = (
                normalize_channel_key(
                    channel
                )
            )

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

        print(
            "ADD "
            f"{summary} "
            f"[{channel}]"
        )

        current_time = None

    print(
        f"Candidate matches: "
        f"{total_candidates}"
    )

    print(
        f"Unique matches: "
        f"{unique_matches}"
    )

    print(
        f"Allowed events: "
        f"{len(events)}"
    )

    print(
        "Skipped "
        "(duplicates): "
        f"{skipped_duplicates}"
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
            "See diagnostics above."
        )

    parse_lines_to_events.total_candidates = (
        total_candidates
    )

    parse_lines_to_events.unique_matches = (
        unique_matches
    )

    parse_lines_to_events.skipped_duplicates = (
        skipped_duplicates
    )

    parse_lines_to_events.skipped_not_allowed = (
        skipped_not_allowed
    )

    parse_lines_to_events.skipped_missing_channel = (
        skipped_missing_channel
    )

    return events


# ============================================================
# ICS WRITER
# ============================================================

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
            "2026-09-26-ordered-dom-v11"
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
                    "@danskhaandbold.tvprogram"
                ),

                (
                    f"DTSTAMP:"
                    f"{now_utc}"
                ),

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


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    ordered_lines = (
        scrape_ordered_payload()
    )

    events = (
        parse_lines_to_events(
            ordered_lines
        )
    )

    write_ics(
        events
    )

    total_candidates = getattr(
        parse_lines_to_events,
        "total_candidates",
        0,
    )

    unique_matches = getattr(
        parse_lines_to_events,
        "unique_matches",
        0,
    )

    skipped_duplicates = getattr(
        parse_lines_to_events,
        "skipped_duplicates",
        0,
    )

    skipped_not_allowed = getattr(
        parse_lines_to_events,
        "skipped_not_allowed",
        0,
    )

    skipped_missing_channel = getattr(
        parse_lines_to_events,
        "skipped_missing_channel",
        0,
    )

    missing_location = sum(
        1
        for event in events
        if not event.location.strip()
    )

    print(
        "================================"
    )

    print(
        f"Generated {len(events)} "
        f"events → {OUT_FILE}"
    )

    print(
        f"Candidate matches: "
        f"{total_candidates}"
    )

    print(
        f"Unique matches: "
        f"{unique_matches}"
    )

    print(
        "Skipped (duplicates): "
        f"{skipped_duplicates}"
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

    print(
        f"Missing LOCATION: "
        f"{missing_location} / "
        f"{len(events)}"
    )

    print(
        "================================"
    )
