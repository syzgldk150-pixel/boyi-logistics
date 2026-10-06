"""Migration permission failures must surface before a release stops services."""
from unittest.mock import Mock

import pytest

from scripts.migration_055_domain_databases import preflight_permissions


def cursor(grants):
    result = Mock()
    result.fetchone.return_value = {"name": "agent_db"}
    result.fetchall.return_value = [{"grant": grant} for grant in grants]
    return result


def test_runtime_only_account_cannot_start_database_split():
    with pytest.raises(RuntimeError, match="waybill_db"):
        preflight_permissions(cursor([
            "GRANT USAGE ON *.* TO 'agent'@'%'",
            "GRANT ALL PRIVILEGES ON `agent_db`.* TO 'agent'@'%'",
        ]))


def test_finance_read_only_grant_does_not_allow_migration():
    with pytest.raises(RuntimeError, match="finance_db"):
        preflight_permissions(cursor([
            "GRANT ALL PRIVILEGES ON `agent_db`.* TO 'agent'@'%'",
            "GRANT ALL PRIVILEGES ON `waybill_db`.* TO 'agent'@'%'",
            "GRANT SELECT ON `finance_db`.* TO 'agent'@'%'",
        ]))


@pytest.mark.parametrize("schemas", [("agent_db", "waybill_db", "finance_db"), ("*",)])
def test_migration_account_with_all_three_schema_grants_is_ready(schemas):
    preflight_permissions(cursor([
        f"GRANT ALL PRIVILEGES ON `{schema}`.* TO 'agent'@'%'" for schema in schemas
    ]))
