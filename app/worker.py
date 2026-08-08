"""
Delivery worker.

Reads normalized events from the Redis Stream consumer group and POSTs them to
DOWNSTREAM_WEBHOOK_URL, retrying with exponential backoff and dead-lettering
anything that exhausts its attempts.

Three invariants the loop is built around:

  1. An entry is only XACKed once it has reached a terminal state -- delivered,
     dead-lettered, or safely re-queued for a later attempt. A kill -9 at any
     other point leaves the entry pending, and another worker reclaims it.
  2. An entry is never acked on the strength of a claim alone. Only a *completed*
     delivery marker retires an entry without re-sending; a claim that is merely
     in flight means some worker died mid-attempt, and the event must be retried.
  3. The claim is held for the whole attempt, backoff sleep included. Releasing
     it early lets a reclaiming worker start a parallel attempt on an entry this
     one still holds, which doubles the copies in flight on every retry round.
"""

import asyncio
import os
import signal
import socket
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import metrics
from .config import settings
from .logging_config import configure_logging, get_logger, set_correlation_id
from .queue import (
    ALREADY_DELIVERED,
    IN_PROGRESS,
    Entry,
    abandon_delivery,
    ack,
    begin_delivery,
    complete_delivery,
    dead_letter,
    enqueue_event,
    ensure_group,
    read_new,
    reclaim_stale,
)

log = get_logger(__name__)

# Identifies this worker inside the consumer group. Including the pid means two
# workers on one host don't share a pending list.
CONSUMER_NAME = f"{socket.gethostname()}-{os.getpid()}"

_shutdown = asyncio.Event()


def request_shutdown(*_args: Any) -> None:
    log.info("shutdown_requested", extra={"consumer": CONSUMER_NAME})
    _shutdown.set()


def _redacted_downstream() -> str:
    """
    Origin only. For the integrations this service is usually pointed at (Slack
    incoming webhooks, n8n webhook nodes) the full URL *is* the credential, and
    startup logs are retained by the container log driver.
    """
    if not settings.DOWNSTREAM_WEBHOOK_URL:
        return "(unset)"
    parts = urlsplit(settings.DOWNSTREAM_WEBHOOK_URL)
    return f"{parts.scheme}://{parts.netloc}/..." if parts.netloc else "(malformed)"


def _log_context(event: dict) -> dict:
    return {
        "message_id": event.get("message_id"),
        "event_type": event.get("event_type"),
        "retry_count": event.get("retry_count", 0),
    }


async def dispatch(event: dict, client: httpx.AsyncClient) -> bool:
    """POST one event downstream. True means delivered, False means retry."""
    if not settings.DOWNSTREAM_WEBHOOK_URL:
        # Nothing configured to receive it. Treat as terminal rather than
        # retrying forever -- this is the "just queue and inspect /stats" mode.
        metrics.deliveries.labels(outcome="no_downstream").inc()
        log.debug("no_downstream_configured", extra=_log_context(event))
        return True

    started = time.monotonic()
    try:
        resp = await client.post(
            settings.DOWNSTREAM_WEBHOOK_URL,
            json=event,
            headers={"X-Correlation-Id": str(event.get("correlation_id", ""))},
        )
    except Exception as exc:
        # Deliberately not just httpx.HTTPError: httpx.InvalidURL is not a
        # subclass of it, so a malformed DOWNSTREAM_WEBHOOK_URL used to escape
        # this handler entirely and strand every event it touched.
        metrics.delivery_duration.observe(time.monotonic() - started)
        metrics.deliveries.labels(outcome="failure").inc()
        log.warning(
            "delivery_transport_error",
            extra={**_log_context(event), "error": f"{type(exc).__name__}: {exc}"},
        )
        return False

    metrics.delivery_duration.observe(time.monotonic() - started)
    delivered = resp.status_code < 300
    metrics.deliveries.labels(outcome="success" if delivered else "failure").inc()

    if delivered:
        log.info("delivered", extra={**_log_context(event), "status": resp.status_code})
    else:
        log.warning("delivery_rejected", extra={**_log_context(event), "status": resp.status_code})
    return delivered


async def handle(entry_id: str, event: dict, client: httpx.AsyncClient) -> None:
    """Take one entry to a terminal state, then ack it."""
    # Re-bind the id the API assigned, so worker logs for this event join up with
    # the ingest logs. ContextVars are per-task, so a concurrent batch stays sane.
    set_correlation_id(event.get("correlation_id") or None)

    message_id = event.get("message_id", "")

    claim = await begin_delivery(message_id)

    if claim == ALREADY_DELIVERED:
        # A previous attempt confirmed the downstream received this, then died
        # before acking. Retire the entry without re-sending.
        metrics.deliveries.labels(outcome="skipped_duplicate").inc()
        log.info("delivery_skipped_already_sent", extra=_log_context(event))
        await ack(entry_id)
        return

    if claim == IN_PROGRESS:
        # Another worker is mid-attempt on this event. Leave the entry pending
        # and untouched: acking here would discard an event nobody has confirmed
        # was delivered, and re-sending would duplicate it downstream.
        metrics.deliveries.labels(outcome="skipped_in_progress").inc()
        log.info("delivery_deferred_claim_held", extra=_log_context(event))
        return

    if await dispatch(event, client):
        # Upgrade the short in-flight claim to the long-lived completed marker
        # BEFORE acking, so a crash in between is absorbed on reclaim.
        await complete_delivery(message_id)
        await ack(entry_id)
        return

    event["retry_count"] = event.get("retry_count", 0) + 1

    if event["retry_count"] > settings.MAX_RETRIES:
        await dead_letter(event)
        await abandon_delivery(message_id)
        await ack(entry_id)
        metrics.dead_lettered.inc()
        log.error("dead_lettered", extra=_log_context(event))
        return

    backoff = min(
        settings.RETRY_BACKOFF_BASE ** event["retry_count"],
        settings.RETRY_BACKOFF_MAX_SECONDS,
    )
    metrics.retries.inc()
    log.info("retry_scheduled", extra={**_log_context(event), "backoff_seconds": backoff})

    # The claim stays held across the sleep. Releasing it first would let a
    # reclaiming worker start its own attempt while this one is still holding
    # the entry, and each round would double the number of copies in flight.
    await asyncio.sleep(backoff)

    # Re-publish BEFORE acking. If the process dies between these two lines the
    # entry stays pending and gets reclaimed -- a duplicate attempt, which the
    # delivery claim absorbs. Acking first would lose the event outright.
    await abandon_delivery(message_id)
    await enqueue_event(event)
    await ack(entry_id)


async def process_batch(entries: list[Entry], client: httpx.AsyncClient) -> None:
    """
    Handle a batch concurrently. One event sleeping out its backoff must not
    stall the others behind it.
    """
    if not entries:
        return

    # return_exceptions keeps one poisoned event from killing the whole batch,
    # but the results are inspected rather than discarded: a silently swallowed
    # exception here would look exactly like a successful delivery.
    results = await asyncio.gather(
        *(handle(entry_id, event, client) for entry_id, event in entries),
        return_exceptions=True,
    )
    for (entry_id, event), result in zip(entries, results, strict=False):
        if isinstance(result, BaseException):
            log.error(
                "handler_failed",
                extra={**_log_context(event), "entry_id": entry_id},
                exc_info=result,
            )


async def run() -> None:
    configure_logging()
    settings.validate()
    await ensure_group()

    log.info(
        "worker_started",
        extra={
            "consumer": CONSUMER_NAME,
            "stream": settings.STREAM_KEY,
            "downstream": _redacted_downstream(),
        },
    )

    reclaim_cursor = "0-0"

    async with httpx.AsyncClient(timeout=settings.DOWNSTREAM_TIMEOUT_SECONDS) as client:
        while not _shutdown.is_set():
            # Adopt anything a dead worker left pending before taking new work.
            reclaimed, reclaim_cursor = await reclaim_stale(CONSUMER_NAME, reclaim_cursor)
            if reclaimed:
                metrics.reclaimed.inc(len(reclaimed))
                log.info("entries_reclaimed", extra={"count": len(reclaimed)})
            await process_batch(reclaimed, client)

            if _shutdown.is_set():
                break

            # Blocks up to BLOCK_MS, which bounds how long shutdown waits.
            batch = await read_new(CONSUMER_NAME)
            await process_batch(batch, client)

    log.info("worker_stopped", extra={"consumer": CONSUMER_NAME})


async def main() -> None:
    loop = asyncio.get_running_loop()
    # SIGTERM is what `docker stop` and Kubernetes send. Handling it means the
    # in-flight batch finishes and acks instead of being yanked mid-delivery.
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, request_shutdown)

    await run()


if __name__ == "__main__":
    asyncio.run(main())
