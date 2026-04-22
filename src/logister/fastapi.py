from __future__ import annotations

from time import perf_counter
from typing import Any, Callable

from .client import LogisterClient

TransactionNamer = Callable[[Any], str]


def instrument_fastapi(
    app: Any,
    client: LogisterClient,
    *,
    transaction_namer: TransactionNamer | None = None,
) -> Any:
    if getattr(app.state, "_logister_fastapi_installed", False):
        return app

    @app.middleware("http")
    async def logister_middleware(request: Any, call_next: Callable[[Any], Any]) -> Any:
        started_at = perf_counter()
        name = _transaction_name(request, transaction_namer)
        trace_id = _header(request, "x-trace-id")
        request_id = _header(request, "x-request-id")

        try:
            response = await call_next(request)
        except Exception as exc:
            duration_ms = (perf_counter() - started_at) * 1000.0
            context = _request_context(request, status_code=500)
            client.capture_transaction(
                name,
                duration_ms,
                context=context,
                trace_id=trace_id,
                request_id=request_id,
            )
            client.capture_exception(
                exc,
                context=context,
                trace_id=trace_id,
                request_id=request_id,
            )
            raise

        duration_ms = (perf_counter() - started_at) * 1000.0
        client.capture_transaction(
            name,
            duration_ms,
            context=_request_context(request, status_code=getattr(response, "status_code", None)),
            trace_id=trace_id,
            request_id=request_id,
        )
        return response

    app.state._logister_fastapi_installed = True
    app.state.logister_client = client
    return app


def _transaction_name(request: Any, transaction_namer: TransactionNamer | None) -> str:
    if transaction_namer is not None:
        return transaction_namer(request)
    return f"{request.method} {request.url.path}"


def _request_context(request: Any, *, status_code: int | None) -> dict[str, Any]:
    client_host = getattr(getattr(request, "client", None), "host", None)
    query = getattr(request.url, "query", "")
    scope = getattr(request, "scope", None)
    route_obj = scope.get("route") if isinstance(scope, dict) else None
    route_path = getattr(route_obj, "path", None)

    headers = {
        _header_name(key): value
        for key in ("host", "user-agent", "accept", "x-forwarded-for", "x-request-id", "x-trace-id")
        if (value := _header(request, key))
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
