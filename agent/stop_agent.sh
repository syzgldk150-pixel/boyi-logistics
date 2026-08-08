#!/usr/bin/env bash
set -euo pipefail

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SESSION_NAME="${AGENT_TMUX_SESSION:-codex-agent}"

tmux kill-session -t "${SESSION_NAME}" >/dev/null 2>&1 || true

mapfile -t pids < <(pgrep -f "${AGENT_DIR}/.venv/bin/python -m uvicorn main:app" || true)
if [[ ${#pids[@]} -eq 0 ]]; then
  echo "No matching agent process found."
  exit 0
fi

for pid in "${pids[@]}"; do
  if [[ -n "${pid}" ]]; then
    kill "${pid}" >/dev/null 2>&1 || true
    echo "Stopped agent PID=${pid}"
  fi
done
