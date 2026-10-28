"""Connector phone columns keep the exact value the source returned."""

from __future__ import annotations

import pytest

from agent.automation_plugins.connector_registry import (
    ConnectorSensitiveDataDenied,
    _reject_sensitive_result,
)


PHONE_FIELDS = (
    "recipient_phone",
    "sender_phone",
    "receiver_phone",
    "ACCEPT_MAN_PHONE",
    "SEND_MAN_PHONE",
    "SEND_MAN_TEL",
    "recipientPhone",
    "\u6536\u8d27\u7535\u8bdd",
    "\u5bc4\u4ef6\u624b\u673a",
    "\u6536\u4ef6\u4eba\u7535\u8bdd",
)


def _reject(value, field=None):
    _reject_sensitive_result(
        value,
        sensitive_identifiers=(),
        reject_wrapped_identifiers=False,
        business_field=field,
    )


@pytest.mark.parametrize("field", PHONE_FIELDS)
@pytest.mark.parametrize(
    "value",
    [
        "/13800000000",
        "13800000000/",
        "138//13900000000",
        "13800000000",
        "138/139/",
        "/",
        "",
        13800000000,
    ],
)
def test_phone_columns_keep_the_source_value(field, value):
    _reject(value, field)


@pytest.mark.parametrize("field", PHONE_FIELDS)
def test_phone_columns_accept_free_form_operator_text(field):
    _reject("/etc/passwd", field)
    _reject("\u89c1\u5907\u6ce8\uff1a//A/B", field)


@pytest.mark.parametrize(
    "field", ["recipient_address", "tracking_number", "remarks", "destination_station"]
)
def test_other_fields_still_reject_paths(field):
    with pytest.raises(ConnectorSensitiveDataDenied):
        _reject("/etc/passwd", field)


def test_phone_columns_inside_a_page_row_are_not_rejected():
    _reject(
        {
            "items": [
                {"tracking_number": "R00000000001", "recipient_phone": "/13800000000"},
                {"tracking_number": "R00000000002", "recipient_phone": "138/139"},
            ],
            "next_cursor": "",
            "pagination_complete": True,
        }
    )


def test_sensitive_keys_inside_a_phone_column_are_still_rejected():
    with pytest.raises(ConnectorSensitiveDataDenied):
        _reject({"password": "x"}, "recipient_phone")
