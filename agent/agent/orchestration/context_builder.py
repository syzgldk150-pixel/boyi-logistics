"""Build deterministic context from authoritative, injected providers."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from agent.orchestration.models import Command, ContextSnapshot, OrchestrationError


AccountResolver = Callable[[Command], Sequence[Mapping[str, Any]]]
ResourceResolver = Callable[[Command], Mapping[str, Any]]
EntityResolver = Callable[[Command], Sequence[Mapping[str, Any]]]
IntegrityResolver = Callable[[Command], Mapping[str, Any]]


def _empty_accounts(_command: Command) -> Sequence[Mapping[str, Any]]:
    return ()


def _empty_mapping(_command: Command) -> Mapping[str, Any]:
    return {}


def _empty_entities(_command: Command) -> Sequence[Mapping[str, Any]]:
    return ()


class ContextBuilder:
    """Resolve only real accounts/resources/entities supplied by the composition root.

    An empty resolver result remains empty. It is never replaced with a default
    account or historical value. Ambiguity is retained for ``PlanValidator`` to
    decide against the selected tool's account-scope contract.
    """

    def __init__(
        self,
        *,
        account_resolver: AccountResolver = _empty_accounts,
        resource_resolver: ResourceResolver = _empty_mapping,
        entity_resolver: EntityResolver = _empty_entities,
        integrity_resolver: IntegrityResolver = _empty_mapping,
    ) -> None:
        self._account_resolver = account_resolver
        self._resource_resolver = resource_resolver
        self._entity_resolver = entity_resolver
        self._integrity_resolver = integrity_resolver

    def build(self, command: Command) -> ContextSnapshot:
        resources = self._resource_resolver(command) or {}
        if not isinstance(resources, Mapping):
            raise OrchestrationError("INVALID_RESOURCE_CONTEXT", "Resource resolver must return an object")
        planning_resources = dict(resources)
        # Clarification history is audit-only and must not perturb plan hashes.
        # The resolver provides one command-scoped effective override which is
        # validated separately before the planner can consume it.
        planning_resources.pop("clarifications", None)
        clarification_override = _clarification_override(
            planning_resources.pop("clarification_override", None),
            command=command,
        )

        accounts = list(self._account_resolver(command) or ())
        normalized_accounts: list[dict[str, Any]] = []
        account_ids: list[str] = []
        for account in accounts:
            if not isinstance(account, Mapping):
                raise OrchestrationError("INVALID_ACCOUNT_CONTEXT", "Account resolver returned a non-object value")
            account_id = str(account.get("account_id") or "").strip()
            if not account_id:
                raise OrchestrationError("INVALID_ACCOUNT_CONTEXT", "Resolved account is missing account_id")
            if account_id in account_ids:
                raise OrchestrationError("DUPLICATE_ACCOUNT_CONTEXT", f"Account was resolved more than once: {account_id}")
            account_ids.append(account_id)
            normalized_accounts.append(dict(account))

        entities = self._entity_resolver(command) or ()
        normalized_entities = []
        for entity in entities:
            if not isinstance(entity, Mapping):
                raise OrchestrationError("INVALID_ENTITY_CONTEXT", "Entity resolver returned a non-object value")
            normalized_entities.append(dict(entity))
        integrity = self._integrity_resolver(command) or {}
        if not isinstance(integrity, Mapping):
            raise OrchestrationError("INVALID_SOURCE_INTEGRITY", "Integrity resolver must return an object")

        return ContextSnapshot(
            values={
                "source": command.source,
                "actor": command.actor.to_dict(),
                "accounts": normalized_accounts,
                "resources": planning_resources,
                **(
                    {"clarification_override": clarification_override}
                    if clarification_override
                    else {}
                ),
                "entities": normalized_entities,
                "command_entities": [ref.to_dict() for ref in command.entity_refs],
            },
            account_ids=tuple(account_ids),
            source_integrity=dict(integrity),
        )


def _clarification_override(value: Any, *, command: Command) -> dict[str, Any]:
    """Validate the authoritative command-scoped v1 business override."""

    if value in (None, {}):
        return {}
    if not isinstance(value, Mapping):
        raise OrchestrationError(
            "INVALID_CLARIFICATION_CONTEXT",
            "Clarification override must be an object",
        )
    allowed = {"schema_version", "command_id", "account_id", "argument_updates"}
    unknown = sorted(str(key) for key in value if str(key) not in allowed)
    if unknown:
        raise OrchestrationError(
            "INVALID_CLARIFICATION_CONTEXT",
            f"Clarification override has unsupported fields: {', '.join(unknown)}",
        )
    if value.get("schema_version") != 1:
        raise OrchestrationError(
            "INVALID_CLARIFICATION_CONTEXT",
            "Clarification override schema_version must be 1",
        )
    if str(value.get("command_id") or "") != command.command_id:
        raise OrchestrationError(
            "CLARIFICATION_COMMAND_MISMATCH",
            "Clarification override does not belong to the current command",
        )

    normalized: dict[str, Any] = {"schema_version": 1}
    if "account_id" in value:
        account_id = str(value.get("account_id") or "").strip()
        if not account_id or len(account_id) > 191:
            raise OrchestrationError(
                "INVALID_CLARIFICATION_CONTEXT",
                "Clarification account_id is invalid",
            )
        normalized["account_id"] = account_id
    if "argument_updates" in value:
        updates = value.get("argument_updates")
        if not isinstance(updates, Mapping):
            raise OrchestrationError(
                "INVALID_CLARIFICATION_CONTEXT",
                "Clarification argument_updates must be an object",
            )
        normalized["argument_updates"] = dict(updates)
    if len(normalized) == 1:
        return {}
    return normalized
