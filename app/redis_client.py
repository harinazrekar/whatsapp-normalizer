from collections.abc import Awaitable
from typing import Optional, cast

import redis.asyncio as redis

from .config import settings

_client: Optional["redis.Redis"] = None


def get_redis() -> "redis.Redis":
    """
    Lazily-built shared client.

    Deliberately not a module-level singleton: building the connection at import
    time makes the module impossible to point at a fake in tests, and creates a
    connection pool before the app has decided it wants one.
    """
    global _client
    if _client is None:
        _client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _client


def set_redis(client: "redis.Redis | None") -> None:
    """Swap in a client (used by the test suite to inject fakeredis)."""
    global _client
    _client = client


async def ping() -> bool:
    """
    Liveness probe for the health check.

    redis-py types `ping()` as `Awaitable[bool] | bool` because one class backs
    both the sync and async clients; on the async client it is always awaitable.
    Narrowed once here rather than casting at every call site.
    """
    return bool(await cast("Awaitable[bool]", get_redis().ping()))


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
