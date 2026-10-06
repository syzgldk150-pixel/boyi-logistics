"""Explicit platform routing for the migration-owned logistics tables.

The runtime database exposes SQL SECURITY INVOKER views onto waybill_db.
These are live aliases, never a second copy or a fallback data source.
"""

WAYBILL_TABLES = {
    "yunda": "yunda_waybills",
    "ronghui": "ronghui_waybills",
    "manual": "boyi_waybills",
    "ocr": "boyi_waybills",
}
RECEIPT_TABLES = {
    "yunda": "yunda_receipts",
    "ronghui": "ronghui_receipts",
    "boyi": "boyi_receipts",
}


def waybill_table(source: str) -> str:
    try:
        return WAYBILL_TABLES[source]
    except KeyError:
        raise ValueError("an explicit, supported waybill source is required") from None


def receipt_table(platform: str) -> str:
    try:
        return RECEIPT_TABLES[platform]
    except KeyError:
        raise ValueError("an explicit, supported receipt platform is required") from None
