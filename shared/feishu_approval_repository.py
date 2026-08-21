"""MySQL persistence for Feishu administrator bindings and serial approvals."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from shared.orchestration_repository_support import (
    RepositoryBase,
    _required_text,
    _row_dict,
    _rows,
)


class FeishuApprovalRepository(RepositoryBase):
    def _lock_binding(self, binding_id: str) -> dict[str, Any] | None:
        """Acquire the queue's first lock: binding before any delivery row."""

        safe_binding_id = _required_text(binding_id, "binding_id")
        with self.cursor() as cursor:
            cursor.execute(
                "SELECT binding_id FROM feishu_admin_bindings WHERE binding_id=%s FOR UPDATE",
                (safe_binding_id,),
            )
            return _row_dict(cursor, cursor.fetchone())

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
                    challenge_id, admin_user_id, code_sha256, expires_at
                ) VALUES (%s, %s, %s, %s)
                """,
                (
                    _required_text(row.get("challenge_id"), "challenge_id"),
                    int(row["admin_user_id"]),
                    _required_text(row.get("code_sha256"), "code_sha256"),
                    row["expires_at"],
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
                f"SELECT * FROM feishu_binding_failures WHERE open_id=%s{suffix}",
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
            cursor.execute(
                """
                SELECT binding_id, admin_user_id, open_id
                FROM feishu_admin_bindings
                WHERE admin_user_id=%s OR open_id=%s
                ORDER BY admin_user_id FOR UPDATE
                """,
                (int(admin_user_id), _required_text(open_id, "open_id")),
            )
            bindings = _rows(cursor)
            open_binding = next(
                (
                    row
                    for row in bindings
                    if str(row.get("open_id") or "") == str(open_id)
                ),
                None,
            )
            if open_binding is not None and int(open_binding["admin_user_id"]) != int(
                admin_user_id
            ):
                raise ValueError("Feishu identity is already bound to another administrator")
            admin_binding = next(
                (
                    row
                    for row in bindings
                    if int(row.get("admin_user_id") or 0) == int(admin_user_id)
                ),
                None,
            )
            cursor.execute(
                """
                UPDATE feishu_admin_binding_challenges
                SET used_at=NOW(6)
                WHERE challenge_id=%s AND used_at IS NULL AND expires_at>NOW(6)
                """,
                (_required_text(challenge_id, "challenge_id"),),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ValueError("binding challenge is no longer usable")
            if admin_binding is None:
                cursor.execute(
                    """
                    INSERT INTO feishu_admin_bindings (
                        binding_id, admin_user_id, open_id, last_chat_id,
                        notifications_enabled, active
                    ) VALUES (%s, %s, %s, %s, TRUE, TRUE)
                    """,
                    (
                        _required_text(binding_id, "binding_id"),
                        int(admin_user_id),
                        _required_text(open_id, "open_id"),
                        _required_text(chat_id, "chat_id"),
                    ),
                )
            else:
                cursor.execute(
                    """
                    UPDATE feishu_admin_bindings
                    SET open_id=%s, last_chat_id=%s,
                        notifications_enabled=TRUE, active=TRUE,
                        revoked_at=NULL, updated_at=NOW(6)
                    WHERE binding_id=%s AND admin_user_id=%s
                    """,
                    (
                        _required_text(open_id, "open_id"),
                        _required_text(chat_id, "chat_id"),
                        admin_binding["binding_id"],
                        int(admin_user_id),
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
                JOIN admin_users AS admin ON admin.id=binding.admin_user_id
                WHERE binding.open_id=%s{suffix}
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
                FROM feishu_admin_bindings WHERE admin_user_id=%s
                """,
                (int(admin_user_id),),
            )
            return _row_dict(cursor, cursor.fetchone())

    def revoke(self, admin_user_id: int) -> None:
        with self.cursor() as cursor:
            cursor.execute(
                """
                UPDATE feishu_admin_bindings
                SET active=FALSE, revoked_at=NOW(6), updated_at=NOW(6)
                WHERE admin_user_id=%s
                """,
                (int(admin_user_id),),
            )

    def enqueue_for_enabled_admins(self, approval_id: str, plan_hash: str) -> list[str]:
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT binding.binding_id
                FROM feishu_admin_bindings AS binding
                JOIN admin_users AS admin ON admin.id=binding.admin_user_id
                WHERE binding.active=TRUE AND binding.notifications_enabled=TRUE
                  AND admin.is_active=1 AND admin.control_plane_role='super_admin'
                ORDER BY binding.binding_id
                FOR UPDATE
                """
            )
            binding_ids = [str(row["binding_id"]) for row in _rows(cursor)]
            for binding_id in binding_ids:
                cursor.execute(
                    """
                    INSERT IGNORE INTO feishu_approval_deliveries (
                        delivery_id, approval_id, binding_id, plan_hash
                    ) VALUES (UUID(), %s, %s, %s)
                    """,
                    (approval_id, binding_id, plan_hash),
                )
                self.activate_next(binding_id)
            return binding_ids

    def activate_next(self, binding_id: str) -> dict[str, Any] | None:
        safe_binding_id = _required_text(binding_id, "binding_id")
        if self._lock_binding(safe_binding_id) is None:
            return None
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT delivery_id FROM feishu_approval_deliveries
                WHERE binding_id=%s AND status='ACTIVE' FOR UPDATE
                """,
                (safe_binding_id,),
            )
            active = _row_dict(cursor, cursor.fetchone())
            if active is None:
                cursor.execute(
                    """
                    SELECT delivery_id FROM feishu_approval_deliveries
                    WHERE binding_id=%s AND status='QUEUED'
                    ORDER BY created_at, delivery_id LIMIT 1 FOR UPDATE
                    """,
                    (safe_binding_id,),
                )
                queued = _row_dict(cursor, cursor.fetchone())
                if queued is not None:
                    cursor.execute(
                        """
                        UPDATE feishu_approval_deliveries
                        SET status='ACTIVE', activated_at=NOW(6), updated_at=NOW(6)
                        WHERE delivery_id=%s AND status='QUEUED'
                        """,
                        (queued["delivery_id"],),
                    )
            return self.active_for_binding(safe_binding_id, for_update=True)

    def active_for_open_id(self, open_id: str, *, for_update: bool = False) -> dict[str, Any] | None:
        binding = self.resolve_binding(open_id, for_update=for_update)
        if binding is None:
            return None
        return self.active_for_binding(str(binding["binding_id"]), for_update=for_update)

    def active_for_binding(self, binding_id: str, *, for_update: bool = False) -> dict[str, Any] | None:
        safe_binding_id = _required_text(binding_id, "binding_id")
        with self.cursor() as cursor:
            if for_update:
                if self._lock_binding(safe_binding_id) is None:
                    return None
                # Serialize only the delivery queue row here.  Approval
                # decisions and project invalidation consistently lock
                # Run -> Approval; a joined FOR UPDATE across delivery,
                # Approval and Run would let MySQL choose the reverse order
                # and deadlock a Feishu reply against Console approval.
                cursor.execute(
                    """
                    SELECT delivery_id
                    FROM feishu_approval_deliveries
                    WHERE binding_id=%s AND status='ACTIVE'
                    FOR UPDATE
                    """,
                    (safe_binding_id,),
                )
                delivery = _row_dict(cursor, cursor.fetchone())
                if delivery is None:
                    return None
                where_clause = "delivery.delivery_id=%s"
                params = (str(delivery["delivery_id"]),)
            else:
                where_clause = (
                    "delivery.binding_id=%s AND delivery.status='ACTIVE'"
                )
                params = (safe_binding_id,)
            cursor.execute(
                f"""
                SELECT delivery.*, binding.open_id, binding.last_chat_id,
                       approval.status AS approval_status, approval.expires_at,
                       approval.risk_level, approval.required_role,
                       command.automation_id AS automation_id, command.source,
                       (
                           SELECT GROUP_CONCAT(
                               DISTINCT step.tool_name
                               ORDER BY step.tool_name SEPARATOR ', '
                           )
                           FROM agent_run_steps AS step
                           WHERE step.run_id=run.run_id
                       ) AS tool_names
                FROM feishu_approval_deliveries AS delivery
                JOIN feishu_admin_bindings AS binding ON binding.binding_id=delivery.binding_id
                JOIN approval_requests AS approval ON approval.approval_id=delivery.approval_id
                JOIN agent_runs AS run ON run.run_id=approval.run_id
                JOIN agent_commands AS command ON command.command_id=run.command_id
                WHERE {where_clause}
                """,
                params,
            )
            return _row_dict(cursor, cursor.fetchone())

    def mark_notified(self, delivery_id: str) -> None:
        with self.cursor() as cursor:
            cursor.execute(
                """
                UPDATE feishu_approval_deliveries
                SET notified_at=NOW(6), updated_at=NOW(6)
                WHERE delivery_id=%s AND status='ACTIVE'
                """,
                (_required_text(delivery_id, "delivery_id"),),
            )

    def finish_approval(self, approval_id: str, *, status: str = "DECIDED") -> list[str]:
        """Finish an approval across queues when no binding lock is pre-held.

        Cross-queue callers acquire every Binding in sorted order.  Code that
        already holds one Binding must use ``finish_active_for_binding`` so it
        cannot expand its lock set against another administrator.
        """

        safe_approval_id = _required_text(approval_id, "approval_id")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT DISTINCT binding_id FROM feishu_approval_deliveries
                WHERE approval_id=%s ORDER BY binding_id
                """,
                (safe_approval_id,),
            )
            binding_ids = [str(row["binding_id"]) for row in _rows(cursor)]
            for binding_id in binding_ids:
                self._lock_binding(binding_id)
            cursor.execute(
                """
                UPDATE feishu_approval_deliveries
                SET status=%s, decided_at=NOW(6), updated_at=NOW(6)
                WHERE approval_id=%s AND status IN ('ACTIVE', 'QUEUED')
                """,
                (_required_text(status, "status"), safe_approval_id),
            )
            for binding_id in binding_ids:
                self.activate_next(binding_id)
            return binding_ids

    def finish_active_for_binding(
        self,
        binding_id: str,
        approval_id: str,
        *,
        status: str = "DECIDED",
    ) -> list[str]:
        """Finish only one binding's current delivery and advance its queue."""

        safe_binding_id = _required_text(binding_id, "binding_id")
        safe_approval_id = _required_text(approval_id, "approval_id")
        if self._lock_binding(safe_binding_id) is None:
            return []
        with self.cursor() as cursor:
            cursor.execute(
                """
                UPDATE feishu_approval_deliveries
                SET status=%s, decided_at=NOW(6), updated_at=NOW(6)
                WHERE binding_id=%s AND approval_id=%s
                  AND status IN ('ACTIVE', 'QUEUED')
                """,
                (
                    _required_text(status, "status"),
                    safe_binding_id,
                    safe_approval_id,
                ),
            )
        self.activate_next(safe_binding_id)
        return [safe_binding_id]

    def expire_approval_if_due(self, approval_id: str) -> bool:
        with self.cursor() as cursor:
            cursor.execute(
                """
                UPDATE approval_requests
                SET status='EXPIRED', decided_at=NOW(6)
                WHERE approval_id=%s AND status='PENDING' AND expires_at<=NOW(6)
                """,
                (_required_text(approval_id, "approval_id"),),
            )
            return int(getattr(cursor, "rowcount", 0) or 0) == 1
