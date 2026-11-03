"""Deterministic pipeline tests: the saved Nov 3 2026 page plus a fake model transcription."""

from datetime import date, time
from pathlib import Path

import pytest

import pnw_dj
from pnw_dj import Flyer, Night

FIXTURE = Path(__file__).parent / "fixtures" / "events_2026-11-03.html"

NOVEMBER = Flyer(
    is_dj_lineup=True,
    month_name="NOVEMBER",
    hours_text="TUESDAY 7PM - 12AM | SATURDAY 9PM - 12AM",
    nights=[Night(month=11, day=d, dj=dj) for d, dj in [
        (3, "SWAY"), (7, "NET WT"), (10, "G"), (14, "E"),
        (17, "SWAY"), (21, "NET WT"), (24, "LOUIS"), (28, "JAH")]],
)

# Tuesdays 7PM-12AM, Saturdays 9PM-12AM, Central Standard Time (DST ended Nov 1 2026).
EXPECTED = [
    ("2026-11-03", "SWAY", "2026-11-03T19:00:00-06:00", "2026-11-04T00:00:00-06:00"),
    ("2026-11-07", "NET WT", "2026-11-07T21:00:00-06:00", "2026-11-08T00:00:00-06:00"),
    ("2026-11-10", "G", "2026-11-10T19:00:00-06:00", "2026-11-11T00:00:00-06:00"),
    ("2026-11-14", "E", "2026-11-14T21:00:00-06:00", "2026-11-15T00:00:00-06:00"),
    ("2026-11-17", "SWAY", "2026-11-17T19:00:00-06:00", "2026-11-18T00:00:00-06:00"),
    ("2026-11-21", "NET WT", "2026-11-21T21:00:00-06:00", "2026-11-22T00:00:00-06:00"),
    ("2026-11-24", "LOUIS", "2026-11-24T19:00:00-06:00", "2026-11-25T00:00:00-06:00"),
    ("2026-11-28", "JAH", "2026-11-28T21:00:00-06:00", "2026-11-29T00:00:00-06:00"),
]


def dj_slide() -> dict:
    slides = pnw_dj.carousel_slides(FIXTURE.read_text())
    return next(s for s in slides if s["filename"] == "Guest DJ Line Up (2).png")


def run_process(monkeypatch, *reads: Flyer) -> dict:
    replies = iter(reads)
    monkeypatch.setattr(pnw_dj, "fetch", lambda url: type("Resp", (), {"content": b"img"})())
    monkeypatch.setattr(pnw_dj, "read_flyer", lambda client, image: next(replies))
    return pnw_dj.process(None, dj_slide())


def test_carousel_slides_come_from_embedded_json():
    slides = pnw_dj.carousel_slides(FIXTURE.read_text())
    assert len(slides) == 6
    slide = dj_slide()
    assert slide["image_id"] == "6ae2094998c45716b8d10007"
    assert slide["uploaded"].startswith("2026-10-28")
    assert "every Tuesday from 7PM–12AM and Saturday from 9PM–12AM" in slide["text"]


@pytest.mark.parametrize("text", [
    "TUESDAY 7PM - 12AM | SATURDAY 9PM - 12AM",  # flyer footer
    "Adult Skate Night takes place every Tuesday from 7PM–12AM and Saturday from 9PM–12AM",  # slide text
])
def test_parse_hours(text):
    assert pnw_dj.parse_hours(text) == {1: (time(19), time(0)), 5: (time(21), time(0))}


def test_parse_hours_ignores_non_weekday_words():
    assert pnw_dj.parse_hours("open every month from 7PM - 12AM") == {}
    assert pnw_dj.parse_hours("Tues 7:30pm to 11pm, Sat 9PM-12AM") == {1: (time(19, 30), time(23)), 5: (time(21), time(0))}


def test_infer_year_uses_upload_date():
    assert pnw_dj.infer_year(11, date(2026, 10, 28)) == 2026  # posted a few days early
    assert pnw_dj.infer_year(5, date(2026, 5, 5)) == 2026  # posted after the month started
    assert pnw_dj.infer_year(1, date(2026, 12, 29)) == 2027  # January flyer posted in December


def test_november_lineup_end_to_end(monkeypatch):
    record = run_process(monkeypatch, NOVEMBER, NOVEMBER)
    assert [(n["date"], n["dj"], n["start"], n["end"]) for n in record["nights"]] == EXPECTED

    ics = pnw_dj.build_ics({record["image_id"]: record})
    assert ics == pnw_dj.build_ics({record["image_id"]: record})  # byte-stable: no churn in git
    text = ics.decode()
    assert text.count("BEGIN:VEVENT") == 8
    assert "BEGIN:VTIMEZONE" in text
    assert "DTSTART;TZID=America/Chicago:20261103T190000" in text
    assert "DTEND;TZID=America/Chicago:20261104T000000" in text
    assert "SUMMARY:DJ SWAY · Adult Skate Night" in text
    assert "UID:adult-skate-2026-11-03@pins-and-wheels-dj" in text


def test_date_off_schedule_is_rejected(monkeypatch):
    sunday = NOVEMBER.model_copy(update={"nights": [*NOVEMBER.nights[1:], Night(month=11, day=8, dj="SWAY")]})
    with pytest.raises(pnw_dj.Rejected, match="Sunday"):
        run_process(monkeypatch, sunday, sunday)


def test_disagreeing_reads_are_retried_not_published(monkeypatch):
    misread = NOVEMBER.model_copy(update={"nights": [*NOVEMBER.nights[:-1], Night(month=11, day=28, dj="JAY")]})
    with pytest.raises(pnw_dj.Unsettled, match="disagree"):
        run_process(monkeypatch, NOVEMBER, misread)


def test_non_lineup_flyer_produces_no_events(monkeypatch):
    halloween = Flyer(is_dj_lineup=False, month_name="", hours_text="", nights=[])
    record = run_process(monkeypatch, halloween, halloween)
    assert record["is_dj_lineup"] is False and record["nights"] == []
