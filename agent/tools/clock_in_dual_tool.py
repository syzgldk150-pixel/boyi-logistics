from tools.governed_tms_adapter import build_clock_in_params, run_cli, validate_clock_in_response


if __name__ == "__main__":
    run_cli(
        "clock_in_dual",
        build_clock_in_params,
        response_validator=validate_clock_in_response,
        write=True,
    )
