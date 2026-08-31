"""Host-owned Console boundary for waybill-entry module slots."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any, Mapping
from urllib.parse import quote
from uuid import UUID, uuid4

from shared.waybill_entry_extensions import (
    WAYBILL_ENTRY_ACTIONS_SLOT,
    WAYBILL_ENTRY_DRAFT_FIELDS,
    WAYBILL_ENTRY_EXTENSION_SLOTS,
    WAYBILL_ENTRY_VALIDATORS_SLOT,
    normalize_waybill_entry_draft,
    normalize_waybill_entry_extension_handle,
    normalize_waybill_entry_slot,
    normalize_waybill_entry_validator_result,
)


WAYBILL_ENTRY_MODULE_SLOTS_ENDPOINT = (
    "/internal/v1/automation-projects/module-slots/waybill-entry"
)
WAYBILL_ENTRY_ACTIVE_VALIDATORS_ENDPOINT = (
    f"{WAYBILL_ENTRY_MODULE_SLOTS_ENDPOINT}/validators/invoke-active"
)
WAYBILL_ENTRY_EXTENSION_TITLE_MAX_LENGTH = 120
WAYBILL_ENTRY_EXTENSION_MAX_SLOTS = 64
WAYBILL_ENTRY_ACTION_RECEIPT_FIELDS = frozenset(
    {
        "command_id",
        "work_item_id",
        "run_id",
        "status",
        "reused",
        "next_poll_after_ms",
    }
)


def _safe_extension_title(value: Any) -> str:
    if type(value) is not str:
        raise ValueError("waybill-entry extension title must be a string")
    title = value.strip()
    if (
        not title
        or title != value
        or len(title) > WAYBILL_ENTRY_EXTENSION_TITLE_MAX_LENGTH
        or any(ord(character) < 32 or ord(character) == 127 for character in title)
    ):
        raise ValueError("waybill-entry extension title is invalid")
    return title


def _normalize_module_slot_projection(value: Any) -> dict[str, tuple[dict[str, str], ...]]:
    if not isinstance(value, Mapping) or set(value) != {"module_slots"}:
        raise ValueError("waybill-entry module-slot projection is invalid")
    raw_slots = value.get("module_slots")
    if not isinstance(raw_slots, list) or len(raw_slots) > WAYBILL_ENTRY_EXTENSION_MAX_SLOTS:
        raise ValueError("waybill-entry module-slot projection is invalid")

    grouped: dict[str, list[dict[str, str]]] = {
        slot: [] for slot in WAYBILL_ENTRY_EXTENSION_SLOTS
    }
    seen: set[tuple[str, str]] = set()
    for raw_slot in raw_slots:
        if not isinstance(raw_slot, Mapping) or set(raw_slot) != {
            "slot",
            "handle",
            "title",
        }:
            raise ValueError("waybill-entry module-slot descriptor is invalid")
        slot = normalize_waybill_entry_slot(raw_slot.get("slot"))
        handle = normalize_waybill_entry_extension_handle(raw_slot.get("handle"))
        title = _safe_extension_title(raw_slot.get("title"))
        identity = (slot, handle)
        if identity in seen:
            raise ValueError("waybill-entry module-slot descriptor is duplicated")
        seen.add(identity)
        grouped[slot].append({"slot": slot, "handle": handle, "title": title})
    return {slot: tuple(grouped[slot]) for slot in WAYBILL_ENTRY_EXTENSION_SLOTS}


def _normalize_action_receipt(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != WAYBILL_ENTRY_ACTION_RECEIPT_FIELDS:
        raise ValueError("waybill-entry action receipt is invalid")
    receipt: dict[str, Any] = {}
    for field_name in ("command_id", "work_item_id", "run_id"):
        field_value = value.get(field_name)
        if type(field_value) is not str or not field_value or len(field_value) > 128:
            raise ValueError("waybill-entry action receipt identity is invalid")
        receipt[field_name] = field_value
    status = value.get("status")
    if (
        type(status) is not str
        or not status
        or len(status) > 64
        or not all(character.isupper() or character == "_" for character in status)
    ):
        raise ValueError("waybill-entry action receipt status is invalid")
    reused = value.get("reused")
    next_poll_after_ms = value.get("next_poll_after_ms")
    if type(reused) is not bool or type(next_poll_after_ms) is not int or not 0 <= next_poll_after_ms <= 60_000:
        raise ValueError("waybill-entry action receipt polling metadata is invalid")
    receipt.update(
        {
            "status": status,
            "reused": reused,
            "next_poll_after_ms": next_poll_after_ms,
        }
    )
    return receipt


def _normalize_invocation_result(slot: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != (
        {"kind", "receipt"}
        if slot == WAYBILL_ENTRY_ACTIONS_SLOT
        else {"kind", "validation"}
    ):
        raise ValueError("waybill-entry extension result is invalid")
    expected_kind = "action" if slot == WAYBILL_ENTRY_ACTIONS_SLOT else "validator"
    if value.get("kind") != expected_kind:
        raise ValueError("waybill-entry extension result kind is invalid")
    if slot == WAYBILL_ENTRY_ACTIONS_SLOT:
        return {"kind": expected_kind, "receipt": _normalize_action_receipt(value.get("receipt"))}
    return {
        "kind": expected_kind,
        "validation": normalize_waybill_entry_validator_result(value.get("validation")),
    }


def _normalize_active_validator_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"kind", "validation"}:
        raise ValueError("waybill-entry active validator result is invalid")
    if value.get("kind") != "validator_set":
        raise ValueError("waybill-entry active validator result kind is invalid")
    return {
        "kind": "validator_set",
        "validation": normalize_waybill_entry_validator_result(
            value.get("validation")
        ),
    }


class WaybillEntryExtensionsServiceMixin:
    """Render and invoke closed module slots without exposing plugin internals."""

    @staticmethod
    def _empty_waybill_entry_extensions(*, unavailable: bool = False) -> dict[str, Any]:
        return {
            WAYBILL_ENTRY_ACTIONS_SLOT: (),
            WAYBILL_ENTRY_VALIDATORS_SLOT: (),
            "unavailable": unavailable,
        }

    def _load_waybill_entry_extensions(self, handler: Any) -> dict[str, Any]:
        principal = self._mysql_console_principal(
            getattr(handler, "current_admin_user", None)
        )
        if principal is None:
            return self._empty_waybill_entry_extensions(unavailable=True)
        result = self._agent_request(
            "GET",
            WAYBILL_ENTRY_MODULE_SLOTS_ENDPOINT,
            timeout=self.settings.agent_timeout_seconds,
            console_principal=principal,
        )
        if not result.get("ok"):
            return self._empty_waybill_entry_extensions(unavailable=True)
        try:
            projection = _normalize_module_slot_projection(result.get("data"))
        except ValueError:
            return self._empty_waybill_entry_extensions(unavailable=True)
        return {
            WAYBILL_ENTRY_ACTIONS_SLOT: projection[WAYBILL_ENTRY_ACTIONS_SLOT],
            WAYBILL_ENTRY_VALIDATORS_SLOT: projection[WAYBILL_ENTRY_VALIDATORS_SLOT],
            "unavailable": False,
        }

    def _validate_active_waybill_entry_extensions(
        self,
        handler: Any,
        form_values: Mapping[str, Any],
    ) -> tuple[bool, str]:
        """Validate one manual save against the authoritative active set."""

        principal = self._mysql_console_principal(
            getattr(handler, "current_admin_user", None)
        )
        if principal is None:
            return False, "录单扩展校验需要有效的管理员登录，请重新登录后再试。"
        try:
            waybill = normalize_waybill_entry_draft(
                {
                    field_name: form_values.get(f"field_{field_name}", "")
                    for field_name in WAYBILL_ENTRY_DRAFT_FIELDS
                }
            )
        except ValueError:
            return False, "录单内容无法安全提交扩展校验，请检查后重试。"

        request_id = str(uuid4())
        try:
            result = self._agent_request(
                "POST",
                WAYBILL_ENTRY_ACTIVE_VALIDATORS_ENDPOINT,
                payload={"request_id": request_id, "waybill": waybill},
                timeout=max(35, self.settings.agent_timeout_seconds),
                console_principal=principal,
            )
        except Exception:
            return False, "录单扩展校验暂不可用，本次保存已停止。"
        if not isinstance(result, Mapping) or result.get("ok") is not True:
            return False, "录单扩展校验暂不可用，本次保存已停止。"
        try:
            payload = _normalize_active_validator_result(result.get("data"))
        except ValueError:
            return False, "录单扩展返回无效校验结果，本次保存已停止。"

        validation = payload["validation"]
        if validation["valid"]:
            return True, ""
        messages = [
            str(issue["message"])
            for issue in validation["issues"]
            if issue["severity"] == "error"
        ]
        return False, f"录单扩展校验未通过：{'；'.join(messages)}"

    def _handle_waybill_entry_extension_invoke(
        self,
        handler: Any,
        *,
        slot: str,
        handle: str,
    ) -> None:
        trusted_context = self._control_plane_write_context(handler)
        if trusted_context is None:
            return
        values = self._read_control_plane_json(handler)
        if values is None:
            return
        try:
            safe_slot = normalize_waybill_entry_slot(slot)
            safe_handle = normalize_waybill_entry_extension_handle(handle)
            if set(values) != {"request_id", "waybill"}:
                raise ValueError("waybill-entry invocation body is invalid")
            raw_request_id = values.get("request_id")
            raw_header_request_id = handler.headers.get("X-Browser-Request-UUID")
            request_id = self._normalize_browser_request_uuid(raw_request_id)
            header_request_id = self._normalize_browser_request_uuid(
                raw_header_request_id
            )
            if (
                type(raw_request_id) is not str
                or type(raw_header_request_id) is not str
                or not request_id
                or raw_request_id != request_id
                or raw_header_request_id != header_request_id
                or request_id != header_request_id
                or UUID(request_id).version != 4
            ):
                raise ValueError("waybill-entry invocation request_id is invalid")
            waybill = normalize_waybill_entry_draft(values.get("waybill"))
        except ValueError:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "INVALID_WAYBILL_ENTRY_EXTENSION_REQUEST",
                "录单扩展请求无效，请刷新页面后重试。",
            )
            return

        endpoint = (
            f"{WAYBILL_ENTRY_MODULE_SLOTS_ENDPOINT}/"
            f"{quote(safe_slot, safe='')}/{quote(safe_handle, safe='')}/invoke"
        )
        result = self._agent_request(
            "POST",
            endpoint,
            payload={"request_id": request_id, "waybill": waybill},
            timeout=max(35, self.settings.agent_timeout_seconds),
            console_principal=trusted_context["_console_principal"],
        )
        if not result.get("ok"):
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_GATEWAY,
                "WAYBILL_ENTRY_EXTENSION_UNAVAILABLE",
                "录单扩展暂不可用；本次扩展操作未执行。",
            )
            return
        try:
            payload = _normalize_invocation_result(safe_slot, result.get("data"))
        except ValueError:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_GATEWAY,
                "INVALID_WAYBILL_ENTRY_EXTENSION_RESULT",
                "录单扩展返回了无效结果；本次扩展操作未执行。",
            )
            return
        status = (
            HTTPStatus.ACCEPTED
            if safe_slot == WAYBILL_ENTRY_ACTIONS_SLOT
            else HTTPStatus.OK
        )
        self._control_plane_success(handler, status, payload)


__all__ = [
    "WAYBILL_ENTRY_ACTIVE_VALIDATORS_ENDPOINT",
    "WAYBILL_ENTRY_MODULE_SLOTS_ENDPOINT",
    "WaybillEntryExtensionsServiceMixin",
]
