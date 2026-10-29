"""Shared executor/verifier recognition of independently verified failed writes."""
from agent.automation_plugins.daily_sign_failure_proof import is_verified_daily_sign_failure


def is_verified_business_failure(**facts):
    return is_verified_daily_sign_failure(**facts)
