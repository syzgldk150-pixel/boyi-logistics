"""Explicit Console configuration bootstrap used only by service entrypoints."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


MODULE_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=1)
def load_console_environment() -> None:
    """Load Console and Agent environment files once at Console process startup."""

    from dotenv import load_dotenv

    project_root = MODULE_DIR.parent / "agent"
    for candidate in (MODULE_DIR / ".env", project_root / ".env"):
        if candidate.exists():
            load_dotenv(candidate)
    from console.config import OCR_MODULE_DIR

    if OCR_MODULE_DIR is not None:
        load_dotenv(OCR_MODULE_DIR / ".env")
