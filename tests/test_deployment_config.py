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
def test_worker_disables_the_inherited_healthcheck(filename):
    """
    `disable: true` explicitly -- an absent block inherits the image's probe.

    Every deployment shares one image, so this has to hold in both files or the
    environment that was missed is the one that reports a permanently failing
    service and trains whoever is on call to ignore container health.
    """
    worker = load_compose(filename)["services"]["worker"]

    assert worker.get("healthcheck") == {"disable": True}


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
