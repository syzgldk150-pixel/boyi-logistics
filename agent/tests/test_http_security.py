import os

from agent.http_security import (
    INTERNAL_API_TOKEN_HEADER,
    authenticate_internal_request,
    is_public_path,
)
from tools.internal_http import InternalApiTokenNotConfigured, internal_api_headers


def test_public_routes_do_not_require_internal_token() -> None:
    assert is_public_path("/health")
    assert is_public_path("/feishu/webhook/event")
    assert is_public_path("/webhook/sign-status")
    assert authenticate_internal_request(
        path="/health",
        expected_token="",
        provided_token="",
    ) is None


def test_protected_route_fails_when_server_token_is_missing() -> None:
    failure = authenticate_internal_request(
        path="/internal/v1/admin/accounts",
        expected_token="",
        provided_token="",
    )

    assert failure is not None
    assert failure.status_code == 503


def test_protected_route_rejects_missing_or_wrong_token() -> None:
    for provided in ("", "wrong"):
        failure = authenticate_internal_request(
            path="/internal/v1/tms/get_price",
            expected_token="correct",
            provided_token=provided,
        )
        assert failure is not None
        assert failure.status_code == 401


def test_protected_route_accepts_matching_token() -> None:
    assert INTERNAL_API_TOKEN_HEADER == "X-Agent-Internal-Token"
    assert authenticate_internal_request(
        path="/internal/v1/tools/run",
        expected_token="correct",
        provided_token="correct",
    ) is None


def test_internal_client_headers_fail_closed_and_send_configured_token() -> None:
    original = os.environ.pop("AGENT_INTERNAL_API_TOKEN", None)
    try:
        try:
            internal_api_headers()
        except InternalApiTokenNotConfigured:
            pass
        else:
            raise AssertionError("missing internal token must fail closed")

        os.environ["AGENT_INTERNAL_API_TOKEN"] = "configured-test-token"
        assert internal_api_headers() == {
            INTERNAL_API_TOKEN_HEADER: "configured-test-token",
        }
    finally:
        if original is None:
            os.environ.pop("AGENT_INTERNAL_API_TOKEN", None)
        else:
            os.environ["AGENT_INTERNAL_API_TOKEN"] = original
