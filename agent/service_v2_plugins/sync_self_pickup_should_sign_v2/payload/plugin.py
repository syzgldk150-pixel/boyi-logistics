"""Only the bound R13 account, statistics reads and target Sheet are exposed."""

from connector_adapter import invoke_primitive

PLUGIN_ID = "sync_self_pickup_should_sign_v2"

PRIMITIVES = {
    ("daily_sign.port", f"{role}.{operation}", role): (service, operation, effect)
    for role, service, operations in (
        (
            "daily_sign_r13",
            "connector.boyi.daily_sign_r13@1",
            (("describe", "read"), ("read_r13", "read")),
        ),
        (
            "daily_sign_store",
            "connector.boyi.daily_sign_store@1",
            (("load_state", "read"),),
        ),
        (
            "daily_sign_sheet",
            "connector.boyi.daily_sign_sheet@1",
            (
                ("describe", "read"),
                ("read_sheet", "read"),
                ("write_sheet", "external_write"),
                ("clear_sheet", "external_write"),
                ("read_sheet_formats", "read"),
                ("write_sheet_format", "external_write"),
            ),
        ),
    )
    for operation, effect in operations
}


def service_invoke_adapter(broker, operation, **kwargs):
    return invoke_primitive(broker, PRIMITIVES, operation, **kwargs)


def prepare_arguments(arguments):
    if arguments != {}:
        raise ValueError("INVALID_ARGUMENT")
    return {}
