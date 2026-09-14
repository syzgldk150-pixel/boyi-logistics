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


def test_yunda_problem_title_punctuation_is_preserved():
    value = {"raw_fields": {"prob_title": "/", "prob_text": "隔离测试问题说明"}}
    check(value)
    assert value["raw_fields"]["prob_title"] == "/"


@pytest.mark.parametrize("value", [
    {"prob_title": "/etc/passwd"},
    {"prob_title": "https://private.invalid/attachment"},
    {"prob_title": "/ password=fixture-secret"},
    {"other_field": "/"},
])
def test_yunda_title_exception_does_not_allow_private_targets(value):
    with pytest.raises(ConnectorSensitiveDataDenied):
        check(value)


def test_yunda_title_matching_bound_identity_is_still_denied():
    with pytest.raises(ConnectorSensitiveDataDenied):
        check({"prob_title": "/"}, bindings=("/",))


@pytest.mark.parametrize("field", ["ACCEPT_MAN_PHONE", "SEND_MAN_PHONE", "收货电话", "寄件手机", "receiver_phone", "sender_phone"])
def test_ronghui_phone_slash_groups_are_preserved(field):
    # Native sending records may include repeated separators and short suffixes.
    value = {"rows": [{field: "123456789012//34/56"}]}
    check(value)
    assert value["rows"][0][field] == "123456789012//34/56"


@pytest.mark.parametrize("value", [
    {"ACCEPT_MAN_PHONE": "/123/456"},
    {"ACCEPT_MAN_PHONE": "123//etc/passwd"},
    {"ACCEPT_MAN_PHONE": "https://private.invalid/123"},
    {"收货电话": "123//456 password=fixture-secret"},
    {"receiver_phone": "127.0.0.1:8080/123"},
    {"other_field": "123456789012//34/56"},
])
def test_phone_separators_do_not_allow_targets_or_other_fields(value):
    with pytest.raises(ConnectorSensitiveDataDenied):
        check(value)


def test_phone_slash_groups_still_reject_bound_identity():
    with pytest.raises(ConnectorSensitiveDataDenied):
        check({"ACCEPT_MAN_PHONE": "123456789012//34/56"}, bindings=("123456789012",))
