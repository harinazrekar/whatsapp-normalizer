import asyncio

import httpx
import pytest

from app import queue, worker
from app.config import settings


class FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class FakeClient:
    """
    Stands in for httpx.AsyncClient. Records every POST so tests can assert on
    how many times an event actually reached the downstream.
    """

    def __init__(self, results=None):
        # results: list of int status codes or Exception instances, consumed in
        # order. Runs out -> keeps returning the last one.
        self.results = list(results or [200])
        self.calls = []

    async def post(self, url, json=None, headers=None):
        self.calls.append(json)
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return FakeResponse(result)

    @property
    def delivered_ids(self):
        return [c["message_id"] for c in self.calls]

    # run() builds its client with `async with`, so the fake needs the protocol.
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


@pytest.fixture(autouse=True)
def downstream_configured(monkeypatch):
    monkeypatch.setattr(settings, "DOWNSTREAM_WEBHOOK_URL", "https://downstream.test/hook")


# Captured before any patching -- a stub that called asyncio.sleep by name would
# call whatever replaced it, i.e. itself.
_REAL_SLEEP = asyncio.sleep


@pytest.fixture(autouse=True)
def no_real_sleeping(monkeypatch):
    """Collapse retry backoff so the suite doesn't actually wait."""
    monkeypatch.setattr(asyncio, "sleep", lambda _s: _REAL_SLEEP(0))


@pytest.fixture
async def stream(fake_redis):
    await queue.ensure_group()
    return fake_redis


def make_event(message_id="wamid.W1", **overrides):
    event = {
        "message_id": message_id,
        "event_type": "message",
        "message_type": "text",
        "text": "hello",
        "raw": {},
        "retry_count": 0,
    }
    event.update(overrides)
    return event


# --- happy path -------------------------------------------------------------


async def test_successful_delivery_acks_the_entry(stream):
    await queue.enqueue_event(make_event())
    entries = await queue.read_new("w1")
    client = FakeClient([200])

    await worker.process_batch(entries, client)

    assert client.delivered_ids == ["wamid.W1"]
    stats = await queue.depth()
    assert stats == {"queued": 0, "in_flight": 0, "dead_lettered": 0}


async def test_missing_downstream_url_acks_without_posting(stream, monkeypatch):
    monkeypatch.setattr(settings, "DOWNSTREAM_WEBHOOK_URL", "")
    await queue.enqueue_event(make_event())
    entries = await queue.read_new("w1")
    client = FakeClient([200])

    await worker.process_batch(entries, client)

    assert client.calls == []
    assert (await queue.depth())["in_flight"] == 0


# --- the invariant that motivated Redis Streams -----------------------------


async def test_crash_after_post_before_ack_does_not_double_deliver(stream, monkeypatch):
    """
    The exact failure a plain list cannot survive: the worker POSTs successfully,
    then dies before acknowledging. The entry is still pending, another worker
    reclaims it -- and must NOT send it downstream a second time.
    """
    await queue.enqueue_event(make_event("wamid.CRASH"))
    entries = await queue.read_new("doomed-worker")
    client = FakeClient([200])

    # Deliver, but die before the ack lands.
    monkeypatch.setattr(worker, "ack", _boom)
    with pytest.raises(RuntimeError):
        await worker.handle(entries[0][0], entries[0][1], client)

    assert client.delivered_ids == ["wamid.CRASH"]
    assert (await queue.depth())["in_flight"] == 1  # still pending, not lost

    # A live worker adopts it.
    monkeypatch.undo()
    monkeypatch.setattr(settings, "CLAIM_MIN_IDLE_MS", 0)
    reclaimed, _cursor = await queue.reclaim_stale("live-worker")
    assert len(reclaimed) == 1

    await worker.process_batch(reclaimed, client)

    # Reclaimed and acked, but not re-sent.
    assert client.delivered_ids == ["wamid.CRASH"]
    assert (await queue.depth())["in_flight"] == 0


async def _boom(*_args, **_kwargs):
    raise RuntimeError("worker died before ack")


async def test_event_survives_a_worker_that_never_processed_it(stream, monkeypatch):
    """Read but never handled -- the event must still be recoverable."""
    await queue.enqueue_event(make_event("wamid.ORPHAN"))
    await queue.read_new("doomed-worker")

    monkeypatch.setattr(settings, "CLAIM_MIN_IDLE_MS", 0)
    reclaimed, _cursor = await queue.reclaim_stale("live-worker")
    client = FakeClient([200])
    await worker.process_batch(reclaimed, client)

    assert client.delivered_ids == ["wamid.ORPHAN"]


# --- retry / backoff / DLQ --------------------------------------------------


async def test_failed_delivery_is_requeued_with_incremented_retry_count(stream):
    await queue.enqueue_event(make_event("wamid.FAIL1"))
    entries = await queue.read_new("w1")

    await worker.process_batch(entries, FakeClient([500]))

    # Original acked, replacement waiting with retry_count bumped.
    assert (await queue.depth())["in_flight"] == 0
    retry_entries = await queue.read_new("w2")
    assert len(retry_entries) == 1
    assert retry_entries[0][1]["retry_count"] == 1


async def test_transport_error_is_treated_as_retryable(stream):
    await queue.enqueue_event(make_event("wamid.NETFAIL"))
    entries = await queue.read_new("w1")

    await worker.process_batch(entries, FakeClient([httpx.ConnectError("refused")]))

    retry_entries = await queue.read_new("w2")
    assert retry_entries[0][1]["retry_count"] == 1


async def test_succeeds_after_transient_failures(stream, monkeypatch):
    """Fail twice, then succeed -- the event must land exactly once."""
    monkeypatch.setattr(settings, "MAX_RETRIES", 5)
    await queue.enqueue_event(make_event("wamid.FLAKY"))
    client = FakeClient([500, 500, 200])

    for attempt in range(3):
        entries = await queue.read_new(f"w{attempt}")
        assert entries, f"nothing to process on attempt {attempt}"
        await worker.process_batch(entries, client)

    assert client.delivered_ids == ["wamid.FLAKY"] * 3  # three POST attempts
    stats = await queue.depth()
    assert stats["queued"] == 0
    assert stats["dead_lettered"] == 0


async def test_exhausted_retries_land_in_the_dlq(stream, monkeypatch):
    monkeypatch.setattr(settings, "MAX_RETRIES", 2)
    await queue.enqueue_event(make_event("wamid.DOOMED"))
    client = FakeClient([500])

    for attempt in range(3):
        entries = await queue.read_new(f"w{attempt}")
        if not entries:
            break
        await worker.process_batch(entries, client)

    stats = await queue.depth()
    assert stats["queued"] == 0
    assert stats["dead_lettered"] == 1

    parked = await queue.peek_dlq()
    assert parked[0]["message_id"] == "wamid.DOOMED"
    assert parked[0]["retry_count"] == 3  # MAX_RETRIES + 1


async def test_backoff_grows_and_is_capped(stream, monkeypatch):
    """Backoff must be exponential but never park a worker indefinitely."""
    monkeypatch.setattr(settings, "RETRY_BACKOFF_BASE", 2)
    monkeypatch.setattr(settings, "RETRY_BACKOFF_MAX_SECONDS", 10)
    monkeypatch.setattr(settings, "MAX_RETRIES", 10)

    slept = []
    monkeypatch.setattr(asyncio, "sleep", lambda s: slept.append(s) or _REAL_SLEEP(0))

    await queue.enqueue_event(make_event("wamid.SLOW"))
    client = FakeClient([500])
    for attempt in range(6):
        entries = await queue.read_new(f"w{attempt}")
        await worker.process_batch(entries, client)

    assert slept == [2, 4, 8, 10, 10, 10]


# --- graceful shutdown ------------------------------------------------------


async def test_batch_is_processed_concurrently(stream, monkeypatch):
    """
    A slow event must not block the rest of its batch. Asserted by ordering, not
    by a bare success count -- a sequential implementation passes that.
    """
    for i in range(3):
        await queue.enqueue_event(make_event(f"wamid.B{i}"))
    entries = await queue.read_new("w1")

    finished = []
    real_dispatch = worker.dispatch

    async def slow_first(event, client):
        # B0 waits; if handling were sequential, B1 and B2 could not finish first.
        if event["message_id"] == "wamid.B0":
            await _REAL_SLEEP(0.05)
        result = await real_dispatch(event, client)
        finished.append(event["message_id"])
        return result

    monkeypatch.setattr(worker, "dispatch", slow_first)
    await worker.process_batch(entries, FakeClient([200]))

    assert finished[-1] == "wamid.B0", f"expected B0 last, got {finished}"
    assert sorted(finished) == ["wamid.B0", "wamid.B1", "wamid.B2"]


async def test_shutdown_flag_stops_the_run_loop(stream, monkeypatch):
    monkeypatch.setattr(settings, "DOWNSTREAM_WEBHOOK_URL", "")
    worker._shutdown.clear()
    worker.request_shutdown()

    # Already flagged, so run() must return promptly rather than block on a read.
    await asyncio.wait_for(worker.run(), timeout=5)

    worker._shutdown.clear()


async def test_shutdown_lets_the_in_flight_batch_finish(stream, monkeypatch):
    """
    SIGTERM during a batch must not yank events mid-delivery. Driven through
    run() -- asserting against process_batch alone proves nothing, since it
    never consults the shutdown flag.
    """
    for i in range(3):
        await queue.enqueue_event(make_event(f"wamid.S{i}"))

    client = FakeClient([200])
    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda **_kwargs: client)

    real_read = worker.read_new

    async def read_then_terminate(consumer):
        entries = await real_read(consumer)
        # SIGTERM lands while this batch is claimed but not yet delivered.
        worker.request_shutdown()
        return entries

    monkeypatch.setattr(worker, "read_new", read_then_terminate)

    worker._shutdown.clear()
    try:
        await asyncio.wait_for(worker.run(), timeout=5)
    finally:
        worker._shutdown.clear()

    assert sorted(client.delivered_ids) == ["wamid.S0", "wamid.S1", "wamid.S2"]
    assert (await queue.depth())["in_flight"] == 0


# --- the run() loop ---------------------------------------------------------


async def test_run_loop_delivers_then_exits_on_shutdown(stream, monkeypatch):
    """End-to-end through the real loop: enqueue, run, deliver, ack, stop."""
    await queue.enqueue_event(make_event("wamid.LOOP"))
    client = FakeClient([200])
    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda **_kwargs: client)

    real_read = worker.read_new

    async def read_once(consumer):
        entries = await real_read(consumer)
        worker.request_shutdown()  # one pass only, then unwind
        return entries

    monkeypatch.setattr(worker, "read_new", read_once)

    worker._shutdown.clear()
    try:
        await asyncio.wait_for(worker.run(), timeout=5)
    finally:
        worker._shutdown.clear()

    assert client.delivered_ids == ["wamid.LOOP"]
    assert (await queue.depth())["in_flight"] == 0


async def test_run_loop_reclaims_before_taking_new_work(stream, monkeypatch):
    """A restarted worker must drain the previous one's orphans first."""
    await queue.enqueue_event(make_event("wamid.LEFTOVER"))
    await queue.read_new("dead-worker")  # claimed, never acked
    monkeypatch.setattr(settings, "CLAIM_MIN_IDLE_MS", 0)
    # run() calls validate(), which rightly rejects a zero reclaim delay. The
    # config relationship is covered in test_security.py; here it is in the way.
    monkeypatch.setattr(settings, "validate", lambda: None)

    client = FakeClient([200])
    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda **_kwargs: client)

    async def read_none(_consumer):
        worker.request_shutdown()
        return []

    monkeypatch.setattr(worker, "read_new", read_none)

    worker._shutdown.clear()
    try:
        await asyncio.wait_for(worker.run(), timeout=5)
    finally:
        worker._shutdown.clear()

    assert client.delivered_ids == ["wamid.LEFTOVER"]


async def test_poisoned_event_does_not_kill_the_batch(stream, monkeypatch, caplog):
    """
    One handler blowing up must not take its siblings down with it -- and the
    failure must be logged, not silently swallowed by gather().
    """
    await queue.enqueue_event(make_event("wamid.OK1"))
    await queue.enqueue_event(make_event("wamid.POISON"))
    entries = await queue.read_new("w1")

    real_dispatch = worker.dispatch

    async def explode_on_poison(event, client):
        if event["message_id"] == "wamid.POISON":
            raise RuntimeError("handler blew up")
        return await real_dispatch(event, client)

    monkeypatch.setattr(worker, "dispatch", explode_on_poison)

    client = FakeClient([200])
    with caplog.at_level("ERROR"):
        await worker.process_batch(entries, client)

    assert client.delivered_ids == ["wamid.OK1"]
    assert "handler_failed" in caplog.text

    # The poisoned entry must remain recoverable, not be silently retired --
    # this is the regression guard for the crash-mid-POST event-loss bug.
    assert (await queue.depth())["in_flight"] == 1

    monkeypatch.setattr(worker, "dispatch", real_dispatch)  # the fault clears
    monkeypatch.setattr(settings, "CLAIM_MIN_IDLE_MS", 0)
    # Stand-in for the in-flight claim's TTL lapsing, which is what makes a
    # worker that died mid-attempt recoverable at all.
    await queue.abandon_delivery("wamid.POISON")

    reclaimed, _cursor = await queue.reclaim_stale("recovery-worker")
    recovered = FakeClient([200])
    await worker.process_batch(reclaimed, recovered)

    assert recovered.delivered_ids == ["wamid.POISON"]
    assert (await queue.depth())["in_flight"] == 0


# --- regressions from the pre-1.0 audit -------------------------------------


async def test_crash_before_post_does_not_lose_the_event(stream, monkeypatch):
    """
    The inverse of the crash-after-POST case, and the one that used to lose data.

    A worker claimed delivery, then died before the downstream ever saw the
    event. The claim looked identical to a completed delivery, so the reclaiming
    worker acked the entry: no delivery, no DLQ entry, no error. Silent loss.
    """
    await queue.enqueue_event(make_event("wamid.STRAND"))
    entries = await queue.read_new("doomed-worker")

    # Patched at the dispatch boundary, not inside it: a transport error is
    # caught and retried, whereas a process death escapes handle() entirely.
    real_dispatch = worker.dispatch

    async def die(_event, _client):
        raise RuntimeError("process died mid-POST")

    monkeypatch.setattr(worker, "dispatch", die)
    await worker.process_batch(entries, FakeClient([200]))
    # Restored by name, not monkeypatch.undo(): undo() shares an instance with
    # the autouse fixture and would also clear DOWNSTREAM_WEBHOOK_URL.
    monkeypatch.setattr(worker, "dispatch", real_dispatch)

    assert (await queue.depth())["in_flight"] == 1  # pending, not acked

    # The claim lapses (short TTL), then a live worker adopts the entry.
    await queue.abandon_delivery("wamid.STRAND")
    monkeypatch.setattr(settings, "CLAIM_MIN_IDLE_MS", 0)
    reclaimed, _cursor = await queue.reclaim_stale("live-worker")

    recovered = FakeClient([200])
    await worker.process_batch(reclaimed, recovered)

    assert recovered.delivered_ids == ["wamid.STRAND"]
    assert (await queue.depth()) == {"queued": 0, "in_flight": 0, "dead_lettered": 0}


async def test_malformed_downstream_url_is_retried_not_stranded(stream, monkeypatch):
    """
    httpx.InvalidURL is not an httpx.HTTPError, so it used to escape dispatch's
    handler entirely and strand every event a misconfigured URL touched.
    """
    monkeypatch.setattr(settings, "DOWNSTREAM_WEBHOOK_URL", "not a url")
    await queue.enqueue_event(make_event("wamid.BADURL"))
    entries = await queue.read_new("w1")

    await worker.process_batch(entries, FakeClient([httpx.InvalidURL("malformed")]))

    # Treated as an ordinary delivery failure: requeued for another attempt.
    assert (await queue.depth())["in_flight"] == 0
    retry_entries = await queue.read_new("w2")
    assert retry_entries[0][1]["retry_count"] == 1


async def test_a_live_claim_blocks_a_concurrent_reclaimer(stream, monkeypatch):
    """
    While one worker is mid-attempt, a reclaimer must neither re-POST nor ack.
    Doing either amplified one event into two copies on every retry round.
    """
    await queue.enqueue_event(make_event("wamid.AMP"))
    entries = await queue.read_new("worker-a")

    # worker-a takes the claim and is still working.
    assert await queue.begin_delivery("wamid.AMP") == queue.WON

    monkeypatch.setattr(settings, "CLAIM_MIN_IDLE_MS", 0)
    reclaimed, _cursor = await queue.reclaim_stale("worker-b")
    assert len(reclaimed) == 1

    intruder = FakeClient([200])
    await worker.process_batch(reclaimed, intruder)

    assert intruder.calls == [], "reclaimer must not deliver an event under a live claim"
    assert (await queue.depth())["in_flight"] == 1, "entry must stay pending"
    assert entries  # original claim still owns the work


async def test_startup_config_is_left_alone_by_the_worker(stream):
    """The shipped defaults must satisfy their own timing guard."""
    from app.config import Settings

    Settings().validate()


async def test_downstream_url_is_redacted_in_logs(monkeypatch):
    """For Slack/n8n endpoints the URL is the credential; logs retain it."""
    monkeypatch.setattr(
        settings, "DOWNSTREAM_WEBHOOK_URL", "https://hooks.slack.com/services/T00/B00/XXXSECRET"
    )
    redacted = worker._redacted_downstream()

    assert "XXXSECRET" not in redacted
    assert redacted == "https://hooks.slack.com/..."


async def test_unset_downstream_url_is_reported_plainly(monkeypatch):
    monkeypatch.setattr(settings, "DOWNSTREAM_WEBHOOK_URL", "")
    assert worker._redacted_downstream() == "(unset)"
