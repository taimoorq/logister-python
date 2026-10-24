"""Integration safety: automatic capture must never raise into the host application."""
from __future__ import annotations

import logging
from contextlib import nullcontext
from typing import Any, Callable

from .client import deferred_delivery

_logger = logging.getLogger("logister")


def safely(function: Callable[..., Any], /, *args: Any, defer: bool = False, **kwargs: Any) -> Any:
    """Run an SDK capture step; log any failure at debug and return None."""
    try:
        with deferred_delivery() if defer else nullcontext():
            return function(*args, **kwargs)
    except Exception:
        _logger.debug("Logister capture failed; the event was dropped", exc_info=True)
        return None


def captures_client_ip(client: Any) -> bool:
    # ``is True`` keeps mocks and unknown client objects on the private default.
    return getattr(client, "capture_client_ip", False) is True
