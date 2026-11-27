from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

import pytest

from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.send_data_export_handlers import (
    RONGHUI_ACTION, YUNDA_ACTION, SendDataExportPorts, build_send_data_export_handler_map,
)
from agent.connector_namespaces import DAILY_SEND_TOOL, YUNDA_SEND_TOOL
from shared.send_data_nas_store import SendDataNasError, SendDataNasStore
from shared.send_data_workbook import (
    SITE_COLUMN, WorkbookError, build_workbook, check_table, merge_site_rows, read_workbook,
)

COLUMNS = ["运单编号", "发件日期", "件数", "备注"]


class MemoryStore:
    def __init__(self, *, fail_write: str | None = None) -> None:
        self.files: dict[tuple[str, str], bytes] = {}
        self.writes = 0
        self.fail_write = fail_write

    def read_optional(self, folder: str, name: str) -> bytes | None:
        return self.files.get((folder, name))

    def write(self, folder: str, name: str, content: bytes) -> None:
        if self.fail_write:
            raise SendDataNasError(self.fail_write, "NAS 上的表格正被占用。")
        self.writes += 1
        self.files[(folder, name)] = content

    def delete(self, folder: str, name: str) -> None:
        self.deletes = getattr(self, "deletes", 0) + 1
        self.files.pop((folder, name), None)


@dataclass
class Context:
    tool_name: str
    operation: str
    action: str
    account_ids: tuple[str, ...] = ("acct-1",)
    role: str = "account_id"
    automation_id: str = "auto-1"
    plugin_version: str = "2.2.0"
    marks: list[int] = field(default_factory=list)

    @property
    def account_bindings(self) -> dict[str, tuple[str, ...]]:
        return {"account_id": self.account_ids}

    def mark_write_started(self) -> None:
        self.marks.append(1)


def _handlers(store: MemoryStore, sites: dict[str, str] | None = None):
    sites = sites or {"acct-1": "测试甲站", "acct-2": "测试乙站"}
    ports = SendDataExportPorts(
        describe_account=lambda account_id, system: {"account_id": account_id, "system": system,
                                                     "session_profile": account_id},
        ronghui_site_name=lambda descriptor: sites[descriptor["account_id"]],
        open_store=lambda: store,
    )
    return build_send_data_export_handler_map(ports, cursor_secret=b"s" * 32)


def _ronghui(account: str = "acct-1") -> Context:
    return Context(DAILY_SEND_TOOL, *RONGHUI_ACTION, account_ids=(account,))


def test_two_ronghui_accounts_share_one_workbook_and_replace_only_their_rows():
    store = MemoryStore()
    handler = _handlers(store)[RONGHUI_ACTION]
    first = handler(_ronghui("acct-1"), {"target_date": "2026-11-24", "columns": COLUMNS,
                                         "rows": [["R001", "2026-11-24", 2, "甲"], ["R002", "2026-11-24", 1, None]]})
    assert first["committed"] and first["verified"] and first["file"] == "融辉寄件/2026-11-24.xlsx"
    handler(_ronghui("acct-2"), {"target_date": "2026-11-24", "columns": COLUMNS,
                                 "rows": [["S001", "2026-11-24", 3, "乙"]]})
    again = handler(_ronghui("acct-1"), {"target_date": "2026-11-24", "columns": COLUMNS,
                                         "rows": [["R003", "2026-11-24", 5.5, "=SUM(A1)"]]})
    columns, rows = read_workbook(store.files[("融辉寄件", "2026-11-24.xlsx")])
    assert columns == [SITE_COLUMN, *COLUMNS]
    assert rows == [["测试乙站", "S001", "2026-11-24", 3, "乙"], ["测试甲站", "R003", "2026-11-24", 5.5, "=SUM(A1)"]]
    assert again["record_count"] == 1 and again["file_record_count"] == 2 and again["site"] == "测试甲站"


def test_yunda_workbook_is_replaced_whole_without_site_column():
    store = MemoryStore()
    handler = _handlers(store)[YUNDA_ACTION]
    context = Context(YUNDA_SEND_TOOL, *YUNDA_ACTION)
    handler(context, {"target_date": "2026-11-24", "columns": COLUMNS, "rows": [["Y1", "2026-11-24", 1, ""]]})
    assert read_workbook(store.files[("韵达寄件", "2026-11-24.xlsx")]) == (COLUMNS, [["Y1", "2026-11-24", 1, None]])
    # 2026-11-27: a later run of the same day finds no sends — the day's file is removed, not emptied.
    result = handler(context, {"target_date": "2026-11-24", "columns": COLUMNS, "rows": []})
    assert ("韵达寄件", "2026-11-24.xlsx") not in store.files
    assert result["verified"] and result["file"] == "" and result["record_count"] == 0 and len(context.marks) == 2


def test_a_day_without_sends_leaves_no_file_on_the_nas():
    store = MemoryStore()
    yunda = _handlers(store)[YUNDA_ACTION]
    context = Context(YUNDA_SEND_TOOL, *YUNDA_ACTION)
    result = yunda(context, {"target_date": "2026-11-26", "columns": COLUMNS, "rows": []})
    assert store.files == {} and store.writes == 0 and context.marks == []
    assert result["committed"] and result["verified"] and result["file"] == "" and result["record_count"] == 0
    assert result["evidence_ref"]


def test_ronghui_keeps_the_shared_file_while_the_other_site_has_rows():
    store = MemoryStore()
    handler = _handlers(store)[RONGHUI_ACTION]
    empty = handler(_ronghui("acct-2"), {"target_date": "2026-11-24", "columns": COLUMNS, "rows": []})
    assert store.files == {} and empty["file"] == "" and empty["site"] == "测试乙站"
    handler(_ronghui("acct-1"), {"target_date": "2026-11-24", "columns": COLUMNS, "rows": [["R1", "2026-11-24", 1, ""]]})
    handler(_ronghui("acct-2"), {"target_date": "2026-11-24", "columns": COLUMNS, "rows": []})
    assert read_workbook(store.files[("融辉寄件", "2026-11-24.xlsx")])[1] == [["测试甲站", "R1", "2026-11-24", 1, None]]
    # The last site's rows go: nothing is left that day, so the file goes too.
    handler(_ronghui("acct-1"), {"target_date": "2026-11-24", "columns": COLUMNS, "rows": []})
    assert store.files == {}


def test_a_file_that_survives_its_delete_is_an_unknown_write():
    class Stubborn(MemoryStore):
        def delete(self, folder, name):
            pass

    store = Stubborn()
    store.files[("韵达寄件", "2026-11-26.xlsx")] = build_workbook(COLUMNS, [])
    with pytest.raises(PluginExecutionError) as raised:
        _handlers(store)[YUNDA_ACTION](Context(YUNDA_SEND_TOOL, *YUNDA_ACTION),
                                      {"target_date": "2026-11-26", "columns": COLUMNS, "rows": []})
    assert raised.value.code == "WRITE_OUTCOME_UNKNOWN"


def test_wrong_tool_bad_table_and_locked_file_fail_clearly():
    store = MemoryStore()
    handler = _handlers(store)[RONGHUI_ACTION]
    with pytest.raises(PluginExecutionError) as wrong:
        handler(Context(YUNDA_SEND_TOOL, *RONGHUI_ACTION), {"target_date": "2026-11-24", "columns": COLUMNS, "rows": []})
    assert wrong.value.code == "BROKER_CONTEXT_INVALID"
    with pytest.raises(PluginExecutionError) as bad:
        handler(_ronghui(), {"target_date": "2026-11-24", "columns": [SITE_COLUMN], "rows": []})
    assert bad.value.code == "BROKER_ARGUMENT_INVALID"
    locked = _handlers(MemoryStore(fail_write="NAS_FILE_LOCKED"))[RONGHUI_ACTION]
    context = _ronghui()
    with pytest.raises(PluginExecutionError) as error:
        locked(context, {"target_date": "2026-11-24", "columns": COLUMNS, "rows": [["R1", "2026-11-24", 1, None]]})
    assert error.value.code == "WRITE_OUTCOME_UNKNOWN" and "占用" in str(error.value) and context.marks == [1]


def test_readback_mismatch_is_unknown_write():
    class Lossy(MemoryStore):
        def read_optional(self, folder, name):
            content = super().read_optional(folder, name)
            return None if content is None else build_workbook([SITE_COLUMN, *COLUMNS], [])

    store = Lossy()
    handler = _handlers(store)[RONGHUI_ACTION]
    with pytest.raises(PluginExecutionError) as error:
        handler(_ronghui(), {"target_date": "2026-11-24", "columns": COLUMNS, "rows": [["R1", "2026-11-24", 1, None]]})
    assert error.value.code == "WRITE_OUTCOME_UNKNOWN"


def test_concurrent_accounts_do_not_lose_each_others_rows():
    store = MemoryStore()
    handler = _handlers(store, {f"a{i}": f"站{i}" for i in range(6)})[RONGHUI_ACTION]
    threads = [threading.Thread(target=handler, args=(_ronghui(f"a{i}"), {
        "target_date": "2026-11-25", "columns": COLUMNS, "rows": [[f"W{i}", "2026-11-25", i, None]]}))
        for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    _columns, rows = read_workbook(store.files[("融辉寄件", "2026-11-25.xlsx")])
    assert sorted(row[1] for row in rows) == [f"W{i}" for i in range(6)]


def test_merge_keeps_other_sites_by_header_name_and_rejects_unsited_files():
    existing = ([SITE_COLUMN, "备注", "运单编号"], [["乙站", "旧", "S1"]])
    columns, rows = merge_site_rows(existing, columns=["运单编号", "件数"], rows=[["R1", 1]], site="甲站")
    assert columns == [SITE_COLUMN, "运单编号", "件数"] and rows == [["乙站", "S1", None], ["甲站", "R1", 1]]
    with pytest.raises(WorkbookError):
        merge_site_rows((["运单编号"], [["X"]]), columns=["运单编号"], rows=[], site="甲站")
    with pytest.raises(WorkbookError):
        check_table(["a", "a"], [])
    with pytest.raises(WorkbookError):
        check_table(["a"], [[True]])


def test_store_paths_reject_traversal_and_need_an_account():
    with pytest.raises(SendDataNasError):
        SendDataNasStore("http://127.0.0.1:9/寄件数据", "", "")
    store = SendDataNasStore("http://127.0.0.1:9/寄件数据", "u", "p")
    for bad in ("..", "a/b", ".hidden", "a:b"):
        with pytest.raises(SendDataNasError) as error:
            store.read_optional(bad, "x.xlsx")
        assert error.value.code == "NAS_PATH_INVALID"
    with pytest.raises(SendDataNasError) as offline:
        store.read_optional("融辉寄件", "2026-11-24.xlsx")
    assert offline.value.code == "NAS_UNAVAILABLE" and "127.0.0.1" not in str(offline.value)


def test_production_store_settings_use_their_own_keys():
    from plugin_core_adapters.send_data_export import store_from_settings

    with pytest.raises(SendDataNasError):
        store_from_settings(None)
    with pytest.raises(SendDataNasError):
        store_from_settings({"RECEIPT_NAS_URL": "http://127.0.0.1:9", "RECEIPT_NAS_RW_USER": "u",
                             "RECEIPT_NAS_RW_PASS": "p"})
    store: Any = store_from_settings({"RECEIPT_NAS_URL": "http://127.0.0.1:9", "SEND_DATA_NAS_USER": "u",
                                      "SEND_DATA_NAS_PASS": "p", "SEND_DATA_NAS_FOLDER": "/NAS资料/寄件数据"})
    assert store._base.endswith("/NAS%E8%B5%84%E6%96%99/%E5%AF%84%E4%BB%B6%E6%95%B0%E6%8D%AE")


def test_store_delete_sends_webdav_delete_and_accepts_an_already_gone_file():
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_DELETE(self):  # noqa: N802 - http.server naming
            seen.append((self.path, self.headers.get("Authorization", "").startswith("Basic ")))
            self.send_response(204 if len(seen) == 1 else 404)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        store = SendDataNasStore(f"http://127.0.0.1:{server.server_port}/团队文件-寄件数据", "u", "p")
        store.delete("韵达寄件", "2026-11-26.xlsx")
        store.delete("韵达寄件", "2026-11-26.xlsx")  # already gone: fine
    finally:
        server.shutdown()
    expected = "/%E5%9B%A2%E9%98%9F%E6%96%87%E4%BB%B6-%E5%AF%84%E4%BB%B6%E6%95%B0%E6%8D%AE/%E9%9F%B5%E8%BE%BE%E5%AF%84%E4%BB%B6/2026-11-26.xlsx"
    assert seen == [(expected, True), (expected, True)]
