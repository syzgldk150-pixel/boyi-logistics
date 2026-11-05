"""Real provider record/address shapes must survive public result validation."""
import base64

import pytest

from agent.automation_plugins.connector_registry import (
    ConnectorSensitiveDataDenied,
    _reject_sensitive_result,
    _result_validation_view,
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


def test_ronghui_attachment_out_guid_is_preserved():
    identity = base64.b64encode(b"\xff" * 16).decode()
    check({"details": [{}, {"attachments": [{"raw": {"OUT_GUID": identity}}]}]})
    with pytest.raises(ConnectorSensitiveDataDenied):
        check({"OUT_GUID": "/var/tmp/result.json"})


def test_record_id_matching_a_bound_identity_is_still_denied():
    identity = base64.b64encode(b"\xff" * 16).decode()
    with pytest.raises(ConnectorSensitiveDataDenied):
        check({"external_id": identity}, bindings=(identity,))


def test_yunda_problem_title_punctuation_is_preserved():
    value = {"raw_fields": {"prob_title": "/", "prob_text": "隔离测试问题说明"}}
    check(value)
    assert value["raw_fields"]["prob_title"] == "/"


def test_yunda_detail_user_field_placeholder_is_preserved():
    # Real Yunda problem details return udf013 == "/" for every record.
    value = {"details": {"udf013": "/", "prob_status": "处理中"}}
    check(value)
    assert value["details"]["udf013"] == "/"


@pytest.mark.parametrize("value", [
    {"prob_title": "/etc/passwd"},
    {"prob_title": "https://private.invalid/attachment"},
    {"prob_title": "/ password=fixture-secret"},
    {"other_field": "/"},
    {"udf013": "/etc/passwd"},
    {"udf13": "/"},
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


def test_phone_cell_exception_does_not_extend_to_other_fields():
    # Phone cells are opaque business data (agent/tests/test_connector_phone_columns.py);
    # the same text in any other field is still checked.
    with pytest.raises(ConnectorSensitiveDataDenied):
        check({"other_field": "123456789012//34/56"})


def test_arrival_list_remark_leading_slash_is_business_punctuation():
    # Real Ronghui arrival remarks start with a quantity/weight note, for
    # example "/3.00-10 Z105,200条", followed by a transfer code.
    value = {"items": [{"tracking_number": "R00024167009", "remarks": "/3.00-10 Z105,200条\n"}]}
    check(value)
    assert value["items"][0]["remarks"] == "/3.00-10 Z105,200条\n"
    check({"remarks": "/转邵阳站"})


@pytest.mark.parametrize("value", [
    {"remarks": "/var/tmp/result.json"},
    {"remarks": "/etc/passwd"},
    {"remarks": "/a/b"},
    {"remarks": "/1 https://internal.invalid"},
    {"remarks": "/1 password=fixture-secret"},
])
def test_arrival_remark_exception_keeps_other_targets_and_secrets_denied(value):
    with pytest.raises(ConnectorSensitiveDataDenied):
        check(value)


SHEET_HEADERS = [
    "运单编号", "货物名称", "包装类型", "派送方式", "件数", "回单号", "实际重量",
    "体积", "备注", "目的站点", "收件人", "收件电话", "收件地址", "结算重量",
    "体积重", "运费", "支付类型", "到付款", "累计到货件数",
]


def sheet_result(remark):
    row = ["R00000000001", "", "", "", "1", "", "", "", remark, "", "", "", "",
           "", "", "", "", "", "0"]
    return {"complete": True, "rows": [list(SHEET_HEADERS), row]}


@pytest.mark.parametrize("service", [
    "connector.boyi.self_pickup_source_sheet@1",
    "connector.boyi.split_pending_source_sheet@1",
])
@pytest.mark.parametrize("remark", ["/3.00-10 Z105,200条", "/转邵阳站", "/"])
def test_problem_source_sheet_remark_keeps_the_operator_leading_slash(service, remark):
    # Source sheets arrive as raw rows; the reviewed 备注 column semantics must
    # still apply, exactly as they do for the arrival-list page records.
    check(_result_validation_view(service, "read_rows", sheet_result(remark)))


@pytest.mark.parametrize("remark", [
    "/etc/passwd",
    "/var/tmp/result.json",
    "https://internal.invalid",
    "/1 password=fixture-secret",
])
def test_problem_source_sheet_remark_still_rejects_other_targets(remark):
    view = _result_validation_view(
        "connector.boyi.split_pending_source_sheet@1", "read_rows", sheet_result(remark)
    )
    with pytest.raises(ConnectorSensitiveDataDenied):
        check(view)
