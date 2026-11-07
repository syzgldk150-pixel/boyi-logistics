"""Closed Host primitives for finance source connectors (contract ``finance.source_batch/v1``).

The connector plugin owns fetching and parsing (online pages through the
generic account ``http.request``, or an uploaded bill file through
``read_file``). The Host only lists the sources this instance produces, hands
out bounded slices of their pending files, and accepts batches whose evidence
it can check: read-only HTTP observations of this same invocation, or the
cited pending file. Accounting stays in ``shared.finance``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from agent.automation_plugins.core_adapter import CoreBrokerHandler, CoreBrokerInvocationContext
from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.connector_namespaces import FINANCE_SOURCE_TOOL

TOOL = FINANCE_SOURCE_TOOL
ROLE = "host_internal"
LIST_ACTION = ("ledger.invoke", "finance.source.list")
READ_FILE_ACTION = ("ledger.invoke", "finance.source_file.read")
SUBMIT_ACTION = ("ledger.invoke", "finance.source_batch.submit")
MAX_READ_BYTES = 4 * 1024 * 1024
_READ_HTTP_ACTIONS = frozenset({"read_json", "read_text"})


@dataclass(frozen=True)
class FinanceSourcePorts:
    list_sources: Callable[[str], list[Mapping[str, Any]]]
    read_file: Callable[[str, int, int, int], Mapping[str, Any]]
    submit_batch: Callable[[str, str, Mapping[str, Any], frozenset[str]], Mapping[str, Any]]


def _error(message: str, code: str) -> PluginExecutionError:
    return PluginExecutionError(message, code=code)


def _require_context(context: CoreBrokerInvocationContext, action: tuple[str, str]) -> None:
    if (context.tool_name, context.operation, context.action, context.role) != (TOOL, *action, ROLE):
        raise _error("finance-source broker context is invalid", "BROKER_CONTEXT_INVALID")
    if not isinstance(context.automation_id, str) or not context.automation_id:
        raise _error("finance-source broker has no plugin instance", "BROKER_CONTEXT_INVALID")


def _finance_error(exc: Exception) -> PluginExecutionError:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return _error(getattr(exc, "message", "finance source operation was refused"), code)
    raise exc


class _FinanceSourceHandlers:
    def __init__(self, ports: FinanceSourcePorts, *, secret: bytes) -> None:
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError("finance-source broker secret must contain at least 32 bytes")
        self._ports = ports
        self._secret = bytes(secret)

    def _evidence(self, context: CoreBrokerInvocationContext, purpose: str, proof: Mapping[str, Any]) -> str:
        body = canonical_json_bytes({
            "context": {"automation_id": context.automation_id, "plugin_version": context.plugin_version,
                        "tool_name": context.tool_name},
            "purpose": purpose, "payload": dict(proof),
        })
        signature = hmac.new(self._secret, body, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(hashlib.sha256(body).digest() + signature).decode("ascii").rstrip("=")

    def list_sources(self, context: CoreBrokerInvocationContext, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        _require_context(context, LIST_ACTION)
        if not isinstance(arguments, Mapping) or set(arguments):
            raise _error("finance-source list takes no arguments", "BROKER_ARGUMENT_INVALID")
        sources = [dict(item) for item in self._ports.list_sources(context.automation_id)]
        proof = {"sources": sorted(item["source_key"] for item in sources)}
        return {"sources": sources, "evidence_ref": self._evidence(context, "finance-source-list", proof)}

    def read_file(self, context: CoreBrokerInvocationContext, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        _require_context(context, READ_FILE_ACTION)
        if not isinstance(arguments, Mapping) or set(arguments) != {"file_id", "offset", "length"}:
            raise _error("finance-source file read arguments are invalid", "BROKER_ARGUMENT_INVALID")
        file_id, offset, length = arguments["file_id"], arguments["offset"], arguments["length"]
        if (type(file_id) is not int or file_id < 1 or type(offset) is not int or offset < 0
                or type(length) is not int or not 1 <= length <= MAX_READ_BYTES):
            raise _error("finance-source file read range is invalid", "BROKER_ARGUMENT_INVALID")
        try:
            part = self._ports.read_file(context.automation_id, file_id, offset, length)
        except Exception as exc:  # FinanceError carries a stable code
            raise _finance_error(exc) from exc
        data = part["data"]
        proof = {"file_id": file_id, "offset": offset, "length": len(data), "sha256": part["sha256"],
                 "slice_sha256": hashlib.sha256(data).hexdigest()}
        return {"file_id": file_id, "offset": offset, "size_bytes": int(part["size_bytes"]), "sha256": part["sha256"],
                "data_base64": base64.b64encode(data).decode("ascii"), "eof": bool(part["eof"]),
                "evidence_ref": self._evidence(context, "finance-source-file-read", proof)}

    def submit_batch(self, context: CoreBrokerInvocationContext, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        _require_context(context, SUBMIT_ACTION)
        if not isinstance(arguments, Mapping) or set(arguments) != {"batch"} or not isinstance(arguments["batch"], Mapping):
            raise _error("finance-source submit takes exactly one batch", "BROKER_ARGUMENT_INVALID")
        if context.host_call_observations is None:
            raise _error("Host observations are unavailable", "BROKER_OBSERVATION_UNAVAILABLE")
        # Only completed, read-only account HTTP calls of this invocation count as online evidence.
        observed = frozenset(
            str(item.get("evidence_ref")) for item in context.host_call_observations()
            if item.get("evidence_ref") and item.get("operation") == "http.request"
            and item.get("action") in _READ_HTTP_ACTIONS and item.get("write_started") is False
        )
        result = dict(self._ports.submit_batch(context.automation_id, context.plugin_version, dict(arguments["batch"]), observed))
        proof = {"status": result.get("status"), "batch_id": result.get("batch_id"), "error_code": result.get("error_code"),
                 "content": hashlib.sha256(canonical_json_bytes(dict(arguments["batch"]))).hexdigest()}
        return {**result, "evidence_ref": self._evidence(context, "finance-source-submit", proof)}

    def handler_map(self) -> dict[tuple[str, str], CoreBrokerHandler]:
        return {LIST_ACTION: self.list_sources, READ_FILE_ACTION: self.read_file, SUBMIT_ACTION: self.submit_batch}


def build_finance_source_handler_map(ports: FinanceSourcePorts, *, cursor_secret: bytes) -> dict[tuple[str, str], CoreBrokerHandler]:
    return _FinanceSourceHandlers(ports, secret=cursor_secret).handler_map()


def finance_source_broker_action_keys() -> frozenset[tuple[str, str]]:
    return frozenset({LIST_ACTION, READ_FILE_ACTION, SUBMIT_ACTION})


__all__ = [
    "FinanceSourcePorts",
    "LIST_ACTION",
    "READ_FILE_ACTION",
    "ROLE",
    "SUBMIT_ACTION",
    "TOOL",
    "build_finance_source_handler_map",
    "finance_source_broker_action_keys",
]
