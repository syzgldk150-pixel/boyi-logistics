#!/usr/bin/env bash
# Explicit local test runtime. Never loads .env or starts a system database.
set -euo pipefail
PROJECT_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
TASK_ENV="$PROJECT_ROOT/.task_tmp/v32/environment"
SHORT_TMP=/tmp/boyi-v32-tmp
cd "$PROJECT_ROOT"
operation=${1:-run}
shift || true
mkdir -p "$TASK_ENV/home" "$PROJECT_ROOT/.task_tmp/tmp"

case "$operation" in
  setup)
    [[ -f /usr/bin/python3.10 && ! -L /usr/bin/python3.10 ]] || { printf 'Install a regular trusted /usr/bin/python3.10 first; see environment.md.\n' >&2; exit 1; }
    /usr/bin/python3.10 -c 'import sys; assert sys.version_info[:2] == (3, 10)'
    command -v bwrap >/dev/null
    command -v prlimit >/dev/null
    mountpoint -q "$SHORT_TMP" || { printf 'Create the documented task-scoped short-path bind mount first.\n' >&2; exit 1; }
    [[ "$(stat -c %d:%i "$SHORT_TMP")" == "$(stat -c %d:%i "$PROJECT_ROOT/.task_tmp/tmp")" ]] || { printf 'Short-path mount points at another directory.\n' >&2; exit 1; }
    if [[ ! -f "$TASK_ENV/venv-system/bin/python" ]]; then
      /usr/bin/python3.10 -m venv --copies "$TASK_ENV/venv-system"
    fi
    env -i HOME="$TASK_ENV/home" PATH="$TASK_ENV/venv-system/bin:/usr/bin:/bin" \
      TMPDIR="$SHORT_TMP" LANG=C.UTF-8 PYTHON_DOTENV_DISABLED=1 \
      "$TASK_ENV/venv-system/bin/python" -m pip install --retries 2 --timeout 30 \
      -r agent/requirements.lock -r console/requirements.lock ruff==0.16.2 pytest==9.1.1
    "$TASK_ENV/venv-system/bin/python" agent/scripts/verify_locked_environment.py agent/requirements.lock
    "$TASK_ENV/venv-system/bin/python" agent/scripts/verify_locked_environment.py console/requirements.lock
    "$TASK_ENV/venv-system/lib/python3.10/site-packages/playwright/driver/node" --version
    mkdir -p "$TASK_ENV/packages" "$TASK_ENV/mysql-dist" "$TASK_ENV/mysql-data"
    if [[ ! -f "$TASK_ENV/mysql-dist/usr/sbin/mysqld" ]]; then
      (cd "$TASK_ENV/packages" && apt-get -o Acquire::Retries=2 download \
        mysql-server-core-8.0=8.0.46-0ubuntu0.24.04.4 \
        mysql-client-core-8.0=8.0.46-0ubuntu0.24.04.4)
      for package in "$TASK_ENV"/packages/mysql-*-core-8.0_8.0.46-0ubuntu0.24.04.4_amd64.deb; do
        dpkg-deb -x "$package" "$TASK_ENV/mysql-dist"
      done
    fi
    "$TASK_ENV/mysql-dist/usr/sbin/mysqld" --no-defaults --version
    if [[ ! -d "$TASK_ENV/mysql-data/mysql" ]]; then
      "$TASK_ENV/mysql-dist/usr/sbin/mysqld" --no-defaults --initialize-insecure \
        --basedir="$TASK_ENV/mysql-dist/usr" --datadir="$TASK_ENV/mysql-data" \
        --log-error="$TASK_ENV/mysql-initialize.log"
    fi
    if [[ ! -S "$TASK_ENV/mysql.sock" ]]; then
      "$TASK_ENV/mysql-dist/usr/sbin/mysqld" --no-defaults \
        --basedir="$TASK_ENV/mysql-dist/usr" --datadir="$TASK_ENV/mysql-data" \
        --bind-address=127.0.0.1 --port=33326 --mysqlx=0 --secure-file-priv=NULL \
        --ssl=OFF --auto-generate-certs=OFF --default-time-zone=+00:00 \
        --socket="$TASK_ENV/mysql.sock" --pid-file="$TASK_ENV/mysql.pid" \
        --log-error="$TASK_ENV/mysql-runtime.log" --skip-log-bin --daemonize
    fi
    "$TASK_ENV/mysql-dist/usr/bin/mysql" --no-defaults --socket="$TASK_ENV/mysql.sock" -u root \
      -e 'CREATE DATABASE IF NOT EXISTS agent_control_plane_test CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;'
    env -i HOME="$TASK_ENV/home" PATH="$TASK_ENV/venv-system/bin:/usr/bin:/bin" \
      PLAYWRIGHT_BROWSERS_PATH="$TASK_ENV/chromium" \
      "$TASK_ENV/venv-system/bin/python" -m playwright install chromium
    ;;
  stop)
    if [[ ! -f "$TASK_ENV/mysql.pid" ]]; then exit 0; fi
    task_pid=$(cat "$TASK_ENV/mysql.pid")
    [[ "$task_pid" =~ ^[0-9]+$ ]] || exit 1
    [[ "$(readlink -f "/proc/$task_pid/exe")" == "$(realpath "$TASK_ENV/mysql-dist/usr/sbin/mysqld")" ]] || { printf 'PID does not belong to this isolated server.\n' >&2; exit 1; }
    "$TASK_ENV/mysql-dist/usr/bin/mysqladmin" --no-defaults --socket="$TASK_ENV/mysql.sock" -u root shutdown
    ;;
  run)
    test_db=agent_control_plane_test
    if [[ "${1:-}" == --database ]]; then test_db=$2; shift 2; fi
    [[ "$test_db" =~ ^[a-z][a-z0-9_]*_test$ ]] || { printf 'Explicit database must end in _test.\n' >&2; exit 1; }
    mountpoint -q "$SHORT_TMP" || { printf 'Task-scoped short-path bind mount is required.\n' >&2; exit 1; }
    [[ "$(stat -c %d:%i "$SHORT_TMP")" == "$(stat -c %d:%i "$PROJECT_ROOT/.task_tmp/tmp")" ]] || exit 1
    browser_path=${V32_CHROMIUM_EXECUTABLE:-$TASK_ENV/chromium/chromium-1208/chrome-linux64/chrome}
    exec env -i HOME="$TASK_ENV/home" TMPDIR="$SHORT_TMP" LANG=C.UTF-8 TZ=UTC \
      PATH="$TASK_ENV/venv-system/bin:$TASK_ENV/venv-system/lib/python3.10/site-packages/playwright/driver:$TASK_ENV/mysql-dist/usr/bin:/usr/bin:/usr/local/bin:/bin" \
      PYTHONPATH="$PROJECT_ROOT/agent:$PROJECT_ROOT" PYTHON_DOTENV_DISABLED=1 \
      RUN_MYSQL_INTEGRATION=1 AGENT_DB_HOST=127.0.0.1 AGENT_DB_PORT=33326 \
      AGENT_DB_USER=root AGENT_DB_PASS='' AGENT_DB_NAME="$test_db" MIGRATION_ENV_FILE=/dev/null \
      PLAYWRIGHT_BROWSERS_PATH="$TASK_ENV/chromium" V32_CHROMIUM_EXECUTABLE="$browser_path" "$@"
    ;;
  *) printf 'Usage: isolated_environment.sh setup|stop|run [--database NAME_test] COMMAND...\n' >&2; exit 2 ;;
esac
