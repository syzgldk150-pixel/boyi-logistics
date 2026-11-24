"""Production ports of the NAS send-data workbook primitives.

The NAS account comes from the deployment's NAS settings file (the same file
as the customer share, ``~/.boyi-receipt-share/nas.env`` by default) under its
own ``SEND_DATA_NAS_*`` keys, read when a workbook is written, so a missing or
changed account is reported on that run instead of at start-up. The Ronghui
site name is the bound account's own login site from its saved session.
"""
from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import quote

from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.send_data_export_handlers import (
    SendDataExportPorts, build_send_data_export_handler_map,
)
from agent.tms_runtime.account_manager import AutomationAccountManager
from agent.tms_runtime.errors import TMSAuthStateError
from shared.receipt_share_settings import load_share_settings
from shared.send_data_nas_store import SendDataNasError, SendDataNasStore

DEFAULT_FOLDER = "/NAS资料/寄件数据"


def store_from_settings(settings: Mapping[str, str] | None) -> SendDataNasStore:
    if not settings:
        raise SendDataNasError("NAS_CONFIG_INVALID", "服务器上没有 NAS 配置文件，寄件数据无法写入 NAS。")
    base = str(settings.get("SEND_DATA_NAS_URL") or settings.get("RECEIPT_NAS_URL") or "").rstrip("/")
    folder = str(settings.get("SEND_DATA_NAS_FOLDER") or DEFAULT_FOLDER)
    if not base:
        raise SendDataNasError("NAS_CONFIG_INVALID", "寄件数据的 NAS 地址未配置。")
    return SendDataNasStore(base + "/".join(quote(part, safe="") for part in folder.split("/")),
                            str(settings.get("SEND_DATA_NAS_USER") or ""), str(settings.get("SEND_DATA_NAS_PASS") or ""))


def _describe(manager: AutomationAccountManager, account_id: str, system: str) -> Mapping[str, Any]:
    try:
        descriptor = manager.require_active_binding_descriptor(account_id)
    except TMSAuthStateError as exc:
        raise PluginExecutionError("the exact bound account is unavailable", code="BROKER_ACCOUNT_UNAVAILABLE") from exc
    if str(descriptor.get("system") or "").strip().lower() != system:
        raise PluginExecutionError("the bound account belongs to another platform", code="BROKER_ACCOUNT_SYSTEM_MISMATCH")
    return descriptor


def _ronghui_site_name(descriptor: Mapping[str, Any]) -> str:
    from agent.tms_runtime.ronghui_user_context import parse_ronghui_user_info_cookie
    from agent.tms_runtime.scripts import Send_order

    profile = str(descriptor.get("session_profile") or "").strip()
    if not profile:
        raise PluginExecutionError("the Ronghui account has no session profile", code="BLOCKED_LOGIN")
    session = Send_order.TMSAuth(profile=profile).login_and_get_session()
    if session is None:
        raise PluginExecutionError("Ronghui login did not return a session", code="BLOCKED_LOGIN")
    names = set()
    for cookie in session.cookies:
        if cookie.name == "userInfo" and "ronghuiwl.com" in str(cookie.domain or ""):
            name = str(parse_ronghui_user_info_cookie(cookie.value).get("loginSiteName") or "").strip()
            if name:
                names.add(name)
    if len(names) != 1:
        raise PluginExecutionError("融辉账号登录信息里没有唯一的网点名称，请到“业务账号”重新登录。", code="BLOCKED_LOGIN")
    return names.pop()


def build_production_send_data_export_handler_map(*, cursor_secret: bytes, account_manager: AutomationAccountManager):
    return build_send_data_export_handler_map(SendDataExportPorts(
        describe_account=lambda account_id, system: _describe(account_manager, account_id, system),
        ronghui_site_name=_ronghui_site_name,
        open_store=lambda: store_from_settings(load_share_settings()),
    ), cursor_secret=cursor_secret)


__all__ = ["build_production_send_data_export_handler_map", "store_from_settings"]
