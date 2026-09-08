"""Closed original-page reads/manual writes with synthetic field values."""

import base64
import json
import unittest
from urllib.parse import urlencode

from shared.manual_entry_contracts import manual_proxy_request_allowed


class ManualProxyRequestContractTests(unittest.TestCase):
    def request(self, *, path="/dataQuery/findAllByCallId", query="", body="", **extra):
        return {
            "method": "POST", "path": path, "query": query, "body": body,
            "content_type": "application/x-www-form-urlencoded; charset=UTF-8", **extra,
        }

    def test_observed_ronghui_initialization_shapes(self):
        calls = (
            ("FIND_SYS_DATE", {}, {}),
            ("FIND_SITE_INFO_BY_SITE_CODE", {"SITE_CODE": "fixture-site"}, {}),
            ("FIND_SITE_AND_CENTER", {"SITE_CODE": "fixture-site"}, {}),
            ("FIND_TAB_SITE_BY_AGENT", {"SITE_CODE": "fixture-site"}, {}),
            ("FIND_TAB_QUOTE_SWITCH_SITE", {"SITE_CODE": "fixture-site"}, {}),
            ("FIND_TMS_SYS_SHARE_SET", {}, {"SHARE_CODE_IN": "fixture-rule"}),
            ("FIND_TAB_SITE_BUSINESS_TYPE", {}, {"SITE_CODE": "fixture-site"}),
            ("FIND_BILL_CHECK", {}, {"CREATE_MAN_CODE": "fixture-operator"}),
            ("FIND_TAB_COLLAR_CURRENT_SITE", {}, {
                "BELONG_SITE_CODE": "fixture-site", "COLLAR_STATUS": "fixture-state",
            }),
            ("FIND_SITE_INFO_BY_SITE_CODE", {}, {"SITE_CODE": "fixture-site"}),
        )
        for selector, query_fields, body_fields in calls:
            with self.subTest(selector=selector, query_fields=tuple(query_fields)):
                if selector == "FIND_TMS_SYS_SHARE_SET":
                    body_fields = {"id": selector, **body_fields}
                    query = query_fields
                else:
                    query = {"id": selector, **query_fields}
                self.assertTrue(manual_proxy_request_allowed("ronghui", self.request(
                    query=urlencode(query), body=urlencode(body_fields),
                )))
        self.assertTrue(manual_proxy_request_allowed("ronghui", self.request(
            path="/minic/combobox", query="optionCode=WEIGHT_RATIO",
        )))

    def test_observed_yunda_initialization_shapes(self):
        for suffix in ("elecStock.html", "getCostInfoPrompt.html"):
            with self.subTest(suffix=suffix):
                self.assertTrue(manual_proxy_request_allowed("yunda", self.request(
                    path=f"/ky_inms/public/index.php/{suffix}",
                )))
        self.assertTrue(manual_proxy_request_allowed("yunda", self.request(
            path="/ky_inms/public/index.php/business/waybill/entry/getTemplateList.html",
            body="CreatedDotCode=fixture-site&IsNew=1&queryType=fixture-type",
        )))

    def test_ronghui_manual_allocation_and_following_existence_check(self):
        for body, content_type in (
            ("vCount=1", "application/x-www-form-urlencoded"),
            ('{"vCount":1}', "application/json"),
            ('{"vCount":"1"}', "application/json"),
        ):
            with self.subTest(body=body):
                self.assertTrue(manual_proxy_request_allowed("ronghui", self.request(
                    query="id=FIND_TMS_BILL_CODE_BY", content_type=content_type,
                    body=body, body_base64=base64.b64encode(body.encode()).decode(),
                )))
        self.assertTrue(manual_proxy_request_allowed("ronghui", self.request(
            query="id=GET_BILL_BY_BILLCODE", body="BILL_CODE=fixture-bill",
        )))

    def test_ronghui_allocation_cannot_expand_count_or_hide_conflicting_fields(self):
        selector = "id=FIND_TMS_BILL_CODE_BY"
        attempts = [self.request(query=selector, body=f"vCount={count}")
                    for count in ("", "0", "2", "-1", "1.0", "01", "true")]
        attempts += [
            {"method": "GET", "path": "/dataQuery/findAllByCallId", "query": selector + "&vCount=1"},
            {"method": "GET", "path": "/dataQuery/findAllByCallId?" + selector + "&vCount=1"},
            {"method": "GET", "path": "/dataQuery/findAllByCallId;ignored", "query": selector + "&vCount=1"},
            self.request(query=selector),
            self.request(query=selector + "&vCount=1", body="vCount=1"),
            self.request(query=selector + "&id=FIND_SYS_DATE", body="vCount=1"),
            self.request(query=selector, body="vCount=1&vCount=2"),
            self.request(query=selector, body="vCount=1&action=delete"),
            self.request(query=selector, body="vCount=1", body_base64=base64.b64encode(b"vCount=2").decode()),
            self.request(query=selector, body='{"vCount":true}', content_type="application/json"),
            self.request(query=selector, body='{"vCount":1.0}', content_type="application/json"),
            self.request(query="id=GET_BILL_BY_BILLCODE", body="BILL_CODE="),
            self.request(query="id=GET_BILL_BY_BILLCODE", body="BILL_CODE=x&action=delete"),
            self.request(query="id=GET_BILL_BY_BILLCODE&BILL_CODE=x", body="BILL_CODE=y"),
        ]
        for params in attempts:
            with self.subTest(params=params):
                self.assertFalse(manual_proxy_request_allowed("ronghui", params))

    def test_form_json_and_base64_inspect_exact_final_body(self):
        for content_type, body in (
            ("application/x-www-form-urlencoded", "id=FIND_SYS_DATE"),
            ("application/json", json.dumps({"id": "FIND_SYS_DATE"})),
        ):
            for as_base64 in (False, True):
                with self.subTest(content_type=content_type, as_base64=as_base64):
                    params = self.request(body=body, content_type=content_type)
                    if as_base64:
                        params.pop("body")
                        params["body_base64"] = base64.b64encode(body.encode()).decode()
                    self.assertTrue(manual_proxy_request_allowed("ronghui", params))
                    # Explicit duplicate representation is acceptable only when
                    # byte-identical to the actual base64 transport.
                    params["body_base64"] = base64.b64encode(body.encode()).decode()
                    params["body"] = body
                    self.assertTrue(manual_proxy_request_allowed("ronghui", params))
        params = self.request(body='{"id":"FIND_SYS_DATE"}', content_type="")
        params["headers"] = {"content-type": "application/json"}
        self.assertTrue(manual_proxy_request_allowed("ronghui", params))

    def test_selector_duplicates_unknowns_and_other_dispatch_arguments_fail_closed(self):
        attempts = (
            self.request(query="id=FIND_SYS_DATE&id=FIND_SYS_DATE"),
            self.request(query="id=FIND_SYS_DATE&id=DELETE_TABLE"),
            self.request(query="id=FIND_SYS_DATE", body="id=FIND_SYS_DATE"),
            self.request(query="id=FIND_SYS_DATE", body="id=DELETE_TABLE"),
            self.request(query="id=FIND_SYS_DATE", body="action=save"),
            self.request(query="id=FIND_UNREVIEWED_OPERATION"),
            self.request(query="id=DELETE_TABLE"),
            self.request(query="ID=FIND_SYS_DATE"),
            self.request(query="id[]=FIND_SYS_DATE"),
            self.request(query="id=FIND_SYS_DATE%26id%3DDELETE_TABLE"),
            self.request(query="id=%2546IND_SYS_DATE"),
            self.request(query="id="),
            self.request(),
            self.request(body='{"id":"FIND_SYS_DATE","id":"DELETE_TABLE"}',
                         content_type="application/json"),
            self.request(body='{"id":["FIND_SYS_DATE"]}', content_type="application/json"),
            self.request(body='["FIND_SYS_DATE"]', content_type="application/json"),
            self.request(body='{"id":"FIND_SYS_DATE"', content_type="application/json"),
            self.request(path="/minic/combobox", query="optionCode=UNREVIEWED"),
            self.request(path="/minic/combobox", query="optionCode=WEIGHT_RATIO",
                         body="optionCode=WEIGHT_RATIO"),
        )
        for params in attempts:
            with self.subTest(params=params):
                self.assertFalse(manual_proxy_request_allowed("ronghui", params))

    def test_body_and_content_type_cannot_authorize_an_unused_representation(self):
        safe_body = "id=FIND_SYS_DATE"
        encoded_write = base64.b64encode(b"id=DELETE_TABLE").decode()
        attempts = (
            self.request(body=safe_body, body_base64=encoded_write),
            self.request(query="id=FIND_SYS_DATE", body_base64=encoded_write),
            self.request(body=safe_body, body_base64="%%%%"),
            self.request(body_base64=base64.b64encode(b"\xff").decode()),
            self.request(body=safe_body, headers={"Content-Type": "application/json"}),
            self.request(body=safe_body, headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "content-type": "application/json",
            }),
            self.request(body=safe_body, content_type="text/plain"),
            self.request(body=safe_body, content_type="multipart/form-data; boundary=x"),
            self.request(body=safe_body, content_type="application/x-www-form-urlencoded; charset=UTF-16"),
        )
        for params in attempts:
            with self.subTest(params=params):
                self.assertFalse(manual_proxy_request_allowed("ronghui", params))

    def test_yunda_lookup_does_not_admit_extra_operations_or_unknown_post(self):
        template = "/ky_inms/public/index.php/business/waybill/entry/getTemplateList.html"
        fields = "CreatedDotCode=fixture-site&IsNew=1&queryType=fixture-type"
        for params in (
            self.request(path=template, body=fields + "&action=save"),
            self.request(path=template, body=fields, query="queryType=other"),
            self.request(path=template, body="CreatedDotCode=fixture-site"),
            self.request(path="/ky_inms/public/index.php/elecStock.html", body="action=save"),
            self.request(path="/ky_inms/public/index.php/elecStock.html", body="{}",
                         content_type="application/json"),
            self.request(path="/ky_inms/public/index.php/getCostInfoPrompt.html", query="action=save"),
            self.request(path="/ky_inms/public/index.php/business/waybill/entry/delete.html"),
        ):
            with self.subTest(params=params):
                self.assertFalse(manual_proxy_request_allowed("yunda", params))

    def test_paths_methods_get_and_exact_save_contract_remain_closed(self):
        for provider, save, read in (
            ("ronghui", "/dataOperation/saveTables", "/module/index"),
            ("yunda", "/ky_inms/public/index.php/business/waybill/entry/save.html",
             "/ky_inms/public/static/app.css"),
        ):
            with self.subTest(provider=provider):
                self.assertTrue(manual_proxy_request_allowed(provider, self.request(path=save)))
                self.assertTrue(manual_proxy_request_allowed(provider, {"method": "GET", "path": read}))
                for method in ("PUT", "DELETE", "PATCH"):
                    self.assertFalse(manual_proxy_request_allowed(provider, {"method": method, "path": save}))
                self.assertFalse(manual_proxy_request_allowed(provider, self.request(path=save + "/extra")))
        for path in (
            "/dataQuery/../dataOperation/saveTables", "/dataQuery/%2e%2e/dataOperation/saveTables",
            "/dataQuery/findAllByCallId?id=FIND_SYS_DATE", "/dataQuery/findAllByCallId/extra",
            "/dataQuery/%66indAllByCallId", "//dataQuery/findAllByCallId",
        ):
            with self.subTest(path=path):
                # Canonical percent-encoded ordinary letters are intentionally
                # accepted by the established path contract.
                expected = path == "/dataQuery/%66indAllByCallId"
                self.assertEqual(expected, manual_proxy_request_allowed("ronghui", self.request(
                    path=path, query="id=FIND_SYS_DATE",
                )))
        self.assertFalse(manual_proxy_request_allowed("unreviewed", {"method": "GET", "path": "/module/"}))
