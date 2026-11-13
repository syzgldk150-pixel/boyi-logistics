"""The deployment's customer-share settings file: NAS accounts, link secret, public address.

A plain ``KEY=value`` file (0600) kept outside the repository. Only service
code that needs the feature calls ``load_share_settings``; importing this
module reads nothing. A missing file means the feature is off.
"""
from __future__ import annotations

import os
from pathlib import Path

SETTINGS_FILE_ENV = "BOYI_RECEIPT_SHARE_ENV_FILE"
DEFAULT_SETTINGS_FILE = "~/.boyi-receipt-share/nas.env"


def load_share_settings(path: str | None = None) -> dict[str, str] | None:
    """Read ``KEY=value`` lines of the share settings file; ``None`` when it does not exist."""

    target = Path(path or os.environ.get(SETTINGS_FILE_ENV) or DEFAULT_SETTINGS_FILE).expanduser()
    if not target.is_file():
        return None
    settings: dict[str, str] = {}
    for line in target.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() and not key.lstrip().startswith("#"):
            settings[key.strip()] = value
    return settings
