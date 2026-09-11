"""Explicit HTTP permission projection; unknown routes remain super-admin only."""
from __future__ import annotations

from shared.identity_permissions import SUPER_ADMIN


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def console_permission(method: str, path: str) -> str:
    path = path.rstrip("/") or "/"
    read = method.upper() in {"GET", "HEAD"}
    if path in {"/logout", "/settings/profile/avatar", "/settings/profile/mobile-navigation"} or path.startswith("/runtime/avatars/"):
        return ""
    if _under(path, "/harness"):
        return "ai.chat"
    if _under(path, "/automation-accounts"):
        return "accounts.manage"
    if _under(path, "/finance") or _under(path, "/modules/finance") or path == "/module-data-sources/finance":
        return "finance.read" if read else "finance.manage"
    if _under(path, "/customer-service") or _under(path, "/modules/customer-service") or path == "/module-data-sources/customer_service":
        return "customer_service"
    if _under(path, "/tracking") or _under(path, "/receipts"):
        return "customer_service" if path.endswith("/audit") else "business.query"
    if _under(path, "/waybills"):
        return "business.query" if read else "waybill.write"
    if any(_under(path, p) for p in ("/ocr", "/workspaces/ocr", "/upload", "/documents", "/templates", "/original-pages", "/waybill-entry")):
        return "waybill.write"
    if _under(path, "/dispatch"):
        return "dispatch"
    if _under(path, "/line-haul-contacts"):
        return "line_haul"
    if path in {"/automations", "/workspaces/automations", "/extensions"}:
        return "plugins.execute" if read else SUPER_ADMIN
    if path in {"/automations/tasks/run-now", "/automations/tasks/confirm-scan-preview", "/automations/tasks/selection-preview", "/automations/tasks/confirm-selection-preview", "/automations/tasks/cancel", "/automations/tasks/output"}:
        return "plugins.execute"
    if read and path.startswith("/automations/projects/") and path.endswith("/invocations"):
        return "plugins.execute"
    return SUPER_ADMIN


def business_permission(operation: str) -> str:
    if operation.startswith("customer-service-") or operation == "receipts-audit":
        return "customer_service"
    if operation in {"get_price", "yunda_price"}:
        return "waybill.write"
    if operation in {"receipts-query", "receipt-feishu-detail", "send-waybills-query", "delivery_status",
                     "query_waybill_detail", "ronghui_tms_tracking", "tracking_query", "waybill_tracking", "yunda_waybill_tracking"}:
        return "business.query"
    return SUPER_ADMIN


def console_permissions(method: str, path: str) -> tuple[str, ...]:
    """Shared invocation URLs also serve collectors; Agent checks the exact instance."""
    permission = console_permission(method, path)
    if permission == "plugins.execute" and path.startswith(("/automations/tasks/", "/automations/projects/")):
        return ("plugins.execute", "finance.manage", "customer_service")
    return (permission,)


def agent_permission(method: str, path: str) -> str:
    read = method.upper() in {"GET", "HEAD"}
    if path.startswith("/internal/v1/harness/") or path == "/internal/v1/chat":
        return "ai.chat"
    if path.startswith("/internal/v1/business/"):
        return business_permission(path.rsplit("/", 1)[-1])
    if _under(path, "/internal/v1/admin/accounts"):
        return "accounts.manage"
    if _under(path, "/internal/v1/admin/finance"):
        return "finance.read" if read else "finance.manage"
    if path.startswith("/internal/v1/automation-projects/module-slots/waybill-entry"):
        return "waybill.write"
    if path in {"/internal/v1/automation/plugins/catalog", "/internal/v1/automation-project-policies", "/internal/v1/scheduled-tasks"}:
        return "plugins.execute"
    # Human invocation routes resolve their exact instance in identity_access.
    # Do not grant lifecycle/configuration routes by a shared URL prefix.
    if path.startswith("/internal/v1/tms/"):
        return "waybill.write"
    return SUPER_ADMIN
