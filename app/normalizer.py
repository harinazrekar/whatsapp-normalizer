from typing import Any

from .models import NormalizedEvent


def _as_dict(value: Any) -> dict[str, Any]:
    """
    Coerce to a dict, treating anything else -- including an explicit JSON null --
    as empty.

    `payload.get("key", {})` only defends against a *missing* key. Meta sends
    keys present with a null value, and `.get("key", {})` happily returns None
    for those, so the next `.get` raises. Every traversal below goes through
    this or _as_list for that reason.
    """
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _extract_text(message: dict) -> str | None:
    """Best-effort human-readable text for whatever message type came in."""
    msg_type = message.get("type")

    if msg_type == "text":
        return _as_dict(message.get("text")).get("body")

    if msg_type == "button":
        return _as_dict(message.get("button")).get("text")

    if msg_type == "interactive":
        interactive = _as_dict(message.get("interactive"))
        if "button_reply" in interactive:
            return _as_dict(interactive["button_reply"]).get("title")
        if "list_reply" in interactive:
            return _as_dict(interactive["list_reply"]).get("title")
        return None

    if msg_type in ("image", "video", "document", "audio", "sticker"):
        return _as_dict(message.get(msg_type)).get("caption")

    if msg_type == "location":
        loc = _as_dict(message.get("location"))
        lat, lng = loc.get("latitude"), loc.get("longitude")
        if lat is not None and lng is not None:
            return f"{lat},{lng}"
        return None

    return None


def _safe_timestamp(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_events(payload: dict) -> list[NormalizedEvent]:
    """
    Walk a WhatsApp Cloud API webhook payload (entry -> changes -> value) and
    return one NormalizedEvent per message and per status update found.
    Malformed or unexpected shapes are skipped rather than raised, since this
    endpoint must always return fast and never 500 back to Meta.
    """
    events: list[NormalizedEvent] = []

    for entry in _as_list(_as_dict(payload).get("entry")):
        for change in _as_list(_as_dict(entry).get("changes")):
            value = _as_dict(_as_dict(change).get("value"))
            to_number = _as_dict(value.get("metadata")).get("display_phone_number")

            for message in _as_list(value.get("messages")):
                message = _as_dict(message)
                message_id = message.get("id")
                if not message_id:
                    continue
                events.append(
                    NormalizedEvent(
                        message_id=message_id,
                        event_type="message",
                        message_type=message.get("type"),
                        from_number=message.get("from"),
                        to_number=to_number,
                        timestamp=_safe_timestamp(message.get("timestamp")),
                        text=_extract_text(message),
                        raw=message,
                    )
                )

            for status in _as_list(value.get("statuses")):
                status = _as_dict(status)
                status_id = status.get("id")
                if not status_id:
                    continue
                events.append(
                    NormalizedEvent(
                        message_id=status_id,
                        event_type="status",
                        from_number=status.get("recipient_id"),
                        to_number=to_number,
                        timestamp=_safe_timestamp(status.get("timestamp")),
                        status=status.get("status"),
                        raw=status,
                    )
                )

    return events
