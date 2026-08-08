import time
from typing import Literal

from pydantic import BaseModel, Field


class NormalizedEvent(BaseModel):
    """
    A single flattened, predictable shape for every WhatsApp webhook event --
    whether it started life as an inbound message or a delivery status update.
    Consumers of this API only ever need to understand this one schema.
    """

    message_id: str
    event_type: Literal["message", "status"]

    # populated for event_type == "message"
    message_type: str | None = None  # text, image, button, interactive, location, ...
    text: str | None = None

    # populated for event_type == "status"
    status: str | None = None  # sent, delivered, read, failed

    from_number: str | None = None
    to_number: str | None = None
    timestamp: int | None = None

    raw: dict  # original WhatsApp payload fragment, kept for anything you need later
    received_at: float = Field(default_factory=time.time)
    retry_count: int = 0

    # Assigned at ingestion and carried through the queue to the downstream POST
    # (as X-Correlation-Id), so one event can be grepped across API and worker logs.
    correlation_id: str = ""
