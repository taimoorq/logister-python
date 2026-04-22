from __future__ import annotations

from time import perf_counter
from typing import Any, Callable

from .client import LogisterClient

TransactionNamer = Callable[[Any], str]


def instrument_flask(
    app: Any,
    client: LogisterClient,
    *,
    transaction_namer: TransactionNamer | None = None,
) -> Any:
    extensions = getattr(app, "extensions", None)
    if extensions is None:
        extensions = {}
        app.extensions = extensions

    if extensions.get("logister_flask_installed"):
        return app

    @app.before_request
    def logister_before_request() -> None:
        g, request = _flask_state()
        g._logister_started_at = perf_counter()
        g._logister_transaction_name = _transaction_name(request, transaction_namer)
        g._logister_transaction_recorded = False
        g._logister_exception_reported = False

    @app.after_request
    def logister_after_request(response: Any) -> Any:
        g, request = _flask_state()
        started_at = getattr(g, "_logister_started_at", None)
        duration_ms = _duration_ms(started_at)
        client.capture_transaction(
            getattr(g, "_logister_transaction_name", _transaction_name(request, transaction_namer)),
            duration_ms,
            context=_request_context(request, status_code=getattr(response, "status_code", None)),
            trace_id=_header(request, "X-Trace-Id"),
            request_id=_header(request, "X-Request-Id"),
        )
        g._logister_transaction_recorded = True
        return response

    @app.teardown_request
    def logister_teardown_request(exception: BaseException | None) -> None:
        if exception is None:
            return None

        g, request = _flask_state()
        if not getattr(g, "_logister_transaction_recorded", False):
            client.capture_transaction(
                getattr(g, "_logister_transaction_name", _transaction_name(request, transaction_namer)),
                _duration_ms(getattr(g, "_logister_started_at", None)),
                context=_request_context(request, status_code=500),
                trace_id=_header(request, "X-Trace-Id"),
                request_id=_header(request, "X-Request-Id"),
            )
            g._logister_transaction_recorded = True

        if getattr(g, "_logister_exception_reported", False):
            return None

        client.capture_exception(
            exception,
            context=_request_context(request, status_code=500),
            trace_id=_header(request, "X-Trace-Id"),
            request_id=_header(request, "X-Request-Id"),
        )
        g._logister_exception_reported = True
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
    return f"{request.method} {request.path}"


def _request_context(request: Any, *, status_code: int | None) -> dict[str, Any]:
    context = {
        "framework": "flask",
        "method": getattr(request, "method", "GET"),
        "path": getattr(request, "path", "/"),
        "status_code": status_code,
    }

    query_string = getattr(request, "query_string", b"")
    if isinstance(query_string, bytes):
        decoded = query_string.decode("utf-8", errors="ignore")
    else:
        decoded = str(query_string or "")
    if decoded:
        context["query_string"] = decoded

    remote_addr = getattr(request, "remote_addr", None)
    if remote_addr:
        context["client_ip"] = remote_addr

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
