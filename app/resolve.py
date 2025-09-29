"""Utilities for stop resolution and route inference."""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from time import monotonic
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .mbta import MBTAClient

logger = logging.getLogger(__name__)

_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")
_INDEX_TTL = 60 * 60 * 6  # 6 hours


def slugify_name(value: str) -> str:
    slug = _SLUG_PATTERN.sub("-", value.lower().strip())
    slug = slug.strip("-")
    slug = re.sub(r"-+", "-", slug)
    return slug


@dataclass(frozen=True)
class StopCandidate:
    stop_id: str
    stop_name: str
    slug: str
    route_id: str
    route_name: str


@dataclass(frozen=True)
class RouteCandidate:
    route_id: str
    long_name: str
    short_name: Optional[str]
    direction_names: Sequence[str]


class StopIndex:
    """Indexes commuter rail stops grouped by route with slug lookup."""

    def __init__(self, client: MBTAClient) -> None:
        self._client = client
        self._lock = asyncio.Lock()
        self._expires_at = 0.0
        self._slug_map: Dict[str, List[StopCandidate]] = {}
        self._all_candidates: List[StopCandidate] = []
        self._routes: Dict[str, RouteCandidate] = {}

    async def ensure_index(self, force_refresh: bool = False) -> None:
        now = monotonic()
        if not force_refresh and now < self._expires_at:
            return
        async with self._lock:
            if not force_refresh and now < self._expires_at:
                return
            await self._refresh()
            self._expires_at = monotonic() + _INDEX_TTL

    async def _refresh(self) -> None:
        routes = await self._client.list_commuter_routes()
        slug_map: Dict[str, List[StopCandidate]] = {}
        candidates: List[StopCandidate] = []
        route_map: Dict[str, RouteCandidate] = {}

        for route in routes:
            route_id = route.get("id")
            attributes = route.get("attributes", {})
            long_name = attributes.get("long_name") or attributes.get("description") or route_id
            short_name = attributes.get("short_name")
            direction_names = attributes.get("direction_names") or []
            route_map[route_id] = RouteCandidate(
                route_id=route_id,
                long_name=long_name,
                short_name=short_name,
                direction_names=direction_names,
            )

        async def load_stops(route: RouteCandidate) -> None:
            data = await self._client.list_stops(route.route_id)
            for stop in data:
                stop_id = stop.get("id")
                attributes = stop.get("attributes", {})
                name = attributes.get("name") or attributes.get("platform_name") or stop_id
                slug = slugify_name(name)
                candidate = StopCandidate(
                    stop_id=stop_id,
                    stop_name=name,
                    slug=slug,
                    route_id=route.route_id,
                    route_name=route.long_name,
                )
                slug_map.setdefault(slug, []).append(candidate)
                candidates.append(candidate)

        await asyncio.gather(*(load_stops(route) for route in route_map.values()))

        self._slug_map = slug_map
        self._all_candidates = candidates
        self._routes = route_map

    async def resolve(self, query: str) -> List[StopCandidate]:
        await self.ensure_index()
        query = query.strip()
        if not query:
            return []
        slug = slugify_name(query)
        matches: List[StopCandidate] = []

        if slug and slug in self._slug_map:
            matches.extend(self._slug_map[slug])

        lowered = query.lower()
        contains = [c for c in self._all_candidates if lowered in c.stop_name.lower()]
        for candidate in contains:
            if candidate not in matches:
                matches.append(candidate)

        if not matches:
            nearest = self._closest_candidates(query)
            matches.extend(nearest)

        return matches[:10]

    def route_candidate(self, route_id: str) -> Optional[RouteCandidate]:
        return self._routes.get(route_id)

    def _closest_candidates(self, query: str, limit: int = 5) -> List[StopCandidate]:
        slug_query = slugify_name(query)
        scored: List[Tuple[float, StopCandidate]] = []
        for candidate in self._all_candidates:
            score = _levenshtein_ratio(slug_query, candidate.slug)
            if score <= 0.6:
                continue
            scored.append((score, candidate))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [candidate for _, candidate in scored[:limit]]


def _levenshtein_ratio(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    len_a, len_b = len(a), len(b)
    dist = [[0] * (len_b + 1) for _ in range(len_a + 1)]
    for i in range(len_a + 1):
        dist[i][0] = i
    for j in range(len_b + 1):
        dist[0][j] = j
    for i in range(1, len_a + 1):
        for j in range(1, len_b + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            dist[i][j] = min(
                dist[i - 1][j] + 1,
                dist[i][j - 1] + 1,
                dist[i - 1][j - 1] + cost,
            )
    lev = dist[len_a][len_b]
    return 1.0 - lev / max(len_a, len_b)


async def infer_route_and_directions(
    home: StopCandidate,
    work: StopCandidate,
    window_start: datetime,
    window_end: datetime,
    client: MBTAClient,
) -> Tuple[RouteCandidate, int, int]:
    """Identify the commuter rail route and direction ids for given stops."""

    if home.route_id != work.route_id:
        candidates = [home.route_id, work.route_id]
    else:
        candidates = [home.route_id]

    seen = set()
    ordered: List[str] = []
    for route in candidates:
        if route in seen:
            continue
        seen.add(route)
        ordered.append(route)

    # Prefer routes that mention the downtown terminals if ambiguous.
    def _route_sort_key(route_id: str) -> Tuple[int, str]:
        route_details = client_route_cache.get(route_id)
        name = route_details.long_name if route_details else ""
        priority = 0
        normalized = name.lower()
        if "south station" in normalized or "north station" in normalized:
            priority = -1
        return (priority, route_id)

    client_route_cache: Dict[str, RouteCandidate] = {}
    for rid in ordered:
        candidate = client_route_cache.get(rid)
        if candidate is None:
            route_info = await client.route_details(rid)
            attributes = route_info.get("attributes", {})
            direction_names = attributes.get("direction_names") or []
            long_name = attributes.get("long_name") or attributes.get("description") or rid
            short_name = attributes.get("short_name")
            candidate = RouteCandidate(
                route_id=rid,
                long_name=long_name,
                short_name=short_name,
                direction_names=direction_names,
            )
            client_route_cache[rid] = candidate
    ordered.sort(key=_route_sort_key)

    for route_id in ordered:
        route_candidate = client_route_cache[route_id]
        direction = await _find_directions_for_route(
            client=client,
            route=route_candidate,
            home=home,
            work=work,
            window_start=window_start,
            window_end=window_end,
        )
        if direction is not None:
            toward_work, toward_home = direction
            return route_candidate, toward_work, toward_home

    raise ValueError("Unable to infer a route that connects both stops")


async def _find_directions_for_route(
    *,
    client: MBTAClient,
    route: RouteCandidate,
    home: StopCandidate,
    work: StopCandidate,
    window_start: datetime,
    window_end: datetime,
) -> Optional[Tuple[int, int]]:
    try:
        home_sched, home_included = await client.schedules(
            route_id=route.route_id,
            stop_id=home.stop_id,
            direction_id=None,
            start=window_start,
            end=window_end,
        )
        work_sched, work_included = await client.schedules(
            route_id=route.route_id,
            stop_id=work.stop_id,
            direction_id=None,
            start=window_start,
            end=window_end,
        )
    except Exception as exc:  # pragma: no cover - network issues logged upstream
        logger.warning("schedule lookup failed for route %s: %s", route.route_id, exc)
        return None

    combined_included = dict(home_included)
    combined_included.update(work_included)

    home_map = _trip_stop_sequence_map(home_sched, combined_included)
    work_map = _trip_stop_sequence_map(work_sched, combined_included)

    for trip_id, home_info in home_map.items():
        work_info = work_map.get(trip_id)
        if work_info is None:
            continue
        if not _home_precedes_work(home_info, work_info):
            continue
        direction_id = _pick_direction(route, home, work, home_info, work_info)
        if direction_id is None:
            continue
        return direction_id, 1 - direction_id
    return None


@dataclass
class _TripStopInfo:
    stop_sequence: Optional[int]
    departure_time: Optional[datetime]
    arrival_time: Optional[datetime]
    direction_id: Optional[int]
    headsign: Optional[str]


def _trip_stop_sequence_map(
    data: Sequence[Dict[str, object]],
    included: Dict[Tuple[str, str], Dict[str, object]],
) -> Dict[str, _TripStopInfo]:
    mapping: Dict[str, _TripStopInfo] = {}
    for item in data:
        relationships = item.get("relationships", {})
        trip = relationships.get("trip", {}) if isinstance(relationships, dict) else {}
        trip_id = trip.get("data", {}).get("id") if isinstance(trip, dict) else None
        attributes = item.get("attributes", {}) if isinstance(item, dict) else {}
        if trip_id and isinstance(attributes, dict):
            seq = attributes.get("stop_sequence")
            dep_raw = attributes.get("departure_time")
            arr_raw = attributes.get("arrival_time")
            trip_info = included.get(("trip", trip_id))
            direction_id = None
            headsign = None
            if isinstance(trip_info, dict):
                trip_attr = trip_info.get("attributes", {})
                if isinstance(trip_attr, dict):
                    direction = trip_attr.get("direction_id")
                    if isinstance(direction, int):
                        direction_id = direction
                    headsign_value = trip_attr.get("headsign") or trip_attr.get("name")
                    if isinstance(headsign_value, str):
                        headsign = headsign_value
            mapping[trip_id] = _TripStopInfo(
                stop_sequence=int(seq) if isinstance(seq, int) else None,
                departure_time=_parse_time(dep_raw),
                arrival_time=_parse_time(arr_raw),
                direction_id=direction_id,
                headsign=headsign,
            )
    return mapping


def _parse_time(value: object) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _home_precedes_work(home: _TripStopInfo, work: _TripStopInfo) -> bool:
    if home.stop_sequence is not None and work.stop_sequence is not None:
        if home.stop_sequence < work.stop_sequence:
            return True
    if home.departure_time and work.departure_time:
        if home.departure_time <= work.departure_time:
            return True
    if home.departure_time and work.arrival_time:
        if home.departure_time <= work.arrival_time:
            return True
    return False


def _pick_direction(
    route: RouteCandidate,
    home_stop: StopCandidate,
    work_stop: StopCandidate,
    home: _TripStopInfo,
    work: _TripStopInfo,
) -> Optional[int]:
    if home.direction_id is not None:
        return home.direction_id
    if work.direction_id is not None:
        return work.direction_id
    for info in (home, work):
        headsign = (info.headsign or "").lower()
        if "south station" in headsign or "north station" in headsign:
            return 0
    home_name = home_stop.stop_name.lower()
    work_name = work_stop.stop_name.lower()
    if work.headsign and work_name in work.headsign.lower():
        return 0
    if home.headsign and home_name in home.headsign.lower():
        return 1
    return None
