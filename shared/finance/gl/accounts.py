"""Chart of accounts: tree rules and maintenance (structure is super-administrator only).

Only leaf accounts take entries. An account that has entries or voucher lines
cannot gain children, change type or be deleted; it can only be deactivated or
frozen. The default 小企业会计准则 chart is seeded by migration 068.
"""

from __future__ import annotations

from typing import Any

from shared.finance.db import fetchall, fetchone
from shared.finance.errors import FinanceError, FinanceNotFoundError, FinanceValidationError
from shared.finance.models import (
    ACCOUNT_TYPES,
    CASH_FLOW_CATEGORIES,
    NORMAL_BALANCES,
    ROOT_TYPES,
    FinanceActor,
    expected_revision,
    optional_text,
    parse_account_code,
)

ACCOUNT_COLUMNS = (
    "code, name, parent_code, root_type, account_type, normal_balance, is_group, is_active, frozen,"
    " requires_party, requires_waybill, cash_flow_category, revision"
)
_FLAGS = ("is_group", "is_active", "frozen", "requires_party", "requires_waybill")
_EDITABLE = {"name", "is_active", "frozen", "requires_waybill", "cash_flow_category", "account_type"}


def account_view(row: dict[str, Any]) -> dict[str, Any]:
    view = {key: row[key] for key in ("code", "name", "parent_code", "root_type", "account_type", "normal_balance", "cash_flow_category")}
    view.update({flag: bool(row[flag]) for flag in _FLAGS})
    view["revision"] = int(row["revision"])
    if "has_entries" in row:
        view["has_entries"] = bool(row["has_entries"])
    return view


def _name(value: Any) -> str:
    text = optional_text(value, "科目名称", 64)
    if not text:
        raise FinanceValidationError("科目名称不能为空。")
    return text


def _choice(value: Any, allowed: tuple[str, ...], label: str) -> str:
    if value not in allowed:
        raise FinanceValidationError(f"{label}无效。")
    return value


def _flag(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise FinanceValidationError(f"{label}必须是开关值。")
    return value


class AccountsMixin:
    @staticmethod
    def _account_in_use(cursor: Any, code: str) -> bool:
        cursor.execute(
            "SELECT EXISTS(SELECT 1 FROM finance_gl_entries WHERE account_code=%s)"
            " OR EXISTS(SELECT 1 FROM finance_voucher_lines WHERE account_code=%s) AS used",
            (code, code),
        )
        return bool(fetchone(cursor)["used"])

    @staticmethod
    def _account_is_default(cursor: Any, code: str) -> bool:
        cursor.execute(
            "SELECT %s IN (suspense_account, transit_account, current_profit_account, retained_earnings_account) AS used"
            " FROM finance_settings WHERE id=1",
            (code,),
        )
        row = fetchone(cursor)
        return bool(row and row["used"])

    @staticmethod
    def _lock_account(cursor: Any, code: str) -> dict[str, Any]:
        cursor.execute(f"SELECT {ACCOUNT_COLUMNS} FROM finance_accounts WHERE code=%s FOR UPDATE", (code,))
        row = fetchone(cursor)
        if row is None:
            raise FinanceNotFoundError(f"科目 {code} 不存在。", code="ACCOUNT_NOT_FOUND")
        return row

    def list_accounts(self) -> list[dict[str, Any]]:
        def work(cursor: Any) -> list[dict[str, Any]]:
            cursor.execute(
                f"SELECT {ACCOUNT_COLUMNS}, (EXISTS(SELECT 1 FROM finance_gl_entries g WHERE g.account_code=a.code)"
                " OR EXISTS(SELECT 1 FROM finance_voucher_lines l WHERE l.account_code=a.code)) AS has_entries"
                " FROM finance_accounts a ORDER BY code"
            )
            return [account_view(row) for row in fetchall(cursor)]

        return self._read(work)

    def create_account(self, payload: dict[str, Any], *, request_id: Any, actor: FinanceActor) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise FinanceValidationError("科目内容无效。")
        allowed = {"code", "name", "parent_code", "root_type", "account_type", "normal_balance",
                   "cash_flow_category", "requires_waybill"}
        if set(payload) - allowed:
            raise FinanceValidationError("科目内容包含不支持的字段。")
        code = parse_account_code(payload.get("code"))
        name = _name(payload.get("name"))
        # The parent is implied by the code (each level adds two digits).
        parent_code = code[:-2] if len(code) > 4 else None
        if (payload.get("parent_code") or None) not in (None, parent_code):
            raise FinanceValidationError("下级科目代码应为上级代码加 2 位数字。", code="FINANCE_INVALID_ACCOUNT_CODE")
        requires_waybill = _flag(payload.get("requires_waybill", False), "必填运单号")
        cash_flow = payload.get("cash_flow_category") or None
        if cash_flow is not None:
            _choice(cash_flow, CASH_FLOW_CATEGORIES, "现金流分类")

        def work(cursor: Any) -> dict[str, Any]:
            cursor.execute("SELECT 1 FROM finance_accounts WHERE code=%s FOR UPDATE", (code,))
            if cursor.fetchone():
                raise FinanceError("ACCOUNT_EXISTS", f"科目代码 {code} 已存在。")
            if parent_code is None:
                root_type = _choice(payload.get("root_type"), ROOT_TYPES, "科目类别")
                normal_balance = _choice(payload.get("normal_balance"), NORMAL_BALANCES, "余额方向")
                account_type = _choice(payload.get("account_type"), ACCOUNT_TYPES, "科目属性")
            else:
                parent = self._lock_account(cursor, parent_code)
                if not parent["is_group"]:
                    if self._account_in_use(cursor, parent["code"]):
                        raise FinanceError("ACCOUNT_HAS_ENTRIES", f"科目 {parent['code']} 已有凭证或分录，不能再增加下级科目。")
                    if self._account_is_default(cursor, parent["code"]):
                        raise FinanceError("ACCOUNT_IS_DEFAULT", f"科目 {parent['code']} 是财务设置中的默认科目，不能增加下级科目。")
                    cursor.execute("UPDATE finance_accounts SET is_group=1, revision=revision+1 WHERE code=%s", (parent["code"],))
                root_type, normal_balance = parent["root_type"], parent["normal_balance"]
                account_type = _choice(payload.get("account_type") or parent["account_type"], ACCOUNT_TYPES, "科目属性")
                if "root_type" in payload and payload["root_type"] != root_type:
                    raise FinanceValidationError("下级科目的类别必须与上级一致。")
                if "normal_balance" in payload and payload["normal_balance"] != normal_balance:
                    raise FinanceValidationError("下级科目的余额方向必须与上级一致。")
            cursor.execute(
                "INSERT INTO finance_accounts (code, name, parent_code, root_type, account_type, normal_balance,"
                " requires_waybill, cash_flow_category) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (code, name, parent_code, root_type, account_type, normal_balance, int(requires_waybill),
                 cash_flow if parent_code is None else (cash_flow or self._parent_cash_flow(cursor, parent_code))),
            )
            cursor.execute(f"SELECT {ACCOUNT_COLUMNS} FROM finance_accounts WHERE code=%s", (code,))
            return account_view(fetchone(cursor))

        return self._write(request_id=request_id, action="account.create", target_type="account", target_id=code,
                           payload=payload, actor=actor, work=work)

    @staticmethod
    def _parent_cash_flow(cursor: Any, parent_code: str) -> str | None:
        cursor.execute("SELECT cash_flow_category FROM finance_accounts WHERE code=%s", (parent_code,))
        row = fetchone(cursor)
        return row["cash_flow_category"] if row else None

    def update_account(self, code: Any, payload: dict[str, Any], *, request_id: Any, actor: FinanceActor) -> dict[str, Any]:
        code = parse_account_code(code)
        if not isinstance(payload, dict):
            raise FinanceValidationError("科目内容无效。")
        revision = expected_revision(payload.get("expected_revision"))
        changes = {key: value for key, value in payload.items() if key != "expected_revision"}
        if not changes or set(changes) - _EDITABLE:
            raise FinanceValidationError("科目修改包含不支持的字段或没有修改。")
        values: dict[str, Any] = {}
        if "name" in changes:
            values["name"] = _name(changes["name"])
        for flag, label in (("is_active", "启用"), ("frozen", "冻结"), ("requires_waybill", "必填运单号")):
            if flag in changes:
                values[flag] = int(_flag(changes[flag], label))
        if "cash_flow_category" in changes:
            category = changes["cash_flow_category"] or None
            values["cash_flow_category"] = category if category is None else _choice(category, CASH_FLOW_CATEGORIES, "现金流分类")
        if "account_type" in changes:
            values["account_type"] = _choice(changes["account_type"], ACCOUNT_TYPES, "科目属性")

        def work(cursor: Any) -> dict[str, Any]:
            current = self._lock_account(cursor, code)
            if int(current["revision"]) != revision:
                raise FinanceError("FINANCE_REVISION_CONFLICT", "科目已被他人修改，请刷新后重试。")
            if values.get("account_type", current["account_type"]) != current["account_type"] and self._account_in_use(cursor, code):
                raise FinanceError("ACCOUNT_HAS_ENTRIES", "科目已有凭证或分录，不能修改科目属性。")
            if values.get("is_active") == 0 and self._account_is_default(cursor, code):
                raise FinanceError("ACCOUNT_IS_DEFAULT", "该科目是财务设置中的默认科目，不能停用。")
            assignments = ", ".join(f"{key}=%s" for key in values)
            cursor.execute(f"UPDATE finance_accounts SET {assignments}, revision=revision+1 WHERE code=%s", (*values.values(), code))
            cursor.execute(f"SELECT {ACCOUNT_COLUMNS} FROM finance_accounts WHERE code=%s", (code,))
            return account_view(fetchone(cursor))

        return self._write(request_id=request_id, action="account.update", target_type="account", target_id=code,
                           payload=payload, actor=actor, work=work)

    def delete_account(self, code: Any, payload: dict[str, Any], *, request_id: Any, actor: FinanceActor) -> dict[str, Any]:
        code = parse_account_code(code)
        revision = expected_revision((payload or {}).get("expected_revision"))

        def work(cursor: Any) -> dict[str, Any]:
            current = self._lock_account(cursor, code)
            if int(current["revision"]) != revision:
                raise FinanceError("FINANCE_REVISION_CONFLICT", "科目已被他人修改，请刷新后重试。")
            cursor.execute("SELECT EXISTS(SELECT 1 FROM finance_accounts WHERE parent_code=%s) AS present", (code,))
            if fetchone(cursor)["present"]:
                raise FinanceError("ACCOUNT_HAS_CHILDREN", "请先删除全部下级科目。")
            if self._account_in_use(cursor, code):
                raise FinanceError("ACCOUNT_HAS_ENTRIES", "科目已有凭证或分录，只能停用，不能删除。")
            if self._account_is_default(cursor, code):
                raise FinanceError("ACCOUNT_IS_DEFAULT", "该科目是财务设置中的默认科目，不能删除。")
            parent_code = current["parent_code"]
            if parent_code:
                self._lock_account(cursor, parent_code)
            cursor.execute("DELETE FROM finance_accounts WHERE code=%s", (code,))
            if parent_code:
                cursor.execute(
                    "UPDATE finance_accounts SET is_group=0, revision=revision+1 WHERE code=%s"
                    " AND NOT EXISTS (SELECT 1 FROM (SELECT code FROM finance_accounts WHERE parent_code=%s) AS siblings)",
                    (parent_code, parent_code),
                )
            return {"id": code, "deleted": True}

        return self._write(request_id=request_id, action="account.delete", target_type="account", target_id=code,
                           payload=payload, actor=actor, work=work)
