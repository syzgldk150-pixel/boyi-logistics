"""Console application services grouped by business responsibility."""

import copy
import math
import threading
import time
from collections.abc import Mapping

from console.app_support import *  # noqa: F403
from console.services.automation_project_contributions import (
    AUTOMATION_PLUGIN_ACTIVE_CONTRIBUTION_FIELDS as _ACTIVE_CONTRIBUTION_FIELDS,
    AUTOMATION_PLUGIN_CONTRIBUTION_PROJECTION_STATES as _CONTRIBUTION_PROJECTION_STATES,
    AUTOMATION_PLUGIN_V2_ENTRYPOINT_ID_RE as AUTOMATION_PLUGIN_V2_ENTRYPOINT_ID_RE,
    normalize_plugin_active_contributions as _normalize_plugin_active_contributions,
)
from console.services.automation_plugin_management import (
    AutomationPluginManagementServiceMixin,
)
from console.services.automation_resource_catalog import (
    RESOURCE_KIND_LABELS as AUTOMATION_RESOURCE_KIND_LABELS,
    RESOURCE_PROBLEM_LABELS,
    normalize_plugin_resources,
    resource_display_name,
)


AUTOMATION_PROJECT_POLICY_ENDPOINT = "/internal/v1/automation-project-policies"
AUTOMATION_PROJECT_POLICY_MODES = frozenset({"REQUIRE_EACH_RUN", "PROJECT_FULL_AUTO"})
AUTOMATION_PROJECT_EFFECTIVE_MODES = frozenset(
    {*AUTOMATION_PROJECT_POLICY_MODES, "LEGACY_SCHEDULE_ONLY"}
)
AUTOMATION_PROJECT_POLICY_STATUSES = frozenset(
    {
        "ACTIVE",
        "RECONCILING",
        "UNAVAILABLE",
        "UNSUPPORTED",
        "LEGACY_SCHEDULE_ONLY",
    }
)
AUTOMATION_PROJECT_RUNTIME_STATUSES = frozenset(
    {"READY", "RECONCILING", "UNAVAILABLE"}
)
AUTOMATION_RUNTIME_REASON_LABELS = {
    "PROJECT_DISABLED": "项目已停用",
    "PROJECT_CONFIGURATION_INCOMPLETE": "项目配置尚未完整；运行、启用和完全自动均已阻断。",
    "ENTRYPOINTS_DISABLED": "所有运行入口均已关闭",
    "PROJECT_RUNTIME_UNAVAILABLE": "运行环境不可用/待修复",
}
AUTOMATION_RUNTIME_CONTRACT_ERROR_LABEL = (
    "任务安装信息有误，当前无法运行。请联系管理员修复后再试。"
)
AUTOMATION_RUNTIME_RECONCILING_LABEL = "运行环境同步中"
AUTOMATION_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,128}$")
AUTOMATION_PENDING_SET_HASH_RE = re.compile(r"^[A-Za-z0-9._~-]{16,256}$")
AUTOMATION_RUN_RECEIPT_ID_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,160}$")
AUTOMATION_RUN_RECEIPT_STATUSES = frozenset(
    {
        "WAITING_APPROVAL",
        "QUEUED",
        "RUNNING",
        "VERIFYING",
        "COMPLETED",
        "PARTIAL",
        "FAILED_TERMINAL",
        "CANCELLED",
    }
)
AUTOMATION_APPROVAL_BATCH_MAX_RUNS = 1000
AUTOMATION_PROJECT_COMMENT_MAX_CHARS = 500
AUTOMATION_PENDING_RISK_LABELS = {
    "LOW": "低",
    "MEDIUM": "中",
    "HIGH": "高",
    "CRITICAL": "严重",
}
AUTOMATION_PLUGIN_CATALOG_ENDPOINT = "/internal/v1/automation/plugins/catalog"
AUTOMATION_WORKERS_ENDPOINT = "/internal/v1/automation/workers"
AUTOMATION_PLUGIN_MAX_PACKAGE_BYTES = 32 * 1024 * 1024
AUTOMATION_PLUGIN_STATE_LABELS = {
    "ENABLED": "已启用",
    "DISABLED": "已停用",
    "INSTALLED": "待配置",
    "PREPARING": "准备中",
    "SWITCHING": "切换中",
    "DRAINING": "排空中",
    "UPGRADING": "升级中",
    "UNINSTALLING": "卸载中",
    "UNINSTALL_PENDING": "待卸载",
    "BLOCKED_DEPENDENCY": "依赖阻断",
    "NEEDS_CONFIGURATION": "需要配置",
    "BLOCKED_LOGIN": "账号未登录",
    "BLOCKED_UNKNOWN_WRITE": "写入结果未知",
    "ERROR": "异常",
    "UNKNOWN": "状态未知",
}
AUTOMATION_PLUGIN_STABLE_STATES = frozenset({"INSTALLED", "ENABLED", "DISABLED"})
AUTOMATION_PLUGIN_RECONCILE_STATES = frozenset(
    {
        "STABLE",
        "PREPARING",
        "WAITING_COEFFECTS",
        "READY_TO_COMMIT",
        "DRAINING",
        "DISPOSING",
        "BLOCKED_UNKNOWN_WRITE",
        "ERROR",
    }
)
AUTOMATION_PLUGIN_RECONCILE_DISPLAY_STATES = {
    "PREPARING": "PREPARING",
    "WAITING_COEFFECTS": "BLOCKED_DEPENDENCY",
    "READY_TO_COMMIT": "SWITCHING",
    "DRAINING": "DRAINING",
    "DISPOSING": "DRAINING",
    "BLOCKED_UNKNOWN_WRITE": "BLOCKED_UNKNOWN_WRITE",
    "ERROR": "ERROR",
    "UNKNOWN": "UNKNOWN",
}
AUTOMATION_PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
AUTOMATION_PLUGIN_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
AUTOMATION_PLUGIN_API_RE = re.compile(r"^[0-9A-Za-z._<>=,!~^*+\-]{1,32}$")
AUTOMATION_PLUGIN_SERVICE_RE = re.compile(
    r"^plugin\.([a-z][a-z0-9_]{1,63})\.([a-z][a-z0-9_.-]{0,127})@(0|[1-9][0-9]*)$"
)
AUTOMATION_PLUGIN_MIGRATION_PAIR_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
AUTOMATION_PLUGIN_RUNTIME_MODEL_LABELS = {
    "ACTION_V1": "旧版操作",
    "SERVICE_V2": "最新版服务",
    "UNSUPPORTED": "不支持的运行时",
}
AUTOMATION_PLUGIN_UNSUPPORTED_RUNTIME_MODEL = "UNSUPPORTED"
AUTOMATION_PLUGIN_DEPENDENCY_STATE_LABELS = {
    "NOT_APPLICABLE": "v1 动作运行时",
    "ACTIVE": "依赖就绪",
    "READY": "依赖就绪",
    "SATISFIED": "依赖就绪",
    "BLOCKED_DEPENDENCY": "依赖阻断",
    "NEEDS_CONFIGURATION": "需要配置",
    "BLOCKED_LOGIN": "账号未登录",
    "UNKNOWN": "依赖状态未知",
}
AUTOMATION_PLUGIN_DEPENDENCY_BLOCKING_STATES = frozenset(
    {"BLOCKED_DEPENDENCY", "NEEDS_CONFIGURATION", "BLOCKED_LOGIN", "UNKNOWN"}
)
AUTOMATION_PLUGIN_MIGRATION_STATE_LABELS = {
    "PREPARING": "准备迁移项目",
    "TESTING": "并行验证",
    "READY": "等待接管",
    "CUTTING_OVER": "接管中",
    "CUTOVER": "已接管",
    "ROLLING_BACK": "回滚中",
    "ROLLED_BACK": "已回滚",
    "COMPLETED": "迁移完成",
    "ERROR": "迁移异常",
}
AUTOMATION_PLUGIN_MIGRATION_TEST_STATE_LABELS = {
    "NOT_STARTED": "尚未真跑",
    "RUNNING": "真跑验证中",
    "PASSED": "真跑已通过",
    "FAILED": "真跑失败",
    "BLOCKED": "验证受阻",
    "WRITE_OUTCOME_UNKNOWN": "写入结果未知",
}
AUTOMATION_PLUGIN_BLOCK_REASON_COPY = {
    "MISSING_PROVIDER": "缺少服务提供方",
    "PROVIDER_BLOCKED": "依赖服务尚未就绪",
    "PROVIDER_CONFLICT": "服务提供方存在冲突",
    "DEPENDENCY_CYCLE": "服务依赖存在循环",
    "BLOCKED_DEPENDENCY": "依赖服务尚未就绪",
    "CONFIGURATION_INCOMPLETE": "项目配置或资源绑定尚未完成",
    "NEEDS_CONFIGURATION": "项目配置未完整",
    "ACCOUNT_BINDING_MISSING": "必需账号尚未绑定或登录",
    "BLOCKED_LOGIN": "必需账号尚未登录",
}
AUTOMATION_WORKER_ID_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,128}$")
AUTOMATION_PLUGIN_BINDING_ID_RE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,160}$")
AUTOMATION_PLUGIN_CONFIG_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
AUTOMATION_PLUGIN_CODE_OWNED_CONFIG_KEY_RE = re.compile(
    r"^_?[A-Za-z][A-Za-z0-9_]{0,62}$"
)
AUTOMATION_PLUGIN_MIGRATION_RESERVED_BUSINESS_KEY_FIELDS = frozenset(
    {"__host_business_date"}
)
AUTOMATION_PLUGIN_ENTRYPOINTS = frozenset({"scheduler", "console", "feishu", "webhook"})
AUTOMATION_PLUGIN_V2_ENTRYPOINT_KINDS = frozenset(
    {"console", "scheduler", "webhook", "feishu", "events", "module_slots"}
)
AUTOMATION_PLUGIN_CONTRIBUTION_PROJECTION_STATES = (
    _CONTRIBUTION_PROJECTION_STATES
)
AUTOMATION_PLUGIN_ACTIVE_CONTRIBUTION_FIELDS = _ACTIVE_CONTRIBUTION_FIELDS
AUTOMATION_PLUGIN_CONFIG_MAX_FIELDS = 100
AUTOMATION_PLUGIN_CONFIG_MAX_BYTES = 128 * 1024
AUTOMATION_PLUGIN_SCHEDULE_MAX_DAILY_TIMES = 96
AUTOMATION_PLUGIN_SCHEDULE_RUNTIME_STATES = frozenset(
    {
        "ACTIVE",
        "DISABLED",
        "ENTRYPOINT_DISABLED",
        "BLOCKED_GENERATION",
        "REFRESH_FAILED",
    }
)
AUTOMATION_PLUGIN_CONFIG_COPY = {
    "dry_run": ("仅预览，不写入", "开启后只检查和预览结果，不会真正修改业务系统或表格。"),
    "target_date": ("业务日期", "留空时使用当天；需要补跑历史数据时再选择日期。"),
    "start_date": ("开始日期", "需要处理日期范围时填写。"),
    "end_date": ("结束日期", "需要处理日期范围时填写。"),
    "batch_size": ("每批处理数量", "单次处理多少条；通常保持默认即可。"),
    "max_batches": ("最多处理批次", "限制本次最多处理多少批，留空表示按任务默认范围。"),
    "page_size": ("每页读取数量", "通常保持默认即可。"),
    "max_pages": ("最多读取页数", "限制本次最多读取多少页。"),
    "limit": ("最多处理数量", "限制本次处理总量，留空使用任务默认范围。"),
    "skip_bill_codes": ("跳过这些单号", "每行填写一个不需要处理的扫描单号。"),
    "selected_bill_codes": ("本次选择的单号", "每行填写一个需要处理的单号。"),
    "bill_codes": ("运单号", "每行填写一个需要处理的运单号。"),
    "record_ids": ("记录编号", "每行填写一个需要处理的记录编号。"),
    "child_item_limit": ("子单处理上限", "只处理前 N 条子单，留空表示不额外限制。"),
    "child_count_limit": ("子单统计上限", "只统计前 N 个主单的子单，留空表示不额外限制。"),
    "missing_limit": ("缺失件检查上限", "只检查前 N 条缺失记录，留空表示不额外限制。"),
    "export_limit": ("导出数量上限", "只导出前 N 条统计记录，留空表示不额外限制。"),
    "scan_window_days": ("扫描日期范围", "填写 1 表示只处理当天扫描；通常保持默认即可。"),
    "scan_codes_retention_days": ("扫描记录保留天数", "填写 0 表示不自动清理历史扫描记录。"),
    "archive_snapshot": ("保存归档快照", "开启后保存本次统计结果的归档副本。"),
    "pending_sheet_disabled": ("暂停写入未齐货物表", "开启后不更新“未齐货物”表。"),
    "refresh_disabled": ("不刷新当天扫描集合", "兼容选项，通常保持关闭。"),
    "dest_brch": ("目的网点编码", "填写本项目固定使用的目的网点编码。"),
    "direction": ("查询方向", "选择本次查询的业务方向。"),
    "recheck_items": ("重新检查已有问题", "开启后也会重新检查已保存的问题件。"),
    "days": ("统计天数", "设置需要统计最近多少天。"),
    "plate_numbers": ("车牌号", "每行填写一个需要处理的车牌号。"),
    "mode": ("运行模式", "选择本次任务的处理方式。"),
    "platform": ("业务平台", "选择本项目使用的平台。"),
    "batch_id": ("批次编号", "仅在需要处理指定批次时填写。"),
    "sync_sheet": ("同步到表格", "开启后把结果同步到项目绑定的表格。"),
}
AUTOMATION_PLUGIN_COMMON_CONFIG_KEYS = frozenset(AUTOMATION_PLUGIN_CONFIG_COPY)


def _valid_migration_business_key_field(value: str) -> bool:
    """Allow the one host-derived key while rejecting all other host fields."""

    if value in AUTOMATION_PLUGIN_MIGRATION_RESERVED_BUSINESS_KEY_FIELDS:
        return True
    if value.startswith("__host_"):
        return False
    return bool(AUTOMATION_PLUGIN_CONFIG_KEY_RE.fullmatch(value))

AUTOMATION_PLUGIN_ACCOUNT_ROLE_COPY = {
    "account_id": ("运行账号", "任务执行时使用这个业务账号。"),
    "account_ids": ("运行账号", "任务会依次使用所选业务账号。"),
    "finance_quote_source": ("报价账单账号", "读取报价业务的账单数据。"),
    "finance_daxiang_s_source": ("大祥 S 站账单账号", "读取大祥 S 站的账单数据。"),
    "finance_self_pickup_source": ("自提部账单账号", "读取自提部的账单数据。"),
    "customer_service_source": ("客服问题件账号", "读取所选账号下的客服问题件。"),
}
AUTOMATION_PLUGIN_ACCOUNT_ROLE_COPY_BY_PLUGIN = {
    "sync_daily_should_sign": {
        "r13_account_id": (
            "R13 应签查询账号",
            "从这个 R13 账号读取其所属站点范围和应签清单。",
        ),
        "account_id": (
            "融辉到货与签收核验账号",
            "从这个融辉账号核验到货、问题件和主单签收证据。",
        ),
    },
}

AUTOMATION_PLUGIN_RESOURCE_ROLE_COPY = {
    "webhook_route": ("外部调用入口", "只有需要由外部系统触发时才使用。"),
    "feishu_route": ("飞书消息入口", "接收飞书中的固定指令。"),
    "arrive_primary_sheet": ("每日到货表", "保存每日到货基础清单。"),
    "arrive_secondary_sheet": ("每日到货备用表", "保存每日到货基础清单的备用副本。"),
    "self_pickup_source_sheet": ("自提问题件来源表", "读取需要处理的自提问题件。"),
    "split_pending_source_sheet": ("分批未到来源表", "读取分批及有发未到记录。"),
    "split_pending_target_sheet": ("分批未到结果表", "保存当前分批及有发未到结果。"),
    "send_order_bitable": ("当日寄件表", "保存当日寄件数据。"),
    "daily_sign_bitable": ("每日应签明细表", "保存每日应签明细和处理状态。"),
    "daily_sign_sheet": ("每日应签结果表", "保存每日应签结果快照。"),
    "delivery_status_bitable": ("签收状态表", "读取并更新运单签收状态。"),
    "arrival_stats_primary_sheet": (
        "每日到货表",
        "保存每单实际到货件数，S 列为实际到达数量。",
    ),
    "arrival_stats_secondary_sheet": ("每日到货备用表", "保存每日到货统计的备用副本。"),
    "arrival_stats_pending_sheet": (
        "未齐货物表",
        "可选。需要单独保存未齐货物时选择；暂停写入时可以留空。",
    ),
    "arrival_stats_archive_sheet": ("到货统计归档", "按日期保存每次到货统计结果。"),
    "arrival_stats_split_pending_sheet": ("分批未到结果表", "保存分批及有发未到快照。"),
    "site_send_bitable": ("网点出港明细表", "保存网点出港明细。"),
    "site_send_sheet": ("网点出港结果表", "保存网点出港结果快照。"),
    "dispatch_forecast_bitable": ("韵达派件预测表", "保存韵达网点派件量预测。"),
    "send_waybills_bitable": ("韵达寄件明细表", "保存韵达寄件运单明细。"),
    "send_waybills_sheet": ("韵达寄件结果表", "保存韵达寄件运单结果快照。"),
}

AUTOMATION_RESOURCE_DISPLAY_NAMES = {
    "phase7.delivery_status_webhook": "签收状态外部入口",
    "phase7.price_query_webhook": "价格查询外部入口",
    "phase7.scan_webhook": "扫描外部入口",
    "phase7.scan_flow_webhook": "扫描后续流程入口",
    "phase7.stats_webhook": "到货统计外部入口",
    "phase7.stats_flow_webhook": "到货统计后续流程入口",
    "automation.feishu_route.arrival_stats": "到货统计飞书入口",
    "automation.feishu_route.arrive_list": "每日到货飞书入口",
    "automation.feishu_route.r7_arrival_checkin": "R7 到达打卡飞书入口",
    "automation.feishu_route.r7_departure_checkin": "R7 发车打卡飞书入口",
    "automation.feishu_route.scan_codes": "扫描飞书入口",
    "automation.feishu_route.self_pickup_problem_upload": "自提问题件飞书入口",
    "automation.feishu_route.send_order": "当日寄件飞书入口",
    "automation.feishu_route.split_pending_problem_upload": "分批未到飞书入口",
    "automation.feishu_route.yunda_dispatch_forecast": "韵达派件预测飞书入口",
    "automation.feishu_route.yunda_send_waybills": "韵达寄件飞书入口",
}

def _plain_role_copy(
    role: str,
    raw_label: object,
    copy: Mapping[str, tuple[str, str]],
    *,
    fallback_label: str,
    fallback_hint: str,
) -> tuple[str, str]:
    friendly = copy.get(role)
    if friendly:
        return friendly
    projected = normalize_feedback_text(redact_text(str(raw_label or "")))[:80]
    if projected and projected != role:
        return projected, fallback_hint
    return fallback_label, fallback_hint


def _resource_display_name(resource_id: str, raw_name: object, *, kind: str = "") -> str:
    name = normalize_feedback_text(redact_text(str(raw_name or "")))[:160]
    return resource_display_name(resource_id, name, kind, AUTOMATION_RESOURCE_DISPLAY_NAMES)


def _configuration_summary(
    account_roles: list[dict[str, Any]],
    resource_roles: list[dict[str, Any]],
) -> str:
    resource_labels: list[str] = []
    for item in resource_roles:
        if item.get("ui_visible") is False:
            continue
        selected_resource_id = str(item.get("selected_resource_id") or "").strip()
        selected_name = ""
        if selected_resource_id:
            for option in item.get("options") or []:
                if str(option.get("resource_id") or "").strip() == selected_resource_id:
                    selected_name = str(option.get("display_name") or "").strip()
                    break
        resource_labels.append(selected_name or str(item.get("label") or "").strip())
    labels = list(
        dict.fromkeys(
            str(item.get("label") or "").strip()
            for item in account_roles
            if str(item.get("label") or "").strip()
        )
    )
    labels.extend(label for label in resource_labels if label and label not in labels)
    if not labels:
        return "无需额外选择账号或表格"
    visible = labels[:4]
    summary = "、".join(visible)
    if len(labels) > len(visible):
        summary += f"等 {len(labels)} 项"
    return summary


def _normalize_browser_plugin_config_value(
    value: Any,
    *,
    depth: int = 0,
) -> tuple[bool, Any]:
    """Accept JSON data only; plugin packages never supply executable UI fragments."""

    if depth > 8:
        return False, None
    if value is None or isinstance(value, (bool, str)):
        if isinstance(value, str) and len(value) > 8192:
            return False, None
        return True, value
    if isinstance(value, int) and not isinstance(value, bool):
        return True, value
    if isinstance(value, float):
        return (math.isfinite(value), value if math.isfinite(value) else None)
    if isinstance(value, list):
        if len(value) > 200:
            return False, None
        normalized_items: list[Any] = []
        for item in value:
            valid, normalized = _normalize_browser_plugin_config_value(
                item, depth=depth + 1
            )
            if not valid:
                return False, None
            normalized_items.append(normalized)
        return True, normalized_items
    if isinstance(value, dict):
        if len(value) > AUTOMATION_PLUGIN_CONFIG_MAX_FIELDS:
            return False, None
        normalized_object: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key or "").strip()
            if not AUTOMATION_PLUGIN_CONFIG_KEY_RE.fullmatch(key):
                return False, None
            valid, normalized = _normalize_browser_plugin_config_value(
                item, depth=depth + 1
            )
            if not valid:
                return False, None
            normalized_object[key] = normalized
        return True, normalized_object
    return False, None


def _normalize_browser_plugin_bindings(value: Any) -> tuple[bool, dict[str, Any]]:
    if not isinstance(value, dict) or len(value) > AUTOMATION_PLUGIN_CONFIG_MAX_FIELDS:
        return False, {}
    normalized: dict[str, Any] = {}
    for raw_role, raw_binding_id in value.items():
        role = str(raw_role or "").strip()
        binding_values = raw_binding_id if isinstance(raw_binding_id, list) else [raw_binding_id]
        binding_ids = [str(item or "").strip() for item in binding_values]
        if (
            not AUTOMATION_PLUGIN_CONFIG_KEY_RE.fullmatch(role)
            or not binding_ids
            or len(binding_ids) > 50
            or len(binding_ids) != len(set(binding_ids))
            or any(not AUTOMATION_PLUGIN_BINDING_ID_RE.fullmatch(item) for item in binding_ids)
        ):
            return False, {}
        normalized[role] = binding_ids if isinstance(raw_binding_id, list) else binding_ids[0]
    return True, normalized


def normalize_automation_project_policy_items(value: Any) -> list[dict[str, Any]]:
    """Return the closed project policy projection; never expose policy hashes or task rows."""

    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            continue
        automation_id = str(raw.get("automation_id") or "").strip()
        configured_mode = str(raw.get("configured_mode") or "").strip().upper()
        effective_mode = str(raw.get("effective_mode") or configured_mode).strip().upper()
        effective_status = str(raw.get("effective_status") or "ACTIVE").strip().upper()
        runnable = raw.get("runnable")
        runtime_status = str(raw.get("runtime_status") or "").strip().upper()
        raw_runtime_reason = raw.get("runtime_reason")
        can_full_auto = raw.get("can_full_auto")
        policy_version = raw.get("policy_version")
        project_configuration_version = raw.get("project_configuration_version")
        if (
            automation_id in seen
            or not AUTOMATION_PROJECT_ID_RE.fullmatch(automation_id)
            or configured_mode not in AUTOMATION_PROJECT_POLICY_MODES
            or effective_mode not in AUTOMATION_PROJECT_EFFECTIVE_MODES
            or effective_status not in AUTOMATION_PROJECT_POLICY_STATUSES
            or not isinstance(runnable, bool)
            or runtime_status not in AUTOMATION_PROJECT_RUNTIME_STATUSES
            or (
                raw_runtime_reason is not None
                and not isinstance(raw_runtime_reason, str)
            )
            or not isinstance(can_full_auto, bool)
            or isinstance(policy_version, bool)
            or not isinstance(policy_version, int)
            or policy_version < 1
            or isinstance(project_configuration_version, bool)
            or not isinstance(project_configuration_version, int)
            or project_configuration_version < 1
        ):
            continue
        seen.add(automation_id)
        normalized.append(
            {
                "automation_id": automation_id,
                "configured_mode": configured_mode,
                "effective_mode": effective_mode,
                "effective_status": effective_status,
                "can_full_auto": can_full_auto,
                "runnable": runnable,
                "runtime_status": runtime_status,
                "runtime_reason": normalize_feedback_text(
                    redact_text(str(raw_runtime_reason or ""))
                )[:160],
                "summary": normalize_feedback_text(
                    redact_text(str(raw.get("summary") or ""))
                )[:300],
                "updated_by": normalize_feedback_text(
                    redact_text(str(raw.get("updated_by") or ""))
                )[:100],
                "updated_at": normalize_feedback_text(
                    redact_text(str(raw.get("updated_at") or ""))
                )[:40],
                # Optimistic concurrency tokens are consumed by JS but never rendered.
                "policy_version": policy_version,
                "project_configuration_version": project_configuration_version,
            }
        )
    return normalized


def build_automation_project_policy_view(
    automation_id: str,
    item: dict[str, Any] | None,
    *,
    load_error: str = "",
) -> dict[str, Any]:
    base = {
        "available": False,
        "automation_id": automation_id,
        "configured_mode": "",
        "effective_mode": "",
        "effective_status": "UNAVAILABLE",
        "can_full_auto": False,
        "runnable": False,
        "runtime_status": "UNAVAILABLE",
        "runtime_reason": "PROJECT_POLICY_UNAVAILABLE",
        "label": "权限状态不可用",
        "summary": load_error or "未取得项目权限，请稍后刷新。",
        "updated_by": "",
        "updated_at": "",
        "policy_version": 0,
        "project_configuration_version": 0,
    }
    if not isinstance(item, dict):
        if not load_error:
            base["summary"] = "未取得该项目权限，后台执行已阻断；请刷新或检查智能服务。"
        return base

    configured_mode = str(item["configured_mode"])
    effective_mode = str(item["effective_mode"])
    effective_status = str(item["effective_status"])
    runtime_status = str(item.get("runtime_status") or "UNAVAILABLE")
    summary = str(item.get("summary") or "").strip()
    if effective_mode == "LEGACY_SCHEDULE_ONLY" or effective_status == "LEGACY_SCHEDULE_ONLY":
        label = "运行权限待确认"
        default_summary = "当前运行权限需要重新确认，请选择项目运行方式。"
    elif effective_mode == "PROJECT_FULL_AUTO":
        if runtime_status == "RECONCILING":
            label = "完全自动，运行环境同步中"
            default_summary = "完全自动权限已保留；同步完成前不会运行旧配置。"
        elif runtime_status != "READY" or effective_status in {
            "UNAVAILABLE",
            "UNSUPPORTED",
        }:
            label = "完全自动，运行环境不可用"
            default_summary = "完全自动权限已保留；运行环境修复前项目不可运行。"
        else:
            label = "完全自动"
            default_summary = "项目清单允许且已启用的定时、后台、飞书与外部验签入口按当前保存配置运行。"
    else:
        if runtime_status == "RECONCILING":
            label = "每次运行审批，运行环境同步中"
            default_summary = "逐次审批权限未变；同步完成前不会运行旧配置。"
        elif runtime_status != "READY":
            label = "每次运行审批，运行环境不可用"
            default_summary = "逐次审批权限未变；运行环境修复前项目不可运行。"
        else:
            label = "每次运行审批"
            default_summary = "定时与 Console 手工执行每次都先进入审批。"

    return {
        **base,
        **item,
        "available": True,
        "label": label,
        "summary": (
            default_summary
            if runtime_status != "READY"
            else summary or default_summary
        ),
        "configured_mode": configured_mode,
        "effective_mode": effective_mode,
        "effective_status": effective_status,
    }


def apply_automation_project_execution_gate(
    task: dict[str, Any],
    policy: dict[str, Any],
) -> None:
    """Combine plugin/entrypoint eligibility with authoritative runtime health."""

    runtime_status = str(policy.get("runtime_status") or "UNAVAILABLE").upper()
    available = bool(policy.get("available"))
    runnable = bool(policy.get("runnable"))
    if available and runtime_status == "READY" and runnable:
        return

    task["can_run_now"] = False
    runtime_reason = str(policy.get("runtime_reason") or "").strip().upper()
    if not available:
        task["run_disabled_reason"] = "项目权限不可用"
    elif runtime_reason == "PROJECT_DISABLED":
        task["run_disabled_reason"] = AUTOMATION_RUNTIME_REASON_LABELS[
            "PROJECT_DISABLED"
        ]
    elif runtime_reason == "PROJECT_CONFIGURATION_INCOMPLETE":
        task["run_disabled_reason"] = AUTOMATION_RUNTIME_REASON_LABELS[
            "PROJECT_CONFIGURATION_INCOMPLETE"
        ]
    elif runtime_reason == "ENTRYPOINTS_DISABLED":
        task["run_disabled_reason"] = AUTOMATION_RUNTIME_REASON_LABELS[
            "ENTRYPOINTS_DISABLED"
        ]
    elif runtime_status == "RECONCILING" or (
        runtime_status == "READY" and runtime_reason.startswith("RECONCILE_")
    ):
        task["run_disabled_reason"] = AUTOMATION_RUNTIME_RECONCILING_LABEL
    elif (
        runtime_reason.endswith("_CONTRACT_UNAVAILABLE")
        or runtime_reason.endswith("_CONTRACT_INVALID")
        or (
            runtime_reason.startswith(("PROJECT_", "PLUGIN_"))
            and runtime_reason
            not in {
                "PROJECT_DISABLED",
                "PROJECT_CONFIGURATION_INCOMPLETE",
                "PROJECT_RUNTIME_UNAVAILABLE",
            }
        )
    ):
        task["run_disabled_reason"] = AUTOMATION_RUNTIME_CONTRACT_ERROR_LABEL
    elif runtime_reason == "PROJECT_RUNTIME_UNAVAILABLE" or runtime_status != "READY":
        task["run_disabled_reason"] = AUTOMATION_RUNTIME_REASON_LABELS[
            "PROJECT_RUNTIME_UNAVAILABLE"
        ]
    elif str(task.get("run_disabled_reason") or "") not in {
        "后台入口已关闭",
        "请到飞书预览并选择运单",
        "当前不可执行",
    }:
        task["run_disabled_reason"] = "项目当前不可运行"


def normalize_automation_pending_approvals(
    value: Any,
    *,
    expected_automation_id: str,
) -> dict[str, Any] | None:
    """Validate one aggregate pending set without accepting approval IDs or plan hashes."""

    if not isinstance(value, dict):
        return None
    automation_id = str(value.get("automation_id") or "").strip()
    pending_count = value.get("pending_count")
    highest_risk = str(value.get("highest_risk") or "").strip().upper()
    source_summary = normalize_feedback_text(
        redact_text(str(value.get("source_summary") or ""))
    )[:240]
    pending_set_hash = str(value.get("pending_set_hash") or "").strip()
    if (
        automation_id != expected_automation_id
        or not AUTOMATION_PROJECT_ID_RE.fullmatch(automation_id)
        or isinstance(pending_count, bool)
        or not isinstance(pending_count, int)
        or pending_count < 0
    ):
        return None
    if pending_count:
        if (
            highest_risk not in AUTOMATION_PENDING_RISK_LABELS
            or not source_summary
            or not AUTOMATION_PENDING_SET_HASH_RE.fullmatch(pending_set_hash)
        ):
            return None
    else:
        highest_risk = ""
        source_summary = ""
        pending_set_hash = ""
    return {
        "automation_id": automation_id,
        "pending_count": pending_count,
        "highest_risk": highest_risk,
        "highest_risk_label": AUTOMATION_PENDING_RISK_LABELS.get(highest_risk, ""),
        "source_summary": source_summary,
        "expected_pending_set_hash": pending_set_hash,
        "can_approve": bool(value.get("can_approve")),
        "can_reject": bool(value.get("can_reject")),
    }


def normalize_automation_approval_batch_result(
    value: Any,
    *,
    expected_automation_id: str,
    expected_decision: str,
) -> dict[str, Any] | None:
    """Project only safe Run/work-item receipts from one atomic decision result."""

    if not isinstance(value, dict):
        return None
    decision = str(value.get("decision") or "").strip().upper()
    decided_count = value.get("decided_count")
    raw_receipts = value.get("run_receipts")
    if (
        decision != expected_decision
        or decision not in {"APPROVED", "REJECTED"}
        or isinstance(decided_count, bool)
        or not isinstance(decided_count, int)
        or decided_count < 0
        or decided_count > AUTOMATION_APPROVAL_BATCH_MAX_RUNS
        or not isinstance(raw_receipts, list)
        or len(raw_receipts) != decided_count
    ):
        return None

    receipts: list[dict[str, Any]] = []
    seen_run_ids: set[str] = set()
    seen_work_item_ids: set[str] = set()
    expected_fields = {"automation_id", "work_item_id", "run_id", "status"}
    for raw in raw_receipts:
        if not isinstance(raw, dict) or set(raw) != expected_fields:
            return None
        automation_id = str(raw.get("automation_id") or "").strip()
        work_item_id = str(raw.get("work_item_id") or "").strip()
        run_id = str(raw.get("run_id") or "").strip()
        status = str(raw.get("status") or "").strip().upper()
        if (
            automation_id != expected_automation_id
            or not AUTOMATION_PROJECT_ID_RE.fullmatch(automation_id)
            or not AUTOMATION_RUN_RECEIPT_ID_RE.fullmatch(work_item_id)
            or not AUTOMATION_RUN_RECEIPT_ID_RE.fullmatch(run_id)
            or status not in AUTOMATION_RUN_RECEIPT_STATUSES
            or run_id in seen_run_ids
            or work_item_id in seen_work_item_ids
        ):
            return None
        seen_run_ids.add(run_id)
        seen_work_item_ids.add(work_item_id)
        receipts.append(
            {
                "automation_id": automation_id,
                "work_item_id": work_item_id,
                "run_id": run_id,
                "status": status,
                "next_poll_after_ms": 1000,
            }
        )
    return {
        "decision": decision,
        "decided_count": decided_count,
        "run_receipts": receipts,
    }


def _normalize_plugin_account_roles(
    value: Any,
    *,
    plugin_id: str = "",
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    roles: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_role in value:
        if not isinstance(raw_role, dict):
            continue
        role = str(raw_role.get("role") or "").strip()
        allowed_systems = raw_role.get("allowed_systems")
        if not isinstance(allowed_systems, list):
            continue
        systems = [
            str(system or "").strip().lower()
            for system in allowed_systems
            if str(system or "").strip().lower() in AUTOMATION_ACCOUNT_SYSTEM_LABELS
        ]
        systems = list(dict.fromkeys(systems))
        if (
            role in seen
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", role)
            or not systems
            or str(raw_role.get("binding_cardinality") or "one") not in {"one", "many"}
        ):
            continue
        seen.add(role)
        role_copy = {
            **AUTOMATION_PLUGIN_ACCOUNT_ROLE_COPY,
            **AUTOMATION_PLUGIN_ACCOUNT_ROLE_COPY_BY_PLUGIN.get(plugin_id, {}),
        }
        label, hint = _plain_role_copy(
            role,
            raw_role.get("label"),
            role_copy,
            fallback_label="业务账号",
            fallback_hint="选择任务实际使用的业务账号。",
        )
        roles.append(
            {
                "role": role,
                "field": role,
                "label": label,
                "hint": hint,
                "technical_name": role,
                "allowed_systems": systems,
                "required": bool(raw_role.get("required", True)),
                "binding_cardinality": (
                    "many"
                    if str(raw_role.get("binding_cardinality") or "one") == "many"
                    else "one"
                ),
            }
        )
    return roles


def _normalize_plugin_resource_roles(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    roles: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_role in value:
        if not isinstance(raw_role, dict):
            continue
        role = str(raw_role.get("role") or "").strip()
        allowed_kinds = raw_role.get("allowed_kinds")
        if not isinstance(allowed_kinds, list):
            continue
        kinds = [
            str(kind or "").strip().lower()
            for kind in allowed_kinds
            if re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", str(kind or "").strip().lower())
        ]
        kinds = list(dict.fromkeys(kinds))
        if role in seen or not AUTOMATION_PLUGIN_CONFIG_KEY_RE.fullmatch(role) or not kinds:
            continue
        seen.add(role)
        label, hint = _plain_role_copy(
            role,
            raw_role.get("label"),
            AUTOMATION_PLUGIN_RESOURCE_ROLE_COPY,
            fallback_label="业务数据",
            fallback_hint="选择这个任务读取或写入的数据位置。",
        )
        roles.append(
            {
                "role": role,
                "label": label,
                "hint": hint,
                "technical_name": role,
                "allowed_kinds": kinds,
                "kind_labels": [
                    AUTOMATION_RESOURCE_KIND_LABELS.get(kind, "业务资源")
                    for kind in kinds
                ],
                "required": bool(raw_role.get("required", True)),
                "ui_visible": not set(kinds) <= {"feishu_route", "webhook_route"},
            }
        )
    return roles


def _normalize_plugin_resources(value: Any) -> tuple[list[dict[str, str]], bool]:
    return normalize_plugin_resources(
        value,
        known_names=AUTOMATION_RESOURCE_DISPLAY_NAMES,
    )


def _plugin_config_value(config: dict[str, Any], path: list[str]) -> tuple[bool, Any]:
    current: Any = config
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return False, None
        current = current[key]
    return True, current


def _normalize_plugin_config_schema(
    schema: Any,
    config: Any,
    *,
    code_owned_fields: frozenset[str] = frozenset(),
) -> tuple[list[dict[str, Any]], bool, str]:
    """Build native form fields from a closed JSON schema; unsupported shapes fail closed."""

    if not isinstance(schema, dict) or not isinstance(config, dict):
        return [], False, "配置 Schema 或当前配置不是对象"
    root_allowed = {
        "$schema",
        "type",
        "title",
        "description",
        "properties",
        "required",
        "additionalProperties",
    }
    if (
        schema.get("type") != "object"
        or schema.get("additionalProperties") is not False
        or not isinstance(schema.get("properties"), dict)
        or set(schema) - root_allowed
    ):
        return [], False, "配置 Schema 不是受支持的闭合对象"
    properties = schema["properties"]
    required = schema.get("required", [])
    if (
        not isinstance(required, list)
        or any(not isinstance(item, str) for item in required)
        or len(required) != len(set(required))
        or not set(required) <= set(properties)
        or set(config) - set(properties)
        or code_owned_fields & set(properties)
    ):
        return [], False, "配置字段或必填声明不一致"

    fields: list[dict[str, Any]] = []

    def walk(node: dict[str, Any], path: list[str], is_required: bool) -> bool:
        if len(fields) >= AUTOMATION_PLUGIN_CONFIG_MAX_FIELDS:
            return False
        node_type = node.get("type")
        if node_type == "object":
            allowed = {
                "type",
                "title",
                "description",
                "properties",
                "required",
                "additionalProperties",
            }
            children = node.get("properties")
            child_required = node.get("required", [])
            if (
                set(node) - allowed
                or node.get("additionalProperties") is not False
                or not isinstance(children, dict)
                or not isinstance(child_required, list)
                or any(not isinstance(item, str) for item in child_required)
                or len(child_required) != len(set(child_required))
                or not set(child_required) <= set(children)
            ):
                return False
            present, current = _plugin_config_value(config, path)
            if present and (not isinstance(current, dict) or set(current) - set(children)):
                return False
            for key, child in children.items():
                if (
                    not AUTOMATION_PLUGIN_CONFIG_KEY_RE.fullmatch(str(key))
                    or not isinstance(child, dict)
                    or not walk(child, [*path, str(key)], str(key) in child_required)
                ):
                    return False
            return True

        allowed = {
            "type",
            "title",
            "description",
            "enum",
            "format",
            "minimum",
            "maximum",
            "minLength",
            "maxLength",
            "items",
        }
        if set(node) - allowed or node_type not in {"string", "integer", "number", "boolean", "array"}:
            return False
        present, value = _plugin_config_value(config, path)
        enum = node.get("enum")
        if enum is not None and (
            not isinstance(enum, list)
            or not enum
            or len(enum) > 100
            or any(isinstance(item, (dict, list)) or item is None for item in enum)
        ):
            return False
        if node_type == "array":
            items = node.get("items")
            if (
                not isinstance(items, dict)
                or set(items) - {"type"}
                or items.get("type") not in {"string", "integer", "number"}
                or enum is not None
                or (present and not isinstance(value, list))
            ):
                return False
            kind = "list"
        elif node_type == "boolean":
            if present and not isinstance(value, bool):
                return False
            kind = "checkbox"
        elif node_type in {"integer", "number"}:
            if present and (isinstance(value, bool) or not isinstance(value, (int, float))):
                return False
            kind = "number"
        else:
            if present and not isinstance(value, str):
                return False
            kind = "select" if enum is not None else "text"
        key = path[-1]
        secret = str(node.get("format") or "").lower() == "password" or key.lower() in AUTOMATION_SECRET_FIELD_NAMES or key.lower().endswith("_token")
        raw_label = normalize_feedback_text(redact_text(str(node.get("title") or key)))[:100]
        raw_hint = normalize_feedback_text(redact_text(str(node.get("description") or "")))[:240]
        friendly_label, friendly_hint = AUTOMATION_PLUGIN_CONFIG_COPY.get(
            key, (raw_label, raw_hint)
        )
        fields.append(
            {
                "path": ".".join(path),
                "label": friendly_label,
                "hint": friendly_hint,
                "technical_name": key,
                "advanced": key not in AUTOMATION_PLUGIN_COMMON_CONFIG_KEYS,
                "kind": kind,
                "value": "" if secret or not present else value,
                "present": present and not secret,
                "required": is_required,
                "secret": secret,
                "enum": enum or [],
                "step": "1" if node_type == "integer" else "any",
                "item_type": str((node.get("items") or {}).get("type") or ""),
                "minimum": node.get("minimum"),
                "maximum": node.get("maximum"),
                "min_length": node.get("minLength"),
                "max_length": node.get("maxLength"),
            }
        )
        return True

    for key, node in properties.items():
        if str(key) in code_owned_fields:
            continue
        if (
            not AUTOMATION_PLUGIN_CONFIG_KEY_RE.fullmatch(str(key))
            or not isinstance(node, dict)
            or not walk(node, [str(key)], str(key) in required)
        ):
            return [], False, f"配置字段 {key} 使用了不支持的 Schema"
    return fields, True, ""


def _normalize_plugin_code_owned_config_fields(
    value: Any,
    schema: Any,
) -> tuple[frozenset[str], bool]:
    """Accept only closed field names already removed from the browser schema."""

    if value is None:
        return frozenset(), True
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return frozenset(), False
    normalized = [str(item).strip() for item in value]
    properties = schema.get("properties") if isinstance(schema, dict) else None
    valid = bool(
        isinstance(properties, dict)
        and len(normalized) == len(set(normalized))
        and all(
            field
            and len(field) <= 128
            and "." not in field
            and AUTOMATION_PLUGIN_CODE_OWNED_CONFIG_KEY_RE.fullmatch(field)
            and field not in properties
            for field in normalized
        )
    )
    return (frozenset(normalized), True) if valid else (frozenset(), False)


def _normalize_plugin_entrypoints(
    value: Any,
    *,
    runtime_model: str = "ACTION_V1",
) -> tuple[list[str], bool]:
    if not isinstance(value, list):
        return [], False
    normalized = [str(item or "").strip().lower() for item in value]
    if len(normalized) != len(set(normalized)) or len(normalized) > 100:
        return [], False
    if runtime_model == "SERVICE_V2":
        if any(
            not AUTOMATION_PLUGIN_V2_ENTRYPOINT_ID_RE.fullmatch(item)
            for item in normalized
        ):
            return [], False
    elif runtime_model == "ACTION_V1":
        if not set(normalized) <= AUTOMATION_PLUGIN_ENTRYPOINTS:
            return [], False
    else:
        return [], False
    return normalized, True


def _normalize_plugin_entrypoint_kinds(
    value: Any,
    *,
    runtime_model: str,
    entrypoints: list[str],
) -> tuple[dict[str, str], bool]:
    if runtime_model == "ACTION_V1":
        return {entrypoint: entrypoint for entrypoint in entrypoints}, True
    if runtime_model != "SERVICE_V2":
        return {}, False
    if not isinstance(value, Mapping) or set(value) != set(entrypoints):
        return {}, False
    normalized: dict[str, str] = {}
    for entrypoint in entrypoints:
        kind = str(value.get(entrypoint) or "").strip().lower()
        if kind not in AUTOMATION_PLUGIN_V2_ENTRYPOINT_KINDS:
            return {}, False
        normalized[entrypoint] = kind
    return normalized, True


def _normalize_plugin_scheduling(
    value: Any,
    entrypoints: list[str],
    entrypoint_kinds: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], bool]:
    if (
        not isinstance(value, dict)
        or set(value) != {"supported", "allowed_kinds", "max_daily_times"}
        or not isinstance(value.get("supported"), bool)
    ):
        return {"supported": False, "allowed_kinds": [], "max_daily_times": 0}, False
    supported = value["supported"]
    raw_kinds = value.get("allowed_kinds")
    max_daily_times = value.get("max_daily_times")
    if (
        not isinstance(raw_kinds, list)
        or any(kind not in {"daily_times", "startup"} for kind in raw_kinds)
        or len(raw_kinds) != len(set(raw_kinds))
        or isinstance(max_daily_times, bool)
        or not isinstance(max_daily_times, int)
    ):
        return {"supported": False, "allowed_kinds": [], "max_daily_times": 0}, False
    kinds = entrypoint_kinds or {entrypoint: entrypoint for entrypoint in entrypoints}
    valid = supported == any(kinds.get(entrypoint) == "scheduler" for entrypoint in entrypoints)
    if supported:
        valid = valid and bool(raw_kinds) and 1 <= max_daily_times <= 96
    else:
        valid = valid and not raw_kinds and max_daily_times == 0
    return {
        "supported": supported,
        "allowed_kinds": list(raw_kinds),
        "max_daily_times": max_daily_times,
    }, valid


def _normalize_plugin_schedule(
    value: Any,
    scheduling: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    fallback = {"kind": "none", "times": [], "enabled": False}
    if not isinstance(value, dict) or set(value) != {"kind", "times", "enabled"}:
        return fallback, False
    kind = str(value.get("kind") or "").strip().lower()
    times = value.get("times")
    enabled = value.get("enabled")
    if not isinstance(times, list) or not isinstance(enabled, bool):
        return fallback, False
    normalized_times = [str(item or "").strip() for item in times]
    if (
        kind not in {"none", "daily_times", "startup"}
        or len(normalized_times) != len(set(normalized_times))
        or any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", item) for item in normalized_times)
    ):
        return fallback, False
    if kind == "none":
        return fallback, not normalized_times and not enabled
    if scheduling.get("supported") is not True or kind not in scheduling.get("allowed_kinds", []):
        return fallback, False
    if kind == "startup":
        return {"kind": "startup", "times": [], "enabled": enabled}, not normalized_times
    max_daily_times = int(scheduling.get("max_daily_times") or 0)
    valid = bool(normalized_times) and len(normalized_times) <= max_daily_times
    return {
        "kind": "daily_times",
        "times": sorted(normalized_times),
        "enabled": enabled,
    }, valid


def _normalize_plugin_binding_map(
    value: Any,
    roles: list[dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    if not isinstance(value, dict):
        return {}, False
    declared = {str(role.get("role") or "") for role in roles}
    roles_by_id = {str(role.get("role") or ""): role for role in roles}
    normalized: dict[str, Any] = {}
    valid = set(value) <= declared
    for raw_role, raw_binding_id in value.items():
        role = str(raw_role or "").strip()
        role_definition = roles_by_id.get(role, {})
        many = role_definition.get("binding_cardinality") == "many"
        binding_values = raw_binding_id if isinstance(raw_binding_id, list) else [raw_binding_id]
        binding_ids = [str(item or "").strip() for item in binding_values]
        if (
            role not in declared
            or not AUTOMATION_PLUGIN_CONFIG_KEY_RE.fullmatch(role)
            or (not many and len(binding_ids) != 1)
            or not binding_ids
            or len(binding_ids) != len(set(binding_ids))
            or any(not AUTOMATION_PLUGIN_BINDING_ID_RE.fullmatch(item) for item in binding_ids)
        ):
            valid = False
            continue
        normalized[role] = binding_ids if many else binding_ids[0]
    return normalized, valid and len(normalized) == len(value)


def _normalize_plugin_runtime_model(
    value: Any,
    *,
    present: bool | None = None,
) -> str:
    """Normalize an explicit runtime discriminator without silently downgrading it.

    Catalogs emitted before runtime-model metadata existed are the only records
    that may be projected as ``ACTION_V1``.  A present-but-unknown discriminator
    is kept as an unsupported contract so that a newer runtime can never be
    mistaken for a legacy action package.
    """

    if present is None:
        present = value is not None
    if not present:
        return "ACTION_V1"
    normalized = str(value or "").strip().upper()
    if normalized in {"ACTION_V1", "SERVICE_V2"}:
        return normalized
    return AUTOMATION_PLUGIN_UNSUPPORTED_RUNTIME_MODEL


def _normalize_plugin_api(value: Any, *, runtime_model: str) -> str:
    fallback = "1.0.0" if runtime_model == "ACTION_V1" else ""
    if not isinstance(value, str):
        return fallback
    normalized = value.strip()
    return normalized if AUTOMATION_PLUGIN_API_RE.fullmatch(normalized) else fallback


def _normalize_plugin_semver(value: Any) -> str:
    normalized = str(value or "").strip()
    return normalized if AUTOMATION_PLUGIN_VERSION_RE.fullmatch(normalized) else ""


def _normalize_plugin_versions(
    raw: Mapping[str, Any],
    *,
    runtime_model: str,
    version: str,
    fallback: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    fallback = fallback or {}
    active_version = _normalize_plugin_semver(
        raw.get("active_version", fallback.get("active_version"))
    )
    target_version = _normalize_plugin_semver(
        raw.get("target_version", fallback.get("target_version"))
    )
    if runtime_model == "ACTION_V1":
        return active_version or version, target_version or version
    if runtime_model == AUTOMATION_PLUGIN_UNSUPPORTED_RUNTIME_MODEL:
        return "", ""
    return active_version, target_version or version


def _normalize_plugin_provided_services(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    services: list[str] = []
    for raw in value:
        candidate = raw.get("service") if isinstance(raw, Mapping) else raw
        service = str(candidate or "").strip()
        if not AUTOMATION_PLUGIN_SERVICE_RE.fullmatch(service):
            continue
        if service not in services:
            services.append(service)
        if len(services) >= 20:
            break
    return services


def _normalize_plugin_dependency_state(
    value: Any,
    *,
    runtime_model: str,
    state_hint: str = "",
) -> str:
    if runtime_model == "ACTION_V1":
        return "NOT_APPLICABLE"
    if runtime_model == AUTOMATION_PLUGIN_UNSUPPORTED_RUNTIME_MODEL:
        return "UNKNOWN"
    normalized = str(value or "").strip().upper()
    if normalized in set(AUTOMATION_PLUGIN_DEPENDENCY_STATE_LABELS) - {"NOT_APPLICABLE"}:
        return normalized
    hinted = str(state_hint or "").strip().upper()
    if hinted in {
        "BLOCKED_DEPENDENCY",
        "NEEDS_CONFIGURATION",
        "BLOCKED_LOGIN",
    }:
        return hinted
    return "UNKNOWN"


def _normalize_plugin_blocking_reasons(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    reasons: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in value:
        code = ""
        service = ""
        message = ""
        if isinstance(raw, Mapping):
            raw_code = str(raw.get("code") or "").strip().upper()
            if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", raw_code):
                code = raw_code
            raw_service = str(raw.get("service") or "").strip()
            if AUTOMATION_PLUGIN_SERVICE_RE.fullmatch(raw_service):
                service = raw_service
            message = normalize_feedback_text(
                redact_text(str(raw.get("message") or ""))
            )[:240]
        elif isinstance(raw, str):
            raw_code = raw.strip().upper()
            if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", raw_code):
                code = raw_code
            message = normalize_feedback_text(redact_text(raw))[:240]
        else:
            continue
        label = AUTOMATION_PLUGIN_BLOCK_REASON_COPY.get(code, "") or message
        if service and label:
            label = f"{label}：{service}"
        if not label:
            continue
        identity = (code, service, label)
        if identity in seen:
            continue
        seen.add(identity)
        reasons.append({"code": code, "service": service, "label": label})
        if len(reasons) >= 20:
            break
    return reasons


def _normalize_plugin_migration(
    value: Any,
    *,
    automation_id: str,
) -> dict[str, Any]:
    if isinstance(value, str):
        raw: Mapping[str, Any] = {"state": value}
    elif isinstance(value, Mapping):
        raw = value
    else:
        return {}
    state = str(raw.get("state") or raw.get("status") or "").strip().upper()
    if state not in AUTOMATION_PLUGIN_MIGRATION_STATE_LABELS:
        return {}
    pair_id = str(raw.get("migration_pair_id") or raw.get("pair_id") or "").strip()
    if not AUTOMATION_PLUGIN_MIGRATION_PAIR_ID_RE.fullmatch(pair_id):
        pair_id = ""
    source_id = str(raw.get("source_automation_id") or "").strip()
    target_id = str(raw.get("target_automation_id") or "").strip()
    counterpart_id = str(raw.get("counterpart_automation_id") or "").strip()
    owner_id = str(raw.get("entrypoint_owner_automation_id") or "").strip()
    source_id = source_id if AUTOMATION_PROJECT_ID_RE.fullmatch(source_id) else ""
    target_id = target_id if AUTOMATION_PROJECT_ID_RE.fullmatch(target_id) else ""
    counterpart_id = (
        counterpart_id
        if AUTOMATION_PROJECT_ID_RE.fullmatch(counterpart_id)
        else ""
    )
    owner_id = owner_id if AUTOMATION_PROJECT_ID_RE.fullmatch(owner_id) else ""
    projected_role = str(raw.get("role") or "").strip().upper()
    role = (
        "TARGET"
        if automation_id and automation_id == target_id
        else "SOURCE"
        if automation_id and automation_id == source_id
        else projected_role
        if projected_role in {"SOURCE", "TARGET"}
        else ""
    )
    paired_automation_id = (
        source_id
        if role == "TARGET" and source_id
        else target_id
        if role == "SOURCE" and target_id
        else counterpart_id
    )
    if not owner_id:
        owner_id = (
            automation_id
            if (state in {"CUTOVER", "COMPLETED"} and role == "TARGET")
            or (state not in {"CUTOVER", "COMPLETED"} and role == "SOURCE")
            else paired_automation_id
        )
    record_version = raw.get("record_version")
    if (
        isinstance(record_version, bool)
        or not isinstance(record_version, int)
        or record_version < 1
    ):
        record_version = 0
    test_state = str(
        raw.get("test_state") or raw.get("test_status") or ""
    ).strip().upper()
    if test_state not in AUTOMATION_PLUGIN_MIGRATION_TEST_STATE_LABELS:
        test_state = ""
    return {
        "migration_pair_id": pair_id,
        "state": state,
        "status_label": AUTOMATION_PLUGIN_MIGRATION_STATE_LABELS[state],
        "role": role,
        "paired_automation_id": paired_automation_id,
        "entrypoint_owner_automation_id": owner_id,
        "owns_entrypoints": bool(owner_id and owner_id == automation_id),
        "record_version": record_version,
        "can_mark_ready": bool(role == "TARGET" and state == "TESTING"),
        "can_cutover": bool(role == "TARGET" and state == "READY"),
        "can_rollback": bool(role == "TARGET" and state == "CUTOVER"),
        "can_complete": bool(role == "TARGET" and state == "CUTOVER"),
        "test_state": test_state,
        "test_status_label": AUTOMATION_PLUGIN_MIGRATION_TEST_STATE_LABELS.get(
            test_state, ""
        ),
    }


AUTOMATION_PLUGIN_MISSING_REQUIREMENT_LABELS = {
    "project_config": "项目配置未完整",
    "account_binding": "必需账号尚未绑定",
    "resource_binding": "必需资源尚未绑定",
    "device_binding": "指定的工作节点尚未绑定",
}


def normalize_automation_plugin_catalog(
    value: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Join safe action-package metadata to safe project instances."""

    if not isinstance(value, dict):
        return [], [], []
    raw_packages = value.get("plugins")
    raw_instances = value.get("instances")
    if not isinstance(raw_packages, list):
        raw_packages = []
    if not isinstance(raw_instances, list):
        raw_instances = value.get("items") if isinstance(value.get("items"), list) else []
    resources, resources_valid = _normalize_plugin_resources(value.get("resources"))
    resource_pool_problem = str(value.get("resource_pool_problem") or "").strip().upper()
    if resource_pool_problem and resource_pool_problem not in RESOURCE_PROBLEM_LABELS:
        resources_valid = False
        resource_pool_problem = "RESOURCE_CATALOG_UNAVAILABLE"
    resource_pool_available = (
        value.get("resource_pool_available") is True and resources_valid
    )

    packages: list[dict[str, Any]] = []
    packages_by_id: dict[str, dict[str, Any]] = {}
    for raw in raw_packages:
        if not isinstance(raw, dict):
            continue
        plugin_id = str(raw.get("plugin_id") or "").strip()
        version = str(raw.get("version") or "").strip()
        platform = str(raw.get("execution_platform") or "").strip().lower()
        if (
            plugin_id in packages_by_id
            or not AUTOMATION_PLUGIN_ID_RE.fullmatch(plugin_id)
            or not AUTOMATION_PLUGIN_VERSION_RE.fullmatch(version)
            or platform not in {"server", "windows"}
        ):
            continue
        raw_account_roles = raw.get("account_roles")
        raw_resource_roles = raw.get("resource_roles")
        account_roles = _normalize_plugin_account_roles(
            raw_account_roles,
            plugin_id=plugin_id,
        )
        resource_roles = _normalize_plugin_resource_roles(raw_resource_roles)
        roles_valid = (
            isinstance(raw_account_roles, list)
            and len(account_roles) == len(raw_account_roles)
            and isinstance(raw_resource_roles, list)
            and len(resource_roles) == len(raw_resource_roles)
        )
        runtime_model = _normalize_plugin_runtime_model(
            raw.get("runtime_model"), present="runtime_model" in raw
        )
        entrypoints, entrypoints_valid = _normalize_plugin_entrypoints(
            raw.get("entrypoints"), runtime_model=runtime_model
        )
        entrypoint_kinds, entrypoint_kinds_valid = _normalize_plugin_entrypoint_kinds(
            raw.get("entrypoint_kinds"),
            runtime_model=runtime_model,
            entrypoints=entrypoints,
        )
        scheduling, scheduling_valid = _normalize_plugin_scheduling(
            raw.get("scheduling"), entrypoints, entrypoint_kinds
        )
        contract_supported = (
            entrypoints_valid
            and entrypoint_kinds_valid
            and scheduling_valid
            and roles_valid
        )
        plugin_api = _normalize_plugin_api(
            raw.get("plugin_api"), runtime_model=runtime_model
        )
        active_version, target_version = _normalize_plugin_versions(
            raw,
            runtime_model=runtime_model,
            version=version,
        )
        dependency_state = _normalize_plugin_dependency_state(
            raw.get("dependency_state"),
            runtime_model=runtime_model,
            state_hint=str(raw.get("state") or ""),
        )
        provided_services = _normalize_plugin_provided_services(
            raw.get("provided_services")
        )
        blocking_reasons = _normalize_plugin_blocking_reasons(
            raw.get("blocking_reasons")
        )
        if runtime_model == AUTOMATION_PLUGIN_UNSUPPORTED_RUNTIME_MODEL:
            # Do not expose untrusted future contract details as if they were
            # usable services or v2 metadata.  The package remains visible with
            # an explicit unsupported runtime label and a blocked contract.
            provided_services = []
            blocking_reasons = []
        package = {
            "plugin_id": plugin_id,
            "name": normalize_feedback_text(redact_text(str(raw.get("name") or plugin_id)))[:120],
            "version": version,
            "execution_platform": platform,
            "platform_label": "Windows 设备" if platform == "windows" else "服务器",
            "can_schedule": bool(scheduling["supported"]) if contract_supported else False,
            "worker_required": bool(raw.get("worker_required")) or platform == "windows",
            "action_summary": normalize_feedback_text(
                redact_text(str(raw.get("action_summary") or raw.get("description") or ""))
            )[:240],
            "resource_summary": normalize_feedback_text(
                redact_text(str(raw.get("resource_summary") or ""))
            )[:200],
            "account_roles": account_roles,
            "resource_roles": resource_roles,
            "configuration_summary": _configuration_summary(
                account_roles, resource_roles
            ),
            "scheduling": scheduling,
            "entrypoints": entrypoints,
            "entrypoint_kinds": entrypoint_kinds,
            "contract_supported": contract_supported,
            "runtime_model": runtime_model,
            "runtime_model_label": AUTOMATION_PLUGIN_RUNTIME_MODEL_LABELS[
                runtime_model
            ],
            "plugin_api": plugin_api,
            "active_version": active_version,
            "target_version": target_version,
            "dependency_state": dependency_state,
            "dependency_state_label": AUTOMATION_PLUGIN_DEPENDENCY_STATE_LABELS[
                dependency_state
            ],
            "provided_services": provided_services,
            "blocking_reasons": blocking_reasons,
            "blocking_reason_labels": [
                reason["label"] for reason in blocking_reasons
            ],
            "migration": _normalize_plugin_migration(
                raw.get("migration"), automation_id=""
            ),
        }
        packages.append(package)
        packages_by_id[plugin_id] = package

    instances: list[dict[str, Any]] = []
    seen_instances: set[str] = set()
    for raw in raw_instances:
        if not isinstance(raw, dict):
            continue
        automation_id = str(raw.get("automation_id") or "").strip()
        plugin_id = str(raw.get("plugin_id") or automation_id).strip()
        package = packages_by_id.get(plugin_id, {})
        version = str(raw.get("version") or package.get("version") or "").strip()
        platform = str(
            raw.get("execution_platform") or package.get("execution_platform") or ""
        ).strip().lower()
        enabled = raw.get("enabled")
        configured = raw.get("configured")
        record_version = raw.get("record_version")
        project_configuration_version = raw.get("project_configuration_version", 0)
        runtime_model = _normalize_plugin_runtime_model(
            raw["runtime_model"] if "runtime_model" in raw else package.get("runtime_model"),
            present="runtime_model" in raw or bool(package),
        )
        plugin_api = _normalize_plugin_api(
            raw.get("plugin_api", package.get("plugin_api")),
            runtime_model=runtime_model,
        )
        active_version, target_version = _normalize_plugin_versions(
            raw,
            runtime_model=runtime_model,
            version=version,
            fallback=package,
        )
        provided_services = (
            _normalize_plugin_provided_services(raw.get("provided_services"))
            if "provided_services" in raw
            else list(package.get("provided_services") or [])
        )
        blocking_reasons = (
            _normalize_plugin_blocking_reasons(raw.get("blocking_reasons"))
            if "blocking_reasons" in raw
            else list(package.get("blocking_reasons") or [])
        )
        if runtime_model == AUTOMATION_PLUGIN_UNSUPPORTED_RUNTIME_MODEL:
            provided_services = []
            blocking_reasons = []
        migration = _normalize_plugin_migration(
            raw.get("migration", package.get("migration")),
            automation_id=automation_id,
        )
        projected_state = str(
            raw.get("state") or ("ENABLED" if enabled else "DISABLED")
        ).strip().upper()
        project_state = (
            projected_state
            if projected_state in AUTOMATION_PLUGIN_STATE_LABELS
            else "UNKNOWN"
        )
        projected_reconcile_state = str(raw.get("reconcile_state") or "").strip().upper()
        reconcile_state = (
            projected_reconcile_state
            if projected_reconcile_state in AUTOMATION_PLUGIN_RECONCILE_STATES
            else "UNKNOWN"
        )
        dependency_state = _normalize_plugin_dependency_state(
            raw.get("dependency_state", package.get("dependency_state")),
            runtime_model=runtime_model,
            state_hint=projected_state,
        )
        state = project_state
        if runtime_model == AUTOMATION_PLUGIN_UNSUPPORTED_RUNTIME_MODEL:
            state = "UNKNOWN"
        elif project_state in AUTOMATION_PLUGIN_STABLE_STATES and reconcile_state != "STABLE":
            state = AUTOMATION_PLUGIN_RECONCILE_DISPLAY_STATES.get(
                reconcile_state,
                "UNKNOWN",
            )
        elif (
            project_state in AUTOMATION_PLUGIN_STABLE_STATES
            and runtime_model == "SERVICE_V2"
            and dependency_state in AUTOMATION_PLUGIN_DEPENDENCY_BLOCKING_STATES
        ):
            state = dependency_state
        if (
            automation_id in seen_instances
            or not AUTOMATION_PROJECT_ID_RE.fullmatch(automation_id)
            or not AUTOMATION_PLUGIN_ID_RE.fullmatch(plugin_id)
            or not AUTOMATION_PLUGIN_VERSION_RE.fullmatch(version)
            or platform not in {"server", "windows"}
            or not isinstance(enabled, bool)
            or not isinstance(configured, bool)
            or isinstance(record_version, bool)
            or not isinstance(record_version, int)
            or record_version < 1
            or isinstance(project_configuration_version, bool)
            or not isinstance(project_configuration_version, int)
            or project_configuration_version < 1
        ):
            continue
        raw_account_roles = raw.get("account_roles")
        raw_resource_roles = raw.get("resource_roles")
        account_roles = (
            _normalize_plugin_account_roles(
                raw_account_roles,
                plugin_id=plugin_id,
            )
            if isinstance(raw_account_roles, list)
            else list(package.get("account_roles") or [])
        )
        resource_roles = (
            _normalize_plugin_resource_roles(raw_resource_roles)
            if isinstance(raw_resource_roles, list)
            else list(package.get("resource_roles") or [])
        )
        roles_valid = (
            (not isinstance(raw_account_roles, list) or len(account_roles) == len(raw_account_roles))
            and (
                not isinstance(raw_resource_roles, list)
                or len(resource_roles) == len(raw_resource_roles)
            )
            and bool(package.get("contract_supported", False))
            and account_roles == list(package.get("account_roles") or [])
            and resource_roles == list(package.get("resource_roles") or [])
        )
        entrypoints, entrypoints_valid = _normalize_plugin_entrypoints(
            raw.get("entrypoints", package.get("entrypoints")),
            runtime_model=runtime_model,
        )
        entrypoint_kinds, entrypoint_kinds_valid = _normalize_plugin_entrypoint_kinds(
            raw.get("entrypoint_kinds", package.get("entrypoint_kinds")),
            runtime_model=runtime_model,
            entrypoints=entrypoints,
        )
        scheduling, scheduling_valid = _normalize_plugin_scheduling(
            raw.get("scheduling", package.get("scheduling")),
            entrypoints,
            entrypoint_kinds,
        )
        schedule, schedule_valid = _normalize_plugin_schedule(
            raw.get("schedule"), scheduling
        )
        code_owned_config_fields, code_owned_config_fields_valid = (
            _normalize_plugin_code_owned_config_fields(
                raw.get("code_owned_config_fields"),
                raw.get("config_schema"),
            )
        )
        config_fields, config_schema_supported, config_schema_error = (
            _normalize_plugin_config_schema(
                raw.get("config_schema"),
                raw.get("config"),
                code_owned_fields=code_owned_config_fields,
            )
        )
        account_bindings, account_bindings_valid = _normalize_plugin_binding_map(
            raw.get("account_bindings"), account_roles
        )
        resource_bindings, resource_bindings_valid = _normalize_plugin_binding_map(
            raw.get("resource_bindings"), resource_roles
        )
        resource_role_bindings: list[dict[str, Any]] = []
        resource_bindings_ready = resource_bindings_valid
        for role_definition in resource_roles:
            role = str(role_definition["role"])
            selected_resource_id = str(resource_bindings.get(role) or "").strip()
            allowed_kinds = set(role_definition.get("allowed_kinds") or [])
            options = [
                dict(resource)
                for resource in resources
                if resource["kind"] in allowed_kinds
                and resource["status"] == "available"
            ]
            options.sort(
                key=lambda item: (
                    item["resource_id"] != selected_resource_id,
                    str(item.get("display_name") or ""),
                    item["resource_id"],
                )
            )
            selected_available = bool(
                selected_resource_id
                and any(
                    option["resource_id"] == selected_resource_id
                    for option in options
                )
            )
            selected_resource = next(
                (
                    resource
                    for resource in resources
                    if resource["resource_id"] == selected_resource_id
                ),
                None,
            )
            blocked_reason = ""
            if not resource_pool_available and not options:
                blocked_reason = (
                    RESOURCE_PROBLEM_LABELS.get(resource_pool_problem, "")
                    or "表格列表暂时无法读取，请稍后刷新"
                )
            elif selected_resource_id and not selected_available:
                blocked_reason = (
                    str((selected_resource or {}).get("problem_label") or "")
                    or "之前选择的数据位置已停用或无法使用，请重新选择"
                )
            elif bool(role_definition.get("required")) and not selected_resource_id:
                blocked_reason = (
                    RESOURCE_PROBLEM_LABELS.get(resource_pool_problem, "")
                    if not options and resource_pool_problem
                    else f"请选择{role_definition['label']}"
                )
            if blocked_reason and role_definition.get("ui_visible") is not False:
                resource_bindings_ready = False
            resource_role_bindings.append(
                {
                    **role_definition,
                    "selected_resource_id": selected_resource_id,
                    "selected_available": selected_available,
                    "options": options,
                    "blocked_reason": blocked_reason,
                }
            )
        enabled_entrypoints, enabled_entrypoints_valid = _normalize_plugin_entrypoints(
            raw.get("enabled_entrypoints"), runtime_model=runtime_model
        )
        if enabled_entrypoints_valid and not set(enabled_entrypoints) <= set(entrypoints):
            enabled_entrypoints_valid = False
            enabled_entrypoints = []
        active_contributions: list[dict[str, Any]] = []
        contribution_projection_state = "UNKNOWN"
        if runtime_model == "SERVICE_V2":
            (
                active_contributions,
                contribution_projection_state,
            ) = _normalize_plugin_active_contributions(
                raw.get("active_contributions"),
                projection_state=raw.get("contribution_projection_state"),
                committed_generation=raw.get("committed_generation"),
                entrypoints=entrypoints,
                entrypoint_kinds=entrypoint_kinds,
                enabled_entrypoints=enabled_entrypoints,
            )
            active_contribution_ids = {
                str(contribution["contribution_id"])
                for contribution in active_contributions
            }
            console_entrypoints = [
                entrypoint
                for entrypoint in entrypoints
                if entrypoint in active_contribution_ids
                and entrypoint_kinds.get(entrypoint) == "console"
            ]
            enabled_console_entrypoints = list(console_entrypoints)
            enabled_entrypoint_kinds = sorted(
                {
                    str(contribution["contribution_kind"])
                    for contribution in active_contributions
                }
            )
        else:
            console_entrypoints = [
                entrypoint
                for entrypoint in entrypoints
                if entrypoint_kinds.get(entrypoint) == "console"
            ]
            enabled_console_entrypoints = [
                entrypoint
                for entrypoint in enabled_entrypoints
                if entrypoint_kinds.get(entrypoint) == "console"
            ]
            enabled_entrypoint_kinds = sorted(
                {
                    entrypoint_kinds[entrypoint]
                    for entrypoint in enabled_entrypoints
                    if entrypoint in entrypoint_kinds
                }
            )
        raw_device = raw.get("device") if isinstance(raw.get("device"), dict) else None
        device = None
        if raw_device is not None:
            device_id = str(
                raw_device.get("id") or raw_device.get("device_id") or ""
            ).strip()
            if AUTOMATION_WORKER_ID_RE.fullmatch(device_id):
                device = {
                    "device_id": device_id,
                    "name": normalize_feedback_text(
                        redact_text(str(raw_device.get("name") or device_id))
                    )[:120],
                    "state": normalize_feedback_text(
                        str(raw_device.get("state") or raw_device.get("status") or "")
                    )[:40],
                    "online": bool(raw_device.get("online")),
                }
        missing_requirements = [
            AUTOMATION_PLUGIN_MISSING_REQUIREMENT_LABELS.get(
                str(item), normalize_feedback_text(redact_text(str(item)))[:120]
            )
            for item in raw.get("missing_requirements", [])
            if isinstance(item, str) and item.strip()
        ][:20]
        blocking_reason_labels = [
            str(reason["label"]) for reason in blocking_reasons
        ]
        if (
            runtime_model == "SERVICE_V2"
            and dependency_state in AUTOMATION_PLUGIN_DEPENDENCY_BLOCKING_STATES
            and not blocking_reason_labels
        ):
            blocking_reason_labels = [
                {
                    "BLOCKED_DEPENDENCY": "依赖服务尚未就绪",
                    "NEEDS_CONFIGURATION": "项目配置未完整",
                    "BLOCKED_LOGIN": "必需账号尚未登录",
                    "UNKNOWN": "v2 依赖状态无法确认",
                }[dependency_state]
            ]
        missing_requirements.extend(blocking_reason_labels)
        projection_warnings: list[str] = []
        if not entrypoints_valid or not scheduling_valid:
            projection_warnings.append("插件入口合同不可识别")
        if runtime_model == AUTOMATION_PLUGIN_UNSUPPORTED_RUNTIME_MODEL:
            projection_warnings.append("插件运行时模型不受支持")
        if not entrypoint_kinds_valid:
            projection_warnings.append("插件入口类型映射不可识别")
        if not schedule_valid:
            projection_warnings.append("项目定时投影无效")
        if not roles_valid:
            projection_warnings.append("插件账号或资源角色合同不可识别")
        if not config_schema_supported:
            projection_warnings.append(config_schema_error or "配置 Schema 不受支持")
        if not code_owned_config_fields_valid:
            projection_warnings.append("代码拥有配置字段投影无效")
        if not account_bindings_valid:
            projection_warnings.append("账号绑定投影无效")
        if not resource_bindings_valid:
            projection_warnings.append("资源绑定投影无效")
        projection_warnings.extend(
            str(binding["blocked_reason"])
            for binding in resource_role_bindings
            if binding.get("blocked_reason") and binding.get("ui_visible") is not False
        )
        if not enabled_entrypoints_valid:
            projection_warnings.append("运行入口配置无效")
        missing_requirements = list(
            dict.fromkeys([*missing_requirements, *projection_warnings])
        )[:20]
        if not configured:
            missing_config_fields = [
                str(field["label"])
                for field in config_fields
                if field.get("required") and not field.get("present")
            ]
            if missing_config_fields:
                missing_requirements.append(
                    "缺少必填配置：" + "、".join(missing_config_fields)
                )
            missing_account_roles = [
                str(role["label"])
                for role in account_roles
                if role.get("required") and not account_bindings.get(str(role["role"]))
            ]
            if missing_account_roles:
                missing_requirements.append(
                    "缺少必需账号：" + "、".join(missing_account_roles)
                )
            if not missing_config_fields and not missing_account_roles and not missing_requirements:
                missing_requirements.append(
                    "项目配置尚未闭合；请展开项目设置检查必填字段、账号和资源"
                )
        missing_requirements = list(dict.fromkeys(missing_requirements))[:20]
        lifecycle_actions_allowed = (
            project_state in AUTOMATION_PLUGIN_STABLE_STATES
            and reconcile_state == "STABLE"
            and runtime_model != AUTOMATION_PLUGIN_UNSUPPORTED_RUNTIME_MODEL
        )
        disable_allowed = bool(enabled) and project_state not in {
            "UPGRADING",
            "UNINSTALLING",
            "UNKNOWN",
        }
        blocked = (
            not configured
            or bool(missing_requirements)
            or state not in AUTOMATION_PLUGIN_STABLE_STATES
            or (platform == "windows" and device is None)
            or not entrypoints_valid
            or not entrypoint_kinds_valid
            or not scheduling_valid
            or not schedule_valid
            or not roles_valid
            or not config_schema_supported
            or not code_owned_config_fields_valid
            or not account_bindings_valid
            or not resource_bindings_valid
            or not resource_bindings_ready
            or not enabled_entrypoints_valid
        )
        seen_instances.add(automation_id)
        instances.append(
            {
                "automation_id": automation_id,
                "plugin_id": plugin_id,
                "instance_name": normalize_feedback_text(
                    redact_text(
                        str(raw.get("instance_name") or raw.get("display_name") or automation_id)
                    )
                )[:120],
                "version": version,
                "active_version": active_version,
                "target_version": target_version,
                "runtime_model": runtime_model,
                "runtime_model_label": AUTOMATION_PLUGIN_RUNTIME_MODEL_LABELS[
                    runtime_model
                ],
                **(
                    {
                        "contribution_projection_state": contribution_projection_state,
                    }
                    if runtime_model == "SERVICE_V2"
                    else {}
                ),
                "plugin_api": plugin_api,
                "dependency_state": dependency_state,
                "dependency_state_label": AUTOMATION_PLUGIN_DEPENDENCY_STATE_LABELS[
                    dependency_state
                ],
                "provided_services": provided_services,
                "blocking_reasons": blocking_reasons,
                "blocking_reason_labels": blocking_reason_labels,
                "migration": migration,
                "enabled": enabled,
                "configured": configured,
                "state": state,
                "status_label": AUTOMATION_PLUGIN_STATE_LABELS[state],
                "project_state": project_state,
                "reconcile_state": reconcile_state,
                "lifecycle_actions_allowed": lifecycle_actions_allowed,
                "menu_actions_allowed": lifecycle_actions_allowed or disable_allowed,
                "enable_allowed": (
                    not enabled and lifecycle_actions_allowed and not blocked
                ),
                "disable_allowed": disable_allowed,
                "record_version": record_version,
                "project_configuration_version": project_configuration_version,
                "execution_platform": platform,
                "platform_label": "Windows 设备" if platform == "windows" else "服务器",
                "can_schedule": bool(scheduling["supported"]),
                "worker_required": bool(
                    raw.get("worker_required", package.get("worker_required", platform == "windows"))
                ),
                "action_summary": normalize_feedback_text(
                    redact_text(
                        str(raw.get("action_summary") or package.get("action_summary") or "")
                    )
                )[:240],
                "resource_summary": normalize_feedback_text(
                    redact_text(
                        str(raw.get("resource_summary") or package.get("resource_summary") or "")
                    )
                )[:200],
                "account_roles": account_roles,
                "account_bindings": account_bindings,
                "resource_roles": resource_roles,
                "resource_bindings": resource_bindings,
                "resource_role_bindings": resource_role_bindings,
                "configuration_summary": _configuration_summary(
                    account_roles, resource_role_bindings
                ),
                "resource_pool_available": resource_pool_available,
                "resource_pool_problem": resource_pool_problem,
                "resource_pool_problem_label": RESOURCE_PROBLEM_LABELS.get(
                    resource_pool_problem,
                    "",
                ),
                "resource_bindings_ready": resource_bindings_ready,
                "config_fields": config_fields,
                "code_owned_config_fields": sorted(code_owned_config_fields),
                "config_schema_supported": config_schema_supported,
                "config_schema_error": config_schema_error,
                "scheduling": scheduling,
                "schedule": schedule,
                "entrypoints": entrypoints,
                "entrypoint_kinds": entrypoint_kinds,
                "console_entrypoints": console_entrypoints,
                "enabled_console_entrypoints": enabled_console_entrypoints,
                "enabled_entrypoints": enabled_entrypoints,
                "enabled_entrypoint_kinds": enabled_entrypoint_kinds,
                "device": device,
                "missing_requirements": missing_requirements,
                "blocked": blocked,
            }
        )

    unsupported = [
        str(item or "").strip()
        for item in value.get("unsupported_automation_ids", [])
        if AUTOMATION_PROJECT_ID_RE.fullmatch(str(item or "").strip())
    ]
    return packages, instances, list(dict.fromkeys(unsupported))


def normalize_automation_workers(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = value.get("workers") if isinstance(value.get("workers"), list) else value.get("items")
    if not isinstance(value, list):
        return []
    workers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            continue
        worker_id = str(raw.get("worker_id") or raw.get("device_id") or raw.get("id") or "").strip()
        platform = str(raw.get("platform") or "").strip().lower()
        if worker_id in seen or not AUTOMATION_WORKER_ID_RE.fullmatch(worker_id) or platform != "windows":
            continue
        state = str(raw.get("state") or raw.get("status") or "").strip().upper()
        session_state = str(raw.get("session_state") or "").strip().upper()
        online = bool(raw.get("online")) or state == "ONLINE" or session_state == "ONLINE"
        display_name = normalize_feedback_text(
            redact_text(str(raw.get("display_name") or raw.get("name") or worker_id))
        )[:120]
        seen.add(worker_id)
        workers.append(
            {
                "worker_id": worker_id,
                "device_id": worker_id,
                "display_name": display_name,
                "name": display_name,
                "platform": "windows",
                "status": normalize_feedback_text(str(raw.get("status") or raw.get("state") or ""))[:40],
                "status_label": "在线" if online else "离线",
                "online": online,
                "binding_usable": online
                and state not in {"DISABLED", "RETIRED", "REVOKED"},
                "last_seen_at": normalize_feedback_text(str(raw.get("last_seen_at") or ""))[:40],
            }
        )
    return workers


def normalize_hidden_automation_ids(value: Any) -> frozenset[str]:
    """Accept only Agent's explicit list of persisted, deferred identities."""

    if not isinstance(value, dict):
        return frozenset()
    raw_ids = value.get("hidden_automation_ids")
    if not isinstance(raw_ids, list):
        return frozenset()
    return frozenset(
        automation_id
        for raw_id in raw_ids
        if isinstance(raw_id, str)
        if AUTOMATION_PROJECT_ID_RE.fullmatch(
            automation_id := raw_id.strip()
        )
    )


def automation_plugin_block_warning(plugin: Mapping[str, Any]) -> str:
    """Keep configuration closure distinct from an immutable runtime transition."""

    if plugin.get("configured") is not True:
        return AUTOMATION_RUNTIME_REASON_LABELS[
            "PROJECT_CONFIGURATION_INCOMPLETE"
        ]
    enabled_entrypoints = plugin.get("enabled_entrypoints")
    if isinstance(enabled_entrypoints, list) and not enabled_entrypoints:
        return AUTOMATION_RUNTIME_REASON_LABELS["ENTRYPOINTS_DISABLED"]
    missing = [
        str(item).strip()
        for item in plugin.get("missing_requirements") or []
        if str(item).strip()
    ]
    if missing:
        if any(
            "合同" in item
            or "Schema" in item
            or "投影" in item
            or "运行描述符" in item
            for item in missing
        ):
            return AUTOMATION_RUNTIME_CONTRACT_ERROR_LABEL
        return "；".join(dict.fromkeys(missing))
    state = str(plugin.get("state") or "UNKNOWN").upper()
    if state not in AUTOMATION_PLUGIN_STABLE_STATES:
        reconcile_state = str(plugin.get("reconcile_state") or "UNKNOWN").upper()
        if reconcile_state == "BLOCKED_UNKNOWN_WRITE":
            return (
                "上次运行的保存结果无法确认。为防止重复写入，任务已暂停。"
                "请先核对业务表格，再检查结果并恢复。"
            )
        if reconcile_state == "ERROR":
            return "运行环境准备失败，任务已暂停。请联系管理员检查后再恢复。"
        return "运行环境正在更新，任务暂时不可运行。已有设置和自动执行状态会保留。"
    return AUTOMATION_RUNTIME_REASON_LABELS["PROJECT_RUNTIME_UNAVAILABLE"]


class AutomationProjectsServiceMixin(AutomationPluginManagementServiceMixin):
    _automation_plugin_max_package_bytes = AUTOMATION_PLUGIN_MAX_PACKAGE_BYTES
    _automation_plugin_version_re = AUTOMATION_PLUGIN_VERSION_RE
    _automation_plugin_migration_pair_id_re = AUTOMATION_PLUGIN_MIGRATION_PAIR_ID_RE
    _valid_migration_business_key_field = staticmethod(
        _valid_migration_business_key_field
    )

    def _clear_automation_plugin_catalog_cache(self) -> None:
        lock = getattr(self, "_automation_catalog_cache_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._automation_catalog_cache_lock = lock
        with lock:
            self._automation_catalog_cache = {}

    def _load_automation_plugin_catalog(
        self,
        handler: BaseHTTPRequestHandler,
        *,
        refresh_resources: bool = False,
    ):
        if refresh_resources:
            self._clear_automation_plugin_catalog_cache()
            return self._load_automation_plugin_catalog_uncached(
                handler,
                refresh_resources=True,
            )
        user = getattr(handler, "current_admin_user", None)
        principal = self._mysql_console_principal(user)
        cache_key = (
            str((principal or {}).get("actor_id") or ""),
            tuple(sorted(str(item) for item in (principal or {}).get("roles") or [])),
        )
        lock = getattr(self, "_automation_catalog_cache_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._automation_catalog_cache_lock = lock
        current = time.monotonic()
        with lock:
            cache = getattr(self, "_automation_catalog_cache", {})
            cached = cache.get(cache_key)
            if cached is not None and cached[0] > current:
                return copy.deepcopy(cached[1])
        result = self._load_automation_plugin_catalog_uncached(handler)
        if not result[5]:
            with lock:
                cache = getattr(self, "_automation_catalog_cache", {})
                cache[cache_key] = (time.monotonic() + 20.0, copy.deepcopy(result))
                self._automation_catalog_cache = cache
        return result

    def _load_automation_plugin_catalog_uncached(
        self,
        handler: BaseHTTPRequestHandler,
        *,
        refresh_resources: bool = False,
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[str],
        frozenset[str],
        str,
        bool,
    ]:
        user = getattr(handler, "current_admin_user", None)
        principal = self._mysql_console_principal(user)
        can_manage = bool(
            principal and "super_admin" in list(principal.get("roles") or [])
        )
        if principal is None:
            return (
                [],
                [],
                [],
                [],
                frozenset(),
                "插件目录只对真实的数据库管理员会话开放。",
                False,
            )

        catalog_endpoint = AUTOMATION_PLUGIN_CATALOG_ENDPOINT
        if refresh_resources:
            catalog_endpoint = f"{catalog_endpoint}?refresh_resources=1"
        catalog_result = self._agent_request(
            "GET",
            catalog_endpoint,
            timeout=15,
            console_principal=principal,
        )
        if not catalog_result.get("ok"):
            code = str(catalog_result.get("error_code") or "PLUGIN_CATALOG_UNAVAILABLE")
            return (
                [],
                [],
                [],
                [],
                frozenset(),
                f"插件目录当前不可用（{code}），所有项目已阻断运行。",
                can_manage,
            )
        packages, instances, unsupported = normalize_automation_plugin_catalog(
            catalog_result.get("data")
        )
        data = catalog_result.get("data")
        hidden_automation_ids = normalize_hidden_automation_ids(data)
        raw_instances = data.get("instances") if isinstance(data, dict) else None
        if not isinstance(raw_instances, list):
            raw_instances = data.get("items") if isinstance(data, dict) else None
        if not isinstance(raw_instances, list):
            return (
                [],
                [],
                [],
                [],
                frozenset(),
                "插件目录返回无效，所有项目已阻断运行。",
                can_manage,
            )
        raw_instance_ids = [
            str(item.get("automation_id") or "").strip()
            if isinstance(item, dict)
            else ""
            for item in raw_instances
        ]
        normalized_instance_ids = [
            str(item.get("automation_id") or "").strip()
            for item in instances
        ]
        if raw_instance_ids != normalized_instance_ids:
            return (
                [],
                [],
                [],
                [],
                frozenset(),
                "插件目录实例投影不完整，所有项目已阻断运行。",
                can_manage,
            )

        requires_workers = any(
            str(item.get("execution_platform") or "").strip().lower() == "windows"
            or bool(item.get("worker_required"))
            for item in [*packages, *instances]
        )
        if not requires_workers:
            return (
                packages,
                instances,
                [],
                unsupported,
                hidden_automation_ids,
                "",
                can_manage,
            )

        workers_result = self._agent_request(
            "GET",
            AUTOMATION_WORKERS_ENDPOINT,
            timeout=12,
            console_principal=principal,
        )
        workers = (
            normalize_automation_workers(workers_result.get("data"))
            if workers_result.get("ok")
            else []
        )
        warning = ""
        if not workers_result.get("ok") and any(
            item.get("execution_platform") == "windows" for item in instances
        ):
            code = str(workers_result.get("error_code") or "WORKERS_UNAVAILABLE")
            warning = f"工作节点列表当前不可用（{code}），相关项目已阻断。"
        return (
            packages,
            instances,
            workers,
            unsupported,
            hidden_automation_ids,
            warning,
            can_manage,
        )

    def _load_automation_project_policies(
        self,
        handler: BaseHTTPRequestHandler,
        tasks: list[dict[str, Any]],
    ) -> tuple[str, bool]:
        user = getattr(handler, "current_admin_user", None)
        principal = self._mysql_console_principal(user)
        can_manage = bool(
            principal and "super_admin" in list(principal.get("roles") or [])
        )
        governed_tasks = [task for task in tasks if task.get("plugin")]
        unlinked_tasks = [
            task for task in tasks if task.get("automation_link_missing")
        ]
        for task in unlinked_tasks:
            # A row without persisted project identity is not an automation
            # project.  Do not derive a policy or pending endpoint from its task ID.
            task["approval_policy"] = None
        blocked_tasks = [
            task
            for task in tasks
            if not task.get("plugin") and not task.get("automation_link_missing")
        ]
        for task in blocked_tasks:
            automation_id = str(task.get("task_id") or "")
            policy = build_automation_project_policy_view(
                automation_id,
                None,
                load_error="插件缺失，项目权限与运行均已阻断。",
            )
            task["approval_policy"] = policy
            apply_automation_project_execution_gate(task, policy)
        if principal is None:
            warning = "项目权限只对真实的数据库管理员会话开放。"
            for task in governed_tasks:
                automation_id = str(task.get("task_id") or "")
                policy = build_automation_project_policy_view(
                    automation_id,
                    None,
                    load_error=warning,
                )
                task["approval_policy"] = policy
                apply_automation_project_execution_gate(task, policy)
            return warning, False

        result = self._agent_request(
            "GET",
            AUTOMATION_PROJECT_POLICY_ENDPOINT,
            timeout=12,
            console_principal=principal,
        )
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        raw_items = data.get("items") if isinstance(data, dict) else None
        safe_items = normalize_automation_project_policy_items(raw_items)
        if not result.get("ok") or not isinstance(raw_items, list):
            error_code = str(
                result.get("error_code") or "PROJECT_POLICY_SERVICE_UNAVAILABLE"
            ).strip()
            warning = f"项目权限当前不可用（{error_code}），任务配置仍可查看。"
            items_by_automation_id: dict[str, dict[str, Any]] = {}
        else:
            warning = ""
            items_by_automation_id = {
                str(item["automation_id"]): item for item in safe_items
            }

        for task in governed_tasks:
            automation_id = str(task.get("task_id") or "")
            policy = build_automation_project_policy_view(
                automation_id,
                items_by_automation_id.get(automation_id),
                load_error=warning,
            )
            task["approval_policy"] = policy
            apply_automation_project_execution_gate(task, policy)
        return warning, can_manage

    @staticmethod
    def _automation_project_id(value: Any) -> str:
        automation_id = str(value or "").strip()
        return automation_id if AUTOMATION_PROJECT_ID_RE.fullmatch(automation_id) else ""

    @staticmethod
    def _automation_project_policy_from_result(
        result: dict[str, Any],
        automation_id: str,
    ) -> dict[str, Any] | None:
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        raw_policy = data.get("policy") if isinstance(data.get("policy"), dict) else data
        safe = normalize_automation_project_policy_items([raw_policy])
        if len(safe) != 1 or safe[0]["automation_id"] != automation_id:
            return None
        return safe[0]

    @staticmethod
    def _automation_pending_from_result(
        result: dict[str, Any],
        automation_id: str,
    ) -> dict[str, Any] | None:
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        raw_pending = data.get("pending") if isinstance(data.get("pending"), dict) else data
        return normalize_automation_pending_approvals(
            raw_pending,
            expected_automation_id=automation_id,
        )

    def _automation_project_agent_error(
        self,
        handler: BaseHTTPRequestHandler,
        result: dict[str, Any],
        *,
        automation_id: str,
        fallback_code: str,
        fallback_message: str,
        include_pending: bool = False,
    ) -> None:
        try:
            status = HTTPStatus(int(result.get("status")))
        except (TypeError, ValueError):
            status = HTTPStatus.BAD_GATEWAY
        if status not in {
            HTTPStatus.BAD_REQUEST,
            HTTPStatus.FORBIDDEN,
            HTTPStatus.NOT_FOUND,
            HTTPStatus.CONFLICT,
            HTTPStatus.UNPROCESSABLE_ENTITY,
            HTTPStatus.TOO_MANY_REQUESTS,
            HTTPStatus.SERVICE_UNAVAILABLE,
        }:
            status = HTTPStatus.BAD_GATEWAY
        safe_data: dict[str, Any] | None = None
        if include_pending:
            pending = self._automation_pending_from_result(result, automation_id)
            if pending is not None:
                safe_data = {"pending": pending}
        self._control_plane_error(
            handler,
            status,
            str(result.get("error_code") or fallback_code)[:128],
            normalize_feedback_text(str(result.get("error") or fallback_message))[:1000],
            data=safe_data,
        )

    def _handle_automation_project_approval_policy(
        self,
        handler: BaseHTTPRequestHandler,
        automation_id: str,
    ) -> None:
        automation_id = self._automation_project_id(automation_id)
        if not automation_id:
            self._control_plane_error(
                handler,
                HTTPStatus.NOT_FOUND,
                "AUTOMATION_PROJECT_NOT_FOUND",
                "自动化项目不存在。",
            )
            return
        trusted_context = self._control_plane_write_context(handler)
        if trusted_context is None:
            return
        if "super_admin" not in list(trusted_context.get("actor_roles") or []):
            self._control_plane_error(
                handler,
                HTTPStatus.FORBIDDEN,
                "SUPER_ADMIN_REQUIRED",
                "只有超级管理员可以修改项目运行权限。",
            )
            return
        values = self._read_control_plane_json(handler)
        if values is None:
            return
        allowed_fields = {
            "mode",
            "request_id",
            "comment",
            "expected_policy_version",
            "expected_project_configuration_version",
        }
        if set(values) - allowed_fields:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "UNSUPPORTED_POLICY_FIELDS",
                "项目权限请求包含不支持的字段，请刷新后重试。",
            )
            return
        mode = str(values.get("mode") or "").strip().upper()
        if mode not in AUTOMATION_PROJECT_POLICY_MODES:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "INVALID_APPROVAL_POLICY_MODE",
                "项目权限只能是完全自动或每次运行审批。",
            )
            return
        request_id = self._normalize_browser_request_uuid(values.get("request_id"))
        comment = normalize_feedback_text(str(values.get("comment") or "")).strip()
        expected_policy_version = values.get("expected_policy_version")
        expected_project_configuration_version = values.get(
            "expected_project_configuration_version"
        )
        if not request_id:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "BROWSER_REQUEST_UUID_REQUIRED",
                "缺少有效且稳定的请求标识，项目权限未保存。",
            )
            return
        if len(comment) > AUTOMATION_PROJECT_COMMENT_MAX_CHARS:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "COMMENT_TOO_LONG",
                "理由不能超过 500 个字符。",
            )
            return
        if any(
            isinstance(version, bool) or not isinstance(version, int) or version < 1
            for version in (
                expected_policy_version,
                expected_project_configuration_version,
            )
        ):
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "PROJECT_POLICY_VERSION_REQUIRED",
                "项目权限或配置版本已缺失，请刷新页面后重试。",
            )
            return
        result = self._agent_request(
            "POST",
            f"/internal/v1/automation-projects/{quote(automation_id, safe='')}/approval-policy",
            payload={
                "mode": mode,
                "request_id": request_id,
                "comment": comment,
                "expected_policy_version": expected_policy_version,
                "expected_project_configuration_version": expected_project_configuration_version,
            },
            timeout=20,
            console_principal=trusted_context["_console_principal"],
        )
        if not result.get("ok"):
            self._automation_project_agent_error(
                handler,
                result,
                automation_id=automation_id,
                fallback_code="PROJECT_POLICY_UPDATE_FAILED",
                fallback_message="项目权限保存失败。",
            )
            return
        policy = self._automation_project_policy_from_result(result, automation_id)
        if policy is None:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_GATEWAY,
                "INVALID_PROJECT_POLICY_RESPONSE",
                "智能服务未返回完整的项目权限结果。",
            )
            return
        self._send_json(
            handler,
            HTTPStatus.OK,
            {
                "ok": True,
                "data": {"policy": build_automation_project_policy_view(automation_id, policy)},
                "message": "项目权限已保存。",
            },
        )

    def _handle_automation_project_pending_approvals_get(
        self,
        handler: BaseHTTPRequestHandler,
        automation_id: str,
    ) -> None:
        automation_id = self._automation_project_id(automation_id)
        if not automation_id:
            self._control_plane_error(
                handler,
                HTTPStatus.NOT_FOUND,
                "AUTOMATION_PROJECT_NOT_FOUND",
                "自动化项目不存在。",
            )
            return
        trusted_context = self._control_plane_read_context(handler)
        if trusted_context is None:
            return
        result = self._agent_request(
            "GET",
            f"/internal/v1/automation-projects/{quote(automation_id, safe='')}/pending-approvals",
            timeout=12,
            console_principal=trusted_context["_console_principal"],
        )
        if not result.get("ok"):
            self._automation_project_agent_error(
                handler,
                result,
                automation_id=automation_id,
                fallback_code="PENDING_APPROVALS_UNAVAILABLE",
                fallback_message="待审批集合暂时不可用。",
            )
            return
        pending = self._automation_pending_from_result(result, automation_id)
        if pending is None:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_GATEWAY,
                "INVALID_PENDING_APPROVALS_RESPONSE",
                "智能服务未返回有效的待审批集合。",
            )
            return
        self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": {"pending": pending}})

    def _handle_automation_project_pending_approvals_action(
        self,
        handler: BaseHTTPRequestHandler,
        automation_id: str,
        action: str,
    ) -> None:
        automation_id = self._automation_project_id(automation_id)
        if not automation_id or action not in {"approve", "reject"}:
            self._control_plane_error(
                handler,
                HTTPStatus.NOT_FOUND,
                "AUTOMATION_APPROVAL_ACTION_NOT_FOUND",
                "批量审批操作不存在。",
            )
            return
        trusted_context = self._control_plane_write_context(handler)
        if trusted_context is None:
            return
        if "super_admin" not in list(trusted_context.get("actor_roles") or []):
            self._control_plane_error(
                handler,
                HTTPStatus.FORBIDDEN,
                "SUPER_ADMIN_REQUIRED",
                "只有超级管理员可以批量处理自动化审批。",
            )
            return
        values = self._read_control_plane_json(handler)
        if values is None:
            return
        allowed_fields = {"expected_pending_set_hash", "request_id", "comment"}
        if set(values) - allowed_fields:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "UNSUPPORTED_PENDING_APPROVAL_FIELDS",
                "批量审批请求不能包含审批标识或计划摘要。",
            )
            return
        expected_hash = str(values.get("expected_pending_set_hash") or "").strip()
        request_id = self._normalize_browser_request_uuid(values.get("request_id"))
        comment = normalize_feedback_text(str(values.get("comment") or "")).strip()
        if not AUTOMATION_PENDING_SET_HASH_RE.fullmatch(expected_hash):
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "EXPECTED_PENDING_SET_HASH_REQUIRED",
                "待审批集合已缺失，请原位刷新后重试。",
            )
            return
        if not request_id:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "BROWSER_REQUEST_UUID_REQUIRED",
                "缺少有效且稳定的请求标识，批量审批未提交。",
            )
            return
        if len(comment) > AUTOMATION_PROJECT_COMMENT_MAX_CHARS:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "COMMENT_TOO_LONG",
                "说明不能超过 500 个字符。",
            )
            return
        result = self._agent_request(
            "POST",
            (
                f"/internal/v1/automation-projects/{quote(automation_id, safe='')}"
                f"/pending-approvals/{action}"
            ),
            payload={
                "expected_pending_set_hash": expected_hash,
                "request_id": request_id,
                "comment": comment,
            },
            timeout=20,
            console_principal=trusted_context["_console_principal"],
        )
        if not result.get("ok"):
            self._automation_project_agent_error(
                handler,
                result,
                automation_id=automation_id,
                fallback_code="PENDING_APPROVAL_ACTION_FAILED",
                fallback_message="批量审批操作失败。",
                include_pending=True,
            )
            return
        pending = self._automation_pending_from_result(result, automation_id)
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        batch = normalize_automation_approval_batch_result(
            data,
            expected_automation_id=automation_id,
            expected_decision="APPROVED" if action == "approve" else "REJECTED",
        )
        if pending is None or batch is None:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_GATEWAY,
                "INVALID_PENDING_APPROVAL_ACTION_RESPONSE",
                "智能服务未返回完整的批量审批结果。",
            )
            return
        safe_data: dict[str, Any] = {
            "pending": pending,
            "decided_count": batch["decided_count"],
        }
        if action == "approve":
            safe_data["run_receipts"] = batch["run_receipts"]
        self._send_json(
            handler,
            HTTPStatus.OK,
            {
                "ok": True,
                "data": safe_data,
                "message": "已批量通过。" if action == "approve" else "已批量驳回。",
            },
        )

    def _handle_automation_plugin_configuration_save(
        self,
        handler: BaseHTTPRequestHandler,
        automation_id: str,
    ) -> None:
        automation_id = self._automation_project_id(automation_id)
        if not automation_id:
            self._control_plane_error(
                handler,
                HTTPStatus.NOT_FOUND,
                "AUTOMATION_PLUGIN_INSTANCE_NOT_FOUND",
                "插件实例不存在。",
            )
            return
        trusted_context = self._control_plane_write_context(handler)
        if trusted_context is None:
            return
        if "super_admin" not in list(trusted_context.get("actor_roles") or []):
            self._control_plane_error(
                handler,
                HTTPStatus.FORBIDDEN,
                "SUPER_ADMIN_REQUIRED",
                "只有超级管理员可以保存自动化项目设置。",
            )
            return
        values = self._read_control_plane_json(handler)
        if values is None:
            return
        allowed_fields = {
            "config",
            "account_bindings",
            "resource_bindings",
            "enabled_entrypoints",
            "device_id",
            "schedule",
            "request_id",
            "expected_project_configuration_version",
        }
        if set(values) != allowed_fields:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "INVALID_PLUGIN_CONFIGURATION_FIELDS",
                "项目设置字段不完整或包含不支持的字段。",
            )
            return

        request_id = self._normalize_browser_request_uuid(values.get("request_id"))
        expected_version = values.get("expected_project_configuration_version")
        config_valid, config = _normalize_browser_plugin_config_value(values.get("config"))
        accounts_valid, account_bindings = _normalize_browser_plugin_bindings(
            values.get("account_bindings")
        )
        resources_valid, resource_bindings = _normalize_browser_plugin_bindings(
            values.get("resource_bindings")
        )
        enabled_entrypoints, entrypoints_valid = _normalize_plugin_entrypoints(
            values.get("enabled_entrypoints"),
            # The Agent validates the submitted IDs against the project's signed
            # v1 manifest or installed v2 contribution contract.
            runtime_model="SERVICE_V2",
        )
        raw_device_id = values.get("device_id")
        device_id = str(raw_device_id or "").strip()
        raw_schedule = values.get("schedule")
        schedule: dict[str, Any] = {}
        schedule_valid = False
        if isinstance(raw_schedule, dict) and set(raw_schedule) == {"kind", "times", "enabled"}:
            schedule_kind = str(raw_schedule.get("kind") or "").strip().lower()
            raw_times = raw_schedule.get("times")
            schedule_enabled = raw_schedule.get("enabled")
            if isinstance(raw_times, list) and isinstance(schedule_enabled, bool):
                schedule_times = [str(item or "").strip() for item in raw_times]
                times_valid = (
                    len(schedule_times) <= AUTOMATION_PLUGIN_SCHEDULE_MAX_DAILY_TIMES
                    and len(schedule_times) == len(set(schedule_times))
                    and all(
                        re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", item)
                        for item in schedule_times
                    )
                )
                schedule_valid = bool(
                    schedule_kind in {"none", "daily_times", "startup"}
                    and times_valid
                    and (
                        (schedule_kind == "none" and not schedule_times and not schedule_enabled)
                        or (schedule_kind == "startup" and not schedule_times)
                        or (schedule_kind == "daily_times" and bool(schedule_times))
                    )
                )
                if schedule_valid:
                    schedule = {
                        "kind": schedule_kind,
                        "times": sorted(schedule_times),
                        "enabled": schedule_enabled,
                    }
        try:
            config_size = len(
                json.dumps(config, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
        except (TypeError, ValueError):
            config_size = AUTOMATION_PLUGIN_CONFIG_MAX_BYTES + 1
        if (
            not request_id
            or isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 1
            or not config_valid
            or config_size > AUTOMATION_PLUGIN_CONFIG_MAX_BYTES
            or not accounts_valid
            or not resources_valid
            or not entrypoints_valid
            or not schedule_valid
            or (raw_device_id is not None and not device_id)
            or (device_id and not AUTOMATION_WORKER_ID_RE.fullmatch(device_id))
        ):
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "INVALID_PLUGIN_CONFIGURATION",
                "项目设置、绑定、运行入口或配置版本无效。",
            )
            return

        payload = {
            "config": config,
            "account_bindings": account_bindings,
            "resource_bindings": resource_bindings,
            "enabled_entrypoints": enabled_entrypoints,
            "device_id": device_id or None,
            "schedule": schedule,
            "request_id": request_id,
            "expected_project_configuration_version": expected_version,
        }
        result = self._agent_request(
            "PUT",
            f"/internal/v1/automation/instances/{quote(automation_id, safe='')}/configuration",
            payload=payload,
            timeout=25,
            console_principal=trusted_context["_console_principal"],
        )
        if not result.get("ok"):
            self._automation_project_agent_error(
                handler,
                result,
                automation_id=automation_id,
                fallback_code="PLUGIN_CONFIGURATION_SAVE_FAILED",
                fallback_message="自动化项目设置保存失败。",
            )
            return

        raw_data = result.get("data") if isinstance(result.get("data"), dict) else {}
        new_version = raw_data.get("project_configuration_version")
        if (
            isinstance(new_version, bool)
            or not isinstance(new_version, int)
            or new_version < 1
        ):
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_GATEWAY,
                "INVALID_PLUGIN_CONFIGURATION_RESPONSE",
                "智能服务未返回新的项目配置版本，请刷新页面核对后重试。",
            )
            return
        response_data: dict[str, Any] = {
            "automation_id": automation_id,
            "project_configuration_version": new_version,
        }
        if isinstance(raw_data.get("configured"), bool):
            response_data["configured"] = raw_data["configured"]
        runtime_state = str(
            raw_data.get("schedule_runtime_state") or "REFRESH_FAILED"
        ).strip().upper()
        if runtime_state not in AUTOMATION_PLUGIN_SCHEDULE_RUNTIME_STATES:
            runtime_state = "REFRESH_FAILED"
        refresh_completed = raw_data.get("scheduler_refresh_completed") is True
        if (
            runtime_state in {"ACTIVE", "DISABLED", "ENTRYPOINT_DISABLED"}
            and not refresh_completed
        ):
            runtime_state = "REFRESH_FAILED"
        response_data["schedule_runtime_state"] = runtime_state
        response_data["schedule_runtime_enabled"] = bool(
            raw_data.get("schedule_runtime_enabled") is True
            and runtime_state == "ACTIVE"
        )
        response_data["scheduler_refresh_completed"] = refresh_completed
        messages = {
            "ACTIVE": "项目设置已保存，运行中定时已按新配置刷新。",
            "DISABLED": "项目设置已保存，运行中定时已关闭。",
            "ENTRYPOINT_DISABLED": (
                "项目设置已保存；定时时间已保留，但系统定时入口关闭，当前不会运行。"
            ),
            "BLOCKED_GENERATION": (
                "项目设置已保存，但新运行代际尚未就绪；请使用同一请求重试，"
                "系统不会沿用旧权限执行。"
            ),
            "REFRESH_FAILED": (
                "项目设置已保存，但运行中调度器刷新失败；旧任务集保持不变，"
                "请使用同一请求重试。"
            ),
        }
        self._send_json(
            handler,
            HTTPStatus.OK,
            {
                "ok": True,
                "data": response_data,
                "message": messages[runtime_state],
            },
        )
