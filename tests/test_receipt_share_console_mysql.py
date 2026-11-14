"""Real MySQL: Console customer-share endpoints from customer rules to link lifecycle."""
from __future__ import annotations

import io
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "console"))
from app import LocalDocFlowApp  # noqa: E402
from shared import receipt_share as rules  # noqa: E402
from tests.mysql_isolated_fixtures import repository, pytestmark  # noqa: E402,F401
from tests.test_receipt_share_refresh_mysql import _insert  # noqa: E402

SECRET = "k" * 48
ADMIN = {"id": 7, "username": "sharer", "display_name": "共享管理员", "role": "admin", "access_permissions": ["receipt.share"]}


class _Handler:
    def __init__(self, body=None, request_id=None):
        raw = json.dumps({**(body or {}), "request_id": request_id}).encode() if body is not None else b""
        self.headers = {"Content-Type": "application/json", "Content-Length": str(len(raw)),
                        "X-Browser-Request-UUID": request_id or ""}
        self.rfile = io.BytesIO(raw)
        self.current_admin_user = ADMIN


@pytest.fixture
def console(repository, tmp_path, monkeypatch):
    settings = tmp_path / "share.env"
    settings.write_text(f"RECEIPT_SHARE_TOKEN_SECRET={SECRET}\nRECEIPT_SHARE_BASE_URL=https://r.example.test\n", encoding="utf-8")
    monkeypatch.setenv("BOYI_RECEIPT_SHARE_ENV_FILE", str(settings))
    connection = repository._connection_factory()
    with connection.cursor() as cursor:
        for table in ("receipt_share_items", "receipt_share_events", "receipt_shares", "receipt_share_customers"):
            cursor.execute(f"DELETE FROM {table}")
        cursor.execute("DELETE FROM ronghui_waybills WHERE waybill_no LIKE 'RC%'")
    for number, sender in (("RC001", "勇胜"), ("RC002", "勇胜"), ("RC003", "别家")):
        _insert(connection, "ronghui_waybills", {"waybill_no": number, "sender_name": sender, "sender_phone": "07315186128",
                                                 "status": "signed", "open_date": "2026-11-10", "source": "ronghui"})
    connection.commit()
    connection.close()

    class _Repository:
        @contextmanager
        def connect(self):
            conn = repository._connection_factory()
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    app = LocalDocFlowApp.__new__(LocalDocFlowApp)
    app.repository = _Repository()
    app._require_same_origin_write = lambda handler: True
    app._mysql_console_principal = lambda user: {"actor_id": str(user["id"])}
    sent, agent = [], []
    app._send_json = lambda handler, status, payload: sent.append((int(status), payload))
    app._agent_request = lambda method, path, **kwargs: agent.append((method, path, kwargs.get("payload"))) or {"ok": True}

    def call(method_name, body=None, *args, query=None):
        sent.clear()
        handler = _Handler(body, str(uuid4())) if body is not None else _Handler()
        target = getattr(app, method_name)
        if body is None:
            target(handler, query or {})
        else:
            target(handler, *args)
        return sent[-1]

    return type("Console", (), {"call": staticmethod(call), "agent": agent, "app": app, "repository": repository})


def _customer(console, **overrides):
    body = {"customer_id": None, "name": "勇胜贸易有限公司", "short_name": "勇胜",
            "match": {"mode": "any", "conditions": [{"field": "sender_name", "op": "eq", "value": "勇胜"}]}, **overrides}
    return console.call("_handle_receipt_share_customer", body)


def _link(console, customer_id, **overrides):
    body = {"customer_id": customer_id, "scope": "all", "date_from": "", "date_to": "", "image_types": ["sign", "receipt"],
            "expires_days": 30, "access_code": "", "naming_template": rules.DEFAULT_NAMING_TEMPLATE, **overrides}
    return console.call("_handle_receipt_share_create", body)


def test_customer_rules_then_link_lifecycle(console):
    status, payload = _customer(console)
    assert status == 200 and payload["data"]["matching_waybills"] == 2  # only this customer's waybills
    customer_id = payload["data"]["customer_id"]
    assert _customer(console)[0] == 409  # the same name again
    assert console.call("_handle_receipt_share_preview", {"match": {"mode": "any", "conditions": [
        {"field": "sender_phone", "op": "contains", "value": "5186"}]}})[1]["data"]["matching_waybills"] == 3
    senders = console.call("_handle_receipt_share_senders", query={"q": ["勇胜"]})[1]["data"]
    assert senders == [{"sender_name": "勇胜", "sender_phone": "07315186128", "waybills": 2}]

    status, payload = _link(console, customer_id, access_code="4821")
    share = payload["data"]["share"]
    assert status == 200 and payload["data"]["access_code"] == "4821"
    assert share["access_code"] == "4821"  # recoverable for copying, sealed in the table
    token = share["url"].rsplit("/", 1)[-1]
    assert share["url"].startswith("https://r.example.test/r/") and rules.is_token_shape(token)
    assert (share["state"], share["image_types_text"], share["has_access_code"]) == ("active", "签收图·回单图", True)
    assert console.agent[-1] == ("POST", "/internal/v1/admin/receipt-shares/refresh", {"share_id": share["id"]})
    with console.repository._connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT token_sha256, access_code_sha256, access_code_sealed FROM receipt_shares WHERE id = %s",
                       (share["id"],))
        stored = cursor.fetchone()
    # Only digests are stored: neither the link code nor the access code can be read from the table.
    assert stored["token_sha256"] == rules.token_sha256(token) and token not in json.dumps(stored)
    assert stored["access_code_sha256"] == rules.access_code_sha256(SECRET, share["id"], "4821")
    assert "4821" not in stored["access_code_sealed"] and rules.open_access_code(SECRET, share["id"], stored["access_code_sealed"]) == "4821"

    def act(action, version, **extra):
        return console.call("_handle_receipt_share_action", {"record_version": version, **extra}, share["id"], action)

    status, payload = act("disable", share["record_version"])
    disabled = payload["data"]["share"]
    assert status == 200 and disabled["state"] == "disabled" and disabled["url"] == share["url"]
    assert act("enable", share["record_version"])[0] == 409  # stale version
    enabled = act("enable", disabled["record_version"])[1]["data"]["share"]
    reset = act("reset", enabled["record_version"])[1]["data"]["share"]
    assert reset["url"] != share["url"] and reset["state"] == "active"  # the old link no longer resolves
    with console.repository._connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS n FROM receipt_shares WHERE token_sha256 = %s", (rules.token_sha256(token),))
        assert cursor.fetchone()["n"] == 0
        cursor.execute("SELECT action FROM receipt_share_events WHERE share_id = %s ORDER BY id", (share["id"],))
        assert [row["action"] for row in cursor.fetchall()] == ["created", "disable", "enable", "reset"]
    renewed = act("renew", reset["record_version"], expires_days=0)[1]["data"]["share"]
    assert renewed["expires_text"] == "长期有效"
    custom = act("renew", renewed["record_version"], expires_days=45)[1]["data"]["share"]
    assert custom["expires_text"].startswith("至 ") and custom["access_code"] == "4821"  # survives a reset
    assert act("renew", custom["record_version"], expires_days=4000)[0] == 400
    listing = console.call("_handle_receipt_shares_data")[1]["data"]
    assert listing["configured"] is True and [item["id"] for item in listing["shares"]] == [share["id"]]


@pytest.mark.parametrize("change", [
    {"image_types": []}, {"image_types": ["other"]}, {"scope": "everything"}, {"expires_days": -1},
    {"expires_days": 3651}, {"expires_days": "30"},
    {"access_code": "12345"}, {"naming_template": "{运单号}.jpg"}, {"scope": "by_date", "date_from": "", "date_to": ""},
    {"customer_id": 999999},
])
def test_invalid_link_settings_are_refused(console, change):
    customer_id = _customer(console)[1]["data"]["customer_id"]
    status, payload = _link(console, change.pop("customer_id", customer_id), **change)
    assert status == 400 and payload["ok"] is False
    with console.repository._connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS n FROM receipt_shares")
        assert cursor.fetchone()["n"] == 0


def test_repeating_the_same_create_request_returns_the_same_link(console):
    customer_id = _customer(console)[1]["data"]["customer_id"]
    request_id = str(uuid4())
    body = {"customer_id": customer_id, "scope": "all", "date_from": "", "date_to": "", "image_types": ["sign"],
            "expires_days": 7, "access_code": "", "naming_template": rules.DEFAULT_NAMING_TEMPLATE}
    urls = []
    for _ in range(2):
        handler = _Handler(body, request_id)
        sent = []
        console.app._send_json = lambda _handler, status, payload: sent.append(payload)
        console.app._handle_receipt_share_create(handler)
        urls.append(sent[-1]["data"]["share"]["url"])
    assert urls[0] == urls[1]


def test_without_a_real_account_or_a_secret_nothing_is_created(console, monkeypatch):
    customer_id = _customer(console)[1]["data"]["customer_id"]
    monkeypatch.setenv("BOYI_RECEIPT_SHARE_ENV_FILE", "/nonexistent/share.env")
    assert _link(console, customer_id)[0] == 503
    assert console.call("_handle_receipt_shares_data")[1]["data"]["configured"] is False
    monkeypatch.setitem(ADMIN, "is_legacy_basic_auth", True)
    assert console.call("_handle_receipt_shares_data")[0] == 403
