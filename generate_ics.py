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
    r"^kl\.\s*(\d{1,2}):(\d{2})$",
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


@dataclass
class Candidate:
    event_date: date
    event_time: tuple[int, int]
    summary: str
    description: str
    channel: str


# ============================================================
# BASIC HELPERS
# ============================================================

def parse_date_line(
    line: str,
    assumed_year: int,
) -> Optional[date]:

    text = (
        line or ""
    ).strip().lower()

    words = text.split()

    if not words:
        return None

    if words[0] not in WEEKDAY_WORDS:
        return None

    match = DATE_RE.search(
        text
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

    match = TIME_RE.match(
        (line or "").strip()
    )

    if not match:
        return None

    return (
        int(match.group(1)),
        int(match.group(2)),
    )


def is_channel_line(
    line: str,
) -> bool:

    return (
        CHANNEL_RE.search(
            (line or "").strip()
        )
        is not None
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
    Hent tekst og IMG-alt-attributter i faktisk DOM-rækkefølge.

    Kanal-logoets alt-attribut bevares dermed på samme sted
    i sekvensen som den kamp, logoet tilhører.
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

        # ----------------------------------------------------
        # LAZY LOADING
        # ----------------------------------------------------

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
                "did not contain BODY."
            )

        # ----------------------------------------------------
        # ORDERED DOM
        # ----------------------------------------------------

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
                            .map(v => v.trim())
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

    return cleaned


# ============================================================
# BLOCK EXTRACTION
# ============================================================

def extract_date_time_blocks(
    lines: List[str],
) -> List[tuple]:

    """
    Opdel DOM-sekvensen efter dato + tidspunkt.

    Håndbold.dk indeholder typisk to responsive
    repræsentationer af hver kamp.
    """

    year = datetime.now().year

    blocks = []

    current_date: Optional[
        date
    ] = None

    index = 0

    while index < len(lines):

        line = lines[index]

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

            index += 1

            continue

        parsed_time = (
            parse_time_line(
                line
            )
        )

        if (
            parsed_time
            and current_date
        ):

            content = []

            cursor = (
                index + 1
            )

            while cursor < len(lines):

                next_line = (
                    lines[cursor]
                )

                if parse_date_line(
                    next_line,
                    year,
                ):
                    break

                if parse_time_line(
                    next_line
                ):
                    break

                content.append(
                    next_line
                )

                cursor += 1

            blocks.append(
                (
                    current_date,
                    parsed_time,
                    content,
                )
            )

            index = cursor

            continue

        index += 1

    return blocks


# ============================================================
# BLOCK ANALYSIS
# ============================================================

def find_channel(
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


def remove_channel_lines(
    block: List[str],
) -> List[str]:

    result = []

    for line in block:

        if is_channel_line(
            line
        ):
            continue

        result.append(
            line
        )

    return result


def looks_like_competition(
    text: str,
) -> bool:

    low = (
        text or ""
    ).strip().lower()

    words = [
        "ligaen",
        "herreligaen",
        "kvindeligaen",
        "champions league",
        "european league",
        "euro cup",
        "klub vm",
        "pokalturnering",
        "gruppespil",
        "semifinale",
        "semifinaler",
        "finale",
        "bronzekamp",
        "1/4-final",
        "1/8-final",
        "indl. grupper",
        "indledende grupper",
    ]

    return any(
        word in low
        for word in words
    )


def reconstruct_fragmented_match(
    content: List[str],
) -> Optional[str]:

    """
    Første responsive variant kan være:

        turnering
        hold 1
        -
        hold 2

    Rekonstruér den til:
        hold 1 - hold 2
    """

    for index, line in enumerate(
        content
    ):

        if line.strip() != "-":
            continue

        if index == 0:
            continue

        if index + 1 >= len(content):
            continue

        left = (
            content[index - 1]
            .strip()
        )

        right = (
            content[index + 1]
            .strip()
        )

        if not left or not right:
            continue

        if is_channel_line(
            left
        ):
            continue

        if is_channel_line(
            right
        ):
            continue

        return (
            f"{left} - {right}"
        )

    return None


def find_complete_match_line(
    content: List[str],
) -> Optional[str]:

    """
    Find den samlede responsive kamptekst.

    Hvis et holdnavn selv indeholder " - ", foretrækkes den
    længste plausible linje.
    """

    candidates = []

    for line in content:

        text = (
            line or ""
        ).strip()

        if " - " not in text:
            continue

        if is_channel_line(
            text
        ):
            continue

        if looks_like_competition(
            text
        ):
            continue

        left, right = text.rsplit(
            " - ",
            1,
        )

        if not left.strip():
            continue

        if not right.strip():
            continue

        candidates.append(
            text
        )

    if not candidates:
        return None

    return max(
        candidates,
        key=len,
    )


def find_description(
    content: List[str],
    summary: str,
) -> str:

    for line in content:

        text = (
            line or ""
        ).strip()

        if not text:
            continue

        if text == summary:
            continue

        if text == "-":
            continue

        if is_channel_line(
            text
        ):
            continue

        if looks_like_competition(
            text
        ):
            return text

    return ""


def block_to_candidate(
    event_date: date,
    event_time: tuple[int, int],
    raw_content: List[str],
) -> Optional[Candidate]:

    channel = find_channel(
        raw_content
    )

    content = remove_channel_lines(
        raw_content
    )

    summary = (
        find_complete_match_line(
            content
        )
    )

    if not summary:

        summary = (
            reconstruct_fragmented_match(
                content
            )
        )

    if not summary:
        return None

    description = (
        find_description(
            content,
            summary,
        )
    )

    return Candidate(
        event_date=event_date,
        event_time=event_time,
        summary=summary,
        description=description,
        channel=channel,
    )


# ============================================================
# CANDIDATE NORMALIZATION
# ============================================================

def normalize_summary(
    text: str,
) -> str:

    return re.sub(
        r"\s+",
        " ",
        (text or "")
        .strip()
        .lower(),
    )


def candidate_time_key(
    candidate: Candidate,
) -> tuple:

    """
    Dato + tidspunkt.

    Bruges til at sammenligne de responsive varianter af samme
    kamp.
    """

    return (
        candidate.event_date.isoformat(),
        candidate.event_time[0],
        candidate.event_time[1],
    )


def candidate_exact_key(
    candidate: Candidate,
) -> tuple:

    return (
        candidate.event_date.isoformat(),
        candidate.event_time[0],
        candidate.event_time[1],
        normalize_summary(
            candidate.summary
        ),
    )


def candidate_quality(
    candidate: Candidate,
) -> tuple:

    return (
        1 if candidate.channel else 0,
        1 if candidate.description else 0,
        len(candidate.summary),
    )


# ============================================================
# DEDUPLICATION
# ============================================================

def collapse_prefix_candidates(
    candidates: List[Candidate],
) -> List[Candidate]:

    """
    Løs problemet med holdnavne, der selv indeholder " - ".

    Eksempel fra Håndbold.dk:

        OTP Bank - PICK Szeged (HUN)

    kan fejlagtigt ligne en hel kamp, mens den anden responsive
    variant korrekt indeholder:

        OTP Bank - PICK Szeged (HUN) - GOG

    Hvis to kandidater har samme dato og tidspunkt, samme kanal,
    og den kortere summary er et præfiks af den længere efterfulgt
    af " - ", betragtes den korte som en fragmenteret variant.

    Den længste kandidat beholdes.
    """

    grouped = {}

    for candidate in candidates:

        key = candidate_time_key(
            candidate
        )

        grouped.setdefault(
            key,
            []
        ).append(
            candidate
        )

    result = []

    for _, group in grouped.items():

        keep = [
            True
            for _ in group
        ]

        for i, candidate_a in enumerate(
            group
        ):

            summary_a = normalize_summary(
                candidate_a.summary
            )

            channel_a = normalize_channel_key(
                candidate_a.channel
            )

            for j, candidate_b in enumerate(
                group
            ):

                if i == j:
                    continue

                summary_b = normalize_summary(
                    candidate_b.summary
                )

                channel_b = normalize_channel_key(
                    candidate_b.channel
                )

                if not summary_a:
                    continue

                if not summary_b:
                    continue

                if channel_a != channel_b:
                    continue

                # A skal være den kortere kandidat.
                if len(summary_a) >= len(summary_b):
                    continue

                # Den længere kandidat skal begynde med hele
                # den korte summary efterfulgt af kampseparator.
                expected_prefix = (
                    summary_a
                    + " - "
                )

                if summary_b.startswith(
                    expected_prefix
                ):

                    print(
                        "COLLAPSE fragment: "
                        f"{candidate_a.summary} "
                        "→ "
                        f"{candidate_b.summary}"
                    )

                    keep[i] = False

                    break

        for index, candidate in enumerate(
            group
        ):

            if keep[index]:
                result.append(
                    candidate
                )

    return result


def deduplicate_exact_candidates(
    candidates: List[Candidate],
) -> List[Candidate]:

    """
    Fjern almindelige responsive dubletter med præcis samme
    dato, tidspunkt og summary.
    """

    selected = {}

    for candidate in candidates:

        key = candidate_exact_key(
            candidate
        )

        existing = selected.get(
            key
        )

        if existing is None:

            selected[key] = (
                candidate
            )

            continue

        if (
            candidate_quality(candidate)
            >
            candidate_quality(existing)
        ):

            selected[key] = (
                candidate
            )

    return list(
        selected.values()
    )


def deduplicate_candidates(
    candidates: List[Candidate],
) -> List[Candidate]:

    """
    To trin:

    1. Fjern præcise responsive dubletter.
    2. Fjern fragmenter, hvor et holdnavn selv indeholder " - ".
    """

    exact = (
        deduplicate_exact_candidates(
            candidates
        )
    )

    collapsed = (
        collapse_prefix_candidates(
            exact
        )
    )

    # Et sidste exact-pass er billigt og fungerer som
    # sikkerhedsnet.
    return (
        deduplicate_exact_candidates(
            collapsed
        )
    )


# ============================================================
# PARSER
# ============================================================

def parse_lines_to_events(
    lines: List[str],
) -> List[Event]:

    blocks = (
        extract_date_time_blocks(
            lines
        )
    )

    print(
        f"Date/time blocks: "
        f"{len(blocks)}"
    )

    raw_candidates: List[
        Candidate
    ] = []

    ignored_blocks = 0

    for (
        event_date,
        event_time,
        content,
    ) in blocks:

        candidate = (
            block_to_candidate(
                event_date,
                event_time,
                content,
            )
        )

        if not candidate:

            ignored_blocks += 1

            continue

        raw_candidates.append(
            candidate
        )

    print(
        f"Raw candidates: "
        f"{len(raw_candidates)}"
    )

    print(
        f"Ignored blocks: "
        f"{ignored_blocks}"
    )

    candidates = (
        deduplicate_candidates(
            raw_candidates
        )
    )

    print(
        f"Unique candidates: "
        f"{len(candidates)}"
    )

    events: List[Event] = []

    skipped_not_allowed = 0
    skipped_missing_channel = 0

    skipped_channel_counts = {}

    for candidate in candidates:

        if not candidate.channel:

            skipped_missing_channel += 1

            print(
                "SKIP missing channel: "
                f"{candidate.summary}"
            )

            continue

        if not is_allowed_channel(
            candidate.channel
        ):

            skipped_not_allowed += 1

            channel_key = (
                normalize_channel_key(
                    candidate.channel
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
                f"{candidate.channel}: "
                f"{candidate.summary}"
            )

            continue

        start_dt = datetime(
            candidate.event_date.year,
            candidate.event_date.month,
            candidate.event_date.day,
            candidate.event_time[0],
            candidate.event_time[1],
        )

        end_dt = (
            start_dt
            + timedelta(
                minutes=(
                    DEFAULT_DURATION_MIN
                )
            )
        )

        event = Event(
            summary=(
                candidate.summary
            ),
            start=start_dt,
            end=end_dt,
            location=(
                candidate.channel
            ),
            description=(
                candidate.description
            ),
        )

        events.append(
            event
        )

        print(
            "ADD "
            f"{event.summary} "
            f"[{event.location}]"
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
            "No allowed events were parsed."
        )

    parse_lines_to_events.blocks = (
        len(blocks)
    )

    parse_lines_to_events.raw_candidates = (
        len(raw_candidates)
    )

    parse_lines_to_events.unique_candidates = (
        len(candidates)
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
            "2026-09-26-structured-blocks-v13"
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

    blocks = getattr(
        parse_lines_to_events,
        "blocks",
        0,
    )

    raw_candidates = getattr(
        parse_lines_to_events,
        "raw_candidates",
        0,
    )

    unique_candidates = getattr(
        parse_lines_to_events,
        "unique_candidates",
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
        f"Date/time blocks: "
        f"{blocks}"
    )

    print(
        f"Raw candidates: "
        f"{raw_candidates}"
    )

    print(
        f"Unique candidates: "
        f"{unique_candidates}"
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
