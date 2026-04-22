from __future__ import annotations

from time import perf_counter
from typing import Any, Callable

from .client import LogisterClient

MonitorSlugFactory = Callable[[Any], str | None]


def instrument_celery(
    celery_app: Any,
    client: LogisterClient,
    *,
    signals_module: Any | None = None,
    monitor_slug_factory: MonitorSlugFactory | None = None,
    capture_retries: bool = True,
) -> Any:
    if getattr(celery_app, "_logister_celery_installed", False):
        return celery_app

    signals = signals_module or _import_celery_signals()
    started_at: dict[str, float] = {}

    def on_task_prerun(
        sender: Any = None,
        task_id: str | None = None,
        task: Any = None,
        args: list[Any] | tuple[Any, ...] | None = None,
        kwargs: dict[str, Any] | None = None,
        **_: Any,
    ) -> None:
        if task_id:
            started_at[task_id] = perf_counter()

    def on_task_postrun(
        sender: Any = None,
        task_id: str | None = None,
        task: Any = None,
        args: list[Any] | tuple[Any, ...] | None = None,
        kwargs: dict[str, Any] | None = None,
        state: str | None = None,
        retval: Any = None,
        **_: Any,
    ) -> None:
        task_name = _task_name(sender, task)
        duration_ms = _duration_ms(started_at.pop(task_id, None))
        context = _task_context(task_name, task_id, task, args, kwargs, state=state, retval=retval)

        client.capture_transaction(
            task_name,
            duration_ms,
            context=context,
            request_id=task_id,
        )

        if monitor_slug_factory is not None:
            slug = monitor_slug_factory(task or sender)
            if slug:
                monitor_status = "ok" if (state or "").upper() == "SUCCESS" else "error"
                client.check_in(
                    slug,
                    monitor_status,
                    context={"framework": "celery", "task_name": task_name},
                    duration_ms=duration_ms,
                    request_id=task_id,
                )

    def on_task_failure(
        sender: Any = None,
        task_id: str | None = None,
        exception: BaseException | None = None,
        args: list[Any] | tuple[Any, ...] | None = None,
        kwargs: dict[str, Any] | None = None,
        traceback: Any = None,
        einfo: Any = None,
        **_: Any,
    ) -> None:
        if exception is None:
            return
        task_name = _task_name(sender, None)
        client.capture_exception(
            exception,
            context=_task_context(task_name, task_id, None, args, kwargs, state="FAILURE"),
            request_id=task_id,
            fingerprint=task_name,
        )

    def on_task_retry(request: Any = None, reason: BaseException | str | None = None, einfo: Any = None, **_: Any) -> None:
        if not capture_retries:
            return
        task_name = getattr(request, "task", None) or getattr(request, "task_name", None) or "celery.task"
        task_id = getattr(request, "id", None)
        retries = getattr(request, "retries", None)
        client.capture_message(
            f"Celery task retry scheduled: {task_name}",
            level="warning",
            context={
                "framework": "celery",
                "task_name": task_name,
                "retry_reason": str(reason) if reason is not None else None,
                "retries": retries,
            },
            request_id=task_id,
        )

    signals.task_prerun.connect(on_task_prerun, weak=False)
    signals.task_postrun.connect(on_task_postrun, weak=False)
    signals.task_failure.connect(on_task_failure, weak=False)
    if capture_retries:
        signals.task_retry.connect(on_task_retry, weak=False)

    celery_app._logister_celery_installed = True
    celery_app.logister_client = client
    return celery_app


def _import_celery_signals() -> Any:
    try:
        from celery import signals
    except ImportError as exc:
        raise RuntimeError("Install celery support with `pip install logister-python[celery]`.") from exc
    return signals


def _task_name(sender: Any, task: Any) -> str:
    return (
        getattr(task, "name", None)
        or getattr(sender, "name", None)
        or str(sender)
        or "celery.task"
    )


def _duration_ms(start: float | None) -> float:
    if start is None:
        return 0.0
    return (perf_counter() - start) * 1000.0


def _task_context(
    task_name: str,
    task_id: str | None,
    task: Any,
    args: list[Any] | tuple[Any, ...] | None,
    kwargs: dict[str, Any] | None,
    *,
    state: str | None = None,
    retval: Any = None,
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "framework": "celery",
        "task_name": task_name,
        "task_id": task_id,
    }
    if state:
        context["task_state"] = state
    if args:
        context["task_args_count"] = len(args)
    if kwargs:
        context["task_kwargs_keys"] = sorted(kwargs.keys())
    if retval is not None and isinstance(retval, (str, int, float, bool)):
        context["task_result"] = retval
    task_module = getattr(task, "__module__", None) or getattr(getattr(task, "__class__", None), "__module__", None)
    if task_module:
        context["task_module"] = task_module
    delivery_info = getattr(getattr(task, "request", None), "delivery_info", None) or {}
    if isinstance(delivery_info, dict):
        queue = delivery_info.get("routing_key") or delivery_info.get("exchange")
        if queue:
            context["queue"] = queue
    retries = getattr(getattr(task, "request", None), "retries", None)
    if retries is not None:
        context["retries"] = retries
    eta = getattr(getattr(task, "request", None), "eta", None)
    if eta is not None:
        context["eta"] = str(eta)
    hostname = getattr(getattr(task, "request", None), "hostname", None)
    if hostname:
        context["hostname"] = hostname
    return context
