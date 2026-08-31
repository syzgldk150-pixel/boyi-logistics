from __future__ import annotations

import pytest

from agent.automation_plugins.runtime_backend_availability import (
    RuntimeContributionBackendAvailability,
)


def test_live_backend_status_is_process_only_and_fail_closed_by_default() -> None:
    availability = RuntimeContributionBackendAvailability()
    structural = ("harness_tool_catalog", "READY", None, None)

    assert availability.effective_status(
        contribution_kind="harness",
        structural_status=structural,
    ) == (
        "harness_tool_catalog",
        "CAPABILITY_UNAVAILABLE",
        "CAPABILITY_UNAVAILABLE",
        "HARNESS_SANDBOX_UNAVAILABLE",
    )
    assert structural == ("harness_tool_catalog", "READY", None, None)

    availability.mark_available("harness")
    assert availability.effective_status(
        contribution_kind="harness",
        structural_status=structural,
    ) == structural
    availability.mark_unavailable("harness", reason_detail="HARNESS_CANARY_FAILED")
    assert availability.state("harness").reason_detail == "HARNESS_CANARY_FAILED"


def test_ingress_backends_share_one_atomic_process_readiness_authority() -> None:
    availability = RuntimeContributionBackendAvailability()
    availability.mark_available("webhook", "events")
    assert availability.is_available("webhook") is False
    assert availability.is_available("events") is False
    assert availability.state("webhook").reason_detail == "MANAGED_INGRESS_UNBOUND"

    ingress = object()
    availability.bind_managed_ingress(ingress)
    assert availability.is_available("webhook") is True
    assert availability.is_available("events") is True
    assert availability.is_available("console") is True

    availability.unbind_managed_ingress(ingress)
    assert availability.is_available("webhook") is False
    assert availability.is_available("events") is False

    availability.bind_managed_ingress(ingress)

    availability.mark_unavailable(
        "webhook",
        "events",
        reason_detail="AGENT_SHUTDOWN",
    )
    assert availability.is_available("webhook") is False
    assert availability.is_available("events") is False
    assert availability.effective_status(
        contribution_kind="events",
        structural_status=("managed_event_subscriptions", "CAPABILITY_UNAVAILABLE", "X", "Y"),
    ) == ("managed_event_subscriptions", "CAPABILITY_UNAVAILABLE", "X", "Y")


def test_runtime_backend_authority_rejects_unknown_or_empty_mutations() -> None:
    availability = RuntimeContributionBackendAvailability()
    with pytest.raises(ValueError):
        availability.mark_available()
    with pytest.raises(ValueError):
        availability.mark_available("scheduler")
    with pytest.raises(ValueError):
        availability.mark_unavailable("harness", reason_detail="")
