"""Turn the Guest DJ lineup flyer on pinsandwheels.com/events into a subscribable .ics calendar.

Only step 3 uses a model; code decides everything else:
  1. Fetch /events and read the "Upcoming Events" carousel from the JSON Squarespace embeds in the HTML.
  2. Skip slide images already handled (cached by Squarespace image id in data/flyers.json).
  3. Claude transcribes each new image twice; both reads must agree exactly.
  4. Code validates: year from the upload date, every date on a scheduled skate night (weekdays and
     hours parsed from the flyer, else the slide text), no duplicates, at least MIN_NIGHTS nights.
  5. Rebuild docs/pins-and-wheels-dj.ics from every cached lineup (stable UIDs, byte-identical output).
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
import httpx
from bs4 import BeautifulSoup
from icalendar import Calendar, Event
from pydantic import BaseModel

EVENTS_URL = "https://www.pinsandwheels.com/events"
VENUE = "Pins & Wheels at Playland, 8822 McCann Dr, Austin, TX 78757"
TZ = ZoneInfo("America/Chicago")
MODEL = os.environ.get("PNW_MODEL", "claude-sonnet-5-5")
MIN_NIGHTS = 3  # lineup flyers list every skate night of the month (8 or 9); fewer means a different flyer
ROOT = Path(__file__).resolve().parent
STATE = ROOT / "data" / "flyers.json"
ICS = ROOT / "docs" / "pins-and-wheels-dj.ics"
USER_AGENT = "Mozilla/5.0 (compatible; pins-and-wheels-dj-calendar/1.0)"

MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]
WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
HOURS_RE = re.compile(
    r"\b(mon|tue|wed|thu|fri|sat|sun)(?:[a-z]*day)?s?\b\D{0,15}?"  # Tuesday, Tues, Sat, Saturdays
    r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\s*(?:-|–|—|to)\s*"
    r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?",
    re.I,
)

PROMPT = (
    "This image is a flyer from the events page of Pins & Wheels, a roller rink in Austin. "
    "Decide whether it is a guest DJ lineup: a list of dates, each credited to a DJ "
    "(for example 'SOUNDS BY <name>'). If it is, transcribe every date printed on it with the DJ "
    "credited for that date, the month named in the headline, and the opening-hours line exactly as "
    "printed (for example 'TUESDAY 7PM - 12AM | SATURDAY 9PM - 12AM'). Copy DJ names exactly as "
    "printed, including symbols such as Greek letters. Transcribe only what is printed: never add, "
    "infer, or complete dates. If the flyer is not a DJ lineup, set is_dj_lineup to false and leave "
    "the other fields empty."
)


class Night(BaseModel):
    month: int  # 1-12, as printed
    day: int
    dj: str  # the name after "SOUNDS BY", as printed


class Flyer(BaseModel):
    is_dj_lineup: bool
    month_name: str  # month in the headline, e.g. "NOVEMBER"; "" if none
    hours_text: str  # opening-hours line as printed; "" if none
    nights: list[Night]


class Rejected(Exception):
    """Both reads agree but the flyer breaks a lineup rule. Cached, so it is reported once."""


class Unsettled(Exception):
    """The reads disagree or the model stopped early. Not cached, so the next run retries."""


def fetch(url: str) -> httpx.Response:
    resp = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True)
    resp.raise_for_status()
    return resp


def carousel_slides(html: str) -> list[dict]:
    """Every image slide in the page's Squarespace list sections, from their embedded JSON."""
    slides = []
    for el in BeautifulSoup(html, "html.parser").select("[data-current-context]"):
        try:
            items = json.loads(el["data-current-context"]).get("userItems") or []
        except json.JSONDecodeError:
            continue
        for item in items:
            img = item.get("image") or {}
            if not (img.get("id") and img.get("assetUrl")):
                continue
            added = img.get("addedOn")
            uploaded = datetime.fromtimestamp(added / 1000, timezone.utc) if added else datetime.now(timezone.utc)
            slides.append({
                "image_id": img["id"],
                "image_url": img["assetUrl"],
                "filename": img.get("filename") or "",
                "uploaded": uploaded.isoformat(timespec="seconds"),
                "title": item.get("title") or "",
                "text": BeautifulSoup(item.get("description") or "", "html.parser").get_text(" ", strip=True),
            })
    return slides


def parse_hours(text: str) -> dict[int, tuple[time, time]]:
    """'TUESDAY 7PM - 12AM | SATURDAY 9PM - 12AM' -> {1: (19:00, 00:00), 5: (21:00, 00:00)}."""
    def to_time(h: str, m: str | None, ampm: str) -> time:
        return time(int(h) % 12 + (12 if ampm.lower() == "p" else 0), int(m or 0))

    return {
        WEEKDAYS[g[0].lower()]: (to_time(g[1], g[2], g[3]), to_time(g[4], g[5], g[6]))
        for g in HOURS_RE.findall(text or "")
    }


def infer_year(month: int, uploaded: date) -> int:
    """A flyer covers the occurrence of its month nearest to when it was uploaded."""
    return min((uploaded.year - 1, uploaded.year, uploaded.year + 1),
               key=lambda y: abs((date(y, month, 15) - uploaded).days))


def canonical(flyer: Flyer) -> tuple:
    """What two reads must agree on: the lineup verdict, every (month, day, DJ), and the parsed hours."""
    nights = sorted((n.month, n.day, " ".join(n.dj.split()).casefold()) for n in flyer.nights)
    return flyer.is_dj_lineup, nights, sorted(parse_hours(flyer.hours_text).items())


def media_type(data: bytes) -> str:
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"GIF":
        return "image/gif"
    return "image/jpeg"


def read_flyer(client: anthropic.Anthropic, image: bytes) -> Flyer:
    # Server-side refusal fallback exists for Sonnet/Opus on the Claude API, not for Haiku.
    fallback = {} if MODEL.startswith("claude-haiku") else {
        "betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    resp = client.beta.messages.parse(
        model=MODEL,
        max_tokens=16000,
        output_config={"effort": "medium"},
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type(image),
                                         "data": base64.standard_b64encode(image).decode()}},
            {"type": "text", "text": PROMPT},
        ]}],
        output_format=Flyer,
        **fallback,
    )
    if resp.stop_reason != "end_turn" or resp.parsed_output is None:
        raise Unsettled(f"model stopped with {resp.stop_reason!r}")
    return resp.parsed_output


def lineup_nights(flyer: Flyer, slide: dict) -> list[dict]:
    """Deterministic checks and dating. Raises Rejected when the transcription breaks a rule."""
    if len(flyer.nights) < MIN_NIGHTS:
        raise Rejected(f"only {len(flyer.nights)} dated nights; a lineup lists at least {MIN_NIGHTS}")
    months = {n.month for n in flyer.nights}
    if len(months) != 1 or not 1 <= min(months) <= 12:
        raise Rejected(f"nights are not all in one valid month: {sorted(months)}")
    month = months.pop()
    if flyer.month_name and flyer.month_name.strip().lower()[:3] != MONTHS[month - 1][:3]:
        raise Rejected(f"headline says {flyer.month_name!r} but the dates are in {MONTHS[month - 1]}")
    hours = parse_hours(flyer.hours_text) or parse_hours(slide["text"])
    if not hours:
        raise Rejected("no opening hours found on the flyer or in the slide text")

    year = infer_year(month, datetime.fromisoformat(slide["uploaded"]).astimezone(TZ).date())
    nights, seen = [], set()
    for n in sorted(flyer.nights, key=lambda n: n.day):
        try:
            day = date(year, month, n.day)
        except ValueError:
            raise Rejected(f"{MONTHS[month - 1].title()} {n.day} is not a real date") from None
        if day in seen:
            raise Rejected(f"{day} is listed twice")
        if day.weekday() not in hours:
            raise Rejected(f"{day} is a {day:%A}, which has no scheduled skate night")
        dj = " ".join(n.dj.split())
        if not dj:
            raise Rejected(f"{day} has no DJ name")
        seen.add(day)
        start_t, end_t = hours[day.weekday()]
        start = datetime.combine(day, start_t, TZ)
        end = datetime.combine(day + timedelta(days=end_t <= start_t), end_t, TZ)
        nights.append({"date": day.isoformat(), "dj": dj,
                       "start": start.isoformat(), "end": end.isoformat()})
    return nights


def process(client: anthropic.Anthropic, slide: dict) -> dict:
    image = fetch(slide["image_url"]).content
    first, second = read_flyer(client, image), read_flyer(client, image)
    if canonical(first) != canonical(second):
        raise Unsettled(f"two reads disagree: {canonical(first)} vs {canonical(second)}")
    record = {**slide, "model": MODEL, "is_dj_lineup": first.is_dj_lineup, "nights": []}
    if first.is_dj_lineup:
        record["nights"] = lineup_nights(first, slide)
    return record


def build_ics(state: dict) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//pins-and-wheels-dj-calendar//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", "Pins & Wheels DJ Nights")
    cal.add("x-wr-caldesc", "Guest DJs at Adult Skate Night, Pins & Wheels at Playland, Austin. "
                            f"Updated daily from {EVENTS_URL}")
    cal.add("x-wr-timezone", str(TZ))
    cal.add("x-published-ttl", "PT6H")

    # Newer uploads win when two flyers list the same date (a corrected flyer replaces the old one).
    latest: dict[str, tuple[dict, dict]] = {}
    for rec in sorted(state.values(), key=lambda r: r["uploaded"]):
        for night in rec.get("nights", []):
            latest[night["date"]] = (night, rec)

    for day in sorted(latest):
        night, rec = latest[day]
        dj = re.sub(r"^DJ\s+", "", night["dj"], flags=re.I)
        ev = Event()
        ev.add("uid", f"adult-skate-{day}@pins-and-wheels-dj")
        ev.add("dtstamp", datetime.fromisoformat(rec["uploaded"]).astimezone(timezone.utc))
        ev.add("dtstart", datetime.fromisoformat(night["start"]).astimezone(TZ))
        ev.add("dtend", datetime.fromisoformat(night["end"]).astimezone(TZ))
        ev.add("summary", f"DJ {dj} · Adult Skate Night")
        ev.add("location", VENUE)
        ev.add("description", "\n\n".join(filter(None, [
            f"Guest DJ: {dj}", rec.get("text"), f"Lineup flyer: {rec['image_url']}", f"Source: {EVENTS_URL}"])))
        ev.add("url", EVENTS_URL)
        cal.add_component(ev)
    if latest:  # VTIMEZONE covering whole years of events: compact, and stable within a year
        years = [int(day[:4]) for day in latest]
        cal.add_missing_timezones(date(min(years), 1, 1), date(max(years) + 1, 1, 1))
    return cal.to_ical()


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> int:
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    slides = carousel_slides(fetch(EVENTS_URL).text)
    if not slides:
        print(f"ERROR: no carousel slides found on {EVENTS_URL}; the page layout may have changed")
        return 1

    client, problems = None, []
    for slide in slides:
        label = slide["filename"] or slide["image_id"]
        if slide["image_id"] in state:
            print(f"cached     {label}")
            continue
        client = client or anthropic.Anthropic()
        try:
            record = process(client, slide)
        except Rejected as e:
            record = {**slide, "model": MODEL, "is_dj_lineup": True, "nights": [], "rejected": str(e)}
            problems.append(f"REJECTED {label}: {e}. Delete its entry in data/flyers.json to retry.")
        except Unsettled as e:
            problems.append(f"RETRY    {label}: {e}")
            continue
        state[slide["image_id"]] = record
        save_state(state)
        nights = record["nights"]
        print(f"lineup     {label}: {len(nights)} nights, {nights[0]['date']} to {nights[-1]['date']}" if nights
              else f"{'rejected' if record.get('rejected') else 'not lineup'} {label}")

    ICS.parent.mkdir(parents=True, exist_ok=True)
    ICS.write_bytes(build_ics(state))
    print(f"wrote {ICS.relative_to(ROOT)}")
    for p in problems:
        print(p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
