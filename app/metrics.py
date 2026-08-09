"""
Prometheus instrumentation.

Counters are process-local, so the API and worker each export their own. That is
the normal Prometheus model -- scrape both targets and sum in the query -- and is
why queue depth is a Gauge read from Redis at scrape time rather than a counter
incremented in code: Redis is the single source of truth both processes agree on.
"""

# CONTENT_TYPE_LATEST must come from the same module as generate_latest: the
# openmetrics variant declares a format this body is not in.
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

REGISTRY = CollectorRegistry()

# --- ingest -----------------------------------------------------------------

events_received = Counter(
    "wa_events_received_total",
    "Normalized events extracted from inbound webhook deliveries.",
    ["event_type"],
    registry=REGISTRY,
)

events_queued = Counter(
    "wa_events_queued_total",
    "Events published to the Redis Stream.",
    ["event_type"],
    registry=REGISTRY,
)

events_duplicate = Counter(
    "wa_events_duplicate_total",
    "Deliveries dropped because the message_id was already seen.",
    ["event_type"],
    registry=REGISTRY,
)

webhook_requests = Counter(
    "wa_webhook_requests_total",
    "Inbound webhook requests by outcome.",
    ["outcome"],  # accepted | bad_signature | bad_json | storage_error
    registry=REGISTRY,
)

# --- delivery ---------------------------------------------------------------

deliveries = Counter(
    "wa_deliveries_total",
    "Downstream delivery attempts by outcome.",
    ["outcome"],  # success | failure | skipped_duplicate | skipped_in_progress | no_downstream
    registry=REGISTRY,
)

delivery_duration = Histogram(
    "wa_delivery_duration_seconds",
    "Wall time of a downstream POST.",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
    registry=REGISTRY,
)

retries = Counter(
    "wa_retries_total",
    "Delivery attempts rescheduled after a failure.",
    registry=REGISTRY,
)

dead_lettered = Counter(
    "wa_dead_lettered_total",
    "Events parked in the DLQ after exhausting retries.",
    registry=REGISTRY,
)

reclaimed = Counter(
    "wa_reclaimed_total",
    "Entries adopted from a consumer that never acknowledged them.",
    registry=REGISTRY,
)

# --- queue state (sampled from Redis at scrape time) ------------------------

queue_depth = Gauge(
    "wa_queue_depth",
    "Entries currently in the event stream.",
    registry=REGISTRY,
)

queue_in_flight = Gauge(
    "wa_queue_in_flight",
    "Entries delivered to a consumer but not yet acknowledged.",
    registry=REGISTRY,
)

queue_dead_lettered = Gauge(
    "wa_queue_dead_lettered",
    "Entries currently sitting in the DLQ.",
    registry=REGISTRY,
)

redis_errors = Counter(
    "wa_redis_errors_total",
    "Redis errors caught by the worker loop and ridden out rather than exiting.",
    registry=REGISTRY,
)

redis_up = Gauge(
    "wa_redis_up",
    "1 if Redis responded to the last health check, 0 otherwise.",
    registry=REGISTRY,
)


def dedup_hit_rate() -> float:
    """
    Share of received events that were duplicates. Exposed as a helper rather
    than a metric because Prometheus computes ratios far better than we can --
    but /stats shows it directly for anyone without a Prometheus.
    """
    received = _counter_total(events_received)
    duplicates = _counter_total(events_duplicate)
    return round(duplicates / received, 4) if received else 0.0


def _counter_total(counter: Counter) -> float:
    total = 0.0
    for metric in counter.collect():
        for sample in metric.samples:
            if sample.name.endswith("_total"):
                total += sample.value
    return total


def render() -> tuple[bytes, str]:
    """Serialized metrics plus the content type Prometheus expects."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
