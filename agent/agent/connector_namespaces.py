"""Internal tool names of the Host primitives behind the reviewed Connectors.

A Connector binds its primitive under one of these names; the primitive's
context checks compare against the same constant. They are Host-internal
namespaces, not plugin IDs, and never appear in a plugin package.
"""

from __future__ import annotations


ARRIVAL_STATS_TOOL = "arrival_stats"
ARRIVE_LIST_TOOL = "arrive_list"
CLOCK_IN_TOOL = "clock_in"
CUSTOMER_PROBLEM_TOOL = "customer_problem"
DAILY_SEND_TOOL = "daily_send"
DAILY_SIGN_TOOL = "daily_sign"
DELIVERY_STATUS_TOOL = "delivery_status"
FINANCE_STATEMENT_TOOL = "finance_statement"
ROUTE_STATUS_TOOL = "route_status"
SCAN_TOOL = "scan"
SELF_PICKUP_PROBLEM_TOOL = "self_pickup_problem"
SITE_SEND_TOOL = "site_send"
SPLIT_PENDING_PROBLEM_TOOL = "split_pending_problem"
YUNDA_DISPATCH_TOOL = "yunda_dispatch"
YUNDA_SEND_TOOL = "yunda_send"
