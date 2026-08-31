"""Feishu super-admin binding and durable serial approval delivery."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from agent.feishu_command_contract import match_feishu_approval_binding
from agent.orchestration.models import Actor, ActorType, OrchestrationError
from agent.orchestration.outbox_dispatcher import OutboxRetryAfter


_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
DEFAULT_NOTIFICATION_LEASE_SECONDS = 120


class FeishuApprovalService:
    def __init__(
        self,
        repository: Any,
        approval_service: Any,
        *,
        send_text: Callable[[str, str, str], bool],
        notification_lease_seconds: int = DEFAULT_NOTIFICATION_LEASE_SECONDS,
    ) -> None:
        self._repository = repository
        self._approvals = approval_service
        self._send_text = send_text
        if (
            isinstance(notification_lease_seconds, bool)
            or not isinstance(notification_lease_seconds, int)
            or notification_lease_seconds <= 0
        ):
            raise ValueError("notification_lease_seconds must be a positive integer")
        self._notification_lease_seconds = notification_lease_seconds

    @staticmethod
    def _digest(code: str) -> str:
        return hashlib.sha256(code.encode("ascii")).hexdigest()

    def create_binding_challenge(self, admin_user_id: int) -> dict[str, Any]:
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(10))
        expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10)
        challenge_id = str(uuid.uuid4())
        with self._repository.unit_of_work() as uow:
            admin = uow.feishu_approvals.get_admin_user(admin_user_id, for_update=True)
            if (
                admin is None
                or admin.get("is_active") not in {True, 1}
                or str(admin.get("control_plane_role") or "") != "super_admin"
            ):
                raise OrchestrationError(
                    "FEISHU_BINDING_FORBIDDEN",
                    "Only an active Console super administrator can create a binding code",
                )
            uow.feishu_approvals.create_challenge(
                {
                    "challenge_id": challenge_id,
                    "admin_user_id": int(admin_user_id),
                    "code_sha256": self._digest(code),
                    "expires_at": expires_at,
                }
            )
            uow.commit()
        return {
            "challenge_id": challenge_id,
            "binding_code": code,
            "expires_at": expires_at.isoformat(),
            "command": f"绑定审批 {code}",
        }

    def binding_status(self, admin_user_id: int) -> dict[str, Any]:
        with self._repository.unit_of_work() as uow:
            admin = uow.feishu_approvals.get_admin_user(admin_user_id)
            binding = (
                uow.feishu_approvals.get_binding_for_admin(admin_user_id)
                if admin is not None
                else None
            )
        is_bound = bool(binding and binding.get("active") in {True, 1})
        public_binding = None
        if binding is not None:
            public_binding = {
                "notifications_enabled": bool(binding.get("notifications_enabled")),
                "active": bool(binding.get("active")),
                "bound_at": binding.get("bound_at"),
                "revoked_at": binding.get("revoked_at"),
                "updated_at": binding.get("updated_at"),
            }
        return {"bound": is_bound, "binding": public_binding}

    def revoke_binding(self, admin_user_id: int) -> None:
        with self._repository.unit_of_work() as uow:
            uow.feishu_approvals.revoke(admin_user_id)
            uow.commit()

    def resolve_actor(self, open_id: str) -> Actor:
        with self._repository.unit_of_work() as uow:
            binding = uow.feishu_approvals.resolve_binding(open_id)
        if (
            binding
            and binding.get("active") in {True, 1}
            and binding.get("is_active") in {True, 1}
            and str(binding.get("control_plane_role") or "") == "super_admin"
        ):
            return Actor(
                ActorType.FEISHU_USER,
                str(open_id),
                roles=("admin", "super_admin"),
                display_name=str(binding.get("display_name") or binding.get("username") or ""),
                authenticated_by="feishu_admin_binding",
            )
        return Actor(
            ActorType.FEISHU_USER,
            str(open_id),
            roles=(),
            authenticated_by="feishu_verified_event",
        )

    def handle_text(self, open_id: str, chat_id: str, text: str) -> str | None:
        normalized = str(text or "").strip()
        match = match_feishu_approval_binding(normalized)
        if match:
            return self._bind(open_id, chat_id, match.group(1).upper())
        if normalized not in {"1", "2"}:
            return None
        actor = self.resolve_actor(open_id)
        if "super_admin" not in actor.roles:
            return None
        stale_binding_ids: list[str] | None = None
        with self._repository.unit_of_work() as uow:
            active = uow.feishu_approvals.active_for_open_id(open_id, for_update=True)
            if active is None:
                uow.commit()
                return None
            approval_id = str(active["approval_id"])
            plan_hash = str(active["plan_hash"])
            approval_status = str(active.get("approval_status") or "")
            expires_at = active.get("expires_at")
            if approval_status != "PENDING" or (
                isinstance(expires_at, datetime)
                and expires_at <= datetime.now(timezone.utc).replace(tzinfo=None)
            ):
                stale_binding_ids = uow.feishu_approvals.finish_active_for_binding(
                    str(active["binding_id"]),
                    approval_id,
                    status="EXPIRED" if approval_status == "PENDING" else "SKIPPED",
                )
                self._append_notification_retry(
                    uow,
                    approval_id=approval_id,
                    binding_ids=stale_binding_ids,
                )
                uow.commit()
            else:
                uow.commit()
        if stale_binding_ids is not None:
            self._notify_active_bindings(
                stale_binding_ids,
                retry_leased=False,
            )
            return "当前审批已过期或已由其他管理员处理，已切换到下一条。"
        decision = "APPROVED" if normalized == "1" else "REJECTED"
        try:
            self._approvals.decide(
                approval_id=approval_id,
                plan_hash=plan_hash,
                actor=actor,
                source="feishu",
                decision=decision,
                comment="Feishu text decision",
            )
        except OrchestrationError as exc:
            if exc.code not in {
                "APPROVAL_NOT_PENDING",
                "APPROVAL_EXPIRED",
                "PLAN_STALE",
            }:
                raise
            with self._repository.unit_of_work() as uow:
                binding_ids = uow.feishu_approvals.finish_approval(
                    approval_id,
                    status="EXPIRED" if exc.code == "APPROVAL_EXPIRED" else "SKIPPED",
                )
                self._append_notification_retry(
                    uow,
                    approval_id=approval_id,
                    binding_ids=binding_ids,
                )
                uow.commit()
            self._notify_active_bindings(binding_ids, retry_leased=False)
            return "当前审批已过期或已由其他管理员处理，已切换到下一条。"
        return "已批准，原事项已恢复执行。" if decision == "APPROVED" else "已驳回该审批。"

    def _bind(self, open_id: str, chat_id: str, code: str) -> str:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            with self._repository.unit_of_work() as uow:
                failure = uow.feishu_approvals.failure_state(open_id, for_update=True)
                if failure and isinstance(failure.get("locked_until"), datetime) and failure["locked_until"] > now:
                    uow.commit()
                    return "绑定尝试过多，请稍后再试。"
                challenge = uow.feishu_approvals.get_challenge_by_digest(
                    self._digest(code),
                    for_update=True,
                )
                valid = bool(
                    challenge
                    and challenge.get("used_at") is None
                    and isinstance(challenge.get("expires_at"), datetime)
                    and challenge["expires_at"] > now
                    and challenge.get("is_active") in {True, 1}
                    and str(challenge.get("control_plane_role") or "") == "super_admin"
                )
                if not valid:
                    uow.feishu_approvals.record_failure(open_id)
                    uow.commit()
                    return "绑定码无效、已过期或已使用。"
                uow.feishu_approvals.consume_challenge(
                    challenge_id=str(challenge["challenge_id"]),
                    admin_user_id=int(challenge["admin_user_id"]),
                    binding_id=str(uuid.uuid4()),
                    open_id=open_id,
                    chat_id=chat_id,
                )
                uow.commit()
        except ValueError:
            return "该飞书身份已绑定其他后台管理员，请先由原账号解绑。"
        return "审批身份绑定成功；账号权限变更或解绑会立即生效。"

    def handle_outbox(
        self,
        delivery: Mapping[str, Any],
        _uow: Any,
    ) -> Mapping[str, Any]:
        topic = str(delivery.get("topic") or "")
        approval_id = str(delivery.get("entity_id") or "")
        payload = delivery.get("payload_json")
        plan_hash = str((payload or {}).get("plan_hash") or "") if isinstance(payload, Mapping) else ""
        with self._repository.unit_of_work() as uow:
            if topic == "agent.feishu.notification.retry":
                binding_ids = self._retry_binding_ids(payload)
            elif topic == "agent.approval.requested":
                binding_ids = uow.feishu_approvals.enqueue_for_enabled_admins(
                    approval_id,
                    plan_hash,
                )
            elif topic == "agent.approval.expiry_check":
                uow.feishu_approvals.expire_approval_if_due(approval_id)
                binding_ids = uow.feishu_approvals.finish_approval(
                    approval_id,
                    status="EXPIRED",
                )
            elif topic in {
                "agent.approval.decided",
                "agent.approval.invalidated",
                "agent.approval.expired",
            }:
                finished_status = {
                    "agent.approval.decided": "DECIDED",
                    "agent.approval.invalidated": "SKIPPED",
                    "agent.approval.expired": "EXPIRED",
                }[topic]
                binding_ids = uow.feishu_approvals.finish_approval(
                    approval_id,
                    status=finished_status,
                )
            else:
                raise RuntimeError(
                    f"Unsupported Feishu approval outbox topic: {topic or '<missing>'}"
                )
            uow.commit()
        sent = self._notify_active_bindings(binding_ids, retry_leased=True)
        return {"approval_id": approval_id, "sent": sent}

    def _notify_active_bindings(
        self,
        binding_ids: list[str],
        *,
        retry_leased: bool,
    ) -> int:
        sent = 0
        for binding_id in dict.fromkeys(str(item) for item in binding_ids):
            while True:
                reservation = self._reserve_active_notification(binding_id)
                state = str(reservation.get("state") or "")
                if state == "ADVANCED":
                    continue
                if state in {"EMPTY", "NOTIFIED"}:
                    break
                if state == "LEASED":
                    if retry_leased:
                        raise OutboxRetryAfter(
                            "Feishu approval notification is already leased",
                            delay_seconds=self._lease_retry_delay(
                                reservation.get("notification_lease_expires_at")
                            ),
                        )
                    break
                if state != "RESERVED":
                    raise RuntimeError("Feishu approval notification state is invalid")
                delivery_id = str(reservation["delivery_id"])
                binding_id = str(reservation["binding_id"])
                lease_token = str(reservation["notification_lease_token"])
                message = self._approval_message(reservation)
                try:
                    delivered = self._send_text(
                        str(reservation["open_id"]),
                        message,
                        "open_id",
                    )
                except Exception:
                    self._release_notification(
                        binding_id,
                        delivery_id,
                        lease_token,
                    )
                    raise
                if not delivered:
                    self._release_notification(
                        binding_id,
                        delivery_id,
                        lease_token,
                    )
                    raise RuntimeError("Feishu approval notification failed")
                if not self._finalize_notification(
                    binding_id,
                    delivery_id,
                    lease_token,
                ):
                    raise RuntimeError(
                        "Feishu approval notification finalization lost its lease"
                    )
                sent += 1
                break
        return sent

    def _reserve_active_notification(self, binding_id: str) -> dict[str, Any]:
        lease_token = str(uuid.uuid4())
        with self._repository.unit_of_work() as uow:
            active = uow.feishu_approvals.active_for_binding(
                binding_id,
                for_update=True,
            )
            if active is None:
                live_lease = uow.feishu_approvals.notification_lease_for_binding(
                    binding_id,
                )
                if live_lease is not None:
                    uow.commit()
                    return {**live_lease, "state": "LEASED"}
                active = uow.feishu_approvals.activate_next(binding_id)
            if active is None:
                uow.commit()
                return {"state": "EMPTY"}
            approval_status = str(active.get("approval_status") or "")
            expires_at = active.get("expires_at")
            expired = bool(
                isinstance(expires_at, datetime)
                and expires_at <= datetime.now(timezone.utc).replace(tzinfo=None)
            )
            if approval_status != "PENDING" or expired:
                uow.feishu_approvals.finish_active_for_binding(
                    binding_id,
                    str(active.get("approval_id") or ""),
                    status="EXPIRED" if expired else "SKIPPED",
                )
                uow.commit()
                return {"state": "ADVANCED"}
            if active.get("notified_at") is not None:
                uow.commit()
                return {"state": "NOTIFIED"}
            reserved = uow.feishu_approvals.reserve_notification(
                binding_id,
                str(active["delivery_id"]),
                lease_token,
                lease_seconds=self._notification_lease_seconds,
            )
            uow.commit()
        if not reserved:
            return {
                "state": "LEASED",
                "notification_lease_expires_at": active.get(
                    "notification_lease_expires_at"
                ),
            }
        return {
            **active,
            "state": "RESERVED",
            "notification_lease_token": lease_token,
        }

    def _finalize_notification(
        self,
        binding_id: str,
        delivery_id: str,
        lease_token: str,
    ) -> bool:
        with self._repository.unit_of_work() as uow:
            finalized = uow.feishu_approvals.finalize_notification(
                binding_id,
                delivery_id,
                lease_token,
            )
            uow.commit()
        return bool(finalized)

    def _release_notification(
        self,
        binding_id: str,
        delivery_id: str,
        lease_token: str,
    ) -> None:
        with self._repository.unit_of_work() as uow:
            uow.feishu_approvals.release_notification(
                binding_id,
                delivery_id,
                lease_token,
                error_summary="Feishu approval notification failed",
            )
            uow.commit()

    def _lease_retry_delay(self, expires_at: Any) -> int:
        if not isinstance(expires_at, datetime):
            return self._notification_lease_seconds
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        return max(1, int((expires_at - now).total_seconds()) + 1)

    @staticmethod
    def _retry_binding_ids(payload: Any) -> list[str]:
        if not isinstance(payload, Mapping):
            raise RuntimeError("Feishu notification retry payload is invalid")
        raw_binding_ids = payload.get("binding_ids")
        if not isinstance(raw_binding_ids, list):
            raise RuntimeError("Feishu notification retry bindings are invalid")
        binding_ids = list(
            dict.fromkeys(
                str(item).strip()
                for item in raw_binding_ids
                if str(item).strip()
            )
        )
        if not binding_ids:
            raise RuntimeError("Feishu notification retry has no binding")
        return binding_ids

    @staticmethod
    def _append_notification_retry(
        uow: Any,
        *,
        approval_id: str,
        binding_ids: list[str],
    ) -> None:
        normalized_binding_ids = sorted(
            dict.fromkeys(str(item).strip() for item in binding_ids if str(item).strip())
        )
        if not normalized_binding_ids:
            return
        event_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        uow.events.append_with_outbox(
            {
                "event_id": event_id,
                "event_type": "agent.feishu.notification.retry",
                "schema_version": 1,
                "source_system": "agent",
                "source_event_id": None,
                "entity_type": "approval_request",
                "entity_id": approval_id,
                "work_item_id": None,
                "run_id": None,
                "step_id": None,
                "occurred_at": now,
                "observed_at": now,
                "correlation_id": event_id,
                "causation_id": approval_id,
                "payload": {
                    "approval_id": approval_id,
                    "binding_ids": normalized_binding_ids,
                },
            },
            (
                {
                    "consumer_name": "feishu.approval",
                    "topic": "agent.feishu.notification.retry",
                    "partition_key": approval_id,
                    "max_attempts": 20,
                },
            ),
        )

    @staticmethod
    def _approval_message(active: Mapping[str, Any]) -> str:
        project = str(active.get("automation_id") or "automation")[:128]
        tool_names = str(active.get("tool_names") or "unknown")[:256]
        source = str(active.get("source") or "unknown")[:32]
        risk = str(active.get("risk_level") or "UNKNOWN")[:16]
        plan_hash = str(active.get("plan_hash") or "")[:12]
        expires_at = active.get("expires_at")
        expires_text = expires_at.isoformat(sep=" ", timespec="minutes") if isinstance(expires_at, datetime) else "未知"
        return "\n".join(
            (
                "【自动化审批】",
                f"项目：{project}",
                f"工具：{tool_names}",
                f"来源：{source}",
                f"风险：{risk}",
                "影响摘要：执行已签名动作，并要求写后证据核验",
                f"Plan：{plan_hash}",
                f"过期：{expires_text}",
                "回复 1 批准，回复 2 驳回。",
            )
        )
