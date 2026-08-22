from tools.governed_tms_adapter import (
    build_receipts_audit_params,
    run_cli,
    validate_receipts_audit_response,
)


if __name__ == "__main__":
    run_cli(
        "receipts_audit",
        build_receipts_audit_params,
        response_validator=validate_receipts_audit_response,
        write=True,
    )
