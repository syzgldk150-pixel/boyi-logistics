"""Explicit runtime configuration bootstrap for Agent service entrypoints."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@lru_cache(maxsize=1)
def load_agent_environment() -> None:
    """Load the Agent environment once, only when a service entrypoint starts."""

    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
