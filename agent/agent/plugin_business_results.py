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
                # Serialize business publication against startup settlement;
                # a late old callback cannot publish after its call ended.
                with uow.connection.cursor() as cursor:
                    cursor.execute('SELECT status FROM automation_plugin_invocations WHERE invocation_id=%s AND automation_id=%s FOR UPDATE',
                                   (invocation_id, automation_id))
                    current = cursor.fetchone()
                if current is None or current['status'] not in {'RUNNING', 'CANCELLING'}:
                    from shared.data_sources import DataSourceError
                    raise DataSourceError('CUSTOMER_INVOCATION_NO_LONGER_ACTIVE')
                publish_verified_customer_collection(uow.connection,
                    invocation_id=invocation_id, automation_id=automation_id, metadata=metadata,
                    outcome=outcome, recheck_items=arguments["recheck_items"])
                uow.commit()
