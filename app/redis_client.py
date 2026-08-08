from typing import Optional

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
        _client = redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            # Set explicitly rather than left to the library default, which
            # changed from None to 5s in redis-py 8 and silently capped the
            # blocking XREADGROUP the worker's read loop depends on.
            socket_timeout=settings.socket_timeout_seconds(),
        )
    return _client


def set_redis(client: "redis.Redis | None") -> None:
    """Swap in a client (used by the test suite to inject fakeredis)."""
    global _client
    _client = client


async def ping() -> bool:
    """
    Liveness probe for the health check.

    redis 5 typed `ping()` as `Awaitable[bool] | bool`, because one class backed
    both the sync and async clients, and that union needed narrowing at every
    call site. redis 8 types the async client's `ping()` as awaitable in its own
    right, so no narrowing is needed here any more.
    """
    return bool(await get_redis().ping())


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
