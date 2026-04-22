# Changelog

All notable changes to this project will be documented in this file.

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
