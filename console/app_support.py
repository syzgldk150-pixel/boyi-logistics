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
from shared.manual_entry_contracts import YUNDA_MANUAL_ENTRY_ROUTE_ACTIONS
from shared.customer_problem_policy import (
    CUSTOMER_SERVICE_ALLOWED_ACCOUNT_SYSTEMS,
    CUSTOMER_SERVICE_DEFAULT_SETTINGS,
    CUSTOMER_SERVICE_NOTIFIED_SITE_KEYS,
    CUSTOMER_SERVICE_PUBLISH_SITE_KEYS,
    CUSTOMER_SERVICE_RESOURCE_KEY,
    CUSTOMER_SERVICE_SITE_FILTER_LOGIN,
    CUSTOMER_SERVICE_SITE_FILTER_SITE,
    customer_problem_clean_text,
    customer_problem_field,
    legacy_customer_problem_included,
)
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
    "phase7.r13_credentials": "旧版每日应签账号配置，仅用于识别历史资源；控制平面不会读取，请迁移到统一账号管理。",
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

YUNDA_ENTRY_ACTIONS = dict(YUNDA_MANUAL_ENTRY_ROUTE_ACTIONS)

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
            "account_id": "price_default",
        },
        "account_roles": [
            {"label": "运行账号", "field": "account_id", "system": "ronghui", "default_account_id": "price_default"},
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
            "account_id": "ronghui_default",
        },
        "default_tool_params": {"account_id": "ronghui_default"},
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
        "default_tool_params": {
            "r13_account_id": "r13_default",
            "account_id": "ronghui_daxiang_s",
            "days": 7,
        },
        "account_roles": [
            {"label": "R13应签查询账号", "field": "r13_account_id", "system": "r13", "default_account_id": "r13_default"},
            {"label": "TMS邵阳大祥站账号", "field": "account_id", "system": "ronghui", "default_account_id": "ronghui_daxiang_s"},
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
        "default_tool_params": {"account_id": "ronghui_default"},
        "account_roles": [
            {"label": "运行账号", "field": "account_id", "system": "ronghui", "default_account_id": "ronghui_default"},
        ],
        "order": 50,
    },
    {
        "task_id": "customer_problems_shadow",
        "display_name": "客服问题件事项影子采集",
        "tool_name": "sync_customer_service_problems",
        "note": "只读遍历全部融辉、韵达账号的发布给我和我发布的列表，并保存新旧口径对账证据。",
        "task_mode": "scheduled",
        "trigger_label": "定时任务 / 手动重新核验",
        "schedule_summary": "每 15 分钟",
        "default_tool_params": {"direction": "both"},
        "account_roles": [],
        "order": 55,
    },
    {
        "task_id": "clockin_daxiang",
        "display_name": "网点打卡-大祥",
        "tool_name": "clock_in_dual",
        "note": "大祥站双打卡是代码锁定的既有自动任务；参数仍只读，审批策略由超级管理员单独配置。",
        "task_mode": "scheduled",
        "trigger_label": "定时任务",
        "schedule_summary": "",
        "control_plane_only": True,
        "default_tool_params": {},
        "account_roles": [
            {"label": "运行账号", "field": "account_id", "system": "ronghui", "default_account_id": "ronghui_default"},
        ],
        "order": 60,
    },
    {
        "task_id": "clockin_daxiang_s",
        "display_name": "网点打卡-大祥S站",
        "tool_name": "clock_in_dual",
        "note": "大祥 S 站双打卡是代码锁定的既有自动任务；参数仍只读，审批策略由超级管理员单独配置。",
        "task_mode": "scheduled",
        "trigger_label": "定时任务",
        "schedule_summary": "",
        "control_plane_only": True,
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
        "default_tool_params": {
            "account_id": "ronghui_default",
        },
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
            "account_id": "yunda_default",
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
            "account_id": "yunda_default",
            "ensure_fields": False,
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
            "target_date": "",
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
            "account_id": "ronghui_default",
            "trigger_flow": False,
        },
        "default_tool_params": {
            "account_id": "ronghui_default",
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

CONTROL_PLANE_ONLY_AUTOMATION_TASK_IDS = frozenset(
    task_id
    for task_id, workflow in AUTOMATION_WORKFLOW_BY_ID.items()
    if workflow.get("control_plane_only") is True
)
CONTROL_PLANE_ONLY_AUTOMATION_MESSAGE = (
    "这两条打卡的时间、账号与参数仍由代码锁定；"
    "此处不能修改任务配置或立即执行，但超级管理员可以单独设置审批策略。"
)

AUTOMATION_PROVIDER_LABELS = {
    "ronghui": "TMS融辉",
    "yunda": "韵达",
}

AUTOMATION_SESSION_PROFILES = {
    "ronghui": {
        "label": "TMS融辉",
        "dot_label": "TMS融辉",
        "console_prefix": "/automations/tms-session",
        "agent_prefix": "/internal/v1/admin/tms/session",
        "login_kind": "image",
    },
    "yunda": {
        "label": "韵达",
        "dot_label": "韵达",
        "console_prefix": "/automations/yunda-session",
        "agent_prefix": "/internal/v1/admin/tms/yunda-session",
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
    "split_pending_problem_upload",
}

UI_LABELS = {
    "ready": "就绪",
    "maintained": "持续维护",
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


def automation_task_control_plane_only(task_id: str) -> bool:
    return normalize_task_group_id(task_id) in CONTROL_PLANE_ONLY_AUTOMATION_TASK_IDS


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



__all__ = [name for name in globals() if not name.startswith("__")]
