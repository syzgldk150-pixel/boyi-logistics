"""Working-directory-independent Windows Worker/Tray executable entrypoint."""

from __future__ import annotations

from agent.windows_worker.__main__ import main


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    raise SystemExit(main())
