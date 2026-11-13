"""Tokens, sender rules and the single file-name renderer of customer share links."""
from datetime import date

import pytest

from shared import receipt_share as rs

SECRET = "s" * 40


def test_token_is_stable_unguessable_shape_and_changes_on_reset():
    first = rs.share_token(SECRET, 7, 1)
    assert first == rs.share_token(SECRET, 7, 1) and rs.is_token_shape(first) and len(first) == 22
    assert rs.share_token(SECRET, 7, 2) != first != rs.share_token(SECRET, 8, 1)
    assert rs.share_token("t" * 40, 7, 1) != first
    assert len(rs.token_sha256(first)) == 64
    with pytest.raises(rs.ReceiptShareError):
        rs.share_token("short", 7, 1)
    assert not rs.is_token_shape("../../etc/passwd") and not rs.is_token_shape(first + "x")


def test_access_code_shape_and_keyed_digest():
    assert rs.normalize_access_code(" 4821 ") == "4821" and rs.normalize_access_code("") == ""
    for bad in ("123", "12345", "12 4", "12;4"):
        with pytest.raises(rs.ReceiptShareError):
            rs.normalize_access_code(bad)
    assert rs.access_code_sha256(SECRET, 1, "4821") != rs.access_code_sha256(SECRET, 2, "4821")


def test_image_kinds_keep_fixed_order_and_need_one():
    assert rs.normalize_image_kinds(["receipt", "sender_label"]) == ["sender_label", "receipt"]
    assert rs.image_kinds_text(["receipt", "sign"]) == "签收图·回单图"
    for bad in ([], ["other"], None, "sign"):
        with pytest.raises(rs.ReceiptShareError):
            rs.normalize_image_kinds(bad)


def _rules(mode="any", *conditions):
    return {"mode": mode, "conditions": [dict(zip(("field", "op", "value"), item)) for item in conditions]}


def test_sender_rules_build_parameterised_sql_and_match_rows_the_same_way():
    rules = _rules("any", ("sender_name", "eq", "勇胜"), ("sender_phone", "contains", "5186"))
    sql, params = rs.match_rules_sql(rules)
    assert sql == "(w.sender_name = %s OR w.sender_phone LIKE %s ESCAPE '!')" and params == ["勇胜", "%5186%"]
    assert rs.match_rules_match(rules, {"sender_name": "别人", "sender_phone": "07315186128"})
    assert not rs.match_rules_match(rules, {"sender_name": "勇胜二部", "sender_phone": ""})
    both = _rules("all", ("sender_name", "contains", "100%_货"), ("sender_address", "contains", "邵阳"))
    sql, params = rs.match_rules_sql(both, alias="b")
    assert " AND " in sql and params[0] == "%100!%!_货%"  # wildcards in the value stay literal


@pytest.mark.parametrize("bad", [
    {}, {"mode": "any", "conditions": []}, {"mode": "either", "conditions": [{"field": "sender_name", "op": "eq", "value": "勇胜"}]},
    {"mode": "any", "conditions": [{"field": "receiver_name", "op": "eq", "value": "勇胜"}]},
    {"mode": "any", "conditions": [{"field": "sender_name", "op": "regex", "value": "勇胜"}]},
    {"mode": "any", "conditions": [{"field": "sender_name", "op": "contains", "value": "勇"}]},
    {"mode": "any", "conditions": [{"field": "sender_name; DROP", "op": "eq", "value": "勇胜"}]},
    {"mode": "any", "conditions": [{"field": "sender_name", "op": "eq", "value": "勇胜", "extra": 1}]},
])
def test_invalid_sender_rules_are_rejected(bad):
    with pytest.raises(rs.ReceiptShareError):
        rs.normalize_match_rules(bad)


def test_file_names_follow_the_template_exactly():
    template = rs.DEFAULT_NAMING_TEMPLATE
    name = rs.render_file_name(template, short_name="融辉", waybill_no="R00024388425", kind="sign",
                               doc_date=date(2026, 11, 13), seq=1)
    assert name == "融辉-R00024388425-签收图.jpg"
    second = rs.render_file_name(template, short_name="融辉", waybill_no="R00024388425", kind="sign",
                                 doc_date=None, seq=2, of=3)
    assert second == "融辉-R00024388425-签收图(2).jpg"  # several pictures of one kind never collide
    dated = rs.render_file_name("{签收日期}_{运单号}_{单据类型}_{序号}.png", short_name="a/b", waybill_no="BY00025",
                                kind="sender_label", doc_date="2026-11-13", seq=2, of=3)
    assert dated == "20261113_BY00025_寄件单_2.png"
    assert "/" not in rs.render_file_name(template, short_name="a/b", waybill_no="X1", kind="receipt", doc_date=None, seq=1)


@pytest.mark.parametrize("bad", ["", "{运单号}.jpg", "{客户简称}-{运单号}-{单据类型}", "{运单号}-{单据类型}-{密码}.jpg",
                                 "../{运单号}-{单据类型}.jpg", "{运单号}-{单据类型}.exe", "{运单号-{单据类型}.jpg"])
def test_invalid_naming_templates_are_rejected(bad):
    with pytest.raises(rs.ReceiptShareError):
        rs.normalize_naming_template(bad)


def test_nas_path_is_by_waybill_and_rejects_traversal():
    path = rs.nas_image_path("ronghui", "R00024388425", "sign", "a" * 64, "jpg", "2026-11")
    assert path == "ronghui/2026-11/R00024388425/sign_aaaaaaaaaaaaaaaa.jpg"
    for args in (("other", "R1", "sign", "a" * 64, "jpg", "2026-11"), ("ronghui", "../x", "sign", "a" * 64, "jpg", "2026-11"),
                 ("ronghui", "R1", "sign", "zz", "jpg", "2026-11"), ("ronghui", "R1", "sign", "a" * 64, "php", "2026-11"),
                 ("ronghui", "R1", "sign", "a" * 64, "jpg", "2026/11")):
        with pytest.raises(ValueError):
            rs.nas_image_path(*args)
