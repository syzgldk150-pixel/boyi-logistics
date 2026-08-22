from tools.governed_tms_adapter import (
    build_receipts_sync_params,
    run_cli,
    validate_receipts_sync_response,
)


if __name__ == "__main__":
    run_cli(
        "receipts_sync",
        build_receipts_sync_params,
        response_validator=validate_receipts_sync_response,
        write=True,
    )
