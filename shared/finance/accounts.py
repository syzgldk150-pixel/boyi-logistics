"""Exact account-manager binding for finance collection."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from shared.finance.models import AccountBinding


class AccountBindingError(LookupError):
    """Base error for an unsafe or unavailable finance account binding."""


class AccountBindingNotFoundError(AccountBindingError):
    pass


class AccountBindingAmbiguousError(AccountBindingError):
    pass


class AccountBindingInvalidError(AccountBindingError):
    pass


def resolve_account_binding(
    accounts: Iterable[Mapping[str, Any]],
    *,
    system: str,
    login_account: str,
) -> AccountBinding:
    """Resolve exactly one active account by ``system + login_account``.

    This intentionally does not inspect ``is_default`` and never falls back to
    another account or session profile.
    """

    expected_system = str(system or "").strip()
    expected_login = str(login_account or "").strip()
    if not expected_system or not expected_login:
        raise AccountBindingInvalidError("system and login_account are required")

    exact_rows: list[Mapping[str, Any]] = []
    for row in accounts:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("system") or "").strip() != expected_system:
            continue
        if str(row.get("login_account") or "").strip() != expected_login:
            continue
        exact_rows.append(row)

    if not exact_rows:
        raise AccountBindingNotFoundError(
            f"no account matches system={expected_system!r} and login_account={expected_login!r}"
        )

    if len(exact_rows) != 1:
        raise AccountBindingAmbiguousError(
            f"multiple accounts match system={expected_system!r} and login_account={expected_login!r}"
        )
    row = exact_rows[0]
    if "is_active" not in row:
        raise AccountBindingInvalidError(
            "matching account is missing explicit is_active state"
        )
    active_value = row.get("is_active")
    if active_value in (True, 1, "1", "true", "True"):
        is_active = True
    elif active_value in (False, 0, "0", "false", "False"):
        is_active = False
    else:
        raise AccountBindingInvalidError(
            "matching account has an invalid explicit is_active state"
        )
    if not is_active:
        raise AccountBindingNotFoundError(
            f"matching account is disabled for system={expected_system!r} and login_account={expected_login!r}"
        )
    account_id = str(row.get("account_id") or "").strip()
    session_profile = str(row.get("session_profile") or "").strip()
    if not account_id or not session_profile:
        missing = "account_id" if not account_id else "session_profile"
        raise AccountBindingInvalidError(f"matching account is missing {missing}")

    return AccountBinding(
        account_id=account_id,
        system=expected_system,
        login_account=expected_login,
        session_profile=session_profile,
        display_name=str(row.get("name") or "").strip(),
    )
