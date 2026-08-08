#!/usr/bin/env bash
set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${BACKEND_DIR}/.." && pwd)"
PYTHON_EXE="${BACKEND_DIR}/.venv/bin/python"
APP_PATH="${BACKEND_DIR}/app.py"
AGENT_START_SCRIPT="${ROOT_DIR}/agent/start_agent.sh"
TUNNEL_HELPER="${ROOT_DIR}/agent/dev_local_tunnel.sh"
SESSION_NAME="${CONSOLE_TMUX_SESSION:-codex-console}"
PORT="${DOCFLOW_PORT:-8765}"
WAIT_SECONDS="${DOCFLOW_START_TIMEOUT_SECONDS:-20}"
MODE="daemon"
START_AGENT="yes"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --foreground)
      MODE="foreground"
      shift
      ;;
    --daemon)
      MODE="daemon"
      shift
      ;;
    --no-agent)
      START_AGENT="no"
      shift
      ;;
    *)
      echo "[ERROR] Unknown option: $1"
      echo "Usage: ./start_backend.sh [--daemon|--foreground] [--no-agent]"
      exit 1
      ;;
  esac
done

if [[ ! -x "${PYTHON_EXE}" ]]; then
  echo "[ERROR] console virtualenv not found."
  echo "Expected: ${PYTHON_EXE}"
  exit 1
fi

if [[ "${START_AGENT}" == "yes" && ! -x "${AGENT_START_SCRIPT}" ]]; then
  echo "[ERROR] agent start script not found."
  echo "Expected: ${AGENT_START_SCRIPT}"
  exit 1
fi

if [[ ! -f "${TUNNEL_HELPER}" ]]; then
  echo "[ERROR] local tunnel helper not found."
  echo "Expected: ${TUNNEL_HELPER}"
  exit 1
fi

source "${TUNNEL_HELPER}"

if ! detect_wsl_mysql_tunnel; then
  echo "[WARN] No reachable local MySQL tunnel auto-detected. Falling back to existing environment/.env."
fi

health_url="http://127.0.0.1:${PORT}/"

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
  mapfile -t stale_pids < <(pgrep -f "${BACKEND_DIR}/app.py" || true)
  for pid in "${stale_pids[@]}"; do
    if [[ -n "${pid}" && "${pid}" != "$$" ]]; then
      kill "${pid}" >/dev/null 2>&1 || true
      echo "Stopped stale console PID=${pid}"
    fi
  done
}

if [[ "${START_AGENT}" == "yes" ]]; then
  "${AGENT_START_SCRIPT}" >/dev/null
fi

if [[ "${MODE}" == "foreground" ]]; then
  stop_stale_processes
  sleep 1
  echo "Starting logistics agent local console in foreground..."
  echo "Open http://127.0.0.1:${PORT}"
  echo "Press Ctrl+C to stop the server."
  cd "${BACKEND_DIR}"
  exec "${PYTHON_EXE}" -u "${APP_PATH}"
fi

if ! command -v tmux >/dev/null 2>&1; then
  echo "[ERROR] tmux is required for stable daemon startup."
  exit 1
fi

tmux kill-session -t "${SESSION_NAME}" >/dev/null 2>&1 || true
stop_stale_processes
sleep 1

tmux new-session -d -s "${SESSION_NAME}" \
  "cd '${BACKEND_DIR}' && exec '${PYTHON_EXE}' -u '${APP_PATH}' > '${BACKEND_DIR}/runtime/console-local.log' 2>&1"

if ! wait_for_health; then
  echo "[ERROR] console failed to become healthy at ${health_url}"
  echo "--- ${BACKEND_DIR}/runtime/console-local.log ---"
  tail -n 80 "${BACKEND_DIR}/runtime/console-local.log" || true
  exit 1
fi

echo "Console started."
echo "Open http://127.0.0.1:${PORT}"
echo "Session: ${SESSION_NAME}"
echo "Log: ${BACKEND_DIR}/runtime/console-local.log"
