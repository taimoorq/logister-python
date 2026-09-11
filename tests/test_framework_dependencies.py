"""Exercise the installed framework extras, beyond the isolated adapter fakes."""
from unittest.mock import Mock

from logister import LogisterClient, LogisterMiddleware, instrument_fastapi, instrument_flask, instrument_celery


def test_installed_fastapi_request():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    client = Mock(spec=LogisterClient)
    app = FastAPI()
    instrument_fastapi(app, client, capture_spans=True)

    @app.get("/health")
    def health():
        return {"ok": True}

    with TestClient(app) as http:
        assert http.get("/health").json() == {"ok": True}
    client.capture_transaction.assert_called_once()
    client.capture_span.assert_called_once()


def test_installed_flask_request():
    from flask import Flask
    client = Mock(spec=LogisterClient)
    app = Flask(__name__)
    instrument_flask(app, client, capture_spans=True)
    app.add_url_rule("/health", view_func=lambda: {"ok": True})
    assert app.test_client().get("/health").json == {"ok": True}
    client.capture_transaction.assert_called_once()
    client.capture_span.assert_called_once()


def test_installed_django_request():
    from django.conf import settings
    from django.http import HttpResponse
    from django.test import RequestFactory
    if not settings.configured:
        settings.configure(SECRET_KEY="test-only", ALLOWED_HOSTS=["testserver"])
    client = Mock(spec=LogisterClient)
    middleware = LogisterMiddleware(lambda request: HttpResponse("ok"), client=client, capture_spans=True)
    assert middleware(RequestFactory().get("/health")).status_code == 200
    client.capture_transaction.assert_called_once()
    client.capture_span.assert_called_once()


def test_installed_celery_task_signals():
    from celery import Celery, signals
    client = Mock(spec=LogisterClient)
    app = Celery("logister-dependency-test", broker="memory://")
    watched = [signals.task_prerun, signals.task_postrun, signals.task_failure, signals.task_retry]
    original = [signal.receivers[:] for signal in watched]
    try:
        instrument_celery(app, client)

        @app.task(name="logister.dependency.smoke")
        def smoke():
            return "ok"

        assert smoke.apply().get() == "ok"
        client.capture_transaction.assert_called_once()
    finally:
        for signal, receivers in zip(watched, original):
            signal.receivers[:] = receivers
            signal.sender_receivers_cache.clear()
        app.close()
