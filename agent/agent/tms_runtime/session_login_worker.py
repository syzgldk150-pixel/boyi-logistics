"""Isolated worker for one bounded TMS browser-login operation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from agent.tms_runtime.errors import TMSAuthStateError
from agent.tms_runtime.session_broker import build_session_broker
from agent.tms_runtime.session_state import SessionStateStore


def _result_path(state_dir: Path) -> Path:
    return state_dir / "operation_result.json"


def _write_result(state_dir: Path, payload: dict[str, Any]) -> None:
    SessionStateStore.write_dict(_result_path(state_dir), payload)


def run_worker(*, profile: str, state_dir: Path, request: dict[str, Any]) -> int:
    action = str(request.get("action") or "").strip().lower()
    code = str(request.get("code") or "").strip()
    broker = build_session_broker(
        profile,
        state_dir_override=state_dir,
        execute_login_inline=True,
    )
    try:
        if action == "send":
            result = broker.send_code()
        elif action == "submit":
            result = broker.submit_code(code)
        else:
            raise TMSAuthStateError("AUTH_UNAVAILABLE", "不支持的登录操作。")
    except TMSAuthStateError as exc:
        _write_result(
            state_dir,
            {
                "ok": False,
                "error_code": exc.code,
                "error": str(exc),
                "commit_staged_state": True,
            },
        )
        return 0
    except Exception:
        _write_result(
            state_dir,
            {
                "ok": False,
                "error_code": "AUTH_UNAVAILABLE",
                "error": "登录工作进程异常退出。",
                "commit_staged_state": False,
            },
        )
        return 0

    _write_result(
        state_dir,
        {
            "ok": True,
            "result": result,
            "commit_staged_state": True,
        },
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--state-dir", required=True)
    args = parser.parse_args()
    try:
        request = json.loads(sys.stdin.read() or "{}")
    except (TypeError, ValueError):
        request = {}
    if not isinstance(request, dict):
        request = {}
    return run_worker(
        profile=str(args.profile),
        state_dir=Path(args.state_dir),
        request=request,
    )


if __name__ == "__main__":
    raise SystemExit(main())
