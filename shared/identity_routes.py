"""Explicit HTTP permission projection; unknown routes remain super-admin only.

Console routes return ``module:view`` or ``module:operate``: reading a page
needs ``view``, changing data needs ``operate``. ``_READ_POSTS`` lists POST
endpoints that only read (queries, previews), so a view-only person can use
them. Agent routes called by a signed Console request use the same tokens.
"""
from __future__ import annotations

from shared.identity_permissions import SUPER_ADMIN


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


# POST endpoints that only read: queries, previews and status reads.
_READ_POSTS = frozenset({
    "/customer-service/problems/query", "/customer-service/problems/live-query",
    "/customer-service/problems/ai-suggest", "/customer-service/problems/ai-status",
    "/customer-service/problems/ai-stats", "/customer-service/pending-summary",
    "/customer-service/problems/attachments/preview",
    "/receipts/sync", "/receipts/download-images",
    "/receipts/shares/preview", "/receipts/shares/naming-example",
    "/receipts/shares/senders", "/receipts/shares/customers",
    "/tracking/query", "/waybills/source-refresh", "/waybills/photos", "/waybills/quote-options",
    "/automations/tasks/output", "/warehouse-monitor/lookup",
})
# Business operations that only read.
_READ_OPERATIONS = frozenset({"customer-service-query", "customer-service-detail", "customer-service-fetch-attachment"})


def _level(module: str, read: bool) -> str:
    return f"{module}:{'view' if read else 'operate'}"


def console_permission(method: str, path: str) -> str:
    path = path.rstrip("/") or "/"
    read = method.upper() in {"GET", "HEAD"} or path in _READ_POSTS
    if path in {"/logout", "/settings/profile/avatar", "/settings/profile/mobile-navigation"} or path.startswith("/runtime/avatars/"):
        return ""
    if path == "/settings/profile/automation-quick-plugins":
        return "plugins.execute:view"  # this admin's own pins on the automation page
    if _under(path, "/harness"):
        # Opening the assistant is a Console module; what it may answer or run
        # is checked against the agent permission set inside the Agent.
        return "ai_assistant:view"
    if _under(path, "/automation-accounts"):
        return _level("accounts.manage", read)
    if not read and path.startswith("/finance/periods/") and path.endswith(("/close", "/reopen")):
        return SUPER_ADMIN
    # Ledger structure, the start month and cancelling posted vouchers.
    if not read and (_under(path, "/finance/settings") or _under(path, "/finance/accounts") or _under(path, "/finance/doc-rules")
                     or path == "/finance/sources" or (path.startswith("/finance/sources/") and not path.endswith("/files"))
                     or (path.startswith("/finance/vouchers/") and path.endswith("/cancel"))):
        return SUPER_ADMIN
    if _under(path, "/finance") or _under(path, "/modules/finance") or path == "/module-data-sources/finance":
        return _level("finance", read)
    if _under(path, "/customer-service") or _under(path, "/modules/customer-service") or path == "/module-data-sources/customer_service":
        return _level("customer_service", read)
    if path == "/receipts/shares/access-code":
        return "receipt.share:operate"  # reveals the code sent to customers
    if _under(path, "/receipts/shares"):
        return _level("receipt.share", read)  # customer share links expose customer data outside the company
    if _under(path, "/tracking") or _under(path, "/receipts"):
        return "customer_service:operate" if path.endswith("/audit") else "business.query:view"
    if path == "/waybills/quote-options":
        return "waybill.write:view"  # price comparison on the entry page
    if _under(path, "/waybills"):
        return "business.query:view" if read else "waybill.write:operate"
    if any(_under(path, p) for p in ("/ocr", "/workspaces/ocr", "/upload", "/documents", "/templates", "/original-pages", "/waybill-entry")):
        return _level("waybill.write", read)
    if _under(path, "/dispatch"):
        return "dispatch:view"  # map-only planning, nothing is written
    if _under(path, "/line-haul-contacts"):
        return _level("line_haul", read)
    if _under(path, "/warehouse-monitor"):
        return "warehouse.monitor:view"
    if path in {"/automations", "/workspaces/automations", "/extensions"}:
        return "plugins.execute:view" if read else SUPER_ADMIN
    if path == "/automations/tasks/output":
        return "plugins.execute:view"
    if path in {"/automations/tasks/run-now", "/automations/tasks/confirm-scan-preview", "/automations/tasks/selection-preview", "/automations/tasks/confirm-selection-preview", "/automations/tasks/cancel"}:
        return "plugins.execute:operate"
    if read and path.startswith("/automations/projects/") and path.endswith("/invocations"):
        return "plugins.execute:view"
    return SUPER_ADMIN


def business_permission(operation: str) -> str:
    if operation.startswith("customer-service-") or operation == "receipts-audit":
        return "customer_service:view" if operation in _READ_OPERATIONS else "customer_service:operate"
    if operation in {"receipts-query", "receipt-attachment-image", "waybill-photos", "send-waybills-query", "delivery_status",
                     "query_waybill_detail", "ronghui_tms_tracking", "tracking_query", "waybill_tracking", "yunda_waybill_tracking"}:
        return "business.query:view"
    if operation == "warehouse-scan-times":
        return "warehouse.monitor:view"
    return SUPER_ADMIN


_COLLECTOR_ALTERNATIVES = {"view": ("plugins.execute:view", "finance:view", "customer_service:view"),
                           "operate": ("plugins.execute:operate", "finance:operate", "customer_service:operate")}


def console_permissions(method: str, path: str) -> tuple[str, ...]:
    """Shared invocation URLs also serve collectors; Agent checks the exact instance."""
    permission = console_permission(method, path)
    module, _, level = permission.partition(":")
    if module == "plugins.execute" and path.startswith(("/automations/tasks/", "/automations/projects/")):
        return _COLLECTOR_ALTERNATIVES[level]
    return (permission,)


def agent_permission(method: str, path: str) -> str:
    """Permission of a signed Console request to the Agent (Console set)."""
    read = method.upper() in {"GET", "HEAD"}
    if path.startswith("/internal/v1/harness/") or path == "/internal/v1/chat":
        return "ai_assistant:view"
    if path.startswith("/internal/v1/business/"):
        return business_permission(path.rsplit("/", 1)[-1])
    if _under(path, "/internal/v1/admin/accounts"):
        return _level("accounts.manage", read)
    if _under(path, "/internal/v1/admin/customer-service"):
        reads = {"/internal/v1/admin/customer-service/ai/suggest", "/internal/v1/admin/customer-service/ai/status",
                 "/internal/v1/admin/customer-service/ai/stats"}
        return _level("customer_service", read or path in reads)
    if _under(path, "/internal/v1/admin/receipt-shares"):
        return "receipt.share:operate"
    if _under(path, "/internal/v1/admin/finance"):
        return _level("finance", read)
    if path.startswith("/internal/v1/automation-projects/module-slots/waybill-entry"):
        return "waybill.write:view" if read else "waybill.write:operate"
    if path in {"/internal/v1/automation/plugins/catalog", "/internal/v1/automation-project-policies", "/internal/v1/scheduled-tasks"}:
        return "plugins.execute:view"
    # Human invocation routes resolve their exact instance in identity_access.
    # Do not grant lifecycle/configuration routes by a shared URL prefix.
    if path.startswith("/internal/v1/tms/"):
        return "waybill.write:operate"
    return SUPER_ADMIN
