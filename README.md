# MBTA CR iCal Service

This project exposes a FastAPI service that delivers a commuter-rail iCalendar feed without hardcoding specific lines. The API accepts `home_stop` and `work_stop` query parameters (slugs or natural names), infers the shared commuter-rail route, and returns a standards-compliant `.ics` stream that calendar clients can refresh daily.

## How It Works
- **Stop inference:** On first use the service indexes all MBTA commuter-rail stops (type `2`). User input is slugified and matched by exact slug, substring, and fuzzy distance.
- **Route resolution:** The MBTA schedule API is probed for a route that serves both stops, preferring trips where the origin precedes the destination in the stop sequence.
- **Noon split rule:** Morning events (`< 12:00 America/New_York`) depart from the provided home stop toward work. Afternoon events (`≥ 12:00`) depart from the work stop heading home.
- **ICS generation:** `icalendar` builds events with 24-hour refresh hints, a VTIMEZONE for `America/New_York`, and stable UIDs (`mbta-{route}-{trip}-{stop}-{date}`). On upstream outages the service emits a tentative, all-day calendar entry to keep clients in sync.

## Running Locally
```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```
Visit `http://127.0.0.1:8000/schedule.ical?home_stop=mansfield&work_stop=south-station` to download a two-week feed. Adjust the planning window with `&days=7` (1-30, default 14).

## Docker
```bash
docker build -t mbta-cr-ical .
docker run -e MBTA_API_KEY="$MBTA_API_KEY" -p 8000:8000 mbta-cr-ical
```
Compose users can run `docker compose up --build` (propagates `MBTA_API_KEY`, `DEFAULT_HOME_STOP`, `DEFAULT_WORK_STOP`).

## Discover Stops
Use `GET /stops?query=mansfield` to inspect stop candidates and their routes. This is especially helpful when multiple platforms share similar names.

## Environment Variables
- `MBTA_API_KEY` (optional): forwarded via `x-api-key` header.
- `DEFAULT_HOME_STOP` / `DEFAULT_WORK_STOP` (optional): fallback stop names when query parameters are omitted.
- `MBTA_API_URL`, `LOG_LEVEL`: advanced overrides for API base URL and logging.

## Caveats & Refresh Behavior
The feed reflects scheduled data only; delays and cancellations are not included. Calendar clients should honor the baked-in 24-hour refresh interval. Force a fresh cache pull with `force_refresh=1` and change horizon with `days` (defaults to 14) when MBTA publishes new timetables.
