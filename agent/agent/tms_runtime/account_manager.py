"""Automation account registry and credential facade.

The console uses this module through admin APIs. Passwords remain in Agent
runtime state files and are never returned by GET-style payloads.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.tms_runtime.errors import TMSAuthStateError
from agent.tms_runtime.session_broker import SAVED_PASSWORD_MASK, get_session_broker


STATE_DIR = Path(__file__).resolve().parent / "state"
ACCOUNTS_PATH = STATE_DIR / "automation_accounts.json"
LOCAL_ACCOUNT_DIR = STATE_DIR / "automation_account_credentials"

ACCOUNT_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{1,63}$")
SYSTEMS: dict[str, dict[str, Any]] = {
    "ronghui": {
        "label": "TMS融辉",
        "login_kind": "image",
        "session_capable": True,
        "default_session_profile": "default",
        "custom_profile_prefix": "ronghui",
        "require_phone": False,
    },
    "yunda": {
        "label": "韵达",
        "login_kind": "password",
        "session_capable": True,
        "default_session_profile": "yunda",
        "custom_profile_prefix": "yunda",
        "require_phone": False,
    },
    "r7": {
        "label": "R7",
        "login_kind": "password",
        "session_capable": False,
        "require_phone": False,
    },
    "r13": {
        "label": "R13",
        "login_kind": "password",
        "session_capable": False,
        "require_phone": False,
    },
}

ACCOUNT_PURPOSES: dict[str, str] = {
    "general": "普通TMS账号",
    "price": "大祥报价",
    "self_pickup_problem": "自提到货问题件",
    "daxiang_s": "大祥S站",
    "custom": "自定义用途",
}

ACCOUNT_PURPOSE_ORDER = ("general", "price", "self_pickup_problem", "daxiang_s", "custom")

RONGHUI_PURPOSE_PROFILE_PREFIXES = {
    "general": "ronghui",
    "price": "price",
    "self_pickup_problem": "self_pickup_problem",
    "daxiang_s": "daxiang_s",
    "custom": "ronghui",
}

AUTO_LOGIN_STATUSES = {"expired", "logged_out", "error"}
AUTO_LOGIN_FAILURE_LIMIT = 3

DEFAULT_ACCOUNTS: list[dict[str, Any]] = [
    {
        "account_id": "ronghui_default",
        "system": "ronghui",
        "name": "TMS融辉默认账号",
        "account_purpose": "general",
        "is_default": True,
        "session_profile": "default",
    },
    {
        "account_id": "ronghui_self_pickup_problem",
        "system": "ronghui",
        "name": "TMS自提到货问题件账号",
        "account_purpose": "self_pickup_problem",
        "is_default": True,
        "session_profile": "self_pickup_problem_upload",
    },
    {
        "account_id": "ronghui_daxiang_s",
        "system": "ronghui",
        "name": "TMS大祥S站账号",
        "account_purpose": "daxiang_s",
        "is_default": True,
        "session_profile": "daxiang_s",
    },
    {
        "account_id": "yunda_default",
        "system": "yunda",
        "name": "韵达默认账号",
        "account_purpose": "general",
        "is_default": True,
        "session_profile": "yunda",
    },
    {
        "account_id": "price_default",
        "system": "ronghui",
        "name": "大祥报价账号",
        "account_purpose": "price",
        "is_default": True,
        "session_profile": "price",
    },
    {
        "account_id": "r7_default",
        "system": "r7",
        "name": "R7默认账号",
        "account_purpose": "general",
        "is_default": True,
    },
    {
        "account_id": "r13_default",
        "system": "r13",
        "name": "R13默认账号",
        "account_purpose": "general",
        "is_default": True,
    },
]


def _now_label() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _safe_account_id(value: Any) -> str:
    account_id = str(value or "").strip()
    if not ACCOUNT_ID_RE.fullmatch(account_id):
        raise TMSAuthStateError("INVALID_ACCOUNT", "账号标识需为 2-64 位字母、数字、下划线或短横线。")
    return account_id


def _safe_system(value: Any) -> str:
    system = str(value or "").strip().lower()
    if system == "price":
        return "ronghui"
    if system not in SYSTEMS:
        raise TMSAuthStateError("INVALID_ACCOUNT_SYSTEM", "不支持的账号系统。")
    return system


def _safe_purpose(value: Any) -> str:
    purpose = str(value or "").strip().lower()
    if not purpose:
        return "general"
    return purpose if purpose in ACCOUNT_PURPOSES else "custom"


def _camel_alias(value: str) -> str:
    parts = [part for part in str(value or "").split("_") if part]
    if not parts:
        return ""
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])


def _infer_account_purpose(system: str, row: dict[str, Any]) -> str:
    explicit = str(row.get("account_purpose") or row.get("purpose") or "").strip().lower()
    if explicit:
        return _safe_purpose(explicit)
    if system != "ronghui":
        return "general"
    account_id = str(row.get("account_id") or "").strip()
    session_profile = str(row.get("session_profile") or "").strip()
    if account_id == "price_default" or session_profile == "price" or session_profile.startswith("price_"):
        return "price"
    if account_id == "ronghui_self_pickup_problem" or session_profile == "self_pickup_problem_upload":
        return "self_pickup_problem"
    if account_id == "ronghui_daxiang_s" or session_profile == "daxiang_s":
        return "daxiang_s"
    return "general"


def _normalize_system_and_purpose(
    system_value: Any,
    purpose_value: Any = "",
    *,
    row: dict[str, Any] | None = None,
) -> tuple[str, str]:
    raw_system = str(system_value or "").strip().lower()
    system = _safe_system(raw_system)
    if raw_system == "price":
        return "ronghui", "price"
    row = row or {}
    purpose_source = purpose_value if str(purpose_value or "").strip() else row.get("account_purpose")
    purpose = _safe_purpose(purpose_source) if str(purpose_source or "").strip() else _infer_account_purpose(system, row)
    if system != "ronghui":
        purpose = "general"
    return system, purpose


def _slug(value: str) -> str:
    keep = []
    for char in str(value or "").strip().lower():
        keep.append(char if char.isalnum() or char in {"_", "-"} else "_")
    return "".join(keep).strip("_") or "account"


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return payload


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _local_credential_path(account_id: str) -> Path:
    return LOCAL_ACCOUNT_DIR / _slug(account_id) / "login_profile.json"


def _empty_credentials() -> dict[str, Any]:
    return {
        "username": "",
        "password": "",
        "phone": "",
        "updated_at": "",
        "last_validation_at": "",
    }


@dataclass(frozen=True)
class AccountRecord:
    account_id: str
    system: str
    account_purpose: str
    name: str
    is_active: bool
    is_default: bool
    session_profile: str
    created_at: str
    updated_at: str


class AutomationAccountManager:
    def __init__(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self._auto_login_locks: dict[str, threading.Lock] = {}
        self._auto_login_locks_guard = threading.Lock()

    def _auto_login_lock(self, account_id: str) -> threading.Lock:
        with self._auto_login_locks_guard:
            return self._auto_login_locks.setdefault(account_id, threading.Lock())

    def _load_accounts(self) -> list[dict[str, Any]]:
        raw = _read_json(ACCOUNTS_PATH, [])
        rows = raw if isinstance(raw, list) else []
        now = _now_label()
        by_id: dict[str, dict[str, Any]] = {}
        changed = False
        for item in rows:
            if not isinstance(item, dict):
                continue
            account_id = str(item.get("account_id") or "").strip()
            if not ACCOUNT_ID_RE.fullmatch(account_id):
                continue
            try:
                system, purpose = _normalize_system_and_purpose(item.get("system"), row={**item, "account_id": account_id})
            except TMSAuthStateError:
                continue
            row = dict(item)
            row["account_id"] = account_id
            if row.get("system") != system or row.get("account_purpose") != purpose:
                changed = True
            row["system"] = system
            row["account_purpose"] = purpose
            row["name"] = str(row.get("name") or account_id).strip()
            row["is_active"] = bool(row.get("is_active", True))
            row["is_default"] = bool(row.get("is_default", False))
            raw_failure_count = row.get("auto_login_failure_count", 0)
            try:
                failure_count = max(int(raw_failure_count), 0)
            except (TypeError, ValueError):
                failure_count = 0
            failure_count = min(failure_count, AUTO_LOGIN_FAILURE_LIMIT)
            auto_login_enabled = bool(row.get("auto_login_enabled", True))
            auto_login_blocked = bool(row.get("auto_login_blocked", False)) or (
                failure_count >= AUTO_LOGIN_FAILURE_LIMIT
            )
            if (
                row.get("auto_login_enabled") != auto_login_enabled
                or row.get("auto_login_failure_count") != failure_count
                or row.get("auto_login_blocked") != auto_login_blocked
            ):
                changed = True
            row["auto_login_enabled"] = auto_login_enabled
            row["auto_login_failure_count"] = failure_count
            row["auto_login_blocked"] = auto_login_blocked
            row["created_at"] = str(row.get("created_at") or now)
            row["updated_at"] = str(row.get("updated_at") or row["created_at"])
            previous_profile = str(row.get("session_profile") or "")
            row["session_profile"] = self._coerce_session_profile(row)
            if row["session_profile"] != previous_profile:
                changed = True
            by_id[account_id] = row

        for default in DEFAULT_ACCOUNTS:
            if default["account_id"] in by_id:
                continue
            row = {
                **default,
                "is_active": True,
                "auto_login_enabled": True,
                "auto_login_failure_count": 0,
                "auto_login_blocked": False,
                "created_at": now,
                "updated_at": now,
            }
            row["session_profile"] = self._coerce_session_profile(row)
            by_id[row["account_id"]] = row
            changed = True

        rows = list(by_id.values())
        keys = sorted(
            {(row["system"], row.get("account_purpose", "general")) for row in rows},
            key=lambda key: (
                tuple(SYSTEMS).index(key[0]) if key[0] in SYSTEMS else len(SYSTEMS),
                ACCOUNT_PURPOSE_ORDER.index(key[1]) if key[1] in ACCOUNT_PURPOSE_ORDER else len(ACCOUNT_PURPOSE_ORDER),
            ),
        )
        for system, purpose in keys:
            purpose_rows = [
                row
                for row in rows
                if row["system"] == system and row.get("account_purpose", "general") == purpose
            ]
            active_rows = [row for row in purpose_rows if row.get("is_active", True)]
            default_rows = [row for row in active_rows if row.get("is_default")]
            keep_default = default_rows[0] if default_rows else active_rows[0] if active_rows else None
            for row in purpose_rows:
                next_default = row is keep_default
                if bool(row.get("is_default")) != next_default:
                    row["is_default"] = next_default
                    row["updated_at"] = now
                    changed = True
        if changed or not ACCOUNTS_PATH.exists():
            self._save_accounts(rows)
        return sorted(
            rows,
            key=lambda row: (
                tuple(SYSTEMS).index(row["system"]) if row["system"] in SYSTEMS else len(SYSTEMS),
                ACCOUNT_PURPOSE_ORDER.index(row.get("account_purpose", "general"))
                if row.get("account_purpose", "general") in ACCOUNT_PURPOSE_ORDER
                else len(ACCOUNT_PURPOSE_ORDER),
                not row.get("is_default"),
                row["account_id"],
            ),
        )

    def _save_accounts(self, rows: list[dict[str, Any]]) -> None:
        _write_json(ACCOUNTS_PATH, rows)

    def _set_auto_login_state(
        self,
        account_id: str,
        *,
        enabled: bool | None = None,
        failure_count: int | None = None,
        blocked: bool | None = None,
    ) -> dict[str, Any]:
        rows = self._load_accounts()
        now = _now_label()
        updated: dict[str, Any] | None = None
        for item in rows:
            if item["account_id"] != account_id:
                continue
            if enabled is not None:
                item["auto_login_enabled"] = bool(enabled)
            if failure_count is not None:
                item["auto_login_failure_count"] = min(
                    max(int(failure_count), 0),
                    AUTO_LOGIN_FAILURE_LIMIT,
                )
            if blocked is not None:
                item["auto_login_blocked"] = bool(blocked)
            item["updated_at"] = now
            updated = item
            break
        if updated is None:
            raise TMSAuthStateError("ACCOUNT_NOT_FOUND", "账号不存在。")
        self._save_accounts(rows)
        return dict(updated)

    def _reset_auto_login_failures(self, account_id: str) -> dict[str, Any]:
        row = self._get_account_row(account_id)
        if not row.get("auto_login_failure_count") and not row.get("auto_login_blocked"):
            return row
        return self._set_auto_login_state(account_id, failure_count=0, blocked=False)

    def _record_auto_login_failure(self, account_id: str, *, exhausted: bool = False) -> dict[str, Any]:
        row = self._get_account_row(account_id)
        current = int(row.get("auto_login_failure_count") or 0)
        next_count = AUTO_LOGIN_FAILURE_LIMIT if exhausted else min(current + 1, AUTO_LOGIN_FAILURE_LIMIT)
        return self._set_auto_login_state(
            account_id,
            failure_count=next_count,
            blocked=next_count >= AUTO_LOGIN_FAILURE_LIMIT,
        )

    def _coerce_session_profile(self, row: dict[str, Any]) -> str:
        system = str(row.get("system") or "").strip().lower()
        config = SYSTEMS.get(system, {})
        if not config.get("session_capable"):
            return ""
        account_id = str(row.get("account_id") or "").strip()
        purpose = str(row.get("account_purpose") or "general").strip().lower() or "general"
        explicit = str(row.get("session_profile") or "").strip()
        default_account = next(
            (item for item in DEFAULT_ACCOUNTS if item["account_id"] == account_id),
            None,
        )
        if default_account:
            default_system, default_purpose = _normalize_system_and_purpose(
                default_account.get("system"),
                default_account.get("account_purpose"),
                row=default_account,
            )
            if default_system == system and default_purpose == purpose:
                return _slug(default_account.get("session_profile") or config.get("default_session_profile") or account_id)
        if explicit:
            return _slug(explicit)
        if system == "ronghui":
            prefix = RONGHUI_PURPOSE_PROFILE_PREFIXES.get(purpose, "ronghui")
        else:
            prefix = str(config.get("custom_profile_prefix") or system)
        return f"{prefix}_{_slug(account_id)}"

    def _get_account_row(self, account_id: str) -> dict[str, Any]:
        safe_id = _safe_account_id(account_id)
        for row in self._load_accounts():
            if row["account_id"] == safe_id:
                return row
        raise TMSAuthStateError("ACCOUNT_NOT_FOUND", "账号不存在。")

    def system_config(self, system: str) -> dict[str, Any]:
        return dict(SYSTEMS[_safe_system(system)])

    def list_accounts(
        self,
        *,
        include_status: bool = True,
        validate: bool = True,
        force: bool = False,
    ) -> list[dict[str, Any]]:
        accounts = []
        for row in self._load_accounts():
            payload = self._public_account(row)
            if include_status:
                if validate and force:
                    payload["status"] = self.check_status_with_auto_login(row["account_id"], force=force)
                else:
                    payload["status"] = self.describe_status(row["account_id"], validate=validate, force=force)
                payload["credentials"] = self.public_credentials(row["account_id"])
            accounts.append(payload)
        return accounts

    def create_account(
        self,
        *,
        account_id: str,
        system: str,
        name: str = "",
        account_purpose: str = "",
    ) -> dict[str, Any]:
        safe_id = _safe_account_id(account_id)
        safe_system, safe_purpose = _normalize_system_and_purpose(system, account_purpose)
        label = str(name or "").strip() or safe_id
        rows = self._load_accounts()
        if any(row["account_id"] == safe_id for row in rows):
            raise TMSAuthStateError("DUPLICATE_ACCOUNT", "账号标识已存在。")
        now = _now_label()
        row = {
            "account_id": safe_id,
            "system": safe_system,
            "account_purpose": safe_purpose,
            "name": label,
            "is_active": True,
            "is_default": False,
            "auto_login_enabled": True,
            "auto_login_failure_count": 0,
            "auto_login_blocked": False,
            "created_at": now,
            "updated_at": now,
        }
        row["session_profile"] = self._coerce_session_profile(row)
        rows.append(row)
        self._save_accounts(rows)
        return self._public_account(row)

    def set_default(self, account_id: str) -> dict[str, Any]:
        row = self._get_account_row(account_id)
        if not row.get("is_active", True):
            raise TMSAuthStateError("ACCOUNT_DISABLED", "账号已停用，不能设为默认。")
        rows = self._load_accounts()
        now = _now_label()
        for item in rows:
            if item["system"] != row["system"] or item.get("account_purpose", "general") != row.get("account_purpose", "general"):
                continue
            item["is_default"] = item["account_id"] == row["account_id"]
            item["updated_at"] = now
        self._save_accounts(rows)
        return self._public_account(self._get_account_row(account_id))

    def set_active(self, account_id: str, is_active: bool) -> dict[str, Any]:
        row = self._get_account_row(account_id)
        rows = self._load_accounts()
        now = _now_label()
        for item in rows:
            if item["account_id"] == row["account_id"]:
                item["is_active"] = bool(is_active)
                if not is_active:
                    item["is_default"] = False
                item["updated_at"] = now
                break
        purpose_rows = [
            item
            for item in rows
            if item["system"] == row["system"]
            and item.get("account_purpose", "general") == row.get("account_purpose", "general")
            and item.get("is_active", True)
        ]
        if purpose_rows and not any(item.get("is_default") for item in purpose_rows):
            purpose_rows[0]["is_default"] = True
            purpose_rows[0]["updated_at"] = now
        self._save_accounts(rows)
        return self._public_account(self._get_account_row(account_id))

    def set_auto_login(self, account_id: str, enabled: bool) -> dict[str, Any]:
        self._get_account_row(account_id)
        row = self._set_auto_login_state(
            account_id,
            enabled=bool(enabled),
            failure_count=0,
            blocked=False,
        )
        return self._public_account(row)

    def _public_account(self, row: dict[str, Any]) -> dict[str, Any]:
        config = SYSTEMS[row["system"]]
        purpose = str(row.get("account_purpose") or "general").strip().lower() or "general"
        return {
            "account_id": row["account_id"],
            "system": row["system"],
            "system_label": config["label"],
            "account_purpose": purpose,
            "account_purpose_label": ACCOUNT_PURPOSES.get(purpose, ACCOUNT_PURPOSES["custom"]),
            "name": row["name"],
            "is_active": bool(row.get("is_active", True)),
            "is_default": bool(row.get("is_default")),
            "auto_login_enabled": bool(row.get("auto_login_enabled", True)),
            "auto_login_failure_count": int(row.get("auto_login_failure_count") or 0),
            "auto_login_failure_limit": AUTO_LOGIN_FAILURE_LIMIT,
            "auto_login_blocked": bool(row.get("auto_login_blocked", False)),
            "login_kind": config["login_kind"],
            "session_capable": bool(config.get("session_capable")),
            "session_profile": row.get("session_profile", ""),
            "created_at": row.get("created_at", ""),
            "updated_at": row.get("updated_at", ""),
        }

    def _with_account_context(self, row: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        config = SYSTEMS[row["system"]]
        purpose = str(row.get("account_purpose") or "general").strip().lower() or "general"
        result = dict(payload or {})
        result.update(
            {
                "account_id": row["account_id"],
                "account_name": row["name"],
                "system": row["system"],
                "system_label": config["label"],
                "account_purpose": purpose,
                "account_purpose_label": ACCOUNT_PURPOSES.get(purpose, ACCOUNT_PURPOSES["custom"]),
                "login_kind": config["login_kind"],
                "session_capable": bool(config.get("session_capable")),
                "session_profile": row.get("session_profile", ""),
                "is_active": bool(row.get("is_active", True)),
                "auto_login_enabled": bool(row.get("auto_login_enabled", True)),
                "auto_login_failure_count": int(row.get("auto_login_failure_count") or 0),
                "auto_login_failure_limit": AUTO_LOGIN_FAILURE_LIMIT,
                "auto_login_blocked": bool(row.get("auto_login_blocked", False)),
            }
        )
        result.pop("password", None)
        return result

    def _broker(self, row: dict[str, Any]):
        profile = self._coerce_session_profile(row)
        return get_session_broker(profile)

    def _load_local_credentials(self, account_id: str) -> dict[str, Any]:
        payload = _read_json(_local_credential_path(account_id), {})
        if not isinstance(payload, dict):
            return _empty_credentials()
        result = _empty_credentials()
        result.update(
            {
                "username": str(payload.get("username") or "").strip(),
                "password": str(payload.get("password") or "").strip(),
                "phone": str(payload.get("phone") or "").strip(),
                "updated_at": str(payload.get("updated_at") or "").strip(),
                "last_validation_at": str(payload.get("last_validation_at") or "").strip(),
            }
        )
        return result

    def private_credentials(self, account_id: str) -> dict[str, Any]:
        row = self._get_account_row(account_id)
        if SYSTEMS[row["system"]].get("session_capable"):
            config = self._broker(row).resolve_login_config()
            return {
                "username": config.username,
                "password": config.password,
                "phone": config.phone,
                "updated_at": self.public_credentials(account_id).get("updated_at", ""),
            }
        return self._load_local_credentials(row["account_id"])

    def public_credentials(self, account_id: str) -> dict[str, Any]:
        row = self._get_account_row(account_id)
        if SYSTEMS[row["system"]].get("session_capable"):
            payload = self._broker(row).get_saved_credentials()
        else:
            private = self._load_local_credentials(row["account_id"])
            has_saved = bool(private.get("username") and private.get("password"))
            payload = {
                **private,
                "has_saved_credentials": has_saved,
                "has_manual_credentials": has_saved,
                "has_env_credentials": False,
                "credential_source": "saved" if has_saved else "",
            }
        public_payload = dict(payload)
        public_payload["password"] = ""
        return public_payload

    def save_credentials(self, account_id: str, *, username: str, password: str, phone: str = "") -> dict[str, Any]:
        row = self._get_account_row(account_id)
        config = SYSTEMS[row["system"]]
        if config.get("session_capable"):
            return self._public_credentials(self._broker(row).save_credentials(
                username=username,
                password=password,
                phone=phone,
            ))

        existing = self._load_local_credentials(row["account_id"])
        incoming_password = str(password or "").strip()
        if incoming_password in {"", SAVED_PASSWORD_MASK} and existing.get("password"):
            incoming_password = str(existing.get("password") or "").strip()
        payload = {
            "username": str(username or "").strip(),
            "password": incoming_password,
            "phone": str(phone or "").strip(),
            "updated_at": _now_label(),
        }
        missing = []
        if not payload["username"]:
            missing.append("账号")
        if not payload["password"]:
            missing.append("密码")
        if config.get("require_phone") and not payload["phone"]:
            missing.append("手机号")
        if missing:
            raise TMSAuthStateError("AUTH_REQUIRED", "、".join(missing) + "不能为空。")
        _write_json(_local_credential_path(row["account_id"]), payload)
        return self.public_credentials(row["account_id"])

    def _public_credentials(self, payload: dict[str, Any]) -> dict[str, Any]:
        public_payload = dict(payload)
        public_payload["password"] = ""
        return public_payload

    def clear_credentials(self, account_id: str) -> dict[str, Any]:
        row = self._get_account_row(account_id)
        if SYSTEMS[row["system"]].get("session_capable"):
            return self._public_credentials(self._broker(row).clear_saved_credentials())
        _local_credential_path(row["account_id"]).unlink(missing_ok=True)
        return self.public_credentials(row["account_id"])

    def describe_status(self, account_id: str, *, validate: bool = True, force: bool = False) -> dict[str, Any]:
        row = self._get_account_row(account_id)
        config = SYSTEMS[row["system"]]
        monitoring_enabled = bool(
            row.get("is_active", True)
            and row.get("auto_login_enabled", True)
            and not row.get("auto_login_blocked", False)
        )
        validate = bool(validate and monitoring_enabled)
        if config.get("session_capable"):
            status = self._broker(row).describe_status(validate=validate, force=force)
        else:
            credentials = self.public_credentials(row["account_id"])
            has_credentials = bool(credentials.get("has_saved_credentials"))
            status = {
                "status": "logged_out" if has_credentials else "error",
                "label": "凭据已配置" if has_credentials else "未配置",
                "status_tone": "success" if has_credentials else "error",
                "authenticated": False,
                "pending_code": False,
                "last_validation_at": credentials.get("last_validation_at") or credentials.get("updated_at", ""),
                "last_error_summary": "" if has_credentials else "请先保存账号密码。",
                "authenticated_at": "",
                "pending_since": "",
                "expires_at": "",
                "challenge_type": "",
                "challenge_label": "",
            }
            status.update(credentials)
            status["last_validation_at"] = credentials.get("last_validation_at") or credentials.get("updated_at", "")
        if not row.get("is_active", True):
            status.update(
                {
                    "label": "已停用",
                    "status_tone": "neutral",
                    "last_error_summary": "",
                    "monitoring_paused": True,
                    "account_disabled": True,
                }
            )
        elif not row.get("auto_login_enabled", True):
            raw_status = str(status.get("status") or "")
            if raw_status == "logged_out":
                paused_label = "已退出"
            elif raw_status == "authenticated":
                paused_label = "已登录（未监控）"
            else:
                paused_label = "自动登录已关闭"
            status.update(
                {
                    "label": paused_label,
                    "status_tone": "warning" if raw_status == "authenticated" else "neutral",
                    "last_error_summary": "",
                    "monitoring_paused": True,
                }
            )
        elif row.get("auto_login_blocked", False):
            status.update(
                {
                    "label": "自动登录已暂停",
                    "status_tone": "warning",
                    "last_error_summary": (
                        f"连续自动登录失败 {AUTO_LOGIN_FAILURE_LIMIT} 次，为防止账号锁定已暂停；请手动登录。"
                    ),
                    "monitoring_paused": True,
                }
            )
        status["account_id"] = row["account_id"]
        status["account_name"] = row["name"]
        status["system"] = row["system"]
        status["system_label"] = config["label"]
        status["account_purpose"] = row.get("account_purpose", "general")
        status["account_purpose_label"] = ACCOUNT_PURPOSES.get(
            str(row.get("account_purpose") or "general"),
            ACCOUNT_PURPOSES["custom"],
        )
        status["login_kind"] = config["login_kind"]
        status["session_capable"] = bool(config.get("session_capable"))
        status["session_profile"] = row.get("session_profile", "")
        status["is_active"] = bool(row.get("is_active", True))
        status["auto_login_enabled"] = bool(row.get("auto_login_enabled", True))
        status["auto_login_failure_count"] = int(row.get("auto_login_failure_count") or 0)
        status["auto_login_failure_limit"] = AUTO_LOGIN_FAILURE_LIMIT
        status["auto_login_blocked"] = bool(row.get("auto_login_blocked", False))
        status.pop("password", None)
        return status

    def _login_error_status(
        self,
        row: dict[str, Any],
        exc: Exception,
        status: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        status = dict(status or {})
        failure_count = int(row.get("auto_login_failure_count") or 0)
        blocked = bool(row.get("auto_login_blocked", False))
        error_summary = str(exc)
        if blocked:
            error_summary = (
                f"连续自动登录失败 {failure_count} 次，为防止账号锁定已暂停；请手动登录。"
                f"最后错误：{error_summary}"
            )
        status.update(
            {
                "status": "error",
                "label": "自动登录已暂停" if blocked else "自动登录失败",
                "status_tone": "warning" if blocked else "error",
                "authenticated": False,
                "pending_code": False,
                "last_error_summary": error_summary,
                "authenticated_at": "",
                "pending_since": "",
                "expires_at": status.get("expires_at", ""),
                "challenge_type": "",
                "challenge_label": "",
            }
        )
        return self._with_account_context(row, status)

    def check_status_with_auto_login(self, account_id: str, *, force: bool = True) -> dict[str, Any]:
        safe_id = _safe_account_id(account_id)
        with self._auto_login_lock(safe_id):
            row = self._get_account_row(safe_id)
            config = SYSTEMS[row["system"]]
            if (
                not row.get("is_active", True)
                or not row.get("auto_login_enabled", True)
                or row.get("auto_login_blocked", False)
            ):
                return self.describe_status(safe_id, validate=False, force=False)
            status = self.describe_status(safe_id, validate=True, force=force)
            if not config.get("session_capable"):
                return status
            if str(status.get("status") or "").strip() == "authenticated":
                row = self._reset_auto_login_failures(safe_id)
                return self._with_account_context(row, status)
            if str(status.get("status") or "").strip() not in AUTO_LOGIN_STATUSES:
                return status
            try:
                result = self._broker(row).send_code()
                result_status = str(result.get("status") or "").strip()
                if result_status == "authenticated":
                    row = self._reset_auto_login_failures(safe_id)
                elif result.get("auto_login_attempts_exhausted"):
                    row = self._record_auto_login_failure(safe_id, exhausted=True)
                elif result_status == "error":
                    row = self._record_auto_login_failure(safe_id)
                else:
                    return self._with_account_context(row, result)
                if row.get("auto_login_blocked", False):
                    last_error = str(result.get("last_error_summary") or "").strip()
                    blocked_summary = (
                        f"连续自动登录失败 {AUTO_LOGIN_FAILURE_LIMIT} 次，为防止账号锁定已暂停；请手动登录。"
                    )
                    if last_error:
                        blocked_summary = f"{blocked_summary}最后错误：{last_error}"
                    result = {
                        **result,
                        "label": "自动登录已暂停",
                        "status_tone": "warning",
                        "last_error_summary": blocked_summary,
                    }
                return self._with_account_context(row, result)
            except Exception as exc:
                row = self._record_auto_login_failure(safe_id)
                return self._login_error_status(row, exc, status)

    def login(self, account_id: str) -> dict[str, Any]:
        row = self._get_account_row(account_id)
        if not row.get("is_active", True):
            raise TMSAuthStateError("ACCOUNT_DISABLED", "账号已停用，请先启用账号。")
        config = SYSTEMS[row["system"]]
        if config.get("session_capable"):
            result = self._broker(row).send_code()
            if result.get("auto_login_attempts_exhausted"):
                row = self._record_auto_login_failure(row["account_id"], exhausted=True)
            elif str(result.get("status") or "").strip() != "error":
                row = self._reset_auto_login_failures(row["account_id"])
            return self._with_account_context(row, result)
        credentials = self.private_credentials(row["account_id"])
        if not credentials.get("username") or not credentials.get("password"):
            raise TMSAuthStateError("AUTH_REQUIRED", "请先保存账号密码。")
        # R7/R13 do not maintain a long-lived shared browser state in v1; this
        # action verifies that stored credentials can be resolved and used.
        if row["system"] == "r7":
            from agent.tms_runtime.scripts.r7_login_manager import R7SSOAuth

            auth = R7SSOAuth(config_path="")
            auth.login_and_get_session(
                username=credentials["username"],
                password=credentials["password"],
                max_attempts=1,
                exchange=False,
                verify=False,
            )
        elif row["system"] == "r13":
            from agent.tms_runtime.scripts.r13_login_manager import R13SSOAuth

            auth = R13SSOAuth(config_path="")
            auth.login_and_get_session(
                username=credentials["username"],
                password=credentials["password"],
                max_attempts=1,
                exchange=False,
                verify=False,
            )
        credentials["last_validation_at"] = _now_label()
        _write_json(_local_credential_path(row["account_id"]), credentials)
        return self._with_account_context(row, {
            "status": "logged_out",
            "label": "凭据验证通过",
            "status_tone": "success",
            "last_validation_at": credentials["last_validation_at"],
            "has_saved_credentials": True,
        })

    def submit_code(self, account_id: str, code: str) -> dict[str, Any]:
        row = self._get_account_row(account_id)
        if not row.get("is_active", True):
            raise TMSAuthStateError("ACCOUNT_DISABLED", "账号已停用，请先启用账号。")
        if not SYSTEMS[row["system"]].get("session_capable"):
            raise TMSAuthStateError("UNSUPPORTED_ACTION", "该账号类型不需要验证码提交。")
        result = self._broker(row).submit_code(code)
        if str(result.get("status") or "").strip() != "error":
            row = self._reset_auto_login_failures(row["account_id"])
        return self._with_account_context(row, result)

    def clear_session(self, account_id: str) -> dict[str, Any]:
        row = self._get_account_row(account_id)
        if SYSTEMS[row["system"]].get("session_capable"):
            row = self._set_auto_login_state(
                row["account_id"],
                enabled=False,
                failure_count=0,
                blocked=False,
            )
            return self._with_account_context(row, self._broker(row).clear())
        return self.describe_status(account_id, validate=False)

    def default_account_for_system(self, system: str, purpose: str = "") -> dict[str, Any] | None:
        safe_system, safe_purpose = _normalize_system_and_purpose(system, purpose)
        rows = [
            row
            for row in self._load_accounts()
            if row["system"] == safe_system
            and row.get("account_purpose", "general") == safe_purpose
            and row.get("is_active", True)
        ]
        if not rows:
            return None
        return next((row for row in rows if row.get("is_default")), rows[0])

    def resolve_execution_params(
        self,
        params: dict[str, Any],
        default_system: str = "",
        default_purpose: str = "",
    ) -> dict[str, Any]:
        effective = dict(params or {})
        account_id = str(effective.get("account_id") or effective.get("accountId") or "").strip()
        session_profile = str(effective.get("session_profile") or "").strip()
        account_purpose = str(
            effective.get("account_purpose")
            or effective.get("accountPurpose")
            or default_purpose
            or ""
        ).strip()
        if not account_id and default_system and not session_profile:
            try:
                default_row = self.default_account_for_system(default_system, account_purpose)
            except TMSAuthStateError:
                default_row = None
            if default_row and SYSTEMS[default_row["system"]].get("session_capable"):
                account_id = str(default_row.get("account_id") or "").strip()
        if not account_id:
            return effective
        effective.setdefault("account_id", account_id)
        row = self._get_account_row(account_id)
        if not row.get("is_active", True):
            raise TMSAuthStateError("ACCOUNT_DISABLED", "账号已停用。")
        config = SYSTEMS[row["system"]]
        if config.get("session_capable"):
            effective["session_profile"] = self._coerce_session_profile(row)
            return effective
        credentials = self.private_credentials(row["account_id"])
        if not credentials.get("username") or not credentials.get("password"):
            raise TMSAuthStateError("AUTH_REQUIRED", f"{row['name']} 未保存账号密码。")
        effective["username"] = credentials["username"]
        effective["password"] = credentials["password"]
        if credentials.get("phone"):
            effective["phone"] = credentials["phone"]
        return effective

    def resolve_role_account_params(
        self,
        params: dict[str, Any],
        *,
        account_field: str = "account_id",
        output_account_field: str | None = None,
        output_session_profile_field: str = "session_profile",
        output_username_field: str = "username",
        output_password_field: str = "password",
        output_phone_field: str = "phone",
    ) -> dict[str, Any]:
        effective = dict(params or {})
        safe_field = str(account_field or "account_id").strip() or "account_id"
        aliases = [safe_field]
        camel = _camel_alias(safe_field)
        if camel and camel not in aliases:
            aliases.append(camel)
        account_id = ""
        for key in aliases:
            account_id = str(effective.get(key) or "").strip()
            if account_id:
                break
        if not account_id:
            return effective

        row = self._get_account_row(account_id)
        if not row.get("is_active", True):
            raise TMSAuthStateError("ACCOUNT_DISABLED", "账号已停用。")

        target_account_field = safe_field if output_account_field is None else str(output_account_field or "")
        if target_account_field:
            effective[target_account_field] = account_id

        config = SYSTEMS[row["system"]]
        if config.get("session_capable"):
            target_session_field = str(output_session_profile_field or "")
            if target_session_field:
                effective[target_session_field] = self._coerce_session_profile(row)
            return effective

        credentials = self.private_credentials(row["account_id"])
        if not credentials.get("username") or not credentials.get("password"):
            raise TMSAuthStateError("AUTH_REQUIRED", f"{row['name']} 未保存账号密码。")
        if output_username_field:
            effective[str(output_username_field)] = credentials["username"]
        if output_password_field:
            effective[str(output_password_field)] = credentials["password"]
        if output_phone_field and credentials.get("phone"):
            effective[str(output_phone_field)] = credentials["phone"]
        return effective


_ACCOUNT_MANAGER: AutomationAccountManager | None = None


def get_account_manager() -> AutomationAccountManager:
    global _ACCOUNT_MANAGER
    if _ACCOUNT_MANAGER is None:
        _ACCOUNT_MANAGER = AutomationAccountManager()
    return _ACCOUNT_MANAGER


def resolve_account_params(
    params: dict[str, Any],
    default_system: str = "",
    default_purpose: str = "",
) -> dict[str, Any]:
    return get_account_manager().resolve_execution_params(
        params,
        default_system=default_system,
        default_purpose=default_purpose,
    )


def resolve_role_account_params(
    params: dict[str, Any],
    *,
    account_field: str = "account_id",
    output_account_field: str | None = None,
    output_session_profile_field: str = "session_profile",
    output_username_field: str = "username",
    output_password_field: str = "password",
    output_phone_field: str = "phone",
) -> dict[str, Any]:
    return get_account_manager().resolve_role_account_params(
        params,
        account_field=account_field,
        output_account_field=output_account_field,
        output_session_profile_field=output_session_profile_field,
        output_username_field=output_username_field,
        output_password_field=output_password_field,
        output_phone_field=output_phone_field,
    )
