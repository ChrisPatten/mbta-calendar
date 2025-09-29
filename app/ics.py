"""ICS calendar builder utilities."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from icalendar import Calendar, Event, Timezone, TimezoneDaylight, TimezoneStandard, vDuration

EASTERN = ZoneInfo("America/New_York")


@dataclass
class CalendarEvent:
    uid: str
    start: datetime
    end: datetime
    summary: str
    description: str
    location: str
    status: str = "CONFIRMED"


def build_calendar(events: list[CalendarEvent], generated_at: datetime) -> bytes:
    cal = Calendar()
    cal.add("PRODID", "-//mbta-cr-ical//EN")
    cal.add("VERSION", "2.0")
    cal.add("CALSCALE", "GREGORIAN")
    cal.add("METHOD", "PUBLISH")
    cal.add("REFRESH-INTERVAL", vDuration(timedelta(days=1)))
    cal["REFRESH-INTERVAL"].params["VALUE"] = "DURATION"
    cal.add("X-PUBLISHED-TTL", vDuration(timedelta(days=1)))
    cal["X-PUBLISHED-TTL"].params["VALUE"] = "DURATION"
    cal.add_component(_build_vtimezone())

    dtstamp = generated_at.astimezone(EASTERN)

    for ev in events:
        event = Event()
        event.add("UID", ev.uid)
        event.add("SUMMARY", ev.summary)
        event.add("DESCRIPTION", ev.description)
        event.add("LOCATION", ev.location)
        event.add("DTSTAMP", dtstamp)
        event.add("DTSTART", ev.start.astimezone(EASTERN))
        event.add("DTEND", ev.end.astimezone(EASTERN))
        event.add("STATUS", ev.status)
        cal.add_component(event)

    return cal.to_ical()


def build_outage_calendar(message: str, generated_at: datetime) -> bytes:
    all_day = CalendarEvent(
        uid=f"mbta-outage-{generated_at.date().isoformat()}",
        start=generated_at.astimezone(EASTERN).replace(hour=0, minute=0, second=0, microsecond=0),
        end=generated_at.astimezone(EASTERN).replace(hour=23, minute=59, second=0, microsecond=0),
        summary="MBTA Commuter Rail schedule unavailable",
        description=message,
        location="MBTA Commuter Rail",
        status="TENTATIVE",
    )
    cal = Calendar()
    cal.add("PRODID", "-//mbta-cr-ical//EN")
    cal.add("VERSION", "2.0")
    cal.add("CALSCALE", "GREGORIAN")
    cal.add("METHOD", "PUBLISH")
    cal.add("REFRESH-INTERVAL", vDuration(timedelta(days=1)))
    cal["REFRESH-INTERVAL"].params["VALUE"] = "DURATION"
    cal.add("X-PUBLISHED-TTL", vDuration(timedelta(days=1)))
    cal["X-PUBLISHED-TTL"].params["VALUE"] = "DURATION"
    cal.add_component(_build_vtimezone())

    event = Event()
    event.add("UID", all_day.uid)
    event.add("SUMMARY", all_day.summary)
    event.add("DESCRIPTION", all_day.description)
    event.add("LOCATION", all_day.location)
    event.add("DTSTAMP", generated_at.astimezone(EASTERN))
    event.add("DTSTART", all_day.start.date())
    event.add("DTEND", (all_day.start + timedelta(days=1)).date())
    event.add("STATUS", all_day.status)
    event.add("TRANSP", "TRANSPARENT")
    cal.add_component(event)

    return cal.to_ical()


def _build_vtimezone() -> Timezone:
    tz = Timezone()
    tz.add("TZID", "America/New_York")

    standard = TimezoneStandard()
    standard.add("DTSTART", datetime(2023, 11, 5, 2, 0, 0))
    standard.add("TZOFFSETFROM", timedelta(hours=-4))
    standard.add("TZOFFSETTO", timedelta(hours=-5))
    standard.add("RRULE", {"FREQ": "YEARLY", "BYMONTH": 11, "BYDAY": "1SU"})
    standard.add("TZNAME", "EST")

    daylight = TimezoneDaylight()
    daylight.add("DTSTART", datetime(2023, 3, 12, 2, 0, 0))
    daylight.add("TZOFFSETFROM", timedelta(hours=-5))
    daylight.add("TZOFFSETTO", timedelta(hours=-4))
    daylight.add("RRULE", {"FREQ": "YEARLY", "BYMONTH": 3, "BYDAY": "2SU"})
    daylight.add("TZNAME", "EDT")

    tz.add_component(standard)
    tz.add_component(daylight)
    return tz
