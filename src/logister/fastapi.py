from __future__ import annotations

from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Callable

from ._guard import captures_client_ip, safely
from .client import LogisterClient
from .tracing import TraceContext, trace_scope

TransactionNamer = Callable[[Any], str]


def instrument_fastapi(
    app: Any,
    client: LogisterClient,
    *,
    transaction_namer: TransactionNamer | None = None,
    capture_spans: bool = False,
) -> Any:
    if getattr(app.state, "_logister_fastapi_installed", False):
        return app

    @app.middleware("http")
    async def logister_middleware(request: Any, call_next: Callable[[Any], Any]) -> Any:
        trace = TraceContext.from_headers(_header(request, "traceparent"), _header(request, "x-request-id"), _header(request, "x-trace-id"))
        with trace_scope(trace):
            return await process_request(request, call_next, trace)

    async def process_request(request, call_next, trace):
        started_at = perf_counter()
        started_wall = datetime.now(UTC)
        name = safely(_transaction_name, request, transaction_namer) or _default_name(request)

        try:
            response = await call_next(request)
        except Exception as exc:
            safely(record, request, name, trace, started_at, started_wall, 500, exc, defer=True)
            raise

        safely(record, request, name, trace, started_at, started_wall, getattr(response, "status_code", None), None, defer=True)
        if hasattr(response, "headers"):
            response.headers["x-request-id"] = trace.request_id
        return response

    def record(request, name, trace, started_at, started_wall, status_code, exc):
        # Events are prepared on the request task; delivery runs on a background thread.
        duration_ms = (perf_counter() - started_at) * 1000.0
        context = _request_context(request, status_code=status_code, client_ip=captures_client_ip(client))
        safely(
            client.capture_transaction,
            name,
            duration_ms,
            context=context,
            trace_id=trace.trace_id,
            request_id=trace.request_id,
        )
        if capture_spans:
            failed = exc is not None or bool(status_code and status_code >= 500)
            safely(
                client.capture_span,
                name,
                duration_ms,
                context=context,
                trace_id=trace.trace_id or trace.request_id,
                request_id=trace.request_id,
                span_id=trace.span_id, parent_span_id=trace.parent_span_id,
                kind="server",
                status="error" if failed else "ok",
                started_at=started_wall,
                ended_at=datetime.now(UTC),
            )
        if exc is not None:
            safely(
                client.capture_exception,
                exc,
                context=context,
                trace_id=trace.trace_id,
                request_id=trace.request_id,
            )

    app.state._logister_fastapi_installed = True
    app.state.logister_client = client
    return app


def _transaction_name(request: Any, transaction_namer: TransactionNamer | None) -> str:
    if transaction_namer is not None:
        return transaction_namer(request)
    return _default_name(request)


def _default_name(request: Any) -> str:
    return f"{getattr(request, 'method', 'GET')} {getattr(getattr(request, 'url', None), 'path', '/')}"


def _request_context(request: Any, *, status_code: int | None, client_ip: bool = False) -> dict[str, Any]:
    client_host = getattr(getattr(request, "client", None), "host", None) if client_ip else None
    query = getattr(request.url, "query", "")
    scope = getattr(request, "scope", None)
    route_obj = scope.get("route") if isinstance(scope, dict) else None
    route_path = getattr(route_obj, "path", None)

    headers = {
        _header_name(key): value
        for key in ("host", "user-agent", "accept", "x-forwarded-for", "x-request-id", "x-trace-id")
        if (value := _header(request, key)) and (client_ip or key != "x-forwarded-for")
    }

    context = {
        "framework": "fastapi",
        "method": request.method,
        "path": request.url.path,
        "status_code": status_code,
        "url": str(request.url),
        "request": {
            "method": request.method,
            "path": request.url.path,
            "url": str(request.url),
            "headers": headers,
            "query_string": query or None,
            "path_params": dict(getattr(request, "path_params", {}) or {}),
            "route": route_path,
        },
    }
    if client_host:
        context["client_ip"] = client_host
        context["request"]["client_ip"] = client_host
    if query:
        context["query_string"] = query
    if route_path:
        context["route"] = route_path
    return context


def _header(request: Any, name: str) -> str | None:
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    value = headers.get(name) or headers.get(name.title())
    return value.strip() if isinstance(value, str) and value.strip() else None


def _header_name(name: str) -> str:
    return "-".join(part.capitalize() for part in name.split("-"))
