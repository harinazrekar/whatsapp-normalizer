"""
Route-level tests for behaviour that isn't security, queueing, or observability
specific: rate limiting, dedup across requests, batching, and the client factory.
"""

import hashlib
import hmac
import json

import pytest

from app import queue, redis_client
from app.config import settings


def sign(body: bytes, secret: str = "test-app-secret") -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def post_signed(client, payload):
    body = json.dumps(payload).encode()
    return client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign(body)})


def message_payload(*message_ids, phone="911234567890"):
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"display_phone_number": phone},
                            "messages": [
                                {
                                    "id": mid,
                                    "from": "919876543210",
                                    "timestamp": "1712345678",
                                    "type": "text",
                                    "text": {"body": f"body of {mid}"},
                                }
                                for mid in message_ids
                            ],
                        }
                    }
                ]
            }
        ]
    }


# --- dedup across requests --------------------------------------------------


def test_redelivery_of_the_same_message_is_dropped(client):
    """WhatsApp redelivers constantly; the second one must not be queued."""
    first = post_signed(client, message_payload("wamid.DUP")).json()
    second = post_signed(client, message_payload("wamid.DUP")).json()

    assert first == {"received": 1, "queued": 1, "duplicates": 0}
    assert second == {"received": 1, "queued": 0, "duplicates": 1}


async def test_duplicate_is_not_written_to_the_stream(client, fake_redis):
    post_signed(client, message_payload("wamid.DUP2"))
    post_signed(client, message_payload("wamid.DUP2"))

    assert (await queue.depth())["queued"] == 1


def test_partially_duplicate_batch_queues_only_the_new_events(client):
    post_signed(client, message_payload("wamid.A"))

    result = post_signed(client, message_payload("wamid.A", "wamid.B", "wamid.C")).json()

    assert result == {"received": 3, "queued": 2, "duplicates": 1}


# --- batching ---------------------------------------------------------------


def test_multiple_messages_in_one_delivery_are_all_queued(client):
    result = post_signed(client, message_payload("wamid.M1", "wamid.M2", "wamid.M3")).json()
    assert result == {"received": 3, "queued": 3, "duplicates": 0}


def test_empty_payload_is_accepted_not_rejected(client):
    """
    Meta sends shapes we don't handle. Returning anything but a 2xx makes it
    back off deliveries, so an unrecognised payload must still succeed.
    """
    result = post_signed(client, {"object": "whatsapp_business_account", "entry": []})
    assert result.status_code == 200
    assert result.json() == {"received": 0, "queued": 0, "duplicates": 0}


def test_unknown_payload_shape_does_not_500(client):
    assert post_signed(client, {"something": "unexpected"}).status_code == 200


def test_json_array_body_is_rejected(client):
    body = json.dumps([1, 2, 3]).encode()
    resp = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign(body)})
    assert resp.status_code == 400


# --- rate limiting ----------------------------------------------------------


def test_webhook_is_rate_limited(monkeypatch, fake_redis):
    """
    Built with its own app instance: the limit is read at decoration time, so it
    can't be monkeypatched onto the already-imported app.
    """
    # Patched on the settings object rather than the environment: reloading
    # app.main does not reload app.config, so the singleton would keep the old
    # value and the decorator would read it.
    monkeypatch.setattr(settings, "WEBHOOK_RATE_LIMIT", "3/minute")

    import importlib

    from fastapi.testclient import TestClient

    from app import main

    importlib.reload(main)

    body = b"{}"
    headers = {"X-Hub-Signature-256": sign(body)}
    try:
        with TestClient(main.app) as limited_client:
            codes = [
                limited_client.post("/webhook", content=body, headers=headers).status_code
                for _ in range(5)
            ]
    finally:
        # Restore the module for every other test in the session.
        monkeypatch.undo()
        importlib.reload(main)

    assert codes == [200, 200, 200, 429, 429]


# --- redis client factory ---------------------------------------------------


def test_get_redis_returns_a_shared_instance(fake_redis):
    assert redis_client.get_redis() is fake_redis
    assert redis_client.get_redis() is redis_client.get_redis()


def test_get_redis_builds_a_client_when_none_is_set(monkeypatch):
    monkeypatch.setattr(settings, "REDIS_URL", "redis://localhost:6379/0")
    redis_client.set_redis(None)
    try:
        built = redis_client.get_redis()
        assert built is not None
        assert redis_client.get_redis() is built  # cached, not rebuilt
    finally:
        redis_client.set_redis(None)


async def test_close_redis_clears_the_cached_client(fake_redis):
    await redis_client.close_redis()
    assert redis_client._client is None


# --- docs -------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/openapi.json", "/docs"])
def test_api_documentation_is_served(client, path):
    assert client.get(path).status_code == 200


def test_openapi_lists_every_public_route(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert set(paths) >= {"/webhook", "/health", "/stats", "/metrics"}


# --- Redis unavailable at ingest (pre-1.0 audit regression) -----------------


def test_ingest_returns_503_when_redis_is_unreachable(client, fake_redis, monkeypatch):
    """
    503, not an unhandled 500, and deliberately not a 200. We cannot durably
    accept the event, and a non-2xx is what makes Meta redeliver it -- answering
    200 here would lose it silently.
    """

    async def refuse(*_args, **_kwargs):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(fake_redis, "set", refuse)

    resp = post_signed(client, message_payload("wamid.NOREDIS"))

    assert resp.status_code == 503
    assert "retry" in resp.json()["detail"].lower()


def test_enqueue_failure_also_returns_503(client, fake_redis, monkeypatch):
    async def refuse(*_args, **_kwargs):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(fake_redis, "xadd", refuse)

    assert post_signed(client, message_payload("wamid.NOXADD")).status_code == 503


def test_storage_failure_is_counted(client, fake_redis, monkeypatch):
    async def refuse(*_args, **_kwargs):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(fake_redis, "xadd", refuse)
    post_signed(client, message_payload("wamid.COUNTED"))

    metrics_text = client.get("/metrics").text
    assert 'wa_webhook_requests_total{outcome="storage_error"}' in metrics_text
