#!/usr/bin/env bash
set -Eeuo pipefail

STAGE_ROOT="${1:?stage root is required}"
RELEASE_SHA="${2:?release SHA is required}"
TARGETS_CSV="${3:?target list is required}"
SKIP_RESTART="${4:-0}"
SKIP_HEALTH="${5:-0}"
EMERGENCY_SCHEDULED_WINDOW_ARGUMENT="--emergency-scheduled-window-override=emergency_user_authorized"
KNOWN_ARRIVAL_STATS_RECOVERY_ARGUMENT="--recover-known-arrival-stats-unknown-write=fb077840-a2d0-4e7f-8089-f68c104ab544"
KNOWN_ARRIVAL_STATS_AUTH_FAILURE_RECOVERY_ARGUMENT="--recover-known-arrival-stats-auth-failure=71510af3-fcf1-461b-9c2e-152665f32f98"
KNOWN_ARRIVAL_STATS_PREWRITE_FAILURE_RECOVERY_ARGUMENT="--recover-known-arrival-stats-prewrite-failure=2a86ba4b-5c63-4bf2-93de-f61372d18274"
EMERGENCY_SCHEDULED_WINDOW_OVERRIDE=0
KNOWN_ARRIVAL_STATS_RECOVERY=0
KNOWN_ARRIVAL_STATS_AUTH_FAILURE_RECOVERY=0
KNOWN_ARRIVAL_STATS_PREWRITE_FAILURE_RECOVERY=0
DELIVERY_STATUS_UNKNOWN_WRITE_QUARANTINED=0
ARRIVAL_STATS_UNKNOWN_WRITE_BLOCKED=0
if (( $# > 9 )); then
  echo "emergency_scheduled_window_override=blocked reason=UNEXPECTED_ARGUMENT_COUNT" >&2
  exit 2
fi
if (( $# == 7 )) \
  && [[ "${6}" == "${EMERGENCY_SCHEDULED_WINDOW_ARGUMENT}" ]] \
  && [[ "${7}" != "${KNOWN_ARRIVAL_STATS_RECOVERY_ARGUMENT}" ]] \
  && [[ "${7}" != "${KNOWN_ARRIVAL_STATS_AUTH_FAILURE_RECOVERY_ARGUMENT}" ]] \
  && [[ "${7}" != "${KNOWN_ARRIVAL_STATS_PREWRITE_FAILURE_RECOVERY_ARGUMENT}" ]]; then
  echo "emergency_scheduled_window_override=blocked reason=UNEXPECTED_ARGUMENT_COUNT" >&2
  exit 2
fi
for release_argument in "${@:6}"; do
  case "${release_argument}" in
    "${EMERGENCY_SCHEDULED_WINDOW_ARGUMENT}")
      [[ "${EMERGENCY_SCHEDULED_WINDOW_OVERRIDE}" == "0" ]] || {
        echo "emergency_scheduled_window_override=blocked reason=DUPLICATE_AUTHORIZATION_ARGUMENT" >&2
        exit 2
      }
      EMERGENCY_SCHEDULED_WINDOW_OVERRIDE=1
      ;;
    "${KNOWN_ARRIVAL_STATS_RECOVERY_ARGUMENT}")
      [[ "${KNOWN_ARRIVAL_STATS_RECOVERY}" == "0" ]] || {
        echo "arrival_stats_unknown_write_recovery=blocked reason=DUPLICATE_AUTHORIZATION_ARGUMENT" >&2
        exit 2
      }
      KNOWN_ARRIVAL_STATS_RECOVERY=1
      ;;
    "${KNOWN_ARRIVAL_STATS_AUTH_FAILURE_RECOVERY_ARGUMENT}")
      [[ "${KNOWN_ARRIVAL_STATS_AUTH_FAILURE_RECOVERY}" == "0" ]] || {
        echo "arrival_stats_auth_failure_recovery=blocked reason=DUPLICATE_AUTHORIZATION_ARGUMENT" >&2
        exit 2
      }
      KNOWN_ARRIVAL_STATS_AUTH_FAILURE_RECOVERY=1
      ;;
    "${KNOWN_ARRIVAL_STATS_PREWRITE_FAILURE_RECOVERY_ARGUMENT}")
      [[ "${KNOWN_ARRIVAL_STATS_PREWRITE_FAILURE_RECOVERY}" == "0" ]] || {
        echo "arrival_stats_prewrite_failure_recovery=blocked reason=DUPLICATE_AUTHORIZATION_ARGUMENT" >&2
        exit 2
      }
      KNOWN_ARRIVAL_STATS_PREWRITE_FAILURE_RECOVERY=1
      ;;
    --emergency-scheduled-window-override=*)
      echo "emergency_scheduled_window_override=blocked reason=INVALID_AUTHORIZATION_ARGUMENT" >&2
      exit 2
      ;;
    *)
      echo "release_authorization=blocked reason=INVALID_AUTHORIZATION_ARGUMENT" >&2
      exit 2
      ;;
  esac
done
# Current production scope is server-only. Windows Worker transport, signer,
# Nginx mTLS prerequisites and dispatcher health are deliberately excluded.
WINDOWS_WORKER_RELEASE_ENABLED=0

DEPLOY_ROOT="/home/boyce/.boyi-deploy"
RELEASE_LOCK_FILE="${DEPLOY_ROOT}/release.lock"
SCHEDULER_RELEASE_HOLD_FILE="${DEPLOY_ROOT}/scheduler-release.pause"
AUTOMATION_PLUGIN_ROOT="/home/boyce/.boyi-automation-plugins"
AUTOMATION_PLUGIN_INSTALL_ROOT="${AUTOMATION_PLUGIN_ROOT}/installed"
FIRST_PARTY_PLUGIN_RELEASES_ROOT="${AUTOMATION_PLUGIN_ROOT}/releases"
FIRST_PARTY_PLUGIN_RELEASE_ROOT="${FIRST_PARTY_PLUGIN_RELEASES_ROOT}/${RELEASE_SHA}"
FIRST_PARTY_PLUGIN_TRUST_ROOT="${AUTOMATION_PLUGIN_ROOT}/trust"
STAGED_FIRST_PARTY_PLUGIN_RELEASE_ROOT="${STAGE_ROOT}/_plugin_artifacts"
STAGED_FIRST_PARTY_PLUGIN_TRUST_ROOT="${STAGE_ROOT}/_plugin_trust"
PLUGIN_RUNTIME_ENV_FILE="/home/boyce/agent/runtime/automation_plugin_release.env"
PLUGIN_CURSOR_SECRET_ENV_FILE="/etc/boyi/automation-plugin-runtime.conf"
WORKER_NGINX_STAGED_CONFIG="${STAGE_ROOT}/agent/deploy/nginx/boyi-worker-mtls.conf"
WORKER_NGINX_INSTALLED_CONFIG="/etc/nginx/snippets/boyi-worker-mtls.conf"
WORKER_NGINX_SITE_CONFIG="/etc/nginx/sites-enabled/boyi.homes.conf"
WORKER_NGINX_SITES_AVAILABLE_ROOT="/etc/nginx/sites-available"
WORKER_NGINX_SITES_ENABLED_ROOT="/etc/nginx/sites-enabled"
WORKER_MTLS_CLIENT_CA="/etc/nginx/mtls/boyi-worker-client-ca.pem"
WORKER_NGINX_BIN="/usr/sbin/nginx"
WORKER_NGINX_REQUIRED_UID=0
BACKUP_DIR="${STAGE_ROOT}/_rollback"
BACKUP_TREE="${BACKUP_DIR}/tree"
PLUGIN_TRUST_ADDITIONS_FILE="${BACKUP_DIR}/automation_plugin_trust.added"
PLUGIN_INSTALL_INVENTORY_FILE="${BACKUP_DIR}/automation_plugin_install.paths"
LEGACY_FINANCE_ETL_ROOT="/home/boyce/agent/finance_reconciliation"
VENV_ROOT="/home/boyce/.boyi-venvs"
PIP_INDEX_URL="${BOYI_PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple}"
PIP_RETRIES="${BOYI_PIP_RETRIES:-8}"
PIP_TIMEOUT_SECONDS="${BOYI_PIP_TIMEOUT_SECONDS:-300}"
MUTATION_STARTED=0
VENV_ACTIVATED=0
SERVICES_QUIESCED=0
RELEASE_STAGE="initialization"
IDENTITY_ENV_FILE="/home/boyce/agent/.env"
CONTROL_PLANE_TASK_CUTOVER_PENDING_AT_APPLY=0
DAILY_SIGN_SINGLE_TMS_PENDING_AT_APPLY=0
SCHEDULED_TASK_CONTRACT_UPGRADE_PENDING_AT_APPLY=0
AUTOMATION_PROJECT_AUTHORIZATION_PENDING_AT_APPLY=0
CONTROL_PLANE_POLICY_BOOTSTRAP_ABSENT_BEFORE_RELEASE=0
MIGRATIONS_ATTEMPTED=0
NEW_RUNTIME_START_ATTEMPTED=0
SCHEDULER_RELEASE_HOLD_CREATED=0
FIRST_PARTY_PLUGIN_INSTALL_ATTEMPTED=0

declare -A ROOTS=(
  [agent]="/home/boyce/agent"
  [console]="/home/boyce/console"
  [shared]="/home/boyce/shared"
)
declare -A SERVICES=(
  [agent]="agent.service"
  [console]="console.service"
)
declare -A WORK_DIRS=(
  [agent]="/home/boyce/agent"
  [console]="/home/boyce/console"
)
declare -A PYTHON_BINS=(
  [agent]="/home/boyce/agent/.venv/bin/python"
  [console]="/home/boyce/console/.venv/bin/python"
)
declare -A UNIT_PATHS=()
RELEASE_VENV=""
CREATED_VENV=0
declare -A VENV_SWITCHED=()
DEPENDENCY_HASH=""
declare -A PREVIOUS_VENV_LINKS=()
declare -A PREVIOUS_VENV_DIRS=()
RUNTIME_TARGETS=(agent console)

IFS=',' read -r -a REQUESTED_TARGETS <<<"${TARGETS_CSV}"
SCOPES=(shared)
for target in "${REQUESTED_TARGETS[@]}"; do
  case "${target}" in
    agent|console) SCOPES+=("${target}") ;;
    *) echo "Unsupported release target: ${target}" >&2; exit 2 ;;
  esac
done

safe_relative_path() {
  local value="$1"
  [[ -n "${value}" && "${value}" != /* && "${value}" != *".."* && "${value}" != *$'\n'* ]]
}

acquire_release_lock() {
  command -v flock >/dev/null 2>&1 || {
    echo "release_lock=blocked reason=FLOCK_UNAVAILABLE" >&2
    return 1
  }
  mkdir -p "${DEPLOY_ROOT}"
  exec 9>"${RELEASE_LOCK_FILE}"
  if ! flock -n 9; then
    echo "release_lock=blocked reason=RELEASE_ALREADY_RUNNING" >&2
    return 1
  fi
  echo "release_lock=acquired"
}

preflight_automation_plugin_runtime_environment() {
  local unit_path="$1" environment_files
  if grep -Fxq "EnvironmentFile=${PLUGIN_RUNTIME_ENV_FILE}" "${unit_path}"; then
    validate_automation_plugin_release_environment_file \
      "${PLUGIN_RUNTIME_ENV_FILE}" \
      "automation_plugin_runtime_environment" || return 1
  fi

  environment_files="$(systemctl show agent.service -p EnvironmentFiles --value)"
  if grep -Fq "${PLUGIN_CURSOR_SECRET_ENV_FILE}" <<<"${environment_files}"; then
    sudo -n test -f "${PLUGIN_CURSOR_SECRET_ENV_FILE}" \
      && sudo -n test -s "${PLUGIN_CURSOR_SECRET_ENV_FILE}" || {
        echo "automation_plugin_runtime_environment=blocked reason=CURSOR_SECRET_ENV_MISSING_OR_EMPTY" >&2
        return 1
      }
    sudo -n env LC_ALL=C awk '
      BEGIN { found = 0 }
      /^[[:space:]]*$/ { next }
      /^BOYI_AUTOMATION_PLUGIN_CURSOR_SECRET=/ {
        if (found != 0) { exit 1 }
        value = substr($0, index($0, "=") + 1)
        if (length(value) < 32 || length(value) > 4096 || value ~ /[[:space:]]/) {
          exit 1
        }
        found = 1
        next
      }
      { exit 1 }
      END { if (found != 1) { exit 1 } }
    ' "${PLUGIN_CURSOR_SECRET_ENV_FILE}" >/dev/null || {
      echo "automation_plugin_runtime_environment=blocked reason=CURSOR_SECRET_ENV_INVALID" >&2
      return 1
    }
  fi
  echo "automation_plugin_runtime_environment=ok"
}

validate_automation_plugin_release_environment_file() {
  local environment_path="$1" status_prefix="$2"
  local artifact_root trust_root verified_sha line_count
  [[ -f "${environment_path}" && ! -L "${environment_path}" ]] || {
    echo "${status_prefix}=blocked reason=MANDATORY_RELEASE_ENV_MISSING_OR_UNSAFE" >&2
    return 1
  }
  line_count="$(wc -l <"${environment_path}")"
  artifact_root="$(sed -n 's/^BOYI_AUTOMATION_PLUGIN_ARTIFACT_ROOT=//p' "${environment_path}")"
  trust_root="$(sed -n 's/^BOYI_AUTOMATION_PLUGIN_TRUST_ROOT=//p' "${environment_path}")"
  verified_sha="$(sed -n 's/^BOYI_AUTOMATION_PLUGIN_VERIFIED_RELEASE_SHA=//p' "${environment_path}")"
  [[ "${line_count}" == "3" \
    && "${artifact_root}" =~ ^/home/boyce/\.boyi-automation-plugins/releases/[0-9a-f]{40}$ \
    && "${trust_root}" == "/home/boyce/.boyi-automation-plugins/trust" \
    && "${verified_sha}" =~ ^[0-9a-f]{40}$ \
    && "${artifact_root##*/}" == "${verified_sha}" ]] || {
    echo "${status_prefix}=blocked reason=MANDATORY_RELEASE_ENV_INVALID" >&2
    return 1
  }
}

validate_automation_plugin_runtime_rollback_snapshot() {
  local rollback_unit="${UNIT_PATHS[agent]}" target
  for target in "${REQUESTED_TARGETS[@]}"; do
    if [[ "${target}" == "agent" ]]; then
      rollback_unit="${BACKUP_DIR}/agent.service"
      break
    fi
  done
  [[ -f "${rollback_unit}" && ! -L "${rollback_unit}" ]] || {
    echo "automation_plugin_runtime_rollback_snapshot=blocked reason=ROLLBACK_UNIT_MISSING_OR_UNSAFE" >&2
    return 1
  }
  if grep -Fxq "EnvironmentFile=${PLUGIN_RUNTIME_ENV_FILE}" "${rollback_unit}"; then
    [[ ! -e "${BACKUP_DIR}/automation_plugin_release.env.absent" \
      && ! -L "${BACKUP_DIR}/automation_plugin_release.env.absent" ]] || {
      echo "automation_plugin_runtime_rollback_snapshot=blocked reason=MANDATORY_RELEASE_ENV_RECORDED_ABSENT" >&2
      return 1
    }
    validate_automation_plugin_release_environment_file \
      "${BACKUP_DIR}/automation_plugin_release.env" \
      "automation_plugin_runtime_rollback_snapshot" || return 1
  fi
  echo "automation_plugin_runtime_rollback_snapshot=ok"
}

create_scheduler_release_hold() {
  [[ ! -e "${SCHEDULER_RELEASE_HOLD_FILE}" && ! -L "${SCHEDULER_RELEASE_HOLD_FILE}" ]] || {
    echo "scheduler_release_hold=blocked reason=STALE_RELEASE_HOLD" >&2
    return 1
  }
  (
    umask 077
    set -o noclobber
    printf '%s\n' "${RELEASE_SHA}" >"${SCHEDULER_RELEASE_HOLD_FILE}"
  )
  SCHEDULER_RELEASE_HOLD_CREATED=1
  echo "scheduler_release_hold=created"
}

ensure_scheduler_release_hold() {
  if [[ "${SCHEDULER_RELEASE_HOLD_CREATED}" == "0" ]]; then
    create_scheduler_release_hold
    return
  fi
  [[ -f "${SCHEDULER_RELEASE_HOLD_FILE}" && ! -L "${SCHEDULER_RELEASE_HOLD_FILE}" ]] || {
    echo "scheduler_release_hold=blocked reason=CURRENT_RELEASE_HOLD_MISSING_OR_UNSAFE" >&2
    return 1
  }
  [[ "$(tr -d '[:space:]' <"${SCHEDULER_RELEASE_HOLD_FILE}")" == "${RELEASE_SHA}" ]] || {
    echo "scheduler_release_hold=blocked reason=CURRENT_RELEASE_HOLD_OWNER_MISMATCH" >&2
    return 1
  }
  echo "scheduler_release_hold=retained"
}

clear_scheduler_release_hold_for_rollback() {
  [[ "${SCHEDULER_RELEASE_HOLD_CREATED}" == "1" ]] || return 0
  [[ -f "${SCHEDULER_RELEASE_HOLD_FILE}" && ! -L "${SCHEDULER_RELEASE_HOLD_FILE}" ]] || {
    echo "Current release scheduler hold is missing or unsafe" >&2
    return 1
  }
  [[ "$(tr -d '[:space:]' <"${SCHEDULER_RELEASE_HOLD_FILE}")" == "${RELEASE_SHA}" ]] || {
    echo "Current release scheduler hold has an unexpected owner" >&2
    return 1
  }
  rm -- "${SCHEDULER_RELEASE_HOLD_FILE}"
  SCHEDULER_RELEASE_HOLD_CREATED=0
}

validate_environment() {
  [[ "$(id -un)" == "boyce" ]] || {
    echo "Release must run as boyce" >&2
    return 1
  }
  [[ "${STAGE_ROOT}" == "${DEPLOY_ROOT}/"* && -d "${STAGE_ROOT}" ]] || {
    echo "Invalid remote stage root: ${STAGE_ROOT}" >&2
    return 1
  }
  [[ "${RELEASE_SHA}" =~ ^[0-9a-f]{40}$ ]] || {
    echo "Invalid release SHA" >&2
    return 1
  }
  [[ ! -e "${SCHEDULER_RELEASE_HOLD_FILE}" && ! -L "${SCHEDULER_RELEASE_HOLD_FILE}" ]] || {
    echo "A stale scheduler release hold requires manual recovery" >&2
    return 1
  }
  [[ "${PIP_INDEX_URL}" =~ ^https://[^[:space:]]+$ ]] || {
    echo "Dependency index must be an HTTPS URL" >&2
    return 1
  }
  [[ "${PIP_RETRIES}" =~ ^[1-9][0-9]*$ ]] || {
    echo "BOYI_PIP_RETRIES must be a positive integer" >&2
    return 1
  }
  [[ "${PIP_TIMEOUT_SECONDS}" =~ ^[1-9][0-9]*$ ]] || {
    echo "BOYI_PIP_TIMEOUT_SECONDS must be a positive integer" >&2
    return 1
  }

  local target service actual_work_dir unit_path runtime_python
  for target in "${RUNTIME_TARGETS[@]}"; do
    service="${SERVICES[$target]}"
    actual_work_dir="$(systemctl show "${service}" -p WorkingDirectory --value)"
    [[ "${actual_work_dir}" == "${WORK_DIRS[$target]}" ]] || {
      echo "Unexpected ${service} WorkingDirectory: ${actual_work_dir}" >&2
      return 1
    }
    unit_path="$(systemctl show "${service}" -p FragmentPath --value)"
    [[ -f "${unit_path}" && "$(basename "${unit_path}")" == "${service}" ]] || {
      echo "Unexpected ${service} FragmentPath: ${unit_path}" >&2
      return 1
    }
    UNIT_PATHS[$target]="${unit_path}"

    runtime_python="${PYTHON_BINS[$target]}"
    [[ -x "${runtime_python}" ]] || {
      echo "Missing runtime Python for ${target}: ${runtime_python}" >&2
      return 1
    }
    "${runtime_python}" -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 10))' || {
      echo "Unsupported runtime Python for ${target}: ${runtime_python}" >&2
      return 1
    }
  done

  # A running process survives deletion of an EnvironmentFile that systemd
  # already loaded, but the next restart does not.  Refuse to enter a
  # rollback-capable mutation when the live mandatory runtime files and the
  # installed unit have already drifted into an unrestartable state.
  preflight_automation_plugin_runtime_environment "${UNIT_PATHS[agent]}"

  if [[ -d "${STAGE_ROOT}/agent/migrations" ]]; then
    runtime_python="${PYTHON_BINS[agent]}"
    [[ -x "${runtime_python}" ]] || {
      echo "Missing Agent runtime Python for migrations: ${runtime_python}" >&2
      return 1
    }
  fi

  local scope manifest relative
  for scope in "${SCOPES[@]}"; do
    [[ -d "${STAGE_ROOT}/${scope}" ]] || {
      echo "Missing staged scope: ${scope}" >&2
      return 1
    }
    manifest="${STAGE_ROOT}/_manifests/${scope}.txt"
    [[ -f "${manifest}" ]] || {
      echo "Missing staged manifest: ${scope}" >&2
      return 1
    }
    while IFS= read -r relative || [[ -n "${relative}" ]]; do
      safe_relative_path "${relative}" || {
        echo "Unsafe manifest path in ${scope}: ${relative}" >&2
        return 1
      }
      [[ -f "${STAGE_ROOT}/${scope}/${relative}" ]] || {
        echo "Manifest entry is not a staged file: ${scope}/${relative}" >&2
        return 1
      }
    done <"${manifest}"
  done
}

worker_mtls_preflight_failure() {
  local reason="$1"
  echo "worker_mtls_proxy_preflight=blocked reason=${reason}" >&2
  return 1
}

worker_mtls_safe_root_path() {
  local path="$1"
  local reason_prefix="$2"
  local kind="$3"
  local resolved owner mode
  case "${kind}" in
    file) [[ -f "${path}" && ! -L "${path}" ]] ;;
    directory) [[ -d "${path}" && ! -L "${path}" ]] ;;
    *) return 2 ;;
  esac || {
    worker_mtls_preflight_failure "${reason_prefix}_MISSING_OR_UNSAFE"
    return 1
  }
  resolved="$(readlink -f -- "${path}")" || {
    worker_mtls_preflight_failure "${reason_prefix}_UNRESOLVED"
    return 1
  }
  [[ "${resolved}" == "${path}" ]] || {
    worker_mtls_preflight_failure "${reason_prefix}_PATH_REDIRECTED"
    return 1
  }
  owner="$(stat -Lc '%u' -- "${path}")" || {
    worker_mtls_preflight_failure "${reason_prefix}_OWNER_UNREADABLE"
    return 1
  }
  mode="$(stat -Lc '%a' -- "${path}")" || {
    worker_mtls_preflight_failure "${reason_prefix}_MODE_UNREADABLE"
    return 1
  }
  [[ "${owner}" == "${WORKER_NGINX_REQUIRED_UID}" && "${mode}" =~ ^[0-7]{3,4}$ ]] || {
    worker_mtls_preflight_failure "${reason_prefix}_OWNERSHIP_INVALID"
    return 1
  }
  (( (8#${mode} & 8#22) == 0 )) || {
    worker_mtls_preflight_failure "${reason_prefix}_WRITABLE_BY_NON_OWNER"
    return 1
  }
}

preflight_worker_mtls_proxy() {
  local staged_sha installed_sha resolved_site include_count
  [[ -f "${WORKER_NGINX_STAGED_CONFIG}" && ! -L "${WORKER_NGINX_STAGED_CONFIG}" ]] || {
    worker_mtls_preflight_failure "STAGED_CONFIG_MISSING_OR_UNSAFE"
    return 1
  }
  worker_mtls_safe_root_path \
    "$(dirname -- "${WORKER_NGINX_INSTALLED_CONFIG}")" \
    "SNIPPET_DIRECTORY" directory || return 1
  worker_mtls_safe_root_path \
    "$(dirname -- "${WORKER_MTLS_CLIENT_CA}")" \
    "CLIENT_CA_DIRECTORY" directory || return 1
  worker_mtls_safe_root_path \
    "${WORKER_NGINX_SITES_AVAILABLE_ROOT}" \
    "SITES_AVAILABLE_DIRECTORY" directory || return 1
  worker_mtls_safe_root_path \
    "${WORKER_NGINX_SITES_ENABLED_ROOT}" \
    "SITES_ENABLED_DIRECTORY" directory || return 1
  worker_mtls_safe_root_path \
    "${WORKER_NGINX_INSTALLED_CONFIG}" \
    "INSTALLED_CONFIG" file || return 1
  worker_mtls_safe_root_path "${WORKER_MTLS_CLIENT_CA}" "CLIENT_CA" file || return 1
  worker_mtls_safe_root_path "${WORKER_NGINX_BIN}" "NGINX_BINARY" file || return 1

  [[ -e "${WORKER_NGINX_SITE_CONFIG}" ]] || {
    worker_mtls_preflight_failure "SITE_CONFIG_MISSING"
    return 1
  }
  resolved_site="$(readlink -f -- "${WORKER_NGINX_SITE_CONFIG}")" || {
    worker_mtls_preflight_failure "SITE_CONFIG_UNRESOLVED"
    return 1
  }
  case "${resolved_site}" in
    "${WORKER_NGINX_SITES_AVAILABLE_ROOT}"/*|"${WORKER_NGINX_SITES_ENABLED_ROOT}"/*) ;;
    *)
      worker_mtls_preflight_failure "SITE_CONFIG_OUTSIDE_NGINX_ROOT"
      return 1
      ;;
  esac
  worker_mtls_safe_root_path "${resolved_site}" "SITE_CONFIG" file || return 1
  include_count="$(grep -Ec \
    '^[[:space:]]*include[[:space:]]+/etc/nginx/snippets/boyi-worker-mtls\.conf;[[:space:]]*$' \
    "${resolved_site}")" || true
  [[ "${include_count}" == "1" ]] || {
    worker_mtls_preflight_failure "SITE_INCLUDE_INVALID"
    return 1
  }

  staged_sha="$(sha256sum -- "${WORKER_NGINX_STAGED_CONFIG}" | awk '{print $1}')" || {
    worker_mtls_preflight_failure "STAGED_CONFIG_HASH_FAILED"
    return 1
  }
  installed_sha="$(sha256sum -- "${WORKER_NGINX_INSTALLED_CONFIG}" | awk '{print $1}')" || {
    worker_mtls_preflight_failure "INSTALLED_CONFIG_HASH_FAILED"
    return 1
  }
  [[ "${staged_sha}" =~ ^[0-9a-f]{64}$ && "${installed_sha}" == "${staged_sha}" ]] || {
    worker_mtls_preflight_failure "INSTALLED_CONFIG_RELEASE_MISMATCH"
    return 1
  }
  systemctl is-active --quiet nginx.service || {
    worker_mtls_preflight_failure "NGINX_INACTIVE"
    return 1
  }
  sudo -n "${WORKER_NGINX_BIN}" -t >/dev/null 2>&1 || {
    worker_mtls_preflight_failure "NGINX_CONFIG_TEST_FAILED"
    return 1
  }
  echo "worker_mtls_proxy_preflight=ok config_sha256=${staged_sha}"
}

preflight_service_identity_configuration() {
  local runtime_python="${PYTHON_BINS[agent]}"
  local staged_root="${STAGE_ROOT}"
  [[ -f "${IDENTITY_ENV_FILE}" ]] || {
    echo "service_identity_preflight=failed reason=configuration_missing" >&2
    return 1
  }
  [[ -f "${STAGE_ROOT}/shared/service_identity.py" ]] || {
    echo "service_identity_preflight=failed reason=staged_validator_missing" >&2
    return 1
  }

  BOYI_IDENTITY_ENV_FILE="${IDENTITY_ENV_FILE}" \
    BOYI_STAGED_ROOT="${staged_root}" \
    "${runtime_python}" - <<'PY'
import os
import sys

from dotenv import dotenv_values

sys.path.insert(0, os.environ["BOYI_STAGED_ROOT"])
from shared.service_identity import validate_service_identity_secrets


try:
    values = dotenv_values(os.environ["BOYI_IDENTITY_ENV_FILE"])
    validate_service_identity_secrets(
        internal_api_token=str(values.get("AGENT_INTERNAL_API_TOKEN") or ""),
        console_signing_secret=str(values.get("CONSOLE_AGENT_SIGNING_SECRET") or ""),
    )
except Exception:
    print("service_identity_preflight=failed reason=invalid_configuration", file=sys.stderr)
    raise SystemExit(1)
print("service_identity_preflight=ok")
PY
}

preflight_control_plane_task_cutover() {
  [[ -d "${STAGE_ROOT}/agent/migrations" ]] || return 0
  local runner="${STAGE_ROOT}/agent/scripts/run_migrations.py"
  local migration_python="${PYTHON_BINS[agent]}"
  local output
  [[ -f "${runner}" && -x "${migration_python}" ]] || {
    echo "control_plane_task_cutover_preflight=blocked reason=PREFLIGHT_RUNNER_MISSING count=1" >&2
    return 1
  }

  if output="$({
    MIGRATION_ENV_FILE="${IDENTITY_ENV_FILE}" "${migration_python}" "${runner}" \
      --preflight-control-plane-task-cutover
  } 2>&1)"; then
    if [[ "${output}" =~ ^control_plane_task_cutover_preflight=ok\ reviewed_rows=[0-9]+\ canonical_rows=[0-9]+\ legacy_rows=[0-9]+$ ]]; then
      echo "${output}"
      return 0
    fi
    echo "control_plane_task_cutover_preflight=blocked reason=UNEXPECTED_PREFLIGHT_RESPONSE count=1" >&2
    return 1
  fi

  if [[ "${output}" =~ ^control_plane_task_cutover_preflight=blocked\ reason=[A-Z0-9_]+\ count=[0-9]+$ ]]; then
    echo "${output}" >&2
  else
    echo "control_plane_task_cutover_preflight=blocked reason=UNEXPECTED_PREFLIGHT_RESPONSE count=1" >&2
  fi
  return 1
}

preflight_scheduled_write_window() {
  # Database/source rollback cannot undo a third-party write that already
  # happened.  Ask the staged, read-only checker about every currently exempt
  # external-write schedule instead of hard-coding particular task IDs/times.
  if [[ "${EMERGENCY_SCHEDULED_WINDOW_OVERRIDE}" == "1" ]]; then
    echo "scheduled_write_window=skipped emergency_user_authorized=true stage=${RELEASE_STAGE}"
    return 0
  fi
  local runner="${STAGE_ROOT}/agent/scripts/run_migrations.py"
  local migration_python="${PYTHON_BINS[agent]}"
  local output
  [[ -f "${runner}" && -x "${migration_python}" ]] || {
    echo "scheduled_write_window=blocked reason=PREFLIGHT_RUNNER_MISSING count=1" >&2
    return 1
  }

  if output="$({
    MIGRATION_ENV_FILE="${IDENTITY_ENV_FILE}" "${migration_python}" "${runner}" \
      --check-scheduled-write-window \
      --scheduled-write-window-before-minutes 60 \
      --scheduled-write-window-after-minutes 45
  } 2>&1)"; then
    if [[ "${output}" =~ ^scheduled_write_window=ok\ checked_schedules=[0-9]+$ ]]; then
      echo "${output}"
      return 0
    fi
    echo "scheduled_write_window=blocked reason=UNEXPECTED_PREFLIGHT_RESPONSE count=1" >&2
    return 1
  fi

  if [[ "${output}" =~ ^scheduled_write_window=blocked\ reason=[A-Z0-9_]+\ count=[0-9]+$ ]]; then
    echo "${output}" >&2
  else
    echo "scheduled_write_window=blocked reason=UNEXPECTED_PREFLIGHT_RESPONSE count=1" >&2
  fi
  return 1
}

run_staged_migration_runner() {
  local runner="${STAGE_ROOT}/agent/scripts/run_migrations.py"
  local migration_python="${PYTHON_BINS[agent]}"
  [[ -f "${runner}" ]] || {
    echo "Missing staged migration runner" >&2
    return 1
  }
  if [[ -n "${RELEASE_VENV}" && -x "${RELEASE_VENV}/bin/python" ]]; then
    migration_python="${RELEASE_VENV}/bin/python"
  fi
  [[ -x "${migration_python}" ]] || {
    echo "Missing Python runtime for staged migration runner" >&2
    return 1
  }
  MIGRATION_ENV_FILE="${IDENTITY_ENV_FILE}" "${migration_python}" "${runner}" "$@"
}

preflight_automation_project_scheduled_task_identities() {
  local output expected_count reason field_name
  local -a lines=()

  if output="$(
    run_staged_migration_runner \
      --check-automation-project-scheduled-task-identities 2>&1
  )"; then
    if [[ "${output}" =~ ^automation_project_scheduled_task_identities=ok\ state=(applied|pending)\ allowed_count=71$ ]]; then
      echo "${output}"
      return 0
    fi
    echo "automation_project_scheduled_task_identities=blocked reason=UNEXPECTED_PREFLIGHT_RESPONSE count=1" >&2
    return 1
  fi

  mapfile -t lines <<<"${output}"
  if [[ "${#lines[@]}" -eq 1 && "${lines[0]}" =~ ^automation_project_scheduled_task_identities=blocked\ reason=(AUTOMATION_PROJECT_IDENTITY_MODULE_MISSING|AUTOMATION_PROJECT_IDENTITY_MODULE_INVALID|AUTOMATION_PROJECT_IDENTITY_SET_INVALID|AUTOMATION_PROJECT_IDENTITY_RESULT_INVALID|AUTOMATION_PROJECT_IDENTITY_PREFLIGHT_RUNTIME_ERROR)\ count=1$ ]]; then
    echo "${lines[0]}" >&2
    return 1
  fi
  if [[ "${#lines[@]}" -lt 2 || ! "${lines[0]}" =~ ^automation_project_scheduled_task_identities=blocked\ count=([1-9][0-9]*)$ ]]; then
    echo "automation_project_scheduled_task_identities=blocked reason=UNEXPECTED_PREFLIGHT_RESPONSE count=1" >&2
    return 1
  fi
  expected_count="${BASH_REMATCH[1]}"
  if (( ${#lines[@]} - 1 != expected_count )); then
    echo "automation_project_scheduled_task_identities=blocked reason=UNEXPECTED_PREFLIGHT_RESPONSE count=1" >&2
    return 1
  fi

  local line
  for line in "${lines[@]:1}"; do
    if [[ "${line}" =~ ^automation_project_scheduled_task_identity\ task_id_hex=([0-9a-f]{0,1024})\ tool_name_hex=([0-9a-f]{0,1024})\ reason=(UNKNOWN_TASK_ID|TOOL_NAME_MISMATCH)\ field=(id|tool_name)$ ]]; then
      reason="${BASH_REMATCH[3]}"
      field_name="${BASH_REMATCH[4]}"
      if [[ "${reason}:${field_name}" != "UNKNOWN_TASK_ID:id" && \
            "${reason}:${field_name}" != "TOOL_NAME_MISMATCH:tool_name" ]]; then
        echo "automation_project_scheduled_task_identities=blocked reason=UNEXPECTED_PREFLIGHT_RESPONSE count=1" >&2
        return 1
      fi
    elif [[ "${line}" =~ ^automation_project_scheduled_task_identity_sha256=([0-9a-f]{64})\ reason=INVALID_IDENTITY\ field=(id|tool_name)$ ]]; then
      :
    else
      echo "automation_project_scheduled_task_identities=blocked reason=UNEXPECTED_PREFLIGHT_RESPONSE count=1" >&2
      return 1
    fi
  done
  printf '%s\n' "${lines[@]}" >&2
  return 1
}

preflight_automation_project_required_resources() {
  local output expected_count
  local -a lines=()

  if output="$(
    run_staged_migration_runner \
      --check-automation-project-required-resources 2>&1
  )"; then
    if [[ "${output}" == "automation_project_required_resources=ok count=8" ]]; then
      echo "${output}"
      return 0
    fi
    echo "automation_project_required_resources=blocked reason=UNEXPECTED_PREFLIGHT_RESPONSE" >&2
    return 1
  fi

  mapfile -t lines <<<"${output}"
  if [[ "${#lines[@]}" -lt 2 ]]; then
    echo "automation_project_required_resources=blocked reason=UNEXPECTED_PREFLIGHT_RESPONSE" >&2
    return 1
  fi
  if [[ "${lines[0]}" =~ ^automation_project_required_resources=blocked\ count=([1-9][0-9]*)$ ]]; then
    expected_count="${BASH_REMATCH[1]}"
  else
    echo "automation_project_required_resources=blocked reason=UNEXPECTED_PREFLIGHT_RESPONSE" >&2
    return 1
  fi
  if (( ${#lines[@]} - 1 != expected_count )); then
    echo "automation_project_required_resources=blocked reason=UNEXPECTED_PREFLIGHT_RESPONSE" >&2
    return 1
  fi
  local line
  for line in "${lines[@]:1}"; do
    if [[ ! "${line}" =~ ^automation_project_required_resource=phase7\.[a-z0-9_]+\ reason=(MISSING_ROW|INVALID_KIND|MISSING_FIELD|INVALID_FIELD_TYPE|EMPTY_FIELD)\ field=[a-z0-9_]+$ ]]; then
      echo "automation_project_required_resources=blocked reason=UNEXPECTED_PREFLIGHT_RESPONSE" >&2
      return 1
    fi
  done
  printf '%s\n' "${lines[@]}" >&2
  return 1
}

preflight_running_protected_writes() {
  local output
  if output="$(run_staged_migration_runner --check-running-protected-writes 2>&1)"; then
    if [[ "${output}" == "protected_write_quiesce=ok running_writes=0" ]]; then
      echo "${output}"
      return 0
    fi
    echo "protected_write_quiesce=blocked reason=UNEXPECTED_PREFLIGHT_RESPONSE count=1" >&2
    return 1
  fi
  if [[ "${output}" =~ ^protected_write_quiesce=blocked\ reason=[A-Z0-9_]+\ count=[0-9]+$ ]]; then
    echo "${output}" >&2
  else
    echo "protected_write_quiesce=blocked reason=UNEXPECTED_PREFLIGHT_RESPONSE count=1" >&2
  fi
  return 1
}

check_control_plane_release_manifest() {
  local expected_initial="0"
  local output pattern
  local -a args=(--check-control-plane-release-manifest)
  if [[ "${AUTOMATION_PROJECT_AUTHORIZATION_PENDING_AT_APPLY}" == "1" ]]; then
    args+=(--expect-initial-production-manifest)
    expected_initial="1"
  fi
  if output="$(run_staged_migration_runner "${args[@]}" 2>&1)"; then
    pattern="^control_plane_release_manifest=ok reviewed_rows=[0-9]+ enabled_rows=[0-9]+ policies=[0-9]+ marker=1 initial=${expected_initial}$"
    if [[ "${output}" =~ ${pattern} ]]; then
      echo "${output}"
      return 0
    fi
    echo "control_plane_release_manifest=blocked reason=UNEXPECTED_MANIFEST_RESPONSE count=1" >&2
    return 1
  fi
  if [[ "${output}" =~ ^control_plane_release_manifest=blocked\ reason=[A-Z0-9_]+\ count=[0-9]+$ ]]; then
    echo "${output}" >&2
  else
    echo "control_plane_release_manifest=blocked reason=UNEXPECTED_MANIFEST_RESPONSE count=1" >&2
  fi
  return 1
}

build_release_virtualenvs() {
  local bootstrap_python release_venv verifier agent_lock console_lock
  local agent_hash console_hash lock_hash active_agent active_console active_hash metadata_file
  verifier="${STAGE_ROOT}/agent/scripts/verify_locked_environment.py"
  [[ -f "${verifier}" ]] || {
    echo "Missing locked-environment verifier" >&2
    return 1
  }
  agent_lock="${STAGE_ROOT}/agent/requirements.lock"
  console_lock="${STAGE_ROOT}/console/requirements.lock"
  [[ -f "${agent_lock}" && -f "${console_lock}" ]] || {
    echo "Both exact dependency locks are required for the shared runtime" >&2
    return 1
  }

  bootstrap_python="$(readlink -f -- "${PYTHON_BINS[agent]}")"
  [[ -x "${bootstrap_python}" ]] || {
    echo "Could not resolve base Python for shared runtime" >&2
    return 1
  }
  "${bootstrap_python}" -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 10))' || {
    echo "Resolved base Python is not Python 3.10: ${bootstrap_python}" >&2
    return 1
  }

  agent_hash="$(sha256sum "${agent_lock}" | awk '{print $1}')"
  console_hash="$(sha256sum "${console_lock}" | awk '{print $1}')"
  lock_hash="$(printf 'agent=%s\nconsole=%s\n' "${agent_hash}" "${console_hash}" | sha256sum | awk '{print $1}')"
  [[ "${lock_hash}" =~ ^[0-9a-f]{64}$ ]] || {
    echo "Could not calculate shared dependency lock hash" >&2
    return 1
  }
  DEPENDENCY_HASH="${lock_hash}"

  active_agent="$(readlink -f -- "${ROOTS[agent]}/.venv")"
  active_console="$(readlink -f -- "${ROOTS[console]}/.venv")"
  active_hash=""
  if [[ "${active_agent}" == "${active_console}" ]]; then
    metadata_file="${active_agent}/.boyi-requirements.sha256"
    if [[ -f "${metadata_file}" ]]; then
      active_hash="$(head -n 1 "${metadata_file}" | tr -d '[:space:]')"
    fi
  fi

  if [[ "${active_hash}" == "${lock_hash}" ]] && \
    "${active_agent}/bin/python" "${verifier}" "${agent_lock}" --python-version 3.10 && \
    "${active_agent}/bin/python" "${verifier}" "${console_lock}" --python-version 3.10; then
    RELEASE_VENV="${active_agent}"
    echo "Reusing shared virtual environment for dependency lock ${lock_hash}"
    return 0
  fi

  mkdir -p "${VENV_ROOT}"
  release_venv="${VENV_ROOT}/runtime-deps-${lock_hash}"
  if [[ -d "${release_venv}" ]]; then
    if "${release_venv}/bin/python" "${verifier}" "${agent_lock}" --python-version 3.10 && \
      "${release_venv}/bin/python" "${verifier}" "${console_lock}" --python-version 3.10; then
      RELEASE_VENV="${release_venv}"
      CREATED_VENV=1
      echo "Reusing prepared shared virtual environment for dependency lock ${lock_hash}"
      return 0
    fi
    if [[ "${release_venv}" == "${active_agent}" || "${release_venv}" == "${active_console}" ]]; then
      echo "Active shared dependency environment failed verification: ${release_venv}" >&2
      return 1
    fi
    rm -rf -- "${release_venv}"
  elif [[ -e "${release_venv}" ]]; then
    echo "Shared dependency environment path is not a directory: ${release_venv}" >&2
    return 1
  fi

  "${bootstrap_python}" -m venv "${release_venv}"
  RELEASE_VENV="${release_venv}"
  CREATED_VENV=1
  "${release_venv}/bin/python" -m pip install --disable-pip-version-check \
    --index-url "${PIP_INDEX_URL}" \
    --retries "${PIP_RETRIES}" \
    --timeout "${PIP_TIMEOUT_SECONDS}" \
    --requirement "${agent_lock}" \
    --requirement "${console_lock}"
  "${release_venv}/bin/python" "${verifier}" "${agent_lock}" --python-version 3.10
  "${release_venv}/bin/python" "${verifier}" "${console_lock}" --python-version 3.10
  printf '%s\n' "${lock_hash}" >"${release_venv}/.boyi-requirements.sha256"
}

activate_release_virtualenvs() {
  local target active_venv active_agent active_console previous_dir
  active_agent="$(readlink -f -- "${ROOTS[agent]}/.venv")"
  active_console="$(readlink -f -- "${ROOTS[console]}/.venv")"
  if [[ "${active_agent}" == "${RELEASE_VENV}" && "${active_console}" == "${RELEASE_VENV}" ]]; then
    echo "Keeping active shared virtual environment: ${RELEASE_VENV}"
    return 0
  fi
  [[ "${SKIP_RESTART}" != "1" ]] || {
    echo "Cannot activate changed dependencies when service restart is disabled" >&2
    return 1
  }

  VENV_ACTIVATED=1
  for target in "${RUNTIME_TARGETS[@]}"; do
    active_venv="${ROOTS[$target]}/.venv"
    if [[ -L "${active_venv}" ]]; then
      PREVIOUS_VENV_LINKS[$target]="$(readlink "${active_venv}")"
      VENV_SWITCHED[$target]="1"
      rm -- "${active_venv}"
    elif [[ -d "${active_venv}" ]]; then
      previous_dir="${BACKUP_DIR}/${target}.venv"
      VENV_SWITCHED[$target]="1"
      mv -- "${active_venv}" "${previous_dir}"
      PREVIOUS_VENV_DIRS[$target]="${previous_dir}"
    elif [[ -e "${active_venv}" ]]; then
      echo "Unsupported active virtual environment path: ${active_venv}" >&2
      return 1
    fi
    ln -s "${RELEASE_VENV}" "${active_venv}"
  done
}

quiesce_runtime_services() {
  [[ "${SKIP_RESTART}" != "1" ]] || {
    echo "Cannot mutate source or database while service restart is disabled" >&2
    return 1
  }
  local target
  # Once shutdown begins, rollback must restore both runtime services even if
  # stopping or verifying the second unit fails midway.
  SERVICES_QUIESCED=1
  for target in "${RUNTIME_TARGETS[@]}"; do
    sudo systemctl stop "${SERVICES[$target]}"
  done
  for target in "${RUNTIME_TARGETS[@]}"; do
    if systemctl is-active --quiet "${SERVICES[$target]}"; then
      echo "Failed to quiesce ${SERVICES[$target]}" >&2
      return 1
    fi
  done
}

restore_virtualenvs() {
  local target active_venv restore_status=0
  for target in "${RUNTIME_TARGETS[@]}"; do
    [[ "${VENV_SWITCHED[$target]:-}" == "1" ]] || continue
    active_venv="${ROOTS[$target]}/.venv"
    if [[ -L "${active_venv}" && "$(readlink -f -- "${active_venv}")" == "$(readlink -f -- "${RELEASE_VENV}")" ]]; then
      rm -- "${active_venv}" || restore_status=1
    elif [[ -e "${active_venv}" || -L "${active_venv}" ]]; then
      echo "Refusing to overwrite unexpected ${target} virtual environment during rollback" >&2
      restore_status=1
      continue
    fi
    if [[ -n "${PREVIOUS_VENV_LINKS[$target]:-}" ]]; then
      ln -s "${PREVIOUS_VENV_LINKS[$target]}" "${active_venv}" || restore_status=1
    elif [[ -n "${PREVIOUS_VENV_DIRS[$target]:-}" && -d "${PREVIOUS_VENV_DIRS[$target]}" ]]; then
      mv -- "${PREVIOUS_VENV_DIRS[$target]}" "${active_venv}" || restore_status=1
    else
      echo "Missing previous ${target} virtual environment rollback material" >&2
      restore_status=1
    fi
  done
  return "${restore_status}"
}

verify_runtime_virtualenvs() {
  local target runtime_python verify_status=0
  for target in "${RUNTIME_TARGETS[@]}"; do
    runtime_python="${PYTHON_BINS[$target]}"
    if [[ ! -x "${runtime_python}" ]]; then
      echo "Recovered ${target} virtual environment is not executable" >&2
      verify_status=1
      continue
    fi
    "${runtime_python}" -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 10))' \
      >/dev/null 2>&1 || {
        echo "Recovered ${target} virtual environment is not Python 3.10" >&2
        verify_status=1
      }
  done
  return "${verify_status}"
}

remove_new_virtualenvs() {
  [[ "${CREATED_VENV}" == "1" ]] || return 0
  if [[ -n "${RELEASE_VENV}" && "${RELEASE_VENV}" == "${VENV_ROOT}/runtime-deps-"* ]]; then
    rm -rf -- "${RELEASE_VENV}"
  fi
}

record_active_dependency_hashes() {
  local target active_release expected_release metadata_temp
  expected_release="$(readlink -f -- "${RELEASE_VENV}")"
  for target in "${RUNTIME_TARGETS[@]}"; do
    active_release="$(readlink -f -- "${ROOTS[$target]}/.venv")"
    [[ "${active_release}" == "${expected_release}" ]] || {
      echo "Active ${target} environment changed during release" >&2
      return 1
    }
  done
  metadata_temp="${expected_release}/.boyi-requirements.sha256.tmp"
  printf '%s\n' "${DEPENDENCY_HASH}" >"${metadata_temp}"
  mv -- "${metadata_temp}" "${expected_release}/.boyi-requirements.sha256"
}

cleanup_successful_release() {
  # The staged tree contains the exact pre-release source, unit files, task
  # cutover backup, and previous virtual-environment references. Keep it until
  # post-release business validation is complete; cleanup is a separate,
  # bounded administrative operation.
  echo "release_recovery_bundle=${STAGE_ROOT} retention=pending_business_validation"
}

backup_managed_sources() {
  mkdir -p "${BACKUP_TREE}"
  local scope root old_manifest new_manifest backup_scope source_manifest relative
  for scope in "${SCOPES[@]}"; do
    root="${ROOTS[$scope]}"
    old_manifest="${root}/.deploy-source-manifest"
    new_manifest="${STAGE_ROOT}/_manifests/${scope}.txt"
    backup_scope="${BACKUP_TREE}/${scope}"
    mkdir -p "${backup_scope}"

    if [[ -f "${old_manifest}" ]]; then
      cp -a "${old_manifest}" "${BACKUP_DIR}/${scope}.manifest"
    else
      : >"${BACKUP_DIR}/${scope}.manifest.absent"
    fi

    source_manifest="${BACKUP_DIR}/${scope}.backup-paths"
    {
      [[ -f "${old_manifest}" ]] && cat "${old_manifest}"
      cat "${new_manifest}"
    } | sort -u >"${source_manifest}"

    if [[ -d "${root}" ]]; then
      while IFS= read -r relative || [[ -n "${relative}" ]]; do
        safe_relative_path "${relative}" || continue
        if [[ -f "${root}/${relative}" ]]; then
          mkdir -p "${backup_scope}/$(dirname "${relative}")"
          cp -a "${root}/${relative}" "${backup_scope}/${relative}"
        fi
      done <"${source_manifest}"
    fi
  done

  if [[ -f "/home/boyce/agent/runtime/release_sha" ]]; then
    cp -a "/home/boyce/agent/runtime/release_sha" "${BACKUP_DIR}/release_sha"
  else
    : >"${BACKUP_DIR}/release_sha.absent"
  fi

  if [[ -f "${PLUGIN_RUNTIME_ENV_FILE}" && ! -L "${PLUGIN_RUNTIME_ENV_FILE}" ]]; then
    cp -a "${PLUGIN_RUNTIME_ENV_FILE}" "${BACKUP_DIR}/automation_plugin_release.env"
  elif [[ ! -e "${PLUGIN_RUNTIME_ENV_FILE}" && ! -L "${PLUGIN_RUNTIME_ENV_FILE}" ]]; then
    : >"${BACKUP_DIR}/automation_plugin_release.env.absent"
  else
    echo "Unsafe automation plugin runtime environment file" >&2
    return 1
  fi

  if [[ -d "${FIRST_PARTY_PLUGIN_RELEASE_ROOT}" && \
    ! -L "${FIRST_PARTY_PLUGIN_RELEASE_ROOT}" ]]; then
    : >"${BACKUP_DIR}/first_party_plugin_release.existing"
  elif [[ ! -e "${FIRST_PARTY_PLUGIN_RELEASE_ROOT}" && \
    ! -L "${FIRST_PARTY_PLUGIN_RELEASE_ROOT}" ]]; then
    : >"${BACKUP_DIR}/first_party_plugin_release.absent"
  else
    echo "Unsafe existing first-party plugin release path" >&2
    return 1
  fi
  for root_state in \
    "${AUTOMATION_PLUGIN_ROOT}:plugin_root" \
    "${FIRST_PARTY_PLUGIN_RELEASES_ROOT}:plugin_releases_root" \
    "${FIRST_PARTY_PLUGIN_TRUST_ROOT}:plugin_trust_root"; do
    local root_path="${root_state%%:*}"
    local root_label="${root_state##*:}"
    if [[ -d "${root_path}" && ! -L "${root_path}" ]]; then
      : >"${BACKUP_DIR}/${root_label}.existing"
    elif [[ ! -e "${root_path}" && ! -L "${root_path}" ]]; then
      : >"${BACKUP_DIR}/${root_label}.absent"
    else
      echo "Unsafe automation plugin production root: ${root_path}" >&2
      return 1
    fi
  done
  : >"${PLUGIN_TRUST_ADDITIONS_FILE}"

  local target
  for target in "${REQUESTED_TARGETS[@]}"; do
    cp "${UNIT_PATHS[$target]}" "${BACKUP_DIR}/${target}.service"
  done
}

retire_legacy_finance_etl() {
  local target retired_path="${BACKUP_DIR}/retired/finance_reconciliation"
  for target in "${REQUESTED_TARGETS[@]}"; do
    [[ "${target}" == "agent" ]] || continue
    [[ "${LEGACY_FINANCE_ETL_ROOT}" == "/home/boyce/agent/finance_reconciliation" ]] || {
      echo "Refusing unexpected legacy finance ETL path: ${LEGACY_FINANCE_ETL_ROOT}" >&2
      return 1
    }
    [[ -e "${LEGACY_FINANCE_ETL_ROOT}" ]] || return 0
    [[ ! -e "${retired_path}" ]] || {
      echo "Legacy finance ETL rollback path already exists: ${retired_path}" >&2
      return 1
    }
    mkdir -p "$(dirname "${retired_path}")"
    mv -- "${LEGACY_FINANCE_ETL_ROOT}" "${retired_path}"
    return 0
  done
}

restore_legacy_finance_etl() {
  local retired_path="${BACKUP_DIR}/retired/finance_reconciliation"
  [[ -e "${retired_path}" ]] || return 0
  [[ "${LEGACY_FINANCE_ETL_ROOT}" == "/home/boyce/agent/finance_reconciliation" ]] || {
    echo "Refusing unexpected legacy finance ETL restore path: ${LEGACY_FINANCE_ETL_ROOT}" >&2
    return 1
  }
  [[ ! -e "${LEGACY_FINANCE_ETL_ROOT}" ]] || {
    echo "Refusing to overwrite legacy finance ETL during rollback" >&2
    return 1
  }
  mv -- "${retired_path}" "${LEGACY_FINANCE_ETL_ROOT}"
}

preflight_staged_first_party_source_scope() {
  local helper="${STAGE_ROOT}/agent/scripts/first_party_release_scope.py"
  local output
  [[ -f "${helper}" && ! -L "${helper}" ]] || {
    echo "first_party_release_source_scope=blocked reason=HELPER_MISSING_OR_UNSAFE" >&2
    return 1
  }
  output="$(
    PYTHONDONTWRITEBYTECODE=1 "${PYTHON_BINS[agent]}" "${helper}" \
      --repository-root "${STAGE_ROOT}" verify-staged
  )" || return 1
  [[ "${output}" == "first_party_release_source_scope=ok" ]] || {
    echo "first_party_release_source_scope=blocked reason=UNEXPECTED_RESPONSE" >&2
    return 1
  }
  echo "${output}"
}

run_static_preflight() {
  preflight_staged_first_party_source_scope
  local target runtime_python shared_python=""
  for target in "${REQUESTED_TARGETS[@]}"; do
    runtime_python="${PYTHON_BINS[$target]}"
    "${runtime_python}" -m compileall -q \
      -x '(^|/)(windows_worker($|/)|windows_worker_host\.py$)' \
      "${STAGE_ROOT}/${target}"
    if [[ -z "${shared_python}" ]]; then
      shared_python="${runtime_python}"
    fi
  done
  "${shared_python}" -m compileall -q "${STAGE_ROOT}/shared"

  local migration_count
  migration_count="$(find "${STAGE_ROOT}" -type f -path '*/migrations/*.sql' | wc -l)"
  if [[ "${migration_count}" -gt 0 ]]; then
    if [[ -f "${STAGE_ROOT}/shared/db/migrate.py" ]]; then
      "${shared_python}" "${STAGE_ROOT}/shared/db/migrate.py" --check
    elif [[ -f "${STAGE_ROOT}/agent/scripts/run_migrations.py" ]]; then
      MIGRATION_ENV_FILE="/home/boyce/agent/.env" "${PYTHON_BINS[agent]}" \
        "${STAGE_ROOT}/agent/scripts/run_migrations.py" --check
    else
      echo "SQL migrations exist but no supported migration preflight runner was staged" >&2
      return 1
    fi
  fi
}

preflight_signed_first_party_plugins() {
  local verifier="${STAGE_ROOT}/agent/scripts/verify_first_party_plugins.py"
  local digest_lock="${STAGE_ROOT}/agent/first_party_automation_plugins/digests.json"
  [[ -f "${verifier}" && ! -L "${verifier}" ]] || {
    echo "Signed first-party plugin verifier is missing or unsafe" >&2
    return 1
  }
  [[ -f "${digest_lock}" ]] || {
    echo "Signed first-party plugin digest lock is missing" >&2
    return 1
  }
  [[ -d "${STAGED_FIRST_PARTY_PLUGIN_RELEASE_ROOT}" && \
    ! -L "${STAGED_FIRST_PARTY_PLUGIN_RELEASE_ROOT}" ]] || {
    echo "Signed first-party plugin release is missing or unsafe: ${RELEASE_SHA}" >&2
    return 1
  }
  [[ -d "${STAGED_FIRST_PARTY_PLUGIN_TRUST_ROOT}" && \
    ! -L "${STAGED_FIRST_PARTY_PLUGIN_TRUST_ROOT}" ]] || {
    echo "First-party plugin trust root is missing or unsafe" >&2
    return 1
  }
  local verifier_python="${PYTHON_BINS[agent]}"
  if [[ -n "${RELEASE_VENV}" ]]; then
    verifier_python="${RELEASE_VENV}/bin/python"
  fi
  local output
  output="$(
    PYTHONPATH="${STAGE_ROOT}/agent:${STAGE_ROOT}" "${verifier_python}" "${verifier}" \
      --artifact-root "${STAGED_FIRST_PARTY_PLUGIN_RELEASE_ROOT}" \
      --trust-root "${STAGED_FIRST_PARTY_PLUGIN_TRUST_ROOT}" \
      --release-sha "${RELEASE_SHA}" \
      --digest-lock "${digest_lock}"
  )" || return 1
  grep -Fxq 'status=ok' <<<"${output}" || {
    echo "Signed first-party plugin preflight returned an invalid status" >&2
    return 1
  }
  grep -Fxq "release_sha=${RELEASE_SHA}" <<<"${output}" || {
    echo "Signed first-party plugin release SHA does not match" >&2
    return 1
  }
  grep -Eq '^package_count=[1-9][0-9]*$' <<<"${output}" || {
    echo "Signed first-party plugin package count is invalid" >&2
    return 1
  }
  grep -Eq '^instance_count=[1-9][0-9]*$' <<<"${output}" || {
    echo "Signed first-party plugin migration instance count is invalid" >&2
    return 1
  }
  grep -Eq '^contracts_sha256=[0-9a-f]{64}$' <<<"${output}" || {
    echo "Signed first-party plugin contract digest is invalid" >&2
    return 1
  }
  printf '%s\n' "${output}"
}

preflight_worker_server_identity() {
  local verifier="${STAGE_ROOT}/agent/scripts/verify_worker_server_identity.py"
  local verifier_python="${PYTHON_BINS[agent]}"
  [[ -z "${RELEASE_VENV}" ]] || verifier_python="${RELEASE_VENV}/bin/python"
  [[ -f "${verifier}" && ! -L "${verifier}" ]] || {
    echo "Windows Worker server identity preflight is missing or unsafe" >&2
    return 1
  }
  local output
  output="$(
    PYTHONPATH="${STAGE_ROOT}/agent:${STAGE_ROOT}" "${verifier_python}" "${verifier}" \
      --environment-file "${IDENTITY_ENV_FILE}"
  )" || return 1
  [[ "${output}" == "status=ok" ]] || {
    echo "Windows Worker server identity preflight returned an invalid status" >&2
    return 1
  }
}

verify_installed_first_party_plugin_artifacts() {
  local artifact_root="$1"
  local verifier="${STAGE_ROOT}/agent/scripts/verify_first_party_plugins.py"
  local digest_lock="${STAGE_ROOT}/agent/first_party_automation_plugins/digests.json"
  local verifier_python="${PYTHON_BINS[agent]}"
  [[ -z "${RELEASE_VENV}" ]] || verifier_python="${RELEASE_VENV}/bin/python"
  [[ -d "${artifact_root}" && ! -L "${artifact_root}" && \
    -d "${FIRST_PARTY_PLUGIN_TRUST_ROOT}" && \
    ! -L "${FIRST_PARTY_PLUGIN_TRUST_ROOT}" ]] || {
    echo "Installed first-party plugin paths are missing or unsafe" >&2
    return 1
  }
  PYTHONPATH="${STAGE_ROOT}/agent:${STAGE_ROOT}" "${verifier_python}" "${verifier}" \
    --artifact-root "${artifact_root}" \
    --trust-root "${FIRST_PARTY_PLUGIN_TRUST_ROOT}" \
    --release-sha "${RELEASE_SHA}" \
    --digest-lock "${digest_lock}" >/dev/null
}

install_verified_first_party_plugin_artifacts() {
  [[ ! -L "${AUTOMATION_PLUGIN_ROOT}" && \
    ! -L "${FIRST_PARTY_PLUGIN_RELEASES_ROOT}" && \
    ! -L "${FIRST_PARTY_PLUGIN_TRUST_ROOT}" ]] || {
    echo "Automation plugin production roots cannot be symbolic links" >&2
    return 1
  }
  mkdir -p "${FIRST_PARTY_PLUGIN_RELEASES_ROOT}" "${FIRST_PARTY_PLUGIN_TRUST_ROOT}"
  chmod 0700 "${AUTOMATION_PLUGIN_ROOT}" "${FIRST_PARTY_PLUGIN_RELEASES_ROOT}" \
    "${FIRST_PARTY_PLUGIN_TRUST_ROOT}"

  local source_key key_name destination_key
  local -a source_keys=("${STAGED_FIRST_PARTY_PLUGIN_TRUST_ROOT}"/*.pub)
  [[ "${#source_keys[@]}" -gt 0 ]] || {
    echo "Verified first-party plugin trust set is empty" >&2
    return 1
  }
  for source_key in "${source_keys[@]}"; do
    [[ -f "${source_key}" && ! -L "${source_key}" ]] || {
      echo "Unsafe staged automation plugin trust key" >&2
      return 1
    }
    key_name="$(basename "${source_key}")"
    [[ "${key_name}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.pub$ ]] || {
      echo "Invalid automation plugin trust key name" >&2
      return 1
    }
    destination_key="${FIRST_PARTY_PLUGIN_TRUST_ROOT}/${key_name}"
    if [[ -e "${destination_key}" || -L "${destination_key}" ]]; then
      [[ -f "${destination_key}" && ! -L "${destination_key}" ]] || {
        echo "Unsafe existing automation plugin trust key" >&2
        return 1
      }
      cmp -s -- "${source_key}" "${destination_key}" || {
        echo "Refusing to overwrite a different automation plugin trust key" >&2
        return 1
      }
    else
      local source_key_sha256
      source_key_sha256="$(sha256sum -- "${source_key}" | awk '{print $1}')"
      [[ "${source_key_sha256}" =~ ^[0-9a-f]{64}$ ]] || {
        echo "Could not fingerprint staged automation plugin trust key" >&2
        return 1
      }
      printf '%s %s\n' "${source_key_sha256}" "${key_name}" \
        >>"${PLUGIN_TRUST_ADDITIONS_FILE}"
      install -m 0644 "${source_key}" "${destination_key}"
    fi
  done

  if [[ -e "${FIRST_PARTY_PLUGIN_RELEASE_ROOT}" || -L "${FIRST_PARTY_PLUGIN_RELEASE_ROOT}" ]]; then
    [[ -d "${FIRST_PARTY_PLUGIN_RELEASE_ROOT}" && \
      ! -L "${FIRST_PARTY_PLUGIN_RELEASE_ROOT}" ]] || {
      echo "Unsafe existing first-party plugin release root" >&2
      return 1
    }
    verify_installed_first_party_plugin_artifacts "${FIRST_PARTY_PLUGIN_RELEASE_ROOT}"
    return 0
  fi

  local temp_release
  temp_release="$(mktemp -d "${FIRST_PARTY_PLUGIN_RELEASES_ROOT}/.${RELEASE_SHA}.XXXXXX")"
  [[ "${temp_release}" == "${FIRST_PARTY_PLUGIN_RELEASES_ROOT}/.${RELEASE_SHA}."* && \
    -d "${temp_release}" && ! -L "${temp_release}" ]] || {
    echo "Unsafe temporary first-party plugin release path" >&2
    return 1
  }
  chmod 0700 "${temp_release}"
  if ! cp -a "${STAGED_FIRST_PARTY_PLUGIN_RELEASE_ROOT}/." "${temp_release}/"; then
    rm -rf -- "${temp_release}"
    return 1
  fi
  find "${temp_release}" -type d -exec chmod 0700 {} +
  find "${temp_release}" -type f -exec chmod 0600 {} +
  if ! verify_installed_first_party_plugin_artifacts "${temp_release}"; then
    rm -rf -- "${temp_release}"
    return 1
  fi
  if ! mv -- "${temp_release}" "${FIRST_PARTY_PLUGIN_RELEASE_ROOT}"; then
    rm -rf -- "${temp_release}"
    return 1
  fi
}

restore_first_party_plugin_artifacts() {
  local restore_status=0 expected_sha key_name destination_key actual_sha
  local remove_release=0
  local -a trust_keys_to_remove=()

  [[ "${FIRST_PARTY_PLUGIN_RELEASE_ROOT}" == \
    "${FIRST_PARTY_PLUGIN_RELEASES_ROOT}/${RELEASE_SHA}" ]] || {
    echo "Refusing unexpected first-party plugin rollback path" >&2
    return 1
  }
  if [[ -f "${BACKUP_DIR}/first_party_plugin_release.absent" ]]; then
    if [[ -e "${FIRST_PARTY_PLUGIN_RELEASE_ROOT}" || \
      -L "${FIRST_PARTY_PLUGIN_RELEASE_ROOT}" ]]; then
      if [[ ! -d "${FIRST_PARTY_PLUGIN_RELEASE_ROOT}" || \
        -L "${FIRST_PARTY_PLUGIN_RELEASE_ROOT}" ]]; then
        echo "Refusing unsafe first-party plugin release rollback target" >&2
        restore_status=1
      elif verify_installed_first_party_plugin_artifacts \
        "${FIRST_PARTY_PLUGIN_RELEASE_ROOT}"; then
        remove_release=1
      else
        echo "Refusing to delete changed first-party plugin release artifacts" >&2
        restore_status=1
      fi
    fi
  elif [[ ! -f "${BACKUP_DIR}/first_party_plugin_release.existing" ]]; then
    echo "Missing first-party plugin release rollback state" >&2
    restore_status=1
  fi

  if [[ ! -f "${PLUGIN_TRUST_ADDITIONS_FILE}" ]]; then
    echo "Missing automation plugin trust rollback state" >&2
    restore_status=1
  else
    while read -r expected_sha key_name trailing; do
      [[ -z "${expected_sha}${key_name}${trailing:-}" ]] && continue
      if [[ -n "${trailing:-}" || \
        ! "${expected_sha}" =~ ^[0-9a-f]{64}$ || \
        ! "${key_name}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.pub$ ]]; then
        echo "Invalid automation plugin trust rollback entry" >&2
        restore_status=1
        continue
      fi
      destination_key="${FIRST_PARTY_PLUGIN_TRUST_ROOT}/${key_name}"
      if [[ ! -e "${destination_key}" && ! -L "${destination_key}" ]]; then
        continue
      fi
      if [[ ! -f "${destination_key}" || -L "${destination_key}" ]]; then
        echo "Refusing unsafe automation plugin trust rollback target" >&2
        restore_status=1
        continue
      fi
      actual_sha="$(sha256sum -- "${destination_key}" | awk '{print $1}')"
      if [[ "${actual_sha}" != "${expected_sha}" ]]; then
        echo "Refusing to delete changed automation plugin trust key" >&2
        restore_status=1
        continue
      fi
      trust_keys_to_remove+=("${destination_key}")
    done <"${PLUGIN_TRUST_ADDITIONS_FILE}"
  fi

  [[ "${restore_status}" == "0" ]] || return "${restore_status}"
  if [[ "${remove_release}" == "1" ]]; then
    rm -rf -- "${FIRST_PARTY_PLUGIN_RELEASE_ROOT}" || restore_status=1
  fi
  for destination_key in "${trust_keys_to_remove[@]}"; do
    rm -- "${destination_key}" || restore_status=1
  done

  if [[ -f "${BACKUP_DIR}/plugin_trust_root.absent" ]]; then
    rmdir -- "${FIRST_PARTY_PLUGIN_TRUST_ROOT}" 2>/dev/null || true
  fi
  if [[ -f "${BACKUP_DIR}/plugin_releases_root.absent" ]]; then
    rmdir -- "${FIRST_PARTY_PLUGIN_RELEASES_ROOT}" 2>/dev/null || true
  fi
  if [[ -f "${BACKUP_DIR}/plugin_root.absent" ]]; then
    rmdir -- "${AUTOMATION_PLUGIN_ROOT}" 2>/dev/null || true
  fi
  return "${restore_status}"
}

validate_automation_plugin_install_inventory() {
  local inventory_file="$1" relative
  [[ -f "${inventory_file}" && ! -L "${inventory_file}" ]] || return 1
  while IFS= read -r relative || [[ -n "${relative}" ]]; do
    [[ -n "${relative}" ]] || continue
    if [[ "${relative}" == ".staging" ]]; then
      continue
    fi
    if [[ "${relative}" =~ ^\.staging/[a-z][a-z0-9_]{1,63}-[0-9]+\.[0-9]+\.[0-9]+-[0-9a-f]{32}$ ]]; then
      continue
    fi
    if [[ "${relative}" =~ ^[a-z][a-z0-9_]{1,63}/[0-9]+\.[0-9]+\.[0-9]+-[0-9a-f]{12}$ ]]; then
      continue
    fi
    if [[ "${relative}" =~ ^[a-z][a-z0-9_]{1,63}$ ]]; then
      continue
    fi
    return 1
  done <"${inventory_file}"
}

capture_automation_plugin_installation_state() {
  mkdir -p "${BACKUP_DIR}"
  : >"${PLUGIN_INSTALL_INVENTORY_FILE}"
  if [[ ! -e "${AUTOMATION_PLUGIN_INSTALL_ROOT}" && \
    ! -L "${AUTOMATION_PLUGIN_INSTALL_ROOT}" ]]; then
    : >"${BACKUP_DIR}/automation_plugin_install_root.absent"
    return 0
  fi
  [[ -d "${AUTOMATION_PLUGIN_INSTALL_ROOT}" && \
    ! -L "${AUTOMATION_PLUGIN_INSTALL_ROOT}" ]] || {
    echo "Unsafe automation plugin installation root" >&2
    return 1
  }
  : >"${BACKUP_DIR}/automation_plugin_install_root.existing"
  if find "${AUTOMATION_PLUGIN_INSTALL_ROOT}" -mindepth 1 -maxdepth 2 \
    ! -type d -print -quit | grep -q .; then
    echo "Automation plugin installation index contains an unsafe entry" >&2
    return 1
  fi
  find "${AUTOMATION_PLUGIN_INSTALL_ROOT}" -mindepth 1 -maxdepth 2 -type d \
    -printf '%P\n' | LC_ALL=C sort -u >"${PLUGIN_INSTALL_INVENTORY_FILE}"
  validate_automation_plugin_install_inventory "${PLUGIN_INSTALL_INVENTORY_FILE}" || {
    echo "Automation plugin installation index is invalid" >&2
    return 1
  }
}

restore_automation_plugin_installations() {
  local current_inventory new_inventory missing_inventory relative source destination
  local quarantine="${BACKUP_DIR}/retired/automation_plugin_installed"
  validate_automation_plugin_install_inventory "${PLUGIN_INSTALL_INVENTORY_FILE}" || {
    echo "Missing or invalid automation plugin installation rollback state" >&2
    return 1
  }
  if [[ ! -e "${AUTOMATION_PLUGIN_INSTALL_ROOT}" && \
    ! -L "${AUTOMATION_PLUGIN_INSTALL_ROOT}" ]]; then
    [[ -f "${BACKUP_DIR}/automation_plugin_install_root.absent" ]] && return 0
    echo "Automation plugin installation root disappeared during release" >&2
    return 1
  fi
  [[ -d "${AUTOMATION_PLUGIN_INSTALL_ROOT}" && \
    ! -L "${AUTOMATION_PLUGIN_INSTALL_ROOT}" ]] || {
    echo "Unsafe automation plugin installation rollback root" >&2
    return 1
  }
  current_inventory="$(mktemp "${BACKUP_DIR}/automation_plugin_install.current.XXXXXX")"
  new_inventory="$(mktemp "${BACKUP_DIR}/automation_plugin_install.new.XXXXXX")"
  missing_inventory="$(mktemp "${BACKUP_DIR}/automation_plugin_install.missing.XXXXXX")"
  if find "${AUTOMATION_PLUGIN_INSTALL_ROOT}" -mindepth 1 -maxdepth 2 \
    ! -type d -print -quit | grep -q .; then
    echo "Automation plugin rollback found an unsafe indexed entry" >&2
    return 1
  fi
  find "${AUTOMATION_PLUGIN_INSTALL_ROOT}" -mindepth 1 -maxdepth 2 -type d \
    -printf '%P\n' | LC_ALL=C sort -u >"${current_inventory}"
  validate_automation_plugin_install_inventory "${current_inventory}" || {
    echo "Automation plugin rollback found an invalid path" >&2
    return 1
  }
  if ! LC_ALL=C comm --check-order -23 \
    "${PLUGIN_INSTALL_INVENTORY_FILE}" "${current_inventory}" >"${missing_inventory}"; then
    echo "Automation plugin rollback inventory comparison failed" >&2
    return 1
  fi
  if [[ -s "${missing_inventory}" ]]; then
    echo "A pre-release automation plugin installation disappeared" >&2
    return 1
  fi
  if ! LC_ALL=C comm --check-order -13 \
    "${PLUGIN_INSTALL_INVENTORY_FILE}" "${current_inventory}" >"${new_inventory}"; then
    echo "Automation plugin rollback inventory comparison failed" >&2
    return 1
  fi

  # Services are stopped and migration 018 has already been restored. Move only
  # release-created immutable version/staging directories out of the live root;
  # never delete or overwrite a path that existed before this release.
  while IFS= read -r relative || [[ -n "${relative}" ]]; do
    [[ "${relative}" == */* ]] || continue
    source="${AUTOMATION_PLUGIN_INSTALL_ROOT}/${relative}"
    destination="${quarantine}/${relative}"
    [[ -d "${source}" && ! -L "${source}" && ! -e "${destination}" ]] || {
      echo "Unsafe automation plugin rollback candidate: ${relative}" >&2
      return 1
    }
    mkdir -p "$(dirname "${destination}")"
    mv -- "${source}" "${destination}"
  done <"${new_inventory}"
  while IFS= read -r relative || [[ -n "${relative}" ]]; do
    [[ -n "${relative}" && "${relative}" != */* ]] || continue
    rmdir -- "${AUTOMATION_PLUGIN_INSTALL_ROOT}/${relative}" || {
      echo "New automation plugin project root is not empty: ${relative}" >&2
      return 1
    }
  done <"${new_inventory}"
  if [[ -f "${BACKUP_DIR}/automation_plugin_install_root.absent" ]]; then
    rmdir -- "${AUTOMATION_PLUGIN_INSTALL_ROOT}" || {
      echo "New automation plugin installation root is not empty" >&2
      return 1
    }
  elif [[ ! -f "${BACKUP_DIR}/automation_plugin_install_root.existing" ]]; then
    echo "Missing automation plugin installation root prestate" >&2
    return 1
  fi
}

prepare_retired_automation_plugins_for_stage_cleanup() {
  local quarantine="${BACKUP_DIR}/retired/automation_plugin_installed"
  local expected_quarantine="${STAGE_ROOT}/_rollback/retired/automation_plugin_installed"
  local inventory path path_device path_uid stage_device expected_uid
  local -a cleanup_paths=()

  [[ "${BACKUP_DIR}" == "${STAGE_ROOT}/_rollback" && \
    "${quarantine}" == "${expected_quarantine}" ]] || {
    echo "Retired automation plugin cleanup path escaped the current release stage" >&2
    return 1
  }
  if [[ ! -e "${quarantine}" && ! -L "${quarantine}" ]]; then
    return 0
  fi
  [[ -d "${quarantine}" && ! -L "${quarantine}" ]] || {
    echo "Retired automation plugin cleanup root is unsafe" >&2
    return 1
  }
  [[ "$(readlink -e -- "${quarantine}")" == "${expected_quarantine}" ]] || {
    echo "Retired automation plugin cleanup root resolved outside the current release stage" >&2
    return 1
  }

  stage_device="$(stat -c '%d' -- "${STAGE_ROOT}")" || return 1
  expected_uid="$(id -u)" || return 1
  inventory="$(mktemp "${BACKUP_DIR}/automation_plugin_cleanup.inventory.XXXXXX")" || return 1
  if ! find "${quarantine}" -xdev -print0 >"${inventory}"; then
    rm -f -- "${inventory}"
    echo "Could not inventory retired automation plugins for cleanup" >&2
    return 1
  fi
  mapfile -d '' -t cleanup_paths <"${inventory}"
  rm -f -- "${inventory}" || return 1

  for path in "${cleanup_paths[@]}"; do
    [[ "${path}" == "${quarantine}" || "${path}" == "${quarantine}/"* ]] || {
      echo "Retired automation plugin cleanup inventory escaped its root" >&2
      return 1
    }
    [[ ! -L "${path}" && ( -d "${path}" || -f "${path}" ) ]] || {
      echo "Retired automation plugin cleanup inventory contains an unsafe entry" >&2
      return 1
    }
    path_device="$(stat -c '%d' -- "${path}")" || return 1
    path_uid="$(stat -c '%u' -- "${path}")" || return 1
    [[ "${path_device}" == "${stage_device}" && "${path_uid}" == "${expected_uid}" ]] || {
      echo "Retired automation plugin cleanup inventory ownership or device changed" >&2
      return 1
    }
  done

  # Signed package directories are intentionally 0555. Only make directories in
  # this already-quarantined, fully validated tree owner-writable so the release
  # stage can be removed; immutable files remain unchanged.
  for path in "${cleanup_paths[@]}"; do
    [[ -d "${path}" ]] || continue
    chmod u+rwx -- "${path}" || return 1
  done
}

cleanup_failed_release_stage() {
  local deploy_resolved stage_name stage_resolved expected_uid

  [[ -d "${DEPLOY_ROOT}" && ! -L "${DEPLOY_ROOT}" && \
    -d "${STAGE_ROOT}" && ! -L "${STAGE_ROOT}" ]] || {
    echo "Release stage cleanup target is missing or unsafe" >&2
    return 1
  }
  stage_name="$(basename -- "${STAGE_ROOT}")"
  [[ "${stage_name}" =~ ^release-${RELEASE_SHA:0:12}-[0-9]{14}$ ]] || {
    echo "Release stage cleanup target has an unexpected identity" >&2
    return 1
  }
  deploy_resolved="$(readlink -e -- "${DEPLOY_ROOT}")" || return 1
  stage_resolved="$(readlink -e -- "${STAGE_ROOT}")" || return 1
  [[ "${stage_resolved}" == "${STAGE_ROOT}" && \
    "${stage_resolved}" == "${deploy_resolved}/${stage_name}" ]] || {
    echo "Release stage cleanup target escaped the deployment root" >&2
    return 1
  }
  expected_uid="$(id -u)" || return 1
  [[ "$(stat -c '%u' -- "${STAGE_ROOT}")" == "${expected_uid}" && \
    "$(stat -c '%d' -- "${STAGE_ROOT}")" == "$(stat -c '%d' -- "${DEPLOY_ROOT}")" ]] || {
    echo "Release stage cleanup target ownership or device changed" >&2
    return 1
  }

  prepare_retired_automation_plugins_for_stage_cleanup || return 1
  rm -rf -- "${STAGE_ROOT}"
}

write_automation_plugin_runtime_environment() {
  local runtime_dir temp_path
  runtime_dir="$(dirname "${PLUGIN_RUNTIME_ENV_FILE}")"
  mkdir -p "${runtime_dir}"
  temp_path="$(mktemp "${runtime_dir}/.automation_plugin_release.XXXXXX")"
  chmod 0600 "${temp_path}"
  if ! {
    printf 'BOYI_AUTOMATION_PLUGIN_ARTIFACT_ROOT=%s\n' "${FIRST_PARTY_PLUGIN_RELEASE_ROOT}"
    printf 'BOYI_AUTOMATION_PLUGIN_TRUST_ROOT=%s\n' "${FIRST_PARTY_PLUGIN_TRUST_ROOT}"
    printf 'BOYI_AUTOMATION_PLUGIN_VERIFIED_RELEASE_SHA=%s\n' "${RELEASE_SHA}"
  } >"${temp_path}"; then
    rm -f -- "${temp_path}"
    return 1
  fi
  if ! mv -f -- "${temp_path}" "${PLUGIN_RUNTIME_ENV_FILE}"; then
    rm -f -- "${temp_path}"
    return 1
  fi
}

apply_migrations() {
  [[ -d "${STAGE_ROOT}/agent/migrations" ]] || return 0
  local runner="${STAGE_ROOT}/agent/scripts/run_migrations.py"
  [[ -f "${runner}" ]] || {
    echo "Staged SQL migrations are missing their runner" >&2
    return 1
  }
  local migration_python="${PYTHON_BINS[agent]}"
  if [[ -n "${RELEASE_VENV}" ]]; then
    migration_python="${RELEASE_VENV}/bin/python"
  fi
  MIGRATION_ENV_FILE="/home/boyce/agent/.env" "${migration_python}" "${runner}"
}

capture_control_plane_release_state() {
  local status
  if [[ ! -d "${STAGE_ROOT}/agent/migrations" ]]; then
    CONTROL_PLANE_TASK_CUTOVER_PENDING_AT_APPLY=0
    DAILY_SIGN_SINGLE_TMS_PENDING_AT_APPLY=0
    SCHEDULED_TASK_CONTRACT_UPGRADE_PENDING_AT_APPLY=0
    AUTOMATION_PROJECT_AUTHORIZATION_PENDING_AT_APPLY=0
    CONTROL_PLANE_POLICY_BOOTSTRAP_ABSENT_BEFORE_RELEASE=0
    return 0
  fi
  status="$(run_staged_migration_runner --control-plane-task-cutover-status)" || return 1
  case "${status}" in
    control_plane_task_cutover_status=pending_clean)
      CONTROL_PLANE_TASK_CUTOVER_PENDING_AT_APPLY=1
      ;;
    control_plane_task_cutover_status=applied)
      CONTROL_PLANE_TASK_CUTOVER_PENDING_AT_APPLY=0
      ;;
    control_plane_task_cutover_status=pending_dirty)
      echo "A previous migration 014 attempt left unrecovered scheduler backup data" >&2
      return 1
      ;;
    *)
      echo "Unexpected control-plane task migration state response" >&2
      return 1
      ;;
  esac

  status="$(run_staged_migration_runner --daily-sign-single-tms-status)" || return 1
  case "${status}" in
    daily_sign_single_tms_status=pending_clean)
      DAILY_SIGN_SINGLE_TMS_PENDING_AT_APPLY=1
      ;;
    daily_sign_single_tms_status=applied)
      DAILY_SIGN_SINGLE_TMS_PENDING_AT_APPLY=0
      ;;
    daily_sign_single_tms_status=pending_dirty)
      echo "A previous migration 016 attempt left unrecovered daily-sign backup data" >&2
      return 1
      ;;
    *)
      echo "Unexpected migration 016 state response" >&2
      return 1
      ;;
  esac

  status="$(run_staged_migration_runner --scheduled-task-contract-upgrade-status)" || return 1
  case "${status}" in
    scheduled_task_contract_upgrade_status=pending_clean)
      SCHEDULED_TASK_CONTRACT_UPGRADE_PENDING_AT_APPLY=1
      ;;
    scheduled_task_contract_upgrade_status=applied)
      SCHEDULED_TASK_CONTRACT_UPGRADE_PENDING_AT_APPLY=0
      ;;
    scheduled_task_contract_upgrade_status=pending_dirty)
      echo "A previous migration 017 attempt left unrecovered scheduler backup data" >&2
      return 1
      ;;
    *)
      echo "Unexpected migration 017 state response" >&2
      return 1
      ;;
  esac

  status="$(run_staged_migration_runner --automation-project-authorization-status)" || return 1
  case "${status}" in
    automation_project_authorization_status=pending_clean)
      AUTOMATION_PROJECT_AUTHORIZATION_PENDING_AT_APPLY=1
      ;;
    automation_project_authorization_status=applied)
      AUTOMATION_PROJECT_AUTHORIZATION_PENDING_AT_APPLY=0
      ;;
    automation_project_authorization_status=pending_dirty)
      echo "A previous migration 018 attempt left unrecovered automation project data" >&2
      return 1
      ;;
    *)
      echo "Unexpected migration 018 state response" >&2
      return 1
      ;;
  esac

  status="$(run_staged_migration_runner --control-plane-policy-bootstrap-marker-status)" || return 1
  case "${status}" in
    control_plane_policy_bootstrap_marker_status=absent)
      CONTROL_PLANE_POLICY_BOOTSTRAP_ABSENT_BEFORE_RELEASE=1
      ;;
    control_plane_policy_bootstrap_marker_status=present)
      CONTROL_PLANE_POLICY_BOOTSTRAP_ABSENT_BEFORE_RELEASE=0
      ;;
    *)
      echo "Unexpected control-plane policy bootstrap marker response" >&2
      return 1
      ;;
  esac
}

restore_control_plane_task_cutover_data() {
  run_staged_migration_runner --restore-control-plane-task-cutover
}

restore_daily_sign_single_tms_data() {
  run_staged_migration_runner --restore-daily-sign-single-tms-account
}

restore_scheduled_task_contract_upgrade_data() {
  run_staged_migration_runner --restore-scheduled-task-contract-upgrade
}

restore_automation_project_authorization_data() {
  run_staged_migration_runner --restore-automation-project-authorization
}

restore_control_plane_policy_bootstrap_data() {
  run_staged_migration_runner --restore-control-plane-policy-bootstrap
}

sync_scope() {
  local scope="$1"
  local root="${ROOTS[$scope]}"
  local old_manifest="${root}/.deploy-source-manifest"
  local new_manifest="${STAGE_ROOT}/_manifests/${scope}.txt"
  local relative

  mkdir -p "${root}"
  if [[ -f "${old_manifest}" ]]; then
    while IFS= read -r relative || [[ -n "${relative}" ]]; do
      safe_relative_path "${relative}" || {
        echo "Unsafe existing manifest path in ${scope}: ${relative}" >&2
        return 1
      }
      if ! grep -Fxq -- "${relative}" "${new_manifest}"; then
        rm -f -- "${root}/${relative}"
      fi
    done <"${old_manifest}"
  fi

  while IFS= read -r relative || [[ -n "${relative}" ]]; do
    mkdir -p "${root}/$(dirname "${relative}")"
    cp -a "${STAGE_ROOT}/${scope}/${relative}" "${root}/${relative}"
    if [[ "${relative}" == *.sh ]]; then
      chmod 0755 "${root}/${relative}"
    fi
  done <"${new_manifest}"
  cp -a "${new_manifest}" "${old_manifest}"
}

install_service_units() {
  local target staged_unit
  for target in "${REQUESTED_TARGETS[@]}"; do
    staged_unit="${STAGE_ROOT}/${target}/${target}.service"
    [[ -f "${staged_unit}" ]] || {
      echo "Missing staged systemd unit: ${staged_unit}" >&2
      return 1
    }
    sudo install -m 0644 "${staged_unit}" "${UNIT_PATHS[$target]}"
  done
  sudo systemctl daemon-reload
}

restart_services() {
  [[ "${SKIP_RESTART}" == "1" ]] && return 0
  local target service
  local -a restart_targets=("${REQUESTED_TARGETS[@]}")
  if [[ "${VENV_ACTIVATED}" == "1" || "${SERVICES_QUIESCED}" == "1" ]]; then
    restart_targets=("${RUNTIME_TARGETS[@]}")
  fi
  for target in "${restart_targets[@]}"; do
    service="${SERVICES[$target]}"
    if ! sudo systemctl restart "${service}"; then
      systemctl show "${service}" -p ActiveState -p SubState -p Result -p ExecMainStatus >&2 || true
      return 1
    fi
    if ! systemctl is-active --quiet "${service}"; then
      systemctl show "${service}" -p ActiveState -p SubState -p Result -p ExecMainStatus >&2 || true
      return 1
    fi
  done
}

stop_runtime_services_for_rollback() {
  local target service stop_status=0
  for target in "${RUNTIME_TARGETS[@]}"; do
    service="${SERVICES[$target]}"
    sudo systemctl stop "${service}" || \
      echo "Rollback stop command failed for ${service}; checking final state" >&2
  done
  for target in "${RUNTIME_TARGETS[@]}"; do
    service="${SERVICES[$target]}"
    if systemctl is-active --quiet "${service}"; then
      echo "Rollback cannot continue while ${service} remains active" >&2
      stop_status=1
    fi
  done
  return "${stop_status}"
}

restart_runtime_services_for_rollback() {
  local target service restart_status=0
  for target in "${RUNTIME_TARGETS[@]}"; do
    service="${SERVICES[$target]}"
    if ! sudo systemctl restart "${service}"; then
      echo "Rollback restart failed for ${service}" >&2
      restart_status=1
    fi
  done
  for target in "${RUNTIME_TARGETS[@]}"; do
    service="${SERVICES[$target]}"
    if ! systemctl is-active --quiet "${service}"; then
      echo "Rollback verification found ${service} inactive" >&2
      restart_status=1
    fi
  done
  return "${restart_status}"
}

check_health() {
  [[ "${SKIP_HEALTH}" == "1" ]] && return 0
  local target attempt body healthy
  local -a health_targets=("${REQUESTED_TARGETS[@]}")
  if [[ "${VENV_ACTIVATED}" == "1" || "${SERVICES_QUIESCED}" == "1" ]]; then
    health_targets=("${RUNTIME_TARGETS[@]}")
  fi
  for target in "${health_targets[@]}"; do
    healthy=0
    for attempt in {1..15}; do
      if [[ "${target}" == "agent" ]]; then
        body="$(curl -fsS --max-time 5 http://127.0.0.1:9000/health 2>/dev/null || true)"
        if RELEASE_BODY="${body}" RELEASE_EXPECTED="${RELEASE_SHA}" "${PYTHON_BINS[agent]}" - <<'PY'
import json
import os

try:
    payload = json.loads(os.environ.get("RELEASE_BODY", ""))
except json.JSONDecodeError:
    raise SystemExit(1)
if payload.get("status") != "ok" or payload.get("release_sha") != os.environ["RELEASE_EXPECTED"]:
    raise SystemExit(1)
PY
        then
          healthy=1
          break
        fi
      elif curl -fsS --max-time 5 http://127.0.0.1:8765/ >/dev/null 2>&1; then
        healthy=1
        break
      fi
      sleep 2
    done
    [[ "${healthy}" == "1" ]] || {
      echo "Health check failed for ${target}" >&2
      return 1
    }
  done
}

check_service_identity_smoke() {
  local smoke_scope="${1:-full}"
  [[ "${smoke_scope}" == "full" || "${smoke_scope}" == "recovery_transport" || "${smoke_scope}" == "delivery_unknown_write_quarantine" ]] || {
    echo "service_identity_smoke=failed reason=identity_configuration" >&2
    return 1
  }
  local console_python="${PYTHON_BINS[console]}"
  [[ -x "${console_python}" && -f "${IDENTITY_ENV_FILE}" ]] || {
    echo "service_identity_smoke=failed reason=runtime_unavailable" >&2
    return 1
  }

  BOYI_IDENTITY_ENV_FILE="${IDENTITY_ENV_FILE}" \
  BOYI_DEPLOYED_ROOT="/home/boyce" \
  BOYI_RELEASE_SHA="${RELEASE_SHA}" \
  BOYI_SERVICE_IDENTITY_SMOKE_SCOPE="${smoke_scope}" \
    "${console_python}" - <<'PY'
import json
import os
import secrets
import sys
from enum import Enum
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

class SmokeGate(str, Enum):
    IDENTITY_CONFIGURATION = "identity_configuration"
    TRANSPORT_UNAVAILABLE = "transport_unavailable"
    HTTP_AUTH_REJECTED = "http_auth_rejected"
    HTTP_SERVER_ERROR = "http_server_error"
    HTTP_REJECTED_OTHER = "http_rejected_other"
    INVALID_JSON_OR_SHAPE = "invalid_json_or_shape"
    STATUS_NOT_OK = "status_not_ok"
    RELEASE_SHA_MISMATCH = "release_sha_mismatch"
    SCHEDULER_STATE = "scheduler_state"
    SCHEDULER_HOLD = "scheduler_hold"
    RUNNER_STATE = "runner_state"
    RUNNER_HOLD = "runner_hold"
    RUNNER_ACTIVE = "runner_active"
    PLUGIN_BROKER = "plugin_broker"
    PLUGIN_CATALOG_UNSUPPORTED = "plugin_catalog_unsupported"
    PLUGIN_CATALOG_ENABLED_BUILTIN = "plugin_catalog_enabled_builtin"
    PLUGIN_CATALOG_INVALID_TRUST = "plugin_catalog_invalid_trust"
    PLUGIN_CATALOG_UNSTABLE_GENERATIONS = "plugin_catalog_unstable_generations"
    PLUGIN_CATALOG_INVALID_RUNTIME = "plugin_catalog_invalid_runtime"
    PLUGIN_CATALOG_AGGREGATE_OR_SHAPE = "plugin_catalog_aggregate_or_shape"
    PLUGIN_GENERATIONS = "plugin_generations"
    PLUGIN_AGGREGATE = "plugin_aggregate"
    PLUGIN_UNAFFECTED_RELEASE = "plugin_unaffected_release"
    PLUGIN_UNAFFECTED_RELEASE_SHAPE = "plugin_unaffected_release_shape"
    WORKER = "worker"
    BOOTSTRAP_SHAPE = "bootstrap_shape"
    BOOTSTRAP_INCOMPLETE = "bootstrap_incomplete"
    BOOTSTRAP_REJECTED = "bootstrap_rejected"


_CLOSED_AUTOMATION_IDS = frozenset(
    {
        "arrival_stats",
        "arrive_list",
        "clockin_daxiang",
        "clockin_daxiang_s",
        "customer_problems_shadow",
        "daily_sign",
        "delivery_status",
        "finance_bills",
        "finance_startup_catchup",
        "scan_codes",
        "self_pickup_problem_upload",
        "send_order",
        "site_send",
        "split_pending_problem_upload",
        "yunda_dispatch_forecast",
        "yunda_send_waybills",
    }
)
_QUARANTINED_AUTOMATION_IDS = frozenset(
    {"arrive_list", "daily_sign", "delivery_status"}
)


def http_failure_gate(status):
    if status in {401, 403}:
        return SmokeGate.HTTP_AUTH_REJECTED
    if isinstance(status, int) and 500 <= status <= 599:
        return SmokeGate.HTTP_SERVER_ERROR
    return SmokeGate.HTTP_REJECTED_OTHER


failure_gate = SmokeGate.IDENTITY_CONFIGURATION
closed_diagnostic_ids: tuple[str, ...] = ()
try:
    from dotenv import dotenv_values

    sys.path.insert(0, os.environ["BOYI_DEPLOYED_ROOT"])
    from shared.service_identity import (
        build_console_identity_headers,
        validate_service_identity_secrets,
    )

    values = dotenv_values(os.environ["BOYI_IDENTITY_ENV_FILE"])
    internal_token = str(values.get("AGENT_INTERNAL_API_TOKEN") or "")
    signing_secret = str(values.get("CONSOLE_AGENT_SIGNING_SECRET") or "")
    validate_service_identity_secrets(
        internal_api_token=internal_token,
        console_signing_secret=signing_secret,
    )
    request_target = "/internal/v1/health"
    for attempt in range(3):
        headers = build_console_identity_headers(
            secret=signing_secret,
            method="GET",
            request_target=request_target,
            body=b"",
            principal={
                "actor_type": "console_admin",
                "actor_id": "release-identity-probe",
                "roles": ["admin"],
                "display_name": "Release identity probe",
                "authenticated_by": "mysql_admin_session",
            },
            nonce=secrets.token_urlsafe(24),
        )
        headers["X-Agent-Internal-Token"] = internal_token
        request = Request(
            f"http://127.0.0.1:9000{request_target}",
            headers=headers,
            method="GET",
        )
        failure_gate = SmokeGate.TRANSPORT_UNAVAILABLE
        try:
            with urlopen(request, timeout=10) as response:
                response_status = response.status
                response_body = response.read()
            break
        except HTTPError as exc:
            failure_gate = http_failure_gate(exc.code)
            raise
        except URLError:
            if attempt == 2:
                raise
    else:
        raise RuntimeError("signed health probe did not return a response")
    if response_status != 200:
        failure_gate = http_failure_gate(response_status)
        raise RuntimeError("signed health probe returned an unexpected status")
    failure_gate = SmokeGate.INVALID_JSON_OR_SHAPE
    payload = json.loads(response_body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("signed health probe response is not an object")
    failure_gate = SmokeGate.STATUS_NOT_OK
    if payload.get("ok") is not True:
        raise RuntimeError("signed health probe was rejected")
    data = payload.get("data")
    if not isinstance(data, dict):
        failure_gate = SmokeGate.INVALID_JSON_OR_SHAPE
        raise RuntimeError("signed health probe data is not an object")
    failure_gate = SmokeGate.RELEASE_SHA_MISMATCH
    if data.get("release_sha") != os.environ["BOYI_RELEASE_SHA"]:
        raise RuntimeError("signed health probe release SHA mismatched")
    components = data.get("components") if isinstance(data, dict) else None
    scheduler = components.get("scheduler") if isinstance(components, dict) else None
    workflow_runner = (
        components.get("workflow_runner") if isinstance(components, dict) else None
    )
    automation_plugins = (
        components.get("automation_plugins") if isinstance(components, dict) else None
    )
    automation_workers = (
        components.get("automation_workers") if isinstance(components, dict) else None
    )
    failure_gate = SmokeGate.SCHEDULER_STATE
    if not isinstance(scheduler, dict) or scheduler.get("state") != "paused":
        raise RuntimeError("scheduler was not held for release validation")
    failure_gate = SmokeGate.SCHEDULER_HOLD
    if scheduler.get("release_hold") is not True:
        raise RuntimeError("scheduler release marker was not active")
    failure_gate = SmokeGate.RUNNER_STATE
    if not isinstance(workflow_runner, dict) or workflow_runner.get("state") != "held":
        raise RuntimeError("workflow runner was not held for release validation")
    failure_gate = SmokeGate.RUNNER_HOLD
    if workflow_runner.get("release_hold") is not True:
        raise RuntimeError("workflow runner release hold was not active")
    failure_gate = SmokeGate.RUNNER_ACTIVE
    if workflow_runner.get("active_runs") != 0:
        raise RuntimeError("workflow runner still had active Runs")
    failure_gate = SmokeGate.PLUGIN_BROKER
    if (
        not isinstance(automation_plugins, dict)
        or not isinstance(automation_plugins.get("broker"), dict)
        or automation_plugins["broker"].get("state") != "running"
    ):
        raise RuntimeError("automation plugin broker is not release-ready")
    if os.environ.get("BOYI_SERVICE_IDENTITY_SMOKE_SCOPE") == "recovery_transport":
        print("service_identity_smoke=recovery_transport_ok")
        raise SystemExit(0)
    smoke_scope = os.environ.get("BOYI_SERVICE_IDENTITY_SMOKE_SCOPE")
    scoped_plugin_catalog = automation_plugins.get("catalog")
    scoped_plugin_generations = automation_plugins.get("generations")
    scoped_plugin_ok = automation_plugins.get("ok")
    if smoke_scope == "delivery_unknown_write_quarantine":
        failure_gate = SmokeGate.PLUGIN_UNAFFECTED_RELEASE_SHAPE
        raw_catalog = scoped_plugin_catalog
        raw_generations = scoped_plugin_generations
        readiness = automation_plugins.get("unaffected_release")
        expected_unaffected_ids = tuple(
            sorted(_CLOSED_AUTOMATION_IDS - _QUARANTINED_AUTOMATION_IDS)
        )
        expected_quarantined_ids = sorted(_QUARANTINED_AUTOMATION_IDS)
        if (
            automation_plugins.get("ok") is not False
            or not isinstance(raw_catalog, dict)
            or raw_catalog.get("ok") is not False
            or raw_catalog.get("unstable_generations")
            != expected_quarantined_ids
            or not isinstance(raw_generations, dict)
            or raw_generations.get("healthy") is not False
            or not isinstance(readiness, dict)
            or readiness.get("ok") is not True
            or readiness.get("quarantined_automation_ids")
            != expected_quarantined_ids
            or readiness.get("expected_automation_ids")
            != list(expected_unaffected_ids)
            or readiness.get("expected_project_count")
            != len(expected_unaffected_ids)
            or not isinstance(readiness.get("catalog"), dict)
            or not isinstance(readiness.get("generations"), dict)
        ):
            raise RuntimeError("unknown-write quarantine readiness shape is invalid")
        scoped_plugin_catalog = readiness["catalog"]
        scoped_plugin_generations = readiness["generations"]
        scoped_plugin_ok = readiness["ok"]
    failure_gate = SmokeGate.PLUGIN_CATALOG_AGGREGATE_OR_SHAPE
    plugin_catalog = scoped_plugin_catalog
    if not isinstance(plugin_catalog, dict):
        raise RuntimeError("automation plugin catalog health shape is invalid")
    catalog_failure_fields = (
        (
            SmokeGate.PLUGIN_CATALOG_UNSUPPORTED,
            "unsupported_automation_ids",
        ),
        (
            SmokeGate.PLUGIN_CATALOG_ENABLED_BUILTIN,
            "enabled_builtin_release",
        ),
        (
            SmokeGate.PLUGIN_CATALOG_INVALID_TRUST,
            "invalid_enabled_trust",
        ),
        (
            SmokeGate.PLUGIN_CATALOG_UNSTABLE_GENERATIONS,
            "unstable_generations",
        ),
        (
            SmokeGate.PLUGIN_CATALOG_INVALID_RUNTIME,
            "invalid_enabled_runtime",
        ),
    )
    for catalog_gate, field_name in catalog_failure_fields:
        field_value = plugin_catalog.get(field_name)
        if not isinstance(field_value, list):
            failure_gate = SmokeGate.PLUGIN_CATALOG_AGGREGATE_OR_SHAPE
            raise RuntimeError("automation plugin catalog health shape is invalid")
        if field_value:
            if catalog_gate is SmokeGate.PLUGIN_CATALOG_UNSTABLE_GENERATIONS:
                closed_diagnostic_ids = tuple(
                    sorted(
                        {
                            value
                            for value in field_value
                            if isinstance(value, str)
                            and value in _CLOSED_AUTOMATION_IDS
                        }
                    )
                )
            failure_gate = catalog_gate
            raise RuntimeError("automation plugin catalog is not release-ready")
    failure_gate = SmokeGate.PLUGIN_CATALOG_AGGREGATE_OR_SHAPE
    if plugin_catalog.get("ok") is not True:
        raise RuntimeError("automation plugin catalog aggregate is not release-ready")
    failure_gate = SmokeGate.PLUGIN_GENERATIONS
    if (
        not isinstance(scoped_plugin_generations, dict)
        or scoped_plugin_generations.get("healthy") is not True
    ):
        raise RuntimeError("automation plugin generations are not release-ready")
    failure_gate = SmokeGate.PLUGIN_AGGREGATE
    if (
        (
            smoke_scope != "delivery_unknown_write_quarantine"
            and automation_plugins.get("ok") is not True
        )
        or (
            smoke_scope == "delivery_unknown_write_quarantine"
            and scoped_plugin_ok is not True
        )
    ):
        raise RuntimeError("automation plugin runtime is not release-ready")
    failure_gate = SmokeGate.WORKER
    if (
        not isinstance(automation_workers, dict)
        or automation_workers.get("enabled") is not False
        or automation_workers.get("state") != "disabled"
        or automation_workers.get("release_hold") is not False
        or int(automation_workers.get("active_jobs") or 0) != 0
    ):
        raise RuntimeError("deferred automation Worker was unexpectedly active")
    bootstrap = (
        components.get("scheduled_task_approval_bootstrap")
        if isinstance(components, dict)
        else None
    )
    failure_gate = SmokeGate.BOOTSTRAP_SHAPE
    if not isinstance(bootstrap, dict):
        raise RuntimeError("scheduled approval bootstrap health is missing")
    reviewed = int(bootstrap.get("reviewed_candidates", -1))
    created = int(bootstrap.get("created", -1))
    existing = int(bootstrap.get("already_present", -1))
    configured = int(bootstrap.get("explicitly_configured", -1))
    rejected = int(bootstrap.get("rejected", -1))
    completed = int(bootstrap.get("completed", -1))
    if min(reviewed, created, existing, configured, rejected, completed) < 0:
        raise RuntimeError("scheduled approval bootstrap health is invalid")
    failure_gate = SmokeGate.BOOTSTRAP_INCOMPLETE
    if completed != 1:
        raise RuntimeError("scheduled approval bootstrap one-time evaluation is incomplete")
    failure_gate = SmokeGate.BOOTSTRAP_REJECTED
    if rejected != 0 or created + existing + configured != reviewed:
        raise RuntimeError("scheduled approval bootstrap did not preserve reviewed tasks")
except SystemExit:
    raise
except Exception:
    diagnostic = (
        " diagnostic_automation_ids=" + ",".join(closed_diagnostic_ids)
        if closed_diagnostic_ids
        else ""
    )
    print(
        f"service_identity_smoke=failed reason={failure_gate.value}{diagnostic}",
        file=sys.stderr,
    )
    raise SystemExit(1)
print("service_identity_smoke=ok")
PY
}

recover_known_arrival_stats_unknown_write() {
  # This is intentionally limited to the named, separately authorized
  # incidents. It never discovers or retries another unknown write.
  local run_id="${1:?known recovery Run is required}"
  case "${run_id}" in
    fb077840-a2d0-4e7f-8089-f68c104ab544|71510af3-fcf1-461b-9c2e-152665f32f98|2a86ba4b-5c63-4bf2-93de-f61372d18274) ;;
    *)
      echo "arrival_stats_unknown_write_recovery=failed reason=scope_invalid" >&2
      return 1
      ;;
  esac
  local console_python="${PYTHON_BINS[console]}"
  [[ -x "${console_python}" && -f "${IDENTITY_ENV_FILE}" ]] || {
    echo "arrival_stats_unknown_write_recovery=failed reason=runtime_unavailable" >&2
    return 1
  }

  BOYI_KNOWN_RECOVERY_RUN_ID="${run_id}" \
    BOYI_IDENTITY_ENV_FILE="${IDENTITY_ENV_FILE}" \
    BOYI_DEPLOYED_ROOT="/home/boyce" \
    "${console_python}" - <<'PY'
import json
import os
import secrets
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dotenv import dotenv_values

sys.path.insert(0, os.environ["BOYI_DEPLOYED_ROOT"])
from shared.service_identity import (
    build_console_identity_headers,
    validate_service_identity_secrets,
)


RUN_ID = os.environ["BOYI_KNOWN_RECOVERY_RUN_ID"]
AUTOMATION_ID = "arrival_stats"
REQUEST_TARGET = (
    f"/internal/v1/automation/instances/{AUTOMATION_ID}/generation/"
    "recover-not-applied"
)
READBACK = {
    "arrival_stat_runs": 0,
    "arrival_stat_items": 0,
    "feishu_rows_created": 0,
}

try:
    values = dotenv_values(os.environ["BOYI_IDENTITY_ENV_FILE"])
    internal_token = str(values.get("AGENT_INTERNAL_API_TOKEN") or "")
    signing_secret = str(values.get("CONSOLE_AGENT_SIGNING_SECRET") or "")
    validate_service_identity_secrets(
        internal_api_token=internal_token,
        console_signing_secret=signing_secret,
    )
    body = json.dumps(
        {"readback": READBACK, "request_id": RUN_ID},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    headers = build_console_identity_headers(
        secret=signing_secret,
        method="POST",
        request_target=REQUEST_TARGET,
        body=body,
        principal={
            "actor_type": "console_admin",
            "actor_id": "release-arrival-stats-recovery",
            "roles": ["admin", "super_admin"],
            "display_name": "Release arrival statistics recovery",
            "authenticated_by": "mysql_admin_session",
        },
        nonce=secrets.token_urlsafe(24),
    )
    headers["X-Agent-Internal-Token"] = internal_token
    headers["Content-Type"] = "application/json"
    request = Request(
        f"http://127.0.0.1:9000{REQUEST_TARGET}",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            response_status = response.status
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError("managed recovery request was rejected") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if (
        response_status != 200
        or payload.get("ok") is not True
        or not isinstance(data, dict)
        or data.get("automation_id") != AUTOMATION_ID
        or data.get("recovery_status") != "NOT_APPLIED"
    ):
        raise RuntimeError("managed recovery response was invalid")
except Exception:
    print(
        "arrival_stats_unknown_write_recovery=failed reason=managed_recovery_rejected",
        file=sys.stderr,
    )
    raise SystemExit(1)
print(
    "arrival_stats_unknown_write_recovery=ok "
    f"run_id={RUN_ID} outcome=NOT_APPLIED"
)
PY
}

diagnose_arrival_stats_generation() {
  local console_python="${PYTHON_BINS[console]}"
  [[ -x "${console_python}" && -f "${IDENTITY_ENV_FILE}" ]] || return 1
  local diagnostic
  diagnostic="$(
    BOYI_IDENTITY_ENV_FILE="${IDENTITY_ENV_FILE}" BOYI_DEPLOYED_ROOT="/home/boyce" "${console_python}" - <<'PY'
import json
import os
import secrets
import sys
from urllib.request import Request, urlopen

from dotenv import dotenv_values

sys.path.insert(0, os.environ["BOYI_DEPLOYED_ROOT"])

from shared.service_identity import (  # noqa: E402
    build_console_identity_headers,
    validate_service_identity_secrets,
)


AUTOMATION_ID = "arrival_stats"
PLUGIN_ID = "sync_arrival_stats"
EXPECTED_FIELDS = {
    "automation_id",
    "plugin_id",
    "target_generation",
    "committed_generation",
    "reconcile_state",
    "lease_id",
    "lease_generation",
    "lease_outcome",
    "lease_acquired_at",
    "lease_expires_at",
    "lease_released_at",
}

try:
    values = dotenv_values(os.environ["BOYI_IDENTITY_ENV_FILE"])
    internal_token = str(values.get("AGENT_INTERNAL_API_TOKEN") or "")
    signing_secret = str(values.get("CONSOLE_AGENT_SIGNING_SECRET") or "")
    validate_service_identity_secrets(
        internal_api_token=internal_token,
        console_signing_secret=signing_secret,
    )
    request_target = (
        "/internal/v1/automation/instances/arrival_stats/generation/diagnostic"
    )
    headers = build_console_identity_headers(
        secret=signing_secret,
        method="GET",
        request_target=request_target,
        body=b"",
        principal={
            "actor_type": "console_admin",
            "actor_id": "release-arrival-stats-diagnostic",
            "roles": ["admin", "super_admin"],
            "display_name": "Release arrival statistics diagnostic",
            "authenticated_by": "mysql_admin_session",
        },
        nonce=secrets.token_urlsafe(24),
    )
    headers["X-Agent-Internal-Token"] = internal_token
    with urlopen(
        Request(
            "http://127.0.0.1:9000" + request_target,
            headers=headers,
            method="GET",
        ),
        timeout=20,
    ) as response:
        payload = json.loads(response.read().decode("utf-8"))
    data = payload.get("data") if isinstance(payload, dict) else None
    if (
        response.status != 200
        or payload.get("ok") is not True
        or not isinstance(data, dict)
        or set(data) != EXPECTED_FIELDS
        or data.get("automation_id") != AUTOMATION_ID
        or data.get("plugin_id") != PLUGIN_ID
    ):
        raise ValueError("diagnostic response shape is invalid")
    blocked = (
        type(data.get("target_generation")) is int
        and data.get("target_generation") > 0
        and type(data.get("committed_generation")) is int
        and data.get("committed_generation") == data.get("target_generation")
        and data.get("reconcile_state") == "BLOCKED_UNKNOWN_WRITE"
        and isinstance(data.get("lease_id"), str)
        and bool(data.get("lease_id"))
        and type(data.get("lease_generation")) is int
        and data.get("lease_generation") == data.get("target_generation")
        and data.get("lease_outcome") == "WRITE_OUTCOME_UNKNOWN"
        and isinstance(data.get("lease_acquired_at"), str)
        and bool(data.get("lease_acquired_at"))
        and isinstance(data.get("lease_expires_at"), str)
        and bool(data.get("lease_expires_at"))
        and isinstance(data.get("lease_released_at"), str)
        and bool(data.get("lease_released_at"))
    )
    normal = (
        type(data.get("target_generation")) is int
        and data.get("target_generation") > 0
        and type(data.get("committed_generation")) is int
        and data.get("committed_generation") == data.get("target_generation")
        and data.get("reconcile_state") == "STABLE"
        and data.get("lease_id") == ""
        and data.get("lease_generation") is None
        and data.get("lease_outcome") == "NO_BLOCKED_WRITE_LEASE"
        and data.get("lease_acquired_at") is None
        and data.get("lease_expires_at") is None
        and data.get("lease_released_at") is None
    )
    if blocked:
        print(
            "arrival_stats_generation_diagnostic=blocked "
            f"lease_id={data['lease_id']} "
            f"generation={data['lease_generation']} "
            f"acquired_at={data['lease_acquired_at']} "
            f"expires_at={data['lease_expires_at']} "
            f"released_at={data['lease_released_at']}"
        )
    elif normal:
        print("arrival_stats_generation_diagnostic=normal")
    else:
        raise ValueError("arrival statistics diagnostic is neither normal nor blocked")
except Exception:
    print("arrival_stats_generation_diagnostic=failed", file=sys.stderr)
    raise SystemExit(1)
PY
  )" || return 1
  case "${diagnostic}" in
    arrival_stats_generation_diagnostic=blocked\ *)
      ARRIVAL_STATS_UNKNOWN_WRITE_BLOCKED=1
      ;;
    arrival_stats_generation_diagnostic=normal)
      ARRIVAL_STATS_UNKNOWN_WRITE_BLOCKED=0
      ;;
    *)
      echo "arrival_stats_generation_diagnostic=failed" >&2
      return 1
      ;;
  esac
  printf '%s\n' "${diagnostic}"
}

diagnose_delivery_status_generation() {
  local console_python="${PYTHON_BINS[console]}"
  [[ -x "${console_python}" && -f "${IDENTITY_ENV_FILE}" ]] || return 1
  local diagnostic
  diagnostic="$(
    BOYI_IDENTITY_ENV_FILE="${IDENTITY_ENV_FILE}" BOYI_DEPLOYED_ROOT="/home/boyce" "${console_python}" - <<'PY'
import json
import os
import secrets
import sys
from urllib.request import Request, urlopen

from dotenv import dotenv_values

sys.path.insert(0, os.environ["BOYI_DEPLOYED_ROOT"])

from shared.service_identity import (  # noqa: E402
    build_console_identity_headers,
    validate_service_identity_secrets,
)


AUTOMATION_ID = "delivery_status"
GENERATION = 1
LEASE_ID = "9918420e-b5c1-41c7-a4ee-543e131272be"
QUARANTINE_STATUS = "QUARANTINED_UNKNOWN_WRITE"
EXPECTED_FIELDS = {
    "automation_id",
    "target_generation",
    "committed_generation",
    "reconcile_state",
    "lease_reason",
    "lease_id",
    "lease_generation",
    "quarantine_status",
}

try:
    values = dotenv_values(os.environ["BOYI_IDENTITY_ENV_FILE"])
    internal_token = str(values.get("AGENT_INTERNAL_API_TOKEN") or "")
    signing_secret = str(values.get("CONSOLE_AGENT_SIGNING_SECRET") or "")
    validate_service_identity_secrets(
        internal_api_token=internal_token,
        console_signing_secret=signing_secret,
    )
    request_target = "/internal/v1/automation/instances/delivery_status/generation/diagnostic"
    headers = build_console_identity_headers(
        secret=signing_secret,
        method="GET",
        request_target=request_target,
        body=b"",
        principal={
            "actor_type": "console_admin",
            "actor_id": "release-delivery-diagnostic",
            "roles": ["admin", "super_admin"],
            "display_name": "Release delivery diagnostic",
            "authenticated_by": "mysql_admin_session",
        },
        nonce=secrets.token_urlsafe(24),
    )
    headers["X-Agent-Internal-Token"] = internal_token
    with urlopen(
        Request(
            "http://127.0.0.1:9000" + request_target,
            headers=headers,
            method="GET",
        ),
        timeout=20,
    ) as response:
        payload = json.loads(response.read().decode("utf-8"))
    data = payload.get("data") if isinstance(payload, dict) else None
    if (
        response.status != 200
        or payload.get("ok") is not True
        or not isinstance(data, dict)
        or set(data) != EXPECTED_FIELDS
        or data.get("automation_id") != AUTOMATION_ID
    ):
        raise ValueError("diagnostic response shape is invalid")
    exact_quarantine = (
        type(data.get("target_generation")) is int
        and data.get("target_generation") == GENERATION
        and type(data.get("committed_generation")) is int
        and data.get("committed_generation") == GENERATION
        and data.get("reconcile_state") == "BLOCKED_UNKNOWN_WRITE"
        and data.get("lease_reason") == "WRITE_OUTCOME_UNKNOWN"
        and data.get("lease_id") == LEASE_ID
        and type(data.get("lease_generation")) is int
        and data.get("lease_generation") == GENERATION
        and data.get("quarantine_status") == QUARANTINE_STATUS
    )
    normal = (
        type(data.get("target_generation")) is int
        and data.get("target_generation") > 0
        and type(data.get("committed_generation")) is int
        and data.get("committed_generation") == data.get("target_generation")
        and data.get("reconcile_state") == "STABLE"
        and data.get("lease_reason") == "NO_BLOCKED_WRITE_LEASE"
        and data.get("lease_id") == ""
        and data.get("lease_generation") is None
        and data.get("quarantine_status") is None
    )
    if exact_quarantine:
        print("delivery_status_generation_diagnostic=quarantined")
    elif normal:
        print("delivery_status_generation_diagnostic=normal")
    else:
        raise ValueError("delivery diagnostic is neither normal nor the audited incident")
except Exception:
    print("delivery_status_generation_diagnostic=failed", file=sys.stderr)
    raise SystemExit(1)
PY
  )" || return 1
  case "${diagnostic}" in
    delivery_status_generation_diagnostic=quarantined)
      DELIVERY_STATUS_UNKNOWN_WRITE_QUARANTINED=1
      ;;
    delivery_status_generation_diagnostic=normal)
      DELIVERY_STATUS_UNKNOWN_WRITE_QUARANTINED=0
      ;;
    *)
      echo "delivery_status_generation_diagnostic=failed" >&2
      return 1
      ;;
  esac
  printf '%s\n' "${diagnostic}"
}

check_post_restart_release_gates() {
  RELEASE_STAGE="diagnose_arrival_stats_generation"
  diagnose_arrival_stats_generation || return 1
  RELEASE_STAGE="diagnose_delivery_status_generation"
  diagnose_delivery_status_generation || return 1
  if [[ "${DELIVERY_STATUS_UNKNOWN_WRITE_QUARANTINED}" == "1" ]]; then
    RELEASE_STAGE="check_service_identity_delivery_unknown_write_quarantine"
    check_service_identity_smoke delivery_unknown_write_quarantine || return 1
  elif [[ "${KNOWN_ARRIVAL_STATS_RECOVERY}" == "1" || "${KNOWN_ARRIVAL_STATS_AUTH_FAILURE_RECOVERY}" == "1" || "${KNOWN_ARRIVAL_STATS_PREWRITE_FAILURE_RECOVERY}" == "1" ]]; then
    RELEASE_STAGE="check_service_identity_recovery_transport"
    check_service_identity_smoke recovery_transport || return 1
  else
    RELEASE_STAGE="check_service_identity_smoke"
    check_service_identity_smoke || return 1
  fi
  if [[ "${KNOWN_ARRIVAL_STATS_RECOVERY}" == "1" ]]; then
    RELEASE_STAGE="recover_known_arrival_stats_unknown_write"
    recover_known_arrival_stats_unknown_write "fb077840-a2d0-4e7f-8089-f68c104ab544" || return 1
  fi
  if [[ "${KNOWN_ARRIVAL_STATS_AUTH_FAILURE_RECOVERY}" == "1" ]]; then
    RELEASE_STAGE="recover_known_arrival_stats_auth_failure"
    recover_known_arrival_stats_unknown_write "71510af3-fcf1-461b-9c2e-152665f32f98" || return 1
  fi
  if [[ "${KNOWN_ARRIVAL_STATS_PREWRITE_FAILURE_RECOVERY}" == "1" ]]; then
    RELEASE_STAGE="recover_known_arrival_stats_prewrite_failure"
    recover_known_arrival_stats_unknown_write "2a86ba4b-5c63-4bf2-93de-f61372d18274" || return 1
  fi
  if [[ "${KNOWN_ARRIVAL_STATS_RECOVERY}" == "1" || "${KNOWN_ARRIVAL_STATS_AUTH_FAILURE_RECOVERY}" == "1" || "${KNOWN_ARRIVAL_STATS_PREWRITE_FAILURE_RECOVERY}" == "1" ]]; then
    if [[ "${DELIVERY_STATUS_UNKNOWN_WRITE_QUARANTINED}" == "1" ]]; then
      RELEASE_STAGE="check_service_identity_delivery_unknown_write_quarantine"
      check_service_identity_smoke delivery_unknown_write_quarantine || return 1
    else
      RELEASE_STAGE="check_service_identity_smoke"
      check_service_identity_smoke || return 1
    fi
  fi
  RELEASE_STAGE="check_control_plane_release_manifest"
  check_control_plane_release_manifest || return 1
}

activate_scheduler_after_release() {
  local console_python="${PYTHON_BINS[console]}"
  [[ -x "${console_python}" && -f "${IDENTITY_ENV_FILE}" ]] || {
    echo "scheduler_release_activation=failed reason=runtime_unavailable" >&2
    return 1
  }

  BOYI_IDENTITY_ENV_FILE="${IDENTITY_ENV_FILE}" \
    BOYI_DEPLOYED_ROOT="/home/boyce" \
    "${console_python}" - <<'PY'
import json
import os
import secrets
import sys
from urllib.request import Request, urlopen

from dotenv import dotenv_values

sys.path.insert(0, os.environ["BOYI_DEPLOYED_ROOT"])
from shared.service_identity import (
    build_console_identity_headers,
    validate_service_identity_secrets,
)


try:
    values = dotenv_values(os.environ["BOYI_IDENTITY_ENV_FILE"])
    internal_token = str(values.get("AGENT_INTERNAL_API_TOKEN") or "")
    signing_secret = str(values.get("CONSOLE_AGENT_SIGNING_SECRET") or "")
    validate_service_identity_secrets(
        internal_api_token=internal_token,
        console_signing_secret=signing_secret,
    )
    request_target = "/internal/v1/admin/scheduler/activate-after-release"
    principal = {
        "actor_type": "console_admin",
        "actor_id": "release-scheduler-activation",
        "roles": ["admin"],
        "display_name": "Release scheduler activation",
        "authenticated_by": "mysql_admin_session",
    }
    payload = None
    response_status = None
    last_error = None
    for _attempt in range(3):
        headers = build_console_identity_headers(
            secret=signing_secret,
            method="POST",
            request_target=request_target,
            body=b"",
            principal=principal,
            nonce=secrets.token_urlsafe(24),
        )
        headers["X-Agent-Internal-Token"] = internal_token
        request = Request(
            f"http://127.0.0.1:9000{request_target}",
            data=b"",
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=10) as response:
                response_status = response.status
                payload = json.loads(response.read().decode("utf-8"))
            break
        except Exception as exc:
            last_error = exc
    if payload is None:
        raise RuntimeError("signed activation did not return a response") from last_error
    data = payload.get("data") if isinstance(payload, dict) else None
    if (
        response_status != 200
        or payload.get("ok") is not True
        or not isinstance(data, dict)
        or data.get("state") != "running"
        or data.get("release_hold") is not False
        or not isinstance(data.get("workflow_runner"), dict)
        or data["workflow_runner"].get("state") != "running"
        or data["workflow_runner"].get("release_hold") is not False
        or not isinstance(data.get("automation_plugins"), dict)
        or data["automation_plugins"].get("ok") is not True
        or not isinstance(data.get("automation_workers"), dict)
        or data["automation_workers"].get("enabled") is not False
        or data["automation_workers"].get("state") != "disabled"
        or data["automation_workers"].get("release_hold") is not False
        or int(data["automation_workers"].get("active_jobs") or 0) != 0
    ):
        raise RuntimeError("release runtime activation was not confirmed")
except Exception:
    print("release_runtime_activation=failed reason=signed_activation_rejected", file=sys.stderr)
    raise SystemExit(1)
print("release_runtime_activation=ok")
PY
}

restore_managed_release_state() {
  local restore_status=0
  local scope root new_manifest backup_scope relative target

  # The rollback unit and its mandatory release environment are one state.
  # Recheck before restoring or deleting anything so a drifted backup cannot
  # turn a recoverable failure into a service that systemd cannot start.
  validate_automation_plugin_runtime_rollback_snapshot || return 1

  if [[ "${FIRST_PARTY_PLUGIN_INSTALL_ATTEMPTED}" == "1" ]]; then
    restore_automation_plugin_installations || {
      echo "Failed to restore automation plugin installations" >&2
      restore_status=1
    }
    restore_first_party_plugin_artifacts || {
      echo "Failed to restore first-party plugin artifacts" >&2
      restore_status=1
    }
  fi
  restore_legacy_finance_etl || {
    echo "Failed to restore legacy finance ETL rollback data" >&2
    restore_status=1
  }
  for scope in "${SCOPES[@]}"; do
    root="${ROOTS[$scope]}"
    new_manifest="${STAGE_ROOT}/_manifests/${scope}.txt"
    backup_scope="${BACKUP_TREE}/${scope}"
    if [[ ! -f "${new_manifest}" ]]; then
      echo "Missing rollback manifest for ${scope}" >&2
      restore_status=1
      continue
    fi
    while IFS= read -r relative || [[ -n "${relative}" ]]; do
      if ! safe_relative_path "${relative}"; then
        echo "Unsafe rollback manifest path in ${scope}: ${relative}" >&2
        restore_status=1
        continue
      fi
      rm -f -- "${root}/${relative}" || restore_status=1
    done <"${new_manifest}"
    if [[ -d "${backup_scope}" ]]; then
      mkdir -p "${root}" || restore_status=1
      cp -a "${backup_scope}/." "${root}/" || restore_status=1
    fi
    if [[ -f "${BACKUP_DIR}/${scope}.manifest" ]]; then
      cp -a "${BACKUP_DIR}/${scope}.manifest" "${root}/.deploy-source-manifest" || \
        restore_status=1
    elif [[ -f "${BACKUP_DIR}/${scope}.manifest.absent" ]]; then
      rm -f -- "${root}/.deploy-source-manifest" || restore_status=1
    else
      echo "Missing previous deployment manifest state for ${scope}" >&2
      restore_status=1
    fi
  done

  if [[ -f "${BACKUP_DIR}/release_sha" ]]; then
    mkdir -p "${ROOTS[agent]}/runtime" || restore_status=1
    cp -a "${BACKUP_DIR}/release_sha" "${ROOTS[agent]}/runtime/release_sha" || \
      restore_status=1
  elif [[ -f "${BACKUP_DIR}/release_sha.absent" ]]; then
    rm -f -- "${ROOTS[agent]}/runtime/release_sha" || restore_status=1
  else
    echo "Missing previous release SHA state" >&2
    restore_status=1
  fi

  if [[ -f "${BACKUP_DIR}/automation_plugin_release.env" ]]; then
    mkdir -p "$(dirname "${PLUGIN_RUNTIME_ENV_FILE}")" || restore_status=1
    cp -a "${BACKUP_DIR}/automation_plugin_release.env" "${PLUGIN_RUNTIME_ENV_FILE}" || \
      restore_status=1
  elif [[ -f "${BACKUP_DIR}/automation_plugin_release.env.absent" ]]; then
    rm -f -- "${PLUGIN_RUNTIME_ENV_FILE}" || restore_status=1
  else
    echo "Missing previous automation plugin runtime environment state" >&2
    restore_status=1
  fi

  for target in "${REQUESTED_TARGETS[@]}"; do
    if [[ ! -f "${BACKUP_DIR}/${target}.service" ]]; then
      echo "Missing rollback unit for ${target}" >&2
      restore_status=1
      continue
    fi
    sudo install -m 0644 "${BACKUP_DIR}/${target}.service" "${UNIT_PATHS[$target]}" || \
      restore_status=1
  done
  sudo systemctl daemon-reload || restore_status=1
  return "${restore_status}"
}

rollback() {
  local exit_code=$?
  local failed_command="${BASH_COMMAND}"
  local failed_line="${BASH_LINENO[0]:-unknown}"
  local rollback_status=0
  local services_stopped=1
  trap - ERR
  set +e
  [[ "${exit_code}" -ne 0 ]] || exit_code=1
  echo "release_error stage=${RELEASE_STAGE} line=${failed_line} command=${failed_command}" >&2
  if [[ "${MUTATION_STARTED}" == "1" ]]; then
    echo "Release failed; stopping both runtime services before rollback" >&2
    if ! stop_runtime_services_for_rollback; then
      rollback_status=1
      services_stopped=0
    fi
    if [[ "${services_stopped}" == "1" ]]; then
      echo "Restoring managed release state" >&2
      if [[ "${AUTOMATION_PROJECT_AUTHORIZATION_PENDING_AT_APPLY}" == "1" && \
        "${MIGRATIONS_ATTEMPTED}" == "1" ]]; then
        restore_automation_project_authorization_data || rollback_status=1
      fi
      if [[ "${CONTROL_PLANE_POLICY_BOOTSTRAP_ABSENT_BEFORE_RELEASE}" == "1" && \
        "${NEW_RUNTIME_START_ATTEMPTED}" == "1" ]]; then
        restore_control_plane_policy_bootstrap_data || rollback_status=1
      fi
      if [[ "${SCHEDULED_TASK_CONTRACT_UPGRADE_PENDING_AT_APPLY}" == "1" && \
        "${MIGRATIONS_ATTEMPTED}" == "1" ]]; then
        restore_scheduled_task_contract_upgrade_data || rollback_status=1
      fi
      if [[ "${DAILY_SIGN_SINGLE_TMS_PENDING_AT_APPLY}" == "1" && \
        "${MIGRATIONS_ATTEMPTED}" == "1" ]]; then
        restore_daily_sign_single_tms_data || rollback_status=1
      fi
      if [[ "${CONTROL_PLANE_TASK_CUTOVER_PENDING_AT_APPLY}" == "1" && \
        "${MIGRATIONS_ATTEMPTED}" == "1" ]]; then
        restore_control_plane_task_cutover_data || rollback_status=1
      fi
      if [[ "${VENV_ACTIVATED}" == "1" ]] && ! restore_virtualenvs; then
        rollback_status=1
      fi
      restore_managed_release_state || rollback_status=1
      verify_runtime_virtualenvs || rollback_status=1
      if [[ "${rollback_status}" == "0" ]]; then
        if clear_scheduler_release_hold_for_rollback; then
          restart_runtime_services_for_rollback || rollback_status=1
        else
          rollback_status=1
          echo "Rollback restart skipped because the scheduler release hold could not be cleared" >&2
        fi
      else
        echo "Rollback activation skipped because managed state restore did not complete" >&2
      fi
    else
      echo "Rollback restore skipped because a runtime service could not be stopped" >&2
    fi

    if [[ "${rollback_status}" != "0" ]]; then
      echo "rollback_incomplete stage_root=${STAGE_ROOT} recovery_material_preserved=1" >&2
      exit "${exit_code:-1}"
    fi

    if ! remove_new_virtualenvs; then
      echo "rollback_incomplete stage_root=${STAGE_ROOT} recovery_material_preserved=1" >&2
      exit "${exit_code:-1}"
    fi
    cleanup_failed_release_stage || {
      echo "rollback_cleanup_incomplete stage_root=${STAGE_ROOT} recovery_material_state=unknown verify_required=1" >&2
      exit "${exit_code:-1}"
    }
  else
    if ! clear_scheduler_release_hold_for_rollback; then
      echo "rollback_incomplete stage_root=${STAGE_ROOT} recovery_material_preserved=1" >&2
      exit "${exit_code:-1}"
    fi
    if ! remove_new_virtualenvs; then
      echo "release_cleanup_incomplete stage_root=${STAGE_ROOT}" >&2
      exit "${exit_code:-1}"
    fi
    cleanup_failed_release_stage || {
      echo "release_cleanup_incomplete stage_root=${STAGE_ROOT} recovery_material_state=unknown verify_required=1" >&2
      exit "${exit_code:-1}"
    }
  fi
  exit "${exit_code}"
}

run_release() {
  RELEASE_STAGE="acquire_release_lock"
  acquire_release_lock
  trap rollback ERR
  RELEASE_STAGE="validate_environment"
  validate_environment
  if [[ "${WINDOWS_WORKER_RELEASE_ENABLED}" == "1" ]]; then
    RELEASE_STAGE="preflight_worker_mtls_proxy"
    preflight_worker_mtls_proxy
  else
    echo "windows_worker_release_scope=disabled"
  fi
  RELEASE_STAGE="preflight_service_identity_configuration"
  preflight_service_identity_configuration
  RELEASE_STAGE="preflight_control_plane_task_cutover"
  preflight_control_plane_task_cutover
  RELEASE_STAGE="preflight_automation_project_scheduled_task_identities"
  preflight_automation_project_scheduled_task_identities
  RELEASE_STAGE="preflight_automation_project_required_resources"
  preflight_automation_project_required_resources
  if [[ "${EMERGENCY_SCHEDULED_WINDOW_OVERRIDE}" == "1" ]]; then
    echo "emergency_scheduled_window_override=authorized emergency_user_authorized=true scope=scheduled_write_window_proximity_only residual_race_user_authorized=true"
    RELEASE_STAGE="preflight_running_protected_writes_before_emergency_window_override"
    preflight_running_protected_writes
  fi
  RELEASE_STAGE="preflight_scheduled_write_window"
  preflight_scheduled_write_window
  RELEASE_STAGE="backup_managed_sources"
  mkdir -p "${BACKUP_DIR}"
  backup_managed_sources
  RELEASE_STAGE="validate_automation_plugin_runtime_rollback_snapshot"
  validate_automation_plugin_runtime_rollback_snapshot
  RELEASE_STAGE="static_preflight"
  run_static_preflight
  RELEASE_STAGE="build_release_virtualenvs"
  build_release_virtualenvs
  RELEASE_STAGE="preflight_signed_first_party_plugins"
  preflight_signed_first_party_plugins
  if [[ "${WINDOWS_WORKER_RELEASE_ENABLED}" == "1" ]]; then
    RELEASE_STAGE="preflight_worker_server_identity"
    preflight_worker_server_identity
  fi

  # Static checks and dependency builds can cross into an external-write
  # schedule. Normal releases recheck the window; the explicitly authorized
  # emergency path below minimizes and audits its old-scheduler residual race.
  RELEASE_STAGE="preflight_scheduled_write_window_before_mutation"
  preflight_scheduled_write_window
  if [[ "${EMERGENCY_SCHEDULED_WINDOW_OVERRIDE}" == "1" ]]; then
    # The old scheduler does not observe this marker dynamically. Capture all
    # read-only database state before creating the marker, then make the final
    # running-write check the immediately preceding operation to quiescence.
    RELEASE_STAGE="capture_control_plane_release_state"
    capture_control_plane_release_state
    RELEASE_STAGE="create_emergency_scheduler_release_hold"
    create_scheduler_release_hold
    RELEASE_STAGE="preflight_running_protected_writes_immediately_before_quiesce"
    preflight_running_protected_writes
  else
    RELEASE_STAGE="preflight_running_protected_writes"
    preflight_running_protected_writes
    RELEASE_STAGE="capture_control_plane_release_state"
    capture_control_plane_release_state
  fi
  MUTATION_STARTED=1
  RELEASE_STAGE="quiesce_runtime_services"
  quiesce_runtime_services
  RELEASE_STAGE="verify_protected_writes_quiesced"
  preflight_running_protected_writes
  RELEASE_STAGE="capture_automation_plugin_installation_state"
  capture_automation_plugin_installation_state
  RELEASE_STAGE="install_verified_first_party_plugin_artifacts"
  FIRST_PARTY_PLUGIN_INSTALL_ATTEMPTED=1
  install_verified_first_party_plugin_artifacts
  RELEASE_STAGE="retire_legacy_finance_etl"
  retire_legacy_finance_etl
  for scope in "${SCOPES[@]}"; do
    RELEASE_STAGE="sync_scope:${scope}"
    sync_scope "${scope}"
  done
  RELEASE_STAGE="apply_migrations"
  MIGRATIONS_ATTEMPTED=1
  apply_migrations
  RELEASE_STAGE="install_service_units"
  install_service_units
  RELEASE_STAGE="activate_release_virtualenvs"
  activate_release_virtualenvs
  RELEASE_STAGE="write_release_sha"
  mkdir -p "${ROOTS[agent]}/runtime"
  printf '%s\n' "${RELEASE_SHA}" >"${ROOTS[agent]}/runtime/release_sha"
  RELEASE_STAGE="write_automation_plugin_runtime_environment"
  write_automation_plugin_runtime_environment
  RELEASE_STAGE="create_scheduler_release_hold"
  ensure_scheduler_release_hold
  RELEASE_STAGE="restart_services"
  NEW_RUNTIME_START_ATTEMPTED=1
  restart_services
  RELEASE_STAGE="check_health"
  check_health
  check_post_restart_release_gates
  RELEASE_STAGE="record_dependency_hashes"
  record_active_dependency_hashes

  # Every rollback-capable gate has passed. Scheduler activation is the
  # release commit point: an ambiguous HTTP response may mean jobs have begun,
  # so never roll source or database state back after this request is sent.
  MUTATION_STARTED=0
  trap - ERR
  RELEASE_STAGE="activate_scheduler_after_release"
  if ! activate_scheduler_after_release; then
    echo "release_activation_incomplete stage_root=${STAGE_ROOT} scheduler_state_requires_recovery=1" >&2
    exit 1
  fi
  SCHEDULER_RELEASE_HOLD_CREATED=0
  RELEASE_STAGE="cleanup_successful_release"
  cleanup_successful_release
  echo "Release completed: ${RELEASE_SHA} (${TARGETS_CSV})"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  run_release
fi
