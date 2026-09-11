import gzip
import json
from unittest.mock import patch

import httpx
import pytest

from logister import LogisterClient, LogisterError, PreparedEvent, RetryPolicy


def client_for(handler, **options):
    client = LogisterClient(api_key="test-token", retry_policy=RetryPolicy(base_delay=0, maximum_delay=0), **options)
    client._http_client = httpx.Client(base_url="https://logister.example", transport=httpx.MockTransport(handler))
    return client


def test_retry_and_manual_replay_preserve_exact_capture_snapshot():
    bodies = []
    context = {"nested": {"value": "original"}}

    def handler(request):
        bodies.append(request.content)
        context["nested"]["value"] = "mutated"
        return httpx.Response(503 if len(bodies) == 1 else 202, json={"status": "accepted"})

    client = client_for(handler)
    prepared = client.prepare_event(event_type="log", level="info", message="hello", context=context)
    client.send_prepared_event(prepared)
    client.send_prepared_event(prepared)
    assert len(bodies) == 3 and len(set(bodies)) == 1
    event = json.loads(bodies[0])["event"]
    assert event["uuid"] == prepared.event_id
    assert event["occurred_at"]
    assert event["context"]["nested"]["value"] == "original"


def test_network_timeout_retries_same_bytes_with_bounded_attempts():
    bodies = []

    def handler(request):
        bodies.append(request.content)
        raise httpx.ReadTimeout("response lost", request=request)

    client = client_for(handler)
    with pytest.raises(LogisterError):
        client.capture_message("hello")
    assert len(bodies) == 3 and len(set(bodies)) == 1


@pytest.mark.parametrize("status", [400, 401, 403, 422])
def test_permanent_rejections_are_not_retried(status):
    calls = []
    client = client_for(lambda request: (calls.append(request) or httpx.Response(status)))
    with pytest.raises(LogisterError):
        client.capture_message("hello")
    assert len(calls) == 1


def test_unsupported_batch_attempts_every_individual_after_failure():
    calls = []

    def handler(request):
        calls.append(request)
        status = 501 if request.url.path.endswith("/batch") else 422 if len(calls) == 2 else 202
        return httpx.Response(status, json={"status": "accepted"})

    client = client_for(handler)
    prepared = [client.prepare_event(event_type="log", level="info", message=str(i)) for i in range(3)]
    results = client.send_events(prepared)
    assert len(calls) == 4
    assert [result.event_id for result in results] == [event.event_id for event in prepared]
    assert results[0].error and all(result.response for result in results[1:])
    assert [request.content for request in calls[1:]] == [event.body for event in prepared]


def test_oversized_batch_splits_without_changing_events():
    accepted, batch_ids = [], []

    def handler(request):
        raw = gzip.decompress(request.content)
        lines = raw.splitlines()
        batch_ids.append(request.headers["X-Logister-Batch-Id"])
        if len(lines) > 1:
            return httpx.Response(413)
        accepted.extend(lines)
        return httpx.Response(202, json={"status": "accepted"})

    client = client_for(handler)
    events = [client.prepare_event(event_type="log", level="info", message=str(i)) for i in range(3)]
    assert all(result.response for result in client.send_events(events))
    assert accepted == [event.body for event in events]
    assert len(set(batch_ids)) == len(batch_ids)


def test_batch_failure_does_not_skip_next_chunk():
    counts = []

    def handler(request):
        counts.append(len(gzip.decompress(request.content).splitlines()))
        return httpx.Response(422 if len(counts) == 1 else 202, json={"status": "accepted"})

    client = client_for(handler)
    event = client.prepare_event(event_type="log", level="info", message="event")
    results = client.send_events([event] * 101)
    assert counts == [100, 1]
    assert len(results) == 101 and results[0].error and results[-1].response
    with pytest.raises(ValueError, match="1,000"):
        client.send_events([event] * 1001)
    assert counts == [100, 1]


def test_retry_after_and_deadline_are_bounded():
    policy = RetryPolicy(maximum_delay=2)
    assert policy.delay(0, httpx.Response(429, headers={"Retry-After": "900"})) == 2
    assert policy.delay(0, httpx.Response(429, headers={"Retry-After": "invalid"})) == 0.25
    calls = []
    client = client_for(lambda request: (calls.append(request) or httpx.Response(429)))
    with patch("logister.delivery.time.monotonic", side_effect=[0, 0, 16]):
        with pytest.raises(LogisterError, match="deadline"):
            client.capture_message("hello")
    assert len(calls) == 1


def test_deployment_posts_and_malformed_success_are_not_retried():
    calls = []
    client = client_for(lambda request: (calls.append(request) or httpx.Response(503)))
    with pytest.raises(LogisterError):
        client.record_deployment(release="test")
    assert len(calls) == 1
    calls.clear()
    client = client_for(lambda request: (calls.append(request) or httpx.Response(202, text="bad-json")))
    with pytest.raises(LogisterError, match="succeeded"):
        client.capture_message("hello")
    assert len(calls) == 1


def test_prepared_identity_cannot_disagree_with_body():
    with pytest.raises(ValueError):
        PreparedEvent("00000000-0000-4000-8000-000000000001", b'{"event":{"uuid":"another"}}')
