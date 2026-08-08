import hashlib
import hmac
import json
import logging

import pytest

from app import queue
from app.logging_config import (
    ConsoleFormatter,
    JsonFormatter,
    get_correlation_id,
    set_correlation_id,
)

PAYLOAD = {
    "entry": [
        {
            "changes": [
                {
                    "value": {
                        "metadata": {"display_phone_number": "911234567890"},
                        "messages": [
                            {
                                "id": "wamid.OBS1",
                                "from": "919876543210",
                                "timestamp": "1712345678",
                                "type": "text",
                                "text": {"body": "trace me"},
                            }
                        ],
                    }
                }
            ]
        }
    ]
}


def sign(body: bytes, secret: str = "test-app-secret") -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def post_signed(client, payload=PAYLOAD, **kwargs):
    body = json.dumps(payload).encode()
    headers = {"X-Hub-Signature-256": sign(body)}
    headers.update(kwargs.pop("headers", {}))
    return client.post("/webhook", content=body, headers=headers, **kwargs)


# --- JSON log formatting ----------------------------------------------------


def make_record(msg="something_happened", **extra):
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_emits_parseable_json():
    parsed = json.loads(JsonFormatter().format(make_record()))
    assert parsed["event"] == "something_happened"
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "app.test"
    assert "ts" in parsed


def test_json_formatter_includes_event_context():
    """message_id and event_type must appear on every event-related log line."""
    record = make_record("event_queued", message_id="wamid.LOG1", event_type="message")
    parsed = json.loads(JsonFormatter().format(record))
    assert parsed["message_id"] == "wamid.LOG1"
    assert parsed["event_type"] == "message"


def test_json_formatter_includes_correlation_id():
    set_correlation_id("abc123")
    try:
        parsed = json.loads(JsonFormatter().format(make_record()))
        assert parsed["correlation_id"] == "abc123"
    finally:
        set_correlation_id(None)


def test_json_formatter_serialises_exceptions():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = make_record("it_broke")
        record.exc_info = sys.exc_info()
        parsed = json.loads(JsonFormatter().format(record))

    assert "ValueError: boom" in parsed["exception"]


def test_json_formatter_handles_unserialisable_values():
    """A stray object in extra must not blow up the logger."""
    parsed = json.loads(JsonFormatter().format(make_record(obj=object())))
    assert "obj" in parsed


def test_console_formatter_is_human_readable():
    line = ConsoleFormatter().format(make_record("event_queued", message_id="wamid.X"))
    assert "event_queued" in line
    assert "message_id=wamid.X" in line


# --- correlation id ---------------------------------------------------------


def test_correlation_id_is_generated_when_absent(client):
    resp = post_signed(client)
    assert resp.headers["X-Correlation-Id"]


def test_supplied_correlation_id_is_honoured(client):
    resp = post_signed(client, headers={"X-Correlation-Id": "trace-me-123"})
    assert resp.headers["X-Correlation-Id"] == "trace-me-123"


async def test_correlation_id_is_carried_onto_the_queued_event(client, fake_redis):
    post_signed(client, headers={"X-Correlation-Id": "trace-me-456"})

    entries = await queue.read_new("test-consumer")
    assert entries[0][1]["correlation_id"] == "trace-me-456"


def test_set_correlation_id_generates_when_given_none():
    generated = set_correlation_id(None)
    assert generated and get_correlation_id() == generated
    set_correlation_id(None)


# --- /health ----------------------------------------------------------------


def test_health_reports_ok_when_redis_is_reachable(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "redis": "ok"}


def test_health_returns_503_when_redis_is_down(client, fake_redis, monkeypatch):
    """A static 200 would keep a broken instance in the load balancer rotation."""

    async def dead_ping():
        raise ConnectionError("connection refused")

    monkeypatch.setattr(fake_redis, "ping", dead_ping)

    resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["redis"] == "unreachable"


# --- /metrics ---------------------------------------------------------------


def test_metrics_endpoint_is_prometheus_formatted(client):
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert "# HELP wa_events_received_total" in resp.text
    assert "# TYPE wa_queue_depth gauge" in resp.text


def test_metrics_count_queued_events(client):
    before = _sample(client, "wa_events_queued_total", 'event_type="message"')
    post_signed(client)
    after = _sample(client, "wa_events_queued_total", 'event_type="message"')
    assert after == before + 1


def test_metrics_count_duplicates(client):
    post_signed(client)
    before = _sample(client, "wa_events_duplicate_total", 'event_type="message"')
    post_signed(client)  # same message_id
    after = _sample(client, "wa_events_duplicate_total", 'event_type="message"')
    assert after == before + 1


def test_metrics_count_rejected_signatures(client):
    before = _sample(client, "wa_webhook_requests_total", 'outcome="bad_signature"')
    client.post("/webhook", content=b"{}", headers={"X-Hub-Signature-256": "sha256=bad"})
    after = _sample(client, "wa_webhook_requests_total", 'outcome="bad_signature"')
    assert after == before + 1


def test_metrics_expose_queue_depth(client, fake_redis):
    post_signed(client)
    assert _sample(client, "wa_queue_depth") == 1.0


def _sample(client, metric: str, labels: str = "") -> float:
    """Pull one sample value out of the Prometheus exposition text."""
    needle = f"{metric}{{{labels}}}" if labels else metric
    for line in client.get("/metrics").text.splitlines():
        if line.startswith("#"):
            continue
        name, _, value = line.rpartition(" ")
        if name.strip() == needle:
            return float(value)
    return 0.0


# --- /stats -----------------------------------------------------------------


async def test_stats_reports_stream_state(client, fake_redis):
    post_signed(client)
    body = client.get("/stats").json()
    assert body["queued"] == 1
    assert body["in_flight"] == 0
    assert body["dead_lettered"] == 0
    assert "dedup_hit_rate" in body


@pytest.mark.parametrize("field", ["queued", "in_flight", "dead_lettered"])
def test_stats_has_all_queue_fields(client, field):
    assert field in client.get("/stats").json()
