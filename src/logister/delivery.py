"""Immutable event snapshots and bounded, ingestion-only delivery retries."""
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import gzip
import hashlib
import json
import time
from uuid import UUID
from typing import Any

import httpx


class LogisterError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PreparedEvent:
    """A captured JSON envelope. Retain this object to retry the same event later."""
    event_id: str
    body: bytes

    def __post_init__(self):
        event = json.loads(self.body)["event"]
        if not isinstance(self.body, bytes) or str(UUID(self.event_id)) != self.event_id or event.get("uuid") != self.event_id:
            raise ValueError("Prepared event body and UUID must identify the same captured event")
        if not event.get("occurred_at"):
            raise ValueError("Prepared events require a captured timestamp")


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    event_id: str
    response: dict[str, Any] | None = None
    error: LogisterError | None = None


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    maximum_attempts: int = 3
    base_delay: float = 0.25
    maximum_delay: float = 5.0
    total_timeout: float = 15.0

    def __post_init__(self):
        import math
        if type(self.maximum_attempts) is not int or not 1 <= self.maximum_attempts <= 10:
            raise ValueError("maximum_attempts must be between 1 and 10")
        if not all(math.isfinite(x) and x >= 0 for x in (self.base_delay, self.maximum_delay, self.total_timeout)) or self.total_timeout == 0:
            raise ValueError("Retry delays must be finite/nonnegative and total_timeout positive")

    def delay(self, attempt: int, response: httpx.Response | None):
        if response is not None:
            value = response.headers.get("Retry-After")
            if value:
                try:
                    seconds = float(value)
                except ValueError:
                    try:
                        seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
                    except (ValueError, TypeError, OverflowError):
                        seconds = -1
                if seconds >= 0:
                    return min(seconds, self.maximum_delay)
        return min(self.base_delay * 2 ** attempt, self.maximum_delay)


def post(http, path, body, *, policy, timeout, headers=None, deadline=None):
    """Retry only prepared ingestion bytes; never rebuild context or identity."""
    if deadline is None:
        deadline = time.monotonic() + policy.total_timeout
    for attempt in range(policy.maximum_attempts):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LogisterError("Ingestion retry deadline exhausted")
        response = None
        try:
            response = http.post(path, content=body, headers=headers or {}, timeout=min(timeout, remaining))
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError as exc:
                # A success response may already represent durable acceptance.
                # Never retry it merely because response parsing failed.
                raise LogisterError("Ingestion succeeded but its response was not valid JSON") from exc
            return data if isinstance(data, dict) else {"data": data}
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            retryable = status in (408, 425, 429) or 500 <= status <= 599
            if path.endswith("/batch") and status == 501:
                retryable = False
            failure = exc
        except httpx.TransportError as exc:
            retryable, failure = True, exc
        except httpx.HTTPError as exc:
            raise LogisterError("Logister ingestion failed") from exc
        if not retryable or attempt + 1 == policy.maximum_attempts:
            raise LogisterError("Logister ingestion failed") from failure
        delay = policy.delay(attempt, response)
        if delay >= deadline - time.monotonic():
            raise LogisterError("Ingestion retry deadline exhausted") from failure
        time.sleep(delay)
    raise AssertionError("unreachable")


def deliver_batch(events, send):
    """Return one result per event, including failures, without skipping later chunks."""
    results = []

    def chunk(items):
        if not items:
            return
        raw = b"\n".join(event.body for event in items) + b"\n"
        body = gzip.compress(raw, mtime=0)
        if len(items) > 1 and (len(raw) > 8 * 1024 * 1024 or len(body) > 2 * 1024 * 1024):
            middle = len(items) // 2
            chunk(items[:middle]); chunk(items[middle:])
            return
        headers = {"Content-Type": "application/x-ndjson", "Content-Encoding": "gzip", "X-Logister-Batch-Id": hashlib.sha256(raw).hexdigest()}
        try:
            response = send("/api/v1/ingest_events/batch", body, headers)
            results.extend(DeliveryResult(event.event_id, response=response) for event in items)
        except LogisterError as error:
            cause = error.__cause__
            status = cause.response.status_code if isinstance(cause, httpx.HTTPStatusError) else None
            if status == 413 and len(items) > 1:
                middle = len(items) // 2
                chunk(items[:middle]); chunk(items[middle:])
            elif status in (404, 405, 415, 501) or status == 413 and len(items) == 1:
                for event in items:
                    try:
                        response = send("/api/v1/ingest_events", event.body, {"Content-Type": "application/json"})
                        results.append(DeliveryResult(event.event_id, response=response))
                    except LogisterError as failure:
                        results.append(DeliveryResult(event.event_id, error=failure))
            else:
                results.extend(DeliveryResult(event.event_id, error=error) for event in items)

    for start in range(0, len(events), 100):
        chunk(events[start:start + 100])
    return results
