"""Drop only the explicitly owned test database and its domain schemas."""
import os
import re

from scripts.migration_055_domain_databases import database_names, quote


def drop_test_database(cursor, name):
    if (not re.fullmatch(r"(?:test_[A-Za-z0-9_]+|[A-Za-z0-9_]+_test)", name)
            or os.environ.get("AGENT_DB_HOST") != "127.0.0.1"):
        raise ValueError("cleanup requires an explicitly owned loopback test database")
    runtime, waybill, finance = database_names(name)
    for schema in (finance, waybill, runtime):
        cursor.execute(f"DROP DATABASE IF EXISTS {quote(schema)}")
