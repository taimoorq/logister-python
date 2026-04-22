from __future__ import annotations

import os
import platform
import socket
import sys
import traceback as traceback_module
from dataclasses import dataclass, field
from datetime import datetime, timezone
from os import getpid
from typing import Any, Mapping

import httpx


class LogisterError(RuntimeError):
    pass


@dataclass(slots=True)
class LogisterClient:
    api_key: str
    base_url: str = "https://logister.org"
    timeout: float = 5.0
    environment: str | None = None
    release: str | None = None
    default_context: Mapping[str, Any] | None = None
    capture_locals: bool = False
    user_agent: str = "logister-python/0.2.0"
    _http_client: httpx.Client | None = field(default=None, init=False, repr=False)

    @classmethod
    def from_env(
        cls,
        *,
        api_key_var: str = "LOGISTER_API_KEY",
        base_url_var: str = "LOGISTER_BASE_URL",
        timeout_var: str = "LOGISTER_TIMEOUT",
        environment_var: str = "LOGISTER_ENVIRONMENT",
        release_var: str = "LOGISTER_RELEASE",
        capture_locals_var: str = "LOGISTER_CAPTURE_LOCALS",
        default_context: Mapping[str, Any] | None = None,
    ) -> "LogisterClient":
        api_key = os.getenv(api_key_var, "").strip()
        if not api_key:
            raise ValueError(f"Missing required environment variable: {api_key_var}")

        timeout_value = os.getenv(timeout_var, "").strip()
        timeout = float(timeout_value) if timeout_value else 5.0
        capture_locals = _env_bool(os.getenv(capture_locals_var, ""))

        return cls(
            api_key=api_key,
            base_url=os.getenv(base_url_var, "https://logister.org").strip() or "https://logister.org",
            timeout=timeout,
            environment=os.getenv(environment_var, "").strip() or None,
            release=os.getenv(release_var, "").strip() or None,
            default_context=default_context,
            capture_locals=capture_locals,
        )

    def capture_exception(
        self,
        error: BaseException,
        *,
        context: Mapping[str, Any] | None = None,
        message: str | None = None,
        fingerprint: str | None = None,
        occurred_at: str | datetime | None = None,
        environment: str | None = None,
        release: str | None = None,
        trace_id: str | None = None,
        request_id: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        error_context = dict(context or {})
        error_context.setdefault(
            "exception",
            self._exception_payload(error),
        )
        return self.send_event(
            event_type="error",
            level="error",
            message=message or str(error) or error.__class__.__name__,
            context=error_context,
            fingerprint=fingerprint,
            occurred_at=occurred_at,
            environment=environment,
            release=release,
            trace_id=trace_id,
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
        )

    def capture_message(
        self,
        message: str,
        *,
        level: str = "info",
        context: Mapping[str, Any] | None = None,
        fingerprint: str | None = None,
        occurred_at: str | datetime | None = None,
        environment: str | None = None,
        release: str | None = None,
        trace_id: str | None = None,
        request_id: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        return self.send_event(
            event_type="log",
            level=level,
            message=message,
            context=context,
            fingerprint=fingerprint,
            occurred_at=occurred_at,
            environment=environment,
            release=release,
            trace_id=trace_id,
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
        )

    def capture_metric(
        self,
        name: str,
        value: float | int,
        *,
        context: Mapping[str, Any] | None = None,
        occurred_at: str | datetime | None = None,
        environment: str | None = None,
        release: str | None = None,
        trace_id: str | None = None,
        request_id: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        metric_context = dict(context or {})
        metric_context.setdefault("metric", {"name": name, "value": value})
        return self.send_event(
            event_type="metric",
            level="info",
            message=name,
            context=metric_context,
            occurred_at=occurred_at,
            environment=environment,
            release=release,
            trace_id=trace_id,
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
        )

    def capture_transaction(
        self,
        name: str,
        duration_ms: float | int,
        *,
        context: Mapping[str, Any] | None = None,
        occurred_at: str | datetime | None = None,
        environment: str | None = None,
        release: str | None = None,
        trace_id: str | None = None,
        request_id: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        return self.send_event(
            event_type="transaction",
            level="info",
            message=name,
            context=context,
            occurred_at=occurred_at,
            environment=environment,
            release=release,
            trace_id=trace_id,
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            transaction_name=name,
            duration_ms=duration_ms,
        )

    def check_in(
        self,
        slug: str,
        status: str = "ok",
        *,
        context: Mapping[str, Any] | None = None,
        occurred_at: str | datetime | None = None,
        environment: str | None = None,
        release: str | None = None,
        expected_interval_seconds: int | None = None,
        duration_ms: float | int | None = None,
        trace_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "slug": slug,
            "status": status,
            "occurred_at": self._normalize_timestamp(occurred_at),
        }
        if environment or self.environment:
            payload["environment"] = environment or self.environment
        if release or self.release:
            payload["release"] = release or self.release
        if expected_interval_seconds is not None:
            payload["expected_interval_seconds"] = expected_interval_seconds
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms
        if trace_id:
            payload["trace_id"] = trace_id
        if request_id:
            payload["request_id"] = request_id

        context_data = dict(context or {})
        if "environment" not in payload and context_data.get("environment"):
            payload["environment"] = context_data["environment"]
        if "release" not in payload and context_data.get("release"):
            payload["release"] = context_data["release"]
        if "trace_id" not in payload and context_data.get("trace_id"):
            payload["trace_id"] = context_data["trace_id"]
        if "request_id" not in payload and context_data.get("request_id"):
            payload["request_id"] = context_data["request_id"]
        if "expected_interval_seconds" not in payload and context_data.get("expected_interval_seconds") is not None:
            payload["expected_interval_seconds"] = context_data["expected_interval_seconds"]
        if "duration_ms" not in payload and context_data.get("duration_ms") is not None:
            payload["duration_ms"] = context_data["duration_ms"]

        return self._post("/api/v1/check_ins", {"check_in": payload})

    def send_event(
        self,
        *,
        event_type: str,
        level: str,
        message: str,
        context: Mapping[str, Any] | None = None,
        fingerprint: str | None = None,
        occurred_at: str | datetime | None = None,
        environment: str | None = None,
        release: str | None = None,
        trace_id: str | None = None,
        request_id: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        transaction_name: str | None = None,
        duration_ms: float | int | None = None,
        expected_interval_seconds: int | None = None,
        check_in_slug: str | None = None,
        check_in_status: str | None = None,
    ) -> dict[str, Any]:
        event_payload: dict[str, Any] = {
            "event_type": event_type,
            "level": level,
            "message": message,
            "context": self._build_context(
                context=context,
                environment=environment,
                release=release,
                trace_id=trace_id,
                request_id=request_id,
                session_id=session_id,
                user_id=user_id,
                transaction_name=transaction_name,
                duration_ms=duration_ms,
                expected_interval_seconds=expected_interval_seconds,
                check_in_slug=check_in_slug,
                check_in_status=check_in_status,
            ),
            "occurred_at": self._normalize_timestamp(occurred_at),
        }
        if fingerprint:
            event_payload["fingerprint"] = fingerprint
        return self._post("/api/v1/ingest_events", {"event": event_payload})

    def close(self) -> None:
        if self._http_client is not None:
            self._http_client.close()
            self._http_client = None

    def _exception_payload(self, error: BaseException) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "class": error.__class__.__name__,
            "qualified_class": f"{error.__class__.__module__}.{error.__class__.__qualname__}",
            "module": error.__class__.__module__,
            "message": str(error),
        }

        if error.__traceback__ is None:
            return payload

        frames, backtrace = self._traceback_frames(error.__traceback__)
        if frames:
            payload["frames"] = frames
        if backtrace:
            payload["backtrace"] = backtrace

        cause = self._nested_exception_payload(error.__cause__)
        if cause:
            payload["cause"] = cause

        context = self._nested_exception_payload(error.__context__)
        if context and error.__context__ is not error.__cause__ and not getattr(error, "__suppress_context__", False):
            payload["context"] = context

        return payload

    def _nested_exception_payload(self, error: BaseException | None, *, depth: int = 0) -> dict[str, Any] | None:
        if error is None or depth >= 3:
            return None

        payload: dict[str, Any] = {
            "class": error.__class__.__name__,
            "qualified_class": f"{error.__class__.__module__}.{error.__class__.__qualname__}",
            "module": error.__class__.__module__,
            "message": str(error),
        }

        if error.__traceback__ is not None:
            frames, backtrace = self._traceback_frames(error.__traceback__)
            if frames:
                payload["frames"] = frames
            if backtrace:
                payload["backtrace"] = backtrace

        cause = self._nested_exception_payload(error.__cause__, depth=depth + 1)
        if cause:
            payload["cause"] = cause

        context = self._nested_exception_payload(error.__context__, depth=depth + 1)
        if context and error.__context__ is not error.__cause__ and not getattr(error, "__suppress_context__", False):
            payload["context"] = context

        return payload

    def _traceback_frames(self, tb: Any) -> tuple[list[dict[str, Any]], list[str]]:
        frames: list[dict[str, Any]] = []
        backtrace: list[str] = []

        current = tb
        while current is not None:
            frame = current.tb_frame
            frame_payload: dict[str, Any] = {
                "filename": frame.f_code.co_filename,
                "lineno": current.tb_lineno,
                "name": frame.f_code.co_name,
                "line": traceback_module.extract_tb(current, limit=1)[0].line,
            }
            if self.capture_locals:
                serialized_locals = _serialize_locals(frame.f_locals)
                if serialized_locals:
                    frame_payload["locals"] = serialized_locals
            frames.append(frame_payload)
            backtrace.append(
                f'File "{frame_payload["filename"]}", line {frame_payload["lineno"]}, in {frame_payload["name"]}'
            )
            current = current.tb_next

        return frames, backtrace

    def __enter__(self) -> "LogisterClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._http().post(path, json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LogisterError(f"Logister request failed: {exc}") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise LogisterError("Logister response was not valid JSON") from exc

        return data if isinstance(data, dict) else {"data": data}

    def _http(self) -> httpx.Client:
        if self._http_client is None:
            self._http_client = httpx.Client(
                base_url=self.base_url.rstrip("/"),
                timeout=self.timeout,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": self.user_agent,
                },
            )
        return self._http_client

    def _build_context(
        self,
        *,
        context: Mapping[str, Any] | None = None,
        environment: str | None = None,
        release: str | None = None,
        trace_id: str | None = None,
        request_id: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        transaction_name: str | None = None,
        duration_ms: float | int | None = None,
        expected_interval_seconds: int | None = None,
        check_in_slug: str | None = None,
        check_in_status: str | None = None,
    ) -> dict[str, Any]:
        merged = dict(self.default_context or {})
        merged.update(dict(context or {}))

        self._set_if_missing(merged, "environment", environment or self.environment)
        self._set_if_missing(merged, "release", release or self.release)
        self._set_if_missing(merged, "trace_id", trace_id)
        self._set_if_missing(merged, "request_id", request_id)
        self._set_if_missing(merged, "session_id", session_id)
        self._set_if_missing(merged, "user_id", user_id)
        self._set_if_missing(merged, "runtime", "python")
        self._set_if_missing(merged, "python_version", platform.python_version())
        self._set_if_missing(merged, "python_implementation", platform.python_implementation())
        self._set_if_missing(merged, "platform", platform.platform())
        self._set_if_missing(merged, "hostname", socket.gethostname())
        self._set_if_missing(merged, "process_id", getpid())
        self._set_if_missing(merged, "runtime_name", sys.executable)
        self._set_if_missing(merged, "transaction_name", transaction_name)
        self._set_if_missing(merged, "duration_ms", duration_ms)
        self._set_if_missing(merged, "expected_interval_seconds", expected_interval_seconds)
        self._set_if_missing(merged, "check_in_slug", check_in_slug)
        self._set_if_missing(merged, "check_in_status", check_in_status)

        return merged

    def _normalize_timestamp(self, value: str | datetime | None) -> str:
        if value is None:
            return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if isinstance(value, datetime):
            normalized = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
            return normalized.isoformat().replace("+00:00", "Z")
        return value

    @staticmethod
    def _set_if_missing(payload: dict[str, Any], key: str, value: Any) -> None:
        if value is None or value == "":
            return
        if payload.get(key) is not None:
            return
        payload[key] = value


def _env_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _safe_repr(value: Any, *, max_length: int = 200) -> str:
    try:
        rendered = repr(value)
    except Exception:
        rendered = object.__repr__(value)
    if len(rendered) > max_length:
        return f"{rendered[:max_length - 3]}..."
    return rendered


def _serialize_locals(values: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(key): _safe_repr(value)
        for key, value in values.items()
        if not str(key).startswith("__")
    }
