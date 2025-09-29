"""FastAPI application serving MBTA commuter rail iCalendar feeds."""
from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import datetime, time, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import Response

from .cache import TTLCache
from .ics import CalendarEvent, EASTERN, build_calendar, build_outage_calendar
from .mbta import MBTAAPIError, MBTAClient
from .resolve import RouteCandidate, StopCandidate, StopIndex, infer_route_and_directions

logger = logging.getLogger(__name__)

DEFAULT_SCHEDULE_TTL = 300.0
MAX_EVENTS_PER_DAY = 8
NOON = time(12, 0)
MBTA_TRIP_LINK = "https://www.mbta.com/schedules/{route}/line?trip={trip}"


def configure_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def create_app() -> FastAPI:
    configure_logging()

    async def lifespan(app: FastAPI):
        base_url = os.getenv("MBTA_API_URL", "https://api-v3.mbta.com")
        api_key = os.getenv("MBTA_API_KEY")
        client = MBTAClient(base_url=base_url, api_key=api_key)
        app.state.mbta_client = client
        app.state.stop_index = StopIndex(client)
        app.state.schedule_cache = TTLCache(default_ttl=DEFAULT_SCHEDULE_TTL)
        yield
        await client.close()

    return FastAPI(title="MBTA CR iCal", version="1.0.0", lifespan=lifespan)


app = create_app()


async def get_client() -> MBTAClient:
    return app.state.mbta_client


async def get_stop_index() -> StopIndex:
    return app.state.stop_index


async def get_schedule_cache() -> TTLCache:
    return app.state.schedule_cache


@app.get("/healthz")
async def healthz() -> Dict[str, bool]:
    return {"ok": True}


@app.get("/stops")
async def stops(
    query: str = Query(..., description="Partial stop name or slug"),
    index: StopIndex = Depends(get_stop_index),
) -> List[Dict[str, str]]:
    matches = await index.resolve(query)
    return [
        {
            "stop_id": match.stop_id,
            "name": match.stop_name,
            "slug": match.slug,
            "route_id": match.route_id,
            "route_name": match.route_name,
        }
        for match in matches
    ]


@app.get("/schedule.ical")
async def schedule_ical(
    home_stop: Optional[str] = Query(None, description="Home origin stop slug/name"),
    work_stop: Optional[str] = Query(None, description="Work destination stop slug/name"),
    days: Optional[int] = Query(None, ge=1, le=30, description="Number of days to include"),
    force_refresh: Optional[int] = Query(0, description="Bypass caches when set"),
    *,
    client: MBTAClient = Depends(get_client),
    index: StopIndex = Depends(get_stop_index),
    cache: TTLCache = Depends(get_schedule_cache),
) -> Response:
    home_query = home_stop or os.getenv("DEFAULT_HOME_STOP")
    work_query = work_stop or os.getenv("DEFAULT_WORK_STOP")

    if not home_query or not work_query:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "missing_parameters",
                "message": "home_stop and work_stop are required",
            },
        )

    home_candidates = await index.resolve(home_query)
    work_candidates = await index.resolve(work_query)

    if not home_candidates:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "home_stop_not_found",
                "query": home_query,
                "suggestions": [],
            },
        )
    if not work_candidates:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "work_stop_not_found",
                "query": work_query,
                "suggestions": [],
            },
        )

    home_choice, work_choice = _select_pair(home_candidates, work_candidates)
    if home_choice is None or work_choice is None:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "route_not_found",
                "message": "Could not find a commuter rail route containing both stops.",
            },
        )

    now = datetime.now(tz=EASTERN)
    inference_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    window_days = days if days is not None else 14
    window_end = (now + timedelta(days=window_days)).replace(hour=23, minute=59, second=59, microsecond=0)

    try:
        route, toward_work, toward_home = await infer_route_and_directions(
            home_choice, work_choice, inference_start, window_end, client
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": "route_unresolved", "message": str(exc)})
    except MBTAAPIError as exc:
        return _service_unavailable(str(exc), now)

    try:
        morning_departures = await _fetch_departures(
            cache,
            client,
            route_id=route.route_id,
            stop=home_choice,
            destination=work_choice,
            direction_id=toward_work,
            window_start=now,
            window_end=window_end,
            force_refresh=bool(force_refresh),
        )
        evening_departures = await _fetch_departures(
            cache,
            client,
            route_id=route.route_id,
            stop=work_choice,
            destination=home_choice,
            direction_id=toward_home,
            window_start=now,
            window_end=window_end,
            force_refresh=bool(force_refresh),
        )
    except MBTAAPIError as exc:
        return _service_unavailable(str(exc), now)

    events = _build_events(
        route,
        home_choice,
        work_choice,
        morning_departures,
        evening_departures,
    )

    ical_bytes = build_calendar(events, now)
    response = Response(content=ical_bytes, media_type="text/calendar; charset=utf-8")
    response.headers["Cache-Control"] = "no-store"
    return response


def _select_pair(
    home_candidates: Iterable[StopCandidate], work_candidates: Iterable[StopCandidate]
) -> Tuple[Optional[StopCandidate], Optional[StopCandidate]]:
    for home in home_candidates:
        for work in work_candidates:
            if home.route_id == work.route_id:
                return home, work
    # fallback: return first entries for clarity
    return None, None


class Departure:
    __slots__ = (
        "trip_id",
        "departure",
        "arrival",
        "stop_sequence",
        "direction_id",
        "headsign",
        "service_date",
        "origin_stop_id",
        "destination_stop_id",
    )

    def __init__(
        self,
        *,
        trip_id: str,
        departure: datetime,
        arrival: Optional[datetime],
        stop_sequence: int,
        direction_id: int,
        headsign: str,
        service_date: datetime,
        origin_stop_id: str,
        destination_stop_id: str,
    ) -> None:
        self.trip_id = trip_id
        self.departure = departure
        self.arrival = arrival
        self.stop_sequence = stop_sequence
        self.direction_id = direction_id
        self.headsign = headsign
        self.service_date = service_date
        self.origin_stop_id = origin_stop_id
        self.destination_stop_id = destination_stop_id


async def _fetch_departures(
    cache: TTLCache,
    client: MBTAClient,
    *,
    route_id: str,
    stop: StopCandidate,
    destination: StopCandidate,
    direction_id: int,
    window_start: datetime,
    window_end: datetime,
    force_refresh: bool,
) -> List[Departure]:
    key = (
        route_id,
        stop.stop_id,
        destination.stop_id,
        direction_id,
        window_start.date().isoformat(),
        window_end.date().isoformat(),
    )
    if force_refresh:
        cache.invalidate(key)
    cached = cache.get(key)
    if cached is not None:
        return cached

    schedules, included = await client.schedules(
        route_id=route_id,
        stop_id=stop.stop_id,
        direction_id=direction_id,
        start=window_start,
        end=window_end,
    )
    dest_schedules, _ = await client.schedules(
        route_id=route_id,
        stop_id=destination.stop_id,
        direction_id=direction_id,
        start=window_start,
        end=window_end,
    )

    dest_arrivals = _build_arrival_map(dest_schedules)

    departures: List[Departure] = []
    for item in schedules:
        attributes = item.get("attributes", {})
        departure_raw = attributes.get("departure_time") or attributes.get("arrival_time")
        if not departure_raw:
            continue
        try:
            departure_dt = datetime.fromisoformat(departure_raw)
        except ValueError:
            continue
        departure_local = departure_dt.astimezone(EASTERN)
        relationships = item.get("relationships", {})
        trip_rel = relationships.get("trip", {}) if isinstance(relationships, dict) else {}
        trip_data = trip_rel.get("data", {}) if isinstance(trip_rel, dict) else {}
        trip_id = trip_data.get("id")
        if not trip_id:
            continue
        trip_info = included.get(("trip", trip_id), {})
        trip_attrs = trip_info.get("attributes", {}) if isinstance(trip_info, dict) else {}
        headsign = trip_attrs.get("headsign") or trip_attrs.get("name") or stop.route_name
        dir_id = trip_attrs.get("direction_id")
        direction_value = direction_id if not isinstance(dir_id, int) else dir_id
        stop_sequence = attributes.get("stop_sequence") or 0
        arrival_local = dest_arrivals.get(trip_id)
        departures.append(
            Departure(
                trip_id=trip_id,
                departure=departure_local,
                arrival=arrival_local,
                stop_sequence=int(stop_sequence),
                direction_id=direction_value,
                headsign=headsign,
                service_date=departure_local,
                origin_stop_id=stop.stop_id,
                destination_stop_id=destination.stop_id,
            )
        )

    departures.sort(key=lambda d: d.departure)
    cache.set(key, departures)
    return departures


def _build_arrival_map(data: List[Dict[str, object]]) -> Dict[str, datetime]:
    arrivals: Dict[str, datetime] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        relationships = item.get("relationships", {})
        trip = relationships.get("trip", {}) if isinstance(relationships, dict) else {}
        trip_id = trip.get("data", {}).get("id") if isinstance(trip, dict) else None
        if not trip_id:
            continue
        attributes = item.get("attributes", {})
        if not isinstance(attributes, dict):
            continue
        raw_time = attributes.get("arrival_time") or attributes.get("departure_time")
        if not isinstance(raw_time, str):
            continue
        try:
            dt = datetime.fromisoformat(raw_time).astimezone(EASTERN)
        except ValueError:
            continue
        current = arrivals.get(trip_id)
        if current is None or dt > current:
            arrivals[trip_id] = dt
    return arrivals


def _build_events(
    route: RouteCandidate,
    home: StopCandidate,
    work: StopCandidate,
    morning: List[Departure],
    evening: List[Departure],
) -> List[CalendarEvent]:
    events: List[CalendarEvent] = []
    events.extend(
        _departures_to_events(
            route,
            origin=home,
            destination=work,
            departures=morning,
            before_noon=True,
        )
    )
    events.extend(
        _departures_to_events(
            route,
            origin=work,
            destination=home,
            departures=evening,
            before_noon=False,
        )
    )
    return events


def _departures_to_events(
    route: RouteCandidate,
    *,
    origin: StopCandidate,
    destination: StopCandidate,
    departures: List[Departure],
    before_noon: bool,
) -> List[CalendarEvent]:
    grouped: Dict[str, List[Departure]] = defaultdict(list)
    for departure in departures:
        local_dt = departure.departure
        if before_noon and local_dt.time() >= NOON:
            continue
        if not before_noon and local_dt.time() < NOON:
            continue
        service_key = local_dt.date().isoformat()
        grouped[service_key].append(departure)

    events: List[CalendarEvent] = []
    for service_date, per_day in grouped.items():
        per_day.sort(key=lambda d: d.departure)
        limited = per_day[:MAX_EVENTS_PER_DAY]
        for dep in limited:
            if dep.arrival:
                end_time = dep.arrival if dep.arrival > dep.departure else dep.departure + timedelta(minutes=1)
            else:
                end_time = dep.departure + timedelta(minutes=5)
            direction_name = _direction_name(route, dep.direction_id)
            time_str = dep.departure.strftime("%I:%M %p").lstrip("0")
            route_label = route.short_name or route.long_name
            summary = f"CR {route_label} – Trip {dep.trip_id} – {direction_name} – {time_str}"
            description_lines = [
                f"Route: {route.long_name}",
                f"Origin: {origin.stop_name}",
                f"Destination: {destination.stop_name}",
                f"Headsign: {dep.headsign}",
                f"Direction: {direction_name}",
                f"Trip: {dep.trip_id}",
                f"Stop sequence: {dep.stop_sequence}",
                f"Link: {MBTA_TRIP_LINK.format(route=route.route_id, trip=dep.trip_id)}",
            ]
            events.append(
                CalendarEvent(
                    uid=f"mbta-{route.route_id}-{dep.trip_id}-{dep.origin_stop_id}-{service_date}",
                    start=dep.departure,
                    end=end_time,
                    summary=summary,
                    description="\n".join(description_lines),
                    location=f"{route.long_name} – {origin.stop_name}",
                )
            )
    return events


def _direction_name(route: RouteCandidate, direction_id: int) -> str:
    if 0 <= direction_id < len(route.direction_names):
        label = route.direction_names[direction_id]
        if isinstance(label, str) and label.strip():
            return label
    return "Inbound" if direction_id == 0 else "Outbound"


def _service_unavailable(reason: str, generated_at: datetime) -> Response:
    ical = build_outage_calendar(reason, generated_at)
    response = Response(content=ical, media_type="text/calendar; charset=utf-8", status_code=503)
    response.headers["Cache-Control"] = "no-store"
    return response
