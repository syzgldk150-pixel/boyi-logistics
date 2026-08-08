#!/usr/bin/env bash
set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${BACKEND_DIR}/.." && pwd)"
AGENT_STOP_SCRIPT="${ROOT_DIR}/agent/stop_agent.sh"
SESSION_NAME="${CONSOLE_TMUX_SESSION:-codex-console}"

tmux kill-session -t "${SESSION_NAME}" >/dev/null 2>&1 || true

mapfile -t pids < <(pgrep -f "${BACKEND_DIR}/app.py" || true)
if [[ ${#pids[@]} -eq 0 ]]; then
  echo "No matching console process found."
else
  for pid in "${pids[@]}"; do
    if [[ -n "${pid}" ]]; then
      kill "${pid}" >/dev/null 2>&1 || true
      echo "Stopped console PID=${pid}"
    fi
  done
fi

if [[ -x "${AGENT_STOP_SCRIPT}" ]]; then
  "${AGENT_STOP_SCRIPT}" || true
fi
