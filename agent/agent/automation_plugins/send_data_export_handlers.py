"""Reviewed primitives that keep the daily send-data workbooks on the NAS.

The plugins decide the columns and the row contents of their own snapshot.
The Host only fixes where it goes (one ``<平台文件夹>/<发件日期>.xlsx`` per
platform), fills the ``来源网点`` cell of Ronghui rows from the bound account's
own login site so the two Ronghui accounts share one workbook without touching
each other's rows, and confirms the write by reading the workbook back.

A day with no rows has no file (用户 2026-11-27: 当天没有寄件就不放空文件): when the
merged workbook would be empty nothing is written, and a file left from an earlier run of
that day is removed and read back as gone.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import threading
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Mapping

from agent.automation_plugins.core_adapter import CoreBrokerHandler, CoreBrokerInvocationContext
from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.connector_namespaces import DAILY_SEND_TOOL, YUNDA_SEND_TOOL
from shared.send_data_nas_store import SendDataNasError
from shared.send_data_workbook import (
    SITE_COLUMN, WorkbookError, build_workbook, check_table, merge_site_rows, read_workbook, same_table,
)

RONGHUI_ACTION = ("network.request", "nas.send_data.ronghui.replace_site")
YUNDA_ACTION = ("network.request", "nas.send_data.yunda.replace_date")
RONGHUI_FOLDER = "融辉寄件"
YUNDA_FOLDER = "韵达寄件"
_ROLE = "account_id"
_TARGETS = {
    RONGHUI_ACTION: (DAILY_SEND_TOOL, "ronghui", RONGHUI_FOLDER),
    YUNDA_ACTION: (YUNDA_SEND_TOOL, "yunda", YUNDA_FOLDER),
}
MARKED_WRITE_ACTION_KEYS = frozenset(_TARGETS)


@dataclass(frozen=True)
class SendDataExportPorts:
    describe_account: Callable[[str, str], Mapping[str, Any]]
    ronghui_site_name: Callable[[Mapping[str, Any]], str]
    open_store: Callable[[], Any]


def _error(message: str, code: str) -> PluginExecutionError:
    return PluginExecutionError(message, code=code)


_FILE_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


def _file_lock(folder: str, name: str) -> threading.Lock:
    with _FILE_LOCKS_GUARD:
        return _FILE_LOCKS.setdefault((folder, name), threading.Lock())


class _SendDataExportHandlers:
    def __init__(self, ports: SendDataExportPorts, *, secret: bytes) -> None:
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError("send-data export broker secret must contain at least 32 bytes")
        self._ports = ports
        self._secret = bytes(secret)

    def _evidence(self, context: CoreBrokerInvocationContext, proof: Mapping[str, Any]) -> str:
        body = canonical_json_bytes({
            "context": {"automation_id": context.automation_id, "plugin_version": context.plugin_version,
                        "tool_name": context.tool_name},
            "purpose": "send-data-workbook", "payload": dict(proof),
        })
        signature = hmac.new(self._secret, body, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(hashlib.sha256(body).digest() + signature).decode("ascii").rstrip("=")

    def _account(self, context: CoreBrokerInvocationContext, system: str) -> Mapping[str, Any]:
        if len(context.account_ids) != 1:
            raise _error("send-data export requires one exact account", "BROKER_CONTEXT_INVALID")
        account_id = str(context.account_ids[0]).strip()
        bound = tuple(str(value).strip() for value in context.account_bindings.get(_ROLE, ()))
        if not account_id or bound != (account_id,):
            raise _error("send-data export account binding changed", "BROKER_CONTEXT_INVALID")
        descriptor = self._ports.describe_account(account_id, system)
        if (not isinstance(descriptor, Mapping) or str(descriptor.get("account_id") or "").strip() != account_id
                or str(descriptor.get("system") or "").strip().lower() != system):
            raise _error("send-data export account descriptor changed", "BROKER_ACCOUNT_INVALID")
        return descriptor

    def _replace(self, key: tuple[str, str], context: CoreBrokerInvocationContext,
                 arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        tool, system, folder = _TARGETS[key]
        if (context.tool_name, context.operation, context.action, context.role) != (tool, *key, _ROLE):
            raise _error("send-data export broker context is invalid", "BROKER_CONTEXT_INVALID")
        if not isinstance(arguments, Mapping) or set(arguments) != {"target_date", "columns", "rows"}:
            raise _error("send-data export arguments are invalid", "BROKER_ARGUMENT_INVALID")
        try:
            target_date = date.fromisoformat(str(arguments["target_date"])).isoformat()
        except ValueError:
            raise _error("send-data export date is invalid", "BROKER_ARGUMENT_INVALID") from None
        if target_date != arguments["target_date"]:
            raise _error("send-data export date is invalid", "BROKER_ARGUMENT_INVALID")
        try:
            columns, rows = check_table(arguments["columns"], arguments["rows"])
        except WorkbookError as exc:
            raise _error(f"寄件数据表格无效：{exc}", "BROKER_ARGUMENT_INVALID") from None
        if system == "ronghui" and SITE_COLUMN in columns:
            raise _error("寄件数据表格无效：来源网点列由系统填写。", "BROKER_ARGUMENT_INVALID")
        descriptor = self._account(context, system)
        site = None
        if system == "ronghui":
            site = str(self._ports.ronghui_site_name(descriptor) or "").strip()
            if not site:
                raise _error("融辉账号登录信息里没有网点名称，请到“业务账号”重新登录。", "BLOCKED_LOGIN")
        name = f"{target_date}.xlsx"
        try:
            store = self._ports.open_store()
        except SendDataNasError as exc:
            raise _error(f"写入 NAS 寄件数据失败：{exc}", "BROKER_RESOURCE_UNAVAILABLE") from None
        with _file_lock(folder, name):
            try:
                previous = store.read_optional(folder, name)
                existing = read_workbook(previous) if previous is not None else None
                expected = merge_site_rows(existing, columns=columns, rows=rows, site=site)
                content = build_workbook(*expected)
            except SendDataNasError as exc:
                raise _error(f"读取 NAS 上的寄件数据失败：{exc}", "BROKER_RESOURCE_UNAVAILABLE") from None
            except WorkbookError as exc:
                raise _error(f"NAS 上的 {folder}/{name} 无法合并：{exc}", "BROKER_RESOURCE_UNAVAILABLE") from None
            if not expected[1]:
                return self._no_rows(context, folder=folder, name=name, target_date=target_date, site=site,
                                     store=store, present=previous is not None)
            if context.mark_write_started is not None:
                context.mark_write_started()
            try:
                store.write(folder, name, content)
                readback = store.read_optional(folder, name)
                observed = read_workbook(readback) if readback is not None else None
            except (SendDataNasError, WorkbookError) as exc:
                raise _error(f"写入 NAS 寄件数据后未能读回确认：{exc}", "WRITE_OUTCOME_UNKNOWN") from None
        if observed is None or not same_table(observed, expected):
            raise _error("写入 NAS 寄件数据后读回的内容不一致。", "WRITE_OUTCOME_UNKNOWN")
        proof = {
            "target_date": target_date,
            "file": f"{folder}/{name}",
            "site": site or "",
            "record_count": len(rows),
            "file_record_count": len(expected[1]),
            "readback_sha256": hashlib.sha256(readback).hexdigest(),
        }
        return {"committed": True, "verified": True, **proof, "evidence_ref": self._evidence(context, proof)}

    def _no_rows(self, context: CoreBrokerInvocationContext, *, folder: str, name: str, target_date: str,
                 site: str | None, store: Any, present: bool) -> Mapping[str, Any]:
        """No sends that day: no file. An earlier file of that day is deleted and read back as gone."""
        if present:
            if context.mark_write_started is not None:
                context.mark_write_started()
            try:
                store.delete(folder, name)
                gone = store.read_optional(folder, name) is None
            except SendDataNasError as exc:
                raise _error(f"删除 NAS 上当天无寄件的 {folder}/{name} 后未能读回确认：{exc}", "WRITE_OUTCOME_UNKNOWN") from None
            if not gone:
                raise _error(f"删除 NAS 上当天无寄件的 {folder}/{name} 后文件仍在。", "WRITE_OUTCOME_UNKNOWN")
        proof = {"target_date": target_date, "file": "", "site": site or "", "record_count": 0,
                 "file_record_count": 0, "readback_sha256": ""}
        return {"committed": True, "verified": True, **proof, "evidence_ref": self._evidence(context, proof)}

    def handler_map(self) -> dict[tuple[str, str], CoreBrokerHandler]:
        return {key: (lambda context, arguments, _key=key: self._replace(_key, context, arguments))
                for key in _TARGETS}


def build_send_data_export_handler_map(ports: SendDataExportPorts, *, cursor_secret: bytes
                                       ) -> dict[tuple[str, str], CoreBrokerHandler]:
    return _SendDataExportHandlers(ports, secret=cursor_secret).handler_map()


__all__ = [
    "MARKED_WRITE_ACTION_KEYS", "RONGHUI_ACTION", "RONGHUI_FOLDER", "SendDataExportPorts", "YUNDA_ACTION", "YUNDA_FOLDER",
    "build_send_data_export_handler_map",
]
