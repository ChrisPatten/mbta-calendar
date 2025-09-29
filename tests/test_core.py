from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ics import EASTERN
from app.main import Departure, _departures_to_events
from app.resolve import RouteCandidate, StopCandidate, StopIndex, infer_route_and_directions, slugify_name


class FakeMBTAClient:
    def __init__(self, *, routes, stops_by_route, schedule_payloads, route_details_map):
        self._routes = routes
        self._stops = stops_by_route
        self._schedules = schedule_payloads
        self._route_details = route_details_map

    async def list_commuter_routes(self):
        return self._routes

    async def list_stops(self, route_id: str):
        return self._stops.get(route_id, [])

    async def schedules(self, **kwargs):
        key = (kwargs["route_id"], kwargs["stop_id"], kwargs.get("direction_id"))
        return self._schedules.get(key, ([], {}))

    async def route_details(self, route_id: str):
        return self._route_details[route_id]


@pytest.mark.asyncio
async def test_slugify_and_resolution_exact_match():
    client = FakeMBTAClient(
        routes=[
            {
                "id": "CR-Line",
                "attributes": {
                    "long_name": "Franklin/Foxboro Line",
                    "short_name": "Franklin",
                    "direction_names": ["Inbound", "Outbound"],
                },
            }
        ],
        stops_by_route={
            "CR-Line": [
                {
                    "id": "place-forgp",
                    "attributes": {"name": "Forge Park/495"},
                },
                {
                    "id": "place-sstat",
                    "attributes": {"name": "South Station"},
                },
            ]
        },
        schedule_payloads={},
        route_details_map={
            "CR-Line": {
                "attributes": {
                    "long_name": "Franklin/Foxboro Line",
                    "short_name": "Franklin",
                    "direction_names": ["Inbound", "Outbound"],
                }
            }
        },
    )
    index = StopIndex(client)
    matches = await index.resolve("South Station")
    assert matches
    assert matches[0].slug == "south-station"
    assert slugify_name("Forge Park 495") == "forge-park-495"


@pytest.mark.asyncio
async def test_route_inference_prefers_correct_direction_order():
    route_id = "CR-Line"
    home = StopCandidate(
        stop_id="place-forgp",
        stop_name="Forge Park/495",
        slug="forge-park-495",
        route_id=route_id,
        route_name="Franklin/Foxboro Line",
    )
    work = StopCandidate(
        stop_id="place-sstat",
        stop_name="South Station",
        slug="south-station",
        route_id=route_id,
        route_name="Franklin/Foxboro Line",
    )

    included_trip1 = {("trip", "Trip-1"): {"attributes": {"headsign": "South Station", "direction_id": 0}}}
    included_trip2 = {("trip", "Trip-2"): {"attributes": {"headsign": "Forge Park", "direction_id": 1}}}

    schedules = {
        (route_id, home.stop_id, None): (
            [
                {
                    "attributes": {
                        "departure_time": "2024-04-01T07:15:00-04:00",
                        "stop_sequence": 3,
                    },
                    "relationships": {"trip": {"data": {"id": "Trip-1"}}},
                },
                {
                    "attributes": {
                        "departure_time": "2024-04-01T17:20:00-04:00",
                        "stop_sequence": 7,
                    },
                    "relationships": {"trip": {"data": {"id": "Trip-2"}}},
                },
            ],
            {**included_trip1, **included_trip2},
        ),
        (route_id, work.stop_id, None): (
            [
                {
                    "attributes": {
                        "arrival_time": "2024-04-01T08:05:00-04:00",
                        "stop_sequence": 10,
                    },
                    "relationships": {"trip": {"data": {"id": "Trip-1"}}},
                },
                {
                    "attributes": {
                        "arrival_time": "2024-04-01T17:55:00-04:00",
                        "stop_sequence": 2,
                    },
                    "relationships": {"trip": {"data": {"id": "Trip-2"}}},
                },
            ],
            {**included_trip1, **included_trip2},
        ),
    }

    client = FakeMBTAClient(
        routes=[
            {
                "id": route_id,
                "attributes": {
                    "long_name": "Franklin/Foxboro Line",
                    "short_name": "Franklin",
                    "direction_names": ["Inbound", "Outbound"],
                },
            }
        ],
        stops_by_route={route_id: []},
        schedule_payloads=schedules,
        route_details_map={
            route_id: {
                "attributes": {
                    "long_name": "Franklin/Foxboro Line",
                    "short_name": "Franklin",
                    "direction_names": ["Inbound", "Outbound"],
                }
            }
        },
    )

    start = datetime(2024, 4, 1, 0, 0, tzinfo=ZoneInfo("America/New_York"))
    end = start + timedelta(days=1)

    route, toward_work, toward_home = await infer_route_and_directions(home, work, start, end, client)
    assert route.route_id == route_id
    assert toward_work == 0
    assert toward_home == 1


def _make_departure(hour: int, minute: int, direction: int, trip_id: str) -> Departure:
    dt = datetime(2024, 4, 1, hour, minute, tzinfo=EASTERN)
    return Departure(
        trip_id=trip_id,
        departure=dt,
        arrival=dt + timedelta(minutes=30),
        stop_sequence=5,
        direction_id=direction,
        headsign="South Station",
        service_date=dt,
        origin_stop_id="place-forgp",
        destination_stop_id="place-sstat",
    )


def test_noon_partition_and_uid_stability():
    route = RouteCandidate(
        route_id="CR-Line",
        long_name="Franklin/Foxboro Line",
        short_name="Franklin",
        direction_names=["Inbound", "Outbound"],
    )
    home = StopCandidate(
        stop_id="place-forgp",
        stop_name="Forge Park/495",
        slug="forge-park-495",
        route_id="CR-Line",
        route_name="Franklin/Foxboro Line",
    )
    work = StopCandidate(
        stop_id="place-sstat",
        stop_name="South Station",
        slug="south-station",
        route_id="CR-Line",
        route_name="Franklin/Foxboro Line",
    )

    morning = [_make_departure(8, 15, 0, "Trip-1"), _make_departure(12, 0, 0, "Trip-2")]
    evening = [_make_departure(17, 30, 1, "Trip-3"), _make_departure(11, 59, 1, "Trip-4")]

    morning_events = _departures_to_events(route, origin=home, destination=work, departures=morning, before_noon=True)
    assert len(morning_events) == 1
    assert morning_events[0].uid.endswith("Trip-1-place-forgp-2024-04-01")

    evening_events = _departures_to_events(route, origin=work, destination=home, departures=evening, before_noon=False)
    assert len(evening_events) == 1
    assert evening_events[0].uid.endswith("Trip-3-place-sstat-2024-04-01")

    # UID stability: running conversion again produces same UID
    second_pass = _departures_to_events(route, origin=work, destination=home, departures=evening, before_noon=False)
    assert second_pass[0].uid == evening_events[0].uid
