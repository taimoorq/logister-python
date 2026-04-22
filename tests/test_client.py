import asyncio
import logging
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from logister import (
    LogisterClient,
    LogisterLoggingHandler,
    LogisterMiddleware,
    build_django_middleware,
    instrument_celery,
    instrument_fastapi,
    instrument_flask,
    instrument_logging,
)


def test_send_event_wraps_payload_and_sets_auth_header() -> None:
    client = LogisterClient(
        api_key="test-token",
        base_url="https://logister.example",
        environment="production",
        release="2026.04.22",
        default_context={"service": "api"},
    )
    response = Mock()
    response.json.return_value = {"status": "accepted"}
    response.raise_for_status.return_value = None

    with patch("logister.client.httpx.Client", autospec=True) as client_class:
        client_instance = client_class.return_value
        client_instance.post.return_value = response

        result = client.capture_message("Hello", request_id="req-123")

    assert result == {"status": "accepted"}
    client_instance.post.assert_called_once()
    _, kwargs = client_instance.post.call_args
    assert kwargs["json"]["event"]["event_type"] == "log"
    assert kwargs["json"]["event"]["message"] == "Hello"
    assert kwargs["json"]["event"]["context"]["service"] == "api"
    assert kwargs["json"]["event"]["context"]["environment"] == "production"
    assert kwargs["json"]["event"]["context"]["release"] == "2026.04.22"
    assert kwargs["json"]["event"]["context"]["request_id"] == "req-123"
    client_class.assert_called_once()
    _, client_kwargs = client_class.call_args
    assert client_kwargs["headers"]["Authorization"] == "Bearer test-token"


def test_check_in_uses_check_in_root_payload() -> None:
    client = LogisterClient(api_key="test-token", environment="production", release="2026.04.22")
    response = Mock()
    response.json.return_value = {"check_in": "ok"}
    response.raise_for_status.return_value = None

    with patch("logister.client.httpx.Client", autospec=True) as client_class:
        client_instance = client_class.return_value
        client_instance.post.return_value = response

        result = client.check_in("nightly-import", "ok", expected_interval_seconds=600, request_id="req-9")

    assert result == {"check_in": "ok"}
    _, kwargs = client_instance.post.call_args
    assert kwargs["json"]["check_in"]["slug"] == "nightly-import"
    assert kwargs["json"]["check_in"]["status"] == "ok"
    assert kwargs["json"]["check_in"]["expected_interval_seconds"] == 600
    assert kwargs["json"]["check_in"]["environment"] == "production"
    assert kwargs["json"]["check_in"]["release"] == "2026.04.22"
    assert kwargs["json"]["check_in"]["request_id"] == "req-9"
    client_class.assert_called_once()


def test_from_env_builds_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOGISTER_API_KEY", "env-token")
    monkeypatch.setenv("LOGISTER_BASE_URL", "https://logs.example")
    monkeypatch.setenv("LOGISTER_TIMEOUT", "9.5")
    monkeypatch.setenv("LOGISTER_ENVIRONMENT", "staging")
    monkeypatch.setenv("LOGISTER_RELEASE", "sha-123")
    monkeypatch.setenv("LOGISTER_CAPTURE_LOCALS", "true")

    client = LogisterClient.from_env(default_context={"service": "billing"})

    assert client.api_key == "env-token"
    assert client.base_url == "https://logs.example"
    assert client.timeout == 9.5
    assert client.environment == "staging"
    assert client.release == "sha-123"
    assert client.capture_locals is True
    assert client.default_context == {"service": "billing"}


def test_capture_exception_includes_python_traceback_frames() -> None:
    client = LogisterClient(api_key="test-token", base_url="https://logister.example", capture_locals=True)
    response = Mock()
    response.json.return_value = {"status": "accepted"}
    response.raise_for_status.return_value = None

    with patch("logister.client.httpx.Client", autospec=True) as client_class:
        client_instance = client_class.return_value
        client_instance.post.return_value = response

        try:
            try:
                order_id = "ord_123"
                raise ValueError("broken checkout")
            except ValueError as inner:
                raise RuntimeError("checkout failed") from inner
        except RuntimeError as exc:
            client.capture_exception(exc)

    _, kwargs = client_instance.post.call_args
    exception = kwargs["json"]["event"]["context"]["exception"]
    assert exception["class"] == "RuntimeError"
    assert exception["message"] == "checkout failed"
    assert exception["qualified_class"] == "builtins.RuntimeError"
    assert exception["frames"]
    assert exception["backtrace"]
    assert exception["frames"][-1]["name"] == "test_capture_exception_includes_python_traceback_frames"
    assert exception["frames"][-1]["locals"]["order_id"] == "'ord_123'"
    assert exception["cause"]["class"] == "ValueError"
    assert exception["cause"]["message"] == "broken checkout"
    assert kwargs["json"]["event"]["context"]["runtime"] == "python"
    assert "python_version" in kwargs["json"]["event"]["context"]
    assert "hostname" in kwargs["json"]["event"]["context"]


def test_logging_handler_captures_log_records() -> None:
    client = Mock(spec=LogisterClient)
    handler = LogisterLoggingHandler(client, context={"service": "api"})

    logger = logging.getLogger("logister.tests.messages")
    logger.handlers = []
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)

    logger.warning("Cache miss", extra={"request_id": "req-501", "trace_id": "trace-501", "cache": "primary"})

    client.capture_message.assert_called_once()
    _, kwargs = client.capture_message.call_args
    assert kwargs["level"] == "warning"
    assert kwargs["request_id"] == "req-501"
    assert kwargs["trace_id"] == "trace-501"
    assert kwargs["context"]["service"] == "api"
    assert kwargs["context"]["logger_name"] == "logister.tests.messages"
    assert kwargs["context"]["logger"]["function"] == "test_logging_handler_captures_log_records"
    assert kwargs["context"]["log_record"]["cache"] == "primary"


def test_logging_handler_captures_exceptions_from_exc_info() -> None:
    client = Mock(spec=LogisterClient)
    handler = LogisterLoggingHandler(client)

    logger = logging.getLogger("logister.tests.exceptions")
    logger.handlers = []
    logger.propagate = False
    logger.setLevel(logging.ERROR)
    logger.addHandler(handler)

    try:
        raise RuntimeError("worker exploded")
    except RuntimeError:
        logger.exception("Task failed", extra={"request_id": "task-99"})

    client.capture_exception.assert_called_once()
    _, kwargs = client.capture_exception.call_args
    assert kwargs["message"] == "Task failed"
    assert kwargs["request_id"] == "task-99"
    assert kwargs["context"]["logger_name"] == "logister.tests.exceptions"


def test_instrument_logging_attaches_once_per_client() -> None:
    client = Mock(spec=LogisterClient)
    logger = logging.getLogger("logister.tests.instrument")
    logger.handlers = []
    logger.propagate = True

    first_handler = instrument_logging(client, logger=logger, context={"service": "billing"}, propagate=False)
    second_handler = instrument_logging(client, logger=logger, context={"service": "billing"}, propagate=False)

    assert first_handler is second_handler
    assert len(logger.handlers) == 1
    assert isinstance(first_handler, LogisterLoggingHandler)
    assert logger.propagate is False


@dataclass
class FakeURL:
    path: str
    query: str = ""


@dataclass
class FakeRequest:
    method: str
    url: FakeURL
    headers: dict[str, str] = field(default_factory=dict)
    client: SimpleNamespace = field(default_factory=lambda: SimpleNamespace(host="127.0.0.1"))


@dataclass
class FakeResponse:
    status_code: int


@dataclass
class FakeDjangoRequest:
    method: str
    path: str
    META: dict[str, str] = field(default_factory=dict)
    resolver_match: SimpleNamespace | None = None

    def build_absolute_uri(self) -> str:
        return f"https://example.com{self.path}"


class FakeFastAPIApp:
    def __init__(self) -> None:
        self.state = SimpleNamespace()
        self.middlewares: dict[str, object] = {}

    def middleware(self, kind: str):
        def decorator(fn):
            self.middlewares[kind] = fn
            return fn

        return decorator


class FakeFlaskApp:
    def __init__(self) -> None:
        self.extensions: dict[str, object] = {}
        self.before_request_funcs: list[object] = []
        self.after_request_funcs: list[object] = []
        self.teardown_request_funcs: list[object] = []

    def before_request(self, fn):
        self.before_request_funcs.append(fn)
        return fn

    def after_request(self, fn):
        self.after_request_funcs.append(fn)
        return fn

    def teardown_request(self, fn):
        self.teardown_request_funcs.append(fn)
        return fn


def test_fastapi_instrumentation_captures_transaction() -> None:
    app = FakeFastAPIApp()
    client = Mock(spec=LogisterClient)
    instrument_fastapi(app, client)

    middleware = app.middlewares["http"]
    request = FakeRequest(
        method="GET",
        url=FakeURL(path="/health", query="full=true"),
        headers={"x-request-id": "req-1", "x-trace-id": "trace-1"},
    )

    async def call_next(_: FakeRequest) -> FakeResponse:
        return FakeResponse(status_code=204)

    response = asyncio.run(middleware(request, call_next))

    assert response.status_code == 204
    client.capture_transaction.assert_called_once()
    _, kwargs = client.capture_transaction.call_args
    assert kwargs["request_id"] == "req-1"
    assert kwargs["trace_id"] == "trace-1"
    assert kwargs["context"]["framework"] == "fastapi"
    assert kwargs["context"]["path"] == "/health"
    assert kwargs["context"]["status_code"] == 204
    assert kwargs["context"]["request"]["query_string"] == "full=true"
    assert kwargs["context"]["request"]["headers"]["X-Request-Id"] == "req-1"


def test_fastapi_instrumentation_captures_uncaught_exception() -> None:
    app = FakeFastAPIApp()
    client = Mock(spec=LogisterClient)
    instrument_fastapi(app, client)

    middleware = app.middlewares["http"]
    request = FakeRequest(method="POST", url=FakeURL(path="/checkout"))

    async def call_next(_: FakeRequest) -> FakeResponse:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(middleware(request, call_next))

    client.capture_transaction.assert_called_once()
    client.capture_exception.assert_called_once()


def test_django_middleware_captures_transaction() -> None:
    client = Mock(spec=LogisterClient)
    middleware = LogisterMiddleware(
        lambda request: FakeResponse(status_code=201),
        client=client,
    )
    request = FakeDjangoRequest(
        method="POST",
        path="/orders/create",
        META={
            "QUERY_STRING": "debug=true",
            "REMOTE_ADDR": "127.0.0.1",
            "HTTP_X_REQUEST_ID": "req-77",
            "HTTP_X_TRACE_ID": "trace-77",
        },
        resolver_match=SimpleNamespace(route="orders/create"),
    )

    response = middleware(request)

    assert response.status_code == 201
    client.capture_transaction.assert_called_once()
    _, kwargs = client.capture_transaction.call_args
    assert kwargs["request_id"] == "req-77"
    assert kwargs["trace_id"] == "trace-77"
    assert kwargs["context"]["framework"] == "django"
    assert kwargs["context"]["route"] == "orders/create"
    assert kwargs["context"]["status_code"] == 201
    assert kwargs["context"]["url"] == "https://example.com/orders/create"
    assert kwargs["context"]["request"]["headers"]["X-Request-Id"] == "req-77"


def test_django_middleware_captures_exception() -> None:
    client = Mock(spec=LogisterClient)
    middleware = LogisterMiddleware(lambda request: FakeResponse(status_code=200), client=client)
    request = FakeDjangoRequest(method="GET", path="/boom", META={"REMOTE_ADDR": "127.0.0.1"})
    request._logister_started_at = 0.0
    request._logister_transaction_name = "GET /boom"

    middleware.process_exception(request, RuntimeError("boom"))

    client.capture_transaction.assert_called_once()
    client.capture_exception.assert_called_once()


def test_build_django_middleware_binds_client() -> None:
    client = Mock(spec=LogisterClient)
    middleware_class = build_django_middleware(client)
    middleware = middleware_class(lambda request: FakeResponse(status_code=204))
    request = FakeDjangoRequest(method="GET", path="/health")

    middleware(request)

    client.capture_transaction.assert_called_once()


@dataclass
class FakeFlaskRequest:
    method: str
    path: str
    headers: dict[str, str] = field(default_factory=dict)
    query_string: bytes = b""
    remote_addr: str | None = None
    endpoint: str | None = None
    blueprint: str | None = None
    url: str | None = None
    view_args: dict[str, str] = field(default_factory=dict)


@dataclass
class FakeFlaskG:
    _logister_started_at: float | None = None
    _logister_transaction_name: str | None = None
    _logister_transaction_recorded: bool = False
    _logister_exception_reported: bool = False


def test_flask_instrumentation_captures_transaction() -> None:
    app = FakeFlaskApp()
    client = Mock(spec=LogisterClient)
    flask_request = FakeFlaskRequest(
        method="GET",
        path="/health",
        headers={"X-Request-Id": "req-88", "X-Trace-Id": "trace-88"},
        query_string=b"full=true",
        remote_addr="127.0.0.1",
        endpoint="health",
        blueprint="core",
        url="https://example.com/health?full=true",
        view_args={"order_id": "42"},
    )
    flask_g = FakeFlaskG()

    with patch("logister.flask._flask_state", return_value=(flask_g, flask_request)):
        instrument_flask(app, client)
        app.before_request_funcs[0]()
        response = app.after_request_funcs[0](FakeResponse(status_code=204))

    assert response.status_code == 204
    client.capture_transaction.assert_called_once()
    _, kwargs = client.capture_transaction.call_args
    assert kwargs["request_id"] == "req-88"
    assert kwargs["trace_id"] == "trace-88"
    assert kwargs["context"]["framework"] == "flask"
    assert kwargs["context"]["endpoint"] == "health"
    assert kwargs["context"]["blueprint"] == "core"
    assert kwargs["context"]["status_code"] == 204
    assert kwargs["context"]["url"] == "https://example.com/health?full=true"
    assert kwargs["context"]["request"]["view_args"]["order_id"] == "42"


def test_flask_instrumentation_captures_exception() -> None:
    app = FakeFlaskApp()
    client = Mock(spec=LogisterClient)
    flask_request = FakeFlaskRequest(method="POST", path="/checkout", remote_addr="127.0.0.1")
    flask_g = FakeFlaskG(_logister_started_at=0.0, _logister_transaction_name="POST /checkout")

    with patch("logister.flask._flask_state", return_value=(flask_g, flask_request)):
        instrument_flask(app, client)
        app.teardown_request_funcs[0](RuntimeError("boom"))

    client.capture_transaction.assert_called_once()
    client.capture_exception.assert_called_once()


class FakeSignal:
    def __init__(self) -> None:
        self.handlers: list[object] = []

    def connect(self, handler: object, weak: bool = False) -> None:
        self.handlers.append(handler)


def test_celery_instrumentation_wires_signals() -> None:
    signals = SimpleNamespace(
        task_prerun=FakeSignal(),
        task_postrun=FakeSignal(),
        task_failure=FakeSignal(),
        task_retry=FakeSignal(),
    )
    celery_app = SimpleNamespace()
    client = Mock(spec=LogisterClient)

    instrument_celery(celery_app, client, signals_module=signals, monitor_slug_factory=lambda task: getattr(task, "name", None))

    task = SimpleNamespace(name="billing.sync", request=SimpleNamespace(delivery_info={"routing_key": "billing"}, retries=1))
    signals.task_prerun.handlers[0](sender=task, task_id="task-1", task=task, args=[1], kwargs={"force": True})
    signals.task_postrun.handlers[0](sender=task, task_id="task-1", task=task, args=[1], kwargs={"force": True}, state="SUCCESS", retval="ok")
    signals.task_failure.handlers[0](sender=task, task_id="task-1", exception=RuntimeError("broken"), args=[], kwargs={})
    signals.task_retry.handlers[0](request=SimpleNamespace(task="billing.sync", id="task-1", retries=2), reason="timeout")

    assert client.capture_transaction.called
    assert client.capture_exception.called
    assert client.capture_message.called
    assert client.check_in.called
    _, kwargs = client.capture_transaction.call_args
    assert kwargs["context"]["task_module"] == "types"
    assert kwargs["context"]["queue"] == "billing"
