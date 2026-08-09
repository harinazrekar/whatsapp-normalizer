import hashlib
import hmac
import json

import pytest

from app.config import Settings, settings
from app.security import compute_signature, verify_signature, verify_token


def sign(body: bytes, secret: str = "test-app-secret") -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


PAYLOAD = {
    "entry": [
        {
            "changes": [
                {
                    "value": {
                        "metadata": {"display_phone_number": "911234567890"},
                        "messages": [
                            {
                                "id": "wamid.SIGNED1",
                                "from": "919876543210",
                                "timestamp": "1712345678",
                                "type": "text",
                                "text": {"body": "Signed hello"},
                            }
                        ],
                    }
                }
            ]
        }
    ]
}


# --- signature helper -------------------------------------------------------


def test_compute_signature_matches_meta_format():
    body = b'{"a":1}'
    assert compute_signature(body, "s") == sign(body, "s")


def test_verify_signature_accepts_valid():
    body = b'{"hello":"world"}'
    assert verify_signature(body, sign(body)) is True


def test_verify_signature_rejects_wrong_secret():
    body = b'{"hello":"world"}'
    assert verify_signature(body, sign(body, "not-the-secret")) is False


def test_verify_signature_rejects_missing_header():
    assert verify_signature(b"{}", None) is False


def test_verify_signature_rejects_unprefixed_header():
    body = b"{}"
    bare = sign(body).removeprefix("sha256=")
    assert verify_signature(body, bare) is False


def test_verify_signature_is_body_sensitive():
    """A signature for one body must not validate a different body."""
    assert verify_signature(b'{"hello":"world "}', sign(b'{"hello":"world"}')) is False


# --- POST /webhook ----------------------------------------------------------


def test_webhook_accepts_correctly_signed_request(client):
    body = json.dumps(PAYLOAD).encode()
    resp = client.post(
        "/webhook",
        content=body,
        headers={"X-Hub-Signature-256": sign(body), "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"received": 1, "queued": 1, "duplicates": 0}


def test_webhook_rejects_missing_signature(client):
    body = json.dumps(PAYLOAD).encode()
    resp = client.post("/webhook", content=body)
    assert resp.status_code == 403


def test_webhook_rejects_invalid_signature(client):
    body = json.dumps(PAYLOAD).encode()
    resp = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": "sha256=deadbeef"})
    assert resp.status_code == 403


def test_webhook_rejects_tampered_body(client):
    """Signature captured from one payload, replayed against a modified one."""
    original = json.dumps(PAYLOAD).encode()
    tampered = json.dumps({**PAYLOAD, "injected": True}).encode()
    resp = client.post(
        "/webhook", content=tampered, headers={"X-Hub-Signature-256": sign(original)}
    )
    assert resp.status_code == 403


def test_webhook_rejects_invalid_json_after_valid_signature(client):
    body = b"this is not json"
    resp = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign(body)})
    assert resp.status_code == 400


# --- GET /webhook handshake -------------------------------------------------


def test_handshake_returns_challenge_for_correct_token(client):
    resp = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "test-verify-token",
            "hub.challenge": "1158201444",
        },
    )
    assert resp.status_code == 200
    assert resp.text == "1158201444"


def test_handshake_rejects_wrong_token(client):
    resp = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong",
            "hub.challenge": "1158201444",
        },
    )
    assert resp.status_code == 403


def test_handshake_rejects_wrong_mode(client):
    resp = client.get(
        "/webhook",
        params={
            "hub.mode": "unsubscribe",
            "hub.verify_token": "test-verify-token",
            "hub.challenge": "1158201444",
        },
    )
    assert resp.status_code == 403


# --- startup validation -----------------------------------------------------


def test_validate_rejects_placeholder_verify_token():
    s = Settings()
    s.WHATSAPP_VERIFY_TOKEN = "change-me"
    s.WHATSAPP_APP_SECRET = "real-secret"
    with pytest.raises(RuntimeError, match="WHATSAPP_VERIFY_TOKEN"):
        s.validate()


def test_validate_rejects_missing_app_secret():
    s = Settings()
    s.WHATSAPP_VERIFY_TOKEN = "real-token"
    s.WHATSAPP_APP_SECRET = ""
    s.REQUIRE_SIGNATURE = True
    with pytest.raises(RuntimeError, match="WHATSAPP_APP_SECRET"):
        s.validate()


def test_validate_allows_missing_app_secret_when_signature_not_required():
    s = Settings()
    s.WHATSAPP_VERIFY_TOKEN = "real-token"
    s.WHATSAPP_APP_SECRET = ""
    s.REQUIRE_SIGNATURE = False
    s.validate()  # must not raise


def test_validate_passes_on_real_values():
    settings.validate()  # the test env from conftest


# --- non-ASCII input (pre-1.0 audit regression) -----------------------------
#
# hmac.compare_digest raises TypeError on str with non-ASCII characters. uvicorn
# decodes header values as latin-1 and obs-text (0x80-0xFF) is legal in HTTP, so
# an anonymous caller could turn any request into an unhandled 500 -- and
# sustained 5xx is what makes Meta back off and disable the webhook.


@pytest.mark.parametrize(
    "header",
    [
        pytest.param("sha256=\xe9", id="latin1-in-digest"),
        pytest.param("sha256=ÿþ", id="high-bytes"),
        pytest.param("\xe9", id="no-prefix-non-ascii"),
        pytest.param("sha256=" + "中文", id="cjk"),
    ],
)
def test_non_ascii_signature_is_rejected_not_fatal(header):
    assert verify_signature(b"{}", header) is False


@pytest.mark.parametrize(
    "token", ["\xe9", "ÿ", "中文", "tok\xe9n"], ids=["latin1", "ff", "cjk", "mixed"]
)
def test_non_ascii_verify_token_is_rejected_not_fatal(token):
    assert verify_token(token) is False


def test_non_ascii_token_does_not_match_an_ascii_secret():
    """The coercion must not accidentally make different strings compare equal."""
    assert verify_token("test-verify-token\xe9") is False
    assert verify_token("test-verify-token") is True


# --- config timing guard ----------------------------------------------------


def test_validate_rejects_reclaim_racing_a_live_attempt():
    """
    If reclaim can fire before an attempt's worst case elapses, two workers
    process the same entry and the downstream sees it twice.
    """
    s = Settings()
    s.WHATSAPP_VERIFY_TOKEN = "real-token"
    s.WHATSAPP_APP_SECRET = "real-secret"
    s.DOWNSTREAM_TIMEOUT_SECONDS = 30
    s.RETRY_BACKOFF_MAX_SECONDS = 60
    s.CLAIM_MIN_IDLE_MS = 60_000  # 60s < 90s worst case

    with pytest.raises(RuntimeError, match="CLAIM_MIN_IDLE_MS"):
        s.validate()


def test_shipped_defaults_satisfy_the_timing_guard():
    s = Settings()
    s.WHATSAPP_VERIFY_TOKEN = "real-token"
    s.WHATSAPP_APP_SECRET = "real-secret"
    s.validate()  # must not raise


def test_claim_ttl_outlasts_a_full_attempt():
    s = Settings()
    assert s.claim_ttl_seconds() > s.DOWNSTREAM_TIMEOUT_SECONDS + s.RETRY_BACKOFF_MAX_SECONDS


@pytest.mark.parametrize("block_ms", [0, 1, 1000, 5000, 30_000, 300_000])
def test_socket_timeout_outlasts_a_blocking_read(block_ms):
    """
    read_new() parks on a blocking XREADGROUP for BLOCK_MS whenever the stream
    is idle. If the socket read times out first, every idle poll raises
    TimeoutError and the worker dies -- which is precisely what redis-py 8's
    new 5s socket_timeout default did against the 5s default BLOCK_MS.

    This is a PROXY, not coverage of the real failure. The blocking path itself
    is untestable here: fakeredis has no blocking XREADGROUP, which is why
    conftest pins BLOCK_MS=0 for the whole suite, and the crash only reproduces
    against a live Redis on an idle stream. What is pinned instead is the
    invariant the fix rests on -- the socket must always outlast the block --
    swept across the range BLOCK_MS could plausibly be configured to, so a
    future formula cannot clear the shipped default by luck alone.
    """
    s = Settings()
    s.BLOCK_MS = block_ms

    assert s.socket_timeout_seconds() > s.BLOCK_MS / 1000


def test_client_is_built_with_the_derived_socket_timeout():
    """
    A derived timeout that never reached the client would not have helped.

    The regression being guarded is a silent one: drop the constructor argument
    and redis-py substitutes its own 5s default, so the client still builds and
    every test still passes. Asserting the value landed in the pool -- and that
    it is not merely inherited -- is the only place that omission shows up.
    """
    from app import redis_client

    redis_client.set_redis(None)
    try:
        pool_kwargs = redis_client.get_redis().connection_pool.connection_kwargs
        socket_timeout = pool_kwargs.get("socket_timeout")
        assert socket_timeout == settings.socket_timeout_seconds()
        assert socket_timeout > settings.BLOCK_MS / 1000
    finally:
        redis_client.set_redis(None)
