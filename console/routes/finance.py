"""Finance workspace routes.

The previous ledger API was retired with migration 067; only the module page
remains while the general ledger is rebuilt.
"""

from __future__ import annotations

from typing import Any


def handle_get(app: Any, handler: Any, path: str, _raw_path: str, query: dict[str, list[str]]) -> bool:
    if path == "/modules/finance":
        app._render_finance(handler, query)
        return True
    return False


def handle_post(app: Any, handler: Any, path: str, _raw_path: str, _query: dict[str, list[str]]) -> bool:
    return False
