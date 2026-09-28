"""Explicit runtime configuration bootstrap for Agent service entrypoints."""

from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path

from shared.mysql_connection import database_target_environment


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@lru_cache(maxsize=1)
def load_agent_environment() -> None:
    """Load the Agent environment once, only when a service entrypoint starts."""

    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    os.environ.update(database_target_environment(PROJECT_ROOT / "runtime" / "database-target.json"))
