# logister-python

Python SDK for sending errors, logs, metrics, transactions, and check-ins to Logister.

Install it from PyPI as `logister-python`.

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
- Insights beta guide: https://docs.logister.org/product/#insights-beta
- Python integration docs: https://docs.logister.org/integrations/python/
- PyPI package: https://pypi.org/project/logister-python/

## Table Of Contents

- [What This Package Is For](#what-this-package-is-for)
- [Install From PyPI](#install-from-pypi)
- [Environment Variables](#environment-variables)
- [Core Client](#core-client)
- [Python Logging](#python-logging)
- [Error Capture](#error-capture)
- [FastAPI](#fastapi)
- [Celery](#celery)
- [Django](#django)
- [Flask](#flask)
- [Check-ins](#check-ins)
- [Using project Insights beta](#using-project-insights-beta)
- [Event Mapping](#event-mapping)
- [Publishing](#publishing)
- [Release Flow](#release-flow)

## What This Package Is For

Use `logister-python` when you want a Python service to send operational telemetry into Logister through the published PyPI package instead of wiring raw HTTP calls by hand.

- API and web apps: FastAPI, Django, Flask
- Worker and scheduler processes: Celery, cron-style jobs, CLI tasks
- Standard-library logging pipelines: `logging` to Logister events
- Shared custom instrumentation: errors, logs, metrics, transactions, and check-ins

## Install From PyPI

Core client:

```bash
pip install logister-python
```

With `uv`:

```bash
uv add logister-python
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

Package index: https://pypi.org/project/logister-python/

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
client.capture_metric(
    "cache.hit_rate",
    0.98,
    unit="ratio",
    level="info",
    fingerprint="metric:cache.hit_rate",
    context={"cache": "primary"},
)
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
    release="worker@2026.05.21",
    expected_interval_seconds=3600,
    duration_ms=842.7,
    trace_id="trace-123",
    request_id="req-123",
)
```

## Using project Insights beta

The Logister project Insights tab combines Inbox, Activity, and Performance data into live dashboard views. Python services get the most useful Insights view when they send consistent `LOGISTER_ENVIRONMENT`, `LOGISTER_RELEASE`, and stable top-level context attributes.

Use `default_context` for attributes that should be present on most events, and pass per-event context for route, queue, worker, or feature dimensions:

```python
from logister import LogisterClient

client = LogisterClient.from_env(
    default_context={
        "service": "billing-api",
        "region": "us-east-1",
    }
)

client.capture_metric(
    "queue.depth",
    42,
    unit="jobs",
    context={
        "service": "billing-worker",
        "queue": "billing",
        "tenant_tier": "enterprise",
    },
)

client.capture_transaction(
    "POST /checkout",
    182.4,
    context={
        "route": "POST /checkout",
        "feature_flag": "new_checkout",
        "tenant_tier": "enterprise",
    },
    request_id="req_123",
)

client.capture_message(
    "payment provider retry",
    level="warn",
    context={
        "service": "billing-worker",
        "provider": "stripe",
        "queue": "billing",
    },
)

client.check_in(
    "nightly-reconcile",
    "ok",
    expected_interval_seconds=3600,
    duration_ms=842.7,
    context={
        "service": "billing-worker",
        "queue": "reconcile",
    },
)
```

Practical Insights recipes:

- Release validation: set `LOGISTER_RELEASE`, then filter Insights to the new release and compare error count, transaction P95, and custom metrics.
- Worker monitoring: report metrics such as `queue.depth`, `queue.latency`, `task.retry_count`, or `celery.active_tasks` with stable `queue` and `service` context keys.
- Performance triage: let FastAPI, Django, or Flask instrumentation send request transactions, then add route-level logs and metrics with matching `route` values.
- Instrumentation audit: open Insights after deploy and confirm errors, logs, metrics, transactions, and check-ins all appear in the recent stream.

Keep custom attributes stable and low-cardinality. Good top-level context keys include `service`, `region`, `queue`, `route`, `tenant_tier`, `provider`, and `feature_flag`. Avoid raw IDs, emails, request bodies, SQL text, and per-user values as Insights dimensions.

## Event Mapping

- web request duration -> `transaction`
- uncaught exception -> `error`
- app log / warning -> `log`
- custom counters / measurements -> `metric`
- scheduled job heartbeat -> `check_in`

## Publishing

This package is intended to publish to PyPI with Trusted Publishing from GitHub Actions. A commit or merge to `main` runs CI only; publishing requires a version tag.

- Push a tag like `v0.2.2`
- GitHub Actions builds the distributions
- PyPI Trusted Publishing handles the upload with OIDC

## Release Flow

- `CHANGELOG.md` tracks package releases
- Git tags trigger PyPI publish and GitHub releases
- This package keeps its own versioning separate from the main Logister app
