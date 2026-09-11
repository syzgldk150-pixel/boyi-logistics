"""Host-owned business storage adapters for verified plugin calls."""
from __future__ import annotations

from agent.customer_collection_business import prepare_customer_rechecks, publish_verified_customer_collection


class PluginBusinessResults:
    def __init__(self, repository):
        self.repository = repository

    def prepare(self, invocation, capability, arguments):
        metadata = capability["_plugin_runtime"]
        prepared = dict(arguments)
        if metadata["plugin_id"] in {"sync_customer_service_problems", "sync_customer_service_problems_v2"}:
            with self.repository.unit_of_work() as uow:
                prepared["recheck_items"] = prepare_customer_rechecks(
                    uow.connection, automation_id=invocation.automation_id, metadata=metadata)
        return prepared

    def publish(self, invocation_id, automation_id, capability, arguments, outcome):
        metadata = capability["_plugin_runtime"]
        if metadata["plugin_id"] in {"sync_customer_service_problems", "sync_customer_service_problems_v2"}:
            with self.repository.unit_of_work() as uow:
                publish_verified_customer_collection(uow.connection,
                    invocation_id=invocation_id, automation_id=automation_id, metadata=metadata,
                    outcome=outcome, recheck_items=arguments["recheck_items"])
                uow.commit()
