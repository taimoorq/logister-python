"""Default redaction shared by every Logister SDK.

A key is sensitive when, lowercased with every non-alphanumeric character removed,
it equals one of ``SENSITIVE_KEYS`` or contains one of ``SENSITIVE_KEY_FRAGMENTS``.
Strings are scrubbed for bearer/basic credentials, JWTs and Logister keys, and URLs
keep scheme, host and path while sensitive query parameter values are replaced.
"""
from __future__ import annotations

import math
import re
from typing import Any, Iterable, Mapping
from urllib.parse import unquote_plus, urlsplit, urlunsplit

REDACTED = "[REDACTED]"

SENSITIVE_KEYS = frozenset({
    "password", "passwd", "pwd", "secret", "token", "apikey", "authorization",
    "proxyauthorization", "cookie", "setcookie", "sessiontoken", "sessionkey",
    "credential", "credentials", "privatekey", "clientsecret", "accesstoken",
    "refreshtoken", "idtoken", "authtoken", "xapikey", "xauthtoken", "xaccesstoken",
    "xcsrftoken", "xxsrftoken", "signature", "ssn", "creditcard", "cardnumber", "cvv",
})
SENSITIVE_KEY_FRAGMENTS = (
    "token", "secret", "password", "passwd", "apikey", "authorization", "credential", "cookie",
)

# Keys whose string values are URLs or raw query strings.
_URL_KEYS = frozenset({"url", "uri", "href", "referer", "referrer", "location"})
_QUERY_KEYS = frozenset({"querystring", "query"})

_AUTH_SCHEME = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_LOGISTER_KEY = re.compile(r"\blgst_[A-Za-z0-9_-]+")
_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)

_MAX_DEPTH = 32


def normalize_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def is_sensitive_key(key: Any, extra_keys: Iterable[str] = ()) -> bool:
    normalized = normalize_key(key)
    if not normalized:
        return False
    if normalized in SENSITIVE_KEYS or any(fragment in normalized for fragment in SENSITIVE_KEY_FRAGMENTS):
        return True
    return normalized in {normalize_key(extra) for extra in extra_keys}


def scrub_string(value: str) -> str:
    """Replace credential-shaped substrings and sensitive query values in free text."""
    value = _AUTH_SCHEME.sub(lambda match: f"{match.group(1)} {REDACTED}", value)
    value = _JWT.sub(REDACTED, value)
    value = _LOGISTER_KEY.sub(REDACTED, value)
    if "://" in value:
        value = _URL_IN_TEXT.sub(lambda match: redact_url(match.group(0)), value)
    return value


def redact_query_string(query: str, extra_keys: Iterable[str] = ()) -> str:
    """Redact values of sensitive parameters, preserving order and raw encoding."""
    if not query:
        return query
    parts = []
    for pair in query.split("&"):
        name, separator, _ = pair.partition("=")
        if separator and is_sensitive_key(unquote_plus(name), extra_keys):
            parts.append(f"{name}={REDACTED}")
        else:
            parts.append(pair)
    return "&".join(parts)


def redact_url(url: str, extra_keys: Iterable[str] = ()) -> str:
    """Keep scheme, host and path; drop userinfo; redact sensitive query/fragment values."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return REDACTED
    netloc = parts.netloc
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
    fragment = redact_query_string(parts.fragment, extra_keys) if "=" in parts.fragment else parts.fragment
    return urlunsplit((parts.scheme, netloc, parts.path, redact_query_string(parts.query, extra_keys), fragment))


def redact(value: Any, extra_keys: Iterable[str] = ()) -> Any:
    """Return a JSON-friendly copy of ``value`` with the default redaction applied."""
    extra = frozenset(normalize_key(key) for key in extra_keys)
    return _redact(value, None, extra, 0, set())


def _redact(value: Any, key: str | None, extra: frozenset[str], depth: int, seen: set[int]) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        normalized = normalize_key(key) if key is not None else ""
        if normalized in _QUERY_KEYS:
            return scrub_string(redact_query_string(value, extra))
        if normalized in _URL_KEYS or value[:8].lower().startswith(("http://", "https://")):
            return scrub_string(redact_url(value, extra))
        return scrub_string(value)
    if isinstance(value, bytes):
        return _redact(value.decode("utf-8", errors="replace"), key, extra, depth, seen)
    if depth >= _MAX_DEPTH:
        return "[MAX_DEPTH]"
    if isinstance(value, Mapping):
        if id(value) in seen:
            return "[CIRCULAR]"
        seen.add(id(value))
        try:
            return {
                str(item_key): REDACTED if is_sensitive_key(item_key, extra) else _redact(item, str(item_key), extra, depth + 1, seen)
                for item_key, item in value.items()
            }
        finally:
            seen.discard(id(value))
    if isinstance(value, (list, tuple, set, frozenset)):
        if id(value) in seen:
            return "[CIRCULAR]"
        seen.add(id(value))
        try:
            return [_redact(item, key, extra, depth + 1, seen) for item in value]
        finally:
            seen.discard(id(value))
    # Leave other objects (UUID, datetime, Decimal, ...) to the JSON fallback.
    return value
