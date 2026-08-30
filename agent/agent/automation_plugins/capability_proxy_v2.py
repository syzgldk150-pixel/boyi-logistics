"""Fail-closed host capability handlers for Service v2 plugins.

Only managed storage is available in this module.  Network, browser, file,
event and service calls require a separately reviewed host backend; registering
their wildcard handlers here makes the absence explicit instead of falling
back to direct process access.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any, Awaitable, Protocol
from zoneinfo import ZoneInfo

from agent.automation_plugins.core_adapter import (
    CoreBrokerHandler,
    CoreBrokerInvocationContext,
)
from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.manifest_v2 import AutomationPluginManifestV2
from agent.automation_plugins.service_registry import (
    ResolvedServiceOperation,
    ServiceRegistry,
)
from agent.automation_plugins.service_v2_contract import SYSTEM_CAPABILITY_ROLE
from agent.automation_plugins.host_capability_registry import (
    CapabilityEffect,
    HOST_CAPABILITY_API_VERSION,
    default_host_capability_registry,
    effect_rank,
)
from shared.orchestration_repository_support import ConcurrentUpdateError


_KV_COLLECTION = "_kv"
_MAX_DOCUMENT_BYTES = 1024 * 1024
_MAX_COLLECTION_QUERY_LIMIT = 100
_SENSITIVE_FIELD_MARKERS = (
    "api_key",
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "session",
    "token",
)
_UNAVAILABLE_CAPABILITIES = (
    "browser.session",
    "event.publish",
    "file.read",
    "file.write",
    "http.request",
    "service.invoke",
)
UNAVAILABLE_SERVICE_V2_HANDLER_KEYS = frozenset((operation, "*") for operation in _UNAVAILABLE_CAPABILITIES)
SERVICE_V2_SERVICE_INVOKE_HANDLER_KEY = ("service.invoke", "*")
_MAX_SERVICE_CALL_DEPTH = 8
_REVIEWED_CLOCK_OPERATION = "browser.invoke"
_SERVICE_V2_CLOCK_OPERATION = "browser.session"
_CLOCK_TOOL_NAME = "clock_in_dual"
_CLOCK_ACTIONS = (
    "ronghui.clock.precheck",
    "ronghui.clock.submit",
    "ronghui.clock.verify",
)


class _ManagedDocumentRepository(Protocol):
    def get_project(self, automation_id: str, *, for_update: bool = False) -> Mapping[str, Any] | None: ...

    def get_version(
        self,
        plugin_id: str,
        version: str,
        *,
        for_update: bool = False,
    ) -> Mapping[str, Any] | None: ...

    def get_plugin_document(
        self,
        automation_id: str,
        collection: str,
        document_key: str,
        *,
        for_update: bool = False,
    ) -> Mapping[str, Any] | None: ...

    def put_plugin_document(
        self,
        automation_id: str,
        collection: str,
        document_key: str,
        document: Mapping[str, Any],
        *,
        expected_document_version: int,
        request_id: str,
        actor_id: str,
        actor_role: str,
        retained_until: Any | None = None,
        index_values_sha256: Mapping[str, str] | None = None,
        unique_values_sha256: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any]: ...

    def query_plugin_documents_by_index(
        self,
        automation_id: str,
        collection: str,
        index_name: str,
        value_sha256: str,
        *,
        limit: int,
    ) -> list[Mapping[str, Any]]: ...


class _PluginUnitOfWork(Protocol):
    automation_plugins: _ManagedDocumentRepository

    def __enter__(self) -> "_PluginUnitOfWork": ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> object: ...

    def commit(self) -> None: ...


class _OrchestrationRepository(Protocol):
    def unit_of_work(self) -> _PluginUnitOfWork: ...


class ServiceV2ProviderExecutor(Protocol):
    def __call__(
        self,
        *,
        provider: ResolvedServiceOperation,
        caller_automation_id: str,
        operation: str,
        arguments: Mapping[str, Any],
        call_chain: tuple[str, ...],
    ) -> Awaitable[Mapping[str, Any]]: ...


def _capability_error(message: str, *, code: str) -> PluginExecutionError:
    return PluginExecutionError(message, code=code)


def _require_static_host_governance(
    context: CoreBrokerInvocationContext,
) -> None:
    """Require the broker grant to reproduce the Registry descriptor exactly."""

    try:
        descriptor = default_host_capability_registry().resolve(
            api_version=HOST_CAPABILITY_API_VERSION,
            capability=context.operation,
            action=context.action,
        )
    except PluginExecutionError as exc:
        raise _capability_error(
            "Host capability is unavailable",
            code="CAPABILITY_UNAVAILABLE",
        ) from exc
    governance = descriptor.governance
    if (
        context.dynamic_effect
        or context.signed_effect != governance.effect.value
        or context.signed_broker_effect != governance.broker_effect
    ):
        raise _capability_error(
            "signed Host capability governance drifted",
            code="CAPABILITY_UNAVAILABLE",
        )


def _require_dynamic_service_invoke_governance(
    context: CoreBrokerInvocationContext,
) -> CapabilityEffect:
    """Validate the only protective service.invoke admission form."""

    if (
        context.dynamic_effect is not True
        or context.signed_effect != CapabilityEffect.EXTERNAL_WRITE.value
        or context.signed_broker_effect != "write"
    ):
        raise _capability_error(
            "service invocation governance is unavailable",
            code="CAPABILITY_UNAVAILABLE",
        )
    try:
        return CapabilityEffect(context.service_effect_ceiling)
    except ValueError as exc:
        raise _capability_error(
            "service invocation effect ceiling is unavailable",
            code="CAPABILITY_UNAVAILABLE",
        ) from exc


def _required_key(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 191:
        raise _capability_error(
            f"managed storage {field} is invalid",
            code="CAPABILITY_ARGUMENT_INVALID",
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise _capability_error(
            f"managed storage {field} is invalid",
            code="CAPABILITY_ARGUMENT_INVALID",
        )
    return value


def _expected_version(value: object) -> int:
    if type(value) is not int or value < 0:
        raise _capability_error(
            "managed storage expected_version must be a non-negative integer",
            code="CAPABILITY_ARGUMENT_INVALID",
        )
    return value


def _query_limit(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_COLLECTION_QUERY_LIMIT:
        raise _capability_error(
            f"managed collection query limit must be between 1 and {_MAX_COLLECTION_QUERY_LIMIT}",
            code="CAPABILITY_ARGUMENT_INVALID",
        )
    return value


def _reject_sensitive_fields(value: Any, *, path: str = "document") -> None:
    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            if not isinstance(raw_key, str):
                raise _capability_error(
                    f"managed storage {path} has a non-string field",
                    code="CAPABILITY_ARGUMENT_INVALID",
                )
            key = raw_key.lower().replace("-", "_")
            if any(marker in key for marker in _SENSITIVE_FIELD_MARKERS):
                raise _capability_error(
                    "managed storage rejects credential or session fields",
                    code="CAPABILITY_SENSITIVE_DATA_DENIED",
                )
            _reject_sensitive_fields(nested, path=f"{path}.{raw_key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _reject_sensitive_fields(nested, path=f"{path}[{index}]")


def _canonical_json(value: Any) -> bytes:
    _reject_sensitive_fields(value)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _capability_error(
            "managed storage accepts only JSON values",
            code="CAPABILITY_ARGUMENT_INVALID",
        ) from exc
    if len(encoded) > _MAX_DOCUMENT_BYTES:
        raise _capability_error(
            "managed storage document is too large",
            code="CAPABILITY_ARGUMENT_INVALID",
        )
    return encoded


def _request_id(
    context: CoreBrokerInvocationContext,
    *,
    collection: str,
    document_key: str,
    expected_version: int,
    document: Mapping[str, Any],
) -> str:
    identity = {
        "automation_id": context.automation_id,
        "plugin_version": context.plugin_version,
        "operation": context.operation,
        "action": context.action,
        "collection": collection,
        "document_key": document_key,
        "expected_version": expected_version,
        "document_sha256": hashlib.sha256(_canonical_json(document)).hexdigest(),
    }
    digest = hashlib.sha256(_canonical_json(identity)).hexdigest()
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"boyi:plugin-document:{digest}"))


def _manifest_for_context(
    repository: _ManagedDocumentRepository,
    context: CoreBrokerInvocationContext,
) -> AutomationPluginManifestV2:
    project = repository.get_project(context.automation_id)
    if not isinstance(project, Mapping):
        raise _capability_error(
            "managed storage project is unavailable",
            code="CAPABILITY_STORAGE_UNAVAILABLE",
        )
    plugin_id = str(project.get("plugin_id") or "")
    version = repository.get_version(plugin_id, context.plugin_version)
    if not isinstance(version, Mapping) or version.get("runtime_model") != "SERVICE_V2":
        raise _capability_error(
            "managed storage requires an installed Service v2 version",
            code="CAPABILITY_CONTRACT_INVALID",
        )
    raw_manifest = version.get("manifest_json")
    if not isinstance(raw_manifest, Mapping):
        raise _capability_error(
            "managed storage manifest is unavailable",
            code="CAPABILITY_CONTRACT_INVALID",
        )
    try:
        manifest = AutomationPluginManifestV2.from_mapping(raw_manifest)
    except Exception as exc:
        raise _capability_error(
            "managed storage manifest is invalid",
            code="CAPABILITY_CONTRACT_INVALID",
        ) from exc
    if manifest.plugin_id != plugin_id or manifest.version != context.plugin_version:
        raise _capability_error(
            "managed storage manifest identity does not match the runtime",
            code="CAPABILITY_CONTRACT_INVALID",
        )
    return manifest


def _required_arguments(arguments: Mapping[str, Any], fields: set[str]) -> None:
    if set(arguments) != fields:
        raise _capability_error(
            "managed storage arguments do not match the Host API contract",
            code="CAPABILITY_ARGUMENT_INVALID",
        )


def _document_row(row: Mapping[str, Any] | None) -> tuple[bool, Mapping[str, Any] | None, int]:
    if row is None:
        return False, None, 0
    state = str(row.get("retention_state") or "")
    document = row.get("document_json")
    version = row.get("document_version")
    if state not in {"ACTIVE", "RETAINED"}:
        return False, None, int(version or 0)
    if not isinstance(document, Mapping) or type(version) is not int or version <= 0:
        raise _capability_error(
            "managed storage contains an invalid document",
            code="CAPABILITY_STORAGE_CORRUPT",
        )
    _canonical_json(document)
    return True, document, version


def _field_value_is_valid(value: Any, field_type: str) -> bool:
    if field_type == "string":
        return isinstance(value, str)
    if field_type == "integer":
        return type(value) is int
    if field_type == "number":
        return type(value) is int or type(value) is float and math.isfinite(value)
    if field_type == "boolean":
        return type(value) is bool
    if field_type == "datetime":
        if not isinstance(value, str):
            return False
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return parsed.tzinfo is not None
    if field_type == "json":
        try:
            _canonical_json(value)
        except PluginExecutionError:
            return False
        return True
    return False


def _validate_collection_document(
    manifest: AutomationPluginManifestV2,
    *,
    collection: str,
    document: object,
) -> dict[str, Any]:
    declaration = _collection_declaration(manifest, collection)
    if not isinstance(document, Mapping):
        raise _capability_error(
            "managed collection document must be an object",
            code="CAPABILITY_ARGUMENT_INVALID",
        )
    normalized = dict(document)
    _canonical_json(normalized)
    fields = _collection_field_contracts(declaration)
    if not set(normalized) <= set(fields):
        raise _capability_error(
            "managed collection document contains undeclared fields",
            code="CAPABILITY_COLLECTION_SCHEMA_INVALID",
        )
    required = {name for name, item in fields.items() if item.get("required") is True}
    if not required <= set(normalized):
        raise _capability_error(
            "managed collection document is missing required fields",
            code="CAPABILITY_COLLECTION_SCHEMA_INVALID",
        )
    for name, value in normalized.items():
        field_type = str(fields[name].get("type") or "")
        if not _field_value_is_valid(value, field_type):
            raise _capability_error(
                "managed collection document has an invalid field type",
                code="CAPABILITY_COLLECTION_SCHEMA_INVALID",
            )
    return normalized


def _collection_declaration(
    manifest: AutomationPluginManifestV2,
    collection: str,
) -> Mapping[str, Any]:
    declarations = [
        item
        for item in manifest.storage.get("collections", ())
        if isinstance(item, Mapping) and item.get("name") == collection
    ]
    if len(declarations) != 1:
        raise _capability_error(
            "managed collection was not declared by this plugin",
            code="CAPABILITY_COLLECTION_DENIED",
        )
    return declarations[0]


def _collection_field_contracts(
    declaration: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    raw_fields = declaration.get("fields")
    if not isinstance(raw_fields, tuple):
        raise _capability_error(
            "managed collection declaration is invalid",
            code="CAPABILITY_CONTRACT_INVALID",
        )
    fields = {
        str(item["name"]): item
        for item in raw_fields
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
    }
    if len(fields) != len(raw_fields):
        raise _capability_error(
            "managed collection declaration is invalid",
            code="CAPABILITY_CONTRACT_INVALID",
        )
    return fields


def _declared_index(
    declaration: Mapping[str, Any],
    *,
    index_name: str,
) -> Mapping[str, Any]:
    matches = [
        item for item in declaration.get("indexes", ()) if isinstance(item, Mapping) and item.get("name") == index_name
    ]
    if len(matches) != 1:
        raise _capability_error(
            "managed collection query index was not declared by this plugin",
            code="CAPABILITY_COLLECTION_INDEX_DENIED",
        )
    return matches[0]


def _index_value_sha256(
    *,
    fields: tuple[str, ...],
    values: Mapping[str, Any],
) -> str:
    ordered_values = [{"field": field_name, "value": values[field_name]} for field_name in fields]
    return hashlib.sha256(_canonical_json(ordered_values)).hexdigest()


def _document_index_digests(
    declaration: Mapping[str, Any],
    document: Mapping[str, Any],
) -> tuple[dict[str, str], dict[str, str]]:
    result: list[dict[str, str]] = [{}, {}]
    for result_index, declaration_key in enumerate(("indexes", "unique_constraints")):
        raw_declarations = declaration.get(declaration_key)
        if not isinstance(raw_declarations, tuple):
            raise _capability_error(
                "managed collection index declaration is invalid",
                code="CAPABILITY_CONTRACT_INVALID",
            )
        for raw_index in raw_declarations:
            if not isinstance(raw_index, Mapping):
                raise _capability_error(
                    "managed collection index declaration is invalid",
                    code="CAPABILITY_CONTRACT_INVALID",
                )
            name = raw_index.get("name")
            raw_fields = raw_index.get("fields")
            if (
                not isinstance(name, str)
                or not isinstance(raw_fields, tuple)
                or not raw_fields
                or any(not isinstance(field, str) for field in raw_fields)
            ):
                raise _capability_error(
                    "managed collection index declaration is invalid",
                    code="CAPABILITY_CONTRACT_INVALID",
                )
            fields = tuple(raw_fields)
            # Optional indexed fields follow SQL NULL-like semantics: a
            # document without every field has no index/unique key at all.
            if not set(fields) <= set(document):
                continue
            result[result_index][name] = _index_value_sha256(
                fields=fields,
                values=document,
            )
    return result[0], result[1]


def _query_index_digest(
    declaration: Mapping[str, Any],
    *,
    index_name: str,
    values: object,
) -> str:
    index = _declared_index(declaration, index_name=index_name)
    raw_fields = index.get("fields")
    if not isinstance(raw_fields, tuple) or not raw_fields or any(not isinstance(field, str) for field in raw_fields):
        raise _capability_error(
            "managed collection index declaration is invalid",
            code="CAPABILITY_CONTRACT_INVALID",
        )
    if not isinstance(values, Mapping) or set(values) != set(raw_fields):
        raise _capability_error(
            "managed collection query values must exactly match the declared index fields",
            code="CAPABILITY_ARGUMENT_INVALID",
        )
    fields = _collection_field_contracts(declaration)
    normalized = dict(values)
    for field_name, value in normalized.items():
        contract = fields.get(field_name)
        if contract is None or not _field_value_is_valid(
            value,
            str(contract.get("type") or ""),
        ):
            raise _capability_error(
                "managed collection query contains an invalid index value",
                code="CAPABILITY_ARGUMENT_INVALID",
            )
    return _index_value_sha256(fields=tuple(raw_fields), values=normalized)


class ServiceV2CapabilityProxy:
    """Host-owned capability implementation with no direct network or file path."""

    def __init__(
        self,
        orchestration_repository: _OrchestrationRepository,
        *,
        service_registry: ServiceRegistry | None = None,
        service_executor: ServiceV2ProviderExecutor | None = None,
    ) -> None:
        if not callable(getattr(orchestration_repository, "unit_of_work", None)):
            raise ValueError("Service v2 capability proxy requires a Unit of Work repository")
        self._orchestration = orchestration_repository
        self._services = service_registry
        self._service_executor = service_executor

    @staticmethod
    def unavailable(
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        del arguments
        raise _capability_error(
            f"Host capability {context.operation} has no approved backend",
            code="CAPABILITY_UNAVAILABLE",
        )

    def kv(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_static_host_governance(context)
        if context.action == "get":
            _required_arguments(arguments, {"key"})
            key = _required_key(arguments["key"], field="key")
            with self._orchestration.unit_of_work() as uow:
                manifest = _manifest_for_context(uow.automation_plugins, context)
                if manifest.storage.get("kv") is not True:
                    raise _capability_error(
                        "managed KV was not declared by this plugin",
                        code="CAPABILITY_STORAGE_DENIED",
                    )
                row = uow.automation_plugins.get_plugin_document(
                    context.automation_id,
                    _KV_COLLECTION,
                    key,
                )
            found, document, version = _document_row(row)
            if not found:
                return {"found": False, "value": None, "version": version}
            if set(document or {}) != {"value"}:
                raise _capability_error(
                    "managed KV contains an invalid document",
                    code="CAPABILITY_STORAGE_CORRUPT",
                )
            return {"found": True, "value": document["value"], "version": version}

        if context.action == "put":
            _required_arguments(arguments, {"key", "value", "expected_version"})
            key = _required_key(arguments["key"], field="key")
            expected = _expected_version(arguments["expected_version"])
            document = {"value": arguments["value"]}
            _canonical_json(document)
            with self._orchestration.unit_of_work() as uow:
                manifest = _manifest_for_context(uow.automation_plugins, context)
                if manifest.storage.get("kv") is not True:
                    raise _capability_error(
                        "managed KV was not declared by this plugin",
                        code="CAPABILITY_STORAGE_DENIED",
                    )
                if context.mark_write_started is None:
                    raise _capability_error(
                        "managed KV write evidence is unavailable",
                        code="WRITE_ATTEMPT_RECEIPT_UNAVAILABLE",
                    )
                request_id = _request_id(
                    context,
                    collection=_KV_COLLECTION,
                    document_key=key,
                    expected_version=expected,
                    document=document,
                )
                context.mark_write_started()
                row = uow.automation_plugins.put_plugin_document(
                    context.automation_id,
                    _KV_COLLECTION,
                    key,
                    document,
                    expected_document_version=expected,
                    request_id=request_id,
                    actor_id=context.automation_id,
                    actor_role="plugin_service",
                )
                uow.commit()
            return {
                "stored": True,
                "version": int(row["document_version"]),
                "content_sha256": str(row["document_sha256"]),
            }

        raise _capability_error(
            "managed KV operation is unavailable",
            code="CAPABILITY_UNAVAILABLE",
        )

    def collection(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_static_host_governance(context)
        if context.action == "get":
            _required_arguments(arguments, {"collection", "document_key"})
            collection = _required_key(arguments["collection"], field="collection")
            document_key = _required_key(arguments["document_key"], field="document_key")
            with self._orchestration.unit_of_work() as uow:
                manifest = _manifest_for_context(uow.automation_plugins, context)
                _collection_declaration(manifest, collection)
                row = uow.automation_plugins.get_plugin_document(
                    context.automation_id,
                    collection,
                    document_key,
                )
            found, document, version = _document_row(row)
            if found:
                document = _validate_collection_document(
                    manifest,
                    collection=collection,
                    document=document,
                )
            return {
                "found": found,
                "document": dict(document) if document is not None else None,
                "version": version,
            }

        if context.action == "query":
            _required_arguments(
                arguments,
                {"collection", "index_name", "values", "limit"},
            )
            collection = _required_key(arguments["collection"], field="collection")
            index_name = _required_key(arguments["index_name"], field="index_name")
            limit = _query_limit(arguments["limit"])
            with self._orchestration.unit_of_work() as uow:
                manifest = _manifest_for_context(uow.automation_plugins, context)
                declaration = _collection_declaration(manifest, collection)
                value_sha256 = _query_index_digest(
                    declaration,
                    index_name=index_name,
                    values=arguments["values"],
                )
                rows = uow.automation_plugins.query_plugin_documents_by_index(
                    context.automation_id,
                    collection,
                    index_name,
                    value_sha256,
                    limit=limit,
                )
            documents: list[dict[str, Any]] = []
            seen_keys: set[str] = set()
            for row in rows:
                document_key = _required_key(
                    row.get("document_key"),
                    field="document_key",
                )
                if document_key in seen_keys:
                    raise _capability_error(
                        "managed collection index contains duplicate documents",
                        code="CAPABILITY_STORAGE_CORRUPT",
                    )
                found, document, version = _document_row(row)
                if not found or document is None:
                    raise _capability_error(
                        "managed collection index references an unavailable document",
                        code="CAPABILITY_STORAGE_CORRUPT",
                    )
                validated = _validate_collection_document(
                    manifest,
                    collection=collection,
                    document=document,
                )
                seen_keys.add(document_key)
                documents.append(
                    {
                        "document_key": document_key,
                        "document": validated,
                        "version": version,
                    }
                )
            return {
                "documents": documents,
                "count": len(documents),
                "limit": limit,
            }

        if context.action in {"put", "upsert"}:
            _required_arguments(
                arguments,
                {"collection", "document_key", "document", "expected_version"},
            )
            collection = _required_key(arguments["collection"], field="collection")
            document_key = _required_key(arguments["document_key"], field="document_key")
            expected = _expected_version(arguments["expected_version"])
            try:
                with self._orchestration.unit_of_work() as uow:
                    manifest = _manifest_for_context(uow.automation_plugins, context)
                    document = _validate_collection_document(
                        manifest,
                        collection=collection,
                        document=arguments["document"],
                    )
                    declaration = _collection_declaration(manifest, collection)
                    index_values, unique_values = _document_index_digests(
                        declaration,
                        document,
                    )
                    if context.mark_write_started is None:
                        raise _capability_error(
                            "managed collection write evidence is unavailable",
                            code="WRITE_ATTEMPT_RECEIPT_UNAVAILABLE",
                        )
                    request_id = _request_id(
                        context,
                        collection=collection,
                        document_key=document_key,
                        expected_version=expected,
                        document=document,
                    )
                    context.mark_write_started()
                    row = uow.automation_plugins.put_plugin_document(
                        context.automation_id,
                        collection,
                        document_key,
                        document,
                        expected_document_version=expected,
                        request_id=request_id,
                        actor_id=context.automation_id,
                        actor_role="plugin_service",
                        index_values_sha256=index_values,
                        unique_values_sha256=unique_values,
                    )
                    uow.commit()
            except ConcurrentUpdateError as exc:
                if str(exc).startswith("managed plugin document unique constraint conflict"):
                    raise _capability_error(
                        "managed collection unique constraint conflicts with another document",
                        code="CAPABILITY_COLLECTION_UNIQUE_CONFLICT",
                    ) from exc
                raise
            return {
                "stored": True,
                "version": int(row["document_version"]),
                "content_sha256": str(row["document_sha256"]),
            }

        raise _capability_error(
            "managed collection operation is unavailable",
            code="CAPABILITY_UNAVAILABLE",
        )

    async def service_invoke(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Invoke one required service through its unique active host Provider."""

        if context.operation != "service.invoke" or context.role != SYSTEM_CAPABILITY_ROLE:
            raise _capability_error(
                "service invocation context is invalid",
                code="SERVICE_INVOKE_CONTEXT_INVALID",
            )
        caller_effect = _require_dynamic_service_invoke_governance(context)
        _required_arguments(arguments, {"service", "operation", "arguments"})
        service = _required_key(arguments["service"], field="service")
        operation = _required_key(arguments["operation"], field="operation")
        if operation != context.action:
            raise _capability_error(
                "service invocation operation does not match the signed action",
                code="SERVICE_OPERATION_MISMATCH",
            )
        service_arguments = arguments["arguments"]
        if not isinstance(service_arguments, Mapping):
            raise _capability_error(
                "service invocation arguments must be an object",
                code="CAPABILITY_ARGUMENT_INVALID",
            )
        public_arguments = dict(service_arguments)
        _canonical_json(public_arguments)

        with self._orchestration.unit_of_work() as uow:
            manifest = _manifest_for_context(uow.automation_plugins, context)
        if service not in manifest.required_services:
            raise _capability_error(
                "service was not declared in this plugin's requires contract",
                code="SERVICE_DEPENDENCY_UNDECLARED",
            )
        if service in context.service_call_chain:
            raise _capability_error(
                "service invocation cycle was detected",
                code="SERVICE_CALL_CYCLE",
            )
        if len(context.service_call_chain) >= _MAX_SERVICE_CALL_DEPTH:
            raise _capability_error(
                "service invocation depth limit was reached",
                code="SERVICE_CALL_DEPTH_EXCEEDED",
            )
        registry = self._services
        executor = self._service_executor
        if registry is None or executor is None:
            raise _capability_error(
                "Host capability service.invoke has no approved backend",
                code="CAPABILITY_UNAVAILABLE",
            )
        provider = registry.require_operation(service, operation)
        call_chain = (*context.service_call_chain, service)
        try:
            effect = provider.effect
        except (TypeError, ValueError, AttributeError) as exc:
            raise _capability_error(
                "service Provider effect is unavailable",
                code="CAPABILITY_UNAVAILABLE",
            ) from exc
        if effect_rank(effect) > effect_rank(caller_effect):
            raise _capability_error(
                "service invocation would exceed its signed effect ceiling",
                code="SERVICE_EFFECT_ESCALATION_DENIED",
            )
        if (
            effect
            in {
                CapabilityEffect.INTERNAL_WRITE,
                CapabilityEffect.EXTERNAL_WRITE,
                CapabilityEffect.DESTRUCTIVE,
            }
            and context.mark_write_started is not None
        ):
            # The Provider's immutable operation descriptor, not the consumer
            # operation name nor a static service.invoke admission ceiling,
            # determines whether this consumer crosses a write boundary.
            context.mark_write_started()
        result = await executor(
            provider=provider,
            caller_automation_id=context.automation_id,
            operation=operation,
            arguments=public_arguments,
            call_chain=call_chain,
        )
        if not isinstance(result, Mapping):
            raise _capability_error(
                "service Provider returned a non-object result",
                code="SERVICE_PROVIDER_RESULT_INVALID",
            )
        public_result = dict(result)
        _canonical_json(public_result)
        return public_result


def _clock_observed_at(value: object) -> str:
    raw = str(value or "").strip()
    try:
        observed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _capability_error(
            "reviewed clock evidence timestamp is invalid",
            code="BROKER_SOURCE_INVALID",
        ) from exc
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return observed.isoformat()


def _service_v2_clock_handler(
    action: str,
    reviewed_handler: CoreBrokerHandler,
) -> CoreBrokerHandler:
    """Adapt one reviewed v1 clock primitive to the account-blind v2 contract."""

    def invoke(
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_static_host_governance(context)
        if context.operation != _SERVICE_V2_CLOCK_OPERATION or context.action != action:
            raise _capability_error(
                "service-v2 clock capability context is invalid",
                code="BROKER_CONTRACT_INVALID",
            )
        translated = replace(
            context,
            tool_name=_CLOCK_TOOL_NAME,
            operation=_REVIEWED_CLOCK_OPERATION,
            role="account_id",
        )
        raw = reviewed_handler(translated, dict(arguments))
        if not isinstance(raw, Mapping):
            raise _capability_error(
                "reviewed clock capability returned a non-object result",
                code="BROKER_SOURCE_INVALID",
            )
        result = dict(raw)
        if action == "ronghui.clock.precheck":
            result.update(
                {
                    "site": dict(arguments.get("site") or {}),
                    "clock_types": list(arguments.get("clock_types") or []),
                }
            )
        elif action == "ronghui.clock.verify":
            result.update(
                {
                    "operation_id": str(arguments.get("operation_id") or ""),
                    "site": dict(arguments.get("site") or {}),
                    "match_count": 1,
                    "outcome_category": "confirmed_exact_source_record",
                    "observed_at": _clock_observed_at(result.get("observed_at")),
                }
            )
        return result

    return invoke


def build_service_v2_capability_handler_map(
    orchestration_repository: _OrchestrationRepository,
    *,
    reviewed_handlers: Mapping[tuple[str, str], CoreBrokerHandler] | None = None,
    service_registry: ServiceRegistry | None = None,
    service_executor: ServiceV2ProviderExecutor | None = None,
) -> dict[tuple[str, str], CoreBrokerHandler]:
    """Return fail-closed platform handlers plus reviewed capability adapters."""

    if (service_registry is None) != (service_executor is None):
        raise ValueError("service registry and executor must be configured together")
    proxy = ServiceV2CapabilityProxy(
        orchestration_repository,
        service_registry=service_registry,
        service_executor=service_executor,
    )
    handlers: dict[tuple[str, str], CoreBrokerHandler] = {
        (operation, "*"): proxy.unavailable for operation in _UNAVAILABLE_CAPABILITIES
    }
    handlers[("storage.kv", "*")] = proxy.kv
    handlers[("storage.collection", "*")] = proxy.collection
    if service_registry is not None:
        handlers[SERVICE_V2_SERVICE_INVOKE_HANDLER_KEY] = proxy.service_invoke
    reviewed = reviewed_handlers or {}
    for action in _CLOCK_ACTIONS:
        reviewed_handler = reviewed.get((_REVIEWED_CLOCK_OPERATION, action))
        if reviewed_handler is not None:
            handlers[(_SERVICE_V2_CLOCK_OPERATION, action)] = _service_v2_clock_handler(action, reviewed_handler)
    return handlers


__all__ = [
    "ServiceV2CapabilityProxy",
    "ServiceV2ProviderExecutor",
    "SERVICE_V2_SERVICE_INVOKE_HANDLER_KEY",
    "UNAVAILABLE_SERVICE_V2_HANDLER_KEYS",
    "build_service_v2_capability_handler_map",
]
