"""
Redis Streams transport for normalized events.

The original implementation used RPUSH/BLPOP on a plain list. That gives you
at-most-once delivery: BLPOP removes the entry, and if the worker dies before
the downstream POST completes the event is gone with nothing to replay from.

A stream with a consumer group gives at-least-once instead:

    XADD        ingest writes the entry
    XREADGROUP  a worker claims it -- it moves to that consumer's pending list
    XACK        the worker confirms it, and only then is it forgotten
    XAUTOCLAIM  a live worker adopts entries a dead worker never acked

At-least-once means duplicates are possible by design, which is why both the
ingest path (dedup on message_id) and the delivery path (the begin/complete
delivery claim below) are idempotent.
"""

import json
from typing import Any, cast

from .config import settings
from .redis_client import get_redis

# One entry = one field, so the payload round-trips as a single JSON blob rather
# than a flattened field map that would lose types.
PAYLOAD_FIELD = "data"

Entry = tuple[str, dict[str, Any]]  # (stream entry id, decoded event dict)


async def ensure_group() -> None:
    """
    Idempotently create the consumer group, creating the stream itself if needed
    (MKSTREAM). Safe to call on every worker start and from tests.
    """
    try:
        await get_redis().xgroup_create(
            name=settings.STREAM_KEY,
            groupname=settings.CONSUMER_GROUP,
            id="0",
            mkstream=True,
        )
    except Exception as exc:  # redis.exceptions.ResponseError
        # BUSYGROUP just means another worker won the race to create it.
        if "BUSYGROUP" not in str(exc):
            raise


async def enqueue_event(event_dict: dict) -> str:
    """Publish one normalized event. Returns the stream entry id."""
    # get_redis() sets decode_responses=True, so the entry id comes back as str.
    # The stubs cannot see that constructor argument and so type every reply as
    # `bytes | str`; this is the one place the decoded-response contract has to
    # be stated for the type checker.
    return cast(
        str,
        await get_redis().xadd(
            name=settings.STREAM_KEY,
            fields={PAYLOAD_FIELD: json.dumps(event_dict, default=str)},
            maxlen=settings.STREAM_MAXLEN,
            approximate=True,
        ),
    )


def _decode(entries: Any) -> list[Entry]:
    """
    Flatten the nested [(stream, [(id, {field: value}), ...]), ...] shape that
    XREADGROUP returns, and drop entries whose payload is unreadable rather than
    letting one bad entry crash the worker loop.
    """
    out: list[Entry] = []
    for _stream, items in entries:
        for entry_id, fields in items:
            raw = fields.get(PAYLOAD_FIELD)
            if raw is None:
                continue
            try:
                out.append((entry_id, json.loads(raw)))
            except json.JSONDecodeError:
                continue
    return out


async def read_new(consumer: str) -> list[Entry]:
    """Claim up to BATCH_SIZE never-before-delivered entries, blocking if idle."""
    # block=None means "return immediately". Tests set BLOCK_MS=0 for this:
    # fakeredis does not implement blocking XREADGROUP, and a blocking read is a
    # Redis feature rather than logic of ours that needs covering.
    block = settings.BLOCK_MS if settings.BLOCK_MS > 0 else None

    entries = await get_redis().xreadgroup(
        groupname=settings.CONSUMER_GROUP,
        consumername=consumer,
        streams={settings.STREAM_KEY: ">"},
        count=settings.BATCH_SIZE,
        block=block,
    )
    return _decode(entries or [])


async def reclaim_stale(consumer: str, start_id: str = "0-0") -> tuple[list[Entry], str]:
    """
    Adopt entries that some consumer claimed but never acked -- i.e. a worker that
    was killed mid-delivery. Returns the reclaimed entries and the cursor to pass
    back on the next sweep.
    """
    result = await get_redis().xautoclaim(
        name=settings.STREAM_KEY,
        groupname=settings.CONSUMER_GROUP,
        consumername=consumer,
        min_idle_time=settings.CLAIM_MIN_IDLE_MS,
        start_id=start_id,
        count=settings.BATCH_SIZE,
    )

    # redis-py returns (next_cursor, entries) and, on newer servers, a third
    # element listing ids that no longer exist.
    next_cursor, claimed = result[0], result[1]

    out: list[Entry] = []
    for entry_id, fields in claimed:
        raw = fields.get(PAYLOAD_FIELD)
        if raw is None:
            continue
        try:
            out.append((entry_id, json.loads(raw)))
        except json.JSONDecodeError:
            continue
    return out, next_cursor


async def ack(entry_id: str) -> None:
    """
    Confirm an entry is fully handled. XACK clears it from the pending list;
    XDEL reclaims the memory, since nothing replays from history here.
    """
    redis = get_redis()
    await redis.xack(settings.STREAM_KEY, settings.CONSUMER_GROUP, entry_id)
    await redis.xdel(settings.STREAM_KEY, entry_id)


async def dead_letter(event: dict) -> None:
    """Park an event that exhausted its retries, for manual inspection or replay."""
    # Capped like the main stream. An uncapped DLQ plus Redis `noeviction` means a
    # downstream that stays down eventually fills memory, at which point XADD on
    # the *ingest* path starts failing and the webhook returns 5xx to Meta.
    await get_redis().xadd(
        name=settings.DLQ_KEY,
        fields={PAYLOAD_FIELD: json.dumps(event, default=str)},
        maxlen=settings.DLQ_MAXLEN,
        approximate=True,
    )


# Delivery-claim states. The distinction matters: "someone is trying right now"
# and "this definitely landed downstream" demand opposite responses, and an
# earlier version of this module conflated them -- a worker that died mid-POST
# left a claim that looked identical to a completed delivery, so the reclaiming
# worker acked the entry and the event was silently dropped.
DELIVERY_IN_FLIGHT = "inflight"
DELIVERY_DONE = "done"

WON = "won"
ALREADY_DELIVERED = "already_delivered"
IN_PROGRESS = "in_progress"


def _delivery_key(message_id: str) -> str:
    return f"wa:delivered:{message_id}"


async def begin_delivery(message_id: str) -> str:
    """
    Try to claim the right to POST message_id downstream.

    Returns WON (go ahead), ALREADY_DELIVERED (a previous attempt confirmed it
    landed -- ack without re-sending), or IN_PROGRESS (another worker holds a
    live claim -- do nothing and let the entry stay pending).

    The claim is written with a SHORT ttl, unlike the completed marker. That is
    what makes a crash mid-POST recoverable: the claim lapses, the reclaimed
    entry wins a fresh one, and the event is retried instead of discarded.
    """
    key = _delivery_key(message_id)
    if await get_redis().set(key, DELIVERY_IN_FLIGHT, ex=settings.claim_ttl_seconds(), nx=True):
        return WON

    current = await get_redis().get(key)
    return ALREADY_DELIVERED if current == DELIVERY_DONE else IN_PROGRESS


async def complete_delivery(message_id: str) -> None:
    """
    Record that the downstream actually received this event.

    Held far longer than the in-flight claim, because its job is to absorb a
    reclaim of an entry whose POST succeeded but whose XACK never landed.
    """
    await get_redis().set(
        _delivery_key(message_id), DELIVERY_DONE, ex=settings.DELIVERED_TTL_SECONDS
    )


async def abandon_delivery(message_id: str) -> None:
    """Drop a claim after a failed POST so the next attempt may proceed."""
    await get_redis().delete(_delivery_key(message_id))


async def depth() -> dict[str, int]:
    """Queue statistics for /stats and /metrics."""
    redis = get_redis()
    stream_len = await redis.xlen(settings.STREAM_KEY)
    dlq_len = await redis.xlen(settings.DLQ_KEY)

    pending = 0
    try:
        summary = await redis.xpending(settings.STREAM_KEY, settings.CONSUMER_GROUP)
        pending = (summary or {}).get("pending", 0)
    except Exception:
        # Group not created yet -- nothing has been ingested.
        pending = 0

    # XLEN counts pending entries too, so reporting it raw would show the same
    # entry under both "queued" and "in_flight". Keep the two disjoint.
    return {
        "queued": max(0, stream_len - pending),
        "in_flight": pending,
        "dead_lettered": dlq_len,
    }


async def peek_dlq(count: int = 20) -> list[dict]:
    """Read the most recent dead-lettered events without consuming them."""
    # `or []` because xrevrange is typed as possibly returning None, which is
    # what a DLQ that has never been written looks like -- the common case on a
    # healthy service, and previously an unguarded iteration over None.
    entries = await get_redis().xrevrange(settings.DLQ_KEY, count=count) or []
    out = []
    for _entry_id, fields in entries:
        # isinstance rather than `is not None`: with decode_responses=True this
        # is always str, and anything else is an entry we cannot read anyway.
        raw = (fields or {}).get(PAYLOAD_FIELD)
        if not isinstance(raw, str):
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return out
