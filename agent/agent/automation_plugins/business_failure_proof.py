"""Shared executor/verifier recognition of independently verified failed writes."""
from agent.automation_plugins.daily_sign_failure_proof import is_verified_daily_sign_failure
from agent.automation_plugins.finance_failure_proof import is_verified_finance_failure


def is_verified_business_failure(**facts):
    return is_verified_finance_failure(**facts) or is_verified_daily_sign_failure(**facts)
