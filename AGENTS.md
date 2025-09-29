# Repository Guidelines

## Project Structure & Module Organization
Core service code lives under `app/`. `app/main.py` wires the FastAPI surface, dependency lifespan, and calendar orchestration. HTTP access to MBTA v3 is isolated in `app/mbta.py`, while `app/resolve.py` manages stop indexing and route inference. ICS shaping is in `app/ics.py`, and `app/cache.py` provides the generic TTL cache. Tests sit in `tests/`, presently focused on resolver and calendar logic. Runtime assets such as Docker manifests and docs stay at the repo root.

## Build, Test, and Development Commands
Run the API locally with `uvicorn app.main:app --reload`. Execute the unit suite via `pytest`. Container builds follow `docker build -t mbta-cr-ical .`, and `docker run -p 8000:8000 mbta-cr-ical` launches the service. For compose users, `docker compose up --build` maps the same defaults.

## Coding Style & Naming Conventions
Python 3.11+ with type hints is required. Follow PEP 8 spacing and prefer descriptive, lower_snake_case identifiers. Async boundaries are explicit, and helper functions that return complex data should document return types. Cache keys and UID strings should stay lowercase with hyphen separators to keep external references stable.

## Testing Guidelines
Pytest is the harness of record. Place new tests under `tests/` mirroring the module under test. Ensure async paths are covered with `pytest.mark.asyncio`. When adding new inference branches, include fixtures that simulate MBTA responses to prevent network coupling. Noon partitioning and UID stability regression tests are particularly valuable.

## Commit & Pull Request Guidelines
Write concise commit subjects in the imperative mood (e.g., “Add ICS outage fallback”). In pull requests, describe the commuter-rail scenario exercised, link any related MBTA API docs, and call out changes to request caching or timezone handling. Include test evidence (`pytest`, Docker build logs) and, when modifying calendar output, attach example `.ics` excerpts or screenshots from a client to aid review.
