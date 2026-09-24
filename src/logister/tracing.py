"""Explicit request identity and task-local propagation. No global last request."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import re
import secrets
from urllib.parse import urlsplit
from uuid import uuid4


@dataclass(frozen=True)
class TraceContext:
    trace_id: str = field(default_factory=lambda: secrets.token_hex(16))
    span_id: str = field(default_factory=lambda: secrets.token_hex(8))
    parent_span_id: str | None = None
    request_id: str = field(default_factory=lambda: str(uuid4()))
    flags: str = "01"

    @classmethod
    def from_headers(cls, traceparent=None, request_id=None, legacy_trace_id=None):
        match = re.fullmatch(r"00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})", traceparent or "")
        valid = match and match[1] != "0" * 32 and match[2] != "0" * 16
        trace_id = match[1] if valid else legacy_trace_id if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", legacy_trace_id or "") else secrets.token_hex(16)
        request_id = request_id if re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", request_id or "") else str(uuid4())
        return cls(trace_id=trace_id, parent_span_id=match[2] if valid else None, request_id=request_id, flags=match[3] if valid else "01")

    def child(self):
        return TraceContext(trace_id=self.trace_id, parent_span_id=self.span_id, request_id=self.request_id, flags=self.flags)

    @property
    def traceparent(self):
        return f"00-{self.trace_id}-{self.span_id}-{self.flags}"

    def fields(self):
        return {key: value for key, value in {"trace_id": self.trace_id, "span_id": self.span_id,
                "parent_span_id": self.parent_span_id, "request_id": self.request_id}.items() if value is not None}

    def headers_for(self, url, *, allowed_origins):
        """Re-evaluate per redirect hop, or set follow_redirects=False on the HTTP client."""
        origin = _origin(url)
        if origin is None or origin not in {_origin(value) for value in allowed_origins}:
            return {}
        if (not re.fullmatch(r"00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}", self.traceparent)
                or self.trace_id == "0" * 32 or self.span_id == "0" * 16
                or not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", self.request_id)):
            return {}  # Opaque legacy IDs cannot become W3C headers.
        return {"traceparent": self.traceparent, "x-request-id": self.request_id}


def _origin(value):
    try:
        url = urlsplit(str(value))
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
            return None
        return url.scheme, url.hostname.lower(), url.port or (443 if url.scheme == "https" else 80)
    except ValueError:
        return None


_current: ContextVar[TraceContext | None] = ContextVar("logister_trace_context", default=None)


def current_trace_context():
    return _current.get()


@contextmanager
def trace_scope(trace):
    token = _current.set(trace)
    try:
        yield trace
    finally:
        _current.reset(token)


def outbound_trace_context():
    return (current_trace_context() or TraceContext()).child()
