from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

import pytest

from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.first_party_handlers import (
    FirstPartyCoreHandlerPorts,
    build_first_party_core_handler_map,
)
from agent.automation_plugins.manifest import canonical_json_bytes
from plugin_core_adapters.first_party import (
    _scan_next_identities_sha256,
    _scan_next_verify,
    build_production_first_party_core_handler_map,
)
from plugin_core_adapters.scan_snapshot import (
    replace_scan_snapshot_verified,
    scan_snapshot_identities_sha256,
)


_SECRET = b"scan-codes-closed-handler-secret-value"


def _context(action: str) -> CoreBrokerInvocationContext:
    return CoreBrokerInvocationContext(
        automation_id="scan-instance",
        plugin_version="1.0.0",
        tool_name="sync_scan_codes",
        operation="browser.invoke",
        action=action,
        role="account_id",
        account_ids=("scan-account",),
    )


def _descriptor(account_id: str) -> Mapping[str, Any]:
    assert account_id == "scan-account"
    return {
        "account_id": account_id,
        "system": "ronghui",
        "session_profile": "scan-profile",
    }


def _snapshot_context() -> CoreBrokerInvocationContext:
    return CoreBrokerInvocationContext(
        automation_id="scan-instance",
        plugin_version="1.0.0",
        tool_name="sync_scan_codes",
        operation="projection.invoke",
        action="scan.snapshot.replace",
        role="account_id",
        account_ids=("scan-account",),
    )


def _snapshot_rows() -> list[dict[str, str]]:
    return [
        {
            "raw_code": "R12345678901",
            "destination": "总站",
            "code_type": "main",
            "main_tracking": "R12345678901",
        },
        {
            "raw_code": "R123456789010001",
            "destination": "A站",
            "code_type": "child",
            "main_tracking": "R12345678901",
        },
    ]


def test_scan_snapshot_production_port_requires_exact_fresh_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _snapshot_rows()
    writes: list[list[dict[str, str]]] = []
    reads = 0

    def write(records, target_date):
        assert target_date == "2026-08-15"
        writes.append(records)
        return {"ok": True, "upserted": len(records)}

    def read(target_date):
        assert target_date == "2026-08-15"
        nonlocal reads
        reads += 1
        return rows

    monkeypatch.setattr("tools.phase7_mysql_store.replace_scan_codes_snapshot", write)
    monkeypatch.setattr("tools.phase7_mysql_store.list_scan_codes_for_date", read)

    result = replace_scan_snapshot_verified(rows, "2026-08-15")

    assert result == {
        "ok": True,
        "verified": True,
        "record_count": 2,
        "readback_count": 2,
        "identities_sha256": scan_snapshot_identities_sha256(rows),
    }
    assert writes == [rows]
    assert reads == 1


def test_empty_scan_snapshot_rejects_stale_rows_after_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_rows = _snapshot_rows()

    monkeypatch.setattr(
        "tools.phase7_mysql_store.replace_scan_codes_snapshot",
        lambda records, target_date: {
            "ok": True,
            "replaced": len(records),
            "target_date": target_date,
        },
    )
    monkeypatch.setattr(
        "tools.phase7_mysql_store.list_scan_codes_for_date",
        lambda _target_date: stale_rows,
    )

    with pytest.raises(PluginExecutionError) as exc:
        replace_scan_snapshot_verified([], "2026-08-24")

    assert exc.value.code == "WRITE_OUTCOME_UNKNOWN"


def test_scan_snapshot_response_loss_is_reconciled_by_exact_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _snapshot_rows()

    def lost_response(_records, _target_date):
        raise TimeoutError("response lost after commit")

    monkeypatch.setattr(
        "tools.phase7_mysql_store.replace_scan_codes_snapshot",
        lost_response,
    )
    monkeypatch.setattr(
        "tools.phase7_mysql_store.list_scan_codes_for_date",
        lambda _target_date: rows,
    )

    result = replace_scan_snapshot_verified(rows, "2026-08-24")

    assert result["verified"] is True
    assert result["readback_count"] == len(rows)


@pytest.mark.parametrize("rows", [[], _snapshot_rows()], ids=["empty", "populated"])
def test_scan_snapshot_store_atomically_deletes_before_insert(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict[str, str]],
) -> None:
    from tools import phase7_mysql_store

    events: list[str] = []

    class Cursor:
        rowcount = 3

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement, params=None):
            assert "DELETE FROM scan_codes" in statement
            assert "snapshot_date = %s" in statement
            assert params == ("2026-08-24",)
            events.append("delete")

        def executemany(self, statement, values):
            assert "INSERT INTO scan_codes" in statement
            assert "snapshot_date" in statement
            assert "last_seen_at" in statement
            assert len(values) == len(rows)
            assert all(value[-2] == "2026-08-24" for value in values)
            assert all(value[-1] == "2026-08-24" for value in values)
            events.append("insert")

    class Connection:
        def begin(self):
            events.append("begin")

        def cursor(self):
            return Cursor()

        def commit(self):
            events.append("commit")

        def rollback(self):
            events.append("rollback")

        def close(self):
            events.append("close")

    monkeypatch.setattr(phase7_mysql_store, "ensure_phase7_tables", lambda: None)
    monkeypatch.setattr(phase7_mysql_store, "_connect", Connection)

    result = phase7_mysql_store.replace_scan_codes_snapshot(rows, "2026-08-24")

    assert result == {
        "ok": True,
        "deleted": 3,
        "replaced": len(rows),
        "target_date": "2026-08-24",
    }
    assert events == [
        "begin",
        "delete",
        *(["insert"] if rows else []),
        "commit",
        "close",
    ]


def test_scan_snapshot_schema_preserves_same_identity_across_dates() -> None:
    migration = (
        Path(__file__).resolve().parents[1]
        / "agent"
        / "migrations"
        / "026_scan_code_daily_identity.sql"
    ).read_text(encoding="utf-8")

    assert "ADD COLUMN snapshot_date DATE" in migration
    assert "SET snapshot_date = DATE(last_seen_at)" in migration
    assert "ADD PRIMARY KEY (snapshot_date, raw_code)" in migration


@pytest.mark.parametrize("case", ["response_loss", "zero", "multiple", "mismatch"])
def test_scan_snapshot_production_port_fails_unknown_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    rows = _snapshot_rows()
    write_calls = 0
    read_calls = 0

    def write(_records, _target_date):
        nonlocal write_calls
        write_calls += 1
        if case == "response_loss":
            raise TimeoutError("lost response")
        return {"ok": True, "upserted": len(rows)}

    def read(_target_date):
        nonlocal read_calls
        read_calls += 1
        if case == "zero":
            return []
        if case == "multiple":
            return [rows[0], rows[0], rows[1]]
        changed = [dict(row) for row in rows]
        changed[1]["destination"] = "错误站"
        return changed

    monkeypatch.setattr("tools.phase7_mysql_store.replace_scan_codes_snapshot", write)
    monkeypatch.setattr("tools.phase7_mysql_store.list_scan_codes_for_date", read)

    with pytest.raises(PluginExecutionError) as exc:
        replace_scan_snapshot_verified(rows, "2026-08-15")

    assert exc.value.code == "WRITE_OUTCOME_UNKNOWN"
    assert write_calls == 1
    assert read_calls == 1


def test_scan_snapshot_core_handler_rejects_ack_or_tampered_readback_proof() -> None:
    rows = _snapshot_rows()

    for raw in (
        {"ok": True, "record_count": 2},
        {
            "ok": True,
            "verified": True,
            "record_count": 2,
            "readback_count": 2,
            "identities_sha256": "0" * 64,
        },
    ):
        calls = 0

        def replace(_records, _target_date):
            nonlocal calls
            calls += 1
            return raw

        handlers = build_first_party_core_handler_map(
            FirstPartyCoreHandlerPorts(
                describe_account=_descriptor,
                replace_scan_snapshot=replace,
            ),
            cursor_secret=_SECRET,
        )
        with pytest.raises(PluginExecutionError) as exc:
            handlers[("projection.invoke", "scan.snapshot.replace")](
                _snapshot_context(),
                {"records": rows, "target_date": "2026-08-15"},
            )
        assert exc.value.code == "WRITE_OUTCOME_UNKNOWN"
        assert calls == 1


def test_scan_next_verify_requires_a_fresh_context_bound_server_read():
    items = [
        {"bill_code": "R123456789010001", "station_name": "A站"},
        {"bill_code": "R123456789010002", "station_name": "A站"},
    ]
    submit_calls = 0
    verify_calls = 0

    def submit(descriptor, requested):
        nonlocal submit_calls
        submit_calls += 1
        assert descriptor["session_profile"] == "scan-profile"
        assert requested == items
        return {
            "ok": True,
            "stage": "done",
            "write_started_at": "2026-08-15T00:00:00+00:00",
            "write_finished_at": "2026-08-15T00:00:05+00:00",
            "detail": {
                "items": items,
                "stations": [
                    {
                        "station_name": "A站",
                        "count": 1,
                        "bill_codes": ["R123456789010001"],
                    }
                ],
                "total_scanned": 1,
                "skipped_signed_codes": ["R123456789010002"],
            },
        }

    def verify(descriptor, requested, started_at, finished_at):
        nonlocal verify_calls
        verify_calls += 1
        assert descriptor["session_profile"] == "scan-profile"
        assert requested == [items[0]]
        assert started_at == "2026-08-15T00:00:00+00:00"
        assert finished_at == "2026-08-15T00:00:05+00:00"
        return {
            "ok": True,
            "verified": True,
            "record_count": 1,
            "identities_sha256": hashlib.sha256(
                canonical_json_bytes(items[:1])
            ).hexdigest(),
        }

    handlers = build_first_party_core_handler_map(
        FirstPartyCoreHandlerPorts(
            describe_account=_descriptor,
            scan_next_submit=submit,
            scan_next_verify=verify,
        ),
        cursor_secret=_SECRET,
    )
    submitted = handlers[("browser.invoke", "ronghui.scan_next.submit")](
        _context("ronghui.scan_next.submit"),
        {"items": items},
    )
    assert submitted["submitted"] == 2
    assert submitted["scanned"] == 1
    assert submitted["skipped_signed_codes"] == ["R123456789010002"]

    verified = handlers[("browser.invoke", "ronghui.scan_next.verify")](
        _context("ronghui.scan_next.verify"),
        {
            "operation_id": submitted["operation_id"],
            "items_sha256": submitted["items_sha256"],
            "submitted": submitted["submitted"],
            "scanned": submitted["scanned"],
            "skipped_signed_codes": submitted["skipped_signed_codes"],
        },
    )
    assert verified | {
        "verified": True,
        "postcondition": "server_ledger_verified",
        "readback_count": 1,
    } == verified
    assert submit_calls == 1
    assert verify_calls == 1

    with pytest.raises(PluginExecutionError) as exc:
        handlers[("browser.invoke", "ronghui.scan_next.verify")](
            _context("ronghui.scan_next.verify"),
            {
                "operation_id": submitted["operation_id"],
                "items_sha256": submitted["items_sha256"],
                "submitted": submitted["submitted"],
                "scanned": 2,
                "skipped_signed_codes": [],
            },
        )
    assert exc.value.code == "BROKER_SOURCE_IDENTITY_MISMATCH"
    assert verify_calls == 1


@pytest.mark.parametrize(
    "raw",
    [
        {"ok": True, "stage": "upload_success", "detail": {}},
        {
            "ok": True,
            "stage": "done",
            "detail": {
                "items": [{"bill_code": "R1", "station_name": "A站"}],
                "stations": [],
                "total_scanned": 0,
                "skipped_signed_codes": [],
            },
        },
    ],
)
def test_scan_next_submit_rejects_ack_without_closed_postcondition(raw):
    handlers = build_first_party_core_handler_map(
        FirstPartyCoreHandlerPorts(
            describe_account=_descriptor,
            scan_next_submit=lambda descriptor, items: raw,
            scan_next_verify=lambda descriptor, items, started, finished: {
                "ok": True,
                "verified": True,
                "record_count": len(items),
                "identities_sha256": _scan_next_identities_sha256(items),
            },
        ),
        cursor_secret=_SECRET,
    )

    with pytest.raises(PluginExecutionError):
        handlers[("browser.invoke", "ronghui.scan_next.submit")](
            _context("ronghui.scan_next.submit"),
            {"items": [{"bill_code": "R1", "station_name": "A站"}]},
        )


def test_production_scan_next_calls_low_level_run_flow_and_never_whole_tool():
    class Manager:
        def require_authenticated_binding(self, account_id: str) -> dict[str, str]:
            assert account_id == "scan-account"
            return {
                "account_id": account_id,
                "system": "ronghui",
                "account_purpose": "general",
                "session_profile": "scan-profile",
            }

    items = [{"bill_code": "R1", "station_name": "A站"}]
    low_level = {
        "ok": True,
        "stage": "done",
        "detail": {
            "items": items,
            "stations": [
                {"station_name": "A站", "count": 1, "bill_codes": ["R1"]}
            ],
            "total_scanned": 1,
            "skipped_signed_codes": [],
        },
    }
    handlers = build_production_first_party_core_handler_map(
        account_manager=Manager(),
        cursor_secret=_SECRET,
    )
    with (
        patch(
            "agent.tms_runtime.scripts.scan_next.run_flow",
            return_value=low_level,
        ) as run_flow,
        patch(
            "agent.tms_runtime.scripts.scan_next.run_once",
            side_effect=AssertionError("whole-tool fallback is forbidden"),
        ) as run_once,
    ):
        result = handlers[("browser.invoke", "ronghui.scan_next.submit")](
            _context("ronghui.scan_next.submit"),
            {"items": items},
        )

    assert result["scanned"] == 1
    assert run_flow.call_args.kwargs | {
        "items": items,
        "session_profile": "scan-profile",
        "max_login_attempts": 1,
        "dump_on_error": False,
    } == run_flow.call_args.kwargs
    run_once.assert_not_called()


def _authoritative_scan_row(*, row_id: str = "row-1") -> dict[str, str]:
    return {
        "BILL_CODE": "R123456789010001",
        "DATA_FROM": "K13",
        "PRE_OR_NEXT_STATION": "A站",
        "REGISTER_DATE": "2026-08-15 08:00:02",
        "ROW_ID": row_id,
        "SCAN_DATE": "2026-08-15 08:00:02",
        "SCAN_SITE_CODE": "site-1",
        "SCAN_TYPE": "发件",
    }


class _ScanReadbackResponse:
    status_code = 200

    def __init__(self, rows: list[dict[str, str]]) -> None:
        self._rows = rows

    def json(self) -> dict[str, object]:
        return {"data": self._rows, "total": len(self._rows)}


def test_scan_next_adapter_reads_the_fresh_authoritative_send_scan_ledger():
    class Session:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def post(self, _url, **kwargs):
            self.calls.append(kwargs)
            return _ScanReadbackResponse([_authoritative_scan_row()])

    session = Session()
    items = [{"bill_code": "R123456789010001", "station_name": "A站"}]
    with (
        patch(
            "agent.tms_runtime.scripts.login_manager.TMSAuth"
        ) as auth_factory,
        patch(
            "agent.tms_runtime.scripts.receipts_sync._read_user_info_cookie",
            return_value={"siteCode": "site-1"},
        ),
        patch(
            "agent.tms_runtime.scripts.receipts_sync._resolve_login_site_code_from_user_info",
            return_value="site-1",
        ),
    ):
        auth_factory.return_value.login_and_get_session.return_value = session
        result = _scan_next_verify(
            _descriptor("scan-account"),
            items,
            "2026-08-15T00:00:00+00:00",
            "2026-08-15T00:00:05+00:00",
        )

    assert result == {
        "ok": True,
        "verified": True,
        "record_count": 1,
        "identities_sha256": _scan_next_identities_sha256(items),
    }
    assert len(session.calls) == 1
    assert session.calls[0]["params"] == {"id": "FIND_SEND_SCAN_RECORD"}
    assert session.calls[0]["data"] | {
        "SCAN_TYPE": "发件",
        "SCAN_SITE_CODE": "site-1",
        "LOGIN_SITE_CODE": "site-1",
        "BILL_CODE": "R123456789010001",
        "searchOrderInput": "R123456789010001",
    } == session.calls[0]["data"]


@pytest.mark.parametrize("case", ["zero", "multiple", "incomplete"])
def test_scan_next_authoritative_readback_fails_unknown_for_non_exact_rows(case):
    if case == "zero":
        rows: list[dict[str, str]] = []
    elif case == "multiple":
        rows = [
            _authoritative_scan_row(row_id="row-1"),
            _authoritative_scan_row(row_id="row-2"),
        ]
    else:
        incomplete = _authoritative_scan_row()
        incomplete.pop("DATA_FROM")
        rows = [incomplete]

    class Session:
        def post(self, _url, **_kwargs):
            return _ScanReadbackResponse(rows)

    with (
        patch(
            "agent.tms_runtime.scripts.login_manager.TMSAuth"
        ) as auth_factory,
        patch(
            "agent.tms_runtime.scripts.receipts_sync._read_user_info_cookie",
            return_value={"siteCode": "site-1"},
        ),
        patch(
            "agent.tms_runtime.scripts.receipts_sync._resolve_login_site_code_from_user_info",
            return_value="site-1",
        ),
    ):
        auth_factory.return_value.login_and_get_session.return_value = Session()
        with pytest.raises(PluginExecutionError) as exc:
            _scan_next_verify(
                _descriptor("scan-account"),
                [{"bill_code": "R123456789010001", "station_name": "A站"}],
                "2026-08-15T00:00:00+00:00",
                "2026-08-15T00:00:05+00:00",
            )

    assert exc.value.code == "WRITE_OUTCOME_UNKNOWN"
