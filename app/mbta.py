"""Async MBTA v3 API client specialized for commuter rail needs."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=5.0)
_RETRIES = 2


class MBTAAPIError(RuntimeError):
    pass


class MBTAClient:
    """Thin async wrapper for MBTA v3 endpoints."""

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        *,
        timeout: httpx.Timeout = _DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        headers: Dict[str, str] = {}
        if api_key:
            headers["x-api-key"] = api_key
        limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=timeout,
            limits=limits,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "MBTAClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def list_commuter_routes(self) -> List[Dict[str, Any]]:
        return await self._get_paginated(
            "/routes",
            params={
                "filter[type]": "2",
                "page[limit]": "100",
            },
        )

    async def list_stops(self, route_id: str) -> List[Dict[str, Any]]:
        return await self._get_paginated(
            "/stops",
            params={
                "filter[route]": route_id,
                "page[limit]": "200",
            },
        )

    async def route_details(self, route_id: str) -> Dict[str, Any]:
        data = await self._call(
            "GET",
            f"/routes/{route_id}",
            params={"include": "line"},
        )
        return data.get("data", {})

    async def schedules(
        self,
        *,
        route_id: str,
        stop_id: str,
        direction_id: Optional[int],
        start: datetime,
        end: datetime,
        include: Sequence[str] | None = ("trip", "stop"),
    ) -> Tuple[List[Dict[str, Any]], Dict[Tuple[str, str], Dict[str, Any]]]:
        """Fetch schedule entries between two datetimes (inclusive) for a stop."""

        if end < start:
            raise ValueError("end must be after start")

        results: List[Dict[str, Any]] = []
        included: Dict[Tuple[str, str], Dict[str, Any]] = {}
        eastern = ZoneInfo("America/New_York")
        start_local = start.astimezone(eastern)
        end_local = end.astimezone(eastern)
        current = start_local.date()
        end_date = end_local.date()
        while current <= end_date:
            params: Dict[str, Any] = {
                "filter[route]": route_id,
                "filter[stop]": stop_id,
                "filter[date]": current.isoformat(),
                "page[limit]": "200",
                "sort": "departure_time",
            }
            if direction_id is not None:
                params["filter[direction_id]"] = str(direction_id)
            if include:
                params["include"] = ",".join(include)

            if current == start_local.date():
                params["filter[min_time]"] = start_local.strftime("%H:%M")
            if current == end_date:
                params["filter[max_time]"] = end_local.strftime("%H:%M")

            await self._collect_schedule_page(params, results, included)
            current = current + timedelta(days=1)

        return results, included

    async def _collect_schedule_page(
        self,
        params: Dict[str, Any],
        results: List[Dict[str, Any]],
        included: Dict[Tuple[str, str], Dict[str, Any]],
    ) -> None:
        payload = await self._call("GET", "/schedules", params=params)
        self._ingest_payload(payload, results, included)
        next_offset = self._next_offset(payload)
        while next_offset is not None:
            paged_params = dict(params)
            paged_params["page[offset]"] = str(next_offset)
            payload = await self._call("GET", "/schedules", params=paged_params)
            self._ingest_payload(payload, results, included)
            next_offset = self._next_offset(payload)

    @staticmethod
    def _ingest_payload(
        payload: Dict[str, Any],
        results: List[Dict[str, Any]],
        included: Dict[Tuple[str, str], Dict[str, Any]],
    ) -> None:
        page_data = payload.get("data", [])
        page_included = payload.get("included", [])
        results.extend(page_data or [])
        for item in page_included or []:
            key = (item.get("type"), item.get("id"))
            if key[0] and key[1]:
                included[key] = item

    async def _get_paginated(self, path: str, *, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        next_offset: Optional[int] = None
        while True:
            current_params = dict(params)
            if next_offset is not None:
                current_params["page[offset]"] = str(next_offset)
            payload = await self._call("GET", path, params=current_params)
            data = payload.get("data", [])
            results.extend(data)
            next_offset = self._next_offset(payload)
            if next_offset is None:
                break
        return results

    async def _call(
        self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        last_exc: Optional[Exception] = None
        for attempt in range(_RETRIES + 1):
            try:
                resp = await self._client.request(method, path, params=params)
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as exc:
                if 400 <= exc.response.status_code < 500:
                    logger.warning("MBTA client 4xx %s", exc)
                    raise MBTAAPIError(str(exc)) from exc
                last_exc = exc
            except (httpx.TimeoutException, httpx.TransportError) as exc:  # pragma: no cover
                last_exc = exc
            if attempt < _RETRIES:
                await asyncio.sleep(0.3 * (attempt + 1))
        assert last_exc is not None
        raise MBTAAPIError(str(last_exc))

    @staticmethod
    def _next_offset(payload: Dict[str, Any]) -> Optional[int]:
        links = payload.get("links", {})
        next_link = links.get("next")
        if not next_link:
            return None
        try:
            # Example: next=/schedules?page[offset]=200
            _, query = next_link.split("?", 1)
            for part in query.split("&"):
                if part.startswith("page[offset]" + "="):
                    return int(part.split("=", 1)[1])
        except Exception:  # pragma: no cover - defensive
            logger.debug("Unable to parse pagination link: %s", next_link)
        return None


async def gather_map(func, items: Iterable[Any]) -> List[Any]:
    coros = [func(item) for item in items]
    return list(await asyncio.gather(*coros))
