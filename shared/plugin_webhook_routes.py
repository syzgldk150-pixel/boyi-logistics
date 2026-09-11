"""Existing public callback paths mapped to their exact plugin entrypoints."""
from types import MappingProxyType


PLUGIN_WEBHOOK_ROUTES = MappingProxyType({
    "delivery_status": ("sync_delivery_status_v2", "phase7.delivery_status_webhook", "webhook/sign-status", "sign-status"),
    "scan_codes": ("sync_scan_codes_v2", "phase7.scan_webhook", "webhook/phase7/scan", "phase7-scan"),
    "arrival_stats": ("sync_arrival_stats_v2", "phase7.stats_webhook", "webhook/phase7/stats", "phase7-stats"),
})
