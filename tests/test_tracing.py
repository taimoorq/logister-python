import asyncio
import json
from types import SimpleNamespace

import httpx

from logister import LogisterClient, TraceContext, current_trace_context, trace_scope
from logister.fastapi import instrument_fastapi
from logister.flask import instrument_flask
from logister.django import LogisterMiddleware


HEADER = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00"


def test_strict_headers_and_task_isolation():
    parent = TraceContext.from_headers(HEADER, "request-1")
    assert parent.parent_span_id == "00f067aa0ba902b7"
    assert parent.flags == "00"
    assert TraceContext.from_headers("00-" + "0" * 32 + "-00f067aa0ba902b7-01").trace_id != "0" * 32
    assert parent.headers_for("https://api.example:443/path", allowed_origins=["https://api.example"])["traceparent"] == parent.traceparent
    for destination in ("http://api.example", "https://api.example.evil", "https://api.example:444", "https://user@api.example"):
        assert parent.headers_for(destination, allowed_origins=["https://api.example"]) == {}

    async def worker():
        trace = TraceContext()
        with trace_scope(trace):
            await asyncio.sleep(0)
            assert current_trace_context() is trace
        assert current_trace_context() is None

    async def run():
        await asyncio.gather(*(worker() for _ in range(20)))
    asyncio.run(run())


def capturing_client():
    events = []
    def receive(request):
        events.append(json.loads(request.content)["event"])
        return httpx.Response(202, json={"status": "accepted"})
    client = LogisterClient(api_key="test", base_url="https://logister.example")
    client._http_client = httpx.Client(base_url="https://logister.example", transport=httpx.MockTransport(receive))
    return client, events


def assert_one_request(events):
    assert {event["context"]["trace_id"] for event in events} == {"4bf92f3577b34da6a3ce929d0e0e4736"}
    assert len({event["context"]["span_id"] for event in events}) == 1
    assert {event["context"]["parent_span_id"] for event in events} == {"00f067aa0ba902b7"}
    assert current_trace_context() is None


def test_actual_fastapi_request_uses_same_context_for_manual_error_and_span():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    client, events = capturing_client()
    app = FastAPI()
    instrument_fastapi(app, client, capture_spans=True)
    @app.get("/work")
    async def work():
        await asyncio.sleep(0)
        client.capture_exception(RuntimeError("handled"))
        return {"ok": True}
    assert TestClient(app).get("/work", headers={"traceparent": HEADER}).status_code == 200
    assert len(events) == 3
    assert_one_request(events)


def test_actual_flask_request_restores_scope():
    from flask import Flask
    client, events = capturing_client()
    app = Flask(__name__)
    instrument_flask(app, client, capture_spans=True)
    @app.get("/work")
    def work():
        client.capture_exception(RuntimeError("handled"))
        return "ok"
    assert app.test_client().get("/work", headers={"traceparent": HEADER}).status_code == 200
    assert len(events) == 3
    assert_one_request(events)


def test_django_async_scope_survives_await_and_cleans_up():
    client, events = capturing_client()
    request = SimpleNamespace(method="GET", path="/work", META={"HTTP_TRACEPARENT": HEADER})
    async def application(request):
        await asyncio.sleep(0)
        client.capture_exception(RuntimeError("handled"))
        return SimpleNamespace(status_code=200, headers={})
    asyncio.run(LogisterMiddleware(application, client=client, capture_spans=True)(request))
    assert_one_request(events)
