"""
Assertions about the compose files rather than about running code.

A whole class of production defect lives in deployment config and is invisible
to a test suite that only imports `app`: the worker container reported
`unhealthy` forever, in every deployment, while delivering events perfectly.
Omitting a `healthcheck:` block does not mean "no healthcheck" -- the image's
HEALTHCHECK is inherited, and that one probes the API's /health over HTTP while
the worker binds no port.

Parsing the compose files is a legitimate check but a limited one: it proves
what was declared, never what Docker does with it. Actually observing a
container settle into `healthy` needs a running daemon, which the suite does
not have. These tests exist so the declaration cannot silently regress.
"""

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILES = ["docker-compose.yml", "docker-compose.prod.yml"]


def load_compose(filename: str) -> dict:
    return yaml.safe_load((REPO_ROOT / filename).read_text())


@pytest.mark.parametrize("filename", COMPOSE_FILES)
def test_worker_does_not_inherit_the_images_healthcheck(filename):
    """
    The image's HEALTHCHECK probes the API's /health on 8000, which the worker
    does not serve. Omitting a `healthcheck:` block does NOT opt out -- the
    image's is inherited, and the worker then reads `unhealthy` forever while
    delivering perfectly, which teaches whoever is on call to ignore container
    health. It must be overridden explicitly.
    """
    worker = load_compose(filename)["services"]["worker"]
    healthcheck = worker.get("healthcheck")

    assert healthcheck is not None, "worker would inherit the image's HTTP probe"
    assert "8000" not in " ".join(map(str, healthcheck.get("test", [])))


@pytest.mark.parametrize("filename", COMPOSE_FILES)
def test_worker_healthcheck_probes_its_own_metrics_port(filename):
    """
    Once the worker serves its own Prometheus registry it binds a socket, so it
    can have a real liveness signal rather than none at all. A worker that dies
    is otherwise invisible: it publishes nothing, and the only symptom is
    in-flight entries quietly stopping.
    """
    from app.config import settings

    worker = load_compose(filename)["services"]["worker"]
    probe = " ".join(map(str, worker["healthcheck"]["test"]))

    assert worker["healthcheck"].get("disable") is not True
    assert str(settings.WORKER_METRICS_PORT) in probe


@pytest.mark.parametrize("filename", COMPOSE_FILES)
def test_api_keeps_a_real_healthcheck(filename):
    """The API does bind a port, so disabling its probe would lose real signal."""
    healthcheck = load_compose(filename)["services"]["api"]["healthcheck"]

    assert healthcheck.get("disable") is not True
    assert healthcheck.get("test")


@pytest.mark.parametrize("filename", COMPOSE_FILES)
def test_nothing_waits_on_the_workers_health(filename):
    """
    A service whose healthcheck is disabled never reports `healthy`, so any
    `condition: service_healthy` pointed at the worker would deadlock startup
    forever. The two settings are only safe together.
    """
    for name, service in load_compose(filename)["services"].items():
        depends_on = service.get("depends_on") or {}
        if not isinstance(depends_on, dict):
            continue  # list form carries no condition, so it cannot gate on health
        condition = (depends_on.get("worker") or {}).get("condition")
        assert condition != "service_healthy", f"{name} waits on the worker's health"


# --- Redis client configuration (post-1.0 audit regression) -----------------


def test_ordinary_client_is_not_coupled_to_the_blocking_read_timeout(monkeypatch):
    """
    BLOCK_MS is a worker knob. The API never issues a blocking command, so
    raising BLOCK_MS to cut idle polling must not silently stretch how long a
    stalled Redis can hold a webhook request open.
    """
    from app import redis_client
    from app.config import settings

    monkeypatch.setattr(settings, "BLOCK_MS", 30_000)
    redis_client.set_redis(None)
    try:
        ordinary = redis_client.get_redis().connection_pool.connection_kwargs
        blocking = redis_client.get_blocking_redis().connection_pool.connection_kwargs

        assert ordinary["socket_timeout"] == settings.REDIS_SOCKET_TIMEOUT_SECONDS
        assert blocking["socket_timeout"] == settings.socket_timeout_seconds()
        assert ordinary["socket_timeout"] < blocking["socket_timeout"]
    finally:
        redis_client.set_redis(None)


def test_clients_are_built_with_retries():
    """
    `from_url` builds the pool before Redis.__init__ can forward its retry
    default, so without an explicit one the connection gets Retry(NoBackoff(), 0)
    -- no retries at all. Combined with the finite socket timeout redis-py 8 now
    applies to every command, one slow reply becomes a hard error.
    """
    from app import redis_client
    from app.config import settings

    redis_client.set_redis(None)
    try:
        for build in (redis_client.get_redis, redis_client.get_blocking_redis):
            kwargs = build().connection_pool.connection_kwargs
            retry = kwargs.get("retry")
            assert retry is not None, "no retry configured"
            assert retry._retries == settings.REDIS_RETRIES
            assert kwargs.get("socket_connect_timeout") is not None
    finally:
        redis_client.set_redis(None)
