import os

# Values that mean "the developer never actually set this". Treated as unset so
# the service refuses to boot rather than running with a guessable secret.
PLACEHOLDER_VALUES = {"", "change-me", "changeme", "your-secret-here", "todo"}


def _is_placeholder(value: str) -> bool:
    return value.strip().lower() in PLACEHOLDER_VALUES


class Settings:
    # Token you choose yourself and enter into the Meta App Dashboard webhook config.
    WHATSAPP_VERIFY_TOKEN: str = os.getenv("WHATSAPP_VERIFY_TOKEN", "change-me")

    # App Secret from the Meta App Dashboard (App Settings -> Basic). Used to verify
    # the X-Hub-Signature-256 header, which is the only thing actually proving a
    # webhook delivery came from Meta and not from someone who guessed your URL.
    WHATSAPP_APP_SECRET: str = os.getenv("WHATSAPP_APP_SECRET", "")

    # Where normalized, deduped events get forwarded to. Your own backend, an
    # n8n webhook node, a Slack incoming webhook -- anything that accepts a POST.
    DOWNSTREAM_WEBHOOK_URL: str = os.getenv("DOWNSTREAM_WEBHOOK_URL", "")

    REDIS_URL: str = os.getenv("REDIS_URL", "redis://redis:6379/0")

    # How long a message_id is remembered for dedup purposes. WhatsApp Cloud API
    # frequently redelivers the same webhook; 24h comfortably covers that.
    DEDUP_TTL_SECONDS: int = int(os.getenv("DEDUP_TTL_SECONDS", "86400"))

    MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "5"))
    RETRY_BACKOFF_BASE: float = float(os.getenv("RETRY_BACKOFF_BASE", "2"))

    # slowapi-style limit string applied to POST /webhook. Meta's own delivery rate
    # is far below this; the limit exists to blunt replay storms and stray traffic.
    WEBHOOK_RATE_LIMIT: str = os.getenv("WEBHOOK_RATE_LIMIT", "120/minute")

    # Escape hatch for local development against a tunnel where you have no app
    # secret yet. Never enable this anywhere a real URL is reachable.
    REQUIRE_SIGNATURE: bool = os.getenv("REQUIRE_SIGNATURE", "true").lower() != "false"

    # --- Redis Streams ------------------------------------------------------
    # A stream + consumer group, not a plain list. A list pops an item and it is
    # gone; if the worker dies between BLPOP and delivery the event is lost with
    # no trace. A consumer group keeps the entry in a pending list until it is
    # explicitly XACKed, so a crash is recoverable.
    STREAM_KEY: str = os.getenv("STREAM_KEY", "wa:events:stream")
    CONSUMER_GROUP: str = os.getenv("CONSUMER_GROUP", "wa-normalizer")
    DLQ_KEY: str = os.getenv("DLQ_KEY", "wa:events:dlq")

    # Cap on stream length. XADD trims opportunistically (~) so delivered-and-
    # acked history doesn't grow without bound.
    STREAM_MAXLEN: int = int(os.getenv("STREAM_MAXLEN", "100000"))

    # How many entries a worker claims per read, and how long XREADGROUP blocks
    # when the stream is empty. The block is what makes SIGTERM feel responsive:
    # shutdown is noticed at most this long after it is requested.
    BATCH_SIZE: int = int(os.getenv("BATCH_SIZE", "10"))
    BLOCK_MS: int = int(os.getenv("BLOCK_MS", "5000"))

    # An entry pending this long without an ack is assumed to belong to a worker
    # that died, and is reclaimed by a live one. Must exceed the worst-case
    # attempt time (DOWNSTREAM_TIMEOUT_SECONDS + RETRY_BACKOFF_MAX_SECONDS =
    # 70s by default), which validate() enforces rather than trusting.
    CLAIM_MIN_IDLE_MS: int = int(os.getenv("CLAIM_MIN_IDLE_MS", "120000"))

    # Ceiling on exponential backoff so a long-dead downstream doesn't park a
    # worker slot for hours.
    RETRY_BACKOFF_MAX_SECONDS: float = float(os.getenv("RETRY_BACKOFF_MAX_SECONDS", "60"))

    DOWNSTREAM_TIMEOUT_SECONDS: float = float(os.getenv("DOWNSTREAM_TIMEOUT_SECONDS", "10"))

    # How long a successful downstream delivery is remembered, so a reclaimed or
    # replayed entry is not delivered twice.
    DELIVERED_TTL_SECONDS: int = int(os.getenv("DELIVERED_TTL_SECONDS", "86400"))

    # Cap on the dead-letter stream. Unbounded, it would eventually consume the
    # Redis memory the ingest path needs.
    DLQ_MAXLEN: int = int(os.getenv("DLQ_MAXLEN", "10000"))

    def claim_ttl_seconds(self) -> int:
        """
        Lifetime of an in-flight delivery claim.

        Must outlast one full attempt -- the POST timeout plus the backoff the
        worker sleeps through while still holding the entry -- or a second worker
        could reclaim the entry mid-attempt and deliver it a second time. Derived
        rather than configured so the two cannot drift apart.
        """
        return int(self.DOWNSTREAM_TIMEOUT_SECONDS + self.RETRY_BACKOFF_MAX_SECONDS) + 5

    def validate(self) -> None:
        """
        Fail fast at startup rather than silently accepting unsigned traffic.
        Called from the app lifespan and from the worker entrypoint.
        """
        problems = []

        if _is_placeholder(self.WHATSAPP_VERIFY_TOKEN):
            problems.append(
                "WHATSAPP_VERIFY_TOKEN is unset or still the placeholder value. "
                "Choose any random string and enter the same value in the Meta App Dashboard."
            )

        if self.REQUIRE_SIGNATURE and _is_placeholder(self.WHATSAPP_APP_SECRET):
            problems.append(
                "WHATSAPP_APP_SECRET is unset or still the placeholder value. "
                "Copy it from Meta App Dashboard -> App Settings -> Basic. "
                "Set REQUIRE_SIGNATURE=false only for local development."
            )

        # Reclaim must not fire while an attempt is legitimately still running,
        # or two workers process the same entry and the downstream sees it twice.
        # These three are independently tunable, so the relationship is checked
        # rather than left as a comment nobody reads.
        attempt_ceiling = self.DOWNSTREAM_TIMEOUT_SECONDS + self.RETRY_BACKOFF_MAX_SECONDS
        if attempt_ceiling >= self.CLAIM_MIN_IDLE_MS / 1000:
            problems.append(
                f"CLAIM_MIN_IDLE_MS ({self.CLAIM_MIN_IDLE_MS}ms) must exceed the worst-case "
                f"attempt time of {attempt_ceiling:.0f}s (DOWNSTREAM_TIMEOUT_SECONDS + "
                "RETRY_BACKOFF_MAX_SECONDS), or a live attempt will be reclaimed by "
                "another worker and delivered twice."
            )

        if problems:
            raise RuntimeError(
                "Refusing to start with an insecure configuration:\n  - " + "\n  - ".join(problems)
            )


settings = Settings()
