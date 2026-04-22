# logister-python

Python SDK for sending errors, logs, metrics, transactions, and check-ins to Logister.

This package is aimed at Python teams running APIs, workers, schedulers, and internal services. The current focus is the set of places Python teams usually need first:

- a shared `LogisterClient`
- native Python `logging` integration
- `FastAPI` request instrumentation
- `Django` request middleware
- `Celery` task instrumentation
- `Flask` request instrumentation

Supports Python 3.11 and newer.

- Main Logister app: https://github.com/taimoorq/logister
- Product docs: https://docs.logister.org/
- Python integration docs: https://docs.logister.org/integrations/python/

## Install

Core client:

```bash
pip install logister-python
```

FastAPI support:

```bash
pip install 'logister-python[fastapi]'
```

Celery support:

```bash
pip install 'logister-python[celery]'
```

Django support:

```bash
pip install 'logister-python[django]'
```

Flask support:

```bash
pip install 'logister-python[flask]'
```

## Environment Variables

`LogisterClient.from_env()` reads:

- `LOGISTER_API_KEY`
- `LOGISTER_BASE_URL` (defaults to `https://logister.org`)
- `LOGISTER_TIMEOUT` (defaults to `5.0`)
- `LOGISTER_ENVIRONMENT`
- `LOGISTER_RELEASE`
- `LOGISTER_CAPTURE_LOCALS` (`true` / `false`, defaults to `false`)

## Core Client

Use the shared client when you are wiring a script, worker, CLI task, or framework hook and want one place to send custom events.

```python
from logister import LogisterClient

client = LogisterClient.from_env(default_context={"service": "api"})

client.capture_message("Application booted", level="info")
client.capture_metric("cache.hit_rate", 0.98, context={"cache": "primary"})
client.capture_transaction("POST /checkout", 182.4, request_id="req_123")
```

## Python Logging

If your app already uses the standard library `logging` module, this is usually the easiest way to start sending application logs into Logister without rewriting call sites.

```python
import logging

from logister import LogisterClient, instrument_logging

client = LogisterClient.from_env(default_context={"service": "api"})
logger = logging.getLogger("checkout")

instrument_logging(client, logger=logger)

logger.warning("Inventory cache miss", extra={"request_id": "req_123", "sku": "sku_42"})
```

What this records:

- standard Python log records as `log` events
- `logger.exception(...)` and other records with `exc_info` as `error` events
- logger metadata like logger name, module, file, function, line number, process, and thread
- extra record fields passed through `extra={...}` so request IDs, trace IDs, and app-specific details show up in Logister

You can also manage the underlying HTTP client explicitly:

```python
from logister import LogisterClient

with LogisterClient.from_env() as client:
    client.capture_message("Worker online")
```

## Error Capture

Python error reports are most useful when you include the service or component name and let the SDK send the traceback structure for you.

```python
from logister import LogisterClient

client = LogisterClient.from_env(default_context={"service": "checkout"})

try:
    run_checkout()
except Exception as exc:
    client.capture_exception(
        exc,
        fingerprint="checkout-failed",
        context={
            "component": "checkout",
            "order_id": 1234,
        },
    )
```

Captured Python exceptions include structured traceback frames, backtrace text, exception module and qualified class name, chained exceptions from `raise ... from ...`, and runtime metadata like Python version, platform, hostname, and process ID.

Set `LOGISTER_CAPTURE_LOCALS=true` if you want frame locals included in error events for the Logister UI.

## FastAPI

This is the cleanest path for modern Python API services.

```python
from fastapi import FastAPI

from logister import LogisterClient, instrument_fastapi

app = FastAPI()
logister = LogisterClient.from_env(default_context={"service": "api"})
instrument_fastapi(app, logister)
```

What this records:

- request duration as a `transaction`
- uncaught request exceptions as an `error`
- request metadata like method, path, route, full URL, selected headers, client IP, path params, query string, `x-request-id`, and `x-trace-id`

You can customize transaction naming:

```python
instrument_fastapi(
    app,
    logister,
    transaction_namer=lambda request: f"{request.method} {request.url.path}",
)
```

## Celery

This is the worker-side path when your Python app does meaningful work outside the request cycle.

```python
from celery import Celery

from logister import LogisterClient, instrument_celery

celery_app = Celery("billing")
logister = LogisterClient.from_env(default_context={"service": "worker"})

instrument_celery(
    celery_app,
    logister,
    monitor_slug_factory=lambda task: getattr(task, "name", None),
)
```

What this records:

- task runtime as a `transaction`
- task failures as an `error`
- task retries as a warning `log`
- optional task-level `check_in` events when you provide `monitor_slug_factory`
- task metadata like queue, module, retry count, ETA, and worker hostname when Celery exposes it

## Django

Use middleware when you want a Django app to report request timing and uncaught view exceptions with very little setup.

Use the built-in middleware directly when env-based configuration is enough:

```python
MIDDLEWARE = [
    # ...
    "logister.django.LogisterMiddleware",
]
```

`LogisterMiddleware` reads the same `LOGISTER_*` environment variables as `LogisterClient.from_env()`.

If you want to build the client yourself, bind it with `build_django_middleware()`:

```python
from logister import LogisterClient, build_django_middleware

logister = LogisterClient.from_env(default_context={"service": "django-web"})
ConfiguredLogisterMiddleware = build_django_middleware(logister)
```

What Django middleware records:

- request duration as a `transaction`
- uncaught view exceptions via `process_exception()` as an `error`
- request metadata like method, path, route, full URL, selected headers, status code, client IP, query string, `X-Request-ID`, and `X-Trace-ID`

## Flask

Use the Flask hooks when you want lightweight request instrumentation without changing your route code.

```python
from flask import Flask

from logister import LogisterClient, instrument_flask

app = Flask(__name__)
logister = LogisterClient.from_env(default_context={"service": "flask-web"})
instrument_flask(app, logister)
```

What Flask instrumentation records:

- request duration as a `transaction`
- uncaught request exceptions as an `error`
- request metadata like method, path, full URL, endpoint, blueprint, selected headers, status code, client IP, query string, `X-Request-ID`, and `X-Trace-ID`

## Check-ins

Check-ins are a good fit for scheduled jobs, cron-style imports, and the “did this worker actually run?” questions Python teams usually end up debugging.

```python
from logister import LogisterClient

client = LogisterClient.from_env(default_context={"service": "scheduler"})

client.check_in(
    "nightly-import",
    "ok",
    expected_interval_seconds=3600,
    duration_ms=842.7,
)
```

## Event Mapping

- web request duration -> `transaction`
- uncaught exception -> `error`
- app log / warning -> `log`
- custom counters / measurements -> `metric`
- scheduled job heartbeat -> `check_in`

## Publishing

This package is intended to publish to PyPI with Trusted Publishing from GitHub Actions.

- Push a tag like `v0.1.0`
- GitHub Actions builds the distributions
- PyPI Trusted Publishing handles the upload with OIDC

## Release Flow

- `CHANGELOG.md` tracks package releases
- Git tags trigger PyPI publish and GitHub releases
- This package keeps its own versioning separate from the main Logister app
