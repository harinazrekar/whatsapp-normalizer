# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Refreshed every pinned dependency to current releases, including major bumps:
  `pytest` 8 → 9, `pytest-asyncio` 0.24 → 1.4, `pytest-cov` 5 → 7,
  `black` 24 → 26, `ruff` 0.6 → 0.16, `mypy` 1.11 → 2.3, `pre-commit` 3 → 4.6,
  `fastapi` 0.115 → 0.141, `uvicorn` 0.30 → 0.52, `redis` 5 → 8,
  `httpx` 0.27 → 0.28, `pydantic` 2.9 → 2.13. All are verified by the full
  suite, lint, format, and type-check.

  The `redis` major was the one that was not routine — it silently broke the
  worker's read loop. See the crash-loop entry under Fixed.
- Dropped the `tests.*` mypy override from `pyproject.toml`. It never applied:
  `exclude` already skips `tests/`, and `make typecheck` only ever passes `app`.
  `mypy` 2.3 reports unused config sections, which is how a dead block that had
  been there since the hardening release finally surfaced.
- Bumped `actions/checkout` to v7, `actions/setup-python` to v7, and
  `actions/upload-artifact` to v7, clearing the Node 20 deprecation warnings on
  every CI run. `.pre-commit-config.yaml` revs realigned with the pinned
  `ruff` and `black` versions so local hooks and CI cannot disagree.

### Fixed

- **The worker crash-looped on `redis` 8.** redis-py 8 changed the default
  `socket_timeout` from `None` to `5` seconds — precisely the value of the
  default `BLOCK_MS`. `read_new()` issues a blocking `XREADGROUP` that parks on
  the socket for `BLOCK_MS` by design when the stream is idle, so the socket now
  timed out at the same instant the block legitimately expired. Every idle poll
  raised `redis.exceptions.TimeoutError`, killing the worker roughly every five
  seconds; with `restart: unless-stopped` it restarted forever and delivered
  nothing. `socket_timeout` is now set explicitly, derived from `BLOCK_MS` via
  `Settings.socket_timeout_seconds()` so the two cannot drift — the same approach
  already used for `claim_ttl_seconds()` — and applied to the dedicated
  `get_blocking_redis()` pool that issues the blocking read.

  Neither the type checker nor the test suite could see this: `fakeredis` does
  not implement blocking `XREADGROUP`, which is exactly why the suite pins
  `BLOCK_MS=0`. It reproduces only against a real Redis, on an idle stream.
- Hardened `peek_dlq()` against a `None` from `xrevrange`. This is a typing
  accommodation, not a fixed runtime bug: `XREVRANGE` on a missing key returns
  an empty array, never nil, so the healthy never-written-DLQ case always worked.
  `redis` 8 types the reply `StreamRangeResponse | None`, and the guard is what
  satisfies that. (An earlier version of this entry claimed a real crash here;
  it was wrong, and the probe that disproved it is in the test suite.)
- The `worker` service reported `unhealthy` forever in both compose files, in
  every deployment, while working perfectly. Leaving the `healthcheck:` block
  out does not mean "no healthcheck" — the image's `HEALTHCHECK` is inherited,
  and that one probes the API's `/health` over HTTP while the worker binds no
  port. Disabled explicitly with `healthcheck: disable: true`. The cost of the
  old behaviour was not cosmetic: a service permanently stuck in a failing state
  trains whoever is on call to ignore container health, and anything that gates
  on it (orchestrator readiness, alerting, health-keyed restart policies) had no
  usable signal for the worker. Caught by running the stack, not by the suite.
- **The worker could still be killed by a slow Redis.** The earlier
  `socket_timeout` fix restored enough headroom for the blocking read, but
  redis-py 8 applies that finite timeout to *every* command, and `from_url`
  supplies no retries at all — it builds the connection pool before
  `Redis.__init__` can forward its retry default, so the connection falls
  through to `Retry(NoBackoff(), 0)`. A single slow reply (an AOF rewrite fork,
  memory pressure) therefore raised straight out of `reclaim_stale()` or
  `read_new()`, neither of which the run loop guarded, exiting the process.
  `restart: unless-stopped` then returned it to the same stalled Redis: a
  restart loop that delivers nothing — the same shape as the bug above, one
  layer up. Both clients now configure `REDIS_RETRIES` with exponential jittered
  backoff, an explicit `socket_connect_timeout`, and a `health_check_interval`;
  the run loop catches `RedisError`, counts it as `wa_redis_errors_total`, and
  waits `REDIS_ERROR_BACKOFF_SECONDS` instead of exiting. Non-Redis exceptions
  still propagate, so a genuine bug is not swallowed.
- **The API inherited a worker-only tuning knob.** One shared client meant the
  API's read timeout was derived from `BLOCK_MS`, despite the API never issuing
  a blocking command. Raising `BLOCK_MS` to reduce idle polling would silently
  stretch how long a stalled Redis could hold a webhook request — long enough to
  fill uvicorn's request slots and starve `/health` — with no visible connection
  between the two settings. `get_blocking_redis()` is now a separate pool used
  only by `read_new()`; ordinary commands use `REDIS_SOCKET_TIMEOUT_SECONDS`.
  The API process never builds the blocking pool at all.
- `redis` 5 typed `ping()` as `Awaitable[bool] | bool`, since one class backed
  both the sync and async clients, and that union needed narrowing at the call
  site. `redis` 8 types the async client's `ping()` as awaitable in its own
  right, so the narrowing cast has been dropped as redundant.
- Tightened two spots `redis` 8's stricter stubs exposed: `enqueue_event()` now
  states the decoded-response contract for the entry id in one place, and
  `peek_dlq()` no longer assumes every stream field is a `str`.

## [1.0.0] — 2026-08-08

The hardening release. `0.1.0` was a working proof-of-concept; this is the
version intended to be run in front of real customer messages.

### Fixed at the tag

- The test suite could not be imported under bare `pytest` — only under
  `python -m pytest`, which happens to put the working directory on `sys.path`.
  CI and `make test` both failed at conftest import with
  `ModuleNotFoundError: No module named 'app'`. Fixed with `pythonpath = ["."]`.

### Added

**Security**

- `X-Hub-Signature-256` verification: HMAC-SHA256 of the raw request body keyed
  with `WHATSAPP_APP_SECRET`, compared with `hmac.compare_digest`. Requests with
  a missing, malformed, or mismatched signature are rejected with `403` before
  any parsing or queueing.
- Per-IP rate limiting on `POST /webhook` via `slowapi` (`WEBHOOK_RATE_LIMIT`,
  default `120/minute`).
- Fail-fast configuration validation (`Settings.validate()`), run from both the
  API lifespan and the worker entrypoint. Startup aborts when
  `WHATSAPP_VERIFY_TOKEN` or `WHATSAPP_APP_SECRET` is unset or still a known
  placeholder, so an insecure deploy fails loudly at boot. It also refuses to
  start when `CLAIM_MIN_IDLE_MS` does not exceed
  `DOWNSTREAM_TIMEOUT_SECONDS + RETRY_BACKOFF_MAX_SECONDS`, since a reclaim
  firing during a live attempt delivers the same event twice.
- `FORWARDED_ALLOW_IPS` set to `"*"` for the API in `docker-compose.prod.yml`.
  Without it uvicorn only trusts `X-Forwarded-For` from loopback, so Caddy's
  header was discarded and every client shared a single rate-limit bucket keyed
  on Caddy's container address. Safe at `"*"` there specifically because the API
  publishes no host port and the Caddyfile overwrites `X-Forwarded-For` with the
  true remote address rather than appending to it.
- `REQUIRE_SIGNATURE` escape hatch for local development before an app secret
  exists. Defaults to `true`.
- `SECURITY.md` with a threat model, mitigations, an explicit out-of-scope list,
  a pre-launch checklist, and a disclosure process.

**Reliability**

- Reclaim routine using `XAUTOCLAIM`: entries left pending by a consumer that
  died mid-delivery are adopted by a live worker after `CLAIM_MIN_IDLE_MS`.
- Delivery-side idempotency with a two-state claim protocol on
  `wa:delivered:<message_id>`. `begin_delivery()` returns `WON`,
  `ALREADY_DELIVERED`, or `IN_PROGRESS`; `complete_delivery()` and
  `abandon_delivery()` settle it. The short-lived in-flight claim is taken
  *before* the downstream POST and upgraded to a long-lived completed marker
  *before* the `XACK`, so a crash between a confirmed POST and its ack cannot
  produce a duplicate downstream, while a crash *during* the POST is retried.
- Derived in-flight claim TTL (`Settings.claim_ttl_seconds()`), computed from
  `DOWNSTREAM_TIMEOUT_SECONDS + RETRY_BACKOFF_MAX_SECONDS` rather than
  configured separately, so the claim always outlasts one full attempt
  (backoff sleep included) and the two cannot drift apart.
- `DLQ_MAXLEN` (default `10000`), capping the dead-letter stream.
- Graceful shutdown on `SIGTERM`/`SIGINT`: the in-flight batch finishes and acks
  instead of being yanked mid-delivery. `stop_grace_period: 30s` on the worker
  in both compose files.
- Dead-letter queue (`wa:events:dlq`) for events that exhaust `MAX_RETRIES`,
  with `peek_dlq()` for inspection.
- Backoff ceiling (`RETRY_BACKOFF_MAX_SECONDS`) and an explicit downstream
  timeout (`DOWNSTREAM_TIMEOUT_SECONDS`).
- Concurrent batch processing, so one event sleeping out its backoff does not
  stall the events behind it. Gathered exceptions are logged rather than
  silently discarded.

**Observability**

- Structured JSON logging (`app/logging_config.py`) with a `console` format for
  local development. uvicorn's handlers are cleared so its records share the
  same shape.
- Correlation IDs stored in a `ContextVar`, generated at ingest or honoured from
  an inbound `X-Correlation-Id`, echoed on the response, carried on the queued
  event, and forwarded as `X-Correlation-Id` on the downstream POST.
- Prometheus `/metrics` endpoint: `wa_webhook_requests_total`,
  `wa_events_received_total`, `wa_events_queued_total`,
  `wa_events_duplicate_total`, `wa_deliveries_total`,
  `wa_delivery_duration_seconds`, `wa_retries_total`, `wa_dead_lettered_total`,
  `wa_reclaimed_total`, and the `wa_queue_depth` / `wa_queue_in_flight` /
  `wa_queue_dead_lettered` / `wa_redis_up` gauges.
- `/stats` endpoint reporting queued depth, in-flight count, DLQ size, and the
  dedup hit rate. `queued` and `in_flight` are disjoint: `queued` is `XLEN`
  minus the consumer group's pending count, so an entry is never reported under
  both at once.

**Testing**

- Test suite of 175 tests covering security, the normalizer, the queue, the
  worker, observability, and the API routes, at 97% statement/branch coverage of
  `app/`. Runs entirely in-process against `fakeredis` — no external services,
  no credentials, no network.
- Worker tests for both crash windows — after a confirmed POST and during the
  POST — plus a live claim blocking a concurrent reclaimer, a malformed
  downstream URL being retried rather than stranding the entry, retry/backoff
  growth and capping, DLQ placement, and graceful shutdown.
- Queue tests for the three `begin_delivery()` outcomes, the in-flight claim
  expiring while the completed marker outlives it, stream and DLQ trimming, and
  `queued`/`in_flight` not double-counting.
- Coverage reporting wired into CI, with a per-Python-version summary and a
  `coverage.xml` artifact.

**Deployment**

- `docker-compose.prod.yml`: Caddy terminating TLS with automatic Let's Encrypt
  certificates, segmented `edge` / `internal` / `egress` networks, per-service
  CPU and memory limits, restart policies, and capped json-file logging.
- `Caddyfile` with HSTS and hardening headers, and `/metrics` restricted to
  private-range clients.
- `docs/DEPLOYMENT.md` covering both supported paths — local ngrok tunnel and
  production domain with Caddy — plus DNS, certificate renewal, secret
  rotation, and operational notes.
- `.dockerignore`.

**Tooling**

- `Makefile` (`install`, `hooks`, `dev`, `worker`, `test`, `lint`, `typecheck`,
  `fmt`, `up`, `down`, `restart`, `ps`, `logs`, `clean`).
- `ruff`, `black`, and `mypy` configured in `pyproject.toml` and wired into
  `.pre-commit-config.yaml`.
- GitHub Actions CI with separate lint, type-check, test (3.11 and 3.12), and
  Docker build jobs.
- `CONTRIBUTING.md`, issue templates, a PR template, and Dependabot.
- This changelog.

### Changed

- **BREAKING — queue transport.** The plain Redis list (`RPUSH`/`BLPOP` on
  `wa:events:queue`) was replaced by a Redis Stream with a consumer group
  (`XADD` → `XREADGROUP` → `XACK`, key `wa:events:stream`, group
  `wa-normalizer`). The list gave at-most-once delivery: `BLPOP` removed the
  entry before any work happened, so a worker crash lost the event silently.
  The stream gives at-least-once with acknowledgement and a recoverable pending
  list.

  *Migration:* the new worker does not read `wa:events:queue`. Drain it with the
  0.1.0 worker before upgrading, or re-publish its contents into
  `wa:events:stream` manually; anything left in the list after the upgrade is
  never delivered.

- `NormalizedEvent` gained `received_at`, `retry_count`, and `correlation_id`.
- `/health` now pings Redis and returns `503` with
  `{"status": "degraded", "redis": "unreachable"}` when it is unreachable,
  instead of always returning a static `200`. Compose healthchecks and
  `depends_on: service_healthy` chains depend on this.
- `POST /webhook` returns a delivery summary (`received` / `queued` /
  `duplicates`) rather than a bare acknowledgement, and returns `503` with
  `{"detail": "Storage unavailable, retry this delivery"}` when Redis cannot
  accept the write. The 5xx is deliberate: a non-2xx makes Meta redeliver,
  whereas a `200` would acknowledge an event nothing durably stored. Counted as
  `wa_webhook_requests_total{outcome="storage_error"}` — alert on it, since
  sustained 5xx eventually makes Meta disable the webhook.
- `CLAIM_MIN_IDLE_MS` default raised from `60000` to `120000`, comfortably clear
  of the 70s worst-case attempt time that `validate()` now enforces.
- `wa_deliveries_total` gained the `skipped_in_progress` outcome, distinguishing
  "another worker is mid-attempt" from `skipped_duplicate`.
- `print()` calls replaced with structured logging carrying `message_id`,
  `event_type`, and `retry_count`.
- Redis now runs with `appendonly yes` and `appendfsync everysec` against a
  named volume in both compose files, since it holds durable events rather than
  a transient list. Production adds `maxmemory-policy noeviction` so writes fail
  loudly instead of quietly evicting undelivered events.
- `Dockerfile` rebuilt as a multi-stage image running as a non-root user
  (uid/gid 10001), with application code owned by root and a stdlib-only
  healthcheck.
- The local compose file binds published ports to `127.0.0.1` only.
- `.env.example` expanded and annotated.

### Fixed

Found by a pre-release security and correctness audit of the hardening work
above, and fixed before any of it shipped.

- **HMAC comparison crashed on non-ASCII input.** `hmac.compare_digest` raises
  `TypeError` on `str` containing non-ASCII, uvicorn decodes header values as
  latin-1, and obs-text (`0x80`–`0xFF`) is legal in HTTP — so any anonymous
  caller could turn a request into an unhandled `500` *before* authentication.
  Both comparisons now encode to bytes first; such a request gets the `403` it
  always should have.
- **Silent event loss when a worker died mid-POST.** The old single-marker claim
  could not distinguish "in flight" from "confirmed delivered", so a reclaiming
  worker saw the dead worker's claim, assumed the event had landed, and acked it
  without ever sending it. The short-TTL claim / long-TTL completed marker split
  makes an unconfirmed attempt retryable and a confirmed one non-repeatable.
- **Retry amplification.** The delivery claim was released before the backoff
  sleep, so a reclaiming worker could start a parallel attempt on an entry the
  first worker was still holding — doubling the copies in flight on every retry
  round. The claim is now held for the whole attempt, sleep included.
- **`httpx.InvalidURL` escaped the delivery error handler.** It is not a
  subclass of `httpx.HTTPError`, so a malformed `DOWNSTREAM_WEBHOOK_URL` bypassed
  the retry path entirely and stranded every entry it touched. The handler now
  catches broadly and treats transport failures of any kind as retryable.
- **Unbounded DLQ.** The dead-letter stream had no cap. With
  `maxmemory-policy noeviction` in production, a downstream that stayed down long
  enough would fill Redis, at which point `XADD` on the *ingest* path starts
  failing and deliveries are refused at the door. Capped at `DLQ_MAXLEN`.
- **The downstream URL was logged in full at worker startup.** For the
  integrations this service is usually pointed at — Slack incoming webhooks, n8n
  webhook nodes — the full URL *is* the credential, and startup logs are retained
  by the container log driver. Only the origin (`https://host/...`) is logged now.
- **Redis failure during ingest raised an unhandled `500`.** It is now a
  deliberate `503`, which is what makes Meta redeliver the event.
- **A single shared rate-limit bucket behind the proxy.** Without
  `FORWARDED_ALLOW_IPS`, uvicorn discarded Caddy's `X-Forwarded-For` and keyed
  every request on Caddy's container address, so 120 junk requests a minute from
  anyone would 429 Meta's real deliveries.
- **`/stats` double-counted in-flight entries.** `queued` reported raw `XLEN`,
  which includes the consumer group's pending entries, so the same entry appeared
  under both `queued` and `in_flight`. `queued` is now `XLEN` minus pending, and
  the two are disjoint. Same fix for the `wa_queue_depth` gauge.

### Security

- Unsigned and mis-signed webhook deliveries are now rejected rather than
  processed. Prior to 1.0.0, anyone who discovered the webhook URL could inject
  arbitrary events.
- All secret comparisons (signature and verify token) are constant-time **and
  operate on bytes**, closing a pre-authentication denial-of-service: non-ASCII
  header bytes previously produced an unhandled `500`, and sustained 5xx is
  exactly what makes Meta back off and eventually disable the webhook.
- The signature is computed over the exact bytes read off the wire, before JSON
  parsing, closing the re-serialization bypass.
- Per-IP rate limiting actually keys on the client IP behind the bundled reverse
  proxy (`FORWARDED_ALLOW_IPS`), instead of collapsing every caller into one
  bucket keyed on the proxy's address.
- The downstream webhook URL is redacted to its origin in logs, since for Slack
  and n8n endpoints the full URL is itself the credential.
- Both Redis streams are capped (`STREAM_MAXLEN`, `DLQ_MAXLEN`), so a persistently
  failing downstream cannot exhaust the memory the ingest path depends on.
- Redis publishes no host port in the production stack and sits on an internal
  network the TLS-terminating container cannot reach.

## [0.1.0]

Initial proof-of-concept.

### Added

- FastAPI service with `GET /webhook` (Meta verification handshake),
  `POST /webhook` (ingest), `GET /health`, and `GET /stats`.
- Normalizer flattening the WhatsApp Cloud API `entry → changes → value`
  structure into a single `NormalizedEvent` shape for both inbound messages and
  status updates, covering text, button, interactive button/list replies, media
  captions, and locations.
- Deduplication of redelivered events via an atomic `SET NX EX` claim on
  `message_id` in Redis.
- Redis list queue (`RPUSH`/`BLPOP`) and a delivery worker that POSTs events to
  `DOWNSTREAM_WEBHOOK_URL` with retries.
- Docker Compose stack (API, worker, Redis) and unit tests for the normalizer.

[Unreleased]: https://github.com/harinazrekar/whatsapp-normalizer/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/harinazrekar/whatsapp-normalizer/releases/tag/v1.0.0
[0.1.0]: https://github.com/harinazrekar/whatsapp-normalizer/releases/tag/v0.1.0
