from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Mapping

from ._guard import safely
from .client import LogisterClient

_RESERVED_RECORD_ATTRS = frozenset(vars(logging.makeLogRecord({})).keys())
# Records from the SDK and its transport would otherwise be captured and re-sent forever.
_IGNORED_LOGGERS = ("logister", "httpx", "httpcore")
_emitting: ContextVar[bool] = ContextVar("logister_logging_emit", default=False)


class LogisterLoggingHandler(logging.Handler):
    def __init__(
        self,
        client: LogisterClient,
        *,
        level: int = logging.NOTSET,
        context: Mapping[str, Any] | None = None,
        capture_exceptions: bool = True,
    ) -> None:
        super().__init__(level=level)
        self.client = client
        self.context = dict(context or {})
        self.capture_exceptions = capture_exceptions

    def emit(self, record: logging.LogRecord) -> None:
        if _emitting.get() or _is_ignored(record.name):
            return
        token = _emitting.set(True)
        try:
            # Inside an event loop, hand delivery to a background thread instead of blocking.
            safely(self._capture, record, defer=_in_event_loop())
        finally:
            _emitting.reset(token)

    def _capture(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        context = _record_context(record, self.context)
        occurred_at = datetime.fromtimestamp(record.created, tz=timezone.utc)
        trace_id = _record_value(record, "trace_id")
        request_id = _record_value(record, "request_id")
        session_id = _record_value(record, "session_id")
        user_id = _record_value(record, "user_id")

        if self.capture_exceptions and record.exc_info and record.exc_info[1] is not None:
            self.client.capture_exception(
                record.exc_info[1],
                message=message,
                context=context,
                occurred_at=occurred_at,
                trace_id=trace_id,
                request_id=request_id,
                session_id=session_id,
                user_id=user_id,
            )
            return

        self.client.capture_message(
            message,
            level=record.levelname.lower(),
            context=context,
            occurred_at=occurred_at,
            trace_id=trace_id,
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
        )


def instrument_logging(
    client: LogisterClient,
    *,
    logger: logging.Logger | None = None,
    level: int = logging.NOTSET,
    context: Mapping[str, Any] | None = None,
    capture_exceptions: bool = True,
    propagate: bool | None = None,
) -> LogisterLoggingHandler:
    target_logger = logger or logging.getLogger()
    existing_handler = next(
        (
            handler
            for handler in target_logger.handlers
            if isinstance(handler, LogisterLoggingHandler) and handler.client is client
        ),
        None,
    )
    if existing_handler is not None:
        if propagate is not None:
            target_logger.propagate = propagate
        return existing_handler

    handler = LogisterLoggingHandler(
        client,
        level=level,
        context=context,
        capture_exceptions=capture_exceptions,
    )
    target_logger.addHandler(handler)
    if propagate is not None:
        target_logger.propagate = propagate
    return handler


def _is_ignored(name: str) -> bool:
    return any(name == prefix or name.startswith(f"{prefix}.") for prefix in _IGNORED_LOGGERS)


def _in_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _record_context(record: logging.LogRecord, default_context: Mapping[str, Any]) -> dict[str, Any]:
    extra = _record_extra(record)
    logger_context: dict[str, Any] = {
        "logger_name": record.name,
        "logger": {
            "name": record.name,
            "module": record.module,
            "pathname": record.pathname,
            "filename": record.filename,
            "function": record.funcName,
            "line_number": record.lineno,
            "process": record.process,
            "thread": record.thread,
        },
    }
    if extra:
        logger_context["log_record"] = extra
    return {**dict(default_context), **logger_context}


def _record_extra(record: logging.LogRecord) -> dict[str, Any]:
    return {
        key: _serialize_value(value)
        for key, value in vars(record).items()
        if key not in _RESERVED_RECORD_ATTRS and key not in {"message", "asctime"}
    }


def _record_value(record: logging.LogRecord, key: str) -> str | None:
    value = getattr(record, key, None)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _serialize_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(key): _serialize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_serialize_value(item) for item in value]
    return repr(value)
