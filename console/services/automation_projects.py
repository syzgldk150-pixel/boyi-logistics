"""Console application services grouped by business responsibility."""

import math

from console.app_support import *  # noqa: F403
from shared.service_identity import (
    ConsoleIdentityError,
    build_console_identity_headers,
)


AUTOMATION_PROJECT_POLICY_ENDPOINT = "/internal/v1/automation-project-policies"
AUTOMATION_PROJECT_POLICY_MODES = frozenset({"REQUIRE_EACH_RUN", "PROJECT_FULL_AUTO"})
AUTOMATION_PROJECT_EFFECTIVE_MODES = frozenset(
    {*AUTOMATION_PROJECT_POLICY_MODES, "LEGACY_SCHEDULE_ONLY"}
)
AUTOMATION_PROJECT_POLICY_STATUSES = frozenset(
    {"ACTIVE", "STALE", "UNSUPPORTED", "LEGACY_SCHEDULE_ONLY"}
)
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
    "ERROR": "异常",
    "UNKNOWN": "状态未知",
}
AUTOMATION_PLUGIN_STABLE_STATES = frozenset({"INSTALLED", "ENABLED", "DISABLED"})
AUTOMATION_PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
AUTOMATION_PLUGIN_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
AUTOMATION_WORKER_ID_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,128}$")
AUTOMATION_PLUGIN_BINDING_ID_RE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,160}$")
AUTOMATION_PLUGIN_CONFIG_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
AUTOMATION_PLUGIN_ENTRYPOINTS = frozenset({"scheduler", "console", "feishu", "webhook"})
AUTOMATION_PLUGIN_CONFIG_MAX_FIELDS = 100
AUTOMATION_PLUGIN_CONFIG_MAX_BYTES = 128 * 1024


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
        can_full_auto = raw.get("can_full_auto")
        policy_version = raw.get("policy_version")
        project_configuration_version = raw.get("project_configuration_version")
        if (
            automation_id in seen
            or not AUTOMATION_PROJECT_ID_RE.fullmatch(automation_id)
            or configured_mode not in AUTOMATION_PROJECT_POLICY_MODES
            or effective_mode not in AUTOMATION_PROJECT_EFFECTIVE_MODES
            or effective_status not in AUTOMATION_PROJECT_POLICY_STATUSES
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
        "label": "权限状态不可用",
        "summary": load_error or "未取得项目权限，请稍后刷新。",
        "updated_by": "",
        "updated_at": "",
        "policy_version": 0,
        "project_configuration_version": 0,
    }
    if not isinstance(item, dict):
        if not load_error:
            base["label"] = "未安装 / 每次运行审批"
            base["summary"] = "该动态项目尚无已安装的项目权限，不能视为完全自动。"
        return base

    configured_mode = str(item["configured_mode"])
    effective_mode = str(item["effective_mode"])
    effective_status = str(item["effective_status"])
    summary = str(item.get("summary") or "").strip()
    if effective_mode == "LEGACY_SCHEDULE_ONLY" or effective_status == "LEGACY_SCHEDULE_ONLY":
        label = "旧版计划权限"
        default_summary = "当前仍按旧版单计划权限生效；请选择新的项目权限完成迁移。"
    elif effective_status == "UNSUPPORTED" or not bool(item.get("can_full_auto")):
        label = "每次运行审批"
        default_summary = "该项目不支持完全自动，每次运行都需要审批。"
    elif effective_status == "STALE":
        label = "配置变更，已恢复审批"
        default_summary = "项目配置已变化；重新确认完全自动前，每次运行都需要审批。"
    elif effective_mode == "PROJECT_FULL_AUTO":
        label = "完全自动"
        default_summary = "项目清单允许且已启用的定时、后台、飞书与验签 Webhook 入口按当前保存配置运行。"
    else:
        label = "每次运行审批"
        default_summary = "定时与 Console 手工执行每次都先进入审批。"

    return {
        **base,
        **item,
        "available": True,
        "label": label,
        "summary": summary or default_summary,
        "configured_mode": configured_mode,
        "effective_mode": effective_mode,
        "effective_status": effective_status,
    }


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


def _normalize_plugin_account_roles(value: Any) -> list[dict[str, Any]]:
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
        roles.append(
            {
                "role": role,
                "field": role,
                "label": normalize_feedback_text(
                    redact_text(str(raw_role.get("label") or role))
                )[:80],
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
        roles.append(
            {
                "role": role,
                "label": normalize_feedback_text(
                    redact_text(str(raw_role.get("label") or role))
                )[:80],
                "allowed_kinds": kinds,
                "required": bool(raw_role.get("required", True)),
            }
        )
    return roles


def _normalize_plugin_resources(value: Any) -> tuple[list[dict[str, str]], bool]:
    """Accept only the closed, credential-free managed-resource projection."""

    if not isinstance(value, list):
        return [], False
    resources: list[dict[str, str]] = []
    seen: set[str] = set()
    expected_fields = {"resource_id", "name", "kind", "status"}
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != expected_fields:
            return [], False
        resource_id = str(raw.get("resource_id") or "").strip()
        name = normalize_feedback_text(redact_text(str(raw.get("name") or "")))[:160]
        kind = str(raw.get("kind") or "").strip().lower()
        status = str(raw.get("status") or "").strip().lower()
        if (
            resource_id in seen
            or not AUTOMATION_PLUGIN_BINDING_ID_RE.fullmatch(resource_id)
            or not name
            or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", kind)
            or status != "available"
        ):
            return [], False
        seen.add(resource_id)
        resources.append(
            {
                "resource_id": resource_id,
                "name": name,
                "kind": kind,
                "status": status,
            }
        )
    return sorted(resources, key=lambda item: item["resource_id"]), True


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
        fields.append(
            {
                "path": ".".join(path),
                "label": normalize_feedback_text(redact_text(str(node.get("title") or key)))[:100],
                "hint": normalize_feedback_text(redact_text(str(node.get("description") or "")))[:240],
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
        if (
            not AUTOMATION_PLUGIN_CONFIG_KEY_RE.fullmatch(str(key))
            or not isinstance(node, dict)
            or not walk(node, [str(key)], str(key) in required)
        ):
            return [], False, f"配置字段 {key} 使用了不支持的 Schema"
    return fields, True, ""


def _normalize_plugin_entrypoints(value: Any) -> tuple[list[str], bool]:
    if not isinstance(value, list):
        return [], False
    normalized = [str(item or "").strip().lower() for item in value]
    if (
        not normalized
        or len(normalized) != len(set(normalized))
        or not set(normalized) <= AUTOMATION_PLUGIN_ENTRYPOINTS
    ):
        return [], False
    return normalized, True


def _normalize_plugin_scheduling(
    value: Any,
    entrypoints: list[str],
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
    valid = supported == ("scheduler" in entrypoints)
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


AUTOMATION_PLUGIN_MISSING_REQUIREMENT_LABELS = {
    "project_config": "项目配置未完整",
    "account_binding": "必需账号尚未绑定",
    "resource_binding": "必需资源尚未绑定",
    "device_binding": "命名 Windows Worker 尚未绑定",
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
        account_roles = _normalize_plugin_account_roles(raw_account_roles)
        resource_roles = _normalize_plugin_resource_roles(raw_resource_roles)
        roles_valid = (
            isinstance(raw_account_roles, list)
            and len(account_roles) == len(raw_account_roles)
            and isinstance(raw_resource_roles, list)
            and len(resource_roles) == len(raw_resource_roles)
        )
        entrypoints, entrypoints_valid = _normalize_plugin_entrypoints(raw.get("entrypoints"))
        scheduling, scheduling_valid = _normalize_plugin_scheduling(
            raw.get("scheduling"), entrypoints
        )
        contract_supported = entrypoints_valid and scheduling_valid and roles_valid
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
            "scheduling": scheduling,
            "entrypoints": entrypoints,
            "contract_supported": contract_supported,
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
        projected_state = str(
            raw.get("state") or ("ENABLED" if enabled else "DISABLED")
        ).strip().upper()
        state = (
            projected_state
            if projected_state in AUTOMATION_PLUGIN_STATE_LABELS
            else "UNKNOWN"
        )
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
            _normalize_plugin_account_roles(raw_account_roles)
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
            raw.get("entrypoints", package.get("entrypoints"))
        )
        scheduling, scheduling_valid = _normalize_plugin_scheduling(
            raw.get("scheduling", package.get("scheduling")), entrypoints
        )
        schedule, schedule_valid = _normalize_plugin_schedule(
            raw.get("schedule"), scheduling
        )
        config_fields, config_schema_supported, config_schema_error = (
            _normalize_plugin_config_schema(raw.get("config_schema"), raw.get("config"))
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
            ] if resource_pool_available else []
            selected_available = bool(
                selected_resource_id
                and any(
                    option["resource_id"] == selected_resource_id
                    for option in options
                )
            )
            blocked_reason = ""
            if not resource_pool_available:
                if bool(role_definition.get("required")) or selected_resource_id:
                    blocked_reason = "受管资源池当前不可用"
            elif selected_resource_id and not selected_available:
                blocked_reason = "已保存资源不存在、不可用或类型不匹配"
            elif bool(role_definition.get("required")) and not selected_resource_id:
                blocked_reason = "未选择必需资源"
            if blocked_reason:
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
            raw.get("enabled_entrypoints")
        )
        if enabled_entrypoints_valid and not set(enabled_entrypoints) <= set(entrypoints):
            enabled_entrypoints_valid = False
            enabled_entrypoints = []
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
        projection_warnings: list[str] = []
        if not entrypoints_valid or not scheduling_valid:
            projection_warnings.append("插件入口合同不可识别")
        if not schedule_valid:
            projection_warnings.append("项目定时投影无效")
        if not roles_valid:
            projection_warnings.append("插件账号或资源角色合同不可识别")
        if not config_schema_supported:
            projection_warnings.append(config_schema_error or "配置 Schema 不受支持")
        if not account_bindings_valid:
            projection_warnings.append("账号绑定投影无效")
        if not resource_bindings_valid:
            projection_warnings.append("资源绑定投影无效")
        projection_warnings.extend(
            str(binding["blocked_reason"])
            for binding in resource_role_bindings
            if binding.get("blocked_reason")
        )
        if not enabled_entrypoints_valid:
            projection_warnings.append("至少保留一个插件清单允许的运行入口")
        missing_requirements = list(
            dict.fromkeys([*missing_requirements, *projection_warnings])
        )[:20]
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
                "enabled": enabled,
                "configured": configured,
                "state": state,
                "status_label": AUTOMATION_PLUGIN_STATE_LABELS[state],
                "lifecycle_actions_allowed": state in AUTOMATION_PLUGIN_STABLE_STATES,
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
                "resource_pool_available": resource_pool_available,
                "config_fields": config_fields,
                "config_schema_supported": config_schema_supported,
                "config_schema_error": config_schema_error,
                "scheduling": scheduling,
                "schedule": schedule,
                "entrypoints": entrypoints,
                "enabled_entrypoints": enabled_entrypoints,
                "device": device,
                "missing_requirements": missing_requirements,
                "blocked": (
                    not configured
                    or bool(missing_requirements)
                    or state not in AUTOMATION_PLUGIN_STABLE_STATES
                    or (platform == "windows" and device is None)
                    or not entrypoints_valid
                    or not scheduling_valid
                    or not schedule_valid
                    or not roles_valid
                    or not config_schema_supported
                    or not account_bindings_valid
                    or not resource_bindings_valid
                    or not resource_bindings_ready
                    or not enabled_entrypoints_valid
                ),
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


class AutomationProjectsServiceMixin:
    def _load_automation_plugin_catalog(
        self,
        handler: BaseHTTPRequestHandler,
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[str],
        str,
        bool,
    ]:
        user = getattr(handler, "current_admin_user", None)
        principal = self._mysql_console_principal(user)
        can_manage = bool(
            principal and "super_admin" in list(principal.get("roles") or [])
        )
        if principal is None:
            return [], [], [], [], "插件目录只对真实 MySQL 管理员会话开放。", False

        catalog_result = self._agent_request(
            "GET",
            AUTOMATION_PLUGIN_CATALOG_ENDPOINT,
            timeout=15,
            console_principal=principal,
        )
        if not catalog_result.get("ok"):
            code = str(catalog_result.get("error_code") or "PLUGIN_CATALOG_UNAVAILABLE")
            return [], [], [], [], f"插件目录当前不可用（{code}），所有项目已阻断运行。", can_manage
        packages, instances, unsupported = normalize_automation_plugin_catalog(
            catalog_result.get("data")
        )
        data = catalog_result.get("data")
        raw_instances = data.get("instances") if isinstance(data, dict) else None
        if not isinstance(raw_instances, list):
            raw_instances = data.get("items") if isinstance(data, dict) else None
        if not isinstance(raw_instances, list):
            return [], [], [], [], "插件目录返回无效，所有项目已阻断运行。", can_manage

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
            warning = f"Windows Worker 列表当前不可用（{code}），相关项目已阻断。"
        return packages, instances, workers, unsupported, warning, can_manage

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
            task["approval_policy"] = build_automation_project_policy_view(
                automation_id,
                None,
                load_error="插件缺失，项目权限与运行均已阻断。",
            )
        if principal is None:
            warning = "项目权限只对真实 MySQL 管理员会话开放。"
            for task in governed_tasks:
                automation_id = str(task.get("task_id") or "")
                task["approval_policy"] = build_automation_project_policy_view(
                    automation_id,
                    None,
                    load_error=warning,
                )
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
            task["approval_policy"] = build_automation_project_policy_view(
                automation_id,
                items_by_automation_id.get(automation_id),
                load_error=warning,
            )
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
                "Agent 未返回完整的项目权限结果。",
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
                "Agent 未返回有效的待审批集合。",
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
                "批量审批请求不能包含审批 ID 或计划哈希。",
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
                "Agent 未返回完整的批量审批结果。",
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

    def _agent_plugin_multipart_request(
        self,
        endpoint: str,
        *,
        package_path: Path,
        fields: dict[str, str],
        console_principal: dict[str, Any],
        timeout: int = 90,
    ) -> dict[str, Any]:
        """Forward one bounded ZIP as signed multipart without trusting browser metadata."""

        try:
            endpoint = self._validate_internal_agent_endpoint(endpoint)
        except ValueError as exc:
            return {
                "ok": False,
                "status": HTTPStatus.BAD_REQUEST,
                "error_code": "INVALID_AGENT_ENDPOINT",
                "error": str(exc),
            }
        token = str(getattr(self.settings, "agent_internal_api_token", "") or "").strip()
        if not token:
            return {
                "ok": False,
                "status": HTTPStatus.SERVICE_UNAVAILABLE,
                "error_code": "AGENT_INTERNAL_TOKEN_NOT_CONFIGURED",
                "error": "Agent 内部接口未配置。",
            }
        try:
            package_size = package_path.stat().st_size
            if package_size <= 0 or package_size > AUTOMATION_PLUGIN_MAX_PACKAGE_BYTES:
                raise ValueError("plugin package size is outside the accepted boundary")
            package_bytes = package_path.read_bytes()
        except (OSError, ValueError) as exc:
            LOGGER.warning("Rejected staged plugin package: %s", type(exc).__name__)
            return {
                "ok": False,
                "status": HTTPStatus.BAD_REQUEST,
                "error_code": "INVALID_PLUGIN_PACKAGE_SIZE",
                "error": "插件包大小无效或暂时无法读取。",
            }
        signed_fields = dict(fields)
        signed_fields["package_sha256"] = hashlib.sha256(package_bytes).hexdigest()
        boundary = f"----ConsoleAutomationPlugin{secrets.token_hex(18)}"
        parts: list[bytes] = []
        for name, value in signed_fields.items():
            safe_name = str(name).replace('"', "")
            safe_value = str(value).replace("\r", " ").replace("\n", " ")
            parts.extend(
                [
                    f"--{boundary}\r\n".encode("ascii"),
                    (
                        f'Content-Disposition: form-data; name="{safe_name}"\r\n\r\n'
                    ).encode("ascii"),
                    safe_value.encode("utf-8"),
                    b"\r\n",
                ]
            )
        parts.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                b'Content-Disposition: form-data; name="package"; filename="automation-plugin.zip"\r\n',
                b"Content-Type: application/zip\r\n\r\n",
                package_bytes,
                b"\r\n",
                f"--{boundary}--\r\n".encode("ascii"),
            ]
        )
        body = b"".join(parts)
        url = f"{self.settings.agent_base_url.rstrip('/')}{endpoint}"
        headers = {
            "X-Agent-Internal-Token": token,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
        signing_secret = str(os.getenv("CONSOLE_AGENT_SIGNING_SECRET", "") or "").strip()
        if not signing_secret:
            return {
                "ok": False,
                "status": HTTPStatus.SERVICE_UNAVAILABLE,
                "error_code": "CONSOLE_SIGNING_SECRET_NOT_CONFIGURED",
                "error": "Console-to-Agent 签名未配置。",
            }
        try:
            headers.update(
                build_console_identity_headers(
                    secret=signing_secret,
                    method="POST",
                    request_target=endpoint,
                    body=body,
                    principal=console_principal,
                    nonce=secrets.token_urlsafe(24),
                )
            )
        except ConsoleIdentityError as exc:
            return {
                "ok": False,
                "status": HTTPStatus.SERVICE_UNAVAILABLE,
                "error_code": exc.code,
                "error": str(exc),
            }
        request = Request(url, data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
                payload = json.loads(raw) if raw else {}
                if not isinstance(payload, dict) or not {"ok", "data", "error"}.issubset(payload):
                    raise ValueError("Agent returned an invalid internal API contract")
                if payload.get("ok") is not True:
                    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
                    return {
                        "ok": False,
                        "status": response.status,
                        "error_code": str(error.get("code") or "PLUGIN_PACKAGE_REJECTED"),
                        "error": redact_text(error.get("message") or "插件包被拒绝。"),
                        "data": payload.get("data"),
                    }
                return {"ok": True, "status": response.status, "data": payload.get("data")}
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                payload = {}
            error = payload.get("error") if isinstance(payload, dict) and isinstance(payload.get("error"), dict) else {}
            return {
                "ok": False,
                "status": exc.code,
                "error_code": str(error.get("code") or "PLUGIN_PACKAGE_REQUEST_FAILED"),
                "error": redact_text(error.get("message") or "插件包上传失败。"),
                "data": payload.get("data") if isinstance(payload, dict) else None,
            }
        except (URLError, ValueError) as exc:
            return {
                "ok": False,
                "status": HTTPStatus.BAD_GATEWAY,
                "error_code": "PLUGIN_PACKAGE_REQUEST_FAILED",
                "error": redact_text(str(exc)),
            }

    def _handle_automation_plugin_package_upload(
        self,
        handler: BaseHTTPRequestHandler,
        *,
        automation_id: str = "",
    ) -> None:
        requested_automation_id = str(automation_id or "").strip()
        automation_id = self._automation_project_id(requested_automation_id)
        if requested_automation_id and not automation_id:
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
                "只有超级管理员可以安装或升级自动化。",
            )
            return
        try:
            content_length = int(handler.headers.get("Content-Length") or "0")
        except (TypeError, ValueError):
            content_length = -1
        if content_length <= 0 or content_length > AUTOMATION_PLUGIN_MAX_PACKAGE_BYTES + 512 * 1024:
            self._control_plane_error(
                handler,
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "PLUGIN_PACKAGE_TOO_LARGE",
                "插件 ZIP 不能超过 32MB。",
            )
            return
        form = self._parse_multipart_form(handler)
        allowed_form_fields = (
            {"package", "request_id", "expected_record_version"}
            if automation_id
            else {"package", "request_id", "instance_name"}
        )
        unexpected_form_fields = set(form.keys()) - allowed_form_fields
        duplicate_form_fields = [
            field for field in form.keys() if isinstance(form[field], list)
        ]
        if unexpected_form_fields or duplicate_form_fields:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "UNSUPPORTED_PLUGIN_PACKAGE_FIELDS",
                "插件包请求包含不支持的字段。",
            )
            return
        package_item = form["package"] if "package" in form else None
        filename = str(getattr(package_item, "filename", "") or "")
        if package_item is None or not filename or Path(filename).suffix.lower() != ".zip":
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "SIGNED_ZIP_REQUIRED",
                "请选择一个签名 ZIP 插件包。",
            )
            return
        request_id = self._normalize_browser_request_uuid(
            form.getvalue("request_id") or handler.headers.get("X-Browser-Request-UUID")
        )
        if not request_id:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "BROWSER_REQUEST_UUID_REQUIRED",
                "缺少有效且稳定的请求标识，插件包未提交。",
            )
            return
        package_bytes = package_item.file.read(AUTOMATION_PLUGIN_MAX_PACKAGE_BYTES + 1)
        if (
            not package_bytes
            or len(package_bytes) > AUTOMATION_PLUGIN_MAX_PACKAGE_BYTES
            or not zipfile.is_zipfile(io.BytesIO(package_bytes))
        ):
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "INVALID_PLUGIN_ZIP",
                "插件包为空、超过 32MB 或不是有效 ZIP。",
            )
            return

        fields = {"request_id": request_id}
        if automation_id:
            expected_record_version_raw = str(form.getvalue("expected_record_version") or "").strip()
            try:
                expected_record_version = int(expected_record_version_raw)
            except ValueError:
                expected_record_version = 0
            if expected_record_version < 1:
                self._control_plane_error(
                    handler,
                    HTTPStatus.BAD_REQUEST,
                    "EXPECTED_RECORD_VERSION_REQUIRED",
                    "实例版本快照已缺失，请刷新后重试。",
                )
                return
            fields["expected_record_version"] = str(expected_record_version)
            endpoint = (
                f"/internal/v1/automation/instances/{quote(automation_id, safe='')}/upgrade"
            )
        else:
            instance_name = normalize_feedback_text(str(form.getvalue("instance_name") or "")).strip()
            if not instance_name or len(instance_name) > 120:
                self._control_plane_error(
                    handler,
                    HTTPStatus.BAD_REQUEST,
                    "INSTANCE_NAME_REQUIRED",
                    "请填写 1 至 120 个字符的实例名称。",
                )
                return
            fields["instance_name"] = instance_name
            endpoint = "/internal/v1/automation/plugins/install"

        upload_root = (self.settings.runtime_dir / "automation_plugin_uploads").resolve()
        upload_root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(upload_root, 0o700)
        except OSError:
            pass
        target = (upload_root / f"{request_id}-{secrets.token_hex(8)}.zip").resolve()
        try:
            target.relative_to(upload_root)
        except ValueError:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "INVALID_PLUGIN_UPLOAD_PATH",
                "插件上传路径无效。",
            )
            return
        try:
            target.write_bytes(package_bytes)
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
            result = self._agent_plugin_multipart_request(
                endpoint,
                package_path=target,
                fields=fields,
                console_principal=trusted_context["_console_principal"],
            )
        except OSError as exc:
            LOGGER.warning("Failed to stage automation plugin package: %s", type(exc).__name__)
            self._control_plane_error(
                handler,
                HTTPStatus.SERVICE_UNAVAILABLE,
                "PLUGIN_UPLOAD_STAGING_FAILED",
                "插件包暂存失败，请稍后重试。",
            )
            return
        finally:
            target.unlink(missing_ok=True)
        if not result.get("ok"):
            self._automation_project_agent_error(
                handler,
                result,
                automation_id=automation_id,
                fallback_code="PLUGIN_PACKAGE_REQUEST_FAILED",
                fallback_message="自动化插件包处理失败。",
            )
            return
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        created_id = self._automation_project_id(data.get("automation_id"))
        self._send_json(
            handler,
            HTTPStatus.OK,
            {
                "ok": True,
                "data": {"automation_id": created_id} if created_id else {},
                "message": "自动化已升级。" if automation_id else "自动化已安装为新的停用实例。",
            },
        )

    def _handle_automation_plugin_instance_action(
        self,
        handler: BaseHTTPRequestHandler,
        automation_id: str,
        action: str,
    ) -> None:
        automation_id = self._automation_project_id(automation_id)
        if not automation_id or action not in {"enable", "disable", "uninstall"}:
            self._control_plane_error(
                handler,
                HTTPStatus.NOT_FOUND,
                "AUTOMATION_PLUGIN_ACTION_NOT_FOUND",
                "插件实例操作不存在。",
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
                "只有超级管理员可以管理插件实例。",
            )
            return
        values = self._read_control_plane_json(handler)
        if values is None:
            return
        request_id = self._normalize_browser_request_uuid(values.get("request_id"))
        expected_record_version = values.get("expected_record_version")
        if (
            not request_id
            or isinstance(expected_record_version, bool)
            or not isinstance(expected_record_version, int)
            or expected_record_version < 1
        ):
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "PLUGIN_ACTION_VERSION_REQUIRED",
                "缺少请求标识或实例版本快照，请刷新后重试。",
            )
            return
        if action in {"enable", "disable"}:
            if set(values) - {"request_id", "expected_record_version"}:
                self._control_plane_error(
                    handler,
                    HTTPStatus.BAD_REQUEST,
                    "UNSUPPORTED_PLUGIN_ACTION_FIELDS",
                    "实例状态请求包含不支持的字段。",
                )
                return
            endpoint = f"/internal/v1/automation/instances/{quote(automation_id, safe='')}/state"
            payload = {
                "enabled": action == "enable",
                "request_id": request_id,
                "expected_record_version": expected_record_version,
            }
        else:
            current_version = str(values.get("current_version") or "").strip()
            if (
                set(values)
                - {"request_id", "expected_record_version", "current_version", "confirm"}
                or not AUTOMATION_PLUGIN_VERSION_RE.fullmatch(current_version)
                or values.get("confirm") is not True
            ):
                self._control_plane_error(
                    handler,
                    HTTPStatus.BAD_REQUEST,
                    "PLUGIN_UNINSTALL_CONFIRMATION_REQUIRED",
                    "卸载必须确认当前实例版本与不可撤销范围。",
                )
                return
            endpoint = f"/internal/v1/automation/instances/{quote(automation_id, safe='')}/uninstall"
            payload = {
                "request_id": request_id,
                "expected_record_version": expected_record_version,
                "current_version": current_version,
                "confirm": True,
            }
        result = self._agent_request(
            "POST",
            endpoint,
            payload=payload,
            timeout=45 if action == "uninstall" else 20,
            console_principal=trusted_context["_console_principal"],
        )
        if not result.get("ok"):
            self._automation_project_agent_error(
                handler,
                result,
                automation_id=automation_id,
                fallback_code="PLUGIN_INSTANCE_ACTION_FAILED",
                fallback_message="插件实例操作失败。",
            )
            return
        self._send_json(
            handler,
            HTTPStatus.OK,
            {
                "ok": True,
                "data": {"automation_id": automation_id},
                "message": {
                    "enable": "自动化实例已启用。",
                    "disable": "自动化实例已停用。",
                    "uninstall": "自动化实例已卸载。",
                }[action],
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
            values.get("enabled_entrypoints")
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
                    len(schedule_times) <= 50
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
        response_data: dict[str, Any] = {"automation_id": automation_id}
        if (
            not isinstance(new_version, bool)
            and isinstance(new_version, int)
            and new_version >= 1
        ):
            response_data["project_configuration_version"] = new_version
        if isinstance(raw_data.get("configured"), bool):
            response_data["configured"] = raw_data["configured"]
        self._send_json(
            handler,
            HTTPStatus.OK,
            {
                "ok": True,
                "data": response_data,
                "message": "项目账号、资源、入口与运行配置已保存；权限会按新配置重新核验。",
            },
        )
