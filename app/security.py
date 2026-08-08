import hashlib
import hmac

from .config import settings

SIGNATURE_HEADER = "X-Hub-Signature-256"
SIGNATURE_PREFIX = "sha256="


def _constant_time_equals(expected: str, candidate: str) -> bool:
    """
    Constant-time comparison that tolerates non-ASCII input.

    `hmac.compare_digest` raises TypeError on str containing non-ASCII, and
    uvicorn decodes header values as latin-1 while obs-text (0x80-0xFF) is legal
    in HTTP. Passing those straight in let any anonymous caller turn a request
    into an unhandled 500 -- and sustained 5xx is exactly what makes Meta back
    off and eventually disable the webhook. Comparing bytes has no such limit.
    """
    return hmac.compare_digest(
        expected.encode("utf-8", "surrogateescape"),
        candidate.encode("utf-8", "surrogateescape"),
    )


def compute_signature(raw_body: bytes, app_secret: str) -> str:
    """The value Meta puts in X-Hub-Signature-256, including the 'sha256=' prefix."""
    digest = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return SIGNATURE_PREFIX + digest


def verify_signature(raw_body: bytes, header_value: str | None) -> bool:
    """
    Verify Meta's X-Hub-Signature-256 against the *exact bytes* of the request body.

    Re-serializing parsed JSON would change whitespace and key order and break the
    digest, so callers must pass the raw body read before any parsing.

    Comparison uses hmac.compare_digest: a plain `==` short-circuits on the first
    differing byte, which leaks the correct prefix to anyone timing the responses.
    """
    if not settings.REQUIRE_SIGNATURE:
        return True

    if not settings.WHATSAPP_APP_SECRET:
        # validate() should have stopped startup long before this; refuse anyway
        # rather than defaulting open.
        return False

    if not header_value:
        return False

    if not header_value.startswith(SIGNATURE_PREFIX):
        return False

    expected = compute_signature(raw_body, settings.WHATSAPP_APP_SECRET)
    return _constant_time_equals(expected, header_value)


def verify_token(candidate: str) -> bool:
    """Constant-time comparison for the GET handshake token."""
    return _constant_time_equals(settings.WHATSAPP_VERIFY_TOKEN, candidate)
