import base64
import cgi
import contextvars
import hashlib
import hmac
import html
import io
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, unquote, urlparse
from urllib.request import Request, urlopen
from http.cookies import SimpleCookie
from jinja2 import Environment, FileSystemLoader, select_autoescape

from console.config import MODULE_DIR, PROJECT_ROOT, load_settings
from console.runtime_config import load_console_environment
from console.database import (
    DocumentRepository,
    WAYBILL_SOURCE_LABELS,
    WAYBILL_STATUS_LABELS,
    WAYBILL_STATUS_TONES,
    normalize_waybill_status,
)
from console.line_haul_contacts import normalize_phone_numbers, parse_line_haul_paste
from console.ocr_providers import build_qwen_provider
from console.preprocessing import new_doc_token, preprocess_document, sanitize_filename, write_bytes
from console.routes import ConsoleRouteDispatcher
from console.task_queue import DocumentTaskQueue
from console.template_store import TemplateStore

from console.finance_service import FinanceError, FinanceService, FinanceValidationError
from shared.redaction import redact_sensitive, redact_text
from shared.yunda_console_waybill import build_console_waybill_from_yunda_data

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


LOGGER = logging.getLogger(__name__)

ADMIN_SESSION_COOKIE = "docflow_admin_session"
ADMIN_PASSWORD_ALGORITHM = "pbkdf2_sha256"
ADMIN_PASSWORD_ITERATIONS = 260_000
ADMIN_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{3,64}$")
AVATAR_MAX_BYTES = 2 * 1024 * 1024
AVATAR_ALLOWED_EXTENSIONS = {".gif", ".jpeg", ".jpg", ".png", ".webp"}
CUSTOMER_SERVICE_RESOURCE_KEY = "customer_service.problem_settings"
CUSTOMER_SERVICE_ALLOWED_ACCOUNT_SYSTEMS = {"ronghui", "yunda"}
CUSTOMER_SERVICE_DEFAULT_SETTINGS = {
    "ronghui_account_ids": [],
    "yunda_account_ids": [],
    "poll_interval_sec": 60,
}
CUSTOMER_SERVICE_SITE_FILTER_LOGIN = "739010002"
CUSTOMER_SERVICE_SITE_FILTER_SITE = "邵阳操作场"
CUSTOMER_SERVICE_PUBLISH_SITE_KEYS = (
    "REGISTER_SITE",
    "register_site",
    "REGISTER_SITE_NAME",
    "register_site_name",
    "site_id",
    "site_name",
    "publish_site",
    "publisher_site",
)
CUSTOMER_SERVICE_NOTIFIED_SITE_KEYS = (
    "SEND_SITE",
    "send_site",
    "SEND_SITE_NAME",
    "send_site_name",
    "recv_site_id",
    "notice_site",
    "notify_site",
    "notified_site",
    "rec_comp",
    "inform_site_name",
)
_CURRENT_ADMIN_USER: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "current_admin_user",
    default=None,
)


def current_admin_user() -> dict[str, Any] | None:
    return _CURRENT_ADMIN_USER.get()


def hash_admin_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        str(password or "").encode("utf-8"),
        salt.encode("ascii"),
        ADMIN_PASSWORD_ITERATIONS,
    ).hex()
    return f"{ADMIN_PASSWORD_ALGORITHM}${ADMIN_PASSWORD_ITERATIONS}${salt}${digest}"


def verify_admin_password(password: str, password_hash: str) -> bool:
    parts = str(password_hash or "").split("$")
    if len(parts) != 4:
        return False
    algorithm, iterations_raw, salt, expected_digest = parts
    if algorithm != ADMIN_PASSWORD_ALGORITHM:
        return False
    try:
        iterations = int(iterations_raw)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        str(password or "").encode("utf-8"),
        salt.encode("ascii"),
        iterations,
    ).hex()
    return hmac.compare_digest(digest, expected_digest)


@dataclass
class UploadItem:
    filename: str
    source_relpath: str
    payload: bytes


@dataclass
class ProcessingResult:
    document_id: int
    status: str
    error_message: str = ""


@dataclass
class ActionResult:
    ok: bool
    message: str
    waybill_id: int | None = None
    waybill_no: str = ""


@dataclass(frozen=True)
class ProjectModule:
    slug: str
    name: str
    status: str
    summary: str
    code_path: str
    docs_path: str
    route: str
    workspace_path: str
    current_focus: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    dependencies: tuple[str, ...]
    consumers: tuple[str, ...]
    commands: tuple[str, ...]


QUALITY_ISSUE_LABELS = {
    "blurred": "图片模糊",
    "too_dark": "图片过暗",
    "too_bright": "图片过曝",
    "document_too_small": "单据在画面中占比过小",
    "document_not_detected": "未稳定检测到单据边界",
}

MONEY_FIELD_NAMES = {
    "freight_fee",
    "pickup_fee",
    "delivery_fee",
    "transfer_fee",
    "insurance_amount",
    "cod_amount",
}

MANUAL_EXTRA_FIELD_LABELS = {
    "insurance_amount": "保价金额",
    "cod_amount": "代收金额",
}

AUTOMATION_RESOURCE_NOTES = {
    "phase7.send_order_bitable": "当日寄件数据的多维表格配置。",
    "phase7.delivery_status_bitable": "查询并更新签收状态定时扫描的多维表格配置。",
    "phase7.delivery_status_webhook": "旧版签收状态工作流的兼容 webhook 映射。",
    "phase7.price_query_webhook": "旧版价格查询工作流的兼容 webhook 映射。",
    "phase7.r13_credentials": "每日应签使用的 R13 独立账号配置；不使用顶部共享 TMS 登录态。",
    "phase7.daily_sign_bitable": "每日应签数据的多维表格配置。",
    "phase7.daily_sign_sheet": "每日应签结果写入表格配置。",
    "phase7.site_send_bitable": "网点出港清单的多维表格配置。",
    "phase7.site_send_sheet": "网点出港清单结果写入表格配置。",
    "phase7.arrive_primary_sheet": "到货清单主表写入配置。",
    "phase7.arrive_secondary_sheet": "到货清单备用表写入配置。",
    "phase7.yunda_dispatch_forecast_bitable": "韵达网点派件量预测主单表的多维表格配置。",
    "phase7.yunda_send_waybills_bitable": "韵达寄件运单管理数据的多维表格配置。",
    "phase7.yunda_send_waybills_sheet": "韵达寄件运单管理数据的普通电子表格副本配置；默认从第 2 行起清空后重写。",
    "phase7.scan_webhook": "旧版扫描数据工作流的兼容 webhook 映射。",
    "phase7.scan_flow_webhook": "扫描后续流程 webhook 配置。",
    "phase7.stats_webhook": "旧版到货统计工作流的兼容 webhook 映射。",
    "phase7.stats_archive_sheet": "到货统计归档表配置。",
    "phase7.split_pending_source_sheet": "分批及有发未到问题件来源表；固定读取每日到货表 A:S。",
    "phase7.split_pending_target_sheet": "分批及有发未到表当前快照；固定保留 A:S 表头和未齐运单。",
    "phase7.stats_flow_webhook": "到货统计后续流程 webhook 配置。",
}

AUTOMATION_RESOURCE_JSON_EXAMPLES = {
    "phase7.send_order_bitable": {
        "base_token": "appxxxxxxxx",
        "table_id": "tblxxxxxxxx",
    },
    "phase7.delivery_status_bitable": {
        "base_token": "Fcm8b2H7wayK1UsYLjlcFmWhnMh",
        "table_id": "tblX96gGAuBfJrtW",
        "view_name": "未签收明细",
        "view_id": "veweDmbdIS",
    },
    "phase7.delivery_status_webhook": {
        "path": "webhook/sign-status",
    },
    "phase7.price_query_webhook": {
        "url": "https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx",
    },
    "phase7.r13_credentials": {
        "username": "R13账号",
        "password": "R13密码",
        "disp_site_code": "7390004",
        "days": 7,
    },
    "phase7.daily_sign_bitable": {
        "base_token": "appxxxxxxxx",
        "table_id": "tblxxxxxxxx",
    },
    "phase7.daily_sign_sheet": {
        "spreadsheet_token": "shtxxxxxxxx",
        "range": "Sheet1!A2:G200",
        "clear_range": "Sheet1!A2:H200",
    },
    "phase7.site_send_bitable": {
        "base_token": "appxxxxxxxx",
        "table_id": "tblxxxxxxxx",
    },
    "phase7.site_send_sheet": {
        "spreadsheet_token": "shtxxxxxxxx",
        "range": "Sheet1!A2:Q200",
        "clear_range": "Sheet1!A2:Q200",
    },
    "phase7.arrive_primary_sheet": {
        "spreadsheet_token": "shtxxxxxxxx",
        "range": "Sheet1!A2:R200",
        "clear_range": "Sheet1!A2:R200",
        "title_range": "Sheet1!A1:R1",
    },
    "phase7.arrive_secondary_sheet": {
        "spreadsheet_token": "shtxxxxxxxx",
        "range": "Sheet1!A2:R200",
        "clear_range": "Sheet1!A2:R200",
        "title_range": "Sheet1!A1:R1",
    },
    "phase7.yunda_dispatch_forecast_bitable": {
        "base_token": "Et8sboZiSahfhYsa0i3c6hkwnXg",
        "table_id": "tblT43ay2KjeXdC0",
    },
    "phase7.yunda_send_waybills_bitable": {
        "base_token": "Fcm8b2H7wayK1UsYLjlcFmWhnMh",
        "table_id": "tblNHfIVVeaTBB7Y",
    },
    "phase7.yunda_send_waybills_sheet": {
        "spreadsheet_token": "GILYss6KhhBBuRt9FPWcXbben7c",
        "range": "Sheet1!A2:A2",
        "clear_range": "Sheet1!A2:Y5000",
    },
    "phase7.scan_webhook": {
        "path": "webhook/phase7/scan",
    },
    "phase7.scan_flow_webhook": {
        "url": "https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx",
    },
    "phase7.stats_webhook": {
        "path": "webhook/phase7/stats",
    },
    "phase7.stats_archive_sheet": {
        "spreadsheet_token": "shtxxxxxxxx",
        "default_write_range": "A1:S199",
    },
    "phase7.split_pending_source_sheet": {
        "spreadsheet_token": "F0NVsI5dlhaWugtw14YcmdrQnvh",
        "sheet_id": "8fc516",
        "range": "8fc516!A1:S5000",
    },
    "phase7.split_pending_target_sheet": {
        "spreadsheet_token": "F0NVsI5dlhaWugtw14YcmdrQnvh",
        "sheet_id": "bNhh7u",
        "range": "bNhh7u!A1:S1",
        "clear_range": "bNhh7u!A2:S5000",
    },
    "phase7.stats_flow_webhook": {
        "url": "https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx",
    },
}

YUNDA_ENTRY_ACTIONS = {
    "/ocr/yunda/bootstrap": "bootstrap",
    "/ocr/yunda/get-logistics-num": "get-logistics-num",
    "/ocr/yunda/address-analysis": "address-analysis",
    "/ocr/yunda/address-resolution": "address-resolution",
    "/ocr/yunda/quote-checks": "quote-checks",
    "/ocr/yunda/feedback/address": "feedback/address",
    "/ocr/yunda/feedback/cost": "feedback/cost",
    "/ocr/yunda/feedback/cost/upload": "feedback/cost/upload",
    "/ocr/yunda/return-upload": "return-upload",
    "/ocr/yunda/download-template": "download-template",
    "/ocr/yunda/save": "save",
    "/ocr/yunda/drafts/save": "drafts/save",
    "/ocr/yunda/drafts/list": "drafts/list",
    "/ocr/yunda/drafts/load": "drafts/load",
    "/ocr/yunda/drafts/delete": "drafts/delete",
    "/ocr/yunda/templates/save": "templates/save",
    "/ocr/yunda/templates/list": "templates/list",
    "/ocr/yunda/templates/load": "templates/load",
    "/ocr/yunda/templates/delete": "templates/delete",
    "/ocr/yunda/templates/set-default": "templates/set-default",
    "/ocr/yunda/print/child": "print/child",
    "/ocr/yunda/print/master": "print/master",
    "/ocr/yunda/print/triplicate": "print/triplicate",
    "/ocr/yunda/print/receipt-label": "print/receipt-label",
}

YUNDA_LIVE_PROXY_PREFIX = "/ocr/yunda/live"
YUNDA_LIVE_ENTRY_PATH = "/ky_inms/public/index.php/business/waybill/entry/indexNew.html"
YUNDA_LIVE_SAVE_PATH = "/ky_inms/public/index.php/business/waybill/entry/save.html"
YUNDA_RECEIPT_LIVE_PROXY_PREFIX = "/receipts/yunda/live"
YUNDA_RECEIPT_LIVE_ENTRY_PATH = "/ky_inms/public/index.php/business/waybill/mailing/index.html"
RONGHUI_LIVE_PROXY_PREFIX = "/ocr/ronghui/live"
RONGHUI_LIVE_SAVE_PATH = "/dataOperation/saveTables"
RONGHUI_RECEIPT_LIVE_PROXY_PREFIX = "/receipts/ronghui/live"
RONGHUI_RECEIPT_ENTRY_MENU_TEXTS = {
    "send": "寄方回单跟踪",
    "receive": "派方回单处理",
}
RECEIPT_QUERY_AGENT_TIMEOUT_SEC = 75
RECEIPT_QUERY_SOURCE_TIMEOUT_SEC = 12
RECEIPT_QUERY_MAX_PAGES = 5
RECEIPT_DETAIL_KEYS = (
    "recipient_name",
    "recipient_address",
    "goods_name",
    "package_type",
    "piece_count",
    "actual_weight",
    "volume",
    "waybill_no",
)
RECEIPT_DETAIL_REQUIRED_KEYS = set(RECEIPT_DETAIL_KEYS)
RECEIPT_FEISHU_BASE_TOKEN = "Fcm8b2H7wayK1UsYLjlcFmWhnMh"
RECEIPT_FEISHU_TABLE_ID = "tblX96gGAuBfJrtW"
RECEIPT_FEISHU_VIEW_ID = "veweDmbdIS"
RECEIPT_FEISHU_WAYBILL_FIELD = "运单编号"
RECEIPT_FEISHU_FIELD_MAP = {
    "recipient_name": ("收货人", "收件人", "收货客户"),
    "recipient_address": ("收件地址", "收货地址", "地址"),
    "goods_name": ("货物名称", "品名", "托寄物"),
    "package_type": ("包装类型", "包装", "包装方式"),
    "piece_count": ("件数", "数量"),
    "actual_weight": ("实际重量",),
    "volume": ("体积",),
    "waybill_no": (RECEIPT_FEISHU_WAYBILL_FIELD, "运单号", "运单号码"),
}
RECEIPT_FEISHU_FIELD_NAMES = tuple(
    dict.fromkeys(field_name for names in RECEIPT_FEISHU_FIELD_MAP.values() for field_name in names)
)
RONGHUI_LIVE_ALLOWED_PREFIXES = (
    "/widget/",
    "/static/",
    "/dataQuery/",
    "/dataOperation/",
    "/minic/",
    "/address/",
    "/advancePayment/",
    "/commonOption/",
    "/fhdquote/",
    "/file/",
    "/map/",
    "/userView/",
    "/unauth/download/",
    "/menuTreeExtend/",
    "/module/",
)
RONGHUI_DIRECT_ATTACHMENT_HOSTS = {"rhk13.obs.cn-east-3.myhuaweicloud.com"}
RECEIPT_ATTACHMENT_MAX_BYTES = 20 * 1024 * 1024
RECEIPT_IMAGE_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".webp"}
RECEIPT_REMARK_TOKEN_RE = re.compile(r"(?<!\d)(\d{4})-(\d{10})(?!\d)")
RECEIPT_REMARK_KEY_FRAGMENTS = ("note", "remark", "memo", "comment", "备注")

AUTOMATION_TASK_RESOURCE_REQUIREMENTS = {
    "send_order": [
        {"resource_key": "phase7.send_order_bitable", "required": True},
    ],
    "delivery_status": [
        {"resource_key": "phase7.delivery_status_bitable", "required": True},
        {"resource_key": "phase7.delivery_status_webhook", "required": False},
    ],
    "daily_sign": [
        {"resource_key": "phase7.daily_sign_bitable", "required": True},
        {"resource_key": "phase7.daily_sign_sheet", "required": True},
    ],
    "site_send": [
        {"resource_key": "phase7.site_send_bitable", "required": True},
        {"resource_key": "phase7.site_send_sheet", "required": True},
    ],
    "arrive_list": [
        {"resource_key": "phase7.arrive_primary_sheet", "required": True},
        {"resource_key": "phase7.arrive_secondary_sheet", "required": True},
    ],
    "yunda_dispatch_forecast": [
        {"resource_key": "phase7.yunda_dispatch_forecast_bitable", "required": False},
    ],
    "yunda_send_waybills": [
        {"resource_key": "phase7.yunda_send_waybills_bitable", "required": False},
        {"resource_key": "phase7.yunda_send_waybills_sheet", "required": False},
    ],
    "scan_codes": [
        {"resource_key": "phase7.scan_webhook", "required": False},
        {"resource_key": "phase7.scan_flow_webhook", "required": False},
    ],
    "arrival_stats": [
        {"resource_key": "phase7.stats_webhook", "required": False},
        {"resource_key": "phase7.arrive_primary_sheet", "required": True},
        {"resource_key": "phase7.arrive_secondary_sheet", "required": True},
        {"resource_key": "phase7.stats_archive_sheet", "required": True},
        {"resource_key": "phase7.stats_flow_webhook", "required": False},
    ],
    "split_pending_problem_upload": [
        {"resource_key": "phase7.split_pending_source_sheet", "required": True},
        {"resource_key": "phase7.split_pending_target_sheet", "required": True},
    ],
}

AUTOMATION_TASK_NOTES = {
    "send_order": "夜间同步当日寄件数据。",
    "send_order_2150": "夜间同步当日寄件数据。",
    "clockin_daxiang": "大祥站双打卡自动化任务。",
    "clockin_daxiang_1830": "大祥站双打卡自动化任务。",
    "clockin_daxiang_s": "大祥 S 站双打卡自动化任务。",
    "clockin_daxiang_s_1830": "大祥 S 站双打卡自动化任务。",
    "r7_arrival_checkin": "R7 到达打卡任务；使用 R7 登录，不依赖顶部 TMS 登录态。",
    "r7_departure_checkin": "R7 发车打卡任务；使用 R7 登录，不依赖顶部 TMS 登录态。",
    "daily_sign": "每日应签同步任务。",
    "daily_sign_0500": "每日应签同步任务。",
    "daily_sign_0700": "每日应签同步任务。",
    "daily_sign_0900": "每日应签同步任务。",
    "daily_sign_1400": "每日应签同步任务。",
    "daily_sign_1530": "每日应签同步任务。",
    "daily_sign_1800": "每日应签同步任务。",
    "site_send": "网点出港清单同步任务。",
    "site_send_0500": "网点出港清单同步任务。",
    "site_send_0530": "网点出港清单同步任务。",
    "site_send_1800": "网点出港清单同步任务。",
    "site_send_1900": "网点出港清单同步任务。",
    "site_send_1930": "网点出港清单同步任务。",
    "site_send_2000": "网点出港清单同步任务。",
    "site_send_2030": "网点出港清单同步任务。",
    "site_send_2100": "网点出港清单同步任务。",
    "arrive_list": "TMS 派件预报基础清单同步任务。",
    "arrive_list_0830": "TMS 派件预报基础清单同步任务。",
    "arrive_list_0930": "TMS 派件预报基础清单同步任务。",
    "yunda_dispatch_forecast": "韵达网点派件量预测主单表同步任务。",
    "yunda_dispatch_forecast_1700": "韵达网点派件量预测主单表同步任务。",
    "yunda_send_waybills": "韵达寄件运单管理同步任务。",
    "self_pickup_problem_upload": "自提到货问题件；默认先预览候选单号。",
    "split_pending_problem_upload": "刷新当前未齐快照并上传少货/分批或有发未到问题件。",
}

AUTOMATION_WORKFLOW_CATALOG = [
    {
        "task_id": "send_order",
        "display_name": "获取当日寄件数据",
        "tool_name": "sync_daily_send_orders",
        "note": "夜间同步当日寄件数据。",
        "task_mode": "scheduled",
        "trigger_label": "定时任务",
        "schedule_summary": "",
        "default_tool_params": {
            "target_date": "",
        },
        "account_roles": [
            {"label": "运行账号", "field": "account_id", "system": "ronghui", "default_account_id": "ronghui_default"},
        ],
        "order": 10,
    },
    {
        "task_id": "delivery_status",
        "display_name": "查询并更新签收状态",
        "tool_name": "sync_delivery_status",
        "note": "定时扫描寄件数据表中未签收记录，查询最新签收状态，已签收则写回飞书多维表格；仍兼容旧 webhook 入参。",
        "task_mode": "scheduled",
        "trigger_label": "定时任务 / 手动执行 / Webhook",
        "schedule_summary": "",
        "webhook_resource_key": "phase7.delivery_status_webhook",
        "webhook_fallback_path": "/webhook/sign-status",
        "webhook_body_template": {
            "BILL_CODE": "YS20260401001",
            "RECORD_ID": "recxxxxxxxxxxxxxxxx",
        },
        "default_tool_params": {},
        "account_roles": [
            {"label": "运行账号", "field": "account_id", "system": "ronghui", "default_account_id": "ronghui_default"},
        ],
        "order": 20,
    },
    {
        "task_id": "daily_sign",
        "display_name": "每日应签",
        "tool_name": "sync_daily_should_sign",
        "note": "每日应签同步任务。",
        "task_mode": "scheduled",
        "trigger_label": "定时任务",
        "schedule_summary": "",
        "default_tool_params": {},
        "account_roles": [
            {"label": "R13应签查询账号", "field": "r13_account_id", "system": "r13", "default_account_id": "r13_default"},
            {"label": "补地址账号", "field": "detail_account_id", "system": "ronghui", "default_account_id": "ronghui_default"},
        ],
        "order": 40,
    },
    {
        "task_id": "site_send",
        "display_name": "网点出港清单",
        "tool_name": "sync_site_send_list",
        "note": "网点出港清单同步任务。",
        "task_mode": "scheduled",
        "trigger_label": "定时任务",
        "schedule_summary": "",
        "default_tool_params": {},
        "account_roles": [
            {"label": "运行账号", "field": "account_id", "system": "ronghui", "default_account_id": "ronghui_default"},
        ],
        "order": 50,
    },
    {
        "task_id": "clockin_daxiang",
        "display_name": "网点打卡-大祥",
        "tool_name": "tms_query",
        "note": "大祥站双打卡自动化任务。",
        "task_mode": "scheduled",
        "trigger_label": "定时任务",
        "schedule_summary": "",
        "default_tool_params": {},
        "account_roles": [
            {"label": "运行账号", "field": "account_id", "system": "ronghui", "default_account_id": "ronghui_default"},
        ],
        "order": 60,
    },
    {
        "task_id": "clockin_daxiang_s",
        "display_name": "网点打卡-大祥S站",
        "tool_name": "tms_query",
        "note": "大祥 S 站双打卡自动化任务。",
        "task_mode": "scheduled",
        "trigger_label": "定时任务",
        "schedule_summary": "",
        "default_tool_params": {},
        "account_roles": [
            {"label": "运行账号", "field": "account_id", "system": "ronghui", "default_account_id": "ronghui_daxiang_s"},
        ],
        "order": 70,
    },
    {
        "task_id": "r7_arrival_checkin",
        "display_name": "R7 到达打卡",
        "tool_name": "r7_arrival_checkin",
        "note": "R7 运输任务管理到达待卸打卡；使用 R7 登录，不依赖顶部 TMS 登录态。",
        "system_badges": [{"label": "R7", "icon": "truck", "tone": "r7"}],
        "task_mode": "scheduled",
        "trigger_label": "定时任务 / 手动执行",
        "schedule_summary": "",
        "default_tool_params": {
            "headless": True,
            "slow_mo_ms": 0,
            "max_login_attempts": 6,
            "status_text": "已调度",
            "verify_status_text": "已到达",
            "flow_mode": 1,
            "do_arrive_wait_unload": True,
            "after_action_delay_ms": 1500,
            "daily_success_limit": 1,
        },
        "account_roles": [
            {"label": "运行账号", "field": "account_id", "system": "r7", "default_account_id": "r7_default"},
        ],
        "order": 75,
    },
    {
        "task_id": "r7_departure_checkin",
        "display_name": "R7 发车打卡",
        "tool_name": "r7_departure_checkin",
        "note": "R7 运输任务管理装车待发打卡；使用 R7 登录，不依赖顶部 TMS 登录态。",
        "system_badges": [{"label": "R7", "icon": "truck", "tone": "r7"}],
        "task_mode": "scheduled",
        "trigger_label": "定时任务 / 手动执行 / 飞书发车",
        "schedule_summary": "",
        "default_tool_params": {
            "headless": True,
            "slow_mo_ms": 0,
            "max_login_attempts": 6,
            "status_text": "已调度",
            "verify_status_text": "装车待发",
            "class_name": "邵阳操作场-长沙",
            "departure_time_fixed": "21:30:00",
            "plate_numbers": "湘AK6980",
            "do_departure_checkin": True,
            "after_action_delay_ms": 1500,
            "daily_success_limit": 1,
        },
        "account_roles": [
            {"label": "运行账号", "field": "account_id", "system": "r7", "default_account_id": "r7_default"},
        ],
        "order": 76,
    },
    {
        "task_id": "arrive_list",
        "display_name": "arrive-list",
        "tool_name": "sync_arrive_list",
        "note": "TMS 派件预报基础清单同步任务。",
        "system_badges": [{"label": "TMS", "icon": "truck", "tone": "neutral"}],
        "task_mode": "scheduled",
        "trigger_label": "定时任务",
        "schedule_summary": "",
        "default_tool_params": {"login_site_code": "73901"},
        "account_roles": [
            {"label": "运行账号", "field": "account_id", "system": "ronghui", "default_account_id": "ronghui_default"},
        ],
        "order": 80,
    },
    {
        "task_id": "yunda_dispatch_forecast",
        "display_name": "韵达派件预测主单表",
        "tool_name": "sync_yunda_dispatch_forecast",
        "note": "韵达网点派件量预测主单表同步任务；使用韵达独立登录态，默认拉取次日应派数据。",
        "provider": "yunda",
        "system_badges": [{"label": "韵达", "icon": "truck", "tone": "neutral"}],
        "task_mode": "scheduled",
        "trigger_label": "定时任务 / 飞书韵达派件预测",
        "schedule_summary": "每天 17:00",
        "default_tool_params": {
            "dest_brch": "56739382",
        },
        "account_roles": [
            {"label": "运行账号", "field": "account_id", "system": "yunda", "default_account_id": "yunda_default"},
        ],
        "order": 85,
    },
    {
        "task_id": "yunda_send_waybills",
        "display_name": "韵达寄件运单同步",
        "tool_name": "sync_yunda_send_waybills",
        "note": "韵达寄件运单管理同步任务；使用韵达独立登录态，默认拉取当天寄件运单，补充快件跟踪详情和小眼睛解密字段后按运单号更新飞书多维表格。",
        "provider": "yunda",
        "system_badges": [{"label": "韵达", "icon": "truck", "tone": "neutral"}],
        "task_mode": "scheduled",
        "trigger_label": "定时任务 / 飞书韵达寄件运单",
        "schedule_summary": "",
        "default_tool_params": {
            "target_date": "",
            "ensure_fields": True,
        },
        "account_roles": [
            {"label": "运行账号", "field": "account_id", "system": "yunda", "default_account_id": "yunda_default"},
        ],
        "order": 86,
    },
    {
        "task_id": "scan_codes",
        "display_name": "获取并扫描数据",
        "tool_name": "sync_scan_codes",
        "note": "飞书机器人菜单“扫描”、Webhook 或后台立即执行，刷新扫描数据并批量执行 scan_next。",
        "task_mode": "manual",
        "trigger_label": "机器人菜单 / Webhook",
        "schedule_summary": "机器人菜单 / Webhook / 手动执行",
        "webhook_resource_key": "phase7.scan_webhook",
        "webhook_body_template": {
            "trigger_flow": False,
        },
        "default_tool_params": {
            "trigger_flow": False,
        },
        "account_roles": [
            {"label": "运行账号", "field": "account_id", "system": "ronghui", "default_account_id": "ronghui_default"},
        ],
        "order": 90,
    },
    {
        "task_id": "arrival_stats",
        "display_name": "统计到货数据",
        "tool_name": "sync_arrival_stats",
        "note": "飞书机器人菜单“统计”、Webhook 或后台立即执行，生成到货统计并更新归档表。",
        "task_mode": "manual",
        "trigger_label": "机器人菜单 / Webhook",
        "schedule_summary": "机器人菜单 / Webhook / 手动执行",
        "webhook_resource_key": "phase7.stats_webhook",
        "webhook_body_template": {
            "trigger_flow": False,
        },
        "default_tool_params": {
            "trigger_flow": False,
        },
        "account_roles": [
            {"label": "运行账号", "field": "account_id", "system": "ronghui", "default_account_id": "ronghui_default"},
        ],
        "order": 100,
    },
    {
        "task_id": "self_pickup_problem_upload",
        "display_name": "自提到货问题件",
        "tool_name": "self_pickup_problem_upload",
        "note": "读取飞书每日到货表中邵阳自提部及邵阳大祥S站自提且已到齐单号，预览后上传“开单为自提件”问题件；默认不上传截图。",
        "task_mode": "manual",
        "trigger_label": "手动执行 / 飞书自提到货问题件",
        "schedule_summary": "手动执行 / 飞书自提到货问题件",
        "default_tool_params": {
            "dry_run": True,
            "account_id": "ronghui_self_pickup_problem",
            "daxiang_s_account_id": "ronghui_daxiang_s",
        },
        "account_roles": [
            {"label": "自提部账号", "field": "account_id", "system": "ronghui", "default_account_id": "ronghui_self_pickup_problem"},
            {"label": "大祥S站账号", "field": "daxiang_s_account_id", "system": "ronghui", "default_account_id": "ronghui_daxiang_s"},
        ],
        "order": 110,
    },
    {
        "task_id": "split_pending_problem_upload",
        "display_name": "分批/未到问题件上传",
        "tool_name": "split_pending_problem_upload",
        "note": "读取每日到货表，刷新分批及有发未到 Sheet/MySQL 当前快照，并逐单上传融辉问题件。",
        "task_mode": "scheduled",
        "trigger_label": "定时任务 / 手动执行 / 飞书上传分批/未到问题件",
        "schedule_summary": "",
        "default_tool_params": {
            "dry_run": False,
            "account_id": "ronghui_default",
        },
        "account_roles": [
            {"label": "运行账号", "field": "account_id", "system": "ronghui", "default_account_id": "ronghui_default"},
        ],
        "order": 120,
    },
]

AUTOMATION_WORKFLOW_BY_ID = {
    str(item.get("task_id", "") or ""): item
    for item in AUTOMATION_WORKFLOW_CATALOG
}

AUTOMATION_PROVIDER_LABELS = {
    "ronghui": "TMS融辉",
    "yunda": "韵达",
}

AUTOMATION_SESSION_PROFILES = {
    "ronghui": {
        "label": "TMS融辉",
        "dot_label": "TMS融辉",
        "console_prefix": "/automations/tms-session",
        "agent_prefix": "/admin/tms/session",
        "login_kind": "image",
    },
    "yunda": {
        "label": "韵达",
        "dot_label": "韵达",
        "console_prefix": "/automations/yunda-session",
        "agent_prefix": "/admin/tms/yunda-session",
        "login_kind": "password",
    },
}

AUTOMATION_ACCOUNT_SYSTEM_LABELS = {
    "ronghui": "TMS融辉",
    "yunda": "韵达",
    "r7": "R7",
    "r13": "R13",
}

AUTOMATION_ACCOUNT_SYSTEM_ORDER = ("ronghui", "yunda", "r7", "r13")

AUTOMATION_DEFAULT_ACCOUNT_IDS = {
    "ronghui": "ronghui_default",
    "yunda": "yunda_default",
    "r7": "r7_default",
    "r13": "r13_default",
}

AUTOMATION_RUN_TIMEOUTS = {
    "sync_delivery_status": 1800,
    "sync_arrive_list": 3600,
    "sync_scan_codes": 21600,
    "sync_arrival_stats": 7200,
    "sync_yunda_dispatch_forecast": 1800,
    "sync_yunda_send_waybills": 1800,
    "self_pickup_problem_upload": 7200,
    "r7_arrival_checkin": 1200,
    "r7_departure_checkin": 1200,
    "finance_etl": 1800,
    "split_pending_problem_upload": 7200,
}

AUTOMATION_LONG_RUNNING_TOOLS = {
    "sync_arrive_list",
    "sync_scan_codes",
    "sync_arrival_stats",
    "sync_yunda_dispatch_forecast",
    "sync_yunda_send_waybills",
    "self_pickup_problem_upload",
    "r7_arrival_checkin",
    "r7_departure_checkin",
    "finance_etl",
    "split_pending_problem_upload",
}

UI_LABELS = {
    "ready": "就绪",
    "maintained": "持续维护",
    "etl-ready": "ETL就绪",
    "planned": "规划中",
    "in-progress": "开发中",
    "queued": "排队中",
    "processing": "处理中",
    "uploaded": "已上传",
    "review_required": "待复核",
    "confirmed": "已确认",
    "error": "异常",
    "unknown": "未知",
    "unreachable": "不可达",
    "connected": "已连接",
    "disconnected": "未连接",
    "standby": "待命",
    "ok": "正常",
    "success": "执行成功",
    "running": "执行中",
    "enabled": "已启用",
    "disabled": "已停用",
    "never-run": "未运行",
    "manual": "手动维护",
    "backend_console": "控制台保存",
    "n8n-readonly-import": "历史迁移导入",
}

AUTOMATION_FIELD_LABELS = {
    "resource": "资源类型",
    "operation": "操作类型",
    "target_date": "指定日期",
    "start_date": "起始日期",
    "end_date": "结束日期",
    "base_token": "多维表格 Token",
    "app_token": "多维表格 Token",
    "table_id": "数据表 ID",
    "view_name": "视图名称",
    "view_id": "视图 ID",
    "spreadsheet_token": "电子表格 Token",
    "sheet_id": "工作表 ID",
    "sheet_name": "工作表名称",
    "range": "读取范围",
    "clear_range": "清空范围",
    "title_range": "标题范围",
    "default_write_range": "写入范围",
    "snapshot_range": "快照范围",
    "count_read_range": "计数范围",
    "receive_id": "接收对象 ID",
    "receive_id_type": "接收对象类型",
    "path": "兼容路径",
    "url": "Webhook 地址",
    "endpoint": "接口路径",
    "timeout_sec": "超时时间(秒)",
    "mode": "执行模式",
    "site_name": "网点名称",
    "first_type": "第一步类型",
    "second_type": "第二步类型",
    "site_fb_name": "飞书网点名",
    "delay_seconds": "延迟秒数",
    "daily_success_limit": "一天打卡次数",
    "page_size": "每页条数",
    "max_pages": "最多页数",
    "request_body": "请求覆盖参数",
    "ensure_fields": "自动补齐表字段",
    "month": "月份",
    "input_path": "输入路径",
    "dry_run": "仅试跑",
}

AUTOMATION_DATE_FIELD_NAMES = {
    "target_date",
    "start_date",
    "end_date",
}

AUTOMATION_SECRET_FIELD_NAMES = {
    "base_token",
    "app_token",
    "spreadsheet_token",
    "password",
    "token",
    "url",
}

AUTOMATION_FIELD_HINTS = {
    "target_date": "不选择时默认拉取当天。",
    "start_date": "只需要单日同步时可不填。",
    "end_date": "只需要单日同步时可不填。",
    "base_token": "从飞书多维表格地址或资源配置中获取。",
    "app_token": "从飞书多维表格地址或资源配置中获取。",
    "table_id": "选择目标数据表后填写。",
    "spreadsheet_token": "从飞书电子表格地址中获取。",
    "range": "例如 Sheet1!A2:R200。",
    "clear_range": "保存前需要清空的范围。",
    "title_range": "表头所在范围。",
    "default_write_range": "默认写入的单元格范围。",
}


def ui_label(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "-"
    return UI_LABELS.get(raw, raw)


def automation_field_label(path: str) -> str:
    tail = path.rsplit(".", 1)[-1]
    return AUTOMATION_FIELD_LABELS.get(path, AUTOMATION_FIELD_LABELS.get(tail, tail))


def automation_field_hint(path: str) -> str:
    tail = path.rsplit(".", 1)[-1]
    return AUTOMATION_FIELD_HINTS.get(path, AUTOMATION_FIELD_HINTS.get(tail, ""))


def automation_resource_display_name(resource_key: str) -> str:
    note = str(AUTOMATION_RESOURCE_NOTES.get(resource_key, "") or "").strip()
    if note:
        return note.rstrip("。.")
    return automation_field_label(resource_key)


def _automation_field_tail(path: str) -> str:
    return str(path or "").rsplit(".", 1)[-1]


def _automation_field_is_secret(path: str) -> bool:
    tail = _automation_field_tail(path).lower()
    return tail in AUTOMATION_SECRET_FIELD_NAMES or tail.endswith("_token")


def _parse_resource_editor_json(editor_json: str) -> dict[str, Any]:
    try:
        parsed = json.loads(str(editor_json or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def flatten_automation_fields(data: dict[str, Any], prefix: str = "") -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []

    fields: list[dict[str, Any]] = []
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            fields.extend(flatten_automation_fields(value, path))
            continue

        field: dict[str, Any] = {
            "path": path,
            "label": automation_field_label(path),
            "hint": automation_field_hint(path),
            "value": "" if value is None else value,
            "kind": "text",
            "empty_null": value is None,
            "secret": _automation_field_is_secret(path),
        }

        tail = _automation_field_tail(path)
        if tail in AUTOMATION_DATE_FIELD_NAMES:
            field["kind"] = "date"
        elif isinstance(value, bool):
            field["kind"] = "checkbox"
        elif isinstance(value, int) and not isinstance(value, bool):
            field["kind"] = "number"
            field["step"] = "1"
        elif isinstance(value, float):
            field["kind"] = "number"
            field["step"] = "0.01"
        elif isinstance(value, list):
            field["kind"] = "list"
            field["value"] = "\n".join(str(item) for item in value if item not in (None, ""))
        elif isinstance(value, str) and (value.startswith("http://") or value.startswith("https://")):
            field["kind"] = "url"

        fields.append(field)
    return fields


_DAILY_CRON_RE = re.compile(r"^\s*(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+\*\s*$")
_TASK_SLOT_ID_RE = re.compile(r"^(?P<base>.+?)__slot_(?P<slot>\d+)$")
_TASK_TIME_SUFFIX_RE = re.compile(r"^(?P<base>.+?)_(?P<hour>[01]\d|2[0-3])(?P<minute>[0-5]\d)$")
_TASK_NAME_TIME_SUFFIX_RE = re.compile(r"^(?P<base>.+?)(?:[-_\s]+)(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)$")


def parse_daily_cron_expression(cron_expression: str) -> dict[str, Any]:
    raw = str(cron_expression or "").strip()
    if not raw:
        return {
            "supported": False,
            "time_value": "",
            "summary": "未设置定时",
            "warning": "当前未设置 CRON 表达式，请在高级设置中补充。",
        }

    match = _DAILY_CRON_RE.fullmatch(raw)
    if not match:
        return {
            "supported": False,
            "time_value": "",
            "summary": raw,
            "warning": "当前为高级 CRON 规则，请在高级设置中维护。",
        }

    minute = int(match.group(1))
    hour = int(match.group(2))
    if not (0 <= minute <= 59 and 0 <= hour <= 23):
        return {
            "supported": False,
            "time_value": "",
            "summary": raw,
            "warning": "当前 CRON 超出可视化时间范围，请在高级设置中维护。",
        }

    return {
        "supported": True,
        "time_value": f"{hour:02d}:{minute:02d}",
        "summary": f"每天 {hour:02d}:{minute:02d}",
        "warning": "",
    }


def build_daily_cron_expression(time_value: str) -> str | None:
    raw = str(time_value or "").strip()
    if not raw or ":" not in raw:
        return None

    hour_text, minute_text = raw.split(":", 1)
    try:
        hour = int(hour_text)
        minute = int(minute_text)
    except ValueError:
        return None

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{minute} {hour} * * *"


def extract_task_time_value(task_id: str) -> str:
    raw = str(task_id or "").strip()
    match = _TASK_TIME_SUFFIX_RE.fullmatch(raw)
    if not match:
        return ""
    return f"{match.group('hour')}:{match.group('minute')}"


def normalize_task_group_id(task_id: str) -> str:
    raw = str(task_id or "").strip()
    match = _TASK_SLOT_ID_RE.fullmatch(raw)
    normalized = match.group("base") if match else raw
    time_match = _TASK_TIME_SUFFIX_RE.fullmatch(normalized)
    return time_match.group("base") if time_match else normalized


def task_group_slot_index(task_id: str) -> int:
    raw = str(task_id or "").strip()
    match = _TASK_SLOT_ID_RE.fullmatch(raw)
    if match:
        try:
            return int(match.group("slot"))
        except ValueError:
            return 0
    time_value = extract_task_time_value(raw)
    if not time_value:
        return 0
    hour_text, minute_text = time_value.split(":", 1)
    return int(hour_text) * 60 + int(minute_text)


def build_task_group_id(base_task_id: str, position: int) -> str:
    if position == 0:
        return base_task_id
    return f"{base_task_id}__slot_{position}"


def build_task_schedule_id(
    base_task_id: str,
    *,
    cron_expression: str = "",
    schedule_time: str = "",
    position: int = 0,
) -> str:
    time_value = str(schedule_time or "").strip()
    if not time_value and cron_expression:
        time_value = str(parse_daily_cron_expression(cron_expression).get("time_value", "") or "").strip()
    if time_value and build_daily_cron_expression(time_value):
        return f"{base_task_id}_{time_value.replace(':', '')}"
    return build_task_group_id(base_task_id, position)


def normalize_task_display_name(name: str) -> str:
    raw = str(name or "").strip()
    if not raw:
        return raw
    match = _TASK_NAME_TIME_SUFFIX_RE.fullmatch(raw)
    if not match:
        return raw
    return str(match.group("base") or "").strip(" -_")


def automation_task_note(task_id: str) -> str:
    normalized = normalize_task_group_id(task_id)
    workflow = AUTOMATION_WORKFLOW_BY_ID.get(normalized, {})
    if workflow.get("note"):
        return str(workflow.get("note") or "")
    return AUTOMATION_TASK_NOTES.get(task_id, AUTOMATION_TASK_NOTES.get(normalized, ""))


def automation_workflow_definition(task_id: str) -> dict[str, Any]:
    return AUTOMATION_WORKFLOW_BY_ID.get(normalize_task_group_id(task_id), {})


def automation_task_provider(
    task_id: str,
    workflow: dict[str, Any] | None = None,
    tool_name: str = "",
) -> str:
    workflow = workflow or automation_workflow_definition(task_id)
    provider = str(workflow.get("provider") or "").strip().lower()
    normalized = normalize_task_group_id(task_id)
    tool_name_value = str(tool_name or workflow.get("tool_name") or "").strip()
    if provider in AUTOMATION_PROVIDER_LABELS:
        return provider
    if normalized.startswith("yunda_") or tool_name_value == "sync_yunda_dispatch_forecast":
        return "yunda"
    return "ronghui"


def automation_provider_label(provider: str) -> str:
    return AUTOMATION_PROVIDER_LABELS.get(provider, AUTOMATION_PROVIDER_LABELS["ronghui"])


def normalize_automation_session_profile(profile: Any) -> str:
    raw = str(profile or "").strip().lower()
    aliases = {
        "": "ronghui",
        "default": "ronghui",
        "tms": "ronghui",
        "rh": "ronghui",
        "ronghui": "ronghui",
        "yunda": "yunda",
        "yd": "yunda",
    }
    normalized = aliases.get(raw, raw)
    if normalized not in AUTOMATION_SESSION_PROFILES:
        return "ronghui"
    return normalized


def webhook_token_header_name() -> str:
    return "X-Agent-Webhook-Token"


def webhook_token_enabled() -> bool:
    return bool(
        (
            os.getenv("DOCFLOW_AGENT_WEBHOOK_TOKEN", "").strip()
            or os.getenv("AGENT_WEBHOOK_TOKEN", "").strip()
        )
    )


def automation_webhook_public_base_url(agent_base_url: str) -> str:
    candidate = (
        os.getenv("DOCFLOW_AGENT_PUBLIC_BASE_URL", "").strip()
        or os.getenv("AGENT_PUBLIC_BASE_URL", "").strip()
    )
    return candidate.rstrip("/") if candidate else ""


def mask_sensitive_webhook(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "未配置"
    if len(raw) <= 18:
        return raw[:6] + "••••" + raw[-4:]
    return raw[:12] + "••••••" + raw[-8:]


def resolve_automation_webhook_info(
    task_id: str,
    *,
    workflow_resources: dict[str, dict[str, Any]],
    agent_base_url: str,
) -> dict[str, Any]:
    workflow = automation_workflow_definition(task_id)
    resource_key = str(workflow.get("webhook_resource_key", "") or "").strip()
    resource = workflow_resources.get(resource_key, {}) if resource_key else {}
    config = resource.get("config") if isinstance(resource, dict) else {}
    if not isinstance(config, dict):
        config = {}

    resource_path = str(config.get("path", "") or "").strip()
    fallback_path = str(workflow.get("webhook_fallback_path", "") or "").strip()
    normalized_path = ""
    if resource_path:
        normalized_path = "/" + resource_path.strip("/")
    elif fallback_path:
        normalized_path = fallback_path if fallback_path.startswith("/") else "/" + fallback_path

    public_base_url = automation_webhook_public_base_url(agent_base_url)
    if public_base_url and normalized_path:
        full_url = f"{public_base_url}{normalized_path}"
    else:
        full_url = normalized_path

    return {
        "resource_key": resource_key,
        "path": normalized_path,
        "full_url": full_url,
        "masked_url": mask_sensitive_webhook(full_url or normalized_path),
        "token_enabled": webhook_token_enabled(),
        "header_name": webhook_token_header_name(),
        "body_example": workflow.get("webhook_body_template")
        or {
            "task_id": normalize_task_group_id(task_id),
            "task_name": workflow.get("display_name") or task_id,
            "tool_name": workflow.get("tool_name") or "",
        },
    }


def build_virtual_task_defaults(task_id: str) -> dict[str, Any]:
    workflow = automation_workflow_definition(task_id)
    default_params = workflow.get("default_tool_params")
    if not isinstance(default_params, dict):
        default_params = {}
    return {
        "task_id": normalize_task_group_id(task_id),
        "name": str(workflow.get("display_name", "") or normalize_task_display_name(task_id)),
        "tool_name": str(workflow.get("tool_name", "") or ""),
        "tool_params": json.loads(json.dumps(default_params, ensure_ascii=False)),
        "enabled": False,
        "task_mode": str(workflow.get("task_mode", "manual") or "manual"),
    }


def normalize_schedule_times(values: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        cron_expression = build_daily_cron_expression(value)
        if not cron_expression:
            continue
        schedule_info = parse_daily_cron_expression(cron_expression)
        time_value = str(schedule_info.get("time_value", "") or "")
        if not time_value or time_value in seen:
            continue
        seen.add(time_value)
        normalized.append(time_value)
    return sorted(normalized)


def summarize_task_group_schedule(cron_expressions: list[str]) -> dict[str, Any]:
    schedule_infos = [parse_daily_cron_expression(item) for item in cron_expressions if str(item or "").strip()]
    if not schedule_infos:
        return {
            "supported": True,
            "time_values": [],
            "summary": "未设置定时",
            "warning": "",
            "raw_value": "",
        }

    if all(info.get("supported") for info in schedule_infos):
        time_values = normalize_schedule_times([str(info.get("time_value", "") or "") for info in schedule_infos])
        summary = " / ".join(f"每天 {time_value}" for time_value in time_values) if time_values else "未设置定时"
        return {
            "supported": True,
            "time_values": time_values,
            "summary": summary,
            "warning": "",
            "raw_value": "\n".join(item.strip() for item in cron_expressions if str(item or "").strip()),
        }

    raw_value = "\n".join(item.strip() for item in cron_expressions if str(item or "").strip())
    return {
        "supported": False,
        "time_values": [],
        "summary": raw_value or "高级规则",
        "warning": "当前包含无法图形化维护的 CRON 规则，请在高级设置中直接编辑。",
        "raw_value": raw_value,
    }


def format_duration_label(duration_ms: Any) -> str:
    try:
        value = int(duration_ms)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    if value < 1000:
        return f"{value} ms"
    seconds = value / 1000
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    minutes = int(seconds // 60)
    remain = int(round(seconds % 60))
    return f"{minutes} 分 {remain} 秒"


def normalize_feedback_text(message: Any) -> str:
    if isinstance(message, (dict, list)):
        try:
            return json.dumps(message, ensure_ascii=False, indent=2)
        except TypeError:
            return str(message).strip()
    return str(message or "").strip()


def shorten_error_message(message: Any, limit: int = 180) -> str:
    text = normalize_feedback_text(message)
    if not text:
        return ""
    compact = re.sub(r"\s+", " ", text)
    return compact[: limit - 1] + "…" if len(compact) > limit else compact


def automation_runtime_feedback_meta(
    *,
    ok: bool,
    cancelled: bool,
    success_message: str,
    error_message: str,
) -> dict[str, str]:
    if cancelled:
        return {
            "status": "cancelled",
            "title": "本次执行已取消",
            "message": error_message or "已取消执行",
            "status_label": "已取消",
        }
    if ok:
        return {
            "status": "success",
            "title": "最近一次立即执行",
            "message": success_message,
            "status_label": "执行成功",
        }
    return {
        "status": "error",
        "title": "最近一次执行失败",
        "message": error_message or "任务执行失败",
        "status_label": "执行失败",
    }


def automation_task_resource_requirements(task_id: str) -> list[dict[str, Any]]:
    entries = AUTOMATION_TASK_RESOURCE_REQUIREMENTS.get(normalize_task_group_id(task_id), [])
    requirements: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        resource_key = str(entry.get("resource_key", "") or "").strip()
        if not resource_key or resource_key in seen:
            continue
        seen.add(resource_key)
        requirements.append(
            {
                "resource_key": resource_key,
                "required": bool(entry.get("required", True)),
                "note": AUTOMATION_RESOURCE_NOTES.get(resource_key, ""),
            }
        )
    return requirements


def build_automation_resource_bindings(
    task_id: str,
    workflow_resources: dict[str, dict[str, Any]],
    *,
    resource_overrides: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    resource_overrides = resource_overrides or {}
    bindings: list[dict[str, Any]] = []
    for requirement in automation_task_resource_requirements(task_id):
        resource_key = str(requirement.get("resource_key", "") or "").strip()
        resource_row = workflow_resources.get(resource_key, {}) if resource_key else {}
        override_row = resource_overrides.get(resource_key, {})
        configured = bool(resource_row)
        example_config = AUTOMATION_RESOURCE_JSON_EXAMPLES.get(resource_key, {})
        if override_row.get("config_json") is not None:
            editor_json = str(override_row.get("config_json") or "")
        elif configured:
            config = resource_row.get("config")
            if isinstance(config, dict):
                editor_json = json.dumps(config, ensure_ascii=False, indent=2)
            else:
                editor_json = str(resource_row.get("config_json") or "")
        else:
            editor_json = json.dumps(example_config, ensure_ascii=False, indent=2)
        visual_config = _parse_resource_editor_json(editor_json)
        if not visual_config and isinstance(example_config, dict):
            visual_config = dict(example_config)
        bindings.append(
            {
                "resource_key": resource_key,
                "display_name": automation_resource_display_name(resource_key),
                "required": bool(requirement.get("required", True)),
                "configured": configured,
                "missing": not configured,
                "note": str(requirement.get("note", "") or ""),
                "source": str(resource_row.get("source", "") or ""),
                "updated_at": str(resource_row.get("updated_at", "") or ""),
                "editor_json": editor_json,
                "visual_fields": flatten_automation_fields(visual_config),
            }
        )
    return bindings


def quality_issue_messages(issue_codes: list[str]) -> list[str]:
    return [QUALITY_ISSUE_LABELS.get(code, code) for code in issue_codes]


def normalize_open_date(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""

    compact = re.sub(r"\s+", "", raw)
    patterns = (
        r"(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})",
        r"(?P<year>\d{4})[./-](?P<month>\d{1,2})[./-](?P<day>\d{1,2})",
        r"(?P<year>\d{4})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日",
    )

    for pattern in patterns:
        match = re.fullmatch(pattern, compact)
        if not match:
            continue
        year = int(match.group("year"))
        month = int(match.group("month"))
        day = int(match.group("day"))
        try:
            parsed = datetime(year, month, day)
        except ValueError:
            return raw
        return parsed.strftime("%Y/%m/%d")

    return raw


def normalize_field_value(field_name: str, value: str) -> str:
    text = str(value or "").strip()
    if field_name == "open_date":
        return normalize_open_date(text)
    return text


def normalize_money_amount(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    cleaned = re.sub(r"[\s,￥¥元]", "", text)
    if not cleaned:
        return ""
    try:
        amount = Decimal(str(cleaned)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("金额格式无效") from exc
    return f"{amount:.2f}"


def normalize_manual_field_value(field_name: str, value: str) -> str:
    normalized = normalize_field_value(field_name, value)
    if field_name in MONEY_FIELD_NAMES:
        return normalize_money_amount(normalized)
    return normalized


QUOTE_MONEY_SCALE = Decimal("0.01")
RONGHUI_PRODUCT_NAMES = ("融惠达", "精准零担", "融安达", "融速达")
QUOTE_AUTH_ERROR_CODES = {"AUTH_REQUIRED", "AUTH_EXPIRED", "LOGIN_REQUIRED", "SESSION_EXPIRED"}


class QuoteOptionsValidationError(ValueError):
    """Raised when a quote-options request misses required pricing input."""


def _clean_quote_text(value: Any) -> str:
    return str(value or "").strip()


def _quote_decimal(value: Any, *, field_name: str = "price") -> Decimal:
    text = _clean_quote_text(value)
    if not text:
        raise ValueError(f"{field_name} 为空")
    cleaned = re.sub(r"[\s,￥¥元]", "", text)
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", cleaned or ""):
        raise ValueError(f"{field_name} 非数字")
    try:
        amount = Decimal(str(cleaned))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} 非数字") from exc
    if amount <= Decimal("0"):
        raise ValueError(f"{field_name} 必须大于 0")
    return amount.quantize(QUOTE_MONEY_SCALE, rounding=ROUND_HALF_UP)


def _quote_money_text(amount: Decimal) -> str:
    return f"{amount.quantize(QUOTE_MONEY_SCALE, rounding=ROUND_HALF_UP):.2f}"


def _normalize_quote_delivery_method(value: Any) -> str:
    text = _clean_quote_text(value)
    if "自提" in text:
        return "自提"
    if "派送" in text or "送货" in text:
        return "派送"
    raise QuoteOptionsValidationError("送货方式必须选择自提或送货。")


def parse_quote_options_request(payload: dict[str, Any]) -> dict[str, Any]:
    body = payload if isinstance(payload, dict) else {}
    receiver_address = _clean_quote_text(body.get("receiver_address") or body.get("field_receiver_address"))
    if not receiver_address:
        raise QuoteOptionsValidationError("收件地址不能为空。")
    try:
        weight = _quote_decimal(body.get("weight_kg") or body.get("field_weight_kg"), field_name="重量")
    except ValueError as exc:
        raise QuoteOptionsValidationError(f"重量 kg 必须填写有效数字：{exc}") from exc
    try:
        volume = _quote_decimal(body.get("volume_m3") or body.get("field_volume_m3"), field_name="体积")
    except ValueError as exc:
        raise QuoteOptionsValidationError(f"体积 m³ 必须填写有效数字：{exc}") from exc
    delivery_method = _normalize_quote_delivery_method(
        body.get("delivery_method") or body.get("field_delivery_method")
    )
    return {
        "receiver_address": receiver_address,
        "weight": weight,
        "volume": volume,
        "delivery_method": delivery_method,
    }


def _quote_error_text(payload: Any, default: str) -> str:
    if not isinstance(payload, dict):
        return default
    for key in ("error", "message", "last_error_summary"):
        value = _clean_quote_text(payload.get(key))
        if value:
            return value
    nested = payload.get("data")
    if isinstance(nested, dict):
        return _quote_error_text(nested, default)
    raw = payload.get("raw")
    if isinstance(raw, dict):
        return _quote_error_text(raw, default)
    return default


def _quote_error_status(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "unavailable"
    code = _clean_quote_text(payload.get("error_code") or payload.get("code")).upper()
    if code in QUOTE_AUTH_ERROR_CODES:
        return "auth_required"
    error_text = _quote_error_text(payload, "")
    if any(marker in error_text for marker in ("登录", "未授权", "过期", "AUTH_REQUIRED")):
        return "auth_required"
    if any(marker in error_text for marker in ("超时", "timeout", "timed out")):
        return "timeout"
    return "unavailable"


def _unavailable_quote(
    *,
    provider: str,
    label: str,
    delivery_method: str,
    error: str,
    status: str = "unavailable",
    details: list[str] | None = None,
    site_name: str = "",
) -> dict[str, Any]:
    return {
        "provider": provider,
        "label": label,
        "status": status,
        "price": "",
        "product_name": "",
        "delivery_method": delivery_method,
        "site_name": site_name,
        "error": error,
        "details": details or [],
    }


def _result_has_explicit_failure(result: Any) -> bool:
    if not isinstance(result, dict):
        return True
    if result.get("ok") is False or result.get("error") or result.get("message"):
        return True
    if result.get("网点不可达") or result.get("不可达") or result.get("unavailable"):
        return True
    return False


def _result_site_name(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    for key in ("目的网点", "网点名称", "site_name", "destination_site"):
        value = _clean_quote_text(result.get(key))
        if value:
            return value
    return ""


def _ronghui_quote(result: Any, delivery_method: str) -> dict[str, Any]:
    label = "融辉"
    site_name = _result_site_name(result)
    if _result_has_explicit_failure(result):
        error = "地址不可达或未匹配到融辉可报价网点。"
        if isinstance(result, dict) and (result.get("error") or result.get("message")):
            error = _quote_error_text(result, error)
        return _unavailable_quote(
            provider="ronghui",
            label=label,
            delivery_method=delivery_method,
            error=error,
            status=_quote_error_status(result),
            site_name=site_name,
        )

    candidates: list[tuple[Decimal, str, str]] = []
    result_items = result.items() if isinstance(result, dict) else ()
    for key, value in result_items:
        product_key = _clean_quote_text(key)
        if not product_key:
            continue
        is_dispatch = product_key.endswith("(派送)")
        product_name = product_key.removesuffix("(派送)")
        if delivery_method == "派送":
            if not is_dispatch:
                continue
        elif is_dispatch or product_name not in RONGHUI_PRODUCT_NAMES:
            continue
        try:
            amount = _quote_decimal(value, field_name=f"融辉 {product_key}")
        except ValueError:
            continue
        candidates.append((amount, product_name, product_key))

    if not candidates:
        return _unavailable_quote(
            provider="ronghui",
            label=label,
            delivery_method=delivery_method,
            error=f"融辉当前送货方式无可用报价：{delivery_method}",
            site_name=site_name,
        )

    amount, product_name, product_key = min(candidates, key=lambda item: item[0])
    return {
        "provider": "ronghui",
        "label": label,
        "status": "available",
        "price": _quote_money_text(amount),
        "product_name": product_name,
        "delivery_method": delivery_method,
        "site_name": site_name,
        "error": "",
        "details": [f"{key}={_quote_money_text(value)}" for value, _, key in sorted(candidates, key=lambda item: item[0])],
        "_price_decimal": amount,
    }


def _yunda_quote(result: Any, delivery_method: str) -> dict[str, Any]:
    label = "韵达"
    site_name = _result_site_name(result)
    if _result_has_explicit_failure(result):
        return _unavailable_quote(
            provider="yunda",
            label=label,
            delivery_method=delivery_method,
            error=_quote_error_text(result, "韵达当前无可用报价。"),
            status=_quote_error_status(result),
            site_name=site_name,
        )
    price_key = "韵达自提" if delivery_method == "自提" else "韵达派送"
    try:
        amount = _quote_decimal((result or {}).get(price_key), field_name=price_key)
    except ValueError as exc:
        return _unavailable_quote(
            provider="yunda",
            label=label,
            delivery_method=delivery_method,
            error=f"韵达当前送货方式无可用报价或金额不可解析：{exc}",
            status="invalid_price",
            site_name=site_name,
        )
    return {
        "provider": "yunda",
        "label": label,
        "status": "available",
        "price": _quote_money_text(amount),
        "product_name": price_key,
        "delivery_method": delivery_method,
        "site_name": site_name,
        "error": "",
        "details": [f"{price_key}={_quote_money_text(amount)}"],
        "_price_decimal": amount,
    }


def _yongsheng_pending_quote(delivery_method: str) -> dict[str, Any]:
    return {
        "provider": "yongsheng",
        "label": "勇胜手工专线",
        "status": "pending",
        "price": "",
        "product_name": "",
        "delivery_method": delivery_method,
        "site_name": "",
        "error": "价格体系待维护，不参与最低价。",
        "details": ["v1 暂不计算勇胜手工专线价格"],
    }


def build_manual_quote_options(
    *,
    ronghui_result: Any,
    yunda_result: Any,
    delivery_method: str,
) -> dict[str, Any]:
    normalized_delivery = _normalize_quote_delivery_method(delivery_method)
    quotes = [
        _yunda_quote(yunda_result, normalized_delivery),
        _ronghui_quote(ronghui_result, normalized_delivery),
        _yongsheng_pending_quote(normalized_delivery),
    ]
    available = [quote for quote in quotes if quote.get("status") == "available" and quote.get("_price_decimal")]
    best_provider = ""
    if available:
        best = min(available, key=lambda item: item["_price_decimal"])
        best_provider = str(best.get("provider") or "")
    public_quotes = []
    for quote in quotes:
        public_quote = dict(quote)
        public_quote.pop("_price_decimal", None)
        public_quotes.append(public_quote)
    return {
        "ok": bool(available),
        "quotes": public_quotes,
        "best_provider": best_provider,
        "available_count": len(available),
        "message": "已获取可用报价。" if available else "无可用报价，请查看各平台失败原因。",
    }


def elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


class DocumentService:
    def __init__(self, settings, repository, template_store, qwen_provider) -> None:
        self.settings = settings
        self.repository = repository
        self.template_store = template_store
        self.qwen_provider = qwen_provider
        self.task_queue: DocumentTaskQueue | None = None

    def attach_task_queue(self, task_queue: DocumentTaskQueue) -> None:
        self.task_queue = task_queue

    def process_upload(self, item: UploadItem, template_name: str | None = None) -> ProcessingResult:
        template_spec = self._get_template_spec(template_name)
        token = new_doc_token()
        safe_name = sanitize_filename(Path(item.filename).name)
        original_relpath = self._build_original_relpath(token, safe_name)
        original_abspath = self.settings.runtime_dir / original_relpath
        write_bytes(original_abspath, item.payload)

        processed_relpath = Path("artifacts") / "processed" / token / "processed.jpg"
        artifacts_relpath = Path("artifacts") / token

        document_id = self.repository.create_document(
            doc_token=token,
            original_name=safe_name,
            source_relpath=item.source_relpath or safe_name,
            template_name=template_spec["template_name"],
            status="queued",
            original_path=str(original_relpath).replace("\\", "/"),
            processed_path=str(processed_relpath).replace("\\", "/"),
            artifacts_dir=str(artifacts_relpath).replace("\\", "/"),
            fields=self._build_initial_fields(template_spec),
            raw_ocr=self._build_empty_raw_ocr(),
        )

        if not self._enqueue_document(document_id):
            message = "Task queue is unavailable, document was not queued."
            self.repository.update_document(
                document_id,
                status="error",
                error_message=message,
            )
            return ProcessingResult(document_id=document_id, status="error", error_message=message)
        return ProcessingResult(document_id=document_id, status="queued")

    def recover_pending_documents(self) -> int:
        recovered = 0
        for document in self.repository.list_documents_by_status(["uploaded", "queued", "processing"]):
            raw_ocr = document.get("raw_ocr", {})
            queue_state = dict(raw_ocr.get("queue", {}))
            queue_state.update(
                {
                    "state": "queued",
                    "queued_at": self._now_string(),
                }
            )
            raw_ocr["queue"] = queue_state
            self.repository.update_document(
                document["id"],
                status="queued",
                raw_ocr=raw_ocr,
                error_message="",
            )
            if self._enqueue_document(document["id"]):
                recovered += 1
        return recovered

    def process_document(self, document_id: int) -> None:
        document = self.repository.get_document(document_id)
        if not document:
            return
        template_spec = self._get_template_spec(document.get("template_name"))
        process_started_at = time.perf_counter()

        token = document["doc_token"]
        original_abspath = self.settings.runtime_dir / document["original_path"]
        processed_abspath = self.settings.runtime_dir / document["processed_path"]
        temp_dir = self.settings.temp_dir / token
        fields = self._build_initial_fields(template_spec)
        raw_ocr = self._build_processing_raw_ocr(document)
        timing_snapshot: dict[str, Any] = {
            "preprocess_ms": 0.0,
            "qwen_ms": 0.0,
            "total_ms": 0.0,
        }

        self.repository.update_document(
            document_id,
            status="processing",
            fields=fields,
            raw_ocr=raw_ocr,
            error_message="",
        )

        try:
            preprocess_started_at = time.perf_counter()
            preprocess_info = preprocess_document(
                original_abspath,
                processed_abspath,
                temp_dir,
                template_spec,
            )
            timing_snapshot["preprocess_ms"] = elapsed_ms(preprocess_started_at)
            preprocess_info["timing"] = {
                "elapsed_ms": timing_snapshot["preprocess_ms"],
            }
            provider_fields = self._build_initial_fields(template_spec)
            raw_ocr["preprocess"] = preprocess_info

            if self._apply_quality_gate(fields, preprocess_info):
                raw_ocr["qwen"] = self._quality_gate_debug(preprocess_info)
                timing_snapshot["total_ms"] = elapsed_ms(process_started_at)
                raw_ocr["timing"] = self._build_timing_snapshot(timing_snapshot, raw_ocr["qwen"])
                raw_ocr["queue"] = self._finalize_queue_state(raw_ocr["queue"], "review_required")
                self.repository.update_document(
                    document_id,
                    fields=fields,
                    raw_ocr=raw_ocr,
                    status="review_required",
                    error_message="",
                )
                self._log_document_timing(document, "review_required", raw_ocr["timing"], note="quality_gate")
                return

            qwen_image_paths = self._build_qwen_image_paths(preprocess_info, processed_abspath)
            qwen_started_at = time.perf_counter()
            qwen_results, qwen_debug = self.qwen_provider.extract_document(
                qwen_image_paths,
                template_spec,
                provider_fields,
            )
            timing_snapshot["qwen_ms"] = elapsed_ms(qwen_started_at)
            qwen_debug["timing"] = self._merge_qwen_timing(
                qwen_debug.get("timing", {}),
                timing_snapshot["qwen_ms"],
            )
            raw_ocr["qwen"] = qwen_debug
            self._merge_results(fields, qwen_results)

            status = self._derive_status(fields, template_spec)
            timing_snapshot["total_ms"] = elapsed_ms(process_started_at)
            raw_ocr["timing"] = self._build_timing_snapshot(timing_snapshot, qwen_debug)
            raw_ocr["queue"] = self._finalize_queue_state(raw_ocr["queue"], status)
            self.repository.update_document(
                document_id,
                fields=fields,
                raw_ocr=raw_ocr,
                status=status,
                error_message="",
            )
            self._log_document_timing(document, status, raw_ocr["timing"])
        except Exception as exc:
            timing_snapshot["total_ms"] = elapsed_ms(process_started_at)
            raw_ocr["timing"] = self._build_timing_snapshot(timing_snapshot, raw_ocr.get("qwen", {}))
            self._mark_document_error(document_id, raw_ocr, str(exc), "process_document")
            self._log_document_timing(document, "error", raw_ocr["timing"], note=str(exc))

    def reprocess_document(self, document_id: int) -> ActionResult:
        document = self.repository.get_document(document_id)
        if not document:
            raise ValueError(f"Document {document_id} not found.")
        template_spec = self._get_template_spec(document.get("template_name"))

        if document["status"] in {"queued", "processing"}:
            return ActionResult(ok=True, message="Document is already queued for processing.")

        fields = self._build_initial_fields(template_spec)
        queue_attempts = int(document.get("raw_ocr", {}).get("queue", {}).get("attempts", 0))
        self.repository.update_document(
            document_id,
            fields=fields,
            raw_ocr=self._build_empty_raw_ocr(queue_attempts=queue_attempts),
            status="queued",
            error_message="",
        )
        queued = self._enqueue_document(document_id)
        if not queued:
            message = "Reprocess failed: task queue is unavailable."
            self.repository.update_document(
                document_id,
                status="error",
                error_message=message,
            )
            return ActionResult(ok=False, message=message)
        return ActionResult(ok=True, message="Document has been requeued.")

    def apply_review(self, document_id: int, form_values: dict[str, str]) -> ActionResult:
        document = self.repository.get_document(document_id)
        if not document:
            raise ValueError(f"Document {document_id} not found.")
        template_spec = self._get_template_spec(document.get("template_name"))
        fields = self.coerce_fields(document["fields"], template_spec)

        writer_id = form_values.get("writer_id", "").strip()

        for field in template_spec["fields"]:
            field_name = field["name"]
            manual_value = normalize_field_value(field_name, form_values.get(f"field_{field_name}", ""))
            entry = fields[field_name]
            if manual_value != entry.get("value", ""):
                entry["value"] = manual_value
                entry["confidence"] = 1.0 if manual_value else 0.0
                entry["source"] = "manual_review"
                entry["message"] = "Updated by reviewer."
            entry["status"] = "confirmed" if manual_value else "missing"

        action = form_values.get("action", "save")
        notes = form_values.get("notes", "").strip()
        derived_status = self._derive_status(fields, template_spec)
        status = derived_status
        reviewed_at = None
        if action == "confirm" and derived_status == "ready":
            status = "confirmed"
            reviewed_at = self._now_string()
            message = "Review confirmed and saved."
        elif action == "confirm":
            status = "review_required"
            message = "Required fields are still missing or low-confidence."
        elif derived_status == "ready":
            message = "Review saved. Document is ready to confirm."
        else:
            message = "Review saved."
        self.repository.update_document(
            document_id,
            fields=fields,
            notes=notes,
            status=status,
            reviewed_at=reviewed_at,
            writer_id=writer_id,
            error_message="",
        )

        # On confirm, also write the normalized waybill row and reviewer info.
        if status == "confirmed":
            try:
                self.repository.create_waybill_from_fields(
                    fields, document_id=document_id,
                    writer_id=writer_id, source="ocr",
                )
                if writer_id:
                    self.repository.upsert_writer(writer_id)
            except Exception as exc:
                import sys
                print(f"[WARNING] waybill sync failed: {exc}", file=sys.stderr)

        return ActionResult(ok=status == "confirmed", message=message)

    def apply_manual_waybill(self, form_values: dict[str, str]) -> ActionResult:
        template_spec = self._get_template_spec()
        writer_id = form_values.get("writer_id", "").strip()
        fields: dict[str, str] = {}
        missing_labels: list[str] = []

        for field in template_spec["fields"]:
            field_name = field["name"]
            if field_name == "waybill_no":
                fields[field_name] = ""
                continue
            try:
                manual_value = normalize_manual_field_value(
                    field_name,
                    form_values.get(f"field_{field_name}", ""),
                )
            except ValueError as exc:
                return ActionResult(
                    ok=False,
                    message=f"{field.get('label', field_name)}：{exc}",
                )
            fields[field_name] = manual_value
            if field.get("required", False) and not manual_value:
                missing_labels.append(str(field.get("label") or field_name))

        for field_name, label in MANUAL_EXTRA_FIELD_LABELS.items():
            try:
                fields[field_name] = normalize_manual_field_value(
                    field_name,
                    form_values.get(f"field_{field_name}", ""),
                )
            except ValueError as exc:
                return ActionResult(ok=False, message=f"{label}：{exc}")

        if missing_labels:
            return ActionResult(
                ok=False,
                message="必填字段未填写：" + "、".join(missing_labels),
            )

        waybill_id, waybill_no = self.repository.create_manual_waybill(fields, writer_id=writer_id)
        if writer_id:
            self.repository.upsert_writer(writer_id)
        return ActionResult(
            ok=True,
            message=f"手工单 {waybill_no} 已保存，请打印。",
            waybill_id=waybill_id,
            waybill_no=waybill_no,
        )

    def _merge_results(self, fields: dict[str, dict], results: dict[str, dict]) -> None:
        for field_name, result in results.items():
            if field_name not in fields:
                continue
            self._merge_single_result(field_name, fields[field_name], result)

    def _merge_single_result(self, field_name: str, entry: dict, result: dict) -> None:
        value = result.get("value", "")
        if value is None:
            value = ""
        entry["value"] = normalize_field_value(field_name, str(value))
        entry["confidence"] = round(float(result.get("confidence", 0.0) or 0.0), 4)
        entry["source"] = result.get("source", entry.get("source", "unknown"))
        entry["message"] = result.get("message", "")
        entry["status"] = "recognized" if entry["value"] else "missing"

    def _derive_status(self, fields: dict[str, dict], template_spec: dict[str, Any]) -> str:
        required_missing = []
        low_confidence = []
        for spec in template_spec["fields"]:
            entry = fields[spec["name"]]
            if spec.get("required", False) and not entry["value"]:
                required_missing.append(spec["name"])
            if entry["value"] and float(entry["confidence"]) < self.settings.confidence_threshold:
                low_confidence.append(spec["name"])
        if required_missing or low_confidence:
            return "review_required"
        return "ready"

    def _build_initial_fields(self, template_spec: dict[str, Any]) -> dict[str, dict]:
        fields = {}
        for field in template_spec["fields"]:
            fields[field["name"]] = {
                "label": field["label"],
                "value": "",
                "confidence": 0.0,
                "source": "pending",
                "message": "",
                "required": bool(field.get("required", False)),
            }
        return fields

    def coerce_fields(self, existing_fields: dict[str, Any], template_spec: dict[str, Any]) -> dict[str, dict]:
        normalized = self._build_initial_fields(template_spec)
        for field in template_spec["fields"]:
            field_name = field["name"]
            current = existing_fields.get(field_name, {}) if isinstance(existing_fields, dict) else {}
            normalized[field_name].update(
                {
                    "label": field["label"],
                    "value": normalize_field_value(field_name, str(current.get("value", "") or "")),
                    "confidence": float(current.get("confidence", 0.0) or 0.0),
                    "source": str(current.get("source", normalized[field_name]["source"]) or normalized[field_name]["source"]),
                    "message": str(current.get("message", "") or ""),
                    "required": bool(field.get("required", False)),
                }
            )
        return normalized

    def _get_template_spec(self, template_name: str | None = None) -> dict[str, Any]:
        if template_name:
            try:
                return self.template_store.get_template_spec(template_name)
            except FileNotFoundError:
                pass
        return self.template_store.get_active_template_spec()

    def _apply_quality_gate(self, fields: dict[str, dict], preprocess_info: dict[str, dict]) -> bool:
        quality = preprocess_info.get("quality", {})
        blocking_issues = quality.get("blocking_issues", [])
        if not blocking_issues:
            return False
        message = "Image quality needs manual review: " + ", ".join(quality_issue_messages(blocking_issues))
        for entry in fields.values():
            entry["source"] = "quality_gate"
            entry["message"] = message
            entry["status"] = "missing"
        return True

    def _quality_gate_debug(self, preprocess_info: dict[str, dict]) -> dict[str, object]:
        quality = preprocess_info.get("quality", {})
        return {
            "skipped": True,
            "reason": "quality_gate",
            "provider": "qwen_vl_ocr_gateway",
            "blocking_issues": quality.get("blocking_issues", []),
            "blocking_messages": quality_issue_messages(quality.get("blocking_issues", [])),
            "warning_issues": [],
            "warning_messages": [],
        }

    def _mark_document_error(
        self,
        document_id: int,
        raw_ocr: dict[str, object],
        error_message: str,
        source: str,
    ) -> None:
        queue_state = dict(raw_ocr.get("queue", {})) if isinstance(raw_ocr, dict) else {}
        queue_state["state"] = "error"
        queue_state["finished_at"] = self._now_string()
        raw_ocr["queue"] = queue_state
        raw_ocr["error"] = {
            "message": error_message,
            "source": source,
            "occurred_at": self._now_string(),
        }
        self.repository.update_document(
            document_id,
            raw_ocr=raw_ocr,
            status="error",
            error_message=error_message,
        )

    def _build_empty_raw_ocr(self, queue_attempts: int = 0) -> dict[str, object]:
        now = self._now_string()
        return {
            "qwen": {},
            "preprocess": {},
            "queue": {
                "state": "queued",
                "attempts": int(queue_attempts),
                "queued_at": now,
            },
        }

    def _build_processing_raw_ocr(self, document: dict) -> dict[str, object]:
        current = dict(document.get("raw_ocr", {}))
        queue_state = dict(current.get("queue", {}))
        attempts = int(queue_state.get("attempts", 0)) + 1
        queue_state.update(
            {
                "state": "processing",
                "attempts": attempts,
                "queued_at": queue_state.get("queued_at", self._now_string()),
                "started_at": self._now_string(),
            }
        )
        return {
            "qwen": {},
            "preprocess": {},
            "queue": queue_state,
        }

    def _finalize_queue_state(self, queue_state: dict[str, object], state: str) -> dict[str, object]:
        updated = dict(queue_state)
        updated["state"] = state
        updated["finished_at"] = self._now_string()
        return updated

    def _enqueue_document(self, document_id: int) -> bool:
        if self.task_queue is None:
            return False
        return self.task_queue.enqueue(document_id)

    def _build_original_relpath(self, token: str, safe_name: str) -> Path:
        day_folder = datetime.now().strftime("%Y%m%d")
        return Path("originals") / day_folder / f"{token}_{safe_name}"

    def _build_qwen_image_paths(self, preprocess_info: dict[str, Any], processed_abspath: Path) -> list[Path]:
        candidates = [
            Path(str(preprocess_info.get("ocr_input_path", "") or "").strip()),
            processed_abspath,
        ]
        image_paths: list[Path] = []
        for candidate in candidates:
            if not str(candidate):
                continue
            if not candidate.exists():
                continue
            if candidate in image_paths:
                continue
            image_paths.append(candidate)
        return image_paths or [processed_abspath]

    def _runtime_relpath(self, absolute_path: Path) -> str:
        return str(absolute_path.relative_to(self.settings.runtime_dir)).replace("\\", "/")

    def _now_string(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _merge_qwen_timing(self, current: dict[str, Any], fallback_total_ms: float) -> dict[str, float]:
        merged = dict(current) if isinstance(current, dict) else {}
        merged["total_ms"] = round(float(merged.get("total_ms", fallback_total_ms) or fallback_total_ms), 2)
        return {
            "main_pass_ms": round(float(merged.get("main_pass_ms", 0.0) or 0.0), 2),
            "second_pass_ms": round(float(merged.get("second_pass_ms", 0.0) or 0.0), 2),
            "region_rerun_ms": round(float(merged.get("region_rerun_ms", 0.0) or 0.0), 2),
            "postprocess_ms": round(float(merged.get("postprocess_ms", 0.0) or 0.0), 2),
            "total_ms": round(float(merged.get("total_ms", fallback_total_ms) or fallback_total_ms), 2),
        }

    def _build_timing_snapshot(self, timing_snapshot: dict[str, Any], qwen_debug: dict[str, Any]) -> dict[str, float]:
        qwen_timing = self._merge_qwen_timing(
            qwen_debug.get("timing", {}) if isinstance(qwen_debug, dict) else {},
            float(timing_snapshot.get("qwen_ms", 0.0) or 0.0),
        )
        return {
            "preprocess_ms": round(float(timing_snapshot.get("preprocess_ms", 0.0) or 0.0), 2),
            "qwen_ms": round(float(timing_snapshot.get("qwen_ms", 0.0) or 0.0), 2),
            "qwen_main_pass_ms": qwen_timing["main_pass_ms"],
            "qwen_second_pass_ms": qwen_timing["second_pass_ms"],
            "qwen_region_rerun_ms": qwen_timing["region_rerun_ms"],
            "qwen_postprocess_ms": qwen_timing["postprocess_ms"],
            "total_ms": round(float(timing_snapshot.get("total_ms", 0.0) or 0.0), 2),
        }

    def _log_document_timing(
        self,
        document: dict[str, Any],
        status: str,
        timing: dict[str, Any],
        note: str = "",
    ) -> None:
        payload = {
            "event": "ocr_timing",
            "document_id": int(document.get("id", 0) or 0),
            "doc_token": str(document.get("doc_token", "") or ""),
            "original_name": str(document.get("original_name", "") or ""),
            "status": status,
            "preprocess_ms": round(float(timing.get("preprocess_ms", 0.0) or 0.0), 2),
            "qwen_ms": round(float(timing.get("qwen_ms", 0.0) or 0.0), 2),
            "qwen_main_pass_ms": round(float(timing.get("qwen_main_pass_ms", 0.0) or 0.0), 2),
            "qwen_second_pass_ms": round(float(timing.get("qwen_second_pass_ms", 0.0) or 0.0), 2),
            "qwen_region_rerun_ms": round(float(timing.get("qwen_region_rerun_ms", 0.0) or 0.0), 2),
            "qwen_postprocess_ms": round(float(timing.get("qwen_postprocess_ms", 0.0) or 0.0), 2),
            "total_ms": round(float(timing.get("total_ms", 0.0) or 0.0), 2),
        }
        if note:
            payload["note"] = note
        serialized = json.dumps(payload, ensure_ascii=False)
        print(f"[ocr-timing] {serialized}")
        log_path = self.settings.runtime_dir / "logs" / "ocr_timing.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(serialized + "\n")


class LocalDocFlowApp:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.template_store = TemplateStore(self.settings)
        self.repository = DocumentRepository(self.settings)
        self.repository.initialize()
        self._session_secret = self.settings.session_secret or secrets.token_hex(32)
        if not self.settings.session_secret:
            print(
                "DOCFLOW_SESSION_SECRET is not set; using a temporary session secret for this process."
            )
        self._ensure_seed_admin_user()
        self.service = DocumentService(
            self.settings,
            self.repository,
            self.template_store,
            build_qwen_provider(self.settings),
        )
        self.task_queue = DocumentTaskQueue(self.settings.ocr_worker_count, self.service.process_document)
        self.service.attach_task_queue(self.task_queue)
        self.task_queue.start()
        self.recovered_documents = []  # self.service.recover_pending_documents()  # TEMP: skip DB
        self.template_env = Environment(
            loader=FileSystemLoader(MODULE_DIR / "templates"),
            autoescape=select_autoescape(["html", "xml"]),
        )
        self.template_env.filters["tojson_pretty"] = lambda value: json.dumps(
            value, ensure_ascii=False, indent=2
        )
        self.template_env.globals["ui_label"] = ui_label
        self.template_env.globals["current_admin_user"] = current_admin_user
        self.project_modules = self._build_project_modules()
        self.finance_service = FinanceService(self.repository, agent_request=self._agent_request)
        self.finance_service.initialize_schema()
        self.automation_virtual_task_state: dict[str, dict[str, Any]] = {}
        self.routes = ConsoleRouteDispatcher()

    def _ensure_seed_admin_user(self) -> None:
        if self.repository.count_admin_users() > 0:
            return
        username = self.settings.admin_seed_username
        password = self.settings.admin_seed_password
        if not username or not password:
            print(
                "No admin user exists. Set DOCFLOW_ADMIN_USERNAME and DOCFLOW_ADMIN_PASSWORD "
                "before starting the console to create the first admin account."
            )
            return
        self.repository.create_admin_user(
            username=username,
            display_name="系统管理员",
            password_hash=hash_admin_password(password),
            is_active=True,
        )
        print("Created the first admin account from DOCFLOW_ADMIN_USERNAME.")

    def run(self) -> None:
        handler = self._build_handler()
        server = ThreadingHTTPServer((self.settings.host, self.settings.port), handler)
        print(
            f"Logistics Agent local console: http://{self.settings.host}:{self.settings.port} "
            f"(Qwen workers={self.settings.ocr_worker_count})"
        )
        if self.recovered_documents:
            print(f"Recovered queued documents: {self.recovered_documents}")
        server.serve_forever()

    def _build_handler(self):
        app = self

        class RequestHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                app.handle_get(self)

            def do_POST(self) -> None:
                app.handle_post(self)

            def do_PUT(self) -> None:
                app.handle_proxy_write(self, "PUT")

            def do_PATCH(self) -> None:
                app.handle_proxy_write(self, "PATCH")

            def do_DELETE(self) -> None:
                app.handle_proxy_write(self, "DELETE")

            def log_message(self, fmt: str, *args) -> None:
                return

        return RequestHandler

    def handle_proxy_write(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        _CURRENT_ADMIN_USER.set(None)
        parsed = urlparse(handler.path)
        query = parse_qs(parsed.query)
        if not self._ensure_authorized(handler):
            return

        if parsed.path.startswith(RONGHUI_RECEIPT_LIVE_PROXY_PREFIX):
            self._handle_ronghui_receipt_live_proxy(handler, parsed.path, method=method.upper(), query=query)
            return
        if parsed.path.startswith(YUNDA_RECEIPT_LIVE_PROXY_PREFIX):
            self._handle_yunda_receipt_live_proxy(handler, parsed.path, method=method.upper(), query=query)
            return
        if parsed.path.startswith(RONGHUI_LIVE_PROXY_PREFIX):
            self._handle_ronghui_live_proxy(handler, parsed.path, method=method.upper(), query=query)
            return
        if parsed.path.startswith(YUNDA_LIVE_PROXY_PREFIX):
            self._handle_yunda_live_proxy(handler, parsed.path, method=method.upper(), query=query)
            return
        self._send_json(
            handler,
            HTTPStatus.NOT_FOUND,
            {"ok": False, "message": "代理路径不存在。", "error_code": "INVALID_PROXY_PATH"},
        )

    def handle_get(self, handler: BaseHTTPRequestHandler) -> None:
        _CURRENT_ADMIN_USER.set(None)
        parsed = urlparse(handler.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        query = parse_qs(parsed.query)

        if path.startswith("/static/"):
            relpath = path[len("/static/") :]
            self._serve_static_file(handler, relpath)
            return
        if path == "/login":
            self._render_login(handler, query)
            return
        if not self._ensure_authorized(handler):
            return
        if self.routes.handle_get(self, handler, path, parsed.path, query):
            return

        if path in {"/", "/portal"}:
            self._render_portal(handler, query)
            return
        if path == "/monitoring/summary":
            self._handle_monitoring_summary(handler, query)
            return
        if path == "/monitoring/daily-sign":
            self._handle_monitoring_daily_sign(handler, query)
            return
        if path == "/monitoring/stream":
            self._handle_monitoring_stream(handler, query)
            return
        if path == "/monitoring/detail-link":
            self._handle_monitoring_detail_link(handler, query)
            return
        if path in {"/ocr", "/workspaces/ocr"}:
            self._render_ocr_workspace(handler, query)
            return
        if path == "/ocr/boyi/frame":
            frame_query = dict(query)
            frame_query["boyi_frame"] = ["1"]
            self._render_ocr_workspace(handler, frame_query)
            return
        if path == "/receipts":
            self._render_receipts(handler, query)
            return
        if path == "/modules/customer-service":
            self._render_customer_service(handler, query)
            return
        if path == "/modules/finance":
            self._render_finance(handler, query)
            return
        if path == "/finance/summary":
            self._handle_finance_get(handler, "summary", query)
            return
        if path == "/finance/trend":
            self._handle_finance_get(handler, "trend", query)
            return
        if path == "/finance/entries":
            self._handle_finance_get(handler, "entries", query)
            return
        if path == "/finance/fee-mappings":
            self._handle_finance_get(handler, "fee_mappings", query)
            return
        if path == "/finance/sync-batches":
            self._handle_finance_get(handler, "sync_batches", query)
            return
        if path == "/customer-service/problems/attachments/preview":
            self._handle_customer_service_attachment_preview(handler, query)
            return
        if path == "/customer-service/problem-settings":
            self._handle_customer_service_problem_settings_get(handler)
            return
        if path == "/receipts/data":
            self._handle_receipts_data(handler, query)
            return
        if path == "/receipts/download-images":
            self._handle_receipts_image_archive(handler, query)
            return
        if path.startswith("/receipts/attachments/"):
            self._handle_receipt_attachment(handler, path, query)
            return
        if parsed.path.startswith(RONGHUI_RECEIPT_LIVE_PROXY_PREFIX):
            self._handle_ronghui_receipt_live_proxy(handler, parsed.path, method="GET", query=query)
            return
        if parsed.path.startswith(YUNDA_RECEIPT_LIVE_PROXY_PREFIX):
            self._handle_yunda_receipt_live_proxy(handler, parsed.path, method="GET", query=query)
            return
        if path.startswith("/receipts/"):
            self._handle_receipt_detail(handler, path)
            return
        if parsed.path.startswith(RONGHUI_LIVE_PROXY_PREFIX):
            self._handle_ronghui_live_proxy(handler, parsed.path, method="GET", query=query)
            return
        if parsed.path.startswith(YUNDA_LIVE_PROXY_PREFIX):
            self._handle_yunda_live_proxy(handler, parsed.path, method="GET", query=query)
            return
        if path == "/dispatch":
            self._render_dispatch(handler, query)
            return
        if path == "/tracking":
            self._render_tracking(handler, query)
            return
        if path == "/waybills":
            self._render_waybills(handler, query)
            return
        if path == "/line-haul-contacts":
            self._render_line_haul_contacts(handler, query)
            return
        if path.startswith("/waybills/") and path.endswith("/print"):
            waybill_id = self._parse_document_id(path)
            if waybill_id is None:
                self._send_text(handler, HTTPStatus.NOT_FOUND, "Waybill not found.")
                return
            self._render_waybill_print(handler, waybill_id, query)
            return
        if path in {"/automations", "/workspaces/automations"}:
            self._render_automations(handler, query)
            return
        if path.startswith("/automation-accounts/") and path.endswith("/status"):
            self._handle_automation_account_status_get(handler, path, query)
            return
        if path == "/automation-accounts/statuses":
            self._handle_automation_accounts_statuses_get(handler, query)
            return
        if path == "/automation-accounts":
            self._render_automation_accounts(handler, query)
            return
        if path == "/settings/accounts":
            self._render_admin_accounts(handler, query)
            return
        if path == "/automations/session-context":
            self._handle_automation_session_context(handler, query)
            return
        session_route = self._automation_session_route(path)
        if session_route and session_route[1] == "/status":
            self._handle_tms_session_status(handler, profile=session_route[0], query=query)
            return
        if path == "/automations/tms-session/status":
            self._handle_tms_session_status(handler, query=query)
            return
        if path == "/templates/new":
            self._render_template_editor(handler, None, query)
            return
        if path.startswith("/templates/") and path.endswith("/edit"):
            template_name = unquote(path[len("/templates/") : -len("/edit")].strip("/"))
            if not template_name:
                self._send_text(handler, HTTPStatus.NOT_FOUND, "Template not found.")
                return
            self._render_template_editor(handler, template_name, query)
            return
        if path.startswith("/modules/"):
            slug = path[len("/modules/") :].strip("/")
            if not slug:
                self._send_text(handler, HTTPStatus.NOT_FOUND, "Module not found.")
                return
            self._render_module(handler, slug, query)
            return
        if path == "/automations/tasks/output":
            self._handle_automation_task_output(handler, query)
            return
        if path.startswith("/documents/") and path.endswith("/export.json"):
            document_id = self._parse_document_id(path)
            if document_id is None:
                self._send_text(handler, HTTPStatus.NOT_FOUND, "Document not found.")
                return
            self._export_document_json(handler, document_id)
            return
        if path.startswith("/documents/"):
            document_id = self._parse_document_id(path)
            if document_id is None:
                self._send_text(handler, HTTPStatus.NOT_FOUND, "Document not found.")
                return
            self._render_document(handler, document_id, query)
            return
        if path.startswith("/runtime/"):
            relpath = path[len("/runtime/") :]
            self._serve_runtime_file(handler, relpath)
            return
        self._send_text(handler, HTTPStatus.NOT_FOUND, "Not found")

    def handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        _CURRENT_ADMIN_USER.set(None)
        parsed = urlparse(handler.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/login":
            self._handle_login(handler)
            return
        if not self._ensure_authorized(handler):
            return
        query = parse_qs(parsed.query)
        if self.routes.handle_post(self, handler, path, parsed.path, query):
            return
        if path == "/logout":
            self._handle_logout(handler)
            return
        if path == "/settings/profile/avatar":
            self._handle_admin_avatar_upload(handler)
            return
        if path == "/settings/accounts/create":
            self._handle_admin_account_create(handler)
            return
        if path.startswith("/settings/accounts/") and path.endswith("/toggle"):
            self._handle_admin_account_toggle(handler, path)
            return
        if path.startswith("/settings/accounts/") and path.endswith("/reset-password"):
            self._handle_admin_account_reset_password(handler, path)
            return
        if self._handle_automation_account_post(handler, path):
            return

        if path == "/customer-service/problem-settings":
            self._handle_customer_service_problem_settings_post(handler)
            return
        if path == "/customer-service/problems/query":
            self._handle_customer_service_problem_query(handler)
            return
        if path == "/customer-service/problems/detail":
            self._handle_customer_service_problem_agent_action(handler, "detail")
            return
        if path == "/customer-service/problems/mark-read":
            self._handle_customer_service_problem_agent_action(handler, "mark_read")
            return
        if path == "/customer-service/problems/reply":
            self._handle_customer_service_problem_agent_action(handler, "reply")
            return
        if path == "/customer-service/problems/publish":
            self._handle_customer_service_problem_agent_action(handler, "publish")
            return
        if path == "/customer-service/problems/attachments/upload":
            self._handle_customer_service_attachment_upload(handler)
            return

        if path == "/finance/sync":
            self._handle_finance_post(handler, "sync")
            return
        if path == "/finance/backfill":
            self._handle_finance_post(handler, "backfill")
            return
        if re.fullmatch(r"/finance/fee-mappings/\d+", path):
            self._handle_finance_post(handler, "save_mapping", path=path)
            return
        if re.fullmatch(r"/finance/sync-batches/\d+/retry", path):
            self._handle_finance_post(handler, "retry_batch", path=path)
            return

        if path == "/tracking/query":
            self._handle_tracking_query(handler)
            return
        if path == "/receipts/sync":
            self._handle_receipts_sync(handler)
            return
        if path.startswith("/receipts/") and path.endswith("/audit"):
            self._handle_receipt_audit(handler, path)
            return
        if parsed.path.startswith(RONGHUI_RECEIPT_LIVE_PROXY_PREFIX):
            self._handle_ronghui_receipt_live_proxy(handler, parsed.path, method="POST", query=parse_qs(parsed.query))
            return
        if parsed.path.startswith(YUNDA_RECEIPT_LIVE_PROXY_PREFIX):
            self._handle_yunda_receipt_live_proxy(handler, parsed.path, method="POST", query=parse_qs(parsed.query))
            return
        if parsed.path.startswith(RONGHUI_LIVE_PROXY_PREFIX):
            self._handle_ronghui_live_proxy(handler, parsed.path, method="POST", query=parse_qs(parsed.query))
            return
        if parsed.path.startswith(YUNDA_LIVE_PROXY_PREFIX):
            self._handle_yunda_live_proxy(handler, parsed.path, method="POST", query=parse_qs(parsed.query))
            return
        if path.startswith("/ocr/yunda/"):
            self._handle_yunda_entry(handler, path)
            return
        if path in {"/upload", "/ocr/upload"}:
            self._handle_upload(handler)
            return
        if path == "/waybills/quote-options":
            self._handle_quote_options(handler)
            return
        if path == "/waybills/manual":
            self._handle_manual_waybill(handler)
            return
        if path.startswith("/waybills/") and path.endswith("/status"):
            waybill_id = self._parse_document_id(path)
            if waybill_id is None:
                self._send_text(handler, HTTPStatus.NOT_FOUND, "Not found")
                return
            self._handle_waybill_status_update(handler, waybill_id)
            return
        if path == "/line-haul-contacts/create":
            self._handle_line_haul_contact_create(handler)
            return
        if path == "/line-haul-contacts/import-paste":
            self._handle_line_haul_contact_import_paste(handler)
            return
        if path.startswith("/line-haul-contacts/") and path.endswith("/update"):
            self._handle_line_haul_contact_update(handler, path)
            return
        if path == "/templates/select":
            self._handle_template_select(handler)
            return
        if path == "/templates/save":
            self._handle_template_save(handler)
            return
        if path == "/automations/resources/save":
            self._handle_automation_resource_save(handler)
            return
        if path == "/automations/tasks/save":
            self._handle_automation_task_save(handler)
            return
        if path == "/automations/tasks/run-now":
            self._handle_automation_task_run_now(handler)
            return
        if path == "/automations/tasks/cancel":
            self._handle_automation_task_cancel(handler)
            return
        session_route = self._automation_session_route(path)
        if session_route and self._handle_automation_session_post(handler, session_route[0], session_route[1]):
            return
        if path == "/automations/tms-session/send-code":
            self._handle_tms_session_action(
                handler,
                endpoint="/admin/tms/session/send-code",
                payload={},
                success_message="TMS融辉登录已提交；如出现图片验证码，请按图输入后提交。",
                timeout=90,
            )
            return
        if path == "/automations/tms-session/save-credentials":
            values = self._parse_urlencoded_form(handler)
            self._handle_tms_session_action(
                handler,
                endpoint="/admin/tms/session/credentials",
                payload={
                    "username": str(values.get("username", "") or "").strip(),
                    "password": str(values.get("password", "") or ""),
                    "phone": str(values.get("phone", "") or "").strip(),
                },
                success_message="TMS 默认登录配置已保存。",
                timeout=20,
            )
            return
        if path == "/automations/tms-session/clear-credentials":
            self._handle_tms_session_action(
                handler,
                endpoint="/admin/tms/session/credentials/clear",
                payload={},
                success_message="TMS 默认登录配置已清空。",
                timeout=20,
            )
            return
        if path == "/automations/tms-session/submit-code":
            values = self._parse_urlencoded_form(handler)
            sms_code = str(values.get("code", "") or "").strip()
            if not sms_code:
                self._respond_tms_action(
                    handler,
                    ok=False,
                    message="验证码不能为空。",
                    kind="warning",
                    http_status=HTTPStatus.BAD_REQUEST,
                )
                return
            self._handle_tms_session_action(
                handler,
                endpoint="/admin/tms/session/submit-code",
                payload={"code": sms_code},
                success_message="TMS 登录成功，共享登录态已更新。",
                timeout=45,
            )
            return
        if path == "/automations/tms-session/clear":
            self._handle_tms_session_action(
                handler,
                endpoint="/admin/tms/session/clear",
                payload={},
                success_message="TMS 登录态已清除。",
                timeout=20,
            )
            return
        if path == "/automations/admin/import-phase7-resources":
            self._handle_automation_admin_action(
                handler,
                endpoint="/admin/import-phase7-resources",
                success_message="Phase 7 资源已重新导入。",
            )
            return
        if path == "/automations/admin/seed-phase7-tasks":
            self._handle_automation_admin_action(
                handler,
                endpoint="/admin/seed-phase7-tasks",
                success_message="Phase 7 默认任务模板已写入并重载调度。",
            )
            return
        if path == "/automations/admin/reload":
            self._handle_automation_admin_action(
                handler,
                endpoint="/admin/reload",
                success_message="Agent 运行时配置已重载。",
            )
            return
        if path.startswith("/documents/") and path.endswith("/review"):
            document_id = self._parse_document_id(path)
            if document_id is None:
                self._send_text(handler, HTTPStatus.NOT_FOUND, "Document not found.")
                return
            self._handle_review(handler, document_id)
            return
        if path.startswith("/documents/") and path.endswith("/reprocess"):
            document_id = self._parse_document_id(path)
            if document_id is None:
                self._send_text(handler, HTTPStatus.NOT_FOUND, "Document not found.")
                return
            self._handle_reprocess(handler, document_id)
            return
        if path.startswith("/documents/") and path.endswith("/delete"):
            document_id = self._parse_document_id(path)
            if document_id is None:
                self._send_text(handler, HTTPStatus.NOT_FOUND, "Document not found.")
                return
            self._handle_delete(handler, document_id)
            return
        self._send_text(handler, HTTPStatus.NOT_FOUND, "Not found")

    def _ensure_authorized(self, handler: BaseHTTPRequestHandler) -> bool:
        user = self._authenticated_user_from_request(handler)
        if user:
            return True

        if self._is_ajax_request(handler):
            self._send_json(
                handler,
                HTTPStatus.UNAUTHORIZED,
                {"ok": False, "message": "请先登录后台。", "login_url": "/login"},
            )
            return False

        next_url = quote(handler.path or "/", safe="")
        self._redirect(handler, f"/login?next={next_url}")
        return False

    def _authenticated_user_from_request(self, handler: BaseHTTPRequestHandler) -> dict[str, Any] | None:
        legacy_user = self._legacy_basic_auth_user(handler)
        if legacy_user:
            self._set_current_admin_user(handler, legacy_user)
            return legacy_user

        session_id = self._session_id_from_cookie(handler)
        if not session_id:
            return None
        session = self.repository.get_admin_session(session_id)
        if not session:
            return None
        if not bool(session.get("is_active")):
            self.repository.delete_admin_session(session_id)
            return None

        expires_at = self._coerce_datetime(session.get("expires_at"))
        if expires_at <= datetime.now():
            self.repository.delete_admin_session(session_id)
            return None

        self.repository.touch_admin_session(session_id)
        user = {
            "id": int(session.get("user_id") or 0),
            "username": str(session.get("username") or ""),
            "display_name": str(session.get("display_name") or ""),
            "avatar_path": str(session.get("avatar_path") or ""),
            "avatar_url": self._admin_avatar_url(str(session.get("avatar_path") or "")),
            "is_legacy_basic_auth": False,
        }
        self._set_current_admin_user(handler, user)
        return user

    def _legacy_basic_auth_user(self, handler: BaseHTTPRequestHandler) -> dict[str, Any] | None:
        username = getattr(self.settings, "basic_auth_user", "")
        password = getattr(self.settings, "basic_auth_password", "")
        if not username or not password:
            return None

        auth_header = handler.headers.get("Authorization", "")
        expected_token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        if not hmac.compare_digest(auth_header, f"Basic {expected_token}"):
            return None
        return {
            "id": 0,
            "username": username,
            "display_name": username,
            "avatar_path": "",
            "avatar_url": "",
            "is_legacy_basic_auth": True,
        }

    def _set_current_admin_user(self, handler: BaseHTTPRequestHandler, user: dict[str, Any]) -> None:
        setattr(handler, "current_admin_user", user)
        _CURRENT_ADMIN_USER.set(user)

    def _session_id_from_cookie(self, handler: BaseHTTPRequestHandler) -> str:
        raw_cookie = str(handler.headers.get("Cookie") or "")
        if not raw_cookie:
            return ""
        cookie = SimpleCookie()
        try:
            cookie.load(raw_cookie)
        except Exception:
            return ""
        morsel = cookie.get(ADMIN_SESSION_COOKIE)
        if morsel is None:
            return ""
        return self._decode_session_cookie(morsel.value)

    def _decode_session_cookie(self, cookie_value: str) -> str:
        raw = str(cookie_value or "")
        session_id, separator, signature = raw.partition(".")
        if not session_id or not separator or not signature:
            return ""
        expected = self._sign_session_id(session_id)
        if not hmac.compare_digest(signature, expected):
            return ""
        return session_id

    def _encode_session_cookie(self, session_id: str) -> str:
        return f"{session_id}.{self._sign_session_id(session_id)}"

    def _sign_session_id(self, session_id: str) -> str:
        secret = getattr(self, "_session_secret", "") or getattr(self.settings, "session_secret", "")
        return hmac.new(secret.encode("utf-8"), session_id.encode("utf-8"), hashlib.sha256).hexdigest()

    def _build_session_cookie_header(self, cookie_value: str, *, max_age: int) -> str:
        secure = "; Secure" if getattr(self.settings, "session_cookie_secure", False) else ""
        return (
            f"{ADMIN_SESSION_COOKIE}={cookie_value}; Path=/; HttpOnly; "
            f"SameSite=Lax; Max-Age={max_age}{secure}"
        )

    def _clear_session_cookie_header(self) -> str:
        secure = "; Secure" if getattr(self.settings, "session_cookie_secure", False) else ""
        return (
            f"{ADMIN_SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; "
            f"Max-Age=0{secure}"
        )

    def _coerce_datetime(self, value: Any) -> datetime:
        if isinstance(value, datetime):
            return value
        text = str(value or "").strip()
        if not text:
            return datetime.min
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return datetime.min

    def _clean_next_url(self, raw_url: str) -> str:
        candidate = str(raw_url or "").strip() or "/"
        parsed = urlparse(candidate)
        if parsed.scheme or parsed.netloc or not candidate.startswith("/") or candidate.startswith("//"):
            return "/"
        return candidate

    def _render_login(self, handler: BaseHTTPRequestHandler, query: dict) -> None:
        user = self._authenticated_user_from_request(handler)
        next_url = self._clean_next_url(query.get("next", ["/"])[0])
        if user:
            self._redirect(handler, next_url)
            return

        template = self.template_env.get_template("login.html")
        body = template.render(
            app_title=self.settings.app_title,
            next_url=next_url,
            username_value=query.get("username", [""])[0],
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
            has_admin_users=self.repository.count_admin_users() > 0,
        )
        self._send_html(handler, body)

    def _handle_login(self, handler: BaseHTTPRequestHandler) -> None:
        values = self._parse_urlencoded_form(handler)
        username = str(values.get("username", "") or "").strip()
        password = str(values.get("password", "") or "")
        next_url = self._clean_next_url(values.get("next", "/"))
        user = self.repository.get_admin_user_by_username(username)
        if not user or not bool(user.get("is_active")) or not verify_admin_password(password, str(user.get("password_hash") or "")):
            template = self.template_env.get_template("login.html")
            body = template.render(
                app_title=self.settings.app_title,
                next_url=next_url,
                username_value=username,
                message="账号或密码不正确。",
                message_kind="warning",
                has_admin_users=self.repository.count_admin_users() > 0,
            )
            self._send_html(handler, body, status=HTTPStatus.UNAUTHORIZED)
            return

        now = datetime.now()
        ttl_hours = getattr(self.settings, "session_ttl_hours", 12)
        expires_at = now + timedelta(hours=ttl_hours)
        self.repository.delete_expired_admin_sessions(now)
        session_id = secrets.token_urlsafe(32)
        self.repository.create_admin_session(
            session_id=session_id,
            user_id=int(user["id"]),
            expires_at=expires_at,
        )
        self.repository.record_admin_login(int(user["id"]))
        cookie_value = self._encode_session_cookie(session_id)
        cookie_header = self._build_session_cookie_header(
            cookie_value,
            max_age=int(ttl_hours) * 3600,
        )
        self._redirect(handler, next_url, headers=[("Set-Cookie", cookie_header)])

    def _handle_logout(self, handler: BaseHTTPRequestHandler) -> None:
        session_id = self._session_id_from_cookie(handler)
        if session_id:
            self.repository.delete_admin_session(session_id)
        self._redirect(
            handler,
            "/login?message=%E5%B7%B2%E9%80%80%E5%87%BA%E5%90%8E%E5%8F%B0%E3%80%82&kind=success",
            headers=[("Set-Cookie", self._clear_session_cookie_header())],
        )

    def _render_admin_accounts(self, handler: BaseHTTPRequestHandler, query: dict) -> None:
        template = self.template_env.get_template("admin_accounts.html")
        body = template.render(
            app_title=self.settings.app_title,
            users=self.repository.list_admin_users(),
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
        )
        self._send_html(handler, body)

    def _render_automation_accounts(self, handler: BaseHTTPRequestHandler, query: dict) -> None:
        accounts, account_warning = self._fetch_automation_accounts(force=False, prefer_cached=True)
        account_groups = self._automation_account_groups(accounts)
        account_system_counts = {group["system"]: group["count"] for group in account_groups}
        valid_systems = set(AUTOMATION_ACCOUNT_SYSTEM_ORDER) | set(account_system_counts)
        requested_system = str(query.get("system", [""])[0] or "").strip().lower()
        account_filter = requested_system if requested_system in valid_systems else ""
        account_rows = [account for group in account_groups for account in group["accounts"]]
        account_tab_systems = [
            system
            for system in AUTOMATION_ACCOUNT_SYSTEM_ORDER
            if account_system_counts.get(system, 0) > 0
        ]
        account_tab_systems.extend(
            sorted(
                system
                for system, count in account_system_counts.items()
                if count > 0 and system not in AUTOMATION_ACCOUNT_SYSTEM_ORDER
            )
        )
        template = self.template_env.get_template("automation_accounts.html")
        body = template.render(
            app_title=self.settings.app_title,
            accounts=accounts,
            account_groups=account_groups,
            account_rows=account_rows,
            account_filter=account_filter,
            account_filter_label=(
                f"{AUTOMATION_ACCOUNT_SYSTEM_LABELS.get(account_filter, account_filter)} "
                if account_filter
                else ""
            ),
            account_total_count=len(accounts),
            account_system_counts=account_system_counts,
            account_tab_systems=account_tab_systems,
            account_system_labels=AUTOMATION_ACCOUNT_SYSTEM_LABELS,
            account_system_order=AUTOMATION_ACCOUNT_SYSTEM_ORDER,
            account_warning=account_warning,
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
        )
        self._send_html(handler, body)

    def _query_bool(self, query: dict | None, name: str, default: bool = False) -> bool:
        raw = str((query or {}).get(name, ["1" if default else ""])[0] or "").strip().lower()
        if not raw:
            return default
        return raw in {"1", "true", "yes", "on"}

    def _handle_automation_account_status_get(
        self,
        handler: BaseHTTPRequestHandler,
        path: str,
        query: dict | None = None,
    ) -> None:
        prefix = "/automation-accounts/"
        suffix = "/status"
        account_id = unquote(path[len(prefix) : -len(suffix)].strip("/"))
        if not account_id:
            self._send_json(
                handler,
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "message": "账号不存在。", "kind": "warning"},
            )
            return
        status_result = self._fetch_automation_account_status_state(
            account_id,
            force=self._query_bool(query, "force", True),
        )
        if not status_result.get("ok"):
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {
                    "ok": False,
                    "message": status_result.get("message") or "账号状态获取失败。",
                    "kind": "warning",
                },
            )
            return
        self._send_json(handler, HTTPStatus.OK, {"ok": True, "state": status_result.get("state") or {}})

    def _handle_automation_accounts_statuses_get(
        self,
        handler: BaseHTTPRequestHandler,
        query: dict | None = None,
    ) -> None:
        accounts, warning = self._fetch_automation_accounts(
            force=self._query_bool(query, "force", False),
            prefer_cached=self._query_bool(query, "prefer_cached", True),
        )
        if warning:
            self._send_json(handler, HTTPStatus.BAD_GATEWAY, {"ok": False, "message": warning, "accounts": []})
            return
        meta = getattr(self, "_automation_accounts_cache_meta", {})
        self._send_json(handler, HTTPStatus.OK, {"ok": True, "accounts": accounts, **meta})

    def _fetch_automation_accounts(
        self,
        *,
        force: bool = True,
        prefer_cached: bool = False,
    ) -> tuple[list[dict[str, Any]], str]:
        query_params = {}
        if force:
            query_params["force"] = "1"
        if prefer_cached:
            query_params["prefer_cached"] = "1"
        endpoint = "/admin/accounts"
        if query_params:
            endpoint = f"{endpoint}?{urlencode(query_params)}"
        self._automation_accounts_cache_meta = {}
        result = self._agent_request("GET", endpoint, timeout=12 if prefer_cached else 45 if force else 12)
        if not result.get("ok"):
            return [], normalize_feedback_text(result.get("error") or "Agent 当前不可达，无法获取业务账号状态。")
        payload = result.get("data")
        if not isinstance(payload, dict):
            return [], "Agent 账号接口返回了无效数据。"
        if payload.get("ok") is False:
            return [], normalize_feedback_text(payload.get("message") or payload.get("error") or "Agent 账号接口调用失败。")
        raw_accounts = payload.get("accounts")
        if not isinstance(raw_accounts, list):
            return [], "Agent 账号接口缺少 accounts 列表。"
        self._automation_accounts_cache_meta = {
            key: payload[key]
            for key in ("cached", "stale", "refreshing", "cache_age_sec")
            if key in payload
        }

        accounts: list[dict[str, Any]] = []
        for item in raw_accounts:
            if not isinstance(item, dict):
                continue
            account = dict(item)
            system = str(account.get("system") or "").strip().lower()
            if system == "price":
                system = "ronghui"
                account["account_purpose"] = account.get("account_purpose") or "price"
            account["system"] = system
            account["system_label"] = str(
                account.get("system_label")
                or AUTOMATION_ACCOUNT_SYSTEM_LABELS.get(system, system or "-")
            )
            if system == "ronghui":
                account["system_label"] = AUTOMATION_ACCOUNT_SYSTEM_LABELS["ronghui"]
            account["name"] = str(account.get("name") or account.get("account_id") or "").strip()
            status = dict(account.get("status") if isinstance(account.get("status"), dict) else {})
            credentials = account.get("credentials") if isinstance(account.get("credentials"), dict) else {}
            safe_credentials = dict(credentials)
            safe_credentials["password"] = ""
            has_manual_credentials = bool(
                safe_credentials.get("has_manual_credentials")
                or status.get("has_manual_credentials")
            )
            has_env_credentials = bool(
                safe_credentials.get("has_env_credentials")
                or status.get("has_env_credentials")
            )
            credential_source = str(
                safe_credentials.get("credential_source")
                or status.get("credential_source")
                or ""
            ).strip()
            has_saved_credentials = bool(
                safe_credentials.get("has_saved_credentials")
                or status.get("has_saved_credentials")
                or has_manual_credentials
                or has_env_credentials
            )
            if has_manual_credentials:
                credentials_label = "已保存账号密码"
                credentials_tone = "success"
            elif has_env_credentials or credential_source == "env":
                credentials_label = "环境变量凭据"
                credentials_tone = "success"
            else:
                credentials_label = "未保存账号密码"
                credentials_tone = "warning"
            raw_status_value = str(status.get("status") or "").strip()
            status_label = str(status.get("label") or "")
            status_tone = str(status.get("status_tone") or "")
            status_note = ""
            if bool(account.get("session_capable")) and raw_status_value == "authenticated" and not has_saved_credentials:
                status_label = "登录态有效"
                status_tone = "warning"
                status_note = "当前只检测到浏览器登录态，未保存账号密码；登录态失效后需重新登录。"
            elif bool(account.get("session_capable")) and raw_status_value == "authenticated" and (has_env_credentials or credential_source == "env"):
                status_note = "账号密码来自环境变量，编辑框不会回显。"
            status["label"] = status_label
            status["status_tone"] = status_tone
            status["status_note"] = status_note
            status["has_saved_credentials"] = has_saved_credentials
            status["has_manual_credentials"] = has_manual_credentials
            status["has_env_credentials"] = has_env_credentials
            status["credential_source"] = credential_source
            account["status"] = status
            account["credentials"] = safe_credentials
            account["status_label"] = status_label
            account["status_tone"] = status_tone
            account["status_note"] = status_note
            account["credential_source"] = credential_source
            account["has_saved_credentials"] = has_saved_credentials
            account["has_manual_credentials"] = has_manual_credentials
            account["has_env_credentials"] = has_env_credentials
            account["credentials_label"] = credentials_label
            account["credentials_tone"] = credentials_tone
            accounts.append(account)
        return accounts, ""

    def _fetch_automation_account_status_state(self, account_id: str, *, force: bool = True) -> dict[str, Any]:
        quoted_id = quote(str(account_id or "").strip(), safe="")
        if not quoted_id:
            return {"ok": False, "message": "账号不存在。"}
        suffix = "?force=1" if force else ""
        result = self._agent_request("GET", f"/admin/accounts/{quoted_id}/status{suffix}", timeout=35)
        if not result.get("ok"):
            return {
                "ok": False,
                "message": f"Agent 调用失败：{normalize_feedback_text(result.get('error') or 'unknown error')}",
            }
        payload = result.get("data")
        if not isinstance(payload, dict):
            return {"ok": False, "message": "Agent 账号状态接口返回了无效数据。"}
        if payload.get("ok") is False:
            return {
                "ok": False,
                "message": normalize_feedback_text(
                    payload.get("message") or payload.get("error") or "账号状态获取失败。"
                ),
            }
        state = dict(payload)
        state.pop("ok", None)
        return {"ok": True, "state": state}

    def _automation_account_groups(self, accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows_by_system: dict[str, list[dict[str, Any]]] = {
            system: [] for system in AUTOMATION_ACCOUNT_SYSTEM_ORDER
        }
        for account in accounts:
            system = str(account.get("system") or "").strip().lower()
            rows_by_system.setdefault(system, []).append(account)

        groups: list[dict[str, Any]] = []
        for system in [*AUTOMATION_ACCOUNT_SYSTEM_ORDER, *sorted(set(rows_by_system) - set(AUTOMATION_ACCOUNT_SYSTEM_ORDER))]:
            rows = sorted(
                rows_by_system.get(system, []),
                key=lambda item: (
                    not bool(item.get("is_default")),
                    not bool(item.get("is_active", True)),
                    str(item.get("name") or item.get("account_id") or ""),
                ),
            )
            groups.append(
                {
                    "system": system,
                    "label": AUTOMATION_ACCOUNT_SYSTEM_LABELS.get(system, system or "-"),
                    "accounts": rows,
                    "count": len(rows),
                }
            )
        return groups

    def _automation_account_options_by_system(self, accounts: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        options: dict[str, list[dict[str, Any]]] = {}
        for account in accounts:
            if not bool(account.get("is_active", True)):
                continue
            system = str(account.get("system") or "").strip().lower()
            options.setdefault(system, []).append(account)
        for system, rows in options.items():
            rows.sort(
                key=lambda item: (
                    not bool(item.get("is_default")),
                    str(item.get("name") or item.get("account_id") or ""),
                )
            )
        return options

    def _automation_task_account_roles(
        self,
        task_id: str,
        workflow: dict[str, Any] | None = None,
        tool_name: str = "",
        provider: str = "",
    ) -> list[dict[str, Any]]:
        workflow = workflow or automation_workflow_definition(task_id)
        raw_roles = workflow.get("account_roles")
        roles = raw_roles if isinstance(raw_roles, list) else []
        if not roles:
            normalized = normalize_task_group_id(task_id)
            tool_name_value = str(tool_name or workflow.get("tool_name") or "").strip()
            provider_value = str(provider or "").strip().lower()
            if tool_name_value.startswith("r7_") or normalized.startswith("r7_"):
                roles = [{"label": "运行账号", "field": "account_id", "system": "r7", "default_account_id": "r7_default"}]
            elif tool_name_value == "sync_daily_should_sign" or normalized.startswith("daily_sign"):
                roles = [
                    {"label": "R13应签查询账号", "field": "r13_account_id", "system": "r13", "default_account_id": "r13_default"},
                    {"label": "补地址账号", "field": "detail_account_id", "system": "ronghui", "default_account_id": "ronghui_default"},
                ]
            elif provider_value == "yunda" or tool_name_value.startswith("sync_yunda_"):
                roles = [{"label": "运行账号", "field": "account_id", "system": "yunda", "default_account_id": "yunda_default"}]
            elif (
                "price" in tool_name_value.lower()
                or normalized.startswith("price")
                or tool_name_value == "ronghui_waybill_proxy"
                or normalized == "ronghui_waybill_proxy"
            ):
                roles = [{"label": "运行账号", "field": "account_id", "system": "ronghui", "default_account_id": "price_default"}]
            else:
                roles = [{"label": "运行账号", "field": "account_id", "system": "ronghui", "default_account_id": "ronghui_default"}]

        normalized_roles: list[dict[str, Any]] = []
        for role in roles:
            if not isinstance(role, dict):
                continue
            system = str(role.get("system") or "").strip().lower()
            if system not in AUTOMATION_ACCOUNT_SYSTEM_LABELS:
                continue
            field = str(role.get("field") or "account_id").strip() or "account_id"
            normalized_roles.append(
                {
                    "label": str(role.get("label") or "运行账号").strip() or "运行账号",
                    "field": field,
                    "system": system,
                    "system_label": AUTOMATION_ACCOUNT_SYSTEM_LABELS.get(system, system),
                    "default_account_id": str(
                        role.get("default_account_id")
                        or AUTOMATION_DEFAULT_ACCOUNT_IDS.get(system, "")
                    ).strip(),
                    "required": bool(role.get("required", True)),
                }
            )
        return normalized_roles

    def _legacy_task_account_system(
        self,
        task_id: str,
        workflow: dict[str, Any] | None = None,
        tool_name: str = "",
        provider: str = "",
    ) -> str:
        roles = self._automation_task_account_roles(task_id, workflow, tool_name, provider)
        return str((roles[0] if roles else {}).get("system") or "ronghui")

    def _legacy_task_account_purpose(
        self,
        task_id: str,
        workflow: dict[str, Any] | None = None,
        tool_name: str = "",
    ) -> str:
        workflow = workflow or automation_workflow_definition(task_id)
        normalized = normalize_task_group_id(task_id)
        tool_name_value = str(tool_name or workflow.get("tool_name") or "").strip()
        if "price" in tool_name_value.lower() or normalized.startswith("price"):
            return "price"
        if tool_name_value == "self_pickup_problem_upload" or normalized == "self_pickup_problem_upload":
            return "self_pickup_problem"
        return "general"

    def _enrich_automation_tasks_with_accounts(
        self,
        tasks: list[dict[str, Any]],
        accounts: list[dict[str, Any]],
    ) -> None:
        options_by_system = self._automation_account_options_by_system(accounts)
        for task in tasks:
            task_id = str(task.get("task_id") or "")
            workflow = automation_workflow_definition(task_id)
            try:
                payload = json.loads(str(task.get("tool_params_json") or "{}"))
            except json.JSONDecodeError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}

            role_bindings: list[dict[str, Any]] = []
            for role in self._automation_task_account_roles(
                task_id,
                workflow,
                str(task.get("tool_name_value") or ""),
                str(task.get("provider") or ""),
            ):
                system = str(role.get("system") or "").strip().lower()
                options = list(options_by_system.get(system, []))
                option_ids = {str(item.get("account_id") or "") for item in options}
                field = str(role.get("field") or "account_id")
                selected = str(payload.get(field) or "").strip()
                if not selected and field == "account_id":
                    selected = str(payload.get("accountId") or "").strip()
                if selected not in option_ids:
                    default_account_id = str(role.get("default_account_id") or "").strip()
                    selected = (
                        default_account_id
                        if default_account_id in option_ids
                        else str(options[0].get("account_id") or "") if options else ""
                    )
                role_bindings.append(
                    {
                        **role,
                        "options": options,
                        "selected_account_id": selected,
                    }
                )

            first_role = role_bindings[0] if role_bindings else {}
            task["account_role_bindings"] = role_bindings
            task["account_system"] = str(first_role.get("system") or "")
            task["account_system_label"] = str(first_role.get("system_label") or "")
            task["account_options"] = list(first_role.get("options") or [])
            task["selected_account_id"] = str(first_role.get("selected_account_id") or "")

    def _handle_automation_account_post(self, handler: BaseHTTPRequestHandler, path: str) -> bool:
        if path == "/automation-accounts/create":
            values = self._parse_urlencoded_form(handler)
            account_id = str(values.get("account_id", "") or "").strip()
            system = str(values.get("system", "") or "").strip()
            name = str(values.get("name", "") or "").strip()
            return self._proxy_automation_account_action(
                handler,
                "POST",
                "/admin/accounts",
                payload={
                    "account_id": account_id,
                    "system": system,
                    "name": name,
                },
                success_message=f"业务账号已创建：{name or account_id}",
                timeout=12,
                account_id=account_id,
            )

        prefix = "/automation-accounts/"
        if not path.startswith(prefix):
            return False
        parts = [part for part in path[len(prefix) :].strip("/").split("/") if part]
        if len(parts) != 2:
            return False
        account_id = unquote(parts[0])
        action = parts[1]
        quoted_id = quote(account_id, safe="")
        values = self._parse_urlencoded_form(handler)

        if action == "credentials":
            return self._proxy_automation_account_action(
                handler,
                "POST",
                f"/admin/accounts/{quoted_id}/credentials",
                payload={
                    "username": str(values.get("username", "") or "").strip(),
                    "password": str(values.get("password", "") or ""),
                    "phone": str(values.get("phone", "") or "").strip(),
                },
                success_message="账号凭据已保存。",
                timeout=20,
                account_id=account_id,
            )
        if action == "login":
            return self._proxy_automation_account_action(
                handler,
                "POST",
                f"/admin/accounts/{quoted_id}/login",
                payload={},
                success_message="登录或验证请求已提交，请按状态提示继续。",
                timeout=90,
                account_id=account_id,
            )
        if action == "submit-code":
            code = str(values.get("code", "") or "").strip()
            if not code:
                if self._is_ajax_request(handler):
                    self._send_json(
                        handler,
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "message": "验证码不能为空。", "kind": "warning"},
                    )
                    return True
                self._redirect_with_message(handler, "/automation-accounts", "验证码不能为空。", "warning")
                return True
            return self._proxy_automation_account_action(
                handler,
                "POST",
                f"/admin/accounts/{quoted_id}/submit-code",
                payload={"code": code},
                success_message="验证码已提交。",
                timeout=45,
                account_id=account_id,
            )
        if action == "clear-session":
            return self._proxy_automation_account_action(
                handler,
                "POST",
                f"/admin/accounts/{quoted_id}/clear-session",
                payload={},
                success_message="登录态已清除。",
                timeout=20,
                account_id=account_id,
            )
        if action == "clear-credentials":
            return self._proxy_automation_account_action(
                handler,
                "POST",
                f"/admin/accounts/{quoted_id}/credentials/clear",
                payload={},
                success_message="账号凭据已清空。",
                timeout=20,
                account_id=account_id,
            )
        if action == "default":
            return self._proxy_automation_account_action(
                handler,
                "POST",
                f"/admin/accounts/{quoted_id}/default",
                payload={},
                success_message="默认账号已更新。",
                timeout=12,
                account_id=account_id,
            )
        if action == "active":
            target_active = str(values.get("target_active", "") or "").strip() == "1"
            return self._proxy_automation_account_action(
                handler,
                "POST",
                f"/admin/accounts/{quoted_id}/active",
                payload={"is_active": target_active},
                success_message="账号状态已更新。",
                timeout=12,
                account_id=account_id,
            )
        return False

    def _proxy_automation_account_action(
        self,
        handler: BaseHTTPRequestHandler,
        method: str,
        endpoint: str,
        *,
        payload: dict[str, Any],
        success_message: str,
        timeout: int,
        account_id: str = "",
    ) -> bool:
        result = self._agent_request(method, endpoint, payload=payload, timeout=timeout)
        message = success_message
        kind = "success"
        response_payload: dict[str, Any] | None = None
        if not result.get("ok"):
            message = f"Agent 调用失败：{normalize_feedback_text(result.get('error') or 'unknown error')}"
            kind = "warning"
        else:
            raw_payload = result.get("data")
            if not isinstance(raw_payload, dict):
                message = "Agent 账号接口返回了无效数据。"
                kind = "warning"
            else:
                response_payload = raw_payload
            if isinstance(response_payload, dict) and response_payload.get("ok") is False:
                message = normalize_feedback_text(
                    response_payload.get("message")
                    or response_payload.get("error")
                    or "账号操作失败。"
                )
                kind = "warning"
        if self._is_ajax_request(handler):
            response: dict[str, Any] = {"ok": kind == "success", "message": message, "kind": kind}
            if isinstance(response_payload, dict) and response_payload.get("ok") is not False:
                direct_state = self._automation_account_state_from_payload(response_payload)
                if direct_state is not None:
                    response["state"] = direct_state
                elif account_id:
                    status_result = self._fetch_automation_account_status_state(account_id)
                    if status_result.get("ok"):
                        response["state"] = status_result.get("state") or {}
                    elif kind == "success":
                        response["ok"] = False
                        response["kind"] = "warning"
                        response["message"] = status_result.get("message") or "账号状态获取失败。"
                credentials = response_payload.get("credentials")
                if isinstance(credentials, dict):
                    public_credentials = dict(credentials)
                    public_credentials["password"] = ""
                    response["credentials"] = public_credentials
            self._send_json(handler, HTTPStatus.OK, response)
            return True
        self._redirect_with_message(handler, "/automation-accounts", message, kind)
        return True

    def _automation_account_state_from_payload(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        nested = payload.get("state")
        if isinstance(nested, dict):
            state = dict(nested)
            state.pop("ok", None)
            return state
        if isinstance(payload.get("status"), str):
            state = dict(payload)
            state.pop("ok", None)
            return state
        return None

    def _handle_admin_account_create(self, handler: BaseHTTPRequestHandler) -> None:
        values = self._parse_urlencoded_form(handler)
        username = str(values.get("username", "") or "").strip()
        display_name = str(values.get("display_name", "") or "").strip()
        password = str(values.get("password", "") or "")
        if not ADMIN_USERNAME_RE.fullmatch(username):
            self._redirect_with_message(handler, "/settings/accounts", "账号需为 3-64 位字母、数字、点、下划线、@ 或短横线。", "warning")
            return
        if len(password) < 8:
            self._redirect_with_message(handler, "/settings/accounts", "密码至少需要 8 位。", "warning")
            return
        if self.repository.get_admin_user_by_username(username):
            self._redirect_with_message(handler, "/settings/accounts", "账号已存在。", "warning")
            return
        self.repository.create_admin_user(
            username=username,
            display_name=display_name or username,
            password_hash=hash_admin_password(password),
            is_active=True,
        )
        self._redirect_with_message(handler, "/settings/accounts", f"账号已创建：{username}", "success")

    def _handle_admin_account_toggle(self, handler: BaseHTTPRequestHandler, path: str) -> None:
        user_id = self._parse_admin_user_id(path, "toggle")
        if user_id is None:
            self._send_text(handler, HTTPStatus.NOT_FOUND, "Admin account not found.")
            return
        values = self._parse_urlencoded_form(handler)
        target_active = str(values.get("target_active", "") or "") == "1"
        current_user = current_admin_user() or {}
        if int(current_user.get("id") or 0) == user_id and not target_active:
            self._redirect_with_message(handler, "/settings/accounts", "不能停用当前登录账号。", "warning")
            return
        if not self.repository.get_admin_user(user_id):
            self._redirect_with_message(handler, "/settings/accounts", "账号不存在。", "warning")
            return
        self.repository.set_admin_user_active(user_id, target_active)
        message = "账号已启用。" if target_active else "账号已停用。"
        self._redirect_with_message(handler, "/settings/accounts", message, "success")

    def _handle_admin_account_reset_password(self, handler: BaseHTTPRequestHandler, path: str) -> None:
        user_id = self._parse_admin_user_id(path, "reset-password")
        if user_id is None:
            self._send_text(handler, HTTPStatus.NOT_FOUND, "Admin account not found.")
            return
        values = self._parse_urlencoded_form(handler)
        password = str(values.get("password", "") or "")
        if len(password) < 8:
            self._redirect_with_message(handler, "/settings/accounts", "新密码至少需要 8 位。", "warning")
            return
        if not self.repository.get_admin_user(user_id):
            self._redirect_with_message(handler, "/settings/accounts", "账号不存在。", "warning")
            return
        self.repository.update_admin_user_password(user_id, hash_admin_password(password))
        self._redirect_with_message(handler, "/settings/accounts", "密码已重置，原有会话已失效。", "success")

    def _handle_admin_avatar_upload(self, handler: BaseHTTPRequestHandler) -> None:
        user = current_admin_user() or {}
        user_id = int(user.get("id") or 0)
        return_to = self._request_return_to(handler, "/")
        if not user_id or bool(user.get("is_legacy_basic_auth")):
            self._send_avatar_upload_error(handler, HTTPStatus.FORBIDDEN, "当前登录方式不支持上传头像。", return_to)
            return

        try:
            content_length = int(handler.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length > AVATAR_MAX_BYTES + 512 * 1024:
            self._send_avatar_upload_error(handler, HTTPStatus.BAD_REQUEST, "头像图片不能超过 2MB。", return_to)
            return

        form = self._parse_multipart_form(handler)
        item = form["avatar"] if "avatar" in form else None
        if isinstance(item, list):
            item = item[0] if item else None
        if item is None or not getattr(item, "filename", ""):
            self._send_avatar_upload_error(handler, HTTPStatus.BAD_REQUEST, "请选择要上传的头像图片。", return_to)
            return

        suffix = Path(str(item.filename or "")).suffix.lower()
        if suffix not in AVATAR_ALLOWED_EXTENSIONS:
            self._send_avatar_upload_error(handler, HTTPStatus.BAD_REQUEST, "头像仅支持 JPG、PNG、WebP 或 GIF 图片。", return_to)
            return

        payload = item.file.read(AVATAR_MAX_BYTES + 1)
        if not payload:
            self._send_avatar_upload_error(handler, HTTPStatus.BAD_REQUEST, "头像图片为空。", return_to)
            return
        if len(payload) > AVATAR_MAX_BYTES:
            self._send_avatar_upload_error(handler, HTTPStatus.BAD_REQUEST, "头像图片不能超过 2MB。", return_to)
            return

        detected_suffix = self._detect_avatar_extension(payload)
        if detected_suffix is None:
            self._send_avatar_upload_error(handler, HTTPStatus.BAD_REQUEST, "头像文件格式无法识别。", return_to)
            return

        avatar_dir = self.settings.runtime_dir / "avatars"
        avatar_dir.mkdir(parents=True, exist_ok=True)
        filename = f"admin_{user_id}_{secrets.token_hex(12)}{detected_suffix}"
        target = (avatar_dir / filename).resolve()
        try:
            target.relative_to(avatar_dir.resolve())
        except ValueError:
            self._send_avatar_upload_error(handler, HTTPStatus.BAD_REQUEST, "头像保存路径无效。", return_to)
            return

        target.write_bytes(payload)
        relpath = str(target.relative_to(self.settings.runtime_dir)).replace("\\", "/")
        previous_avatar_path = str(user.get("avatar_path") or "")
        self.repository.update_admin_user_avatar(user_id, relpath)
        self._delete_admin_avatar_file(previous_avatar_path, keep_relpath=relpath)

        avatar_url = self._admin_avatar_url(relpath)
        updated_user = dict(user)
        updated_user["avatar_path"] = relpath
        updated_user["avatar_url"] = avatar_url
        self._set_current_admin_user(handler, updated_user)

        if self._is_ajax_request(handler):
            self._send_json(handler, HTTPStatus.OK, {"ok": True, "avatar_url": avatar_url, "message": "头像已更新。"})
            return
        self._redirect_with_message(handler, return_to, "头像已更新。", "success")

    def _send_avatar_upload_error(
        self,
        handler: BaseHTTPRequestHandler,
        status: HTTPStatus,
        message: str,
        return_to: str,
    ) -> None:
        if self._is_ajax_request(handler):
            self._send_json(handler, status, {"ok": False, "message": message})
            return
        self._redirect_with_message(handler, return_to, message, "warning")

    def _detect_avatar_extension(self, payload: bytes) -> str | None:
        if payload.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if payload.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if payload.startswith((b"GIF87a", b"GIF89a")):
            return ".gif"
        if len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
            return ".webp"
        return None

    def _admin_avatar_url(self, avatar_path: str) -> str:
        normalized = str(avatar_path or "").strip().replace("\\", "/")
        if not normalized or not normalized.startswith("avatars/"):
            return ""
        return self._runtime_url(normalized)

    def _delete_admin_avatar_file(self, relpath: str, *, keep_relpath: str = "") -> None:
        normalized = str(relpath or "").strip().replace("\\", "/")
        if not normalized or normalized == keep_relpath or not normalized.startswith("avatars/"):
            return
        avatar_root = (self.settings.runtime_dir / "avatars").resolve()
        target = (self.settings.runtime_dir / Path(normalized)).resolve()
        try:
            target.relative_to(avatar_root)
        except ValueError:
            return
        if target.exists() and target.is_file():
            target.unlink()

    def _request_return_to(self, handler: BaseHTTPRequestHandler, fallback: str = "/") -> str:
        referer = str(handler.headers.get("Referer") or "").strip()
        if referer.startswith("/"):
            return self._safe_return_to(referer, fallback)
        parsed = urlparse(referer)
        host = str(handler.headers.get("Host") or "").strip()
        if parsed.netloc and parsed.netloc == host:
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"
            return self._safe_return_to(path, fallback)
        return fallback

    def _parse_admin_user_id(self, path: str, suffix: str) -> int | None:
        prefix = "/settings/accounts/"
        suffix_value = f"/{suffix}"
        if not path.startswith(prefix) or not path.endswith(suffix_value):
            return None
        raw = path[len(prefix) : -len(suffix_value)].strip("/")
        try:
            return int(raw)
        except ValueError:
            return None

    def _render_portal(self, handler: BaseHTTPRequestHandler, query: dict) -> None:
        template = self.template_env.get_template("portal.html")
        body = template.render(
            app_title=self.settings.app_title,
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
            settings=self.settings,
        )
        self._send_html(handler, body)

    def _monitoring_snapshot_from_agent(self, query: dict[str, list[str]] | None = None) -> dict[str, Any]:
        query = query or {}
        systems = str(query.get("systems", ["yunda,ronghui"])[0] or "yunda,ronghui").strip() or "yunda,ronghui"
        force_value = str(query.get("force", ["0"])[0] or "0").strip().lower()
        force = "1" if force_value in {"1", "true", "yes", "on"} else "0"
        prefer_cached_value = str(query.get("prefer_cached", ["0"])[0] or "0").strip().lower()
        prefer_cached = "1" if prefer_cached_value in {"1", "true", "yes", "on"} else "0"
        params = {"systems": systems, "force": force}
        if prefer_cached == "1":
            params["prefer_cached"] = prefer_cached
        endpoint = "/admin/monitoring/snapshot?" + urlencode(params)
        result = self._agent_request("GET", endpoint, timeout=75)
        if result.get("ok") and isinstance(result.get("data"), dict):
            payload = result["data"]
            if "ok" not in payload:
                payload = {"ok": True, **payload}
            return payload
        error = result.get("error")
        return {
            "ok": False,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "poll_interval_sec": 60,
            "message": error if isinstance(error, str) else json.dumps(error, ensure_ascii=False),
            "totals": {
                "total_pending": 0,
                "yunda_pending": 0,
                "ronghui_pending": 0,
                "exception_pending": 0,
            },
            "systems": [],
        }

    def _handle_monitoring_summary(self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> None:
        self._send_json(handler, HTTPStatus.OK, self._monitoring_snapshot_from_agent(query))

    def _monitoring_daily_sign_from_agent(self, query: dict[str, list[str]] | None = None) -> dict[str, Any]:
        query = query or {}
        force_value = str(query.get("force", ["0"])[0] or "0").strip().lower()
        force = "1" if force_value in {"1", "true", "yes", "on"} else "0"
        params = {"force": force}
        target_date = str(query.get("target_date", [""])[0] or "").strip()
        if target_date:
            params["target_date"] = target_date
        prefer_cached_value = str(query.get("prefer_cached", ["0"])[0] or "0").strip().lower()
        if prefer_cached_value in {"1", "true", "yes", "on"}:
            params["prefer_cached"] = "1"
        endpoint = "/admin/monitoring/daily-sign?" + urlencode(params)
        result = self._agent_request("GET", endpoint, timeout=75)
        if result.get("ok") and isinstance(result.get("data"), dict):
            payload = result["data"]
            if "ok" not in payload:
                payload = {"ok": True, **payload}
            return payload
        error = result.get("error")
        return {
            "ok": False,
            "status": "error",
            "target_date": target_date,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "poll_interval_sec": 60,
            "counts": {"unsigned_today": 0},
            "message": error if isinstance(error, str) else json.dumps(error, ensure_ascii=False),
        }

    def _handle_monitoring_daily_sign(self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> None:
        self._send_json(handler, HTTPStatus.OK, self._monitoring_daily_sign_from_agent(query))

    def _handle_monitoring_stream(self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> None:
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "keep-alive")
        handler.end_headers()

        once = str(query.get("once", ["0"])[0] or "0").strip().lower() in {"1", "true", "yes"}
        while True:
            payload = self._monitoring_snapshot_from_agent(query)
            payload_text = json.dumps(payload, ensure_ascii=False)
            event = f"event: snapshot\ndata: {payload_text}\n\n".encode("utf-8")
            try:
                handler.wfile.write(event)
                flush = getattr(handler.wfile, "flush", None)
                if callable(flush):
                    flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                break
            if once:
                break
            interval = max(int(payload.get("poll_interval_sec") or 60), 30)
            time.sleep(interval)

    def _handle_monitoring_detail_link(self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> None:
        def first(name: str) -> str:
            return str(query.get(name, [""])[0] or "").strip()

        payload = {
            "system": first("system"),
            "category_id": first("category_id"),
            "title": first("title"),
            "resource_id": first("resource_id"),
            "type_code": first("type_code"),
            "target_title": first("target_title"),
        }
        result = self._agent_request("POST", "/admin/monitoring/detail-link", payload=payload, timeout=20)
        if result.get("ok") and isinstance(result.get("data"), dict):
            self._send_json(handler, HTTPStatus.OK, result["data"])
            return
        error = result.get("error")
        self._send_json(
            handler,
            HTTPStatus.BAD_GATEWAY,
            {
                "ok": False,
                "error_code": "AGENT_UNAVAILABLE",
                "message": error if isinstance(error, str) else json.dumps(error, ensure_ascii=False),
            },
        )

    def _render_ocr_workspace(self, handler: BaseHTTPRequestHandler, query: dict) -> None:
        self._render_document(handler, None, query)

    def _get_finance_service(self) -> FinanceService:
        service = getattr(self, "finance_service", None)
        if service is None:
            service = FinanceService(self.repository, agent_request=self._agent_request)
            self.finance_service = service
        return service

    def _render_finance(self, handler: BaseHTTPRequestHandler, query: dict) -> None:
        today = datetime.now().date()
        template = self.template_env.get_template("finance.html")
        body = template.render(
            app_title=self.settings.app_title,
            today=today.isoformat(),
            month_start=today.replace(day=1).isoformat(),
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
        )
        self._send_html(handler, body)

    def _handle_finance_get(
        self,
        handler: BaseHTTPRequestHandler,
        resource: str,
        query: dict[str, list[str]],
    ) -> None:
        service = self._get_finance_service()
        operations = {
            "summary": service.get_summary,
            "trend": service.get_trend,
            "entries": service.list_entries,
            "fee_mappings": service.list_fee_mappings,
            "sync_batches": service.list_sync_batches,
        }
        operation = operations.get(resource)
        if operation is None:
            self._send_json(
                handler,
                HTTPStatus.NOT_FOUND,
                {"ok": False, "error_code": "FINANCE_ROUTE_NOT_FOUND", "message": "财务接口不存在。"},
            )
            return
        try:
            data = operation(query)
        except FinanceError as exc:
            self._send_finance_error(handler, exc)
            return
        except Exception as exc:
            LOGGER.exception("Finance GET failed: %s", type(exc).__name__)
            self._send_json(
                handler,
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "ok": False,
                    "error_code": "FINANCE_INTERNAL_ERROR",
                    "message": "财务数据查询失败，请查看服务日志后重试。",
                },
            )
            return
        self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": data})

    def _handle_finance_post(
        self,
        handler: BaseHTTPRequestHandler,
        action: str,
        *,
        path: str = "",
    ) -> None:
        service = self._get_finance_service()
        body = self._parse_json_body(handler)
        try:
            if action == "sync":
                data = service.start_sync(body)
            elif action == "backfill":
                data = service.start_backfill(body)
            elif action == "save_mapping":
                match = re.fullmatch(r"/finance/fee-mappings/(\d+)", path)
                if not match:
                    raise FinanceValidationError("费用项目 ID 无效。")
                admin = current_admin_user() or {}
                changed_by = str(admin.get("username") or admin.get("display_name") or "").strip()
                if not changed_by:
                    raise FinanceValidationError("无法识别当前操作人，请重新登录后再保存。")
                data = service.save_fee_mapping(int(match.group(1)), body, changed_by=changed_by)
            elif action == "retry_batch":
                match = re.fullmatch(r"/finance/sync-batches/(\d+)/retry", path)
                if not match:
                    raise FinanceValidationError("同步批次 ID 无效。")
                data = service.retry_batch(int(match.group(1)))
            else:
                self._send_json(
                    handler,
                    HTTPStatus.NOT_FOUND,
                    {"ok": False, "error_code": "FINANCE_ROUTE_NOT_FOUND", "message": "财务接口不存在。"},
                )
                return
        except FinanceError as exc:
            self._send_finance_error(handler, exc)
            return
        except Exception as exc:
            LOGGER.exception("Finance POST failed: %s", type(exc).__name__)
            self._send_json(
                handler,
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "ok": False,
                    "error_code": "FINANCE_INTERNAL_ERROR",
                    "message": "财务操作未完成，请查看服务日志后重试。",
                },
            )
            return
        self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": data})

    def _send_finance_error(self, handler: BaseHTTPRequestHandler, error: FinanceError) -> None:
        try:
            status = HTTPStatus(int(getattr(error, "http_status", HTTPStatus.INTERNAL_SERVER_ERROR)))
        except ValueError:
            status = HTTPStatus.INTERNAL_SERVER_ERROR
        self._send_json(
            handler,
            status,
            {
                "ok": False,
                "error_code": str(getattr(error, "error_code", "FINANCE_ERROR")),
                "message": str(error),
            },
        )

    def _render_module(self, handler: BaseHTTPRequestHandler, slug: str, query: dict) -> None:
        counts = self.repository.count_by_status()
        modules = self._build_module_view_models(counts)
        module = modules.get(slug)
        if not module:
            self._send_text(handler, HTTPStatus.NOT_FOUND, "Module not found.")
            return
        module = dict(module)
        module["dependencies"] = [modules[item]["name"] for item in module["dependencies"]]
        module["consumers"] = [modules[item]["name"] for item in module["consumers"]]
        related_modules = [
            modules[item]
            for item in modules
            if item != slug and (
                item in self.project_modules[slug].dependencies or item in self.project_modules[slug].consumers
            )
        ]
        template = self.template_env.get_template("module.html")
        body = template.render(
            app_title=self.settings.app_title,
            module=module,
            related_modules=related_modules,
            counts=counts,
            recent_documents=self.repository.list_documents(limit=5) if slug == "ocr" else [],
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
        )
        self._send_html(handler, body)

    @staticmethod
    def _customer_service_login_account(account: dict[str, Any]) -> str:
        credentials = account.get("credentials") if isinstance(account.get("credentials"), dict) else {}
        status = account.get("status") if isinstance(account.get("status"), dict) else {}
        for source in (account, credentials, status):
            for key in (
                "login_account",
                "login_username",
                "username",
                "user_name",
                "account_no",
                "account",
                "user_id",
            ):
                value = str(source.get(key) or "").strip()
                if value:
                    return value
        return ""

    def _customer_service_public_accounts(self, accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        public_accounts: list[dict[str, Any]] = []
        for item in accounts or []:
            if not isinstance(item, dict):
                continue
            system = str(item.get("system") or "").strip().lower()
            if system == "price":
                system = "ronghui"
            if system not in CUSTOMER_SERVICE_ALLOWED_ACCOUNT_SYSTEMS:
                continue
            account_id = str(item.get("account_id") or item.get("id") or "").strip()
            if not account_id:
                continue
            status = item.get("status") if isinstance(item.get("status"), dict) else {}
            public_accounts.append(
                {
                    "account_id": account_id,
                    "name": str(item.get("name") or account_id).strip(),
                    "login_account": self._customer_service_login_account(item),
                    "system": system,
                    "system_label": str(
                        item.get("system_label")
                        or AUTOMATION_ACCOUNT_SYSTEM_LABELS.get(system, system)
                    ),
                    "account_purpose": str(item.get("account_purpose") or "").strip(),
                    "status_label": str(item.get("status_label") or status.get("label") or "").strip(),
                    "status_tone": str(item.get("status_tone") or status.get("status_tone") or "").strip(),
                    "status_note": str(item.get("status_note") or status.get("status_note") or "").strip(),
                    "session_capable": bool(item.get("session_capable")),
                    "has_saved_credentials": bool(item.get("has_saved_credentials") or status.get("has_saved_credentials")),
                    "credentials_label": str(item.get("credentials_label") or "").strip(),
                    "credentials_tone": str(item.get("credentials_tone") or "").strip(),
                }
            )
        return public_accounts

    def _customer_service_account_maps(self, *, force: bool = False) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], str]:
        accounts, warning = self._fetch_automation_accounts(force=force, prefer_cached=not force)
        public_accounts = self._customer_service_public_accounts(accounts)
        return public_accounts, {item["account_id"]: item for item in public_accounts}, warning

    @staticmethod
    def _customer_service_list(value: Any) -> list[str]:
        values = value if isinstance(value, list) else []
        normalized: list[str] = []
        for item in values:
            text = str(item or "").strip()
            if text and text not in normalized:
                normalized.append(text)
        return normalized

    @staticmethod
    def _customer_service_poll_interval(value: Any) -> int:
        try:
            parsed = int(float(value))
        except (TypeError, ValueError):
            parsed = int(CUSTOMER_SERVICE_DEFAULT_SETTINGS["poll_interval_sec"])
        return min(max(parsed, 15), 600)

    def _sanitize_customer_service_settings(
        self,
        raw_settings: dict[str, Any] | None,
        account_map: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        raw = raw_settings if isinstance(raw_settings, dict) else {}
        allowed_by_system = {
            "ronghui": [item_id for item_id, item in account_map.items() if item.get("system") == "ronghui"],
            "yunda": [item_id for item_id, item in account_map.items() if item.get("system") == "yunda"],
        }

        def keep_ids(key: str, system: str) -> list[str]:
            selected = self._customer_service_list(raw.get(key))
            allowed = set(allowed_by_system.get(system) or [])
            return [item for item in selected if item in allowed]

        return {
            "ronghui_account_ids": keep_ids("ronghui_account_ids", "ronghui"),
            "yunda_account_ids": keep_ids("yunda_account_ids", "yunda"),
            "poll_interval_sec": self._customer_service_poll_interval(raw.get("poll_interval_sec")),
        }

    def _customer_service_settings(self, account_map: dict[str, dict[str, Any]]) -> dict[str, Any]:
        try:
            row = self.repository.get_workflow_resource(CUSTOMER_SERVICE_RESOURCE_KEY)
        except Exception:
            row = None
        config = row.get("config") if isinstance(row, dict) and isinstance(row.get("config"), dict) else {}
        merged = {**CUSTOMER_SERVICE_DEFAULT_SETTINGS, **config}
        return self._sanitize_customer_service_settings(merged, account_map)

    @staticmethod
    def _customer_service_clean_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (list, tuple, set)):
            return "、".join(
                item for item in (LocalDocFlowApp._customer_service_clean_text(part) for part in value) if item
            )
        if isinstance(value, dict):
            return ""
        return str(value).strip()

    @classmethod
    def _customer_service_problem_field(cls, row: dict[str, Any], keys: tuple[str, ...]) -> str:
        sources: list[dict[str, Any]] = [row]
        raw = row.get("raw")
        if isinstance(raw, dict):
            sources.append(raw)
        for source in sources:
            for key in keys:
                value = cls._customer_service_clean_text(source.get(key))
                if value:
                    return value
        return ""

    @classmethod
    def _customer_service_should_include_problem_row(cls, row: dict[str, Any]) -> bool:
        account_login = cls._customer_service_clean_text(row.get("account_login"))
        if account_login != CUSTOMER_SERVICE_SITE_FILTER_LOGIN:
            return True
        publish_site = cls._customer_service_problem_field(row, CUSTOMER_SERVICE_PUBLISH_SITE_KEYS)
        notified_site = cls._customer_service_problem_field(row, CUSTOMER_SERVICE_NOTIFIED_SITE_KEYS)
        return publish_site == CUSTOMER_SERVICE_SITE_FILTER_SITE and notified_site == CUSTOMER_SERVICE_SITE_FILTER_SITE

    def _render_customer_service(self, handler: BaseHTTPRequestHandler, query: dict) -> None:
        accounts, account_map, warning = self._customer_service_account_maps(force=False)
        settings = self._customer_service_settings(account_map)
        template = self.template_env.get_template("customer_service.html")
        body = template.render(
            app_title=self.settings.app_title,
            accounts=accounts,
            settings=settings,
            account_warning=warning,
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
        )
        self._send_html(handler, body)

    def _handle_customer_service_problem_settings_get(self, handler: BaseHTTPRequestHandler) -> None:
        accounts, account_map, warning = self._customer_service_account_maps(force=False)
        settings = self._customer_service_settings(account_map)
        self._send_json(
            handler,
            HTTPStatus.OK,
            {
                "ok": not bool(warning),
                "message": warning,
                "settings": settings,
                "accounts": accounts,
            },
        )

    def _handle_customer_service_problem_settings_post(self, handler: BaseHTTPRequestHandler) -> None:
        body = self._parse_json_body(handler)
        accounts, account_map, warning = self._customer_service_account_maps(force=False)
        if warning:
            self._send_json(handler, HTTPStatus.BAD_GATEWAY, {"ok": False, "message": warning})
            return
        settings = self._sanitize_customer_service_settings(body, account_map)
        try:
            self.repository.upsert_workflow_resource(
                CUSTOMER_SERVICE_RESOURCE_KEY,
                settings,
                source="customer_service",
            )
        except Exception as exc:
            self._send_json(
                handler,
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "message": f"客服问题件设置保存失败：{exc}"},
            )
            return
        self._send_json(handler, HTTPStatus.OK, {"ok": True, "settings": settings, "accounts": accounts})

    def _customer_service_selected_accounts(
        self,
        body: dict[str, Any],
        account_map: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        platforms = {
            str(item or "").strip().lower()
            for item in (body.get("platforms") if isinstance(body.get("platforms"), list) else [])
            if str(item or "").strip()
        }
        platforms = platforms or set(CUSTOMER_SERVICE_ALLOWED_ACCOUNT_SYSTEMS)
        raw_account_ids = self._customer_service_list(body.get("account_ids"))
        if not raw_account_ids:
            settings = self._customer_service_settings(account_map)
            raw_account_ids = [
                *settings.get("ronghui_account_ids", []),
                *settings.get("yunda_account_ids", []),
            ]
        selected: list[dict[str, Any]] = []
        for account_id in raw_account_ids:
            account = account_map.get(account_id)
            if not account:
                continue
            if account.get("system") not in platforms:
                continue
            selected.append(account)
        return selected

    def _customer_service_agent_payload(self, account: dict[str, Any], action: str, body: dict[str, Any]) -> dict[str, Any]:
        return {
            "platform": account["system"],
            "account_id": account["account_id"],
            "account_label": account.get("name") or account["account_id"],
            "account_login": account.get("login_account") or "",
            "action": action,
            "filters": body.get("filters") if isinstance(body.get("filters"), dict) else {},
            "item": body.get("item") if isinstance(body.get("item"), dict) else {},
            "payload": body.get("payload") if isinstance(body.get("payload"), dict) else {},
        }

    def _call_customer_service_problem_agent(self, payload: dict[str, Any], *, timeout_sec: int = 120) -> dict[str, Any]:
        return self._agent_request(
            "POST",
            "/tms/customer_service_problem",
            payload={"params": payload, "timeout_sec": timeout_sec},
            timeout=max(timeout_sec + 15, self.settings.agent_timeout_seconds),
        )

    @staticmethod
    def _unwrap_customer_service_agent_result(result: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        if not result.get("ok"):
            error = result.get("error")
            if isinstance(error, dict):
                return None, str(error.get("error") or error.get("message") or error)
            return None, str(error or "Agent 调用失败。")
        data = result.get("data")
        if not isinstance(data, dict):
            return None, "Agent 返回了无效数据。"
        if isinstance(data.get("data"), dict):
            nested = data["data"]
            if (
                "rows" in nested
                or "result" in nested
                or "details" in nested
                or "body_base64" in nested
                or "content_type" in nested
                or nested.get("ok") is False
            ):
                data = nested
        if data.get("ok") is False:
            return data, str(data.get("message") or data.get("error") or "问题件接口调用失败。")
        return data, ""

    def _handle_customer_service_problem_query(self, handler: BaseHTTPRequestHandler) -> None:
        body = self._parse_json_body(handler)
        _accounts, account_map, warning = self._customer_service_account_maps(force=False)
        if warning:
            self._send_json(handler, HTTPStatus.BAD_GATEWAY, {"ok": False, "message": warning, "rows": []})
            return
        selected_accounts = self._customer_service_selected_accounts(body, account_map)
        if not selected_accounts:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": "请先选择融辉或韵达账号。", "rows": []})
            return

        rows: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        def fetch_one(account: dict[str, Any]) -> dict[str, Any]:
            payload = self._customer_service_agent_payload(account, "query", body)
            result = self._call_customer_service_problem_agent(payload, timeout_sec=180)
            data, error = self._unwrap_customer_service_agent_result(result)
            return {"account": account, "payload": payload, "data": data, "error": error}

        with ThreadPoolExecutor(max_workers=min(max(len(selected_accounts), 1), 6)) as executor:
            futures = [executor.submit(fetch_one, account) for account in selected_accounts]
            for future in as_completed(futures):
                item = future.result()
                account = item["account"]
                data = item["data"]
                if item["error"]:
                    errors.append(
                        {
                            "platform": account["system"],
                            "account_id": account["account_id"],
                            "account_label": account.get("name") or account["account_id"],
                            "account_login": account.get("login_account") or "",
                            "message": item["error"],
                            "error_code": (data or {}).get("error_code") if isinstance(data, dict) else "",
                        }
                    )
                    continue
                for row in (data or {}).get("rows") or []:
                    if not isinstance(row, dict):
                        continue
                    if not str(row.get("external_id") or "").strip():
                        errors.append(
                            {
                                "platform": account["system"],
                                "account_id": account["account_id"],
                                "account_label": account.get("name") or account["account_id"],
                                "account_login": account.get("login_account") or "",
                                "message": "原系统返回问题件缺少 external_id，已跳过该账号结果。",
                                "error_code": "MISSING_EXTERNAL_ID",
                            }
                        )
                        rows = [existing for existing in rows if existing.get("account_id") != account["account_id"]]
                        break
                    normalized = dict(row)
                    normalized.setdefault("platform", account["system"])
                    normalized.setdefault("account_id", account["account_id"])
                    normalized.setdefault("account_label", account.get("name") or account["account_id"])
                    normalized.setdefault("account_login", account.get("login_account") or "")
                    if not self._customer_service_should_include_problem_row(normalized):
                        continue
                    rows.append(normalized)

        rows.sort(key=lambda row: str(row.get("updated_at") or row.get("created_at") or ""), reverse=True)
        self._send_json(
            handler,
            HTTPStatus.OK,
            {
                "ok": not errors,
                "rows": rows,
                "errors": errors,
                "stats": {
                    "account_count": len(selected_accounts),
                    "row_count": len(rows),
                    "error_count": len(errors),
                },
            },
        )

    def _resolve_customer_service_action_account(
        self,
        body: dict[str, Any],
        account_map: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, Any] | None, str]:
        account_id = str(body.get("account_id") or (body.get("item") or {}).get("account_id") or "").strip()
        platform = str(body.get("platform") or (body.get("item") or {}).get("platform") or "").strip().lower()
        account = account_map.get(account_id)
        if not account:
            return None, "问题件处理必须带回原账号 account_id。"
        if platform and account.get("system") != platform:
            return None, "问题件平台与账号不一致，已停止提交。"
        return account, ""

    def _handle_customer_service_problem_agent_action(self, handler: BaseHTTPRequestHandler, action: str) -> None:
        body = self._parse_json_body(handler)
        _accounts, account_map, warning = self._customer_service_account_maps(force=False)
        if warning:
            self._send_json(handler, HTTPStatus.BAD_GATEWAY, {"ok": False, "message": warning})
            return
        account, error = self._resolve_customer_service_action_account(body, account_map)
        if error or not account:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": error})
            return
        payload = self._customer_service_agent_payload(account, action, body)
        result = self._call_customer_service_problem_agent(payload, timeout_sec=180)
        data, error = self._unwrap_customer_service_agent_result(result)
        if error:
            self._send_json(handler, HTTPStatus.BAD_GATEWAY, {"ok": False, "message": error, "agent": data or {}})
            return
        self._send_json(handler, HTTPStatus.OK, data or {"ok": True})

    def _handle_customer_service_attachment_preview(
        self,
        handler: BaseHTTPRequestHandler,
        query: dict[str, list[str]],
    ) -> None:
        source_url = str((query.get("src") or [""])[0] or "").strip()
        body = {
            "platform": str((query.get("platform") or [""])[0] or "").strip(),
            "account_id": str((query.get("account_id") or [""])[0] or "").strip(),
            "item": {},
            "payload": {"source_url": source_url},
        }
        if not source_url:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": "附件图片地址为空。"})
            return
        _accounts, account_map, warning = self._customer_service_account_maps(force=False)
        if warning:
            self._send_json(handler, HTTPStatus.BAD_GATEWAY, {"ok": False, "message": warning})
            return
        account, error = self._resolve_customer_service_action_account(body, account_map)
        if error or not account:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": error})
            return
        payload = self._customer_service_agent_payload(account, "fetch_attachment", body)
        result = self._call_customer_service_problem_agent(payload, timeout_sec=90)
        data, error = self._unwrap_customer_service_agent_result(result)
        if error:
            self._send_json(handler, HTTPStatus.BAD_GATEWAY, {"ok": False, "message": error, "agent": data or {}})
            return
        encoded = str((data or {}).get("body_base64") or "")
        try:
            image_bytes = base64.b64decode(encoded, validate=True)
        except Exception:
            self._send_json(handler, HTTPStatus.BAD_GATEWAY, {"ok": False, "message": "Agent 返回的附件图片内容无效。"})
            return
        content_type = str((data or {}).get("content_type") or "image/jpeg").split(";", 1)[0].strip() or "image/jpeg"
        filename = sanitize_filename(str((data or {}).get("filename") or "problem-attachment").strip()) or "problem-attachment"
        self._send_bytes(
            handler,
            HTTPStatus.OK,
            image_bytes,
            content_type,
            cache_control="no-store",
            extra_headers={"Content-Disposition": f'inline; filename="{filename}"'},
        )

    def _handle_customer_service_attachment_upload(self, handler: BaseHTTPRequestHandler) -> None:
        form = self._parse_multipart_form(handler)
        file_item = form["file"] if "file" in form else None
        if file_item is None or not getattr(file_item, "filename", ""):
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": "请选择附件文件。"})
            return
        body = {
            "platform": str(form.getvalue("platform") or "").strip(),
            "account_id": str(form.getvalue("account_id") or "").strip(),
            "item": {},
            "payload": {},
        }
        if "item" in form:
            try:
                item_value = json.loads(str(form.getvalue("item") or "{}"))
                if isinstance(item_value, dict):
                    body["item"] = item_value
            except Exception:
                body["item"] = {}

        _accounts, account_map, warning = self._customer_service_account_maps(force=False)
        if warning:
            self._send_json(handler, HTTPStatus.BAD_GATEWAY, {"ok": False, "message": warning})
            return
        account, error = self._resolve_customer_service_action_account(body, account_map)
        if error or not account:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": error})
            return

        upload_root = (getattr(self.settings, "temp_dir", MODULE_DIR / "runtime" / "artifacts" / "temp") / "customer_service").resolve()
        upload_root.mkdir(parents=True, exist_ok=True)
        suffix = Path(str(file_item.filename or "")).suffix.lower()
        safe_name = sanitize_filename(Path(str(file_item.filename or "attachment")).name) or f"attachment{suffix}"
        target = (upload_root / f"{secrets.token_hex(12)}_{safe_name}").resolve()
        try:
            target.relative_to(upload_root)
        except ValueError:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": "附件文件名无效。"})
            return
        payload_bytes = file_item.file.read()
        if not payload_bytes:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": "附件文件为空。"})
            return
        target.write_bytes(payload_bytes)
        body["payload"] = {
            "file_path": str(target),
            "file_name": safe_name,
            "delete_after_upload": True,
            "scene": str(form.getvalue("scene") or "").strip(),
        }
        payload = self._customer_service_agent_payload(account, "upload_attachment", body)
        result = self._call_customer_service_problem_agent(payload, timeout_sec=180)
        data, error = self._unwrap_customer_service_agent_result(result)
        if target.exists():
            target.unlink(missing_ok=True)
        if error:
            self._send_json(handler, HTTPStatus.BAD_GATEWAY, {"ok": False, "message": error, "agent": data or {}})
            return
        self._send_json(handler, HTTPStatus.OK, data or {"ok": True})

    def _render_dispatch(self, handler: BaseHTTPRequestHandler, query: dict) -> None:
        dispatch_config = {
            "amap_js_key":        self.settings.amap_api_key or "YOUR_AMAP_JS_API_KEY",
            "amap_security_code": self.settings.amap_security_code or "",
        }
        dispatch_sdk_should_load = not dispatch_config["amap_js_key"].startswith("YOUR_")
        template = self.template_env.get_template("dispatch.html")
        body = template.render(
            app_title=self.settings.app_title,
            dispatch_config=dispatch_config,
            dispatch_sdk_should_load=dispatch_sdk_should_load,
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
        )
        self._send_html(handler, body)

    def _render_tracking(self, handler: BaseHTTPRequestHandler, query: dict) -> None:
        template = self.template_env.get_template("tracking.html")
        body = template.render(
            app_title=self.settings.app_title,
            initial_tracking_number=query.get("tracking_number", [""])[0].strip(),
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
        )
        self._send_html(handler, body)

    @staticmethod
    def _waybill_sync_date_span(filters: dict[str, Any]) -> tuple[str, str, str]:
        date_from = str(filters.get("date_from") or "").strip()
        date_to = str(filters.get("date_to") or "").strip()
        if not date_from and not date_to:
            return "", "", ""

        start_text = (date_from or date_to).replace("/", "-")
        end_text = (date_to or date_from).replace("/", "-")
        try:
            start_date = datetime.strptime(start_text, "%Y-%m-%d").date()
            end_date = datetime.strptime(end_text, "%Y-%m-%d").date()
        except ValueError:
            return "", "", "开单日期格式无效，已跳过外部刷新。"
        if start_date > end_date:
            return "", "", "开单开始日期不能晚于结束日期，已跳过外部刷新。"
        if (end_date - start_date).days + 1 > 31:
            return "", "", "开单日期范围超过 31 天，已跳过外部刷新，请缩小范围后重试。"
        return start_date.isoformat(), end_date.isoformat(), ""

    @staticmethod
    def _waybill_sync_providers(source: str) -> list[str]:
        if source == "all":
            return ["ronghui", "yunda"]
        if source in {"ronghui", "yunda"}:
            return [source]
        return []

    @staticmethod
    def _waybill_default_date() -> str:
        return datetime.now().strftime("%Y/%m/%d")

    @staticmethod
    def _waybill_sync_message_from_agent(provider_label: str, tool_result: dict[str, Any]) -> str:
        fetched = tool_result.get("fetched")
        sql_upserted = tool_result.get("sql_upserted")
        sql_deleted = tool_result.get("sql_deleted_stale")
        parts = []
        if fetched not in (None, ""):
            parts.append(f"拉取 {fetched} 条")
        if sql_upserted not in (None, ""):
            parts.append(f"入库 {sql_upserted} 条")
        if sql_deleted not in (None, "", 0):
            parts.append(f"清理旧数据 {sql_deleted} 条")
        return f"已刷新{provider_label}" + (f"（{'，'.join(parts)}）" if parts else "")

    @staticmethod
    def _waybill_agent_error_text(result: dict[str, Any]) -> str:
        error = result.get("error")
        if isinstance(error, dict):
            return str(error.get("error") or error.get("message") or error.get("detail") or error)
        if error:
            return str(error)
        return "Agent 调用失败。"

    def _refresh_waybill_sources_for_filters(self, filters: dict[str, Any]) -> dict[str, list[str]]:
        sync_status: dict[str, list[str]] = {"messages": [], "warnings": []}
        start_date, end_date, date_error = self._waybill_sync_date_span(filters)
        if date_error:
            sync_status["warnings"].append(date_error)
            return sync_status
        if not start_date or not end_date:
            return sync_status

        for provider in self._waybill_sync_providers(str(filters.get("source") or "all").lower()):
            label = "融辉" if provider == "ronghui" else "韵达"
            params: dict[str, Any] = {"sql_only": True, "sync_sql": True}
            if start_date == end_date:
                params["target_date"] = start_date
            else:
                params["start_date"] = start_date
                params["end_date"] = end_date
            if provider == "ronghui":
                tool_name = "sync_daily_send_orders"
            else:
                tool_name = "sync_yunda_send_waybills"
                params.update({"sync_sheet": False, "session_profile": "yunda"})

            result = self._agent_request(
                "POST",
                "/run-tool",
                payload={"tool_name": tool_name, "params": params},
                timeout=max(1860, self.settings.agent_timeout_seconds),
            )
            if not result.get("ok"):
                sync_status["warnings"].append(f"{label}刷新失败：{self._waybill_agent_error_text(result)}")
                continue

            data = result.get("data")
            if not isinstance(data, dict):
                sync_status["warnings"].append(f"{label}刷新失败：Agent 返回格式异常。")
                continue
            if data.get("success") is False:
                sync_status["warnings"].append(f"{label}刷新失败：{self._waybill_agent_error_text(data)}")
                continue

            if isinstance(data.get("result"), dict):
                tool_result = data["result"]
            elif isinstance(data.get("data"), dict):
                tool_result = data["data"]
            else:
                tool_result = data
            if tool_result.get("error") or tool_result.get("ok") is False:
                sync_status["warnings"].append(f"{label}刷新失败：{self._waybill_agent_error_text(tool_result)}")
                continue
            sync_status["messages"].append(self._waybill_sync_message_from_agent(label, tool_result))

        return sync_status

    def _render_waybills(self, handler: BaseHTTPRequestHandler, query: dict) -> None:
        def first_value(name: str, default: str = "") -> str:
            return str(query.get(name, [default])[0] or "").strip()

        def positive_int(name: str, default: int) -> int:
            try:
                return max(int(first_value(name, str(default))), 1)
            except ValueError:
                return default

        requested_date_from = normalize_open_date(first_value("date_from")).replace("-", "/")
        requested_date_to = normalize_open_date(first_value("date_to")).replace("-", "/")
        filters = {
            "q": first_value("q"),
            "date_from": requested_date_from,
            "date_to": requested_date_to,
            "status": first_value("status", "all").lower() or "all",
            "source": first_value("source", "all").lower() or "all",
            "payment_method": first_value("payment_method"),
            "delivery_method": first_value("delivery_method"),
            "sort": first_value("sort", "open_date_desc") or "open_date_desc",
        }
        if filters["status"] != "all":
            filters["status"] = normalize_waybill_status(filters["status"])
        if filters["source"] not in {"all", *WAYBILL_SOURCE_LABELS.keys()}:
            filters["source"] = "all"
        if filters["sort"] not in {"open_date_desc", "open_date_asc"}:
            filters["sort"] = "open_date_desc"
        page = positive_int("page", 1)
        page_size = min(max(positive_int("page_size", 50), 10), 100)
        has_requested_date_filter = bool(requested_date_from or requested_date_to)
        has_active_non_date_filters = any(
            str(filters.get(name, "") or "").strip()
            for name in ("q", "payment_method", "delivery_method")
        ) or filters["status"] != "all" or filters["source"] != "all"
        if not has_requested_date_filter and not has_active_non_date_filters:
            today = self._waybill_default_date()
            filters["date_from"] = today
            filters["date_to"] = today
        has_active_filters = has_requested_date_filter or has_active_non_date_filters

        status_options = [
            {"value": "all", "label": "全部状态", "tone": "muted"},
            *[
                {"value": value, "label": label, "tone": WAYBILL_STATUS_TONES[value]}
                for value, label in WAYBILL_STATUS_LABELS.items()
            ],
        ]
        source_options = [
            {"value": "all", "label": "全部来源"},
            *[
                {"value": value, "label": label}
                for value, label in WAYBILL_SOURCE_LABELS.items()
            ],
        ]
        payment_options = ["", "现付", "寄付", "到付", "提付", "月结"]
        delivery_options = ["", "送货", "自提", "派送"]
        sort_options = [
            {"value": "open_date_desc", "label": "按开单日期倒序"},
            {"value": "open_date_asc", "label": "按开单日期正序"},
        ]

        def empty_result() -> dict[str, Any]:
            result = {
                "rows": [],
                "summary": {
                    "total": 0,
                    "manual_count": 0,
                    "ocr_count": 0,
                    "fee_total": "0.00",
                    "opening_cost_total": "0.00",
                    "insurance_total": "0.00",
                    "cod_total": "0.00",
                    "pickup_payment_total": "0.00",
                    "invalid_money_count": 0,
                    "latest_created_at": "",
                    "latest_open_date": "",
                },
                "pagination": {
                    "page": 1,
                    "page_size": page_size,
                    "total": 0,
                    "total_pages": 1,
                    "offset": 0,
                    "has_prev": False,
                    "has_next": False,
                },
            }
            return result

        sync_status = self._refresh_waybill_sources_for_filters(filters) if has_active_filters else {"messages": [], "warnings": []}

        if has_active_filters:
            try:
                result = self.repository.search_waybills(filters, page=page, page_size=page_size)
                db_error = ""
            except Exception as exc:
                result = empty_result()
                db_error = str(exc)
        else:
            result = empty_result()
            db_error = ""

        pagination = result["pagination"]
        base_query = {
            **{
                k: v
                for k, v in filters.items()
                if str(v) and not (k in {"status", "source"} and v == "all")
            },
            "page_size": str(pagination["page_size"]),
        }
        prev_url = ""
        next_url = ""
        if pagination["has_prev"]:
            prev_url = "/waybills?" + urlencode({**base_query, "page": pagination["page"] - 1})
        if pagination["has_next"]:
            next_url = "/waybills?" + urlencode({**base_query, "page": pagination["page"] + 1})
        current_url = "/waybills?" + urlencode({**base_query, "page": pagination["page"]})

        template = self.template_env.get_template("waybills.html")
        body = template.render(
            app_title=self.settings.app_title,
            filters=filters,
            rows=result["rows"],
            summary=result["summary"],
            pagination=pagination,
            prev_url=prev_url,
            next_url=next_url,
            current_url=current_url,
            status_options=status_options,
            source_options=source_options,
            payment_options=payment_options,
            delivery_options=delivery_options,
            sort_options=sort_options,
            db_error=db_error,
            sync_status=sync_status,
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
        )
        self._send_html(handler, body)

    @staticmethod
    def _receipt_default_date() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _receipt_filters_from_query(self, query: dict[str, list[str]]) -> dict[str, str]:
        def first_value(name: str, default: str = "") -> str:
            return str((query or {}).get(name, [default])[0] or "").strip()

        platform = first_value("platform", "all").lower() or "all"
        if platform not in {"all", "yunda", "ronghui"}:
            platform = "all"
        photo_status = first_value("photo_status", "all").lower() or "all"
        if photo_status not in {"all", "has_photo", "missing_photo"}:
            photo_status = "all"
        date_from = first_value("date_from")
        date_to = first_value("date_to")
        if not date_from and not date_to:
            today = self._receipt_default_date()
            date_from = today
            date_to = today
        return {
            "platform": platform,
            "direction": "send",
            "q": first_value("q"),
            "receipt_status": first_value("receipt_status", "all") or "all",
            "audit_status": first_value("audit_status", "all") or "all",
            "photo_status": photo_status,
            "date_from": date_from,
            "date_to": date_to,
        }

    def _receipt_positive_int(self, query: dict[str, list[str]], name: str, default: int) -> int:
        try:
            return max(int(str(query.get(name, [str(default)])[0] or default)), 1)
        except (TypeError, ValueError):
            return default

    def _receipt_query_requested(self, query: dict[str, list[str]]) -> bool:
        raw = str(query.get("queried", [""])[0] or "").strip().lower()
        return raw in {"1", "true", "yes"}

    def _empty_receipt_search_result(self, *, page_size: int) -> dict[str, Any]:
        return {
            "rows": [],
            "pagination": {
                "page": 1,
                "page_size": page_size,
                "total": 0,
                "total_pages": 1,
                "offset": 0,
                "has_prev": False,
                "has_next": False,
            },
        }

    def _render_receipts(self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> None:
        filters = self._receipt_filters_from_query(query)
        page = self._receipt_positive_int(query, "page", 1)
        page_size = min(max(self._receipt_positive_int(query, "page_size", 50), 10), 100)
        query_requested = self._receipt_query_requested(query)
        db_error = ""
        if not query_requested:
            result = self._empty_receipt_search_result(page_size=page_size)
        else:
            try:
                result = self.repository.search_receipts(filters, page=page, page_size=page_size)
            except Exception as exc:
                result = self._empty_receipt_search_result(page_size=page_size)
                db_error = str(exc)
        pagination = result["pagination"]
        base_query = {
            key: value
            for key, value in filters.items()
            if str(value or "").strip() and value != "all"
        }
        if query_requested:
            base_query["queried"] = "1"
        base_query["page_size"] = str(pagination["page_size"])
        prev_url = ""
        next_url = ""
        if pagination.get("has_prev"):
            prev_url = "/receipts?" + urlencode({**base_query, "page": pagination["page"] - 1})
        if pagination.get("has_next"):
            next_url = "/receipts?" + urlencode({**base_query, "page": pagination["page"] + 1})
        template = self.template_env.get_template("receipts.html")
        body = template.render(
            app_title=self.settings.app_title,
            filters=filters,
            rows=result["rows"],
            pagination=pagination,
            prev_url=prev_url,
            next_url=next_url,
            yunda_live_url=YUNDA_RECEIPT_LIVE_PROXY_PREFIX,
            ronghui_live_url=RONGHUI_RECEIPT_LIVE_PROXY_PREFIX,
            query_requested=query_requested,
            db_error=db_error,
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
        )
        self._send_html(handler, body)

    def _handle_receipts_data(self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> None:
        filters = self._receipt_filters_from_query(query)
        page = self._receipt_positive_int(query, "page", 1)
        page_size = min(max(self._receipt_positive_int(query, "page_size", 50), 10), 100)
        if not self._receipt_query_requested(query):
            self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": self._empty_receipt_search_result(page_size=page_size)})
            return
        try:
            result = self.repository.search_receipts(filters, page=page, page_size=page_size)
        except Exception as exc:
            self._send_json(
                handler,
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "message": f"回单列表查询失败：{exc}", "data": {"rows": [], "pagination": {}}},
            )
            return
        self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": result})

    def _parse_receipt_path_id(self, path: str, prefix: str) -> int | None:
        raw = str(path or "").strip().rstrip("/")
        if not raw.startswith(prefix):
            return None
        tail = raw[len(prefix) :].strip("/")
        if not tail or "/" in tail:
            return None
        try:
            return int(tail)
        except ValueError:
            return None

    def _receipt_detail_text(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (list, tuple, set)):
            parts = [self._receipt_detail_text(item) for item in value]
            return " / ".join(part for part in parts if part)
        if isinstance(value, dict):
            for key in ("text", "value", "name", "title", "link"):
                text = self._receipt_detail_text(value.get(key))
                if text:
                    return text
            parts = [self._receipt_detail_text(item) for item in value.values()]
            return " / ".join(part for part in parts if part)
        text = str(value).strip()
        if text.startswith("="):
            text = text[1:].strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
            text = text[1:-1].strip()
        return re.sub(r"\s+", " ", text).strip()

    def _receipt_detail_missing(self, summary: dict[str, Any]) -> list[str]:
        return [key for key in RECEIPT_DETAIL_KEYS if not self._receipt_detail_text(summary.get(key))]

    def _receipt_detail_append_source(self, sources: list[str], source: str) -> None:
        if source and source not in sources:
            sources.append(source)

    def _receipt_detail_merge_missing(
        self,
        summary: dict[str, str],
        values: dict[str, Any],
        *,
        source: str,
        sources: list[str],
    ) -> bool:
        filled = False
        for key in RECEIPT_DETAIL_KEYS:
            if self._receipt_detail_text(summary.get(key)):
                continue
            text = self._receipt_detail_text(values.get(key))
            if not text:
                continue
            summary[key] = text
            filled = True
        if filled:
            self._receipt_detail_append_source(sources, source)
        return filled

    def _receipt_detail_summary_from_record(self, record: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
        raw_payload = record.get("raw_payload")
        existing = record.get("detail_summary")
        if isinstance(existing, dict):
            base = {key: self._receipt_detail_text(existing.get(key)) for key in RECEIPT_DETAIL_KEYS}
        else:
            base = DocumentRepository._receipt_detail_summary(raw_payload, record)
            base = {key: self._receipt_detail_text(base.get(key)) for key in RECEIPT_DETAIL_KEYS}
        if not self._receipt_detail_text(base.get("waybill_no")):
            base["waybill_no"] = self._receipt_detail_text(record.get("waybill_no") or record.get("receipt_no"))

        sources: list[str] = []
        raw_only = DocumentRepository._receipt_detail_summary(raw_payload, {})
        if any(self._receipt_detail_text(raw_only.get(key)) for key in RECEIPT_DETAIL_KEYS):
            self._receipt_detail_append_source(sources, "raw_payload")
        return base, sources

    def _receipt_detail_weight_volume(self, value: Any) -> dict[str, str]:
        text = self._receipt_detail_text(value)
        if not text:
            return {}
        result: dict[str, str] = {}
        weight_match = re.search(r"实际重量\s*[:：]?\s*([^/；;|,\n]+)", text)
        if weight_match:
            result["actual_weight"] = weight_match.group(1).strip()
        volume_match = re.search(r"(?:^|[/；;|,\s])体积(?!重)\s*[:：]?\s*([^/；;|,\n]+)", text)
        if volume_match:
            result["volume"] = volume_match.group(1).strip()
        return result

    def _receipt_detail_from_local_waybill(self, waybill: dict[str, Any] | None) -> dict[str, str]:
        if not isinstance(waybill, dict):
            return {}
        values = {
            "recipient_name": waybill.get("receiver_name"),
            "recipient_address": waybill.get("receiver_address"),
            "goods_name": waybill.get("goods_name_lines"),
            "package_type": waybill.get("package_type_lines"),
            "piece_count": waybill.get("quantity_lines"),
            "waybill_no": waybill.get("waybill_no"),
        }
        values.update(self._receipt_detail_weight_volume(waybill.get("weight_volume")))
        return {key: self._receipt_detail_text(values.get(key)) for key in RECEIPT_DETAIL_KEYS if self._receipt_detail_text(values.get(key))}

    def _receipt_detail_platform(self, record: dict[str, Any]) -> str:
        return str(record.get("platform") or "").strip().lower()

    def _receipt_detail_should_query_tms(self, record: dict[str, Any], waybill_no: str) -> bool:
        platform = self._receipt_detail_platform(record)
        code = str(waybill_no or "").strip().upper()
        return platform in {"ronghui", "r7"} or code.startswith("R")

    def _receipt_detail_should_query_feishu(self, record: dict[str, Any], waybill_no: str) -> bool:
        return self._receipt_detail_platform(record) == "yunda" and bool(str(waybill_no or "").strip())

    def _receipt_detail_first_matching_row(self, rows: list[Any], waybill_no: str) -> tuple[dict[str, Any] | None, str]:
        wanted = str(waybill_no or "").strip()
        dict_rows = [row for row in rows if isinstance(row, dict)]
        if not dict_rows:
            return None, "未返回详情行"
        matches = []
        for row in dict_rows:
            for key in ("waybill_no", "bill_code", "billCode", "tracking_number", "trackingNumber"):
                if self._receipt_detail_text(row.get(key)) == wanted:
                    matches.append(row)
                    break
        if len(matches) == 1:
            return matches[0], ""
        if not matches and len(dict_rows) == 1:
            return dict_rows[0], ""
        return None, f"返回 {len(dict_rows)} 行但无法精确匹配单号 {wanted}"

    def _receipt_detail_rows_from_agent_payload(self, payload: Any) -> list[Any]:
        current = payload
        for _ in range(4):
            if isinstance(current, list):
                return current
            if not isinstance(current, dict):
                return []
            for key in ("records", "rows", "items"):
                value = current.get(key)
                if isinstance(value, list):
                    return value
            current = current.get("data")
        return []

    def _receipt_detail_from_tms(self, waybill_no: str) -> tuple[dict[str, str], str]:
        payload = {
            "params": {
                "bill_codes": [waybill_no],
                "decrypt_masked": True,
                "browser_headless": True,
                "browser_timeout_ms": 30_000,
                "browser_batch_size": 1,
                "browser_max_workers": 1,
                "max_workers": 1,
            },
            "timeout_sec": max(45, min(120, int(getattr(self.settings, "agent_timeout_seconds", 30) or 30) + 15)),
        }
        response = self._agent_request(
            "POST",
            "/tms/query_waybill_detail",
            payload=payload,
            timeout=max(50, payload["timeout_sec"] + 5),
        )
        if not response.get("ok"):
            return {}, self._receipt_detail_text(response.get("error")) or "TMS 详情接口不可达"
        data = response.get("data")
        if isinstance(data, dict) and data.get("ok") is False:
            return {}, self._receipt_detail_text(data.get("message") or data.get("error")) or "TMS 详情接口返回失败"
        row, error = self._receipt_detail_first_matching_row(self._receipt_detail_rows_from_agent_payload(data), waybill_no)
        if error:
            return {}, error
        if not row:
            return {}, "TMS 详情接口未返回数据"
        values = {
            "recipient_name": row.get("recipient_name"),
            "recipient_address": row.get("recipient_address"),
            "goods_name": row.get("goods_name"),
            "package_type": row.get("package_type"),
            "piece_count": row.get("quantity") or row.get("piece_count"),
            "actual_weight": row.get("actual_weight"),
            "volume": row.get("volume"),
            "waybill_no": row.get("tracking_number") or row.get("waybill_no") or row.get("bill_code"),
        }
        return {key: self._receipt_detail_text(values.get(key)) for key in RECEIPT_DETAIL_KEYS if self._receipt_detail_text(values.get(key))}, ""

    def _receipt_detail_from_feishu_fields(self, fields: dict[str, Any]) -> dict[str, str]:
        values: dict[str, str] = {}
        for key, field_names in RECEIPT_FEISHU_FIELD_MAP.items():
            for field_name in field_names:
                text = self._receipt_detail_text(fields.get(field_name))
                if text:
                    values[key] = text
                    break
        return values

    def _receipt_detail_from_feishu(self, waybill_no: str) -> tuple[dict[str, str], str]:
        filter_payload = {
            "conjunction": "and",
            "conditions": [
                {
                    "field_name": RECEIPT_FEISHU_WAYBILL_FIELD,
                    "operator": "is",
                    "value": [waybill_no],
                }
            ],
        }
        response = self._agent_request(
            "POST",
            "/run-tool",
            payload={
                "tool_name": "feishu_operation",
                "params": {
                    "action": "search_records",
                    "params": {
                        "base_token": RECEIPT_FEISHU_BASE_TOKEN,
                        "table_id": RECEIPT_FEISHU_TABLE_ID,
                        "view_id": RECEIPT_FEISHU_VIEW_ID,
                        "field_names": list(RECEIPT_FEISHU_FIELD_NAMES),
                        "filter": filter_payload,
                        "page_size": 1,
                    },
                },
            },
            timeout=35,
        )
        if not response.get("ok"):
            return {}, self._receipt_detail_text(response.get("error")) or "飞书精确查询不可达"
        data = response.get("data")
        if isinstance(data, dict) and data.get("ok") is False:
            return {}, self._receipt_detail_text(data.get("message") or data.get("error")) or "飞书精确查询失败"
        rows = self._receipt_detail_rows_from_agent_payload(data)
        row, error = self._receipt_detail_first_matching_row(rows, waybill_no)
        if error:
            return {}, error
        fields = row.get("fields") if isinstance(row, dict) else None
        if not isinstance(fields, dict):
            return {}, "飞书精确查询未返回字段"
        return self._receipt_detail_from_feishu_fields(fields), ""

    def _enrich_receipt_detail_record(self, record: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(record)
        summary, sources = self._receipt_detail_summary_from_record(enriched)
        errors: list[str] = []
        waybill_no = self._receipt_detail_text(summary.get("waybill_no") or enriched.get("waybill_no"))

        if waybill_no and self._receipt_detail_missing(summary):
            platform = self._receipt_detail_platform(enriched)
            try:
                waybill = self.repository.get_waybill_by_no(waybill_no, source=platform if platform else None)
            except Exception as exc:
                waybill = None
                errors.append(f"local_waybills: {exc}")
            self._receipt_detail_merge_missing(
                summary,
                self._receipt_detail_from_local_waybill(waybill),
                source="local_waybills",
                sources=sources,
            )

        if waybill_no and self._receipt_detail_missing(summary) and self._receipt_detail_should_query_tms(enriched, waybill_no):
            values, error = self._receipt_detail_from_tms(waybill_no)
            if error:
                errors.append(f"tms_detail: {error}")
            self._receipt_detail_merge_missing(summary, values, source="tms_detail", sources=sources)

        if waybill_no and self._receipt_detail_missing(summary) and self._receipt_detail_should_query_feishu(enriched, waybill_no):
            values, error = self._receipt_detail_from_feishu(waybill_no)
            if error:
                errors.append(f"feishu_bitable: {error}")
            self._receipt_detail_merge_missing(summary, values, source="feishu_bitable", sources=sources)

        enriched["detail_summary"] = {key: self._receipt_detail_text(summary.get(key)) for key in RECEIPT_DETAIL_KEYS}
        enriched["detail_summary_source"] = ",".join(sources) if sources else "无数据"
        enriched["detail_summary_missing"] = self._receipt_detail_missing(enriched["detail_summary"])
        if errors:
            enriched["detail_summary_error"] = "；".join(errors)
        else:
            enriched.pop("detail_summary_error", None)
        return enriched

    def _handle_receipt_detail(self, handler: BaseHTTPRequestHandler, path: str) -> None:
        receipt_id = self._parse_receipt_path_id(path, "/receipts/")
        if receipt_id is None:
            self._send_json(handler, HTTPStatus.NOT_FOUND, {"ok": False, "message": "回单不存在。"})
            return
        try:
            detail = self.repository.get_receipt_detail(receipt_id)
        except Exception as exc:
            self._send_json(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "message": f"回单详情查询失败：{exc}"})
            return
        if not detail:
            self._send_json(handler, HTTPStatus.NOT_FOUND, {"ok": False, "message": "回单不存在。"})
            return
        record = detail.get("record") if isinstance(detail, dict) else None
        if isinstance(record, dict):
            detail = dict(detail)
            detail["record"] = self._enrich_receipt_detail_record(record)
        self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": detail})

    def _parse_receipt_audit_path_id(self, path: str) -> int | None:
        raw = str(path or "").strip().rstrip("/")
        prefix = "/receipts/"
        suffix = "/audit"
        if not raw.startswith(prefix) or not raw.endswith(suffix):
            return None
        tail = raw[len(prefix) : -len(suffix)].strip("/")
        if not tail or "/" in tail:
            return None
        try:
            return int(tail)
        except ValueError:
            return None

    def _parse_receipt_audit_body(self, handler: BaseHTTPRequestHandler) -> dict[str, Any]:
        raw_body = self._read_request_body(handler)
        if not raw_body:
            return {}
        content_type = str(handler.headers.get("Content-Type") or "").lower()
        if "json" in content_type:
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError:
                return {}
            return payload if isinstance(payload, dict) else {}
        parsed = parse_qs(raw_body.decode("utf-8", errors="replace"), keep_blank_values=True)
        return {str(key): str(values[-1] if values else "") for key, values in parsed.items()}

    def _handle_receipt_audit(self, handler: BaseHTTPRequestHandler, path: str) -> None:
        receipt_id = self._parse_receipt_audit_path_id(path)
        if receipt_id is None:
            self._send_json(handler, HTTPStatus.NOT_FOUND, {"ok": False, "message": "回单不存在。"})
            return
        body = self._parse_receipt_audit_body(handler)
        result_value = str(body.get("result") or "").strip().lower()
        if result_value not in {"passed", "failed"}:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": "审核结果必须是 passed 或 failed。"})
            return
        reason = str(body.get("reason") or "").strip()
        try:
            record = self.repository.get_receipt_record(receipt_id)
        except Exception as exc:
            self._send_json(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "message": f"回单详情查询失败：{exc}"})
            return
        if not record:
            self._send_json(handler, HTTPStatus.NOT_FOUND, {"ok": False, "message": "回单不存在。"})
            return

        operator = str((getattr(handler, "current_admin_user", None) or {}).get("username") or "")
        params = {
            "receipt_id": receipt_id,
            "platform": str(record.get("platform") or "").strip(),
            "direction": str(record.get("direction") or "").strip(),
            "result": result_value,
            "reason": reason,
            "waybill_no": str(record.get("waybill_no") or "").strip(),
            "receipt_no": str(record.get("receipt_no") or "").strip(),
            "return_waybill_no": str(record.get("return_waybill_no") or "").strip(),
            "raw_payload": record.get("raw_payload") if isinstance(record.get("raw_payload"), dict) else {},
        }
        audit_log_request = {
            key: params[key]
            for key in (
                "receipt_id",
                "platform",
                "direction",
                "result",
                "reason",
                "waybill_no",
                "receipt_no",
                "return_waybill_no",
            )
        }
        execution = str(body.get("execution") or "").strip().lower()
        if execution == "original_page":
            audit_status = "审核通过" if result_value == "passed" else "审核不通过"
            try:
                updated_record = self.repository.update_receipt_audit_status(receipt_id, audit_status)
            except Exception as exc:
                self.repository.record_receipt_audit_log(
                    receipt_id=receipt_id,
                    platform=params["platform"],
                    direction=params["direction"],
                    action="audit_original_page",
                    result_status="failed",
                    operator=operator,
                    request_summary=audit_log_request,
                    response_status="LOCAL_UPDATE_FAILED",
                    message=str(exc),
                )
                self._send_json(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "message": f"本地审核状态更新失败：{exc}"})
                return
            message = f"已在原页执行{'审核通过' if result_value == 'passed' else '审核不通过'}。"
            audit_payload = {
                "ok": True,
                "platform": params["platform"],
                "result_status": "original_page_executed",
                "audit_status": audit_status,
                "message": message,
            }
            self.repository.record_receipt_audit_log(
                receipt_id=receipt_id,
                platform=params["platform"],
                direction=params["direction"],
                action="audit_original_page",
                result_status="success",
                operator=operator,
                request_summary=audit_log_request,
                response_status="ORIGINAL_PAGE_EXECUTED",
                message=message,
            )
            self._send_json(
                handler,
                HTTPStatus.OK,
                {
                    "ok": True,
                    "message": message,
                    "data": {
                        "record": updated_record or {**record, "audit_status": audit_status},
                        "audit": audit_payload,
                    },
                },
            )
            return
        agent_result = self._agent_request(
            "POST",
            "/tms/receipts_audit",
            payload={"params": params, "timeout_sec": RECEIPT_QUERY_AGENT_TIMEOUT_SEC},
            timeout=max(RECEIPT_QUERY_AGENT_TIMEOUT_SEC + 15, self.settings.agent_timeout_seconds),
        )
        if not agent_result.get("ok"):
            self.repository.record_receipt_audit_log(
                receipt_id=receipt_id,
                platform=params["platform"],
                direction=params["direction"],
                action="audit",
                result_status="failed",
                operator=operator,
                request_summary=audit_log_request,
                response_status=str(agent_result.get("status") or ""),
                message=str(agent_result.get("error") or "Agent call failed"),
            )
            self._send_json(handler, HTTPStatus.BAD_GATEWAY, {"ok": False, "message": "回单审核调用失败。", "error": agent_result.get("error")})
            return

        outer = agent_result.get("data") if isinstance(agent_result.get("data"), dict) else {}
        audit_payload = outer.get("data") if isinstance(outer.get("data"), dict) else outer
        if outer.get("ok") is False or (isinstance(audit_payload, dict) and audit_payload.get("ok") is False):
            message = str(
                (audit_payload if isinstance(audit_payload, dict) else {}).get("message")
                or outer.get("message")
                or outer.get("error")
                or "回单审核失败。"
            )
            error_code = str(
                (audit_payload if isinstance(audit_payload, dict) else {}).get("error_code")
                or outer.get("error_code")
                or "AUDIT_FAILED"
            )
            self.repository.record_receipt_audit_log(
                receipt_id=receipt_id,
                platform=params["platform"],
                direction=params["direction"],
                action="audit",
                result_status="failed",
                operator=operator,
                request_summary=audit_log_request,
                response_status=error_code,
                message=message,
            )
            self._send_json(handler, HTTPStatus.OK, {"ok": False, "error_code": error_code, "message": message})
            return

        if not isinstance(audit_payload, dict):
            audit_payload = {}
        audit_status = str(audit_payload.get("audit_status") or ("审核通过" if result_value == "passed" else "审核不通过")).strip()
        try:
            updated_record = self.repository.update_receipt_audit_status(receipt_id, audit_status)
        except Exception as exc:
            self.repository.record_receipt_audit_log(
                receipt_id=receipt_id,
                platform=params["platform"],
                direction=params["direction"],
                action="audit",
                result_status="failed",
                operator=operator,
                request_summary=audit_log_request,
                response_status="LOCAL_UPDATE_FAILED",
                message=str(exc),
            )
            self._send_json(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "message": f"本地审核状态更新失败：{exc}"})
            return
        self.repository.record_receipt_audit_log(
            receipt_id=receipt_id,
            platform=params["platform"],
            direction=params["direction"],
            action="audit",
            result_status="success",
            operator=operator,
            request_summary=audit_log_request,
            response_status=str(audit_payload.get("result_status") or "ok"),
            message=str(audit_payload.get("message") or "回单审核完成。"),
        )
        self._send_json(
            handler,
            HTTPStatus.OK,
            {
                "ok": True,
                "message": str(audit_payload.get("message") or "回单审核完成。"),
                "data": {
                    "record": updated_record or record,
                    "audit": audit_payload,
                },
            },
        )

    def _handle_receipt_attachment(
        self,
        handler: BaseHTTPRequestHandler,
        path: str,
        query: dict[str, list[str]] | None = None,
    ) -> None:
        attachment_id = self._parse_receipt_path_id(path, "/receipts/attachments/")
        if attachment_id is None:
            self._send_json(handler, HTTPStatus.NOT_FOUND, {"ok": False, "message": "附件不存在。"})
            return
        try:
            attachment = self.repository.get_receipt_attachment(attachment_id)
        except Exception as exc:
            self._send_json(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "message": f"附件查询失败：{exc}"})
            return
        if not attachment:
            self._send_json(handler, HTTPStatus.NOT_FOUND, {"ok": False, "message": "附件不存在。"})
            return
        target = self._resolve_receipt_attachment_target(attachment)
        if not target:
            self._send_json(
                handler,
                HTTPStatus.NOT_FOUND,
                {"ok": False, "message": "本地缓存缺失。", "source_url": str(attachment.get("source_url") or "")},
            )
            return
        content_type = str(attachment.get("mime_type") or "").strip()
        if not content_type:
            content_type, _ = mimetypes.guess_type(str(target))
        with target.open("rb") as handle:
            payload = handle.read()
        extra_headers = None
        if self._receipt_download_requested(query):
            filename = self._receipt_attachment_download_filename(attachment, target, content_type or "")
            extra_headers = {"Content-Disposition": self._content_disposition_attachment(filename)}
        self._send_bytes(
            handler,
            HTTPStatus.OK,
            payload,
            content_type or "application/octet-stream",
            extra_headers=extra_headers,
        )

    def _handle_receipts_image_archive(self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> None:
        if not self._receipt_query_requested(query):
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": "请先查询回单列表后再下载图片。"})
            return
        filters = self._receipt_filters_from_query(query)
        try:
            attachments = self.repository.list_receipt_image_attachments_for_filters(filters)
        except Exception as exc:
            self._send_json(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "message": f"回单图片查询失败：{exc}"})
            return
        if not attachments:
            self._send_json(handler, HTTPStatus.NOT_FOUND, {"ok": False, "message": "当前查询列表没有可下载的回单图片。"})
            return
        archive_buffer = io.BytesIO()
        used_names: set[str] = set()
        added = 0
        with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for attachment in attachments:
                target = self._resolve_receipt_attachment_target(attachment)
                if not target:
                    continue
                content_type = str(attachment.get("mime_type") or "").strip()
                if not content_type:
                    content_type, _ = mimetypes.guess_type(str(target))
                base_name = self._receipt_archive_entry_base_name(attachment)
                suffix = self._receipt_archive_entry_suffix(attachment, target, content_type or "")
                archive_name = self._unique_receipt_archive_entry_name(base_name, suffix, used_names)
                archive.write(target, archive_name)
                added += 1
        if added <= 0:
            self._send_json(handler, HTTPStatus.NOT_FOUND, {"ok": False, "message": "当前查询列表没有可下载的本地回单图片。"})
            return
        filename = self._receipt_archive_filename(filters)
        payload = archive_buffer.getvalue()
        self._send_bytes(
            handler,
            HTTPStatus.OK,
            payload,
            "application/zip",
            extra_headers={"Content-Disposition": self._content_disposition_attachment(filename)},
        )

    def _resolve_receipt_attachment_target(self, attachment: dict[str, Any]) -> Path | None:
        runtime_root = self.settings.runtime_dir.resolve()

        def resolve_local_path(local_path: str) -> Path | None:
            candidate = Path(str(local_path or "").replace("\\", "/"))
            if not str(candidate):
                return None
            target = candidate.resolve() if candidate.is_absolute() else (runtime_root / candidate).resolve()
            try:
                target.relative_to(runtime_root)
            except ValueError:
                return None
            return target if target.exists() and target.is_file() else None

        target = resolve_local_path(str(attachment.get("local_path") or "").strip())
        if target and self._is_receipt_attachment_image_file(target):
            return target
        local_path = self._cache_receipt_attachment_from_source(attachment)
        if not local_path:
            return None
        return resolve_local_path(local_path)

    def _receipt_archive_filename(self, filters: dict[str, Any]) -> str:
        date_from = str(filters.get("date_from") or "").strip()
        date_to = str(filters.get("date_to") or "").strip()
        if date_from and date_to:
            return f"receipt-images-{date_from}-{date_to}.zip"
        return "receipt-images.zip"

    def _receipt_archive_entry_base_name(self, attachment: dict[str, Any]) -> str:
        waybill_no = self._safe_receipt_archive_name_part(
            attachment.get("waybill_no") or attachment.get("receipt_no") or attachment.get("id") or "receipt"
        )
        remark_suffix = self._receipt_remark_suffix(attachment.get("receipt_raw_payload"))
        return f"{waybill_no}-{remark_suffix}" if remark_suffix else waybill_no

    def _receipt_archive_entry_suffix(self, attachment: dict[str, Any], target: Path, content_type: str) -> str:
        candidates = [
            target.suffix,
            Path(str(attachment.get("display_name") or "").replace("\\", "/")).suffix,
            Path(urlparse(str(attachment.get("source_url") or "")).path).suffix,
            mimetypes.guess_extension(str(content_type or "").split(";", 1)[0].strip()),
        ]
        for suffix in candidates:
            suffix_text = str(suffix or "").strip().lower()
            if suffix_text in {".gif", ".jpeg", ".jpg", ".png", ".webp"}:
                return suffix_text
        return ".bin"

    def _unique_receipt_archive_entry_name(self, base_name: str, suffix: str, used_names: set[str]) -> str:
        cleaned_base = self._safe_receipt_archive_name_part(base_name)
        cleaned_suffix = str(suffix or "").strip()
        if cleaned_suffix and not cleaned_suffix.startswith("."):
            cleaned_suffix = f".{cleaned_suffix}"
        candidate = f"{cleaned_base}{cleaned_suffix or '.bin'}"
        index = 2
        while candidate in used_names:
            candidate = f"{cleaned_base}-{index}{cleaned_suffix or '.bin'}"
            index += 1
        used_names.add(candidate)
        return candidate

    def _safe_receipt_archive_name_part(self, value: Any) -> str:
        cleaned = re.sub(r"[^0-9A-Za-z._-]+", "_", str(value or "").strip())
        return cleaned.strip("._-") or "receipt"

    def _receipt_remark_suffix(self, raw_payload: Any) -> str:
        for value in self._receipt_remark_values(raw_payload):
            match = RECEIPT_REMARK_TOKEN_RE.search(value)
            if match:
                return f"{match.group(1)[-3:]}-{match.group(2)}"
        return ""

    def _receipt_remark_values(self, raw_payload: Any) -> list[str]:
        values: list[str] = []

        def collect_scalars(item: Any) -> None:
            if isinstance(item, (str, int, float)):
                values.append(str(item))
                return
            if isinstance(item, dict):
                for value in item.values():
                    collect_scalars(value)
                return
            if isinstance(item, (list, tuple)):
                for value in item:
                    collect_scalars(value)

        def walk(item: Any) -> None:
            if isinstance(item, dict):
                for key, value in item.items():
                    key_text = str(key or "")
                    key_lower = key_text.lower()
                    is_remark_key = any(fragment in key_lower or fragment in key_text for fragment in RECEIPT_REMARK_KEY_FRAGMENTS)
                    if is_remark_key:
                        collect_scalars(value)
                    elif isinstance(value, (dict, list, tuple)):
                        walk(value)
                return
            if isinstance(item, (list, tuple)):
                for value in item:
                    walk(value)

        walk(raw_payload)
        return values

    def _receipt_download_requested(self, query: dict[str, list[str]] | None) -> bool:
        raw = str((query or {}).get("download", [""])[0] or "").strip().lower()
        return raw in {"1", "true", "yes", "download"}

    def _receipt_attachment_download_filename(
        self,
        attachment: dict[str, Any],
        target: Path,
        content_type: str,
    ) -> str:
        attachment_id = int(attachment.get("id") or 0)
        display_name = str(attachment.get("display_name") or "").strip()
        base_name = Path(display_name.replace("\\", "/")).name.strip() if display_name else ""
        fallback = f"receipt-{attachment_id or 'attachment'}"
        if not base_name:
            base_name = fallback
        suffix = Path(base_name).suffix
        if not suffix:
            suffix = target.suffix or mimetypes.guess_extension(content_type or "") or ""
            if suffix:
                base_name = f"{base_name}{suffix}"
        return base_name

    def _content_disposition_attachment(self, filename: str) -> str:
        cleaned = "".join("_" if ord(ch) < 32 or ch in {'"', "\\", "/", ":"} else ch for ch in str(filename or "receipt"))
        cleaned = cleaned.strip().strip(".") or "receipt"
        ascii_name = "".join(ch if 32 <= ord(ch) < 127 else "_" for ch in cleaned).strip() or "receipt"
        return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(cleaned.encode('utf-8'))}"

    def _cache_receipt_attachment_from_source(self, attachment: dict[str, Any]) -> str:
        source_url = self._normalize_receipt_attachment_source_url(attachment, str(attachment.get("source_url") or ""))
        if not source_url:
            return ""
        fetched = self._fetch_receipt_attachment_source(attachment, source_url)
        if not fetched:
            return ""
        payload, content_type = fetched
        if not self._looks_like_receipt_image(payload):
            return ""
        digest = hashlib.sha256(payload).hexdigest()
        attachment_id = int(attachment.get("id") or 0)
        platform = str(attachment.get("platform") or "unknown").strip().lower() or "unknown"
        record_id = int(attachment.get("record_id") or 0)
        url_suffix = Path(urlparse(source_url).path).suffix.lower()
        ext = url_suffix if url_suffix in RECEIPT_IMAGE_SUFFIXES else ""
        if not ext:
            guessed = mimetypes.guess_extension(str(content_type or "").split(";", 1)[0].strip())
            ext = guessed if guessed in RECEIPT_IMAGE_SUFFIXES else ".bin"
        relative_path = Path("receipts") / platform / str(record_id or "unknown") / f"{attachment_id}_{digest[:12]}{ext}"
        target = (self.settings.runtime_dir / relative_path).resolve()
        runtime_root = self.settings.runtime_dir.resolve()
        try:
            target.relative_to(runtime_root)
        except ValueError:
            return ""
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        relpath = str(relative_path).replace("\\", "/")
        try:
            self.repository.update_receipt_attachment_cache(
                attachment_id,
                local_path=relpath,
                file_hash=digest,
                mime_type=content_type,
                file_size=len(payload),
            )
        except Exception:
            pass
        attachment["local_path"] = relpath
        attachment["file_hash"] = digest
        attachment["mime_type"] = content_type
        attachment["file_size"] = len(payload)
        return relpath

    @staticmethod
    def _looks_like_receipt_image(payload: bytes) -> bool:
        if not payload:
            return False
        return (
            payload.startswith(b"\xff\xd8")
            or payload.startswith(b"\x89PNG")
            or payload.startswith(b"GIF87a")
            or payload.startswith(b"GIF89a")
            or (len(payload) >= 12 and payload.startswith(b"RIFF") and payload[8:12] == b"WEBP")
        )

    def _is_receipt_attachment_image_file(self, target: Path) -> bool:
        try:
            with target.open("rb") as handle:
                return self._looks_like_receipt_image(handle.read(16))
        except OSError:
            return False

    def _normalize_receipt_attachment_source_url(self, attachment: dict[str, Any], source_url: str) -> str:
        raw = html.unescape(str(source_url or "").strip()).replace("\\", "/")
        if not raw:
            return ""
        if raw.startswith("//"):
            raw = f"https:{raw}"
        platform = str(attachment.get("platform") or "").strip().lower()
        parsed = urlparse(raw)
        if platform == "ronghui":
            host_path = raw.lstrip("/")
            if any(host_path.lower().startswith(f"{host}/") for host in RONGHUI_DIRECT_ATTACHMENT_HOSTS):
                raw = f"https://{host_path}"
                parsed = urlparse(raw)
            elif "ronghuiwl.com" in str(parsed.netloc).lower():
                embedded_host_path = (parsed.path or "").lstrip("/")
                if any(embedded_host_path.lower().startswith(f"{host}/") for host in RONGHUI_DIRECT_ATTACHMENT_HOSTS):
                    raw = f"https://{embedded_host_path}"
                    if parsed.query:
                        raw = f"{raw}?{parsed.query}"
                    parsed = urlparse(raw)
        host = str(parsed.hostname or "").strip().lower()
        if host in RONGHUI_DIRECT_ATTACHMENT_HOSTS and parsed.scheme.lower() != "https":
            raw = parsed._replace(scheme="https").geturl()
        return raw

    def _fetch_direct_receipt_attachment(self, source_url: str) -> tuple[bytes, str] | None:
        parsed = urlparse(source_url)
        host = str(parsed.hostname or "").strip().lower()
        if parsed.scheme.lower() != "https" or host not in RONGHUI_DIRECT_ATTACHMENT_HOSTS:
            return None
        suffix = Path(parsed.path or "").suffix.lower()
        if suffix not in RECEIPT_IMAGE_SUFFIXES:
            return None
        request = Request(source_url, headers={"Accept": "image/*,*/*", "User-Agent": "Mozilla/5.0"})
        try:
            with urlopen(request, timeout=180) as response:
                status_code = int(getattr(response, "status", 200) or 200)
                if status_code < 200 or status_code >= 300:
                    return None
                content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                if content_type and not content_type.startswith("image/"):
                    return None
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > RECEIPT_ATTACHMENT_MAX_BYTES:
                        return None
                    chunks.append(chunk)
        except (HTTPError, URLError, OSError, TimeoutError, ValueError):
            return None
        payload = b"".join(chunks)
        if not self._looks_like_receipt_image(payload):
            return None
        if not content_type:
            content_type = mimetypes.guess_type(parsed.path or "")[0] or "application/octet-stream"
        return payload, content_type

    def _fetch_receipt_attachment_source(self, attachment: dict[str, Any], source_url: str) -> tuple[bytes, str] | None:
        source_url = self._normalize_receipt_attachment_source_url(attachment, source_url)
        if not source_url:
            return None
        direct_attachment = self._fetch_direct_receipt_attachment(source_url)
        if direct_attachment:
            return direct_attachment
        parsed = urlparse(source_url)
        platform = str(attachment.get("platform") or "").strip().lower()
        netloc = parsed.netloc.lower()
        path = parsed.path or ""
        query = parse_qs(parsed.query)
        if platform == "yunda" or "yunda56.com" in netloc:
            if not path.startswith("/ky_inms/public/"):
                return None
            endpoint = "/tms/yunda_waybill_proxy"
            proxy_prefix = YUNDA_RECEIPT_LIVE_PROXY_PREFIX
        elif platform == "ronghui" or "ronghuiwl.com" in netloc:
            if not any(path.startswith(prefix) for prefix in RONGHUI_LIVE_ALLOWED_PREFIXES):
                return None
            endpoint = "/tms/ronghui_waybill_proxy"
            proxy_prefix = RONGHUI_RECEIPT_LIVE_PROXY_PREFIX
        else:
            return None
        result = self._agent_request(
            "POST",
            endpoint,
            payload={
                "params": {
                    "method": "GET",
                    "path": path,
                    "query": self._flatten_query(query),
                    "headers": {"Accept": "image/*,*/*"},
                    "content_type": "",
                    "proxy_prefix": proxy_prefix,
                },
                "timeout_sec": 180,
            },
            timeout=max(195, self.settings.agent_timeout_seconds),
        )
        if not result.get("ok"):
            return None
        proxy_payload = self._unwrap_yunda_live_proxy_payload(result)
        if not proxy_payload or proxy_payload.get("ok") is False:
            return None
        status_code = int(proxy_payload.get("status_code") or 200)
        if status_code < 200 or status_code >= 300:
            return None
        response_headers = proxy_payload.get("headers") if isinstance(proxy_payload.get("headers"), dict) else {}
        content_type = self._header_value(response_headers, "Content-Type") or "application/octet-stream"
        payload = self._decode_proxy_body(proxy_payload)
        if not self._looks_like_receipt_image(payload):
            return None
        return payload, content_type

    def _handle_receipts_sync(self, handler: BaseHTTPRequestHandler) -> None:
        raw_body = self._read_request_body(handler)
        content_type = str(handler.headers.get("Content-Type") or "").lower()
        body: dict[str, Any] = {}
        if raw_body and "json" in content_type:
            try:
                parsed_body = json.loads(raw_body.decode("utf-8"))
                body = parsed_body if isinstance(parsed_body, dict) else {}
            except json.JSONDecodeError:
                body = {}
        elif raw_body:
            parsed = parse_qs(raw_body.decode("utf-8", errors="replace"), keep_blank_values=True)
            body = {str(key): str(values[-1] if values else "") for key, values in parsed.items()}
        safe_params = {
            "platform": str(body.get("platform", "") or "").strip(),
            "direction": "send",
            "date_from": str(body.get("date_from", "") or "").strip(),
            "date_to": str(body.get("date_to", "") or "").strip(),
            "q": str(body.get("q", "") or "").strip(),
            "receipt_status": str(body.get("receipt_status", "") or "").strip(),
            "audit_status": str(body.get("audit_status", "") or "").strip(),
        }
        for optional_name in ("datagrid_url", "yunda_datagrid_url", "date_type", "code_type"):
            value = str(body.get(optional_name, "") or "").strip()
            if value:
                safe_params[optional_name] = value
        has_date_from = bool(safe_params["date_from"])
        has_date_to = bool(safe_params["date_to"])
        if has_date_from != has_date_to:
            self._send_json(
                handler,
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "message": "请选择完整的更新时间范围：开始日期和结束日期必须同时填写。"},
            )
            return
        if not has_date_from and not has_date_to:
            today = self._receipt_default_date()
            safe_params["date_from"] = today
            safe_params["date_to"] = today
        for date_name in ("date_from", "date_to"):
            value = safe_params[date_name]
            if not value:
                continue
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                self._send_json(
                    handler,
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "message": "更新时间范围格式无效，请使用 YYYY-MM-DD。"},
                )
                return
        safe_params["max_pages"] = str(RECEIPT_QUERY_MAX_PAGES)
        safe_params["timeout_sec"] = str(RECEIPT_QUERY_SOURCE_TIMEOUT_SEC)
        result = self._agent_request(
            "POST",
            "/tms/receipts_sync",
            payload={"params": safe_params, "timeout_sec": RECEIPT_QUERY_AGENT_TIMEOUT_SEC},
            timeout=max(RECEIPT_QUERY_AGENT_TIMEOUT_SEC + 15, self.settings.agent_timeout_seconds),
        )
        operator = str((getattr(handler, "current_admin_user", None) or {}).get("username") or "")
        if not result.get("ok"):
            self.repository.record_receipt_audit_log(
                action="sync",
                result_status="failed",
                operator=operator,
                request_summary=safe_params,
                response_status=str(result.get("status") or ""),
                message=str(result.get("error") or "Agent call failed"),
            )
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "message": "回单查询调用失败。", "error": result.get("error")},
            )
            return
        outer = result.get("data") if isinstance(result.get("data"), dict) else {}
        if outer.get("ok") is False:
            error_code = str(outer.get("error_code") or "").strip() or "SYNC_FAILED"
            message = str(outer.get("message") or outer.get("error") or "回单查询失败。")
            self.repository.record_receipt_audit_log(
                action="sync",
                result_status="failed",
                operator=operator,
                request_summary=safe_params,
                response_status=error_code,
                message=message,
            )
            self._send_json(handler, HTTPStatus.OK, {"ok": False, "error_code": error_code, "message": message})
            return
        sync_payload = outer.get("data") if isinstance(outer.get("data"), dict) else outer
        records = sync_payload.get("records") if isinstance(sync_payload, dict) else []
        if not isinstance(records, list):
            records = []

        stats = {
            "fetched": int((sync_payload.get("stats") or {}).get("fetched") or len(records)) if isinstance(sync_payload, dict) else len(records),
            "filtered": 0,
            "upserted": 0,
            "attachments": 0,
        }
        for record_payload in records:
            if not isinstance(record_payload, dict):
                stats["filtered"] += 1
                continue
            payload = dict(record_payload)
            attachments = payload.pop("attachments", [])
            try:
                saved = self.repository.upsert_receipt_record(payload)
            except Exception:
                stats["filtered"] += 1
                continue
            if not saved:
                stats["filtered"] += 1
                continue
            stats["upserted"] += 1
            if isinstance(attachments, list):
                for attachment in attachments:
                    if not isinstance(attachment, dict):
                        continue
                    attachment_payload = dict(attachment)
                    attachment_payload["record_id"] = int(saved.get("id") or 0)
                    try:
                        self.repository.upsert_receipt_attachment(attachment_payload)
                        stats["attachments"] += 1
                    except Exception:
                        continue
        warnings = sync_payload.get("warnings") if isinstance(sync_payload, dict) else []
        self.repository.record_receipt_audit_log(
            action="sync",
            result_status="success",
            operator=operator,
            request_summary=safe_params,
            response_status="ok",
            message=json.dumps({"stats": stats, "warnings": warnings or []}, ensure_ascii=False),
        )
        self._send_json(
            handler,
            HTTPStatus.OK,
            {
                "ok": True,
                "message": "回单查询完成。",
                "params": safe_params,
                "stats": stats,
                "warnings": warnings or [],
            },
        )

    def _render_line_haul_contacts(self, handler: BaseHTTPRequestHandler, query: dict) -> None:
        def first_value(name: str, default: str = "") -> str:
            return str(query.get(name, [default])[0] or "").strip()

        def positive_int(name: str, default: int) -> int:
            try:
                return max(int(first_value(name, str(default))), 1)
            except ValueError:
                return default

        filters = {
            "q": first_value("q"),
        }
        page = positive_int("page", 1)
        page_size = min(max(positive_int("page_size", 50), 20), 100)
        has_search = bool(filters["q"])
        db_error = ""

        def empty_result() -> dict[str, Any]:
            return {
                "rows": [],
                "summary": {
                    "total": 0,
                    "active_count": 0,
                    "inactive_count": 0,
                },
                "pagination": {
                    "page": 1,
                    "page_size": page_size,
                    "total": 0,
                    "total_pages": 1,
                    "offset": 0,
                    "has_prev": False,
                    "has_next": False,
                },
            }

        if has_search:
            try:
                result = self.repository.search_line_haul_contacts_page(filters, page=page, page_size=page_size)
            except Exception as exc:
                result = empty_result()
                db_error = str(exc)
        else:
            result = empty_result()

        pagination = result["pagination"]
        base_query = {
            **{k: v for k, v in filters.items() if str(v)},
            "page_size": str(pagination["page_size"]),
        }
        current_query = dict(base_query)
        if has_search:
            current_query["page"] = str(pagination["page"])
        return_to = "/line-haul-contacts"
        if current_query:
            return_to += "?" + urlencode(current_query)
        prev_url = ""
        next_url = ""
        if pagination["has_prev"]:
            prev_url = "/line-haul-contacts?" + urlencode({**base_query, "page": pagination["page"] - 1})
        if pagination["has_next"]:
            next_url = "/line-haul-contacts?" + urlencode({**base_query, "page": pagination["page"] + 1})

        display_rows = self._line_haul_contact_display_rows(result["rows"])
        template = self.template_env.get_template("line_haul_contacts.html")
        body = template.render(
            app_title=self.settings.app_title,
            filters=filters,
            rows=display_rows,
            summary=result["summary"],
            pagination=pagination,
            prev_url=prev_url,
            next_url=next_url,
            return_to=return_to,
            has_search=has_search,
            db_error=db_error,
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
        )
        self._send_html(handler, body)

    @staticmethod
    def _line_haul_contact_display_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        display_rows: list[dict[str, Any]] = []
        index = 0
        while index < len(rows):
            company_name = str(rows[index].get("company_name", "") or "")
            group_end = index + 1
            while (
                group_end < len(rows)
                and str(rows[group_end].get("company_name", "") or "") == company_name
            ):
                group_end += 1
            group_size = group_end - index
            for row_index in range(index, group_end):
                display_row = dict(rows[row_index])
                display_row["show_company"] = row_index == index
                display_row["company_rowspan"] = group_size if row_index == index else 0
                display_rows.append(display_row)
            index = group_end
        return display_rows

    def _render_waybill_print(self, handler: BaseHTTPRequestHandler, waybill_id: int, query: dict) -> None:
        waybill = self.repository.get_waybill(waybill_id)
        if not waybill:
            self._send_text(handler, HTTPStatus.NOT_FOUND, "Waybill not found.")
            return
        autoprint = str(query.get("autoprint", [""])[0]).lower() in {"1", "true", "yes"}
        print_preview = str(query.get("preview", [""])[0]).lower() in {"1", "true", "yes"}
        template = self.template_env.get_template("waybill_print.html")
        body = template.render(
            app_title=self.settings.app_title,
            waybill=waybill,
            autoprint=autoprint,
            print_preview=print_preview,
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
        )
        self._send_html(handler, body)

    @staticmethod
    def _first_tracking_text(row: dict[str, Any], *keys: str) -> str:
        for key in keys:
            value = row.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    @classmethod
    def _normalize_tracking_payload(cls, data: dict[str, Any]) -> dict[str, Any]:
        return data

    def _handle_tracking_query(self, handler: BaseHTTPRequestHandler) -> None:
        content_length = int(handler.headers.get("Content-Length", 0))
        raw = handler.rfile.read(content_length) if content_length else b""
        try:
            body = json.loads(raw) if raw else {}
        except Exception:
            body = {}
        tracking_number = str(body.get("tracking_number", "")).strip()
        if not tracking_number:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "请输入运单号"})
            return

        result = self._agent_request(
            "POST",
            "/tms/tracking_query",
            payload={
                "params": {"tracking_number": tracking_number, "decrypt_masked": True},
                "timeout_sec": 180,
            },
            timeout=max(195, self.settings.agent_timeout_seconds),
        )
        if not result.get("ok"):
            error = result.get("error")
            message = "单号查询服务暂时不可用，请稍后重试。"
            if isinstance(error, dict):
                message = str(error.get("error") or error.get("message") or message)
            elif error:
                message = str(error)
            self._send_json(handler, HTTPStatus.BAD_GATEWAY, {"error": message})
            return

        data = result.get("data")
        if not isinstance(data, dict):
            self._send_json(handler, HTTPStatus.BAD_GATEWAY, {"error": "单号查询服务返回格式异常。"})
            return
        if data.get("ok") is False and data.get("error"):
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {"error": str(data.get("error") or data.get("message") or "单号查询失败。")},
            )
            return
        if isinstance(data.get("data"), dict) and data.get("type") is None:
            data = data["data"]
        if data.get("ok") is False or data.get("error"):
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {"error": str(data.get("error") or data.get("message") or "单号查询失败。")},
            )
            return
        data = self._normalize_tracking_payload(data)
        self._send_json(handler, HTTPStatus.OK, data)

    def _parse_json_body(self, handler: BaseHTTPRequestHandler) -> dict[str, Any]:
        content_length = int(handler.headers.get("Content-Length", 0))
        raw = handler.rfile.read(content_length) if content_length else b""
        if not raw:
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            return {}
        return body if isinstance(body, dict) else {}

    def _read_request_body(self, handler: BaseHTTPRequestHandler) -> bytes:
        content_length = int(handler.headers.get("Content-Length", 0) or 0)
        return handler.rfile.read(content_length) if content_length else b""

    def _yunda_live_remote_path(self, path: str) -> str:
        raw_path = str(path or "").strip()
        if raw_path.rstrip("/") == YUNDA_LIVE_PROXY_PREFIX:
            return YUNDA_LIVE_ENTRY_PATH
        if not raw_path.startswith(YUNDA_LIVE_PROXY_PREFIX + "/"):
            return ""
        remote_path = "/" + unquote(raw_path[len(YUNDA_LIVE_PROXY_PREFIX) :].lstrip("/"))
        return remote_path if remote_path.startswith("/ky_inms/public/") else ""

    def _yunda_receipt_live_remote_path(self, path: str) -> str:
        raw_path = str(path or "").strip()
        if raw_path.rstrip("/") == YUNDA_RECEIPT_LIVE_PROXY_PREFIX:
            return YUNDA_RECEIPT_LIVE_ENTRY_PATH
        if not raw_path.startswith(YUNDA_RECEIPT_LIVE_PROXY_PREFIX + "/"):
            return ""
        remote_path = "/" + unquote(raw_path[len(YUNDA_RECEIPT_LIVE_PROXY_PREFIX) :].lstrip("/"))
        return remote_path if remote_path.startswith("/ky_inms/public/") else ""

    def _ronghui_live_remote_path(self, path: str) -> str:
        raw_path = str(path or "").strip()
        if raw_path.rstrip("/") == RONGHUI_LIVE_PROXY_PREFIX:
            return ""
        if not raw_path.startswith(RONGHUI_LIVE_PROXY_PREFIX + "/"):
            return ""
        remote_path = "/" + unquote(raw_path[len(RONGHUI_LIVE_PROXY_PREFIX) :].lstrip("/"))
        return remote_path if any(remote_path.startswith(prefix) for prefix in RONGHUI_LIVE_ALLOWED_PREFIXES) else ""

    def _ronghui_receipt_live_remote_path(self, path: str) -> str:
        raw_path = str(path or "").strip()
        if raw_path.rstrip("/") == RONGHUI_RECEIPT_LIVE_PROXY_PREFIX:
            return ""
        if not raw_path.startswith(RONGHUI_RECEIPT_LIVE_PROXY_PREFIX + "/"):
            return ""
        remote_path = "/" + unquote(raw_path[len(RONGHUI_RECEIPT_LIVE_PROXY_PREFIX) :].lstrip("/"))
        return remote_path if any(remote_path.startswith(prefix) for prefix in RONGHUI_LIVE_ALLOWED_PREFIXES) else ""

    def _flatten_query(self, query: dict[str, list[str]]) -> str:
        pairs: list[tuple[str, str]] = []
        for key, values in (query or {}).items():
            if isinstance(values, list):
                pairs.extend((str(key), str(value)) for value in values)
            else:
                pairs.append((str(key), str(values)))
        return urlencode(pairs, doseq=True)

    def _ronghui_receipt_entry_menu_text(self, query: dict[str, list[str]]) -> str:
        values = (query or {}).get("receipt_entry") or ["send"]
        raw_entry = values[0] if isinstance(values, list) and values else values
        entry = str(raw_entry or "send").strip().lower()
        return RONGHUI_RECEIPT_ENTRY_MENU_TEXTS.get(entry, RONGHUI_RECEIPT_ENTRY_MENU_TEXTS["send"])

    def _handler_headers(self, handler: BaseHTTPRequestHandler) -> dict[str, str]:
        headers: dict[str, str] = {}
        for key, value in getattr(handler, "headers", {}).items():
            headers[str(key)] = str(value)
        return headers

    def _unwrap_yunda_live_proxy_payload(self, result: dict[str, Any]) -> dict[str, Any] | None:
        outer = result.get("data")
        if not isinstance(outer, dict):
            return None
        nested = outer.get("data")
        if isinstance(nested, dict) and ("body_base64" in nested or "status_code" in nested):
            return nested
        if "body_base64" in outer or "status_code" in outer:
            return outer
        return None

    def _decode_proxy_body(self, proxy_payload: dict[str, Any]) -> bytes:
        raw_base64 = str(proxy_payload.get("body_base64") or "")
        if not raw_base64:
            return b""
        try:
            return base64.b64decode(raw_base64)
        except Exception:
            return b""

    def _header_value(self, headers: dict[str, Any], name: str) -> str:
        for key, value in (headers or {}).items():
            if str(key).lower() == name.lower():
                return str(value)
        return ""

    def _parse_urlencoded_form_body(self, body: bytes, content_type: str) -> dict[str, str]:
        charset = "utf-8"
        match = re.search(r"charset=([A-Za-z0-9._-]+)", str(content_type or ""), flags=re.IGNORECASE)
        if match:
            charset = match.group(1)
        parsed = parse_qs(body.decode(charset, errors="replace"), keep_blank_values=True)
        return {str(key): str(values[-1] if values else "") for key, values in parsed.items()}

    def _decode_proxy_json_body(self, proxy_payload: dict[str, Any]) -> dict[str, Any]:
        headers = proxy_payload.get("headers") if isinstance(proxy_payload.get("headers"), dict) else {}
        content_type = self._header_value(headers, "Content-Type")
        raw_body = self._decode_proxy_body(proxy_payload)
        text = raw_body.decode("utf-8", errors="replace").strip()
        if "json" not in content_type.lower() and not text.startswith("{"):
            return {}
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _persist_yunda_live_save_result(
        self,
        *,
        request_body: bytes,
        request_content_type: str,
        proxy_payload: dict[str, Any],
    ) -> dict[str, Any]:
        response_json = self._decode_proxy_json_body(proxy_payload)
        if not response_json:
            return {}
        save_ok = str(response_json.get("info") or "").strip() == "1" or response_json.get("ok") is True
        if not save_ok:
            return {}
        form_fields = self._parse_urlencoded_form_body(request_body, request_content_type)
        normalized_form = {**form_fields}
        for key, value in response_json.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                normalized_form[str(key)] = "" if value is None else str(value)
        remote_waybill_no = str(response_json.get("LogisticsId") or normalized_form.get("LogisticsId") or "").strip()
        mapped = build_console_waybill_from_yunda_data(normalized_form, remote_waybill_no=remote_waybill_no)
        if not mapped:
            return {}
        waybill = self.repository.upsert_provider_waybill(mapped, source="yunda")
        waybill_id = int(waybill.get("id", 0) or 0) if waybill else None
        self.repository.create_waybill_provider_snapshot(
            provider="yunda",
            remote_waybill_no=remote_waybill_no,
            snapshot_kind="save_request",
            payload={
                "action": "live_proxy_save",
                "normalized_form": normalized_form,
                "content_type": request_content_type,
            },
            waybill_id=waybill_id,
        )
        self.repository.create_waybill_provider_snapshot(
            provider="yunda",
            remote_waybill_no=remote_waybill_no,
            snapshot_kind="save_response",
            payload={
                "action": "live_proxy_save",
                "response": response_json,
                "remote_path": proxy_payload.get("remote_path", ""),
            },
            waybill_id=waybill_id,
        )
        if not waybill_id:
            return {}
        return {
            "shipnow_local_waybill_id": waybill_id,
            "shipnow_print_url": f"/waybills/{waybill_id}/print?preview=1",
            "shipnow_autoprint_url": f"/waybills/{waybill_id}/print?autoprint=1",
        }

    def _patch_proxy_json_body(self, proxy_payload: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        if not patch:
            return proxy_payload
        response_json = self._decode_proxy_json_body(proxy_payload)
        if not response_json:
            return proxy_payload
        patched = dict(response_json)
        patched.update(patch)
        updated_payload = dict(proxy_payload)
        updated_payload["body_base64"] = base64.b64encode(
            json.dumps(patched, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        return updated_payload

    def _persist_ronghui_live_save_result(
        self,
        *,
        request_body: bytes,
        request_content_type: str,
        proxy_payload: dict[str, Any],
    ) -> None:
        response_json = self._decode_proxy_json_body(proxy_payload)
        if not response_json:
            return
        message = str(response_json.get("message") or response_json.get("msg") or "").strip()
        save_ok = response_json.get("success") is True or response_json.get("ok") is True or "成功" in message
        if not save_ok:
            return
        form_fields = self._parse_urlencoded_form_body(request_body, request_content_type)
        remote_waybill_no = str(response_json.get("BILL_CODE") or form_fields.get("BILL_CODE") or "").strip()
        self.repository.create_waybill_provider_snapshot(
            provider="ronghui",
            remote_waybill_no=remote_waybill_no,
            snapshot_kind="save_request",
            payload={
                "action": "live_proxy_save",
                "form_fields": form_fields,
                "content_type": request_content_type,
            },
            waybill_id=None,
        )
        self.repository.create_waybill_provider_snapshot(
            provider="ronghui",
            remote_waybill_no=remote_waybill_no,
            snapshot_kind="save_response",
            payload={
                "action": "live_proxy_save",
                "response": response_json,
                "remote_path": proxy_payload.get("remote_path", ""),
            },
            waybill_id=None,
        )

    def _send_proxy_bytes(
        self,
        handler: BaseHTTPRequestHandler,
        status: HTTPStatus,
        payload: bytes,
        headers: dict[str, Any],
    ) -> None:
        content_type = self._header_value(headers, "Content-Type") or "application/octet-stream"
        cache_control = self._header_value(headers, "Cache-Control") or "no-store"
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        for key, value in (headers or {}).items():
            key_text = str(key)
            if key_text.lower() in {
                "content-type",
                "content-length",
                "transfer-encoding",
                "set-cookie",
                "cache-control",
            }:
                continue
            handler.send_header(key_text, str(value))
        handler.send_header("Cache-Control", cache_control)
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    def _handle_yunda_receipt_live_proxy(
        self,
        handler: BaseHTTPRequestHandler,
        path: str,
        *,
        method: str,
        query: dict[str, list[str]],
    ) -> None:
        remote_path = self._yunda_receipt_live_remote_path(path)
        if not remote_path:
            self._send_json(
                handler,
                HTTPStatus.NOT_FOUND,
                {"ok": False, "message": "韵达回单原页代理路径不存在。", "error_code": "INVALID_PROXY_PATH"},
            )
            return
        request_body = self._read_request_body(handler) if method.upper() != "GET" else b""
        request_content_type = str(handler.headers.get("Content-Type") or "")
        params = {
            "method": method.upper(),
            "path": remote_path,
            "query": self._flatten_query(query),
            "headers": self._handler_headers(handler),
            "content_type": request_content_type,
            "proxy_prefix": YUNDA_RECEIPT_LIVE_PROXY_PREFIX,
        }
        if request_body:
            params["body_base64"] = base64.b64encode(request_body).decode("ascii")
        result = self._agent_request(
            "POST",
            "/tms/yunda_waybill_proxy",
            payload={"params": params, "timeout_sec": 180},
            timeout=max(195, self.settings.agent_timeout_seconds),
        )
        if not result.get("ok"):
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "message": "韵达回单原页代理调用失败。", "error": result.get("error")},
            )
            return
        proxy_payload = self._unwrap_yunda_live_proxy_payload(result)
        if not proxy_payload:
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "message": "韵达回单原页代理返回格式异常。"},
            )
            return
        if proxy_payload.get("ok") is False:
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "message": str(proxy_payload.get("error") or "韵达回单原页代理失败。")},
            )
            return
        response_headers = proxy_payload.get("headers") if isinstance(proxy_payload.get("headers"), dict) else {}
        response_status = HTTPStatus(int(proxy_payload.get("status_code") or 200))
        self._send_proxy_bytes(
            handler,
            response_status,
            self._decode_proxy_body(proxy_payload),
            response_headers,
        )

    def _handle_ronghui_receipt_live_proxy(
        self,
        handler: BaseHTTPRequestHandler,
        path: str,
        *,
        method: str,
        query: dict[str, list[str]],
    ) -> None:
        remote_path = self._ronghui_receipt_live_remote_path(path)
        is_entry_root = str(path or "").strip().rstrip("/") == RONGHUI_RECEIPT_LIVE_PROXY_PREFIX
        if not remote_path and not is_entry_root:
            self._send_json(
                handler,
                HTTPStatus.NOT_FOUND,
                {"ok": False, "message": "融辉回单原页代理路径不存在。", "error_code": "INVALID_PROXY_PATH"},
            )
            return
        request_body = self._read_request_body(handler) if method.upper() != "GET" else b""
        request_content_type = str(handler.headers.get("Content-Type") or "")
        remote_query = "" if is_entry_root else self._flatten_query(query)
        params = {
            "method": method.upper(),
            "path": remote_path,
            "query": remote_query,
            "headers": self._handler_headers(handler),
            "content_type": request_content_type,
            "proxy_prefix": RONGHUI_RECEIPT_LIVE_PROXY_PREFIX,
        }
        if is_entry_root:
            params["entry_menu_text"] = self._ronghui_receipt_entry_menu_text(query)
        if request_body:
            params["body_base64"] = base64.b64encode(request_body).decode("ascii")
        result = self._agent_request(
            "POST",
            "/tms/ronghui_waybill_proxy",
            payload={"params": params, "timeout_sec": 180},
            timeout=max(195, self.settings.agent_timeout_seconds),
        )
        if not result.get("ok"):
            if str(result.get("error_code") or "") == "AUTH_REQUIRED":
                self._send_ronghui_auth_required_iframe(
                    handler,
                    str(result.get("error") or result.get("message") or "当前未登录或登录态已过期。"),
                )
                return
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "message": "融辉回单原页代理调用失败。", "error": result.get("error")},
            )
            return
        agent_payload = result.get("data") if isinstance(result.get("data"), dict) else {}
        auth_payload = None
        if isinstance(agent_payload, dict) and agent_payload.get("ok") is False:
            auth_payload = agent_payload
        nested_agent_payload = agent_payload.get("data") if isinstance(agent_payload, dict) else None
        if isinstance(nested_agent_payload, dict) and nested_agent_payload.get("ok") is False:
            auth_payload = nested_agent_payload
        if isinstance(auth_payload, dict) and str(auth_payload.get("error_code") or "") == "AUTH_REQUIRED":
            self._send_ronghui_auth_required_iframe(
                handler,
                str(auth_payload.get("error") or auth_payload.get("message") or "当前未登录或登录态已过期。"),
            )
            return
        proxy_payload = self._unwrap_yunda_live_proxy_payload(result)
        if not proxy_payload:
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "message": "融辉回单原页代理返回格式异常。"},
            )
            return
        if proxy_payload.get("ok") is False:
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "message": str(proxy_payload.get("error") or "融辉回单原页代理失败。")},
            )
            return
        response_headers = proxy_payload.get("headers") if isinstance(proxy_payload.get("headers"), dict) else {}
        response_status = HTTPStatus(int(proxy_payload.get("status_code") or 200))
        self._send_proxy_bytes(
            handler,
            response_status,
            self._decode_proxy_body(proxy_payload),
            response_headers,
        )

    def _handle_yunda_live_proxy(
        self,
        handler: BaseHTTPRequestHandler,
        path: str,
        *,
        method: str,
        query: dict[str, list[str]],
    ) -> None:
        remote_path = self._yunda_live_remote_path(path)
        if not remote_path:
            self._send_json(
                handler,
                HTTPStatus.NOT_FOUND,
                {"ok": False, "message": "韵达原页代理路径不存在。", "error_code": "INVALID_PROXY_PATH"},
            )
            return
        request_body = self._read_request_body(handler) if method.upper() != "GET" else b""
        request_content_type = str(handler.headers.get("Content-Type") or "")
        params = {
            "method": method.upper(),
            "path": remote_path,
            "query": self._flatten_query(query),
            "headers": self._handler_headers(handler),
            "content_type": request_content_type,
            "proxy_prefix": YUNDA_LIVE_PROXY_PREFIX,
        }
        if request_body:
            params["body_base64"] = base64.b64encode(request_body).decode("ascii")
        result = self._agent_request(
            "POST",
            "/tms/yunda_waybill_proxy",
            payload={"params": params, "timeout_sec": 180},
            timeout=max(195, self.settings.agent_timeout_seconds),
        )
        if not result.get("ok"):
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "message": "韵达原页代理调用失败。", "error": result.get("error")},
            )
            return
        proxy_payload = self._unwrap_yunda_live_proxy_payload(result)
        if not proxy_payload:
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "message": "韵达原页代理返回格式异常。"},
            )
            return
        if proxy_payload.get("ok") is False:
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "message": str(proxy_payload.get("error") or "韵达原页代理失败。")},
            )
            return
        if method.upper() == "POST" and str(proxy_payload.get("remote_path") or remote_path) == YUNDA_LIVE_SAVE_PATH:
            print_patch = self._persist_yunda_live_save_result(
                request_body=request_body,
                request_content_type=request_content_type,
                proxy_payload=proxy_payload,
            )
            proxy_payload = self._patch_proxy_json_body(proxy_payload, print_patch)
        response_headers = proxy_payload.get("headers") if isinstance(proxy_payload.get("headers"), dict) else {}
        response_status = HTTPStatus(int(proxy_payload.get("status_code") or 200))
        self._send_proxy_bytes(
            handler,
            response_status,
            self._decode_proxy_body(proxy_payload),
            response_headers,
        )

    def _handle_ronghui_live_proxy(
        self,
        handler: BaseHTTPRequestHandler,
        path: str,
        *,
        method: str,
        query: dict[str, list[str]],
    ) -> None:
        remote_path = self._ronghui_live_remote_path(path)
        is_entry_root = str(path or "").strip().rstrip("/") == RONGHUI_LIVE_PROXY_PREFIX
        if not remote_path and not is_entry_root:
            self._send_json(
                handler,
                HTTPStatus.NOT_FOUND,
                {"ok": False, "message": "融辉原页代理路径不存在。", "error_code": "INVALID_PROXY_PATH"},
            )
            return
        request_body = self._read_request_body(handler) if method.upper() != "GET" else b""
        request_content_type = str(handler.headers.get("Content-Type") or "")
        params = {
            "method": method.upper(),
            "path": remote_path,
            "query": self._flatten_query(query),
            "headers": self._handler_headers(handler),
            "content_type": request_content_type,
            "proxy_prefix": RONGHUI_LIVE_PROXY_PREFIX,
        }
        if request_body:
            params["body_base64"] = base64.b64encode(request_body).decode("ascii")
        result = self._agent_request(
            "POST",
            "/tms/ronghui_waybill_proxy",
            payload={"params": params, "timeout_sec": 180},
            timeout=max(195, self.settings.agent_timeout_seconds),
        )
        if not result.get("ok"):
            if str(result.get("error_code") or "") == "AUTH_REQUIRED":
                self._send_ronghui_auth_required_iframe(
                    handler,
                    str(result.get("error") or result.get("message") or "当前未登录或登录态已过期。"),
                )
                return
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "message": "融辉原页代理调用失败。", "error": result.get("error")},
            )
            return
        agent_payload = result.get("data") if isinstance(result.get("data"), dict) else {}
        auth_payload = None
        if isinstance(agent_payload, dict) and agent_payload.get("ok") is False:
            auth_payload = agent_payload
        nested_agent_payload = agent_payload.get("data") if isinstance(agent_payload, dict) else None
        if isinstance(nested_agent_payload, dict) and nested_agent_payload.get("ok") is False:
            auth_payload = nested_agent_payload
        if isinstance(auth_payload, dict) and str(auth_payload.get("error_code") or "") == "AUTH_REQUIRED":
            self._send_ronghui_auth_required_iframe(
                handler,
                str(auth_payload.get("error") or auth_payload.get("message") or "当前未登录或登录态已过期。"),
            )
            return
        proxy_payload = self._unwrap_yunda_live_proxy_payload(result)
        if not proxy_payload:
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "message": "融辉原页代理返回格式异常。"},
            )
            return
        if proxy_payload.get("ok") is False:
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "message": str(proxy_payload.get("error") or "融辉原页代理失败。")},
            )
            return
        if method.upper() == "POST" and str(proxy_payload.get("remote_path") or remote_path) == RONGHUI_LIVE_SAVE_PATH:
            self._persist_ronghui_live_save_result(
                request_body=request_body,
                request_content_type=request_content_type,
                proxy_payload=proxy_payload,
            )
        response_headers = proxy_payload.get("headers") if isinstance(proxy_payload.get("headers"), dict) else {}
        response_status = HTTPStatus(int(proxy_payload.get("status_code") or 200))
        self._send_proxy_bytes(
            handler,
            response_status,
            self._decode_proxy_body(proxy_payload),
            response_headers,
        )

    def _send_ronghui_auth_required_iframe(self, handler: BaseHTTPRequestHandler, auth_text: str) -> None:
        body = (
            "<!doctype html><html><head><meta charset=\"utf-8\"></head><body>"
            "<pre>AUTH_REQUIRED\n"
            f"{html.escape(str(auth_text or '当前未登录或登录态已过期。'))}"
            "</pre></body></html>"
        ).encode("utf-8")
        self._send_proxy_bytes(
            handler,
            HTTPStatus.OK,
            body,
            {"Content-Type": "text/html; charset=utf-8"},
        )

    def _call_yunda_entry_runtime(
        self,
        action: str,
        *,
        form: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        timeout_sec: int = 180,
    ) -> tuple[HTTPStatus, dict[str, Any]]:
        result = self._agent_request(
            "POST",
            "/tms/yunda_waybill_entry",
            payload={
                "params": {
                    "action": action,
                    "form": form or {},
                    "context": context or {},
                },
                "timeout_sec": timeout_sec,
            },
            timeout=max(timeout_sec + 15, self.settings.agent_timeout_seconds),
        )
        if not result.get("ok"):
            error = result.get("error")
            if isinstance(error, dict):
                message = str(error.get("error") or error.get("message") or "韵达运行时调用失败。")
            else:
                message = str(error or "韵达运行时调用失败。")
            return HTTPStatus.BAD_GATEWAY, {
                "ok": False,
                "message": message,
                "auth_state": None,
                "data": {},
                "field_errors": {},
            }

        outer = result.get("data")
        if not isinstance(outer, dict):
            return HTTPStatus.BAD_GATEWAY, {
                "ok": False,
                "message": "韵达运行时返回格式异常。",
                "auth_state": None,
                "data": {},
                "field_errors": {},
            }

        auth_code = str(outer.get("error_code") or "").strip()
        if auth_code in {"AUTH_REQUIRED", "AUTH_PENDING_CODE"}:
            message = str(outer.get("error") or "韵达登录态不可用。")
            return HTTPStatus.OK, {
                "ok": False,
                "message": message,
                "auth_state": {"code": auth_code, "message": message},
                "data": {},
                "field_errors": {},
            }

        payload = outer.get("data")
        if not isinstance(payload, dict):
            payload = {
                "ok": bool(outer.get("ok")),
                "message": str(outer.get("error") or "韵达运行时返回格式异常。"),
                "data": {},
                "field_errors": {},
            }
        payload.setdefault("ok", bool(outer.get("ok", False)))
        payload.setdefault("message", "")
        payload.setdefault("data", {})
        payload.setdefault("field_errors", {})
        payload.setdefault("auth_state", {"code": "AUTHENTICATED"})
        return HTTPStatus.OK, payload

    def _persist_yunda_runtime_result(
        self,
        *,
        action: str,
        request_body: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        response = {
            "ok": bool(payload.get("ok")),
            "message": str(payload.get("message") or ""),
            "data": dict(payload.get("data") or {}),
            "field_errors": dict(payload.get("field_errors") or {}),
            "auth_state": payload.get("auth_state"),
        }
        if not response["ok"]:
            return response

        runtime_data = response["data"]
        normalized_form = runtime_data.get("normalized_form") if isinstance(runtime_data.get("normalized_form"), dict) else {}
        remote_waybill_no = str(runtime_data.get("waybill_no") or normalized_form.get("LogisticsId") or "").strip()
        snapshot_request = {
            "action": action,
            "request": request_body,
            "normalized_form": normalized_form,
        }
        snapshot_response = {
            "action": action,
            "payload": payload,
        }
        if action == "save":
            mapped = build_console_waybill_from_yunda_data(normalized_form, remote_waybill_no=remote_waybill_no)
            waybill = None
            waybill_id = None
            if mapped:
                waybill = self.repository.upsert_provider_waybill(mapped, source="yunda")
                waybill_id = int(waybill.get("id", 0) or 0) if waybill else None
            self.repository.create_waybill_provider_snapshot(
                provider="yunda",
                remote_waybill_no=remote_waybill_no,
                snapshot_kind="save_request",
                payload=snapshot_request,
                waybill_id=waybill_id,
            )
            self.repository.create_waybill_provider_snapshot(
                provider="yunda",
                remote_waybill_no=remote_waybill_no,
                snapshot_kind="save_response",
                payload={**snapshot_response, "mapped_record": mapped},
                waybill_id=waybill_id,
            )
            if waybill:
                runtime_data["local_waybill"] = waybill
                runtime_data["local_waybill_id"] = waybill_id
                runtime_data["print_url"] = f"/waybills/{waybill_id}/print?preview=1"
        elif action in {"drafts/save", "templates/save"}:
            self.repository.create_waybill_provider_snapshot(
                provider="yunda",
                remote_waybill_no=remote_waybill_no,
                snapshot_kind="draft_save" if action == "drafts/save" else "template_save",
                payload={
                    **snapshot_request,
                    "payload": payload,
                },
                waybill_id=None,
            )
        elif action.startswith("print/"):
            waybill = self.repository.get_waybill_by_no(remote_waybill_no, source="yunda") if remote_waybill_no else None
            waybill_id = int(waybill.get("id", 0) or 0) if waybill else None
            self.repository.create_waybill_provider_snapshot(
                provider="yunda",
                remote_waybill_no=remote_waybill_no,
                snapshot_kind="print",
                payload=snapshot_response,
                waybill_id=waybill_id,
            )
            if waybill_id:
                runtime_data["preview_url"] = f"/waybills/{waybill_id}/print?preview=1"
        return response

    def _handle_yunda_entry(self, handler: BaseHTTPRequestHandler, path: str) -> None:
        action = YUNDA_ENTRY_ACTIONS.get(path)
        if not action:
            self._send_json(
                handler,
                HTTPStatus.NOT_FOUND,
                {"ok": False, "message": "韵达动作不存在。", "data": {}, "field_errors": {}, "auth_state": None},
            )
            return
        body = self._parse_json_body(handler)
        form = body.get("form") if isinstance(body.get("form"), dict) else {}
        context = body.get("context") if isinstance(body.get("context"), dict) else {}
        client_meta = body.get("client_meta") if isinstance(body.get("client_meta"), dict) else {}
        runtime_context = dict(context)
        if client_meta:
            runtime_context["client_meta"] = client_meta
        status, payload = self._call_yunda_entry_runtime(action, form=form, context=runtime_context)
        if status == HTTPStatus.OK:
            payload = self._persist_yunda_runtime_result(action=action, request_body=body, payload=payload)
        self._send_json(handler, status, payload)

    def _build_virtual_automation_task(
        self,
        task_id: str,
        *,
        override: dict[str, Any] | None = None,
        feedback: dict[str, Any] | None = None,
        open_task_id: str | None = None,
        workflow_resources: dict[str, dict[str, Any]] | None = None,
        resource_overrides: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        override = override or {}
        workflow = automation_workflow_definition(task_id)
        runtime_state = self.automation_virtual_task_state.get(task_id, {})
        baseline = build_virtual_task_defaults(task_id)
        workflow_resources = workflow_resources or {}
        resource_overrides = resource_overrides or {}
        webhook_info = resolve_automation_webhook_info(
            task_id,
            workflow_resources=workflow_resources,
            agent_base_url=self.settings.agent_base_url,
        )
        resource_bindings = build_automation_resource_bindings(
            task_id,
            workflow_resources,
            resource_overrides=resource_overrides,
        )
        missing_required_resources = [
            item["resource_key"]
            for item in resource_bindings
            if item.get("missing") and item.get("required")
        ]
        missing_required_resource_labels = [
            item.get("display_name") or item["resource_key"]
            for item in resource_bindings
            if item.get("missing") and item.get("required")
        ]
        tool_params = runtime_state.get("tool_params")
        if not isinstance(tool_params, dict):
            tool_params = dict(baseline.get("tool_params") or {})
        raw_json = override.get("tool_params_json")
        if raw_json is None:
            raw_json = runtime_state.get("tool_params_json")
        if raw_json is None:
            raw_json = json.dumps(tool_params, ensure_ascii=False, indent=2)

        name_value = str(
            override.get("name")
            or runtime_state.get("name")
            or workflow.get("display_name")
            or baseline.get("name")
            or task_id
        )
        tool_name_value = str(
            override.get("tool_name")
            or runtime_state.get("tool_name")
            or workflow.get("tool_name")
            or baseline.get("tool_name")
            or ""
        )
        provider_value = automation_task_provider(task_id, workflow, tool_name_value)
        last_run_value = str(runtime_state.get("last_run") or "")
        last_status = str(runtime_state.get("last_status") or "")
        last_duration_ms = runtime_state.get("last_duration_ms")
        last_message_value = str(runtime_state.get("last_message") or "")
        task_mode_value = str(workflow.get("task_mode") or baseline.get("task_mode") or "manual")
        is_schedulable = task_mode_value == "scheduled"
        trigger_label = str(workflow.get("trigger_label") or "手动执行")
        override_schedule_times_raw = str(override.get("schedule_times_json", "") or "").strip()
        override_schedule_times: list[str] = []
        if override_schedule_times_raw:
            try:
                parsed_schedule_times = json.loads(override_schedule_times_raw)
            except json.JSONDecodeError:
                parsed_schedule_times = []
            if isinstance(parsed_schedule_times, list):
                override_schedule_times = normalize_schedule_times(
                    [str(item or "").strip() for item in parsed_schedule_times]
                )

        cron_values: list[str] = []
        if override.get("cron_expression"):
            cron_values = [
                item.strip()
                for item in str(override.get("cron_expression", "") or "").splitlines()
                if item.strip()
            ]
        if override_schedule_times:
            cron_values = [build_daily_cron_expression(item) or "" for item in override_schedule_times]
        if not cron_values and is_schedulable:
            default_schedule_times = workflow.get("default_schedule_times")
            if isinstance(default_schedule_times, list):
                cron_values = [
                    build_daily_cron_expression(str(item or "").strip()) or ""
                    for item in default_schedule_times
                    if str(item or "").strip()
                ]

        schedule_info = summarize_task_group_schedule(cron_values) if is_schedulable else {
            "supported": False,
            "time_values": [],
            "summary": str(workflow.get("schedule_summary") or trigger_label),
            "warning": "",
            "raw_value": "",
        }
        schedule_supported = bool(schedule_info.get("supported")) or (is_schedulable and not schedule_info.get("raw_value"))
        schedule_summary = str(schedule_info.get("summary") or workflow.get("schedule_summary") or trigger_label)
        schedule_warning = str(schedule_info.get("warning") or "")
        if is_schedulable and not schedule_info.get("raw_value"):
            schedule_warning = "当前未设置定时；可先手动执行，也可保存后启用定时。"

        return {
            "id": task_id,
            "note": str(workflow.get("note") or automation_task_note(task_id)),
            "provider": provider_value,
            "provider_label": automation_provider_label(provider_value),
            "system_badges": list(workflow.get("system_badges") or []),
            "task_id": task_id,
            "task_mode": task_mode_value,
            "display_task_id": task_id,
            "group_size": 1,
            "name_value": name_value,
            "tool_name_value": tool_name_value,
            "cron_expression_value": str(schedule_info.get("raw_value") or ""),
            "enabled_value": bool(override.get("enabled")) if is_schedulable else False,
            "tool_params_json": raw_json,
            "tool_param_fields": flatten_automation_fields(tool_params),
            "schedule_supported": schedule_supported if is_schedulable else False,
            "schedule_editable": is_schedulable,
            "schedule_time_values": list(schedule_info.get("time_values") or []) if is_schedulable else [],
            "schedule_summary": schedule_summary,
            "schedule_icon": "clock" if is_schedulable else "zap",
            "schedule_warning": schedule_warning if is_schedulable else f"此流程不写入 scheduled_tasks，默认通过{trigger_label}触发。",
            "trigger_label": trigger_label,
            "is_schedulable": is_schedulable,
            "has_webhook": bool(webhook_info.get("path")),
            "webhook_path": str(webhook_info.get("path") or ""),
            "webhook_full_url": str(webhook_info.get("full_url") or ""),
            "webhook_masked_url": str(webhook_info.get("masked_url") or ""),
            "webhook_token_enabled": bool(webhook_info.get("token_enabled")),
            "webhook_header_name": str(webhook_info.get("header_name") or ""),
            "webhook_body_json": json.dumps(webhook_info.get("body_example") or {}, ensure_ascii=False, indent=2),
            "resource_bindings": resource_bindings,
            "missing_required_resources": missing_required_resources,
            "missing_required_resource_labels": missing_required_resource_labels,
            "resource_blocked": bool(missing_required_resources),
            "last_status_value": last_status,
            "last_run_value": last_run_value,
            "last_activity_value": last_run_value,
            "last_duration_label": format_duration_label(last_duration_ms),
            "last_message_value": last_message_value,
            "last_error_summary": shorten_error_message(last_message_value) if last_status == "error" else "",
            "search_text": " ".join(
                item.lower()
                for item in (
                    name_value,
                    str(workflow.get("note") or ""),
                    tool_name_value,
                    task_id,
                    trigger_label,
                    automation_provider_label(provider_value),
                )
                if item
            ),
            "sort_order": int(workflow.get("order") or 999),
            "is_open": bool(open_task_id == task_id or feedback),
            "feedback": feedback,
        }

    def _record_virtual_task_runtime(
        self,
        task_id: str,
        *,
        payload: dict[str, Any] | None = None,
        last_run: str | None = None,
        last_status: str | None = None,
        last_duration_ms: int | None = None,
        last_message: str | None = None,
    ) -> None:
        state = dict(self.automation_virtual_task_state.get(task_id, {}))
        if payload:
            state["name"] = payload.get("name")
            state["tool_name"] = payload.get("tool_name")
            state["tool_params"] = dict(payload.get("tool_params") or {})
            state["tool_params_json"] = payload.get("tool_params_json") or json.dumps(
                payload.get("tool_params") or {},
                ensure_ascii=False,
                indent=2,
            )
        if last_run is not None:
            state["last_run"] = last_run
        if last_status is not None:
            state["last_status"] = last_status
        if last_duration_ms is not None:
            state["last_duration_ms"] = last_duration_ms
        if last_message is not None:
            state["last_message"] = last_message
        self.automation_virtual_task_state[task_id] = state

    def _finalize_automation_task_run(
        self,
        payload: dict[str, Any],
        run_result: dict[str, Any],
        *,
        started_at: float,
    ) -> dict[str, Any]:
        tool_response = run_result.get("data") if isinstance(run_result.get("data"), dict) else {}
        run_ok = bool(run_result.get("ok") and tool_response.get("success"))
        run_cancelled = bool(tool_response.get("canceled"))
        duration_s = tool_response.get("duration_s")
        try:
            duration_ms = (
                int(round(float(duration_s) * 1000))
                if duration_s is not None
                else int(round((time.perf_counter() - started_at) * 1000))
            )
        except (TypeError, ValueError):
            duration_ms = int(round((time.perf_counter() - started_at) * 1000))
        run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        error_message_full = normalize_feedback_text(
            tool_response.get("error") or run_result.get("error") or ""
        )
        error_message_value = shorten_error_message(error_message_full)
        feedback_meta = automation_runtime_feedback_meta(
            ok=run_ok,
            cancelled=run_cancelled,
            success_message=payload["name"],
            error_message=error_message_value,
        )

        if payload.get("task_mode") == "scheduled":
            self.repository.update_scheduled_task_runtime(
                base_task_id=payload["task_id"],
                last_run=run_time,
                last_status=feedback_meta["status"],
                last_duration_ms=duration_ms,
                last_message="" if run_ok else error_message_full,
            )
        else:
            self._record_virtual_task_runtime(
                payload["task_id"],
                payload=payload,
                last_run=run_time,
                last_status=feedback_meta["status"],
                last_duration_ms=duration_ms,
                last_message="" if run_ok else error_message_full,
            )

        return {
            "ok": run_ok,
            "cancelled": run_cancelled,
            "task_id": payload["task_id"],
            "title": feedback_meta["title"],
            "message": feedback_meta["message"],
            "status_label": feedback_meta["status_label"],
            "activity_label": "最近运行",
            "activity_value": run_time,
            "duration_label": format_duration_label(duration_ms),
            "error": error_message_full,
            "payload": tool_response.get("data") if run_ok else run_result,
        }

    def _start_automation_task_run(
        self,
        payload: dict[str, Any],
        *,
        started_at: float,
    ) -> None:
        if payload["tool_name"] in AUTOMATION_LONG_RUNNING_TOOLS:
            run_timeout = None
        else:
            run_timeout = max(
                AUTOMATION_RUN_TIMEOUTS.get(payload["tool_name"], 180),
                self.settings.agent_timeout_seconds,
            )

        started_stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if payload.get("task_mode") == "scheduled":
            self.repository.update_scheduled_task_runtime(
                base_task_id=payload["task_id"],
                last_run=started_stamp,
                last_status="running",
                last_duration_ms=None,
                last_message="",
            )
        else:
            self._record_virtual_task_runtime(
                payload["task_id"],
                payload=payload,
                last_run=started_stamp,
                last_status="running",
                last_duration_ms=None,
                last_message="",
            )

        def runner() -> None:
            run_result = self._agent_request(
                "POST",
                "/run-tool",
                payload={"tool_name": payload["tool_name"], "params": payload["tool_params"]},
                timeout=run_timeout,
            )
            self._finalize_automation_task_run(payload, run_result, started_at=started_at)

        thread = threading.Thread(
            target=runner,
            name=f"automation-run-{payload['task_id']}",
            daemon=True,
        )
        thread.start()

    def _render_automations(
        self,
        handler: BaseHTTPRequestHandler,
        query: dict,
        *,
        resource_overrides: dict[str, dict[str, Any]] | None = None,
        task_overrides: dict[str, dict[str, Any]] | None = None,
        task_feedbacks: dict[str, dict[str, Any]] | None = None,
        open_task_id: str | None = None,
    ) -> None:
        resource_overrides = resource_overrides or {}
        task_overrides = task_overrides or {}
        task_feedbacks = task_feedbacks or {}

        automation_db_warning = ""
        try:
            scheduled_rows = self.repository.list_scheduled_tasks()
            workflow_resource_rows = self.repository.list_workflow_resources()
        except Exception as exc:
            LOGGER.warning("Failed to load automations from repository: %s", exc)
            scheduled_rows = []
            workflow_resource_rows = []
            automation_db_warning = normalize_feedback_text(
                f"自动化任务数据库当前不可达，任务列表已临时降级为空，仅保留顶部 TMS 登录态验证模块。详情：{exc}"
            )

        grouped_rows: dict[str, list[dict[str, Any]]] = {}
        for row in scheduled_rows:
            grouped_rows.setdefault(normalize_task_group_id(str(row.get("id", "") or "")), []).append(row)
        workflow_resources = {
            str(item.get("resource_key", "") or ""): item
            for item in workflow_resource_rows
        }

        tasks_by_id: dict[str, dict[str, Any]] = {}
        for base_task_id, rows in grouped_rows.items():
            rows = sorted(
                rows,
                key=lambda item: (
                    task_group_slot_index(str(item.get("id", "") or "")),
                    str(item.get("id", "") or ""),
                ),
            )
            primary_row = next((item for item in rows if str(item.get("id", "") or "") == base_task_id), rows[0])
            workflow = automation_workflow_definition(base_task_id)
            override = task_overrides.get(base_task_id, {})
            tool_params = primary_row.get("tool_params", {})
            default_tool_params = workflow.get("default_tool_params")
            if isinstance(default_tool_params, dict) and isinstance(tool_params, dict):
                merged_tool_params = json.loads(json.dumps(default_tool_params, ensure_ascii=False))
                merged_tool_params.update(tool_params)
                tool_params = merged_tool_params
            raw_json = override.get("tool_params_json")
            if raw_json is None:
                raw_json = json.dumps(tool_params, ensure_ascii=False, indent=2)

            override_schedule_times_raw = str(override.get("schedule_times_json", "") or "").strip()
            override_schedule_times: list[str] = []
            if override_schedule_times_raw:
                try:
                    parsed_schedule_times = json.loads(override_schedule_times_raw)
                except json.JSONDecodeError:
                    parsed_schedule_times = []
                if isinstance(parsed_schedule_times, list):
                    override_schedule_times = normalize_schedule_times(
                        [str(item or "").strip() for item in parsed_schedule_times]
                    )

            cron_values = [str(item.get("cron_expression", "") or "").strip() for item in rows]
            if override.get("cron_expression"):
                cron_values = [str(override.get("cron_expression", "") or "").strip()]
            if override_schedule_times:
                cron_values = [build_daily_cron_expression(item) or "" for item in override_schedule_times]

            schedule_info = summarize_task_group_schedule(cron_values)
            last_run_value = max((str(item.get("last_run") or "") for item in rows), default="")
            created_at_value = max((str(item.get("created_at") or "") for item in rows), default="")
            last_activity = last_run_value or created_at_value
            last_status = next(
                (
                    str(item.get("last_status", "") or "")
                    for item in rows
                    if str(item.get("last_run") or "") == last_run_value and last_run_value
                ),
                str(primary_row.get("last_status", "") or ""),
            )
            last_duration_ms = next(
                (
                    item.get("last_duration_ms")
                    for item in rows
                    if str(item.get("last_run") or "") == last_run_value and last_run_value
                ),
                primary_row.get("last_duration_ms"),
            )
            last_message_value = next(
                (
                    str(item.get("last_message", "") or "")
                    for item in rows
                    if str(item.get("last_run") or "") == last_run_value and last_run_value
                ),
                str(primary_row.get("last_message", "") or ""),
            )
            enabled_value = override.get("enabled")
            if enabled_value is None:
                enabled_value = any(bool(item.get("enabled")) for item in rows)

            name_value = str(
                override.get("name")
                or workflow.get("display_name")
                or normalize_task_display_name(str(primary_row.get("name", "") or ""))
            )
            tool_name_value = str(override.get("tool_name") or primary_row.get("tool_name", "") or "")
            provider_value = automation_task_provider(base_task_id, workflow, tool_name_value)
            resource_bindings = build_automation_resource_bindings(
                base_task_id,
                workflow_resources,
                resource_overrides=resource_overrides,
            )
            missing_required_resources = [
                item["resource_key"]
                for item in resource_bindings
                if item.get("missing") and item.get("required")
            ]
            missing_required_resource_labels = [
                item.get("display_name") or item["resource_key"]
                for item in resource_bindings
                if item.get("missing") and item.get("required")
            ]
            tasks_by_id[base_task_id] = {
                **primary_row,
                "note": str(workflow.get("note") or automation_task_note(base_task_id)),
                "provider": provider_value,
                "provider_label": automation_provider_label(provider_value),
                "system_badges": list(workflow.get("system_badges") or []),
                "task_id": base_task_id,
                "task_mode": "scheduled",
                "display_task_id": str(primary_row.get("id", "") or base_task_id) if len(rows) == 1 else base_task_id,
                "group_size": len(rows),
                "name_value": name_value,
                "tool_name_value": tool_name_value,
                "cron_expression_value": schedule_info["raw_value"],
                "enabled_value": bool(enabled_value),
                "tool_params_json": raw_json,
                "tool_param_fields": flatten_automation_fields(tool_params),
                "schedule_supported": bool(schedule_info["supported"] or not schedule_info["raw_value"]),
                "schedule_editable": True,
                "schedule_time_values": schedule_info["time_values"],
                "schedule_summary": schedule_info["summary"],
                "schedule_icon": "clock",
                "schedule_warning": schedule_info["warning"],
                "trigger_label": str(workflow.get("trigger_label") or "定时任务"),
                "is_schedulable": True,
                "has_webhook": False,
                "webhook_path": "",
                "webhook_full_url": "",
                "webhook_masked_url": "",
                "webhook_token_enabled": False,
                "webhook_header_name": webhook_token_header_name(),
                "webhook_body_json": "{}",
                "resource_bindings": resource_bindings,
                "missing_required_resources": missing_required_resources,
                "missing_required_resource_labels": missing_required_resource_labels,
                "resource_blocked": bool(missing_required_resources),
                "last_status_value": last_status,
                "last_run_value": last_run_value,
                "last_activity_value": last_activity,
                "last_duration_label": format_duration_label(last_duration_ms),
                "last_message_value": last_message_value,
                "last_error_summary": shorten_error_message(last_message_value) if last_status == "error" else "",
                "search_text": " ".join(
                    item.lower()
                    for item in (
                        name_value,
                        str(workflow.get("note") or automation_task_note(base_task_id)),
                        tool_name_value,
                        base_task_id,
                        automation_provider_label(provider_value),
                    )
                    if item
                ),
                "sort_order": int(workflow.get("order") or 999),
                "is_open": bool(
                    open_task_id == base_task_id
                    or task_feedbacks.get(base_task_id)
                ),
                "feedback": task_feedbacks.get(base_task_id),
            }

        for workflow in AUTOMATION_WORKFLOW_CATALOG:
            workflow_task_id = str(workflow.get("task_id", "") or "")
            if not workflow_task_id or workflow_task_id in tasks_by_id:
                continue
            tasks_by_id[workflow_task_id] = self._build_virtual_automation_task(
                workflow_task_id,
                override=task_overrides.get(workflow_task_id),
                feedback=task_feedbacks.get(workflow_task_id),
                open_task_id=open_task_id,
                workflow_resources=workflow_resources,
                resource_overrides=resource_overrides,
            )

        tasks = sorted(
            tasks_by_id.values(),
            key=lambda item: (
                int(item.get("sort_order") or 999),
                str(item.get("name_value") or ""),
            ),
        )
        automation_accounts, automation_account_warning = self._fetch_automation_accounts(
            force=False,
            prefer_cached=True,
        )
        self._enrich_automation_tasks_with_accounts(tasks, automation_accounts)
        automation_provider_counts = {
            provider: sum(1 for row in tasks if str(row.get("provider") or "ronghui") == provider)
            for provider in AUTOMATION_PROVIDER_LABELS
        }
        automation_provider_enabled_counts = {
            provider: sum(
                1
                for row in tasks
                if str(row.get("provider") or "ronghui") == provider and row.get("enabled_value")
            )
            for provider in AUTOMATION_PROVIDER_LABELS
        }

        template = self.template_env.get_template("automation.html")
        body = template.render(
            app_title=self.settings.app_title,
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
            settings=self.settings,
            scheduled_tasks=tasks,
            scheduled_task_count=len(tasks),
            enabled_task_count=sum(1 for row in tasks if row.get("enabled_value")),
            automation_provider_labels=AUTOMATION_PROVIDER_LABELS,
            automation_provider_counts=automation_provider_counts,
            automation_provider_enabled_counts=automation_provider_enabled_counts,
            automation_db_warning=automation_db_warning,
            automation_account_warning=automation_account_warning,
            tms_session_status=self._fetch_tms_session_status(),
            tms_session_credentials=self._fetch_tms_session_credentials(),
        )
        self._send_html(handler, body)

    def _handle_automation_resource_save(self, handler: BaseHTTPRequestHandler) -> None:
        values = self._parse_urlencoded_form(handler)
        ajax_request = self._is_ajax_request(handler)
        resources_json = str(values.get("resources_json", "") or "").strip()
        resource_key = str(values.get("resource_key", "") or "").strip()
        config_json = str(values.get("config_json", "") or "").strip()
        open_task_id = str(values.get("task_id", "") or "").strip()

        if resources_json:
            try:
                resource_payload = json.loads(resources_json)
            except json.JSONDecodeError as exc:
                message = f"批量资源 JSON 解析失败：{exc.msg}"
                if ajax_request:
                    self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": message})
                    return
                self._redirect_with_message(handler, "/automations", message, "warning")
                return

            if not isinstance(resource_payload, dict):
                message = "批量资源必须是 JSON 对象。"
                if ajax_request:
                    self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": message})
                    return
                self._redirect_with_message(handler, "/automations", message, "warning")
                return

            parsed_resources: dict[str, dict[str, Any]] = {}
            resource_overrides: dict[str, dict[str, Any]] = {}
            errors: list[str] = []
            for raw_key, raw_json in resource_payload.items():
                key = str(raw_key or "").strip()
                config_text = str(raw_json or "").strip()
                if not key or not config_text:
                    continue
                resource_overrides[key] = {"config_json": config_text}
                try:
                    parsed_config = json.loads(config_text)
                except json.JSONDecodeError as exc:
                    errors.append(f"{key}: JSON 解析失败：{exc.msg}")
                    continue
                if not isinstance(parsed_config, dict):
                    errors.append(f"{key}: 必须是 JSON 对象")
                    continue
                parsed_resources[key] = parsed_config

            if errors:
                message = "；".join(errors)
                if ajax_request:
                    self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": message})
                    return
                self._render_automations(
                    handler,
                    {"message": [message], "kind": ["warning"]},
                    resource_overrides=resource_overrides,
                    open_task_id=open_task_id or None,
                )
                return

            if not parsed_resources:
                message = "没有需要保存的资源。"
                if ajax_request:
                    self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": message})
                    return
                self._redirect_with_message(handler, "/automations", message, "warning")
                return

            for key, config in parsed_resources.items():
                self.repository.upsert_workflow_resource(key, config, source="backend_console")

            message = f"已保存 {len(parsed_resources)} 个资源。"
            if ajax_request:
                self._send_json(
                    handler,
                    HTTPStatus.OK,
                    {"ok": True, "message": message, "saved": sorted(parsed_resources)},
                )
                return
            self._redirect_with_message(handler, "/automations", message, "success")
            return

        if not resource_key:
            self._redirect_with_message(handler, "/automations", "缺少资源标识。", "warning")
            return

        try:
            config = json.loads(config_json or "{}")
        except json.JSONDecodeError as exc:
            self._render_automations(
                handler,
                {"message": [f"资源 {resource_key} 的 JSON 解析失败：{exc.msg}"], "kind": ["warning"]},
                resource_overrides={resource_key: {"config_json": config_json}},
                open_task_id=open_task_id or None,
            )
            return

        if not isinstance(config, dict):
            self._render_automations(
                handler,
                {"message": [f"资源 {resource_key} 必须是 JSON 对象。"], "kind": ["warning"]},
                resource_overrides={resource_key: {"config_json": config_json}},
                open_task_id=open_task_id or None,
            )
            return

        self.repository.upsert_workflow_resource(resource_key, config, source="backend_console")
        self._redirect_with_message(handler, "/automations", f"资源已保存：{resource_key}", "success")

    def _handle_automation_task_save(self, handler: BaseHTTPRequestHandler) -> None:
        ajax_request = self._is_ajax_request(handler)
        payload, override, error_message = self._collect_automation_task_submission(handler)
        if not payload:
            task_id = next(iter(override.keys()), "")
            if ajax_request:
                self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": error_message})
                return
            self._render_automations(
                handler,
                {"message": [error_message], "kind": ["warning"]},
                task_overrides=override,
                open_task_id=task_id,
            )
            return

        reload_result = self._persist_automation_task(payload)
        save_message = str(payload.get("save_message") or "").strip()
        success_message = f"已保存：{payload['name']}"
        if save_message:
            success_message = f"{success_message}；{save_message}"

        if ajax_request:
            ok = reload_result.get("ok", True) if payload.get("task_mode") == "scheduled" else True
            self._send_json(handler, HTTPStatus.OK, {
                "ok": ok,
                "task_id": payload.get("task_id", ""),
                "message": success_message,
            })
            return

        if payload.get("task_mode") != "scheduled":
            self._redirect_with_message(handler, "/automations", success_message.replace("已保存", "任务配置已保存", 1), "success")
            return
        if not reload_result.get("ok"):
            self._redirect_with_message(
                handler,
                "/automations",
                f"任务已保存。Agent 当前未连接，稍后重载后生效：{reload_result.get('error', 'unknown error')}",
                "warning",
            )
            return

        self._redirect_with_message(handler, "/automations", success_message.replace("已保存", "任务已保存", 1), "success")

    def _handle_automation_task_run_now(self, handler: BaseHTTPRequestHandler) -> None:
        ajax_request = self._is_ajax_request(handler)
        payload, override, error_message = self._collect_automation_task_submission(
            handler,
            allow_missing_schedule=True,
        )
        if not payload:
            task_id = next(iter(override.keys()), "")
            if ajax_request:
                self._send_json(
                    handler,
                    HTTPStatus.BAD_REQUEST,
                    {
                        "ok": False,
                        "task_id": task_id,
                        "title": "执行未开始",
                        "message": error_message,
                    },
                )
                return
            self._render_automations(
                handler,
                {"message": [error_message], "kind": ["warning"]},
                task_overrides=override,
                open_task_id=task_id,
            )
            return

        try:
            workflow_resource_rows = self.repository.list_workflow_resources()
        except Exception:
            workflow_resource_rows = []
        workflow_resources = {
            str(item.get("resource_key", "") or ""): item
            for item in workflow_resource_rows
        }
        resource_bindings = build_automation_resource_bindings(payload["task_id"], workflow_resources)
        missing_required_resources = [
            item.get("display_name") or item["resource_key"]
            for item in resource_bindings
            if item.get("missing") and item.get("required")
        ]
        if missing_required_resources:
            message = (
                "缺少运行资源："
                + "、".join(missing_required_resources)
                + "。请先在当前任务设置中补齐资源配置。"
            )
            if ajax_request:
                self._send_json(
                    handler,
                    HTTPStatus.BAD_REQUEST,
                    {
                        "ok": False,
                        "task_id": payload["task_id"],
                        "title": "执行未开始",
                        "message": message,
                    },
                )
                return
            self._render_automations(
                handler,
                {"message": [message], "kind": ["warning"]},
                task_overrides=override,
                open_task_id=payload["task_id"],
            )
            return

        started_at = time.perf_counter()
        started_stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        reload_result = self._persist_automation_task(payload)
        if not reload_result.get("ok"):
            failure_message = normalize_feedback_text(reload_result.get("error", "unknown error"))
            duration_ms = int(round((time.perf_counter() - started_at) * 1000))
            if payload.get("task_mode") == "scheduled":
                self.repository.update_scheduled_task_runtime(
                    base_task_id=payload["task_id"],
                    last_run=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    last_status="error",
                    last_duration_ms=duration_ms,
                    last_message=failure_message,
                )
            else:
                self._record_virtual_task_runtime(
                    payload["task_id"],
                    payload=payload,
                    last_run=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    last_status="error",
                    last_duration_ms=duration_ms,
                    last_message=failure_message,
                )
            response_payload = {
                "ok": False,
                "task_id": payload["task_id"],
                "title": "立即执行未开始",
                "message": failure_message,
                "status_label": "执行失败",
                "activity_label": "最近运行",
                "activity_value": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "duration_label": format_duration_label(duration_ms),
                "error": failure_message,
            }
            if ajax_request:
                self._send_json(handler, HTTPStatus.OK, response_payload)
                return
            self._render_automations(
                handler,
                {"message": [f"任务已保存，但 Agent 当前不可达，无法立即执行：{failure_message}"], "kind": ["warning"]},
                task_overrides=override,
                task_feedbacks={
                    payload["task_id"]: {
                        "kind": "error",
                        "title": "立即执行未开始",
                        "message": failure_message,
                    }
                },
                open_task_id=payload["task_id"],
            )
            return

        self._start_automation_task_run(payload, started_at=started_at)
        response_payload = {
            "ok": False,
            "pending": True,
            "task_id": payload["task_id"],
            "title": "执行中",
            "message": "脚本已开始执行，结果会自动更新。",
            "status_label": "后台执行中",
            "activity_label": "开始时间",
            "activity_value": started_stamp,
            "duration_label": format_duration_label(0),
            "error": "",
        }
        if ajax_request:
            self._send_json(handler, HTTPStatus.OK, response_payload)
            return
        self._render_automations(
            handler,
            {"message": [f"任务已开始执行：{payload['name']}"], "kind": ["success"]},
            task_overrides=override,
            task_feedbacks={
                payload["task_id"]: {
                    "kind": "info",
                    "title": response_payload["title"],
                    "message": response_payload["message"],
                }
            },
            open_task_id=payload["task_id"],
        )

    def _handle_automation_task_cancel(self, handler: BaseHTTPRequestHandler) -> None:
        values = self._parse_urlencoded_form(handler)
        task_id = str(values.get("task_id", "") or "").strip()
        tool_name = str(values.get("tool_name", "") or "").strip()
        started_at = str(values.get("started_at", "") or "").strip()

        if not task_id or not tool_name:
            self._send_json(
                handler,
                HTTPStatus.BAD_REQUEST,
                {
                    "ok": False,
                    "task_id": task_id,
                    "message": "缺少任务标识，无法取消执行。",
                },
            )
            return

        result = self._agent_request(
            "POST",
            "/cancel-tool",
            payload={"tool_name": tool_name, "started_at": started_at},
            timeout=10,
        )
        if not result.get("ok"):
            self._send_json(
                handler,
                HTTPStatus.OK,
                {
                    "ok": False,
                    "task_id": task_id,
                    "message": normalize_feedback_text(result.get("error") or "取消请求失败"),
                },
            )
            return

        payload = result.get("data")
        if not isinstance(payload, dict):
            payload = {}

        if payload.get("ok"):
            self._send_json(
                handler,
                HTTPStatus.OK,
                {
                    "ok": True,
                    "task_id": task_id,
                    "title": "取消中",
                    "message": str(payload.get("message") or "已发送取消请求，正在停止脚本。"),
                    "activity_value": str(payload.get("started_at") or started_at),
                    "pending": True,
                    "cancel_requested": True,
                },
            )
            return

        runtime = self._sync_task_runtime_from_latest_tool_log(
            task_id,
            tool_name,
            since=started_at or None,
        )
        response_payload: dict[str, Any] = {
            "ok": False,
            "task_id": task_id,
            "message": str(payload.get("message") or "当前没有运行中的任务。"),
        }
        if runtime:
            response_payload["runtime"] = runtime
        self._send_json(handler, HTTPStatus.OK, response_payload)

    def _handle_automation_task_output(self, handler: BaseHTTPRequestHandler, query: dict) -> None:
        """代理 Agent 的实时工具输出接口"""
        tool_name = str(query.get("tool_name", [""])[0]).strip()
        task_id = str(query.get("task_id", [""])[0]).strip()
        started_at = str(query.get("started_at", [""])[0]).strip()
        try:
            offset = int(query.get("offset", ["0"])[0])
        except (ValueError, IndexError):
            offset = 0
        if not tool_name:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "missing tool_name"})
            return
        query_string = f"/tool-output/{tool_name}?offset={offset}"
        if started_at:
            query_string += f"&started_at={quote(started_at, safe='')}"
        result = self._agent_request("GET", query_string, timeout=5)
        if result.get("ok"):
            payload = dict(result["data"])
            if task_id:
                runtime = self._runtime_from_local_task_state(task_id, since=started_at or None)
                if runtime and payload.get("running"):
                    payload["running"] = False
                if not runtime and not payload.get("running"):
                    runtime = self._sync_task_runtime_from_output_payload(task_id, payload)
                    if not runtime:
                        runtime = self._sync_task_runtime_from_latest_tool_log(
                            task_id,
                            tool_name,
                            since=started_at or None,
                        )
                if runtime:
                    payload["runtime"] = runtime
            self._send_json(handler, HTTPStatus.OK, payload)
        else:
            payload = {"lines": [], "running": False, "offset": 0, "total": 0}
            if task_id:
                runtime = self._runtime_from_local_task_state(task_id, since=started_at or None)
                if not runtime:
                    runtime = self._sync_task_runtime_from_latest_tool_log(
                        task_id,
                        tool_name,
                        since=started_at or None,
                    )
                if runtime:
                    payload["runtime"] = runtime
            self._send_json(handler, HTTPStatus.OK, payload)

    def _merge_submitted_account_roles(
        self,
        *,
        task_id: str,
        tool_name: str,
        tool_params: dict[str, Any],
        values: dict[str, Any],
    ) -> dict[str, Any]:
        merged = dict(tool_params or {})
        workflow = automation_workflow_definition(task_id)
        for role in self._automation_task_account_roles(task_id, workflow, tool_name):
            field = str(role.get("field") or "account_id").strip() or "account_id"
            form_key = f"account_role__{field}"
            if form_key not in values:
                continue
            raw_value = values.get(form_key)
            if isinstance(raw_value, list):
                raw_value = raw_value[-1] if raw_value else ""
            selected_account_id = str(raw_value or "").strip()
            if selected_account_id:
                merged[field] = selected_account_id
            else:
                merged.pop(field, None)
        return merged

    def _collect_automation_task_submission(
        self,
        handler: BaseHTTPRequestHandler,
        *,
        allow_missing_schedule: bool = False,
    ) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]], str]:
        values = self._parse_urlencoded_form(handler)
        task_id = str(values.get("task_id", "") or "").strip()
        name = str(values.get("name", "") or "").strip()
        tool_name = str(values.get("tool_name", "") or "").strip()
        task_mode = str(values.get("task_mode", "") or "scheduled").strip().lower() or "scheduled"
        cron_expression = str(values.get("cron_expression", "") or values.get("cron_expression_raw", "") or "").strip()
        tool_params_json = str(values.get("tool_params_json", "") or "").strip()
        enabled = str(values.get("enabled", "") or "").strip().lower() in {"1", "on", "true", "yes"}
        schedule_times_json = str(values.get("schedule_times_json", "") or "").strip()

        override = {
            task_id: {
                "name": name,
                "tool_name": tool_name,
                "task_mode": task_mode,
                "cron_expression": cron_expression,
                "tool_params_json": tool_params_json,
                "schedule_times_json": schedule_times_json,
                "enabled": enabled,
            }
        }

        if not task_id or not name or not tool_name:
            return None, override, "任务 ID、任务名称和工具名称不能为空。"

        try:
            tool_params = json.loads(tool_params_json or "{}")
        except json.JSONDecodeError as exc:
            return None, override, f"任务 {task_id} 的参数 JSON 解析失败：{exc.msg}"

        if not isinstance(tool_params, dict):
            return None, override, f"任务 {task_id} 的参数必须是 JSON 对象。"

        tool_params = self._merge_submitted_account_roles(
            task_id=task_id,
            tool_name=tool_name,
            tool_params=tool_params,
            values=values,
        )
        tool_params_json = json.dumps(tool_params, ensure_ascii=False, indent=2)
        override[task_id]["tool_params_json"] = tool_params_json

        if task_mode != "scheduled":
            return (
                {
                    "task_id": task_id,
                    "name": name,
                    "tool_name": tool_name,
                    "task_mode": task_mode,
                    "cron_expression": "",
                    "cron_expressions": [],
                    "tool_params_json": tool_params_json,
                    "tool_params": tool_params,
                    "schedule_times": [],
                    "schedule_times_json": "[]",
                    "enabled": False,
                },
                override,
                "",
            )

        schedule_times: list[str] = []
        if schedule_times_json:
            try:
                parsed_schedule_times = json.loads(schedule_times_json)
            except json.JSONDecodeError as exc:
                return None, override, f"任务 {task_id} 的执行时间 JSON 解析失败：{exc.msg}"
            if not isinstance(parsed_schedule_times, list):
                return None, override, f"任务 {task_id} 的执行时间必须是数组。"
            schedule_times = normalize_schedule_times(
                [str(item or "").strip() for item in parsed_schedule_times]
            )

        cron_expressions: list[str] = []
        if schedule_times:
            cron_expressions = [build_daily_cron_expression(item) or "" for item in schedule_times]
        elif cron_expression:
            cron_expressions = [
                item.strip()
                for item in str(cron_expression).splitlines()
                if item.strip()
            ]

        cron_expressions = [item for item in cron_expressions if item]
        if not cron_expressions:
            if not allow_missing_schedule:
                return None, override, "请至少设置一个执行时间"
            return (
                {
                    "task_id": task_id,
                    "name": name,
                    "tool_name": tool_name,
                    "task_mode": "manual",
                    "cron_expression": "",
                    "cron_expressions": [],
                    "tool_params_json": tool_params_json,
                    "tool_params": tool_params,
                    "schedule_times": [],
                    "schedule_times_json": "[]",
                    "enabled": False,
                },
                override,
                "",
            )

        return (
            {
                "task_id": task_id,
                "name": name,
                "tool_name": tool_name,
                "task_mode": task_mode,
                "cron_expression": cron_expressions[0],
                "cron_expressions": cron_expressions,
                "tool_params_json": tool_params_json,
                "tool_params": tool_params,
                "schedule_times": schedule_times,
                "schedule_times_json": json.dumps(schedule_times, ensure_ascii=False),
                "enabled": enabled,
            },
            override,
            "",
        )

    def _persist_automation_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        if str(payload.get("task_mode") or "scheduled") != "scheduled":
            self._record_virtual_task_runtime(payload["task_id"], payload=payload)
            return {"ok": True, "status": None, "data": {"mode": "virtual"}}

        task_group = []
        schedule_times = list(payload.get("schedule_times") or [])
        cron_expressions = list(payload.get("cron_expressions") or [])
        if not cron_expressions:
            task_group.append(
                {
                    "task_id": payload["task_id"],
                    "name": payload["name"],
                    "tool_name": payload["tool_name"],
                    "tool_params": payload["tool_params"],
                    "cron_expression": "",
                    "enabled": False,
                }
            )
        for index, cron_expression in enumerate(cron_expressions):
            schedule_time = schedule_times[index] if index < len(schedule_times) else ""
            task_group.append(
                {
                    "task_id": build_task_schedule_id(
                        payload["task_id"],
                        cron_expression=cron_expression,
                        schedule_time=schedule_time,
                        position=index,
                    ),
                    "name": payload["name"],
                    "tool_name": payload["tool_name"],
                    "tool_params": payload["tool_params"],
                    "cron_expression": cron_expression,
                    "enabled": payload["enabled"],
                }
            )
        self.repository.replace_scheduled_task_group(
            base_task_id=payload["task_id"],
            tasks=task_group,
        )
        return self._agent_request("POST", "/admin/reload", payload={})

    def _handle_automation_admin_action(
        self,
        handler: BaseHTTPRequestHandler,
        *,
        endpoint: str,
        success_message: str,
    ) -> None:
        result = self._agent_request("POST", endpoint, payload={})
        if result.get("ok"):
            self._redirect_with_message(handler, "/automations", success_message, "success")
            return
        self._redirect_with_message(
            handler,
            "/automations",
            f"Agent call failed: {result.get('error', 'unknown error')}",
            "warning",
        )

    def _automation_session_config(self, profile: Any) -> dict[str, str]:
        normalized = normalize_automation_session_profile(profile)
        return dict(AUTOMATION_SESSION_PROFILES[normalized])

    def _automation_session_route(self, path: str) -> tuple[str, str] | None:
        normalized_path = str(path or "").rstrip("/") or "/"
        for profile, config in AUTOMATION_SESSION_PROFILES.items():
            prefix = str(config["console_prefix"]).rstrip("/")
            if normalized_path == prefix or normalized_path.startswith(prefix + "/"):
                suffix = normalized_path[len(prefix) :] or "/"
                return profile, suffix
        return None

    def _automation_session_agent_endpoint(self, profile: Any, suffix: str) -> str:
        config = self._automation_session_config(profile)
        return f"{config['agent_prefix'].rstrip('/')}/{str(suffix or '').strip('/')}"

    def _automation_session_actions(self, profile: Any) -> dict[str, str]:
        config = self._automation_session_config(profile)
        prefix = config["console_prefix"].rstrip("/")
        return {
            "status": f"{prefix}/status",
            "send_code": f"{prefix}/send-code",
            "save_credentials": f"{prefix}/save-credentials",
            "clear_credentials": f"{prefix}/clear-credentials",
            "submit_code": f"{prefix}/submit-code",
            "clear": f"{prefix}/clear",
        }

    def _automation_session_context(self, profile: Any, *, force: bool = True) -> dict[str, Any]:
        normalized = normalize_automation_session_profile(profile)
        config = self._automation_session_config(normalized)
        state = self._fetch_tms_session_status(normalized, force=force)
        credentials = self._fetch_tms_session_credentials(normalized)
        state.setdefault("profile", normalized)
        state.update(
            {
                "profile_label": config["label"],
                "dot_label": config["dot_label"],
                "login_kind": config["login_kind"],
            }
        )
        return {
            "ok": True,
            "profile": normalized,
            "profile_label": config["label"],
            "dot_label": config["dot_label"],
            "login_kind": config["login_kind"],
            "actions": self._automation_session_actions(normalized),
            "state": state,
            "credentials": credentials,
        }

    def _handle_automation_session_context(
        self,
        handler: BaseHTTPRequestHandler,
        query: dict,
    ) -> None:
        profile = query.get("profile", ["ronghui"])[0]
        force = self._query_bool(query, "force", True)
        self._send_json(handler, HTTPStatus.OK, self._automation_session_context(profile, force=force))

    def _handle_automation_session_post(
        self,
        handler: BaseHTTPRequestHandler,
        profile: str,
        suffix: str,
    ) -> bool:
        normalized = normalize_automation_session_profile(profile)
        config = self._automation_session_config(normalized)
        label = config["label"]
        image_login = config.get("login_kind") == "image"
        if suffix == "/send-code":
            if image_login:
                self._handle_tms_session_action(
                    handler,
                    profile=normalized,
                    endpoint=self._automation_session_agent_endpoint(normalized, "send-code"),
                    payload={},
                    success_message=f"{label}正在自动识别图片验证码并登录；如自动识别失败，将保留人工输入入口。",
                    timeout=90,
                )
                return True
            self._handle_tms_session_action(
                handler,
                profile=normalized,
                endpoint=self._automation_session_agent_endpoint(normalized, "send-code"),
                payload={},
                success_message=(
                    f"{label}登录已提交；如出现图片验证码，请按图输入后提交。"
                    if image_login
                    else f"{label} 验证码已发送，请在顶部模块输入短信验证码。"
                ),
                timeout=90,
            )
            return True
        if suffix == "/save-credentials":
            values = self._parse_urlencoded_form(handler)
            self._handle_tms_session_action(
                handler,
                profile=normalized,
                endpoint=self._automation_session_agent_endpoint(normalized, "credentials"),
                payload={
                    "username": str(values.get("username", "") or "").strip(),
                    "password": str(values.get("password", "") or ""),
                    "phone": str(values.get("phone", "") or "").strip(),
                },
                success_message=f"{label} 登录配置已保存。",
                timeout=20,
            )
            return True
        if suffix == "/clear-credentials":
            self._handle_tms_session_action(
                handler,
                profile=normalized,
                endpoint=self._automation_session_agent_endpoint(normalized, "credentials/clear"),
                payload={},
                success_message=f"{label} 登录配置已清空。",
                timeout=20,
            )
            return True
        if suffix == "/submit-code":
            values = self._parse_urlencoded_form(handler)
            sms_code = str(values.get("code", "") or "").strip()
            if not sms_code:
                self._respond_tms_action(
                    handler,
                    profile=normalized,
                    ok=False,
                    message=f"{label} {'图片验证码' if image_login else '验证码'}不能为空。",
                    kind="warning",
                    http_status=HTTPStatus.BAD_REQUEST,
                )
                return True
            self._handle_tms_session_action(
                handler,
                profile=normalized,
                endpoint=self._automation_session_agent_endpoint(normalized, "submit-code"),
                payload={"code": sms_code},
                success_message=f"{label} 登录成功，登录态已更新。",
                timeout=45,
            )
            return True
        if suffix == "/clear":
            self._handle_tms_session_action(
                handler,
                profile=normalized,
                endpoint=self._automation_session_agent_endpoint(normalized, "clear"),
                payload={},
                success_message=f"{label} 登录态已清除。",
                timeout=20,
            )
            return True
        return False

    def _default_tms_session_status(
        self,
        *,
        status: str = "error",
        label: str = "异常",
        last_error_summary: str = "",
        agent_ok: bool = False,
    ) -> dict[str, Any]:
        tone_map = {
            "authenticated": "success",
            "pending_code": "warning",
            "logged_out": "neutral",
            "expired": "error",
            "error": "error",
        }
        safe_status = status if status in tone_map else "error"
        return {
            "status": safe_status,
            "label": label,
            "status_tone": tone_map[safe_status],
            "authenticated": safe_status == "authenticated",
            "pending_code": safe_status == "pending_code",
            "last_validation_at": "",
            "last_error_summary": last_error_summary,
            "authenticated_at": "",
            "pending_since": "",
            "expires_at": "",
            "has_saved_credentials": False,
            "has_manual_credentials": False,
            "has_env_credentials": False,
            "credential_source": "",
            "challenge_type": "",
            "challenge_label": "",
            "captcha_image": "",
            "captcha_image_mime": "",
            "captcha_captured_at": "",
            "agent_ok": agent_ok,
        }

    def _normalize_tms_session_status_payload(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return self._default_tms_session_status(last_error_summary="TMS 登录态接口返回了无效数据。")

        if payload.get("ok") is False:
            return self._default_tms_session_status(
                last_error_summary=normalize_feedback_text(
                    payload.get("message") or payload.get("error") or "TMS 登录态接口调用失败。"
                ),
            )

        status = str(payload.get("status") or "").strip() or "error"
        label = str(payload.get("label") or "").strip()
        default_labels = {
            "authenticated": "已登录",
            "pending_code": "待输入验证码",
            "logged_out": "未登录",
            "expired": "已过期",
            "error": "异常",
        }
        normalized = self._default_tms_session_status(
            status=status,
            label=label or default_labels.get(status, "异常"),
            last_error_summary=str(payload.get("last_error_summary") or "").strip(),
            agent_ok=True,
        )
        normalized.update(
            {
                "last_validation_at": str(payload.get("last_validation_at") or "").strip(),
                "authenticated_at": str(payload.get("authenticated_at") or "").strip(),
                "pending_since": str(payload.get("pending_since") or "").strip(),
                "expires_at": str(payload.get("expires_at") or "").strip(),
                "has_saved_credentials": bool(payload.get("has_saved_credentials")),
                "has_manual_credentials": bool(payload.get("has_manual_credentials")),
                "has_env_credentials": bool(payload.get("has_env_credentials")),
                "credential_source": str(payload.get("credential_source") or "").strip(),
                "challenge_type": str(payload.get("challenge_type") or "").strip(),
                "challenge_label": str(payload.get("challenge_label") or "").strip(),
                "captcha_image": str(payload.get("captcha_image") or "").strip(),
                "captcha_image_mime": str(payload.get("captcha_image_mime") or "").strip(),
                "captcha_captured_at": str(payload.get("captcha_captured_at") or "").strip(),
                "profile": str(payload.get("profile") or "").strip(),
                "account_id": str(payload.get("account_id") or "").strip(),
                "account_name": str(payload.get("account_name") or "").strip(),
                "system": str(payload.get("system") or "").strip(),
                "system_label": str(payload.get("system_label") or "").strip(),
                "login_kind": str(payload.get("login_kind") or "").strip(),
            }
        )
        return normalized

    def _fetch_tms_session_status(self, profile: Any = "ronghui", *, force: bool = True) -> dict[str, Any]:
        normalized = normalize_automation_session_profile(profile)
        label = self._automation_session_config(normalized)["label"]
        endpoint = self._automation_session_agent_endpoint(normalized, "status")
        if force:
            endpoint = f"{endpoint}?force=1"
        result = self._agent_request("GET", endpoint, timeout=35 if force else 12)
        if not result.get("ok"):
            return self._default_tms_session_status(
                last_error_summary=normalize_feedback_text(
                    result.get("error") or f"Agent 当前不可达，无法获取{label}登录态。"
                ),
            )
        return self._normalize_tms_session_status_payload(result.get("data"))

    def _default_tms_session_credentials(self) -> dict[str, Any]:
        return {
            "username": "",
            "password": "",
            "phone": "",
            "updated_at": "",
            "has_saved_credentials": False,
            "has_manual_credentials": False,
            "has_env_credentials": False,
            "credential_source": "",
        }

    def _normalize_tms_session_credentials_payload(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return self._default_tms_session_credentials()
        if payload.get("ok") is False:
            return self._default_tms_session_credentials()
        return {
            "username": str(payload.get("username") or ""),
            "password": "",
            "phone": str(payload.get("phone") or ""),
            "updated_at": str(payload.get("updated_at") or ""),
            "has_saved_credentials": bool(payload.get("has_saved_credentials")),
            "has_manual_credentials": bool(payload.get("has_manual_credentials")),
            "has_env_credentials": bool(payload.get("has_env_credentials")),
            "credential_source": str(payload.get("credential_source") or "").strip(),
        }

    def _fetch_tms_session_credentials(self, profile: Any = "ronghui") -> dict[str, Any]:
        normalized = normalize_automation_session_profile(profile)
        result = self._agent_request("GET", self._automation_session_agent_endpoint(normalized, "credentials"), timeout=8)
        if not result.get("ok"):
            return self._default_tms_session_credentials()
        return self._normalize_tms_session_credentials_payload(result.get("data"))

    def _handle_tms_session_status(
        self,
        handler: BaseHTTPRequestHandler,
        profile: Any = "ronghui",
        query: dict | None = None,
    ) -> None:
        force = self._query_bool(query, "force", True)
        self._send_json(handler, HTTPStatus.OK, self._fetch_tms_session_status(profile, force=force))

    def _wants_json_response(self, handler: BaseHTTPRequestHandler) -> bool:
        accept = str(handler.headers.get("Accept") or "").lower()
        requested_with = str(handler.headers.get("X-Requested-With") or "").lower()
        return "application/json" in accept or requested_with == "fetch"

    def _respond_tms_action(
        self,
        handler: BaseHTTPRequestHandler,
        *,
        profile: Any = "ronghui",
        ok: bool,
        message: str,
        kind: str,
        http_status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        normalized = normalize_automation_session_profile(profile)
        config = self._automation_session_config(normalized)
        if self._wants_json_response(handler):
            state = self._fetch_tms_session_status(normalized)
            state.update(
                {
                    "profile": normalized,
                    "profile_label": config["label"],
                    "dot_label": config["dot_label"],
                    "login_kind": config["login_kind"],
                }
            )
            self._send_json(
                handler,
                http_status,
                {
                    "ok": ok,
                    "message": message,
                    "kind": kind,
                    "profile": normalized,
                    "profile_label": config["label"],
                    "dot_label": config["dot_label"],
                    "login_kind": config["login_kind"],
                    "actions": self._automation_session_actions(normalized),
                    "state": state,
                    "credentials": self._fetch_tms_session_credentials(normalized),
                },
            )
            return
        self._redirect_with_message(handler, "/automations", message, kind)

    def _handle_tms_session_action(
        self,
        handler: BaseHTTPRequestHandler,
        *,
        profile: Any = "ronghui",
        endpoint: str,
        payload: dict[str, Any],
        success_message: str,
        timeout: int,
    ) -> None:
        normalized = normalize_automation_session_profile(profile)
        label = self._automation_session_config(normalized)["label"]
        result = self._agent_request("POST", endpoint, payload=payload, timeout=timeout)
        if not result.get("ok"):
            self._respond_tms_action(
                handler,
                profile=normalized,
                ok=False,
                message=f"{label}登录态操作失败：{normalize_feedback_text(result.get('error') or 'unknown error')}",
                kind="warning",
                http_status=HTTPStatus.BAD_GATEWAY,
            )
            return

        response_payload = result.get("data")
        if not isinstance(response_payload, dict):
            self._respond_tms_action(
                handler,
                profile=normalized,
                ok=False,
                message=f"{label}登录态接口返回了无效数据。",
                kind="warning",
                http_status=HTTPStatus.BAD_GATEWAY,
            )
            return

        if response_payload.get("ok") is False:
            error_text = normalize_feedback_text(
                response_payload.get("message") or response_payload.get("error") or "TMS 登录态操作失败。"
            )
            error_code = str(response_payload.get("error_code") or "").strip().upper()
            kind = "warning" if error_code in {"AUTH_REQUIRED", "AUTH_PENDING_CODE"} else "error"
            self._respond_tms_action(
                handler,
                profile=normalized,
                ok=False,
                message=error_text,
                kind=kind,
                http_status=HTTPStatus.OK,
            )
            return

        self._respond_tms_action(
            handler,
            profile=normalized,
            ok=True,
            message=success_message,
            kind="success",
            http_status=HTTPStatus.OK,
        )

    def _agent_request(
        self,
        method: str,
        endpoint: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        agent_internal_api_token = str(
            getattr(self.settings, "agent_internal_api_token", "") or ""
        ).strip()
        if not agent_internal_api_token:
            return {
                "ok": False,
                "status": None,
                "error": "AGENT_INTERNAL_API_TOKEN is not configured",
            }
        url = f"{self.settings.agent_base_url.rstrip('/')}{endpoint}"
        body: bytes | None = None
        headers: dict[str, str] = {
            "X-Agent-Internal-Token": agent_internal_api_token,
        }
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"

        request = Request(url, data=body, headers=headers, method=method.upper())
        try:
            if timeout is None:
                response_handle = urlopen(request)
            else:
                response_handle = urlopen(request, timeout=timeout or self.settings.agent_timeout_seconds)
            with response_handle as response:
                raw = response.read().decode("utf-8")
                data = json.loads(raw) if raw else {}
                return {
                    "ok": True,
                    "status": response.status,
                    "data": data,
                }
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = raw
            return {
                "ok": False,
                "status": exc.code,
                "error": redact_sensitive(data or str(exc)),
            }
        except URLError as exc:
            return {"ok": False, "status": None, "error": redact_text(exc.reason)}
        except Exception as exc:
            return {"ok": False, "status": None, "error": redact_text(exc)}

    def _latest_tool_log(self, tool_name: str, *, since: str | None = None) -> dict[str, Any] | None:
        endpoint = f"/tool-logs?limit=5&tool_name={quote(tool_name, safe='')}"
        result = self._agent_request("GET", endpoint, timeout=5)
        if not result.get("ok"):
            return None
        data = result.get("data")
        rows = data.get("rows") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return None
        for row in rows:
            if not isinstance(row, dict):
                continue
            created_at = str(row.get("created_at") or "")
            if since and created_at and created_at < since:
                continue
            return row
        return None

    def _sync_task_runtime_from_latest_tool_log(
        self,
        task_id: str,
        tool_name: str,
        *,
        since: str | None = None,
    ) -> dict[str, Any] | None:
        row = self._latest_tool_log(tool_name, since=since)
        if not row:
            return None

        result = row.get("result")
        if not isinstance(result, dict):
            result = {}
        cancelled = bool(result.get("canceled"))
        ok = bool(row.get("success")) and not cancelled
        last_run = str(row.get("created_at") or "")
        duration_ms = row.get("duration_ms")
        error_message_full = normalize_feedback_text(result.get("error") or "")
        error_message_value = shorten_error_message(error_message_full)
        payload = result.get("data") if ok else result
        feedback_meta = automation_runtime_feedback_meta(
            ok=ok,
            cancelled=cancelled,
            success_message=str(automation_workflow_definition(task_id).get("display_name") or task_id),
            error_message=error_message_value,
        )

        if self.repository.list_scheduled_task_group(task_id):
            self.repository.update_scheduled_task_runtime(
                base_task_id=task_id,
                last_run=last_run,
                last_status=feedback_meta["status"],
                last_duration_ms=duration_ms,
                last_message="" if ok else error_message_full,
            )
        else:
            self._record_virtual_task_runtime(
                task_id,
                last_run=last_run,
                last_status=feedback_meta["status"],
                last_duration_ms=duration_ms,
                last_message="" if ok else error_message_full,
            )

        return {
            "ok": ok,
            "cancelled": cancelled,
            "title": feedback_meta["title"],
            "message": feedback_meta["message"],
            "last_run": last_run,
            "duration_label": format_duration_label(duration_ms),
            "error": error_message_full,
            "payload": payload,
        }

    def _runtime_from_local_task_state(self, task_id: str, *, since: str | None = None) -> dict[str, Any] | None:
        rows: list[dict[str, Any]] = []
        try:
            rows = self.repository.list_scheduled_task_group(task_id)
        except Exception:
            rows = []

        if rows:
            state = max(rows, key=lambda item: str(item.get("last_run") or ""))
        else:
            state = dict(getattr(self, "automation_virtual_task_state", {}).get(task_id) or {})

        last_run = str(state.get("last_run") or "")
        if not last_run or (since and last_run < since):
            return None

        last_status = str(state.get("last_status") or "")
        if last_status in {"", "running"}:
            return None

        cancelled = last_status == "cancelled"
        ok = last_status == "success" and not cancelled
        error_message_full = normalize_feedback_text(state.get("last_message") or "")
        feedback_meta = automation_runtime_feedback_meta(
            ok=ok,
            cancelled=cancelled,
            success_message=str(automation_workflow_definition(task_id).get("display_name") or task_id),
            error_message=shorten_error_message(error_message_full),
        )
        return {
            "ok": ok,
            "cancelled": cancelled,
            "title": feedback_meta["title"],
            "message": feedback_meta["message"],
            "last_run": last_run,
            "duration_label": format_duration_label(state.get("last_duration_ms")),
            "error": error_message_full,
            "payload": None,
        }

    def _sync_task_runtime_from_output_payload(
        self,
        task_id: str,
        output_payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        lines = output_payload.get("lines")
        if not isinstance(lines, list) or not lines:
            return None

        parsed_result: dict[str, Any] | None = None
        for row in reversed(lines):
            text = str(row or "").strip()
            if not text or not text.startswith("{"):
                continue
            try:
                candidate = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                parsed_result = candidate
                break

        if not parsed_result:
            return None

        cancelled = bool(parsed_result.get("canceled"))
        ok = bool(parsed_result.get("ok")) and not parsed_result.get("error") and not cancelled
        last_run = str(output_payload.get("started_at") or "") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        duration_ms = None
        for key in ("duration_s", "cost_sec"):
            raw_value = parsed_result.get(key)
            try:
                if raw_value not in (None, ""):
                    duration_ms = int(round(float(raw_value) * 1000))
                    break
            except (TypeError, ValueError):
                continue
        error_message_full = normalize_feedback_text(parsed_result.get("error") or "")
        error_message_value = shorten_error_message(error_message_full)
        payload = parsed_result if ok else {"data": parsed_result}
        feedback_meta = automation_runtime_feedback_meta(
            ok=ok,
            cancelled=cancelled,
            success_message=str(automation_workflow_definition(task_id).get("display_name") or task_id),
            error_message=error_message_value,
        )

        if self.repository.list_scheduled_task_group(task_id):
            self.repository.update_scheduled_task_runtime(
                base_task_id=task_id,
                last_run=last_run,
                last_status=feedback_meta["status"],
                last_duration_ms=duration_ms,
                last_message="" if ok else error_message_full,
            )
        else:
            self._record_virtual_task_runtime(
                task_id,
                last_run=last_run,
                last_status=feedback_meta["status"],
                last_duration_ms=duration_ms,
                last_message="" if ok else error_message_full,
            )

        return {
            "ok": ok,
            "cancelled": cancelled,
            "title": feedback_meta["title"],
            "message": feedback_meta["message"],
            "last_run": last_run,
            "duration_label": format_duration_label(duration_ms),
            "error": error_message_full,
            "payload": payload,
        }

    def _is_ajax_request(self, handler: BaseHTTPRequestHandler) -> bool:
        requested_with = str(handler.headers.get("X-Requested-With", "") or "").strip().lower()
        accept_header = str(handler.headers.get("Accept", "") or "").strip().lower()
        return requested_with in {"xmlhttprequest", "fetch"} or "application/json" in accept_header

    def _render_document(self, handler: BaseHTTPRequestHandler, document_id: int | None, query: dict) -> None:
        document = None
        if document_id is not None:
            document = self.repository.get_document(document_id)
            if not document:
                self._send_text(handler, HTTPStatus.NOT_FOUND, "Document not found.")
                return

        counts = self.repository.count_by_status()
        pending_docs = self.repository.list_documents_by_status(["review_required", "processing", "queued", "error"])
        if document is not None:
            pending_docs = self._pin_document_to_top(pending_docs, document["id"])
        mode_value = query.get("mode", [""])[0].strip().lower()
        boyi_frame_mode = document is None and str(query.get("boyi_frame", [""])[0]).strip().lower() in {"1", "true", "yes"}
        ocr_mode = document is None and mode_value == "ocr"
        yunda_mode = document is None and mode_value == "yunda"
        ronghui_mode = document is None and mode_value == "ronghui"
        active_template_name = self.template_store.get_active_template_name()
        template_spec = self._get_template_spec_for_document(document)
        manual_amap_config = {
            "amap_js_key": self.settings.amap_api_key or "YOUR_AMAP_JS_API_KEY",
            "amap_security_code": self.settings.amap_security_code or "",
        }
        manual_amap_sdk_should_load = not manual_amap_config["amap_js_key"].startswith("YOUR_")
        manual_preview_waybill_no = ""
        if document is None:
            try:
                manual_preview_waybill_no = self.repository.peek_next_manual_waybill_no()
            except Exception:
                manual_preview_waybill_no = ""

        fields = []
        preprocess_info = {}
        preprocess_quality = {"blocking_messages": [], "warning_messages": []}

        if document:
            preprocess_info = document["raw_ocr"].get("preprocess", {})
            preprocess_quality = dict(preprocess_info.get("quality", {}))
            preprocess_quality["blocking_messages"] = quality_issue_messages(
                preprocess_quality.get("blocking_issues", [])
            )
            preprocess_quality["warning_messages"] = []
            normalized_fields = self.service.coerce_fields(document["fields"], template_spec)

            for spec in template_spec["fields"]:
                entry = normalized_fields.get(spec["name"], {})
                fields.append(
                    {
                        "name": spec["name"],
                        "label": spec["label"],
                        "required": spec.get("required", False),
                        "hint": spec.get("hint", ""),
                        "value": entry.get("value", ""),
                        "confidence": entry.get("confidence", 0.0),
                        "source": entry.get("source", ""),
                        "message": entry.get("message", ""),
                    }
                )

        template = self.template_env.get_template("document.html")
        body = template.render(
            app_title=self.settings.app_title,
            document=document,
            fields=fields,
            counts=counts,
            pending_docs=pending_docs,
            queue_snapshot=self.task_queue.snapshot(),
            auto_refresh=(document and document["status"] in {"queued", "processing"}) or (
                ocr_mode and bool(counts.get("queued", 0) or counts.get("processing", 0))
            ),
            ocr_mode=ocr_mode,
            yunda_mode=yunda_mode,
            ronghui_mode=ronghui_mode,
            boyi_frame_mode=boyi_frame_mode,
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
            original_url=self._runtime_url(document["original_path"]) if document else "",
            processed_url=self._runtime_url(document["processed_path"]) if document else "",
            preprocess_info=preprocess_info,
            preprocess_quality=preprocess_quality,
            raw_ocr=document["raw_ocr"] if document else {},
            available_templates=self.template_store.list_templates(),
            active_template_name=active_template_name,
            document_template_name=document["template_name"] if document else active_template_name,
            settings=self.settings,
            writers=self.repository.list_writers(),
            document_writer_id=document.get("writer_id", "") if document else "",
            manual_amap_config=manual_amap_config,
            manual_amap_sdk_should_load=manual_amap_sdk_should_load,
            manual_preview_waybill_no=manual_preview_waybill_no,
        )
        self._send_html(handler, body)

    def _pin_document_to_top(self, documents: list[dict[str, Any]], document_id: int) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        others: list[dict[str, Any]] = []
        for item in documents:
            if int(item.get("id", 0) or 0) == document_id:
                selected.append(item)
            else:
                others.append(item)
        return selected + others

    def _render_template_editor(
        self,
        handler: BaseHTTPRequestHandler,
        template_name: str | None,
        query: dict,
        *,
        spec_override: dict[str, Any] | None = None,
        template_json_override: str | None = None,
        original_template_name_override: str | None = None,
    ) -> None:
        try:
            if spec_override is not None:
                template_spec = spec_override
                original_template_name = original_template_name_override or template_name or ""
            elif template_name:
                template_spec = self.template_store.get_template_spec(template_name)
                original_template_name = template_name
            else:
                copy_from = query.get("copy_from", [""])[0].strip() or self.template_store.get_active_template_name()
                template_spec = self.template_store.build_new_template_spec(copy_from)
                original_template_name = ""
        except FileNotFoundError:
            self._send_text(handler, HTTPStatus.NOT_FOUND, "Template not found.")
            return

        template = self.template_env.get_template("template_editor.html")
        body = template.render(
            app_title=self.settings.app_title,
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
            available_templates=self.template_store.list_templates(),
            active_template_name=self.template_store.get_active_template_name(),
            is_new=not bool(original_template_name),
            original_template_name=original_template_name,
            template_name_value=str(template_spec.get("template_name", "") or ""),
            description_value=str(template_spec.get("description", "") or ""),
            template_json=template_json_override if template_json_override is not None else json.dumps(template_spec, ensure_ascii=False, indent=2),
        )
        self._send_html(handler, body)

    def _build_project_modules(self) -> dict[str, ProjectModule]:
        return {
            "ocr": ProjectModule(
                slug="ocr",
                name="运单录入",
                status="ready",
                summary="支持手工录单与 OCR 图文复核，确认后写回数据库。",
                code_path="console/",
                docs_path="docs/ocr/",
                route="/modules/ocr",
                workspace_path="/ocr",
                current_focus="手工录单、批量 OCR、人工复核流转与 MySQL 回写。",
                inputs=("手工表单", "运单图片", "Qwen OCR API", "人工复核"),
                outputs=("结构化字段", "归档原图", "预处理图片", "数据库记录"),
                dependencies=(),
                consumers=("finance", "ai-service", "customer-service"),
                commands=("cd /home/deng/projects/console && ./start_backend.sh",),
            ),
            "pricing": ProjectModule(
                slug="pricing",
                name="价格获取",
                status="maintained",
                summary="基于地址库和荣辉 TMS 生成报价资产与成本底表。",
                code_path="price_scripts/",
                docs_path="docs/price_scripts/",
                route="/modules/pricing",
                workspace_path="",
                current_focus="地址标准化、TMS 批量取价与报价单产出。",
                inputs=("地址数据库", "TMS 登录态", "网点映射规则"),
                outputs=("全国报价表", "客户报价单", "价格图表", "网点报价表"),
                dependencies=(),
                consumers=("finance", "ai-service", "customer-service"),
                commands=(
                    'cd /d C:\\Users\\DENG\\Desktop\\agent\\price_scripts\\scripts\\02_tms_price_fetch && python -u batch_run.py',
                    'cd /d C:\\Users\\DENG\\Desktop\\agent\\price_scripts\\scripts\\03_finance_summary_charts && python 生成客户报价表.py',
                ),
            ),
            "finance": ProjectModule(
                slug="finance",
                name="财务对账",
                status="etl-ready",
                summary="生成财务对账结果、月度 ETL 数据和核验报表。",
                code_path="finance_reconciliation/",
                docs_path="docs/finance_reconciliation/",
                route="/modules/finance",
                workspace_path="",
                current_focus="运单对账、月度损益和差异核验。",
                inputs=("支付流水", "平台订单", "运单数据", "发票数据", "价格底表"),
                outputs=("财务工作簿", "清洗中间表", "月度损益", "差异清单"),
                dependencies=("ocr", "pricing"),
                consumers=("ai-service", "customer-service"),
                commands=('cd /d C:\\Users\\DENG\\Desktop\\agent\\finance_reconciliation && python -m etl.main',),
            ),
            "customer-service": ProjectModule(
                slug="customer-service",
                name="客服系统",
                status="in-progress",
                summary="集中处理融辉和韵达的问题件实时查询、提醒、详情、回复和发布。",
                code_path="console/",
                docs_path="docs/customer_service/",
                route="/modules/customer-service",
                workspace_path="/modules/customer-service",
                current_focus="第一版只做问题件闭环；差错、调拨件等客服类别后续逐步接入。",
                inputs=("问题件", "差错", "调拨件", "平台工单", "运单状态"),
                outputs=("问题件工作台", "页面提醒", "处理回复", "发布记录", "附件上传"),
                dependencies=("ocr", "pricing", "finance", "dispatch"),
                consumers=(),
                commands=("打开 /modules/customer-service 后选择融辉或韵达业务账号并实时查询。",),
            ),
            "ai-service": ProjectModule(
                slug="ai-service",
                name="AI客服",
                status="planned",
                summary="消费 OCR、报价和财务结果，为客服问答提供统一入口。",
                code_path="agent/ + feishu/",
                docs_path="docs/ai_service/",
                route="/modules/ai-service",
                workspace_path="",
                current_focus="报价问答、查询回复和异常解释编排。",
                inputs=("OCR 字段", "客户报价表", "财务结果", "知识规则"),
                outputs=("客服回复", "报价回答", "异常说明", "工单"),
                dependencies=("ocr", "pricing", "finance"),
                consumers=(),
                commands=("待补实现。",),
            ),
            "dispatch": ProjectModule(
                slug="dispatch",
                name="货拉拉调度",
                status="in-progress",
                summary="管理车队资源，支撑调度计划与轨迹监控。",
                code_path="console/",
                docs_path="docs/dispatch/",
                route="/modules/dispatch",
                workspace_path="/dispatch",
                current_focus="车队主数据、调度面板、线路与监控。",
                inputs=("车辆信息", "运单数据", "线路规则", "司机排班"),
                outputs=("调度单", "车辆轨迹", "运力报表", "预警信息"),
                dependencies=("ocr", "pricing"),
                consumers=("finance", "ai-service", "customer-service"),
                commands=("待补实现。",),
            ),
        }

    def _build_module_view_models(self, counts: dict[str, int]) -> dict[str, dict]:
        pricing_output_dir = PROJECT_ROOT / "浠锋牸鑾峰彇鑴氭湰" / "杈撳嚭缁撴灉"
        pricing_file_count = 0
        if pricing_output_dir.exists():
            pricing_file_count = sum(1 for item in pricing_output_dir.rglob("*") if item.is_file())
        finance_report = PROJECT_ROOT / "璐㈠姟瀵硅处" / "output" / "reports" / "璐㈠姟瀵硅处鎶ヨ〃.xlsx"
        ai_dir = PROJECT_ROOT / "agent"
        ai_file_count = 0
        if ai_dir.exists():
            ai_file_count = sum(1 for item in ai_dir.rglob("*") if item.is_file())
        dispatch_dir = MODULE_DIR
        dispatch_file_count = 0
        if dispatch_dir.exists():
            dispatch_file_count = sum(1 for item in dispatch_dir.rglob("*") if item.is_file())
        total_documents = sum(counts.values())

        metrics = {
            "ocr": {
                "metric_label": "记录数",
                "metric_value": f"{total_documents} 条",
                "highlights": [
                    f"排队中 {counts.get('queued', 0)}",
                    f"处理中 {counts.get('processing', 0)}",
                    f"待复核 {counts.get('review_required', 0)}",
                    f"已确认 {counts.get('confirmed', 0)}",
                ],
                "workspace_label": "进入运单录入",
            },
            "pricing": {
                "metric_label": "产出文件",
                "metric_value": f"{pricing_file_count} 个",
                "highlights": [
                    "地址库 -> TMS 取价 -> 客户报价表",
                    "支撑客服报价与财务成本底表",
                    "作为价格资产层持续维护",
                ],
                "workspace_label": "查看价格模块",
            },
            "finance": {
                "metric_label": "报表状态",
                "metric_value": "已生成" if finance_report.exists() else "待生成",
                "highlights": [
                    "多渠道财务对账",
                    "月度损益与发票差异",
                    "清洗表与核验链路",
                ],
                "workspace_label": "查看财务模块",
            },
            "customer-service": {
                "metric_label": "接入状态",
                "metric_value": "待接入",
                "highlights": [
                    "问题件、差错、调拨件集中入口",
                    "后续按平台来源逐步接入处理流程",
                    "当前不读取真实工单或第三方接口",
                ],
                "workspace_label": "查看客服系统",
            },
            "ai-service": {
                "metric_label": "文件数",
                "metric_value": f"{ai_file_count} 个",
                "highlights": [
                    "消费 OCR、报价和财务结果",
                    "处理报价问答与订单查询",
                    "当前以规划和接口对接为主",
                ],
                "workspace_label": "查看 AI 客服规划",
            },
            "dispatch": {
                "metric_label": "文件数",
                "metric_value": f"{dispatch_file_count} 个",
                "highlights": [
                    "车队资源监控",
                    "调度与线路规划",
                    "运力分配与预警",
                ],
                "workspace_label": "进入调度中心",
            },
        }

        modules: dict[str, dict] = {}
        for slug, module in self.project_modules.items():
            data = metrics[slug]
            modules[slug] = {
                "slug": module.slug,
                "name": module.name,
                "status": module.status,
                "summary": module.summary,
                "code_path": module.code_path,
                "docs_path": module.docs_path,
                "route": module.route,
                "workspace_path": module.workspace_path,
                "current_focus": module.current_focus,
                "inputs": list(module.inputs),
                "outputs": list(module.outputs),
                "dependencies": list(module.dependencies),
                "consumers": list(module.consumers),
                "commands": list(module.commands),
                "metric_label": data["metric_label"],
                "metric_value": data["metric_value"],
                "highlights": data["highlights"],
                "workspace_label": data["workspace_label"],
            }
        return modules

    def _build_relationship_cards(self) -> list[dict[str, object]]:
        return [
            {
                "title": "OCR 入库",
                "description": "运单图片先进入 OCR，再经过排队、识别和人工复核。",
                "inputs": ["图片目录", "Qwen OCR API", "复核规则"],
                "outputs": ["结构化字段", "归档图片"],
            },
            {
                "title": "报价资产",
                "description": "价格模块基于地址数据和荣辉 TMS 生成标准报价表。",
                "inputs": ["地址库", "TMS 登录态", "网点映射"],
                "outputs": ["全国报价表", "客户报价单", "价格图表"],
            },
            {
                "title": "财务对账",
                "description": "财务模块消费运单、支付和发票数据，产出对账报表。",
                "inputs": ["OCR 运单", "支付记录", "发票数据", "价格底表"],
                "outputs": ["财务工作簿", "差异记录"],
            },
            {
                "title": "客服系统",
                "description": "客服系统集中承接问题件、差错和调拨件，后续按平台逐步接入真实处理链路。",
                "inputs": ["问题件", "差错", "调拨件", "平台工单"],
                "outputs": ["处理记录", "责任状态", "协同备注"],
            },
            {
                "title": "调度作业",
                "description": "调度模块使用业务数据监控车队运力和线路执行情况。",
                "inputs": ["运单数据", "价格底表", "车队数据", "司机排班"],
                "outputs": ["调度单", "车辆轨迹", "运力报表"],
            },
            {
                "title": "AI 客服编排",
                "description": "AI 客服模块消费 OCR、报价和财务结果，对外提供问答能力。",
                "inputs": ["OCR 字段", "报价表", "财务差异信息"],
                "outputs": ["客服回复", "异常说明", "工单"],
            },
        ]

    def _get_template_spec_for_document(self, document: dict[str, Any] | None) -> dict[str, Any]:
        template_name = ""
        if document:
            template_name = str(document.get("template_name", "") or "").strip()
        try:
            return self.template_store.get_template_spec(template_name)
        except FileNotFoundError:
            return self.template_store.get_active_template_spec()

    def _safe_return_to(self, value: str, fallback: str = "/ocr") -> str:
        candidate = (value or "").strip()
        if candidate.startswith("/") and not candidate.startswith("//") and "\r" not in candidate and "\n" not in candidate:
            return candidate
        return fallback

    def _validate_template_spec(self, spec: dict[str, Any]) -> str | None:
        if not isinstance(spec, dict):
            return "Template JSON must be an object."
        if not isinstance(spec.get("preprocess"), dict):
            return "Template JSON must include a preprocess object."
        fields = spec.get("fields")
        if not isinstance(fields, list):
            return "Template JSON must include a fields array."
        for index, field in enumerate(fields, start=1):
            if not isinstance(field, dict):
                return f"fields[{index}] must be an object."
            if not str(field.get("name", "") or "").strip():
                return f"fields[{index}] is missing name."
            if not str(field.get("label", "") or "").strip():
                return f"fields[{index}] is missing label."
        return None

    def _handle_template_select(self, handler: BaseHTTPRequestHandler) -> None:
        values = self._parse_urlencoded_form(handler)
        template_name = values.get("template_name", "").strip()
        return_to = self._safe_return_to(values.get("return_to", ""), "/ocr")
        try:
            self.template_store.set_active_template_name(template_name)
        except FileNotFoundError:
            self._redirect_with_message(handler, return_to, "Template not found.", "warning")
            return
        self._redirect_with_message(handler, return_to, f"宸插垏鎹㈡ā鏉匡細{template_name}", "success")

    def _handle_template_save(self, handler: BaseHTTPRequestHandler) -> None:
        values = self._parse_urlencoded_form(handler)
        original_template_name = values.get("original_template_name", "").strip()
        template_name = values.get("template_name", "").strip()
        description = values.get("description", "").strip()
        template_json = values.get("template_json", "").strip()
        set_active = values.get("set_active", "").strip() in {"1", "on", "true", "yes"}
        active_before = self.template_store.get_active_template_name()

        try:
            parsed = json.loads(template_json)
        except json.JSONDecodeError as exc:
            self._render_template_editor(
                handler,
                original_template_name or None,
                {"message": [f"Template JSON parse failed: {exc.msg}"], "kind": ["warning"]},
                spec_override={"template_name": template_name, "description": description, "preprocess": {}, "fields": []},
                template_json_override=template_json,
                original_template_name_override=original_template_name,
            )
            return

        if not isinstance(parsed, dict):
            self._render_template_editor(
                handler,
                original_template_name or None,
                {"message": ["Template JSON must be an object."], "kind": ["warning"]},
                spec_override={"template_name": template_name, "description": description, "preprocess": {}, "fields": []},
                template_json_override=template_json,
                original_template_name_override=original_template_name,
            )
            return

        parsed["template_name"] = template_name or parsed.get("template_name", "")
        parsed["description"] = description
        validation_error = self._validate_template_spec(parsed)
        if validation_error:
            self._render_template_editor(
                handler,
                original_template_name or None,
                {"message": [validation_error], "kind": ["warning"]},
                spec_override=parsed,
                template_json_override=json.dumps(parsed, ensure_ascii=False, indent=2),
                original_template_name_override=original_template_name,
            )
            return

        saved_name = self.template_store.save_template_spec(parsed, original_template_name or None)
        if original_template_name and original_template_name != saved_name:
            self.repository.rename_template_name(original_template_name, saved_name)
        if set_active or (original_template_name and original_template_name == active_before):
            self.template_store.set_active_template_name(saved_name)

        message = f"Template saved: {saved_name}"
        if set_active:
            message += " and set as active template."
        self._redirect_with_message(handler, "/ocr", message, "success")

    def _handle_upload(self, handler: BaseHTTPRequestHandler) -> None:
        form = self._parse_multipart_form(handler)
        file_items = form["files"] if "files" in form else []
        if not isinstance(file_items, list):
            file_items = [file_items]
        raw_return_to = form.getvalue("return_to") if "return_to" in form else "/ocr?mode=ocr"
        return_to = self._safe_return_to(str(raw_return_to or ""), "/ocr?mode=ocr")
        selected_template = ""
        if "template_name" in form:
            raw_template = form.getvalue("template_name")
            if isinstance(raw_template, str):
                selected_template = raw_template.strip()

        queued = 0
        failed = 0
        skipped = 0
        for item in file_items:
            filename = item.filename or ""
            if not filename:
                continue
            suffix = Path(filename).suffix.lower()
            if suffix not in {".jpg", ".jpeg", ".png"}:
                skipped += 1
                continue
            payload = item.file.read()
            if not payload:
                skipped += 1
                continue
            upload_item = UploadItem(
                filename=Path(filename).name,
                source_relpath=filename.replace("\\", "/"),
                payload=payload,
            )
            try:
                result = self.service.process_upload(upload_item, template_name=selected_template)
            except Exception:
                failed += 1
                continue
            if result.status == "error":
                failed += 1
            else:
                queued += 1

        message = f"Queued {queued}, failed {failed}, skipped {skipped}."
        kind = "warning" if failed else "success"
        self._redirect_with_message(handler, return_to, message, kind)

    def _handle_line_haul_contact_create(self, handler: BaseHTTPRequestHandler) -> None:
        values = self._parse_urlencoded_form(handler)
        return_to = self._safe_return_to(values.get("return_to", ""), "/line-haul-contacts")
        payload = self._line_haul_contact_payload(values)
        if not payload["company_name"] or not payload["service_area"]:
            self._redirect_with_message(handler, return_to, "公司名称和分流站点不能为空。", "warning")
            return
        try:
            row = self.repository.create_line_haul_contact(payload)
        except Exception as exc:
            self._redirect_with_message(handler, return_to, f"新增专线分流资料失败：{exc}", "warning")
            return
        self._redirect_with_message(
            handler,
            return_to,
            f"已新增：{row.get('company_name', '')} / {row.get('service_area', '')}",
            "success",
        )

    def _handle_line_haul_contact_update(self, handler: BaseHTTPRequestHandler, path: str) -> None:
        contact_id = self._parse_line_haul_contact_id(path, "update")
        if contact_id is None:
            self._send_json(handler, HTTPStatus.NOT_FOUND, {"ok": False, "message": "专线分流资料不存在。"})
            return
        values = self._parse_urlencoded_form(handler)
        return_to = self._safe_return_to(values.get("return_to", ""), "/line-haul-contacts")
        wants_redirect = bool(str(values.get("return_to", "") or "").strip())
        payload = self._line_haul_contact_payload(values)
        if not payload["company_name"] or not payload["service_area"]:
            if wants_redirect:
                self._redirect_with_message(handler, return_to, "公司名称和分流站点不能为空。", "warning")
                return
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": "公司名称和分流站点不能为空。"})
            return
        try:
            row = self.repository.update_line_haul_contact(contact_id, payload)
        except Exception as exc:
            if wants_redirect:
                self._redirect_with_message(handler, return_to, f"保存失败：{exc}", "warning")
                return
            self._send_json(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "message": f"保存失败：{exc}"})
            return
        if not row:
            if wants_redirect:
                self._redirect_with_message(handler, return_to, "专线分流资料不存在。", "warning")
                return
            self._send_json(handler, HTTPStatus.NOT_FOUND, {"ok": False, "message": "专线分流资料不存在。"})
            return
        if wants_redirect:
            self._redirect_with_message(
                handler,
                return_to,
                f"已保存：{row.get('company_name', '')} / {row.get('service_area', '')}",
                "success",
            )
            return
        self._send_json(handler, HTTPStatus.OK, {"ok": True, "message": "已保存", "row": row})

    def _handle_line_haul_contact_import_paste(self, handler: BaseHTTPRequestHandler) -> None:
        values = self._parse_urlencoded_form(handler)
        return_to = self._safe_return_to(values.get("return_to", ""), "/line-haul-contacts")
        paste_text = values.get("paste_text", "")
        parsed = parse_line_haul_paste(paste_text)
        rows = parsed["rows"]
        if not rows:
            self._redirect_with_message(handler, return_to, "没有可导入的有效行。", "warning")
            return
        try:
            stats = self.repository.import_line_haul_contacts(rows)
        except Exception as exc:
            self._redirect_with_message(handler, return_to, f"导入失败：{exc}", "warning")
            return
        message = (
            f"已导入 {stats.get('inserted', 0)} 条，"
            f"跳过重复 {stats.get('skipped_duplicate', 0)} 条，"
            f"跳过空行 {parsed.get('skipped_empty', 0)} 条。"
        )
        kind = "success" if stats.get("inserted", 0) else "warning"
        self._redirect_with_message(handler, return_to, message, kind)

    def _handle_waybill_status_update(self, handler: BaseHTTPRequestHandler, waybill_id: int) -> None:
        values = self._parse_urlencoded_form(handler)
        return_to = self._clean_next_url(values.get("return_to", "/waybills"))
        if not return_to.startswith("/waybills"):
            return_to = "/waybills"
        status = normalize_waybill_status(values.get("status", ""))
        if status != "cancelled":
            self._redirect_with_message(handler, return_to, "当前只支持作废运单。", "warning")
            return
        try:
            updated = self.repository.update_waybill_status(waybill_id, status)
        except Exception as exc:
            self._redirect_with_message(handler, return_to, f"运单状态更新失败：{exc}", "warning")
            return
        if not updated:
            self._redirect_with_message(handler, return_to, "运单不存在或状态未更新。", "warning")
            return
        self._redirect_with_message(handler, return_to, "运单已作废。", "success")

    def _unwrap_quote_agent_result(self, result: dict[str, Any], *, label: str) -> dict[str, Any]:
        if not result.get("ok"):
            error = result.get("error")
            payload = error if isinstance(error, dict) else {"error": str(error or f"{label}报价调用失败")}
            if result.get("status"):
                payload["status_code"] = result.get("status")
            return payload
        outer = result.get("data")
        if isinstance(outer, dict):
            if outer.get("ok") is False:
                return outer
            nested = outer.get("data")
            if isinstance(nested, dict):
                return nested
            return outer
        return {"error": f"{label}报价返回格式异常"}

    def _call_quote_agent_source(
        self,
        *,
        endpoint: str,
        label: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        timeout_sec = 75
        payload = {
            "params": {
                "address": request["receiver_address"],
                "weight": str(request["weight"]),
                "volume": str(request["volume"]),
            },
            "timeout_sec": timeout_sec,
        }
        result = self._agent_request(
            "POST",
            endpoint,
            payload=payload,
            timeout=max(timeout_sec + 15, self.settings.agent_timeout_seconds),
        )
        return self._unwrap_quote_agent_result(result, label=label)

    def _handle_quote_options(self, handler: BaseHTTPRequestHandler) -> None:
        body = self._parse_json_body(handler)
        try:
            request = parse_quote_options_request(body)
        except QuoteOptionsValidationError as exc:
            self._send_json(
                handler,
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "message": str(exc), "quotes": [], "best_provider": "", "available_count": 0},
            )
            return

        sources = {
            "ronghui": ("/tms/get_price", "融辉"),
            "yunda": ("/tms/yunda_price", "韵达"),
        }
        results: dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {
                executor.submit(
                    self._call_quote_agent_source,
                    endpoint=endpoint,
                    label=label,
                    request=request,
                ): provider
                for provider, (endpoint, label) in sources.items()
            }
            for future in as_completed(futures):
                provider = futures[future]
                try:
                    results[provider] = future.result()
                except Exception as exc:
                    results[provider] = {"error": f"{sources[provider][1]}报价调用失败：{exc}"}

        payload = build_manual_quote_options(
            ronghui_result=results.get("ronghui") or {"error": "融辉报价无返回"},
            yunda_result=results.get("yunda") or {"error": "韵达报价无返回"},
            delivery_method=request["delivery_method"],
        )
        self._send_json(handler, HTTPStatus.OK, payload)

    def _handle_manual_waybill(self, handler: BaseHTTPRequestHandler) -> None:
        return_to = "/ocr"
        try:
            values = self._parse_urlencoded_form(handler)
            return_to = self._safe_return_to(values.get("return_to", ""), "/ocr")
            should_print = str(values.get("auto_print", "")).lower() in {"1", "true", "yes", "on"}
            result = self.service.apply_manual_waybill(values)
        except Exception as exc:
            self._redirect_with_message(handler, return_to, f"手工单保存失败：{exc}", "warning")
            return

        if not result.ok or not result.waybill_id:
            self._redirect_with_message(handler, return_to, result.message, "warning")
            return

        if not should_print:
            self._redirect_with_message(handler, return_to, result.message, "success")
            return

        self._redirect_with_message(
            handler,
            f"/waybills/{result.waybill_id}/print?autoprint=1",
            result.message,
            "success",
        )

    def _handle_review(self, handler: BaseHTTPRequestHandler, document_id: int) -> None:
        try:
            values = self._parse_urlencoded_form(handler)
            action = values.get("action", "save")
            result = self.service.apply_review(document_id, values)
        except ValueError:
            self._send_text(handler, HTTPStatus.NOT_FOUND, "Document not found.")
            return
        except Exception as exc:
            self._redirect_with_message(handler, f"/documents/{document_id}", f"Save failed: {exc}", "warning")
            return

        kind = "success" if result.ok else "warning"

        # If confirmed successfully, redirect to the queue to find the next document
        if action == "confirm" and result.ok:
            self._redirect_with_message(handler, "/ocr?mode=ocr", result.message, kind)
        else:
            self._redirect_with_message(handler, f"/documents/{document_id}", result.message, kind)

    def _handle_reprocess(self, handler: BaseHTTPRequestHandler, document_id: int) -> None:
        try:
            result = self.service.reprocess_document(document_id)
        except ValueError:
            self._send_text(handler, HTTPStatus.NOT_FOUND, "Document not found.")
            return
        kind = "success" if result.ok else "warning"
        self._redirect_with_message(handler, f"/documents/{document_id}", result.message, kind)

    def _handle_delete(self, handler: BaseHTTPRequestHandler, document_id: int) -> None:
        values = self._parse_urlencoded_form(handler)
        return_to = values.get("return_to", "").strip()
        if not return_to.startswith("/"):
            return_to = "/ocr"

        document = self.repository.get_document(document_id)
        if not document:
            self._redirect_with_message(handler, return_to, "Document does not exist or was already deleted.", "warning")
            return

        self._delete_document_files(document)
        deleted = self.repository.delete_document(document_id)
        if not deleted:
            self._redirect_with_message(handler, return_to, "Delete failed because the database row was not found.", "warning")
            return

        if return_to == f"/documents/{document_id}":
            return_to = "/ocr"
        self._redirect_with_message(handler, return_to, f"Deleted document: {document['original_name']}", "success")

    def _export_document_json(self, handler: BaseHTTPRequestHandler, document_id: int) -> None:
        document = self.repository.get_document(document_id)
        if not document:
            self._send_text(handler, HTTPStatus.NOT_FOUND, "Document not found.")
            return
        payload = json.dumps(document, ensure_ascii=False, indent=2)
        self._send_bytes(
            handler,
            HTTPStatus.OK,
            payload.encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _serve_runtime_file(self, handler: BaseHTTPRequestHandler, relpath: str) -> None:
        self._serve_file(handler, self.settings.runtime_dir, relpath)

    def _serve_static_file(self, handler: BaseHTTPRequestHandler, relpath: str) -> None:
        self._serve_file(handler, MODULE_DIR / "static", relpath)

    def _serve_file(self, handler: BaseHTTPRequestHandler, root: Path, relpath: str) -> None:
        root = root.resolve()
        target = (root / Path(unquote(relpath))).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            self._send_text(handler, HTTPStatus.NOT_FOUND, "File not found.")
            return
        if not target.exists() or not target.is_file():
            self._send_text(handler, HTTPStatus.NOT_FOUND, "File not found.")
            return
        mime_type, _ = mimetypes.guess_type(str(target))
        with target.open("rb") as handle:
            payload = handle.read()
        self._send_bytes(
            handler,
            HTTPStatus.OK,
            payload,
            mime_type or "application/octet-stream",
        )

    def _parse_multipart_form(self, handler: BaseHTTPRequestHandler):
        return cgi.FieldStorage(
            fp=handler.rfile,
            headers=handler.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": handler.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": handler.headers.get("Content-Length", "0"),
            },
        )

    def _parse_urlencoded_form(self, handler: BaseHTTPRequestHandler) -> dict[str, str]:
        content_length = int(handler.headers.get("Content-Length", "0"))
        body = handler.rfile.read(content_length).decode("utf-8")
        parsed = parse_qs(body, keep_blank_values=True)
        return {key: values[-1] if values else "" for key, values in parsed.items()}

    def _runtime_url(self, relpath: str) -> str:
        normalized = relpath.replace("\\", "/")
        url = "/runtime/" + quote(normalized)
        target = self.settings.runtime_dir / normalized
        if target.exists() and target.is_file():
            stamp = int(target.stat().st_mtime_ns)
            return f"{url}?v={stamp}"
        return url

    def _delete_document_files(self, document: dict[str, Any]) -> None:
        runtime_paths: list[Path] = []
        for key in ("original_path", "processed_path", "artifacts_dir"):
            relpath = str(document.get(key, "") or "").strip()
            if not relpath:
                continue
            runtime_paths.append(self.settings.runtime_dir / Path(relpath))

        token = str(document.get("doc_token", "") or "").strip()
        if token:
            runtime_paths.append(self.settings.temp_dir / token)
            runtime_paths.append(self.settings.runtime_dir / "artifacts" / "processed" / token)

        seen: set[Path] = set()
        runtime_root = self.settings.runtime_dir.resolve()
        for candidate in runtime_paths:
            target = candidate.resolve()
            if target in seen:
                continue
            seen.add(target)
            try:
                target.relative_to(runtime_root)
            except ValueError:
                continue
            if not target.exists():
                continue
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)

    def _parse_document_id(self, path: str) -> int | None:
        parts = [part for part in path.split("/") if part]
        if len(parts) < 2:
            return None
        try:
            return int(parts[1])
        except ValueError:
            return None

    def _parse_line_haul_contact_id(self, path: str, suffix: str) -> int | None:
        prefix = "/line-haul-contacts/"
        suffix_value = f"/{suffix}"
        if not path.startswith(prefix) or not path.endswith(suffix_value):
            return None
        raw = path[len(prefix) : -len(suffix_value)].strip("/")
        try:
            return int(raw)
        except ValueError:
            return None

    @staticmethod
    def _line_haul_contact_payload(values: dict[str, str]) -> dict[str, str]:
        payload = {
            "company_name": str(values.get("company_name", "") or "").strip(),
            "service_area": str(values.get("service_area", "") or "").strip(),
            "address": str(values.get("address", "") or "").strip(),
            "contact_name": str(values.get("contact_name", "") or "").strip(),
            "phone_numbers": normalize_phone_numbers(values.get("phone_numbers", "")),
            "remark": str(values.get("remark", "") or "").strip(),
            "source_text": str(values.get("source_text", "") or "").strip(),
        }
        if not payload["source_text"]:
            payload["source_text"] = " ".join(
                value
                for key, value in payload.items()
                if key not in {"source_text"} and value
            )
        return payload

    def _redirect(
        self,
        handler: BaseHTTPRequestHandler,
        location: str,
        headers: list[tuple[str, str]] | None = None,
    ) -> None:
        handler.send_response(HTTPStatus.SEE_OTHER)
        handler.send_header("Location", location)
        for header_name, header_value in headers or []:
            handler.send_header(header_name, header_value)
        handler.end_headers()

    def _redirect_with_message(
        self,
        handler: BaseHTTPRequestHandler,
        location: str,
        message: str,
        kind: str = "info",
    ) -> None:
        separator = "&" if "?" in location else "?"
        encoded_message = quote(message)
        self._redirect(handler, f"{location}{separator}message={encoded_message}&kind={kind}")

    def _send_html(self, handler: BaseHTTPRequestHandler, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send_bytes(
            handler,
            status,
            body.encode("utf-8"),
            "text/html; charset=utf-8",
            cache_control="no-store",
        )

    def _send_text(self, handler: BaseHTTPRequestHandler, status: HTTPStatus, text: str) -> None:
        payload = html.escape(text).encode("utf-8")
        self._send_bytes(handler, status, payload, "text/plain; charset=utf-8")

    def _send_json(self, handler: BaseHTTPRequestHandler, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(
            handler,
            status,
            body,
            "application/json; charset=utf-8",
            cache_control="no-store",
        )

    def _send_bytes(
        self,
        handler: BaseHTTPRequestHandler,
        status: HTTPStatus,
        payload: bytes,
        content_type: str,
        cache_control: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        if cache_control:
            handler.send_header("Cache-Control", cache_control)
        for name, value in (extra_headers or {}).items():
            handler.send_header(name, value)
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)


if __name__ == "__main__":
    load_console_environment()
    LocalDocFlowApp().run()
