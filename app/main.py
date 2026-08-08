import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from . import metrics
from .config import settings
from .dedup import is_duplicate
from .logging_config import configure_logging, get_correlation_id, get_logger, set_correlation_id
from .models import NormalizedEvent
from .normalizer import extract_events
from .queue import depth, enqueue_event, ensure_group
from .redis_client import get_redis
from .security import SIGNATURE_HEADER, verify_signature, verify_token

log = get_logger(__name__)
limiter = Limiter(key_func=get_remote_address)

# Honoured if the caller supplies one, so a correlation id set by an upstream
# proxy survives into our logs instead of being replaced.
CORRELATION_HEADER = "X-Correlation-Id"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    # Raises and aborts startup if secrets are missing or still placeholders,
    # so an insecure deploy fails loudly at boot instead of quietly at 3am.
    settings.validate()
    # Create the stream and consumer group up front so the very first delivery
    # isn't dropped on the floor waiting for a worker to create them.
    await ensure_group()
    log.info("api_started", extra={"stream": settings.STREAM_KEY})
    yield
    log.info("api_stopping")


app = FastAPI(
    title="WhatsApp Webhook Normalizer",
    description=(
        "Sits in front of the WhatsApp Cloud API webhook. Verifies the handshake, "
        "normalizes every message/status shape into one predictable schema, drops "
        "duplicate redeliveries, and queues events for reliable, retried forwarding "
        "to a downstream URL."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.state.limiter = limiter
# slowapi's handler is typed for its own exception rather than the broad
# Exception that Starlette's signature declares.
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]


@app.middleware("http")
async def correlation_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    cid = set_correlation_id(request.headers.get(CORRELATION_HEADER))
    response = await call_next(request)
    response.headers[CORRELATION_HEADER] = cid
    return response


@app.get("/webhook", summary="Meta webhook verification handshake")
async def verify_webhook(
    hub_mode: str = Query(..., alias="hub.mode"),
    hub_verify_token: str = Query(..., alias="hub.verify_token"),
    hub_challenge: str = Query(..., alias="hub.challenge"),
) -> Response:
    if hub_mode == "subscribe" and verify_token(hub_verify_token):
        log.info("handshake_verified")
        return Response(content=hub_challenge, media_type="text/plain")

    log.warning("handshake_rejected", extra={"hub_mode": hub_mode})
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhook", summary="Receive a WhatsApp Cloud API webhook delivery")
@limiter.limit(settings.WEBHOOK_RATE_LIMIT)
async def receive_webhook(request: Request) -> dict[str, int]:
    # Read the raw bytes BEFORE parsing. The HMAC is computed over exactly what
    # Meta sent; re-serializing parsed JSON changes whitespace and key order and
    # would never match.
    raw_body = await request.body()

    if not verify_signature(raw_body, request.headers.get(SIGNATURE_HEADER)):
        metrics.webhook_requests.labels(outcome="bad_signature").inc()
        log.warning(
            "signature_rejected",
            extra={"client": request.client.host if request.client else None},
        )
        raise HTTPException(status_code=403, detail="Invalid or missing signature")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        metrics.webhook_requests.labels(outcome="bad_json").inc()
        log.warning("malformed_json", extra={"body_bytes": len(raw_body)})
        # `from None`: the decoder's position details would be echoed to an
        # unauthenticated-ish caller, and they say nothing we want to disclose.
        raise HTTPException(status_code=400, detail="Body is not valid JSON") from None

    if not isinstance(payload, dict):
        metrics.webhook_requests.labels(outcome="bad_json").inc()
        raise HTTPException(status_code=400, detail="Body must be a JSON object")

    correlation_id = get_correlation_id() or ""
    events = extract_events(payload)

    try:
        accepted, duplicates = await _ingest(events, correlation_id)
    except Exception as exc:
        # Redis is unreachable or refusing writes. Returning 503 is deliberate:
        # we cannot durably accept the event, and a non-2xx is what makes Meta
        # redeliver it. Answering 200 here would lose it silently. Sustained 5xx
        # does eventually make Meta disable the webhook -- alert on this.
        metrics.webhook_requests.labels(outcome="storage_error").inc()
        log.error("ingest_failed", extra={"error": f"{type(exc).__name__}: {exc}"})
        raise HTTPException(
            status_code=503, detail="Storage unavailable, retry this delivery"
        ) from None

    metrics.webhook_requests.labels(outcome="accepted").inc()

    # Always return fast with a 2xx -- WhatsApp treats slow/non-2xx responses
    # as failures and will start retrying (and eventually back off) deliveries.
    return {"received": len(events), "queued": accepted, "duplicates": duplicates}


async def _ingest(events: list[NormalizedEvent], correlation_id: str) -> tuple[int, int]:
    """Dedup and enqueue. Raises if Redis is unavailable, so the caller can 503."""
    accepted, duplicates = 0, 0
    for event in events:
        metrics.events_received.labels(event_type=event.event_type).inc()

        if await is_duplicate(event.message_id):
            duplicates += 1
            metrics.events_duplicate.labels(event_type=event.event_type).inc()
            log.info(
                "duplicate_dropped",
                extra={"message_id": event.message_id, "event_type": event.event_type},
            )
            continue

        event.correlation_id = correlation_id
        await enqueue_event(event.model_dump())
        accepted += 1
        metrics.events_queued.labels(event_type=event.event_type).inc()
        log.info(
            "event_queued",
            extra={
                "message_id": event.message_id,
                "event_type": event.event_type,
                "message_type": event.message_type,
            },
        )

    return accepted, duplicates


# response_model=None because the return annotation is a union of a dict and a
# Response, which FastAPI would otherwise try to turn into a Pydantic model.
@app.get("/health", summary="Liveness and Redis connectivity check", response_model=None)
async def health() -> dict[str, str] | Response:
    try:
        await get_redis().ping()
    except Exception as exc:
        metrics.redis_up.set(0)
        log.error("health_check_failed", extra={"error": str(exc)})
        # 503, not 200 -- a load balancer must be able to take this instance out
        # of rotation, and it cannot do that if an unreachable Redis still reads
        # as healthy.
        return Response(
            content=json.dumps({"status": "degraded", "redis": "unreachable"}),
            status_code=503,
            media_type="application/json",
        )

    metrics.redis_up.set(1)
    return {"status": "ok", "redis": "ok"}


@app.get("/stats", summary="Current queue depth, in-flight count, and dead letters")
async def stats() -> dict[str, Any]:
    counts = await depth()
    return {**counts, "dedup_hit_rate": metrics.dedup_hit_rate()}


@app.get("/metrics", summary="Prometheus metrics")
async def prometheus_metrics() -> Response:
    # Sampled at scrape time from Redis, which is the only view both the API and
    # the worker share.
    try:
        counts = await depth()
        metrics.queue_depth.set(counts["queued"])
        metrics.queue_in_flight.set(counts["in_flight"])
        metrics.queue_dead_lettered.set(counts["dead_lettered"])
        metrics.redis_up.set(1)
    except Exception:
        metrics.redis_up.set(0)

    body, content_type = metrics.render()
    return Response(content=body, media_type=content_type)
