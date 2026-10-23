"""The code-owned production Connector catalog; no offline fixture backends."""
from agent.automation_plugins.arrival_connectors_v2 import build_arrival_connectors
from agent.automation_plugins.problem_connectors_v2 import build_problem_connectors
from agent.automation_plugins.scan_connectors_v2 import build_scan_connectors
from agent.automation_plugins.list_connectors_v2 import build_list_connectors
from agent.automation_plugins.yunda_connectors_v2 import build_yunda_connectors
from agent.automation_plugins.finance_connectors_v2 import (
    build_finance_connectors,
    finance_platform_capture_broker_action_keys,
)
from agent.automation_plugins.daily_send_connectors_v2 import build_daily_send_connectors
from agent.automation_plugins.delivery_connectors_v2 import build_delivery_connectors
from agent.automation_plugins.customer_connectors_v2 import build_customer_connectors
from agent.automation_plugins.daily_sign_connectors_v2 import build_daily_sign_connectors, daily_sign_broker_action_keys
from agent.automation_plugins.route_status_connectors_v2 import (
    build_route_status_connectors,
    route_status_broker_action_keys,
)
from agent.automation_plugins.connector_registry import ConnectorRegistry


def production_connector_broker_action_keys() -> frozenset[tuple[str, str]]:
    """Reviewed primitives only Connectors use; main.py adds them to the release set."""
    return daily_sign_broker_action_keys() | route_status_broker_action_keys() | finance_platform_capture_broker_action_keys()


def build_production_connector_registry(reviewed) -> ConnectorRegistry:
    return ConnectorRegistry((*build_arrival_connectors(reviewed), *build_problem_connectors(reviewed),
                              *build_scan_connectors(reviewed), *build_list_connectors(reviewed), *build_yunda_connectors(reviewed),
                              *build_finance_connectors(reviewed), *build_daily_send_connectors(reviewed), *build_delivery_connectors(reviewed),
                              *build_customer_connectors(reviewed), *build_daily_sign_connectors(reviewed),
                              *build_route_status_connectors(reviewed)))
