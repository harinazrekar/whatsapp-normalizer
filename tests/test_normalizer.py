import pytest

from app.normalizer import extract_events

TEXT_MESSAGE_PAYLOAD = {
    "entry": [
        {
            "changes": [
                {
                    "field": "messages",
                    "value": {
                        "metadata": {"display_phone_number": "911234567890"},
                        "messages": [
                            {
                                "id": "wamid.ABC123",
                                "from": "919876543210",
                                "timestamp": "1712345678",
                                "type": "text",
                                "text": {"body": "Hello there"},
                            }
                        ],
                    },
                }
            ]
        }
    ]
}

BUTTON_REPLY_PAYLOAD = {
    "entry": [
        {
            "changes": [
                {
                    "value": {
                        "metadata": {"display_phone_number": "911234567890"},
                        "messages": [
                            {
                                "id": "wamid.BTN1",
                                "from": "919876543210",
                                "timestamp": "1712345680",
                                "type": "interactive",
                                "interactive": {
                                    "type": "button_reply",
                                    "button_reply": {"id": "yes", "title": "Yes please"},
                                },
                            }
                        ],
                    }
                }
            ]
        }
    ]
}

STATUS_PAYLOAD = {
    "entry": [
        {
            "changes": [
                {
                    "value": {
                        "metadata": {"display_phone_number": "911234567890"},
                        "statuses": [
                            {
                                "id": "wamid.ABC123",
                                "recipient_id": "919876543210",
                                "timestamp": "1712345700",
                                "status": "delivered",
                            }
                        ],
                    }
                }
            ]
        }
    ]
}


def test_extracts_text_message():
    events = extract_events(TEXT_MESSAGE_PAYLOAD)
    assert len(events) == 1
    event = events[0]
    assert event.event_type == "message"
    assert event.message_type == "text"
    assert event.text == "Hello there"
    assert event.from_number == "919876543210"
    assert event.to_number == "911234567890"
    assert event.message_id == "wamid.ABC123"


def test_extracts_interactive_button_reply():
    events = extract_events(BUTTON_REPLY_PAYLOAD)
    assert len(events) == 1
    assert events[0].text == "Yes please"


def test_extracts_status_update():
    events = extract_events(STATUS_PAYLOAD)
    assert len(events) == 1
    event = events[0]
    assert event.event_type == "status"
    assert event.status == "delivered"
    assert event.message_id == "wamid.ABC123"


def test_handles_empty_payload():
    assert extract_events({}) == []


def test_skips_messages_without_id():
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [{"type": "text", "text": {"body": "no id here"}}],
                        }
                    }
                ]
            }
        ]
    }
    assert extract_events(payload) == []


def _message_payload(message: dict) -> dict:
    """Wrap a single message dict in the entry -> changes -> value envelope."""
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"display_phone_number": "911234567890"},
                            "messages": [message],
                        }
                    }
                ]
            }
        ]
    }


def _text_of(message: dict):
    events = extract_events(_message_payload(message))
    assert len(events) == 1
    return events[0].text


# ---------------------------------------------------------------------------
# _extract_text: one branch per WhatsApp message type
# ---------------------------------------------------------------------------


def test_extracts_text_from_quick_reply_button():
    text = _text_of(
        {
            "id": "wamid.BTN_TEMPLATE",
            "from": "919876543210",
            "type": "button",
            "button": {"payload": "confirm-booking", "text": "Confirm booking"},
        }
    )
    assert text == "Confirm booking"


def test_button_message_without_text_yields_none():
    text = _text_of(
        {
            "id": "wamid.BTN_NO_TEXT",
            "from": "919876543210",
            "type": "button",
            "button": {"payload": "confirm-booking"},
        }
    )
    assert text is None


def test_extracts_interactive_list_reply_title():
    text = _text_of(
        {
            "id": "wamid.LIST1",
            "from": "919876543210",
            "type": "interactive",
            "interactive": {
                "type": "list_reply",
                "list_reply": {
                    "id": "plan-pro",
                    "title": "Pro plan",
                    "description": "Everything in Starter, plus priority support",
                },
            },
        }
    )
    assert text == "Pro plan"


def test_interactive_with_unknown_sub_shape_yields_none():
    """Meta adds new interactive reply kinds over time; unknown ones must not raise."""
    text = _text_of(
        {
            "id": "wamid.NFM1",
            "from": "919876543210",
            "type": "interactive",
            "interactive": {"type": "nfm_reply", "nfm_reply": {"response_json": "{}"}},
        }
    )
    assert text is None


@pytest.mark.parametrize("media_type", ["image", "video", "document", "audio", "sticker"])
def test_extracts_caption_from_media_message(media_type):
    text = _text_of(
        {
            "id": f"wamid.MEDIA_{media_type.upper()}",
            "from": "919876543210",
            "type": media_type,
            media_type: {
                "id": "media-123",
                "mime_type": "application/octet-stream",
                "caption": "Here is the invoice",
            },
        }
    )
    assert text == "Here is the invoice"


@pytest.mark.parametrize("media_type", ["image", "video", "document", "audio", "sticker"])
def test_media_message_without_caption_yields_none(media_type):
    text = _text_of(
        {
            "id": f"wamid.NOCAP_{media_type.upper()}",
            "from": "919876543210",
            "type": media_type,
            media_type: {"id": "media-123", "mime_type": "application/octet-stream"},
        }
    )
    assert text is None


def test_extracts_location_as_lat_lng_pair():
    text = _text_of(
        {
            "id": "wamid.LOC1",
            "from": "919876543210",
            "type": "location",
            "location": {"latitude": 19.076, "longitude": 72.8777, "name": "Mumbai"},
        }
    )
    assert text == "19.076,72.8777"


@pytest.mark.parametrize(
    "location",
    [
        {"longitude": 72.8777},
        {"latitude": 19.076},
        {},
    ],
    ids=["missing_latitude", "missing_longitude", "empty"],
)
def test_partial_location_yields_none(location):
    text = _text_of(
        {
            "id": "wamid.LOC_PARTIAL",
            "from": "919876543210",
            "type": "location",
            "location": location,
        }
    )
    assert text is None


def test_unknown_message_type_yields_none_but_still_emits_event():
    events = extract_events(
        _message_payload(
            {
                "id": "wamid.UNSUPPORTED",
                "from": "919876543210",
                "type": "order",
                "order": {"catalog_id": "cat-1"},
            }
        )
    )
    assert len(events) == 1
    assert events[0].message_type == "order"
    assert events[0].text is None


def test_message_with_no_type_yields_none_text():
    events = extract_events(_message_payload({"id": "wamid.NOTYPE", "from": "919876543210"}))
    assert len(events) == 1
    assert events[0].message_type is None
    assert events[0].text is None


# ---------------------------------------------------------------------------
# _safe_timestamp
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw_timestamp", "expected"),
    [
        ("1712345678", 1712345678),
        (1712345678, 1712345678),
        (None, None),
        ("not-a-timestamp", None),
        ("", None),
        ({"seconds": 1712345678}, None),
    ],
    ids=["numeric_string", "integer", "missing", "garbage_string", "empty_string", "wrong_type"],
)
def test_timestamp_is_coerced_or_dropped(raw_timestamp, expected):
    message = {"id": "wamid.TS", "from": "919876543210", "type": "text", "text": {"body": "hi"}}
    if raw_timestamp is not None:
        message["timestamp"] = raw_timestamp
    events = extract_events(_message_payload(message))
    assert events[0].timestamp == expected


# ---------------------------------------------------------------------------
# extract_events: structural edge cases
# ---------------------------------------------------------------------------


MULTI_ENTRY_PAYLOAD = {
    "entry": [
        {
            "changes": [
                {
                    "value": {
                        "metadata": {"display_phone_number": "911111111111"},
                        "messages": [
                            {
                                "id": "wamid.E1C1M1",
                                "from": "919000000001",
                                "timestamp": "1712345601",
                                "type": "text",
                                "text": {"body": "first change"},
                            }
                        ],
                    }
                },
                {
                    "value": {
                        "metadata": {"display_phone_number": "911111111111"},
                        "messages": [
                            {
                                "id": "wamid.E1C2M1",
                                "from": "919000000002",
                                "timestamp": "1712345602",
                                "type": "text",
                                "text": {"body": "second change"},
                            }
                        ],
                    }
                },
            ]
        },
        {
            "changes": [
                {
                    "value": {
                        "metadata": {"display_phone_number": "922222222222"},
                        "messages": [
                            {
                                "id": "wamid.E2C1M1",
                                "from": "919000000003",
                                "timestamp": "1712345603",
                                "type": "text",
                                "text": {"body": "second entry"},
                            }
                        ],
                    }
                }
            ]
        },
    ]
}

MIXED_MESSAGES_AND_STATUSES_PAYLOAD = {
    "entry": [
        {
            "changes": [
                {
                    "value": {
                        "metadata": {"display_phone_number": "911234567890"},
                        "messages": [
                            {
                                "id": "wamid.MSG_A",
                                "from": "919876543210",
                                "timestamp": "1712345678",
                                "type": "text",
                                "text": {"body": "inbound"},
                            },
                            {
                                "id": "wamid.MSG_B",
                                "from": "919876543211",
                                "timestamp": "1712345679",
                                "type": "text",
                                "text": {"body": "also inbound"},
                            },
                        ],
                        "statuses": [
                            {
                                "id": "wamid.STATUS_A",
                                "recipient_id": "919876543210",
                                "timestamp": "1712345700",
                                "status": "read",
                            }
                        ],
                    }
                }
            ]
        }
    ]
}


def test_flattens_multiple_entries_and_changes_in_document_order():
    events = extract_events(MULTI_ENTRY_PAYLOAD)
    assert [e.message_id for e in events] == ["wamid.E1C1M1", "wamid.E1C2M1", "wamid.E2C1M1"]
    assert [e.to_number for e in events] == ["911111111111", "911111111111", "922222222222"]


def test_messages_are_emitted_before_statuses_within_a_change():
    events = extract_events(MIXED_MESSAGES_AND_STATUSES_PAYLOAD)
    assert len(events) == 3
    assert [e.event_type for e in events] == ["message", "message", "status"]
    assert [e.message_id for e in events] == ["wamid.MSG_A", "wamid.MSG_B", "wamid.STATUS_A"]


def test_missing_metadata_leaves_to_number_none():
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "id": "wamid.NOMETA",
                                    "from": "919876543210",
                                    "timestamp": "1712345678",
                                    "type": "text",
                                    "text": {"body": "hello"},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    events = extract_events(payload)
    assert len(events) == 1
    assert events[0].to_number is None


def test_skips_statuses_without_id():
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "statuses": [
                                {"recipient_id": "919876543210", "status": "delivered"},
                                {
                                    "id": "wamid.KEEP",
                                    "recipient_id": "919876543210",
                                    "status": "sent",
                                },
                            ]
                        }
                    }
                ]
            }
        ]
    }
    events = extract_events(payload)
    assert [e.message_id for e in events] == ["wamid.KEEP"]


@pytest.mark.parametrize(
    "payload",
    [
        {"entry": []},
        {"entry": [{}]},
        {"entry": [{"changes": []}]},
        {"entry": [{"changes": [{}]}]},
        {"entry": [{"changes": [{"value": {}}]}]},
        {"entry": [{"changes": [{"value": {"messages": [], "statuses": []}}]}]},
        {"object": "whatsapp_business_account"},
    ],
    ids=[
        "no_entries",
        "entry_without_changes",
        "empty_changes",
        "change_without_value",
        "empty_value",
        "empty_message_and_status_lists",
        "unrelated_payload",
    ],
)
def test_structurally_empty_payloads_produce_no_events(payload):
    assert extract_events(payload) == []


def test_message_event_populates_every_field_and_keeps_raw_fragment():
    message = {
        "id": "wamid.FULL",
        "from": "919876543210",
        "timestamp": "1712345678",
        "type": "text",
        "text": {"body": "Hello there"},
        "context": {"from": "911234567890", "id": "wamid.PREVIOUS"},
    }
    events = extract_events(_message_payload(message))
    assert len(events) == 1
    event = events[0]
    assert event.message_id == "wamid.FULL"
    assert event.event_type == "message"
    assert event.message_type == "text"
    assert event.text == "Hello there"
    assert event.from_number == "919876543210"
    assert event.to_number == "911234567890"
    assert event.timestamp == 1712345678
    assert event.status is None
    assert event.retry_count == 0
    assert event.correlation_id == ""
    assert event.raw == message


def test_status_event_populates_every_field_and_keeps_raw_fragment():
    events = extract_events(STATUS_PAYLOAD)
    assert len(events) == 1
    event = events[0]
    assert event.message_id == "wamid.ABC123"
    assert event.event_type == "status"
    assert event.status == "delivered"
    assert event.message_type is None
    assert event.text is None
    assert event.from_number == "919876543210"
    assert event.to_number == "911234567890"
    assert event.timestamp == 1712345700
    assert event.raw == STATUS_PAYLOAD["entry"][0]["changes"][0]["value"]["statuses"][0]


# --- hostile / null-laden payloads ------------------------------------------
#
# `.get(key, {})` only substitutes a default when the key is ABSENT. Meta sends
# keys present with an explicit null, which sails past the default and raises on
# the next access. Every one of these used to be a 500 -- and a 5xx is precisely
# what makes Meta throttle and eventually disable the webhook.

NULL_SHAPES = [
    pytest.param({"entry": None}, id="entry-null"),
    pytest.param({"entry": "not-a-list"}, id="entry-not-a-list"),
    pytest.param({"entry": ["a string"]}, id="entry-element-not-a-dict"),
    pytest.param({"entry": [None]}, id="entry-element-null"),
    pytest.param({"entry": [{"changes": None}]}, id="changes-null"),
    pytest.param({"entry": [{"changes": [None]}]}, id="change-element-null"),
    pytest.param({"entry": [{"changes": [{"value": None}]}]}, id="value-null"),
    pytest.param(
        {"entry": [{"changes": [{"value": {"metadata": None, "messages": []}}]}]},
        id="metadata-null",
    ),
    pytest.param({"entry": [{"changes": [{"value": {"messages": None}}]}]}, id="messages-null"),
    pytest.param({"entry": [{"changes": [{"value": {"statuses": None}}]}]}, id="statuses-null"),
    pytest.param(
        {"entry": [{"changes": [{"value": {"messages": ["nope"]}}]}]},
        id="message-not-a-dict",
    ),
]


@pytest.mark.parametrize("payload", NULL_SHAPES)
def test_null_laden_payloads_yield_no_events_instead_of_raising(payload):
    assert extract_events(payload) == []


@pytest.mark.parametrize(
    "message",
    [
        pytest.param({"id": "wamid.N1", "type": "text", "text": None}, id="text"),
        pytest.param({"id": "wamid.N2", "type": "button", "button": None}, id="button"),
        pytest.param(
            {"id": "wamid.N3", "type": "interactive", "interactive": None},
            id="interactive",
        ),
        pytest.param(
            {"id": "wamid.N4", "type": "interactive", "interactive": {"button_reply": None}},
            id="button-reply-null",
        ),
        pytest.param(
            {"id": "wamid.N5", "type": "interactive", "interactive": {"list_reply": None}},
            id="list-reply-null",
        ),
        pytest.param({"id": "wamid.N6", "type": "image", "image": None}, id="media"),
        pytest.param({"id": "wamid.N7", "type": "location", "location": None}, id="location"),
    ],
)
def test_null_message_bodies_normalize_to_none_text(message):
    """The event must still be produced -- just without text."""
    payload = {"entry": [{"changes": [{"value": {"messages": [message]}}]}]}

    events = extract_events(payload)

    assert len(events) == 1
    assert events[0].text is None
    assert events[0].message_id == message["id"]


def test_valid_events_survive_alongside_malformed_siblings():
    """One bad entry must not discard the good ones next to it."""
    payload = {
        "entry": [
            None,
            {"changes": [{"value": None}]},
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"display_phone_number": "911234567890"},
                            "messages": [
                                {
                                    "id": "wamid.SURVIVOR",
                                    "from": "919876543210",
                                    "type": "text",
                                    "text": {"body": "still here"},
                                }
                            ],
                        }
                    }
                ]
            },
        ]
    }

    events = extract_events(payload)

    assert len(events) == 1
    assert events[0].text == "still here"
