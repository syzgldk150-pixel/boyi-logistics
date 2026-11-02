"""MySQL persistence for Feishu identity bindings and binding challenges."""

from __future__ import annotations

from typing import Any, Mapping

from shared.orchestration_repository_support import (
    RepositoryBase,
    _required_text,
    _row_dict,
    _rows,
)
from shared.identity_repository import IdentityRepository


class BindingConflict(ValueError):
    """A currently active binding must be explicitly revoked first."""


class BindingChallengeExpired(ValueError):
    """The one-time challenge expired or was already consumed."""


class FeishuBindingRepository(RepositoryBase):
    def get_admin_user(self, admin_user_id: int, *, for_update: bool = False) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"SELECT id, username, display_name, is_active, control_plane_role FROM admin_users WHERE id=%s{suffix}",
                (int(admin_user_id),),
            )
            return _row_dict(cursor, cursor.fetchone())

    def create_challenge(self, row: Mapping[str, Any]) -> dict[str, Any]:
        with self.cursor() as cursor:
            role_id = row.get("access_role_id")
            account_id = row.get("identity_account_id")
            IdentityRepository.check_target(cursor, role_id=role_id, account_id=account_id)
            cursor.execute(
                """
                UPDATE feishu_admin_binding_challenges
                SET used_at=COALESCE(used_at, NOW(6))
                WHERE admin_user_id=%s AND used_at IS NULL
                """,
                (int(row["admin_user_id"]),),
            )
            cursor.execute(
                """
                INSERT INTO feishu_admin_binding_challenges (
                    challenge_id, admin_user_id, code_sha256, expires_at,
                    access_role_id, identity_account_id, identity_label
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    _required_text(row.get("challenge_id"), "challenge_id"),
                    int(row["admin_user_id"]),
                    _required_text(row.get("code_sha256"), "code_sha256"),
                    row["expires_at"],
                    role_id, account_id, str(row.get("identity_label") or ""),
                ),
            )
            cursor.execute(
                "SELECT * FROM feishu_admin_binding_challenges WHERE challenge_id=%s",
                (row["challenge_id"],),
            )
            return _row_dict(cursor, cursor.fetchone()) or {}

    def get_challenge_by_digest(self, digest: str, *, for_update: bool = False) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT challenge.*, admin.is_active, admin.control_plane_role,
                       admin.display_name
                FROM feishu_admin_binding_challenges AS challenge
                JOIN admin_users AS admin ON admin.id=challenge.admin_user_id
                WHERE challenge.code_sha256=%s{suffix}
                """,
                (_required_text(digest, "code_sha256"),),
            )
            return _row_dict(cursor, cursor.fetchone())

    def failure_state(self, open_id: str, *, for_update: bool = False) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"SELECT *, (locked_until>NOW(6)) AS is_locked FROM feishu_binding_failures WHERE open_id=%s{suffix}",
                (_required_text(open_id, "open_id"),),
            )
            return _row_dict(cursor, cursor.fetchone())

    def record_failure(self, open_id: str, *, lock_minutes: int = 10) -> dict[str, Any]:
        safe_open_id = _required_text(open_id, "open_id")
        with self.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO feishu_binding_failures (open_id, failed_attempts)
                VALUES (%s, 1)
                ON DUPLICATE KEY UPDATE
                    locked_until=IF(
                        window_started_at >= NOW(6)-INTERVAL 10 MINUTE
                            AND failed_attempts >= 4,
                        NOW(6)+INTERVAL %s MINUTE,
                        IF(
                            window_started_at < NOW(6)-INTERVAL 10 MINUTE,
                            NULL,
                            locked_until
                        )
                    ),
                    failed_attempts=IF(
                        window_started_at < NOW(6)-INTERVAL 10 MINUTE,
                        1,
                        failed_attempts+1
                    ),
                    window_started_at=IF(
                        window_started_at < NOW(6)-INTERVAL 10 MINUTE,
                        NOW(6),
                        window_started_at
                    ),
                    updated_at=NOW(6)
                """,
                (safe_open_id, max(1, int(lock_minutes))),
            )
            return self.failure_state(safe_open_id, for_update=True) or {}

    def consume_challenge(
        self,
        *,
        challenge_id: str,
        admin_user_id: int,
        binding_id: str,
        open_id: str,
        chat_id: str,
    ) -> dict[str, Any]:
        with self.cursor() as cursor:
            cursor.execute("SELECT * FROM feishu_admin_binding_challenges WHERE challenge_id=%s AND admin_user_id=%s FOR UPDATE",
                           (challenge_id, int(admin_user_id)))
            challenge = _row_dict(cursor, cursor.fetchone())
            if not challenge:
                raise BindingChallengeExpired("binding challenge is no longer usable")
            IdentityRepository.check_target(cursor, role_id=challenge["access_role_id"], account_id=challenge["identity_account_id"])
            cursor.execute(
                """
                SELECT binding_id, admin_user_id, open_id
                FROM feishu_admin_bindings
                WHERE active=TRUE AND open_id=%s
                ORDER BY admin_user_id FOR UPDATE
                """,
                (_required_text(open_id, "open_id"),),
            )
            bindings = _rows(cursor)
            if bindings:
                raise BindingConflict("Feishu identity already has an active binding")
            cursor.execute(
                """
                UPDATE feishu_admin_binding_challenges
                SET used_at=NOW(6)
                WHERE challenge_id=%s AND used_at IS NULL AND expires_at>UTC_TIMESTAMP(6)
                """,
                (_required_text(challenge_id, "challenge_id"),),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise BindingChallengeExpired("binding challenge is no longer usable")
            cursor.execute(
                    """
                    INSERT INTO feishu_admin_bindings (
                        binding_id, admin_user_id, open_id, last_chat_id,
                        notifications_enabled, active, access_role_id, identity_account_id, identity_label
                    ) VALUES (%s, %s, %s, %s, TRUE, TRUE, %s, %s, %s)
                    """,
                    (
                        _required_text(binding_id, "binding_id"),
                        int(admin_user_id),
                        _required_text(open_id, "open_id"),
                        _required_text(chat_id, "chat_id"),
                        challenge["access_role_id"], challenge["identity_account_id"], challenge["identity_label"],
                    ),
                )
            cursor.execute("DELETE FROM feishu_binding_failures WHERE open_id=%s", (open_id,))
            return self.resolve_binding(open_id, for_update=True) or {}

    def resolve_binding(self, open_id: str, *, for_update: bool = False) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT binding.*, admin.is_active, admin.control_plane_role,
                       admin.display_name, admin.username
                FROM feishu_admin_bindings AS binding
                LEFT JOIN admin_users AS admin ON admin.id=binding.identity_account_id
                WHERE binding.open_id=%s AND binding.active=TRUE{suffix}
                """,
                (_required_text(open_id, "open_id"),),
            )
            return _row_dict(cursor, cursor.fetchone())

    def get_binding_for_admin(self, admin_user_id: int) -> dict[str, Any] | None:
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT binding_id, open_id, notifications_enabled, active,
                       bound_at, revoked_at, updated_at
                FROM feishu_admin_bindings WHERE identity_account_id=%s AND active=TRUE
                """,
                (int(admin_user_id),),
            )
            rows = _rows(cursor)
            return {"active": True, "notifications_enabled": any(row["notifications_enabled"] for row in rows)} if rows else None

    def revoke(self, admin_user_id: int) -> None:
        with self.cursor() as cursor:
            cursor.execute(
                """
                UPDATE feishu_admin_bindings
                SET active=FALSE, revoked_at=NOW(6), updated_at=NOW(6)
                WHERE identity_account_id=%s
                """,
                (int(admin_user_id),),
            )
