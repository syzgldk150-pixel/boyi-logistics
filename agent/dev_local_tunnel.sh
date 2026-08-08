#!/usr/bin/env bash
set -euo pipefail

can_tcp_connect() {
  local host="$1"
  local port="$2"
  python3 - "$host" "$port" <<'PY'
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])
sock = socket.socket()
sock.settimeout(0.6)
try:
    sock.connect((host, port))
except Exception:
    raise SystemExit(1)
finally:
    sock.close()
PY
}

detect_wsl_mysql_tunnel() {
  local gateway=""
  local current_host="${DOCFLOW_MYSQL_HOST:-${AGENT_DB_HOST:-}}"
  local current_port="${DOCFLOW_MYSQL_PORT:-${AGENT_DB_PORT:-}}"
  local candidate host port
  local -a candidates=()
  local -A seen=()

  gateway="$(ip route show default | awk '/default/ {print $3; exit}')"

  if [[ -n "${current_host}" && -n "${current_port}" ]]; then
    candidates+=("${current_host}:${current_port}")
  fi

  if [[ -n "${gateway}" ]]; then
    candidates+=("${gateway}:23306" "${gateway}:13306" "${gateway}:3306")
  fi

  candidates+=("127.0.0.1:23306" "127.0.0.1:13306" "127.0.0.1:3306")

  for candidate in "${candidates[@]}"; do
    if [[ -n "${seen[${candidate}]:-}" ]]; then
      continue
    fi
    seen["${candidate}"]=1
    host="${candidate%%:*}"
    port="${candidate##*:}"
    if can_tcp_connect "${host}" "${port}"; then
      export AGENT_DB_HOST="${host}"
      export AGENT_DB_PORT="${port}"
      export DOCFLOW_MYSQL_HOST="${host}"
      export DOCFLOW_MYSQL_PORT="${port}"
      echo "Using MySQL tunnel: ${host}:${port}"
      return 0
    fi
  done

  return 1
}
