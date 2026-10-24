from __future__ import annotations

from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Callable

from ._guard import captures_client_ip, safely
from .client import LogisterClient
from .tracing import TraceContext, _current

TransactionNamer = Callable[[Any], str]


def instrument_flask(
    app: Any,
    client: LogisterClient,
    *,
    transaction_namer: TransactionNamer | None = None,
    capture_spans: bool = False,
) -> Any:
    extensions = getattr(app, "extensions", None)
    if extensions is None:
        extensions = {}
        app.extensions = extensions

    if extensions.get("logister_flask_installed"):
        return app

    @app.before_request
    def logister_before_request() -> None:
        safely(start_request)

    def start_request():
        g, request = _flask_state()
        trace = TraceContext.from_headers(_header(request, "traceparent"), _header(request, "X-Request-Id"), _header(request, "X-Trace-Id"))
        g._logister_trace = trace
        g._logister_trace_token = _current.set(trace)
        g._logister_started_at = perf_counter()
        g._logister_started_at_wall = datetime.now(UTC)
        g._logister_transaction_recorded = False
        g._logister_exception_reported = False
        g._logister_transaction_name = safely(_transaction_name, request, transaction_namer) or _default_name(request)

    @app.after_request
    def logister_after_request(response: Any) -> Any:
        safely(record_response, response)
        return response

    def record_response(response):
        g, request = _flask_state()
        trace = _trace(g, request)
        status_code = getattr(response, "status_code", None)
        record_transaction(g, request, trace, status_code)
        if hasattr(response, "headers"):
            response.headers["x-request-id"] = trace.request_id

    def record_transaction(g, request, trace, status_code):
        g._logister_transaction_recorded = True
        name = getattr(g, "_logister_transaction_name", None) or _default_name(request)
        duration_ms = _duration_ms(getattr(g, "_logister_started_at", None))
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
            safely(
                client.capture_span,
                name,
                duration_ms,
                context=context,
                trace_id=trace.trace_id,
                request_id=trace.request_id,
                span_id=trace.span_id, parent_span_id=trace.parent_span_id,
                kind="server",
                status="error" if status_code and status_code >= 500 else "ok",
                started_at=getattr(g, "_logister_started_at_wall", None),
                ended_at=datetime.now(UTC),
            )

    @app.teardown_request
    def logister_teardown_request(exception: BaseException | None) -> None:
        try:
            safely(record_teardown, exception)
        finally:
            safely(reset_trace)

    def reset_trace():
        g, _ = _flask_state()
        token = getattr(g, "_logister_trace_token", None)
        if token is not None:
            del g._logister_trace_token
            _current.reset(token)

    def record_teardown(exception):
        if exception is None:
            return None

        g, request = _flask_state()
        trace = _trace(g, request)
        if not getattr(g, "_logister_transaction_recorded", False):
            record_transaction(g, request, trace, 500)

        if getattr(g, "_logister_exception_reported", False):
            return None

        g._logister_exception_reported = True
        client.capture_exception(
            exception,
            context=_request_context(request, status_code=500, client_ip=captures_client_ip(client)),
            trace_id=trace.trace_id,
            request_id=trace.request_id,
        )
        return None

    extensions["logister_flask_installed"] = True
    extensions["logister_client"] = client
    return app


def _flask_state() -> tuple[Any, Any]:
    try:
        from flask import g, request
    except ImportError as exc:
        raise RuntimeError("Install Flask support with `pip install logister-python[flask]`.") from exc
    return g, request


def _transaction_name(request: Any, transaction_namer: TransactionNamer | None) -> str:
    if transaction_namer is not None:
        return transaction_namer(request)
    return _default_name(request)


def _default_name(request: Any) -> str:
    return f"{getattr(request, 'method', 'GET')} {getattr(request, 'path', '/')}"


def _trace(g: Any, request: Any) -> TraceContext:
    trace = getattr(g, "_logister_trace", None)
    if trace is None:
        trace = TraceContext.from_headers(_header(request, "traceparent"), _header(request, "X-Request-Id"), _header(request, "X-Trace-Id"))
        g._logister_trace = trace
    return trace


def _request_context(request: Any, *, status_code: int | None, client_ip: bool = False) -> dict[str, Any]:
    headers = {
        key: value
        for key in ("Host", "User-Agent", "Accept", "Referer", "X-Forwarded-For", "X-Request-Id", "X-Trace-Id")
        if (value := _header(request, key)) and (client_ip or key != "X-Forwarded-For")
    }
    context = {
        "framework": "flask",
        "method": getattr(request, "method", "GET"),
        "path": getattr(request, "path", "/"),
        "status_code": status_code,
        "url": getattr(request, "url", None),
        "request": {
            "method": getattr(request, "method", "GET"),
            "path": getattr(request, "path", "/"),
            "url": getattr(request, "url", None),
            "headers": headers,
            "query_string": None,
            "endpoint": getattr(request, "endpoint", None),
            "blueprint": getattr(request, "blueprint", None),
            "view_args": dict(getattr(request, "view_args", {}) or {}),
        },
    }

    query_string = getattr(request, "query_string", b"")
    if isinstance(query_string, bytes):
        decoded = query_string.decode("utf-8", errors="ignore")
    else:
        decoded = str(query_string or "")
    if decoded:
        context["query_string"] = decoded
        context["request"]["query_string"] = decoded

    remote_addr = getattr(request, "remote_addr", None) if client_ip else None
    if remote_addr:
        context["client_ip"] = remote_addr
        context["request"]["client_ip"] = remote_addr

    endpoint = getattr(request, "endpoint", None)
    if endpoint:
        context["endpoint"] = endpoint

    blueprint = getattr(request, "blueprint", None)
    if blueprint:
        context["blueprint"] = blueprint

    return context


def _header(request: Any, name: str) -> str | None:
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    value = headers.get(name)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _duration_ms(started_at: float | None) -> float:
    if started_at is None:
        return 0.0
    return (perf_counter() - started_at) * 1000.0
