"""Production ports for the automatic customer-service reply primitives.

The customer-service AI service is built after the plugin runtime starts, so
it is bound late through ``bind_customer_service_ai``; until then every
primitive fails closed with ``BROKER_ACTION_UNAVAILABLE``.
"""
from __future__ import annotations

from typing import Any, Mapping

from agent.automation_plugins.customer_auto_reply_handlers import (
    CustomerAutoReplyPorts,
    build_customer_auto_reply_handler_map,
)

_bound: dict[str, Any] = {"service": None}


def bind_customer_service_ai(service: Any) -> None:
    _bound["service"] = service


def build_production_customer_auto_reply_handler_map(*, cursor_secret: bytes, describe_account, customer_action):
    def describe(account_id: str) -> Mapping[str, Any]:
        return describe_account(account_id)

    return build_customer_auto_reply_handler_map(
        CustomerAutoReplyPorts(describe_account=describe, customer_action=customer_action,
                               service=lambda: _bound["service"]),
        cursor_secret=cursor_secret,
    )


__all__ = ["bind_customer_service_ai", "build_production_customer_auto_reply_handler_map"]
