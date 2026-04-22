from __future__ import annotations

from time import perf_counter
from typing import Any, Callable

from .client import LogisterClient

TransactionNamer = Callable[[Any], str]


class LogisterMiddleware:
    def __init__(
        self,
        get_response: Callable[[Any], Any],
        *,
        client: LogisterClient | None = None,
        transaction_namer: TransactionNamer | None = None,
    ) -> None:
        self.get_response = get_response
        self.client = client or LogisterClient.from_env()
        self.transaction_namer = transaction_namer

    def __call__(self, request: Any) -> Any:
        started_at = perf_counter()
        request._logister_started_at = started_at
        request._logister_transaction_name = _transaction_name(request, self.transaction_namer)

        response = self.get_response(request)

        duration_ms = (perf_counter() - started_at) * 1000.0
        self.client.capture_transaction(
            request._logister_transaction_name,
            duration_ms,
            context=_request_context(request, status_code=getattr(response, "status_code", None)),
            trace_id=_header(request, "HTTP_X_TRACE_ID"),
            request_id=_request_id(request),
        )
        return response

    def process_exception(self, request: Any, exception: BaseException) -> None:
        started_at = getattr(request, "_logister_started_at", None)
        transaction_name = getattr(request, "_logister_transaction_name", _transaction_name(request, self.transaction_namer))
        duration_ms = (perf_counter() - started_at) * 1000.0 if started_at is not None else 0.0
        context = _request_context(request, status_code=500)

        self.client.capture_transaction(
            transaction_name,
            duration_ms,
            context=context,
            trace_id=_header(request, "HTTP_X_TRACE_ID"),
            request_id=_request_id(request),
        )
        self.client.capture_exception(
            exception,
            context=context,
            trace_id=_header(request, "HTTP_X_TRACE_ID"),
            request_id=_request_id(request),
        )
        return None


def build_django_middleware(
    client: LogisterClient,
    *,
    transaction_namer: TransactionNamer | None = None,
) -> type[LogisterMiddleware]:
    class ConfiguredLogisterMiddleware(LogisterMiddleware):
        def __init__(self, get_response: Callable[[Any], Any]) -> None:
            super().__init__(get_response, client=client, transaction_namer=transaction_namer)

    ConfiguredLogisterMiddleware.__name__ = "ConfiguredLogisterMiddleware"
    return ConfiguredLogisterMiddleware


def _transaction_name(request: Any, transaction_namer: TransactionNamer | None) -> str:
    if transaction_namer is not None:
        return transaction_namer(request)
    return f"{request.method} {request.path}"


def _request_context(request: Any, *, status_code: int | None) -> dict[str, Any]:
    context = {
        "framework": "django",
        "method": getattr(request, "method", "GET"),
        "path": getattr(request, "path", "/"),
        "status_code": status_code,
    }

    query_string = _meta(request).get("QUERY_STRING")
    if query_string:
        context["query_string"] = query_string

    client_ip = _meta(request).get("REMOTE_ADDR")
    if client_ip:
        context["client_ip"] = client_ip

    route = getattr(getattr(request, "resolver_match", None), "route", None)
    if route:
        context["route"] = route

    return context


def _meta(request: Any) -> dict[str, Any]:
    meta = getattr(request, "META", None)
    return meta if isinstance(meta, dict) else {}


def _header(request: Any, key: str) -> str | None:
    value = _meta(request).get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _request_id(request: Any) -> str | None:
    return _header(request, "HTTP_X_REQUEST_ID") or _header(request, "REQUEST_ID")
