# Changelog

## v0.5.0 - 2026-09-24

- Propagate task-local request context through Django, Flask, and FastAPI, including async Django handlers.
- Add immutable request handles and outbound header helpers with strict W3C parsing and exact-origin allowlists.

## v0.4.0 - 2026-09-11

- Added immutable prepared events with stable UUIDs and capture timestamps, ingestion-only bounded retries, Retry-After handling, and retained snapshots for explicit replay.
- Added bounded gzip/NDJSON batches, deterministic batch IDs, 413 splitting, legacy endpoint fallback, and ordered outcomes for every event, including failures.
- Added reproducible dependency constraints across Python 3.11–3.14, real installed-framework smoke tests, and weekly fresh dependency resolution/auditing.
- Restored explicit publication dispatch and exact-tag recovery while preserving the PyPI trusted publisher identity.

All notable changes to this project will be documented in this file.

## v0.3.1 - 2026-08-09

- Align the default SDK user agent with the published package version.

## v0.3.0 - 2026-07-25

- Added Python 3.14 to the tested and declared support matrix.
- Raised the Django integration floor to the supported Django 5.2 LTS line and added Django 6 compatibility.
- Updated the test suite to patched pytest 9 releases and added dependency vulnerability auditing to CI.
- Hardened release automation with immutable action references and one ordered publish-and-release path.

## v0.2.4 - 2026-06-18

- Added first-class source context fields (`repository`, `commit_sha`, and `branch`) with `LOGISTER_*` and GitHub Actions environment variable support.
- Added `record_deployment()` for posting release-to-commit deployment records to Logister.

## v0.2.3 - 2026-05-22

- Added `capture_span` plus opt-in FastAPI, Django, and Flask request span capture for request load waterfall charts.

## v0.2.2 - 2026-05-22

- Added README guidance for using Python reports with the Logister project Insights beta, including practical metric, transaction, log, check-in, and custom attribute examples.

## v0.2.1 - 2026-05-21

- Added `unit`, `level`, and `fingerprint` options to `capture_metric`.
- Added top-level metric value/unit context while preserving the existing structured metric payload.

## v0.2.0 - 2026-04-22

- Added Flask request instrumentation alongside the existing FastAPI, Django, and Celery integrations.
- Added native Python `logging` support via `instrument_logging()` and `LogisterLoggingHandler`.
- Expanded captured exception payloads with chained exceptions, richer traceback frames, optional frame locals, and Python runtime metadata.
- Expanded framework context capture for FastAPI, Django, Flask, and Celery with fuller request and task metadata.
- Updated README guidance to cover framework setup, logging integration, richer error capture, and current package capabilities.

## v0.1.0 - 2026-04-22

- Initial `logister-python` package scaffold
- Shared `LogisterClient` with env-based configuration and reusable HTTP transport
- First `FastAPI` integration for request transactions and uncaught exceptions
- First `Django` middleware integration for request transactions and uncaught exceptions
- First `Celery` integration for task transactions, failures, retries, and optional check-ins
- Packaging, CI, and PyPI Trusted Publishing setup
- Python support starts at 3.11
