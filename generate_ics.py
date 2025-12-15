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

# Kanaler vi forsøger at finde (både som tekst og i HTML-attributter)
CHANNEL_ANYWHERE_RE = re.compile(
    r"(TV2\s*Sport|TV2\s*Play|TV2\b|DR1\b|DR2\b|TV3\s*Sport)",
    re.IGNORECASE
)

FOOTER_STOP_RE = re.compile(
    r"(Besøg Landsholdshoppen|CVR nummer|danskhaandbold@danskhaandbold\.dk|DanskHåndbold 2025|GDPR)",
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

def find_channel_near_html(raw_html: str, start_pos: int) -> tuple[str, int]:
    """
    Finder første forekomst af en kendt kanal i rå HTML efter start_pos.
    Returnerer (kanal, ny_pos).
    """
    window = raw_html[start_pos:start_pos + 12000]  # søg i et "lokalt" udsnit
    m = CHANNEL_ANYWHERE_RE.search(window)
    if not m:
        return "", start_pos
    channel = norm(m.group(1))
    # flyt cursor frem, så næste søgning ikke rammer samme sted
    new_pos = start_pos + m.end()
    return channel, new_pos

def extract_match_summary_and_notes(block_lines: list[str]) -> tuple[str, str]:
    """
    Returnerer (summary, notes) fra en blok.
    Håndterer:
      - "Hold A - Hold B" på én linje
      - "Hold A", "-", "Hold B" på tre linjer
    """
    # 1) én-linjers kamp
    one_line_match_re = re.compile(r".+\s[–-]\s.+")
    for ln in block_lines:
        if one_line_match_re.match(ln):
            summary = ln
            notes = "\n".join([x for x in block_lines if x != ln]).strip()
            return summary, notes

    # 2) tre-linjers kamp: team, '-', team
    for idx in range(1, len(block_lines) - 1):
        if block_lines[idx] == "-" and block_lines[idx - 1] and block_lines[idx + 1]:
            team1 = block_lines[idx - 1]
            team2 = block_lines[idx + 1]
            summary = f"{team1} - {team2}"

            used = {idx - 1, idx, idx + 1}
            notes_parts = [block_lines[k] for k in range(len(block_lines)) if k
