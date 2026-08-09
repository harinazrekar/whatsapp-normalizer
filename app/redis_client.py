from typing import Optional

import redis.asyncio as redis
from redis.backoff import ExponentialWithJitterBackoff
from redis.exceptions import ConnectionError, TimeoutError
from redis.retry import Retry

from .config import settings

_client: Optional["redis.Redis"] = None
_blocking_client: Optional["redis.Redis"] = None


def _build_client(socket_timeout: float) -> "redis.Redis":
    return redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_timeout=socket_timeout,
        socket_connect_timeout=settings.REDIS_CONNECT_TIMEOUT_SECONDS,
        # `from_url` builds the pool before Redis.__init__ can forward its own
        # retry default, so without this the connection falls through to
        # Retry(NoBackoff(), 0) -- no retries at all. Combined with the finite
        # socket timeout redis-py 8 now applies to *every* command, a single
        # slow reply (an AOF rewrite fork, memory pressure) would surface as a
        # hard error rather than being ridden out.
        retry=Retry(ExponentialWithJitterBackoff(base=0.1, cap=2), settings.REDIS_RETRIES),
        retry_on_error=[ConnectionError, TimeoutError],
        # Detects a connection silently dropped by a NAT or firewall before a
        # real command inherits the failure.
        health_check_interval=settings.REDIS_HEALTH_CHECK_INTERVAL_SECONDS,
    )


def get_redis() -> "redis.Redis":
    """
    Lazily-built shared client for ordinary, non-blocking commands.

    Deliberately not a module-level singleton: building the connection at import
    time makes the module impossible to point at a fake in tests, and creates a
    connection pool before the app has decided it wants one.
    """
    global _client
    if _client is None:
        _client = _build_client(settings.REDIS_SOCKET_TIMEOUT_SECONDS)
    return _client


def get_blocking_redis() -> "redis.Redis":
    """
    Separate client for the one command that parks on the socket on purpose.

    The read timeout a blocking XREADGROUP needs is derived from BLOCK_MS, and
    that is a worker tuning knob. Sharing one pool meant the API -- which never
    issues a blocking command -- inherited it: raising BLOCK_MS to reduce idle
    polling would silently stretch how long a stalled Redis could hold a webhook
    request, with no apparent connection between the two settings.

    In the API process this pool is simply never built.
    """
    global _blocking_client
    if _blocking_client is None:
        _blocking_client = _build_client(settings.socket_timeout_seconds())
    return _blocking_client


def set_redis(client: "redis.Redis | None") -> None:
    """
    Swap in a client (used by the test suite to inject fakeredis).

    Sets both pools: tests exercise the blocking read path through the same fake,
    and leaving them split would give a test two different datasets.
    """
    global _client, _blocking_client
    _client = client
    _blocking_client = client


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
    global _client, _blocking_client
    for client in {id(_client): _client, id(_blocking_client): _blocking_client}.values():
        if client is not None:
            await client.aclose()
    _client = None
    _blocking_client = None
