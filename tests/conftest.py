import os

# Set before anything imports app.config -- the Settings singleton reads the
# environment at import time, and app startup refuses to boot without these.
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("WHATSAPP_APP_SECRET", "test-app-secret")
os.environ.setdefault("REQUIRE_SIGNATURE", "true")
os.environ.setdefault("WEBHOOK_RATE_LIMIT", "10000/minute")
# Non-blocking stream reads: fakeredis has no blocking XREADGROUP, and tests
# should never sit waiting on one anyway.
os.environ.setdefault("BLOCK_MS", "0")

import fakeredis.aioredis  # noqa: E402
import pytest  # noqa: E402

from app import redis_client  # noqa: E402


@pytest.fixture
def fake_redis():
    """A clean in-process Redis for every test. No external server required."""
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    redis_client.set_redis(client)
    yield client
    redis_client.set_redis(None)


@pytest.fixture
def client(fake_redis):
    """TestClient with the app lifespan running (so startup validation is exercised)."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
