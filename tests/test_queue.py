import pytest

from app import queue
from app.config import settings


@pytest.fixture
async def stream(fake_redis):
    await queue.ensure_group()
    return fake_redis


def make_event(message_id="wamid.Q1", **overrides):
    event = {
        "message_id": message_id,
        "event_type": "message",
        "message_type": "text",
        "text": "hi",
        "raw": {},
        "retry_count": 0,
    }
    event.update(overrides)
    return event


async def test_ensure_group_is_idempotent(fake_redis):
    await queue.ensure_group()
    await queue.ensure_group()  # BUSYGROUP must be swallowed, not raised


async def test_enqueue_then_read_new_round_trips_the_event(stream):
    await queue.enqueue_event(make_event())

    entries = await queue.read_new("consumer-a")
    assert len(entries) == 1
    entry_id, event = entries[0]
    assert event["message_id"] == "wamid.Q1"
    assert event["text"] == "hi"
    assert entry_id


async def test_read_new_does_not_redeliver_to_the_same_group(stream):
    await queue.enqueue_event(make_event())

    assert len(await queue.read_new("consumer-a")) == 1
    # Second consumer in the same group must not see it -- it's pending on A.
    assert await queue.read_new("consumer-b") == []


async def test_unacked_entry_stays_pending(stream):
    """The whole point of a consumer group: reading is not consuming."""
    await queue.enqueue_event(make_event())
    await queue.read_new("consumer-a")

    stats = await queue.depth()
    assert stats["in_flight"] == 1


async def test_ack_clears_the_pending_entry(stream):
    await queue.enqueue_event(make_event())
    ((entry_id, _event),) = await queue.read_new("consumer-a")

    await queue.ack(entry_id)

    stats = await queue.depth()
    assert stats["in_flight"] == 0
    assert stats["queued"] == 0


async def test_reclaim_picks_up_what_a_dead_worker_never_acked(stream, monkeypatch):
    """Simulates a worker that read an entry and then died before acking."""
    await queue.enqueue_event(make_event("wamid.ORPHAN"))
    ((original_id, _event),) = await queue.read_new("dead-worker")

    # Pretend enough time passed that the entry is considered abandoned.
    monkeypatch.setattr(settings, "CLAIM_MIN_IDLE_MS", 0)

    reclaimed, _cursor = await queue.reclaim_stale("live-worker")

    assert len(reclaimed) == 1
    entry_id, event = reclaimed[0]
    assert entry_id == original_id
    assert event["message_id"] == "wamid.ORPHAN"


async def test_reclaim_leaves_fresh_entries_alone(stream, monkeypatch):
    await queue.enqueue_event(make_event())
    await queue.read_new("busy-worker")

    monkeypatch.setattr(settings, "CLAIM_MIN_IDLE_MS", 600_000)
    reclaimed, _cursor = await queue.reclaim_stale("live-worker")

    assert reclaimed == []


async def test_first_caller_wins_the_delivery_claim(stream):
    assert await queue.begin_delivery("wamid.ONCE") == queue.WON


async def test_second_caller_sees_an_attempt_in_progress(stream):
    """Not ALREADY_DELIVERED -- nobody has confirmed the downstream got it yet."""
    await queue.begin_delivery("wamid.ONCE")
    assert await queue.begin_delivery("wamid.ONCE") == queue.IN_PROGRESS


async def test_completed_delivery_is_reported_as_already_delivered(stream):
    await queue.begin_delivery("wamid.DONE")
    await queue.complete_delivery("wamid.DONE")
    assert await queue.begin_delivery("wamid.DONE") == queue.ALREADY_DELIVERED


async def test_abandoning_a_claim_frees_it_for_a_retry(stream):
    await queue.begin_delivery("wamid.RETRY")
    await queue.abandon_delivery("wamid.RETRY")
    assert await queue.begin_delivery("wamid.RETRY") == queue.WON


async def test_in_flight_claim_expires_so_a_dead_worker_cannot_block_forever(
    stream, fake_redis, monkeypatch
):
    """
    The claim TTL is the recovery mechanism: if it never lapsed, an event whose
    worker died mid-POST could never be retried by anyone.
    """
    monkeypatch.setattr(settings, "claim_ttl_seconds", lambda: 1)
    await queue.begin_delivery("wamid.STALE")

    await fake_redis.delete("wa:delivered:wamid.STALE")  # stand-in for TTL expiry

    assert await queue.begin_delivery("wamid.STALE") == queue.WON


async def test_completed_marker_outlives_the_in_flight_claim(stream, fake_redis):
    """A confirmed delivery must be remembered far longer than an attempt."""
    await queue.begin_delivery("wamid.TTL")
    claim_ttl = await fake_redis.ttl("wa:delivered:wamid.TTL")

    await queue.complete_delivery("wamid.TTL")
    done_ttl = await fake_redis.ttl("wa:delivered:wamid.TTL")

    assert done_ttl > claim_ttl


async def test_dead_letter_is_readable(stream):
    await queue.dead_letter(make_event("wamid.DEAD", retry_count=6))

    stats = await queue.depth()
    assert stats["dead_lettered"] == 1

    parked = await queue.peek_dlq()
    assert parked[0]["message_id"] == "wamid.DEAD"
    assert parked[0]["retry_count"] == 6


async def test_malformed_entry_is_skipped_not_fatal(stream, fake_redis):
    """One unreadable entry must not take down the worker loop."""
    await fake_redis.xadd(settings.STREAM_KEY, {"data": "not json"})
    await queue.enqueue_event(make_event("wamid.GOOD"))

    entries = await queue.read_new("consumer-a")

    assert [e["message_id"] for _id, e in entries] == ["wamid.GOOD"]


# --- error paths ------------------------------------------------------------


async def test_ensure_group_reraises_unexpected_errors(fake_redis, monkeypatch):
    """Only BUSYGROUP is benign; anything else must not be swallowed."""

    async def refuse(**_kwargs):
        raise RuntimeError("READONLY You can't write against a read only replica")

    monkeypatch.setattr(fake_redis, "xgroup_create", refuse)

    with pytest.raises(RuntimeError, match="READONLY"):
        await queue.ensure_group()


async def test_entry_without_a_payload_field_is_skipped(stream, fake_redis):
    await fake_redis.xadd(settings.STREAM_KEY, {"unexpected": "shape"})
    await queue.enqueue_event(make_event("wamid.FINE"))

    entries = await queue.read_new("consumer-a")

    assert [e["message_id"] for _id, e in entries] == ["wamid.FINE"]


async def test_reclaim_skips_undecodable_entries(stream, fake_redis, monkeypatch):
    await fake_redis.xadd(settings.STREAM_KEY, {"data": "{{{ not json"})
    await queue.enqueue_event(make_event("wamid.CLAIMABLE"))
    await queue.read_new("dead-worker")

    monkeypatch.setattr(settings, "CLAIM_MIN_IDLE_MS", 0)
    reclaimed, _cursor = await queue.reclaim_stale("live-worker")

    assert [e["message_id"] for _id, e in reclaimed] == ["wamid.CLAIMABLE"]


async def test_depth_reports_zero_pending_before_the_group_exists(fake_redis):
    """/stats and /metrics must work on a cold start, not raise."""
    counts = await queue.depth()
    assert counts == {"queued": 0, "in_flight": 0, "dead_lettered": 0}


async def test_peek_dlq_skips_undecodable_entries(stream, fake_redis):
    await fake_redis.xadd(settings.DLQ_KEY, {"data": "not json"})
    await fake_redis.xadd(settings.DLQ_KEY, {"no_payload": "field"})
    await queue.dead_letter(make_event("wamid.READABLE"))

    assert [e["message_id"] for e in await queue.peek_dlq()] == ["wamid.READABLE"]


async def test_stream_is_trimmed_to_maxlen(stream, fake_redis, monkeypatch):
    """Delivered history must not grow without bound."""
    monkeypatch.setattr(settings, "STREAM_MAXLEN", 5)
    for i in range(500):
        await queue.enqueue_event(make_event(f"wamid.T{i}"))

    # MAXLEN ~ trims in whole nodes, so the exact length is not guaranteed --
    # but it must be bounded near the limit, not merely "fewer than we added".
    assert await fake_redis.xlen(settings.STREAM_KEY) <= 100


async def test_dlq_is_trimmed_to_maxlen(stream, fake_redis, monkeypatch):
    """An uncapped DLQ eventually starves the ingest path of Redis memory."""
    monkeypatch.setattr(settings, "DLQ_MAXLEN", 5)
    for i in range(500):
        await queue.dead_letter(make_event(f"wamid.D{i}"))

    assert await fake_redis.xlen(settings.DLQ_KEY) <= 100


async def test_queued_and_in_flight_do_not_double_count(stream):
    """XLEN includes pending entries; the two counts must stay disjoint."""
    await queue.enqueue_event(make_event("wamid.PENDING"))
    await queue.enqueue_event(make_event("wamid.WAITING"))
    await queue.read_new("w1")  # claims both

    counts = await queue.depth()

    assert counts["in_flight"] == 2
    assert counts["queued"] == 0
