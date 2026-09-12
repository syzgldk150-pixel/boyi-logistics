"""Real provider record/address shapes must survive public result validation."""
import base64

import pytest

from agent.automation_plugins.connector_registry import (
    ConnectorSensitiveDataDenied,
    _reject_sensitive_result,
)


def check(value, *, bindings=()):
    _reject_sensitive_result(value, sensitive_identifiers=bindings, reject_wrapped_identifiers=True)


def test_provider_id_and_slash_address_are_preserved():
    identity = base64.b64encode(b"\xff" * 16).decode()
    address = "/湖南省邵阳市测试地址"
    value = {"rows": [{"external_id": identity, "recipient_address": address}]}
    check(value)
    check({"valueRange": {"values": [[address]]}})
    assert value["rows"][0] == {"external_id": identity, "recipient_address": address}


@pytest.mark.parametrize("value", [
    {"external_id": "/var/tmp/result.json"},
    {"external_id": "https://internal.invalid"},
    {"recipient_address": "/var/tmp/result.json"},
    {"recipient_address": "/湖南省测试地址 /etc/passwd"},
    {"recipient_address": "/湖南省测试地址 https://internal.invalid"},
    {"recipient_address": "/湖南省测试地址 password=fixture-secret"},
    {"value": base64.b64encode(b"\xff" * 16).decode()},
    {"value": "/湖南省邵阳市测试地址"},
])
def test_business_fields_do_not_allow_other_targets_or_secrets(value):
    with pytest.raises(ConnectorSensitiveDataDenied):
        check(value)


def test_record_id_matching_a_bound_identity_is_still_denied():
    identity = base64.b64encode(b"\xff" * 16).decode()
    with pytest.raises(ConnectorSensitiveDataDenied):
        check({"external_id": identity}, bindings=(identity,))
