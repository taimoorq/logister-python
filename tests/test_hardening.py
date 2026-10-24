"""SDK safety contract: captures never raise into the host, and default redaction."""
import asyncio
import json
import logging
import os
import threading
import time
import uuid
import warnings
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import Mock, patch

import httpx
import pytest

from logister import LogisterClient, LogisterMiddleware, RetryPolicy, instrument_celery, instrument_fastapi, instrument_flask
from logister.delivery import BackgroundDelivery
from logister.logging import LogisterLoggingHandler
from logister.redaction import REDACTED, SENSITIVE_KEYS, redact, redact_query_string, redact_url, scrub_string

FAST_RETRIES = RetryPolicy(maximum_attempts=2, base_delay=0, maximum_delay=0, total_timeout=5)


def failing_handler(mode):
    def handle(request):
        if mode == "network":
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(mode, json={"error": "nope"})
    return handle


def make_client(handler, **options):
    events = []

    def receive(request):
        if request.url.path == "/api/v1/ingest_events":
            events.append(json.loads(request.content)["event"])
        return handler(request)

    client = LogisterClient(api_key="lgst_test", base_url="https://logister.example", retry_policy=FAST_RETRIES, **options)
    client._http_client = httpx.Client(base_url="https://logister.example", transport=httpx.MockTransport(receive))
    return client, events


def accepting(request):
    return httpx.Response(202, json={"status": "accepted"})


FAILURES = [401, 500, "network"]


# --- PY-1: web integrations never raise SDK failures -------------------------

@pytest.mark.parametrize("mode", FAILURES)
def test_fastapi_delivery_failures_never_raise(mode):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    client, events = make_client(failing_handler(mode))
    app = FastAPI()
    instrument_fastapi(app, client, capture_spans=True)

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/boom")
    def boom():
        raise RuntimeError("application failure")

    with TestClient(app) as http:
        assert http.get("/health").json() == {"ok": True}
        with pytest.raises(RuntimeError, match="application failure"):
            http.get("/boom")
    assert client.flush(5)
    assert {event["event_type"] for event in events} == {"transaction", "span", "error"}


@pytest.mark.parametrize("mode", FAILURES)
def test_flask_delivery_failures_never_raise(mode):
    from flask import Flask

    client, events = make_client(failing_handler(mode))
    app = Flask(__name__)
    app.testing = True
    instrument_flask(app, client, capture_spans=True)
    app.add_url_rule("/health", view_func=lambda: {"ok": True})

    def boom():
        raise RuntimeError("application failure")

    app.add_url_rule("/boom", view_func=boom)
    http = app.test_client()
    assert http.get("/health").json == {"ok": True}
    with pytest.raises(RuntimeError, match="application failure"):
        http.get("/boom")
    assert "error" in {event["event_type"] for event in events}


def django_settings():
    from django.conf import settings
    if not settings.configured:
        settings.configure(SECRET_KEY="test-only", ALLOWED_HOSTS=["testserver"])


@pytest.mark.parametrize("mode", FAILURES)
def test_django_delivery_failures_never_raise(mode):
    django_settings()
    from django.http import HttpResponse
    from django.test import RequestFactory

    client, events = make_client(failing_handler(mode))
    ok = LogisterMiddleware(lambda request: HttpResponse("ok"), client=client, capture_spans=True)
    assert ok(RequestFactory().get("/health")).status_code == 200

    def boom(request):
        raise RuntimeError("application failure")

    failing = LogisterMiddleware(boom, client=client)
    with pytest.raises(RuntimeError, match="application failure"):
        failing(RequestFactory().get("/boom"))
    assert failing.process_exception(RequestFactory().get("/boom"), RuntimeError("view")) is None
    assert {"transaction", "span", "error"} <= {event["event_type"] for event in events}


@pytest.mark.parametrize("mode", FAILURES)
def test_django_async_delivery_failures_never_raise(mode):
    django_settings()
    from django.http import HttpResponse
    from django.test import RequestFactory

    client, _ = make_client(failing_handler(mode))

    async def view(request):
        return HttpResponse("ok")

    async def boom(request):
        raise RuntimeError("application failure")

    async def run():
        assert (await LogisterMiddleware(view, client=client)(RequestFactory().get("/a"))).status_code == 200
        with pytest.raises(RuntimeError, match="application failure"):
            await LogisterMiddleware(boom, client=client)(RequestFactory().get("/b"))

    asyncio.run(run())
    assert client.flush(5)


def test_capture_bugs_and_namer_errors_do_not_replace_app_exception():
    client = Mock(spec=LogisterClient)
    client.capture_transaction.side_effect = TypeError("serialization bug")
    client.capture_exception.side_effect = ValueError("capture bug")

    def namer(request):
        raise KeyError("namer bug")

    django_settings()
    from django.test import RequestFactory

    def boom(request):
        raise RuntimeError("original")

    with pytest.raises(RuntimeError, match="original"):
        LogisterMiddleware(boom, client=client, transaction_namer=namer)(RequestFactory().get("/x"))

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    instrument_fastapi(app, client, transaction_namer=namer)

    @app.get("/x")
    def fail():
        raise RuntimeError("original")

    with TestClient(app) as http, pytest.raises(RuntimeError, match="original"):
        http.get("/x")
    client.capture_exception.assert_called()


@pytest.mark.parametrize("mode", FAILURES)
def test_celery_and_logging_failures_never_raise(mode, capsys):
    from types import SimpleNamespace

    client, _ = make_client(failing_handler(mode))

    class Signal:
        def __init__(self):
            self.receivers = []

        def connect(self, receiver, weak=False):
            self.receivers.append(receiver)

    signals = SimpleNamespace(task_prerun=Signal(), task_postrun=Signal(), task_failure=Signal(), task_retry=Signal())

    def bad_slug(task):
        raise RuntimeError("slug bug")

    instrument_celery(SimpleNamespace(), client, signals_module=signals, monitor_slug_factory=bad_slug)
    task = SimpleNamespace(name="jobs.run", request=SimpleNamespace())
    signals.task_prerun.receivers[0](sender=task, task_id="t1", task=task)
    signals.task_postrun.receivers[0](sender=task, task_id="t1", task=task, state="SUCCESS", signal=object())
    signals.task_failure.receivers[0](sender=task, task_id="t1", exception=RuntimeError("x"), args=[], kwargs={})
    signals.task_retry.receivers[0](request=SimpleNamespace(task="jobs.run", id="t1"), reason="later")

    logger = logging.getLogger("app.hardening.failures")
    logger.handlers = [LogisterLoggingHandler(client)]
    logger.propagate = False
    logger.error("delivery will fail")
    assert "Traceback" not in capsys.readouterr().err


# --- PY-4: non-JSON values in captured context -------------------------------

def test_uuid_route_params_and_other_objects_serialize():
    from flask import Flask
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    client, events = make_client(accepting)
    order = uuid.uuid4()

    flask_app = Flask(__name__)
    instrument_flask(flask_app, client)
    flask_app.add_url_rule("/orders/<uuid:order_id>", view_func=lambda order_id: "ok")
    assert flask_app.test_client().get(f"/orders/{order}").status_code == 200

    fastapi_app = FastAPI()
    instrument_fastapi(fastapi_app, client)

    @fastapi_app.get("/orders/{order_id}")
    def show(order_id: uuid.UUID):
        return {"id": str(order_id)}

    with TestClient(fastapi_app) as http:
        assert http.get(f"/orders/{order}").status_code == 200
    assert client.flush(5)
    assert events[0]["context"]["request"]["view_args"]["order_id"] == str(order)
    assert len(events) == 2

    class Custom:
        def __str__(self):
            return "custom-value"

    cyclic = {"name": "loop"}
    cyclic["self_ref"] = cyclic
    client.capture_message("odd values", context={
        "when": datetime(2026, 10, 23, tzinfo=timezone.utc), "amount": Decimal("1.50"),
        "obj": Custom(), "ids": {1}, "cyclic": cyclic, (1, 2): "tuple key", "nan": float("nan"),
    })
    context = events[-1]["context"]
    assert context["when"] == "2026-10-23T00:00:00+00:00"
    assert context["amount"] == "1.50"
    assert context["obj"] == "custom-value"
    assert context["ids"] == [1]
    assert context["cyclic"]["self_ref"] == "[CIRCULAR]"
    assert context["(1, 2)"] == "tuple key"
    assert context["nan"] == "nan"


# --- PY-3: logging recursion -------------------------------------------------

def test_root_info_logger_posts_exactly_once_per_line():
    client, events = make_client(accepting)
    root = logging.getLogger()
    previous_level, previous_handlers = root.level, root.handlers[:]
    root.setLevel(logging.INFO)
    from logister import instrument_logging
    handler = instrument_logging(client)
    try:
        logging.getLogger("app.checkout").info("first line")
        logging.getLogger("app.checkout").info("second line")
        logging.getLogger("httpx").info("HTTP Request: POST ...")
        logging.getLogger("logister").warning("sdk internal")

        async def in_loop():
            logging.getLogger("app.async").info("from the loop")

        asyncio.run(in_loop())
        assert client.flush(5)
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)
        root.handlers[:] = previous_handlers
    assert [event["message"] for event in events] == ["first line", "second line", "from the loop"]


# --- PY-2: FastAPI does not block the event loop -----------------------------

def test_fastapi_slow_logister_does_not_block_concurrent_requests():
    from fastapi import FastAPI

    delay = 0.5

    def slow(request):
        time.sleep(delay)
        return httpx.Response(202, json={"status": "accepted"})

    client, events = make_client(slow)
    app = FastAPI()
    instrument_fastapi(app, client, capture_spans=True)

    @app.get("/work/{n}")
    async def work(n: int):
        return {"n": n}

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            started = time.perf_counter()
            responses = await asyncio.gather(*(http.get(f"/work/{n}") for n in range(5)))
            return time.perf_counter() - started, responses

    elapsed, responses = asyncio.run(run())
    assert [response.json()["n"] for response in responses] == list(range(5))
    # Ten slow deliveries would take 5s inline; requests finish before even one completes.
    assert elapsed < delay
    assert client.flush(10)
    assert len(events) == 10


def test_background_queue_is_bounded_and_drops_when_full():
    gate, started = threading.Event(), threading.Event()
    worker = BackgroundDelivery(maxsize=1)
    assert worker.submit(lambda: (started.set(), gate.wait()))
    assert started.wait(5)
    assert worker.submit(lambda: None)
    assert worker.submit(lambda: None) is False
    gate.set()
    assert worker.flush(5)


# --- PY-5 / PY-6: shared default redaction -----------------------------------

@pytest.mark.parametrize("key", sorted(SENSITIVE_KEYS))
def test_every_exact_sensitive_key_is_redacted(key):
    assert redact({key: "value", key.upper(): "value"}) == {key: REDACTED, key.upper(): REDACTED}


@pytest.mark.parametrize("key", [
    "access_token", "accessToken", "X-API-Key", "api-key", "Proxy-Authorization", "set_cookie",
    "client.secret", "CREDIT_CARD", "card-number", "private_key", "user_password_hash",
    "stripeSecretKey", "my_api_key_v2", "github_credentials", "csrf_cookie_name", "x-xsrf-token",
    "passwd_old", "id_token", "pwd", "SSN", "cvv", "signature", "session-token", "session_key",
])
def test_normalized_and_contains_keys_are_redacted(key):
    assert redact({key: "value"}) == {key: REDACTED}


@pytest.mark.parametrize("key", ["user_id", "session_id", "trace_id", "request_id", "path", "author", "status"])
def test_ordinary_keys_are_kept(key):
    assert redact({key: "value"}) == {key: "value"}


@pytest.mark.parametrize("raw, expected", [
    ("Authorization: Bearer abc.def-123", f"Authorization: Bearer {REDACTED}"),
    ("bearer abc123", f"bearer {REDACTED}"),
    ("Basic dXNlcjpwYXNz", f"Basic {REDACTED}"),
    ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl here", f"jwt {REDACTED} here"),
    ("key=lgst_abc123_DEF", f"key={REDACTED}"),
    ("see https://x.example/cb?code=1&token=abc&page=2", f"see https://x.example/cb?code=1&token={REDACTED}&page=2"),
    ("nothing sensitive", "nothing sensitive"),
])
def test_string_value_rules(raw, expected):
    assert scrub_string(raw) == expected


def test_url_and_query_rules():
    assert redact_url("https://u:p@x.example/a/b?api_key=1&q=shoes&Access-Token=2#access_token=3") == (
        f"https://x.example/a/b?api_key={REDACTED}&q=shoes&Access-Token={REDACTED}#access_token={REDACTED}"
    )
    assert redact_query_string("password=hunter2&user=amy&flag") == f"password={REDACTED}&user=amy&flag"
    assert redact_query_string("reset=1", extra_keys=["reset"]) == f"reset={REDACTED}"
    assert redact({"url": "/login?secret=1", "query_string": "token=1&a=2", "referer": "https://r.example/?sessionToken=x"}) == {
        "url": f"/login?secret={REDACTED}", "query_string": f"token={REDACTED}&a=2",
        "referer": f"https://r.example/?sessionToken={REDACTED}",
    }


def test_integration_context_is_redacted_and_ip_is_opt_in():
    from flask import Flask

    client, events = make_client(accepting, redact_keys=["order_ref"])
    app = Flask(__name__)
    instrument_flask(app, client)

    app.add_url_rule("/cb", view_func=lambda: "ok")
    app.test_client().get(
        "/cb?code=ok&access_token=abc&order_ref=9",
        headers={"Referer": "https://ref.example/p?password=pw", "X-Forwarded-For": "203.0.113.9", "User-Agent": "Bearer tkn"},
    )
    context = events[-1]["context"]
    assert context["url"] == f"http://localhost/cb?code=ok&access_token={REDACTED}&order_ref={REDACTED}"
    assert context["query_string"] == f"code=ok&access_token={REDACTED}&order_ref={REDACTED}"
    assert context["request"]["headers"]["Referer"] == f"https://ref.example/p?password={REDACTED}"
    assert context["request"]["headers"]["User-Agent"] == f"Bearer {REDACTED}"
    assert "X-Forwarded-For" not in context["request"]["headers"]
    assert "client_ip" not in context
    # SDK envelope fields are never redacted.
    assert uuid.UUID(events[-1]["uuid"]) and context["trace_id"] and context["request_id"]

    opted_in, opted_events = make_client(accepting, capture_client_ip=True)
    other = Flask("opted")
    instrument_flask(other, opted_in)
    other.add_url_rule("/", view_func=lambda: "ok")
    other.test_client().get("/", headers={"X-Forwarded-For": "203.0.113.9"}, environ_base={"REMOTE_ADDR": "198.51.100.4"})
    assert opted_events[-1]["context"]["client_ip"] == "198.51.100.4"
    assert opted_events[-1]["context"]["request"]["headers"]["X-Forwarded-For"] == "203.0.113.9"


def test_log_extra_and_exception_data_are_redacted():
    client, events = make_client(accepting)
    logger = logging.getLogger("app.hardening.redaction")
    logger.handlers = [LogisterLoggingHandler(client)]
    logger.propagate = False
    logger.warning("login with Bearer abc", extra={"password": "pw", "payload": {"apiKey": "k", "ok": 1}})
    context = events[-1]["context"]["log_record"]
    assert events[-1]["message"] == f"login with Bearer {REDACTED}"
    assert context["password"] == REDACTED
    assert context["payload"] == {"apiKey": REDACTED, "ok": 1}

    try:
        raise ValueError("token lgst_leaked in message")
    except ValueError as error:
        client.capture_exception(error, context={"tags": {"auth_token": "t"}, "user": {"email": "a@example.com"}})
    event = events[-1]
    assert event["message"] == f"token {REDACTED} in message"
    assert event["context"]["exception"]["message"] == f"token {REDACTED} in message"
    assert event["context"]["tags"]["auth_token"] == REDACTED


class Service:
    def run(self, request):
        password = "hunter2"
        header = "Bearer abc"
        order_id = 42
        raise RuntimeError(f"failed {order_id} {len(password)} {len(header)}")

    @classmethod
    def build(cls):
        cls_token = "x"
        raise RuntimeError(cls_token)


def test_capture_locals_are_redacted_and_skip_self_cls_request():
    client, events = make_client(accepting, capture_locals=True)
    try:
        Service().run(object())
    except RuntimeError as error:
        client.capture_exception(error)
    frame_locals = events[-1]["context"]["exception"]["frames"][-1]["locals"]
    assert frame_locals == {"password": REDACTED, "header": f"'Bearer {REDACTED}'", "order_id": "42"}

    try:
        Service.build()
    except RuntimeError as error:
        client.capture_exception(error)
    assert events[-1]["context"]["exception"]["frames"][-1]["locals"] == {"cls_token": REDACTED}


# --- PY-7 / PY-8 / PY-9 ------------------------------------------------------

def test_host_metadata_is_opt_in(monkeypatch):
    client, events = make_client(accepting)
    client.capture_message("hi")
    assert "hostname" not in events[-1]["context"] and "runtime_name" not in events[-1]["context"]
    assert events[-1]["context"]["process_id"] == os.getpid()

    with_host, host_events = make_client(accepting, capture_hostname=True)
    with_host.capture_message("hi")
    assert host_events[-1]["context"]["hostname"]

    monkeypatch.setenv("LOGISTER_API_KEY", "k")
    monkeypatch.setenv("LOGISTER_CAPTURE_HOSTNAME", "true")
    monkeypatch.setenv("LOGISTER_CAPTURE_CLIENT_IP", "1")
    monkeypatch.setenv("LOGISTER_REDACT_KEYS", "order_ref, member-number")
    configured = LogisterClient.from_env()
    assert configured.capture_hostname and configured.capture_client_ip
    assert configured.redact_keys == ("order_ref", "member-number")


def test_http_client_is_recreated_after_fork():
    client = LogisterClient(api_key="k", base_url="https://logister.example")
    parent = client._http()
    assert client._http() is parent
    parent.close = Mock(side_effect=AssertionError("must not close the parent's pool"))
    client._http_client_pid = os.getpid() + 1  # as seen from a forked child
    child = client._http()
    assert child is not parent and client._http_client_pid == os.getpid()
    client._http_client_pid = os.getpid() + 1
    client.close()
    parent.close.assert_not_called()

    worker = BackgroundDelivery()
    done = threading.Event()
    worker.submit(lambda: None)
    worker._pid = os.getpid() + 1
    worker.submit(done.set)
    assert done.wait(5)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_real_fork_child_gets_fresh_http_client_and_worker():
    client, _ = make_client(accepting)
    client._http_client_pid = os.getpid()
    parent_http = client._http()
    client._background.submit(lambda: None)
    assert client.flush(5)
    client._background._lock.acquire()  # simulate another thread holding it at fork time
    read_end, write_end = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child process
        ok = False
        try:
            done = threading.Event()
            # A real httpx.Client here trips macOS's Objective-C fork-safety check on 3.14.
            with patch("logister.client.httpx.Client", lambda **_: object()):
                fresh_http = client._http() is not parent_http
            ok = fresh_http and client._background.submit(done.set) and done.wait(5)
        finally:
            os.write(write_end, b"1" if ok else b"0")
            os._exit(0)
    client._background._lock.release()
    os.close(write_end)
    deadline = time.monotonic() + 10
    while os.waitpid(pid, os.WNOHANG) == (0, 0):
        if time.monotonic() > deadline:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            break
        time.sleep(0.01)
    assert os.read(read_end, 1) == b"1", "forked child deadlocked or reused the parent's client"
    os.close(read_end)


@pytest.mark.parametrize("url, warns", [
    ("http://logister.example", True),
    ("http://10.0.0.5:3000", True),
    ("http://localhost:3000", False),
    ("http://api.localhost", False),
    ("http://127.0.0.1:3000", False),
    ("http://[::1]:3000", False),
    ("https://logister.example", False),
])
def test_plain_http_base_url_warns_unless_loopback(url, warns):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        LogisterClient(api_key="k", base_url=url)
    assert bool([w for w in caught if "plain http://" in str(w.message)]) is warns


def test_celery_worker_hostname_follows_capture_hostname():
    from types import SimpleNamespace

    class Signal:
        def __init__(self):
            self.receivers = []

        def connect(self, receiver, weak=False):
            self.receivers.append(receiver)

    for enabled in (False, True):
        client, events = make_client(accepting, capture_hostname=enabled)
        signals = SimpleNamespace(task_prerun=Signal(), task_postrun=Signal(), task_failure=Signal(), task_retry=Signal())
        instrument_celery(SimpleNamespace(), client, signals_module=signals)
        task = SimpleNamespace(name="jobs.run", request=SimpleNamespace(hostname="celery@worker-1"))
        signals.task_postrun.receivers[0](sender=task, task_id="t1", task=task, state="SUCCESS")
        assert (events[-1]["context"].get("hostname") == "celery@worker-1") is enabled
