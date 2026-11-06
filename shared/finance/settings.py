"""Finance settings: the GL start month (locked once entries exist) and default accounts."""

from __future__ import annotations

from typing import Any

from shared.finance.db import fetchone
from shared.finance.errors import FinanceError, FinanceValidationError
from shared.finance.models import FinanceActor, expected_revision, parse_account_code, parse_month

DEFAULT_ACCOUNT_FIELDS = {
    # field: (allowed account types, label)
    "suspense_account": (("suspense",), "暂记科目"),
    "transit_account": (("transit",), "资金在途科目"),
    "current_profit_account": (("equity",), "本年利润科目"),
    "retained_earnings_account": (("equity",), "未分配利润科目"),
}
_SETTINGS_COLUMNS = "start_month, suspense_account, transit_account, current_profit_account, retained_earnings_account, revision, updated_by, updated_at"


def _settings_view(row: dict[str, Any], *, locked: bool) -> dict[str, Any]:
    return {
        "start_month": row["start_month"],
        "start_month_locked": locked,
        **{field: row[field] for field in DEFAULT_ACCOUNT_FIELDS},
        "revision": int(row["revision"]),
        "updated_by": row["updated_by"],
        "updated_at": row["updated_at"].isoformat(sep=" ", timespec="seconds") if row.get("updated_at") else None,
    }


class SettingsMixin:
    @staticmethod
    def _load_settings(cursor: Any, *, lock: str = "") -> dict[str, Any]:
        cursor.execute(f"SELECT {_SETTINGS_COLUMNS} FROM finance_settings WHERE id=1 {lock}")
        row = fetchone(cursor)
        if row is None:
            raise FinanceError("FINANCE_SETTINGS_MISSING", "财务设置缺失，请确认迁移 068 已执行。", http_status=503)
        return row

    @staticmethod
    def _ledger_has_entries(cursor: Any) -> bool:
        cursor.execute("SELECT id FROM finance_gl_entries LIMIT 1")
        return cursor.fetchone() is not None

    def get_settings(self) -> dict[str, Any]:
        def work(cursor: Any) -> dict[str, Any]:
            return _settings_view(self._load_settings(cursor), locked=self._ledger_has_entries(cursor))

        return self._read(work)

    def update_settings(self, payload: dict[str, Any], *, request_id: Any, actor: FinanceActor) -> dict[str, Any]:
        """Change the start month or default accounts (super administrator only)."""

        if not isinstance(payload, dict):
            raise FinanceValidationError("设置内容无效。")
        revision = expected_revision(payload.get("expected_revision"))
        changes: dict[str, Any] = {}
        if "start_month" in payload:
            changes["start_month"] = parse_month(payload["start_month"], "启用月")
        for field in DEFAULT_ACCOUNT_FIELDS:
            if field in payload:
                changes[field] = parse_account_code(payload[field])
        unknown = set(payload) - {"expected_revision", "start_month", *DEFAULT_ACCOUNT_FIELDS}
        if unknown or not changes:
            raise FinanceValidationError("设置内容包含不支持的字段或没有修改。")

        def work(cursor: Any) -> dict[str, Any]:
            current = self._load_settings(cursor, lock="FOR UPDATE")
            if int(current["revision"]) != revision:
                raise FinanceError("FINANCE_REVISION_CONFLICT", "设置已被他人修改，请刷新后重试。")
            has_entries = self._ledger_has_entries(cursor)
            if changes.get("start_month", current["start_month"]) != current["start_month"] and has_entries:
                raise FinanceError("START_MONTH_LOCKED", "总账已有分录，启用月不能再修改。")
            if "start_month" in changes:
                cursor.execute(
                    "SELECT MIN(posting_date) AS earliest FROM finance_vouchers WHERE status='draft'"
                )
                earliest = fetchone(cursor)["earliest"]
                if earliest is not None and f"{earliest:%Y-%m}" < changes["start_month"]:
                    raise FinanceError("START_MONTH_AFTER_DRAFTS", "存在早于该启用月的草稿凭证，请先修改或删除这些草稿。")
            for field, (types, label) in DEFAULT_ACCOUNT_FIELDS.items():
                if field not in changes:
                    continue
                cursor.execute(
                    "SELECT account_type, is_group, is_active FROM finance_accounts WHERE code=%s FOR SHARE",
                    (changes[field],),
                )
                account = fetchone(cursor)
                if not account or account["is_group"] or not account["is_active"] or account["account_type"] not in types:
                    raise FinanceValidationError(f"{label}必须是已启用的末级科目，类型为 {'/'.join(types)}。", code="FINANCE_INVALID_DEFAULT_ACCOUNT")
            assignments = ", ".join(f"{field}=%s" for field in changes)
            cursor.execute(
                f"UPDATE finance_settings SET {assignments}, revision=revision+1, updated_by=%s WHERE id=1",
                (*changes.values(), actor.label),
            )
            return _settings_view(self._load_settings(cursor), locked=has_entries)

        return self._write(request_id=request_id, action="settings.update", target_type="settings", target_id=1,
                           payload=payload, actor=actor, work=work)
