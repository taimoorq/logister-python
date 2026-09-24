from __future__ import annotations

from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Callable

from .client import LogisterClient
from .tracing import TraceContext, trace_scope
from inspect import iscoroutinefunction

TransactionNamer = Callable[[Any], str]


class LogisterMiddleware:
    sync_capable = True
    async_capable = True

    def __init__(self, get_response, *, client=None, transaction_namer=None, capture_spans=False):
        self.get_response = get_response
        self.client = client or LogisterClient.from_env()
        self.transaction_namer = transaction_namer
        self.capture_spans = capture_spans
        self.is_async = iscoroutinefunction(get_response)
        if self.is_async:
            from asgiref.sync import markcoroutinefunction
            markcoroutinefunction(self)

    def _start(self, request):
        request._logister_trace = TraceContext.from_headers(_header(request, "HTTP_TRACEPARENT"), _request_id(request), _header(request, "HTTP_X_TRACE_ID"))
        request._logister_started_at = perf_counter()
        request._logister_started_at_wall = datetime.now(UTC)
        request._logister_recorded = False
        return request._logister_trace

    def __call__(self, request):
        if self.is_async:
            return self._async_call(request)
        with trace_scope(self._start(request)):
            try:
                response = self.get_response(request)
            except Exception as error:
                self.process_exception(request, error)
                raise
            self._record(request, getattr(response, "status_code", None))
            if hasattr(response, "headers"):
                response.headers["x-request-id"] = request._logister_trace.request_id
            return response

    async def _async_call(self, request):
        with trace_scope(self._start(request)):
            try:
                response = await self.get_response(request)
            except Exception as error:
                self.process_exception(request, error)
                raise
            self._record(request, getattr(response, "status_code", None))
            response.headers["x-request-id"] = request._logister_trace.request_id
            return response

    def _record(self, request, status):
        if getattr(request, "_logister_recorded", False):
            return
        trace = getattr(request, "_logister_trace", None) or self._start(request)
        duration_ms = (perf_counter() - request._logister_started_at) * 1000
        name = _transaction_name(request, self.transaction_namer)
        context = {**_request_context(request, status_code=status), **trace.fields()}
        self.client.capture_transaction(name, duration_ms, context=context, trace_id=trace.trace_id, request_id=trace.request_id)
        if self.capture_spans:
            self.client.capture_span(name, duration_ms, context=context, **trace.fields(), kind="server",
                status="error" if status and status >= 500 else "ok", started_at=request._logister_started_at_wall, ended_at=datetime.now(UTC))
        request._logister_recorded = True

    def process_exception(self, request, exception):
        self._record(request, 500)
        trace = request._logister_trace
        self.client.capture_exception(exception, context={**_request_context(request, status_code=500), **trace.fields()},
            trace_id=trace.trace_id, request_id=trace.request_id)
        return None


def build_django_middleware(
    client: LogisterClient,
    *,
    transaction_namer: TransactionNamer | None = None,
    capture_spans: bool = False,
) -> type[LogisterMiddleware]:
    class ConfiguredLogisterMiddleware(LogisterMiddleware):
        def __init__(self, get_response: Callable[[Any], Any]) -> None:
            super().__init__(
                get_response,
                client=client,
                transaction_namer=transaction_namer,
                capture_spans=capture_spans,
            )

    ConfiguredLogisterMiddleware.__name__ = "ConfiguredLogisterMiddleware"
    return ConfiguredLogisterMiddleware


def _transaction_name(request: Any, transaction_namer: TransactionNamer | None) -> str:
    if transaction_namer is not None:
        return transaction_namer(request)
    return f"{request.method} {request.path}"


def _request_context(request: Any, *, status_code: int | None) -> dict[str, Any]:
    headers = _request_headers(request)
    query_string = _meta(request).get("QUERY_STRING")
    client_ip = _meta(request).get("REMOTE_ADDR")
    route = getattr(getattr(request, "resolver_match", None), "route", None)
    url = None
    build_absolute_uri = getattr(request, "build_absolute_uri", None)
    if callable(build_absolute_uri):
        try:
            url = build_absolute_uri()
        except Exception:
            url = None

    context = {
        "framework": "django",
        "method": getattr(request, "method", "GET"),
        "path": getattr(request, "path", "/"),
        "status_code": status_code,
        "url": url,
        "request": {
            "method": getattr(request, "method", "GET"),
            "path": getattr(request, "path", "/"),
            "url": url,
            "headers": headers,
            "query_string": query_string or None,
            "route": route,
        },
    }

    if query_string:
        context["query_string"] = query_string

    if client_ip:
        context["client_ip"] = client_ip
        context["request"]["client_ip"] = client_ip

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


def _request_headers(request: Any) -> dict[str, str]:
    allowed = {
        "HTTP_USER_AGENT": "User-Agent",
        "HTTP_ACCEPT": "Accept",
        "HTTP_HOST": "Host",
        "HTTP_REFERER": "Referer",
        "HTTP_X_FORWARDED_FOR": "X-Forwarded-For",
        "HTTP_X_REQUEST_ID": "X-Request-Id",
        "HTTP_X_TRACE_ID": "X-Trace-Id",
    }
    return {
        header_name: value
        for meta_key, header_name in allowed.items()
        if isinstance((value := _meta(request).get(meta_key)), str) and value.strip()
    }
