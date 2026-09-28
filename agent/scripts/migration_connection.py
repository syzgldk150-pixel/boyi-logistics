"""Deployment database bootstrap using the exact staged shared module."""

import importlib.util
import os
from pathlib import Path


def connect(project_root: Path):
    from dotenv import load_dotenv
    import pymysql

    module_path = project_root.parent / "shared" / "mysql_connection.py"
    spec = importlib.util.spec_from_file_location("_boyi_migration_mysql", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Staged MySQL connection module is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    env_file = Path(os.getenv("MIGRATION_ENV_FILE", project_root / ".env"))
    load_dotenv(env_file)
    os.environ.update(module.database_target_environment(
        env_file.parent / "runtime" / "database-target.json"
    ))
    return pymysql.connect(
        host=os.getenv("AGENT_DB_HOST", "127.0.0.1"),
        port=int(os.getenv("AGENT_DB_PORT", "3306")),
        user=os.getenv("AGENT_DB_USER", "agent"),
        password=os.getenv("AGENT_DB_PASS", ""),
        database=os.getenv("AGENT_DB_NAME", "agent_db"),
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
        **module.mysql_tls_options(os.getenv("AGENT_DB_SSL_CA")),
    )
