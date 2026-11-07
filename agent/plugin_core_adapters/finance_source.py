"""Production ports for finance source connectors: the shared finance repository."""

from __future__ import annotations

from agent.automation_plugins.finance_source_handlers import FinanceSourcePorts, build_finance_source_handler_map
from plugin_core_adapters.waybill_query import waybill_connection
from shared.finance import FinanceRepository


def build_production_finance_source_ports(*, connection_factory=None) -> FinanceSourcePorts:
    repository = FinanceRepository(connection_factory or waybill_connection)
    return FinanceSourcePorts(
        list_sources=repository.producer_sources,
        read_file=repository.read_source_file,
        submit_batch=lambda automation_id, plugin_version, batch, observed: repository.submit_source_batch(
            automation_id=automation_id, plugin_version=plugin_version, payload=batch, observed_read_refs=observed),
    )


def build_production_finance_source_handler_map(*, cursor_secret: bytes):
    return build_finance_source_handler_map(build_production_finance_source_ports(), cursor_secret=cursor_secret)


__all__ = ["build_production_finance_source_handler_map", "build_production_finance_source_ports"]
