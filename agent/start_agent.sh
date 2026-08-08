#!/usr/bin/env bash
set -euo pipefail

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_EXE="${AGENT_DIR}/.venv/bin/python"
LOG_DIR="${AGENT_DIR}/logs"
LOG_FILE="${LOG_DIR}/agent-local.log"
SESSION_NAME="${AGENT_TMUX_SESSION:-codex-agent}"
PORT="${AGENT_PORT:-9000}"
HOST="${AGENT_HOST:-0.0.0.0}"
WAIT_SECONDS="${AGENT_START_TIMEOUT_SECONDS:-20}"
MODE="daemon"

source "${AGENT_DIR}/dev_local_tunnel.sh"

if [[ "${1:-}" == "--foreground" ]]; then
  MODE="foreground"
fi

if [[ ! -x "${PYTHON_EXE}" ]]; then
  echo "[ERROR] agent virtualenv not found."
  echo "Expected: ${PYTHON_EXE}"
  exit 1
fi

mkdir -p "${LOG_DIR}"

if ! detect_wsl_mysql_tunnel; then
  echo "[WARN] No reachable local MySQL tunnel auto-detected. Falling back to existing environment/.env."
fi

health_url="http://127.0.0.1:${PORT}/health"

wait_for_health() {
  local attempt
  for ((attempt=1; attempt<=WAIT_SECONDS; attempt++)); do
    if curl -fsS "${health_url}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

stop_stale_processes() {
  mapfile -t stale_pids < <(pgrep -f "${AGENT_DIR}/.venv/bin/python -m uvicorn main:app" || true)
  for pid in "${stale_pids[@]}"; do
    if [[ -n "${pid}" && "${pid}" != "$$" ]]; then
      kill "${pid}" >/dev/null 2>&1 || true
      echo "Stopped stale agent PID=${pid}"
    fi
  done
}

if [[ "${MODE}" == "foreground" ]]; then
  echo "Starting logistics agent in foreground..."
  echo "Health: ${health_url}"
  cd "${AGENT_DIR}"
  exec "${PYTHON_EXE}" -m uvicorn main:app --host "${HOST}" --port "${PORT}" --log-level info
fi

if ! command -v tmux >/dev/null 2>&1; then
  echo "[ERROR] tmux is required for stable daemon startup."
  exit 1
fi

tmux kill-session -t "${SESSION_NAME}" >/dev/null 2>&1 || true
stop_stale_processes
sleep 1

tmux new-session -d -s "${SESSION_NAME}" \
  "cd '${AGENT_DIR}' && exec '${PYTHON_EXE}' -m uvicorn main:app --host '${HOST}' --port '${PORT}' --log-level info >> '${LOG_FILE}' 2>&1"

if ! wait_for_health; then
  echo "[ERROR] agent failed to become healthy at ${health_url}"
  echo "--- ${LOG_FILE} ---"
  tail -n 80 "${LOG_FILE}" || true
  exit 1
fi

echo "Agent started."
echo "Health: ${health_url}"
echo "Session: ${SESSION_NAME}"
echo "Log: ${LOG_FILE}"
