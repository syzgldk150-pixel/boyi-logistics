#!/usr/bin/env bash
set -Eeuo pipefail

STAGE_ROOT="${1:?stage root is required}"
RELEASE_SHA="${2:?release SHA is required}"
TARGETS_CSV="${3:?target list is required}"
SKIP_RESTART="${4:-0}"
SKIP_HEALTH="${5:-0}"
EMERGENCY_SCHEDULED_WINDOW_ARGUMENT="--emergency-scheduled-window-override=emergency_user_authorized"
EMERGENCY_SCHEDULED_WINDOW_OVERRIDE=0
RECOVER_RELEASE_HOLD_SHA=""
SCHEDULER_RELEASE_HOLD_ADOPTED=0
if (( $# > 7 )); then
  echo "emergency_scheduled_window_override=blocked reason=UNEXPECTED_ARGUMENT_COUNT" >&2
  exit 2
fi
case "${6:-}" in
  "") ;;
  "${EMERGENCY_SCHEDULED_WINDOW_ARGUMENT}")
    EMERGENCY_SCHEDULED_WINDOW_OVERRIDE=1
    ;;
  *)
    echo "emergency_scheduled_window_override=blocked reason=INVALID_AUTHORIZATION_ARGUMENT" >&2
    exit 2
    ;;
esac
if [[ -n "${7:-}" ]]; then
  if [[ "${7}" =~ ^--recover-held-release=([0-9a-f]{40})$ ]]; then
    RECOVER_RELEASE_HOLD_SHA="${BASH_REMATCH[1]}"
  else
    echo "scheduler_release_hold=blocked reason=INVALID_RECOVERY_ARGUMENT" >&2
    exit 2
  fi
fi
DEPLOY_ROOT="/home/boyce/.boyi-deploy"
RELEASE_LOCK_FILE="${DEPLOY_ROOT}/release.lock"
SCHEDULER_RELEASE_HOLD_FILE="${DEPLOY_ROOT}/scheduler-release.pause"
# Installed plugin versions live in ${AUTOMATION_PLUGIN_ROOT}/installed and are
# managed only by the Agent; a release never installs or removes packages.
AUTOMATION_PLUGIN_ROOT="/home/boyce/.boyi-automation-plugins"
PLUGIN_RUNTIME_ENV_FILE="/home/boyce/agent/runtime/automation_plugin_release.env"
BACKUP_DIR="${STAGE_ROOT}/_rollback"
BACKUP_TREE="${BACKUP_DIR}/tree"
VENV_ROOT="/home/boyce/.boyi-venvs"
PIP_INDEX_URL="${BOYI_PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple}"
PIP_RETRIES="${BOYI_PIP_RETRIES:-8}"
PIP_TIMEOUT_SECONDS="${BOYI_PIP_TIMEOUT_SECONDS:-300}"
MUTATION_STARTED=0
PROTECTED_WRITE_WAIT_SECONDS=600
PROTECTED_WRITE_POLL_SECONDS=15
VENV_ACTIVATED=0
SERVICES_QUIESCED=0
RELEASE_STAGE="initialization"
IDENTITY_ENV_FILE="/home/boyce/agent/.env"
MIGRATIONS_ATTEMPTED=0
DOMAIN_DATABASE_SPLIT_PENDING_AT_APPLY=0
NEW_RUNTIME_START_ATTEMPTED=0
SCHEDULER_RELEASE_HOLD_CREATED=0

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
IFS=',' read -r -a REQUESTED_TARGETS <<<"${TARGETS_CSV}"
RUNTIME_TARGETS=()
SCOPES=()
AGENT_RELEASE=0
COORDINATED_RELEASE=0
if [[ "${#REQUESTED_TARGETS[@]}" == "1" && "${REQUESTED_TARGETS[0]}" == "shared" ]]; then
  COORDINATED_RELEASE=1
  AGENT_RELEASE=1
  REQUESTED_TARGETS=(agent console)
  RUNTIME_TARGETS=(agent console)
  SCOPES=(shared agent console)
else
  declare -A SEEN_TARGETS=()
  for target in "${REQUESTED_TARGETS[@]}"; do
    case "${target}" in
      agent|console)
        [[ -z "${SEEN_TARGETS[$target]:-}" ]] || {
          echo "Duplicate release target: ${target}" >&2
          exit 2
        }
        SEEN_TARGETS[$target]=1
        RUNTIME_TARGETS+=("${target}")
        SCOPES+=("${target}")
        [[ "${target}" == "agent" ]] && AGENT_RELEASE=1
        ;;
      shared)
        echo "Shared release target cannot be combined with service targets" >&2
        exit 2
        ;;
      *) echo "Unsupported release target: ${target}" >&2; exit 2 ;;
    esac
  done
  [[ "${#RUNTIME_TARGETS[@]}" == "1" ]] || {
    echo "Agent and Console releases require separate remote invocations" >&2
    exit 2
  }
fi
[[ "${#RUNTIME_TARGETS[@]}" -gt 0 ]] || {
  echo "At least one release target is required" >&2
  exit 2
}

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

validate_scheduler_release_hold_recovery() {
  [[ "${COORDINATED_RELEASE}" == "1" &&
     "${RECOVER_RELEASE_HOLD_SHA}" =~ ^[0-9a-f]{40}$ &&
     -f "${SCHEDULER_RELEASE_HOLD_FILE}" && ! -L "${SCHEDULER_RELEASE_HOLD_FILE}" &&
     "$(tr -d '[:space:]' <"${SCHEDULER_RELEASE_HOLD_FILE}")" == "${RECOVER_RELEASE_HOLD_SHA}" ]] || {
    echo "scheduler_release_hold=blocked reason=HELD_RELEASE_IDENTITY_MISMATCH" >&2
    return 1
  }
  local target
  for target in agent console; do
    [[ "$(systemctl show "${SERVICES[$target]}" -p ActiveState --value)" == "inactive" &&
       "$(systemctl show "${SERVICES[$target]}" -p MainPID --value)" == "0" &&
       "$(systemctl show "${SERVICES[$target]}" -p ControlPID --value)" == "0" ]] || {
      echo "scheduler_release_hold=blocked reason=RECOVERY_SERVICE_NOT_STOPPED service=${SERVICES[$target]}" >&2
      return 1
    }
  done
}

create_scheduler_release_hold() {
  if [[ -n "${RECOVER_RELEASE_HOLD_SHA}" ]]; then
    validate_scheduler_release_hold_recovery || return 1
    local replacement
    replacement="$(mktemp "${SCHEDULER_RELEASE_HOLD_FILE}.XXXXXX")"
    printf '%s\n' "${RELEASE_SHA}" >"${replacement}"
    printf '%s\n' "${RECOVER_RELEASE_HOLD_SHA}" >"${BACKUP_DIR}/recovered_scheduler_release_hold.sha"
    mv -T -- "${replacement}" "${SCHEDULER_RELEASE_HOLD_FILE}"
    SCHEDULER_RELEASE_HOLD_CREATED=1
    SCHEDULER_RELEASE_HOLD_ADOPTED=1
    echo "scheduler_release_hold=adopted previous_release=${RECOVER_RELEASE_HOLD_SHA}"
    return
  fi
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
  if [[ "${SCHEDULER_RELEASE_HOLD_ADOPTED}" == "1" ]]; then
    echo "scheduler_release_hold=retained reason=RECOVERED_HELD_RELEASE" >&2
    return 1
  fi
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
  if [[ "${AGENT_RELEASE}" == "1" ]]; then
    if [[ -n "${RECOVER_RELEASE_HOLD_SHA}" ]]; then
      validate_scheduler_release_hold_recovery || return 1
    else
      [[ ! -e "${SCHEDULER_RELEASE_HOLD_FILE}" && ! -L "${SCHEDULER_RELEASE_HOLD_FILE}" ]] || {
        echo "A stale scheduler release hold requires manual recovery" >&2
        return 1
      }
    fi
  elif [[ -n "${RECOVER_RELEASE_HOLD_SHA}" ]]; then
    echo "scheduler_release_hold=blocked reason=RECOVERY_REQUIRES_SHARED_RELEASE" >&2
    return 1
  fi
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

  if [[ "${COORDINATED_RELEASE}" == "1" && -d "${STAGE_ROOT}/agent/migrations" ]]; then
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

preflight_running_protected_writes() {
  local output waited=0
  while true; do
    if output="$(run_staged_migration_runner --check-running-protected-writes 2>&1)"; then
      if [[ "${output}" == "protected_write_quiesce=ok running_writes=0" ]]; then
        echo "${output}"
        if (( waited > 0 )); then
          # Waiting moved the release closer to the next write schedule.
          preflight_scheduled_write_window || return 1
        fi
        return 0
      fi
      echo "protected_write_quiesce=blocked reason=UNEXPECTED_PREFLIGHT_RESPONSE count=1" >&2
      return 1
    fi
    if [[ ! "${output}" =~ ^protected_write_quiesce=blocked\ reason=[A-Z0-9_]+\ count=[0-9]+$ ]]; then
      echo "protected_write_quiesce=blocked reason=UNEXPECTED_PREFLIGHT_RESPONSE count=1" >&2
      return 1
    fi
    # Before shutdown, wait for a running plugin call (the 15-minute customer
    # collector takes ~5 minutes) instead of failing the release. Once mutation
    # has started the services are stopped and the check stays immediate.
    if [[ "${MUTATION_STARTED}" != "1" && "${output}" == *" reason=PROTECTED_WRITE_RUNNING "* ]] \
      && (( waited < PROTECTED_WRITE_WAIT_SECONDS )); then
      echo "protected_write_quiesce=waiting waited_seconds=${waited}"
      sleep "${PROTECTED_WRITE_POLL_SECONDS}"
      waited=$(( waited + PROTECTED_WRITE_POLL_SECONDS ))
      continue
    fi
    echo "${output}" >&2
    return 1
  done
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
  # Once shutdown begins, rollback must restore every selected runtime service
  # even if stopping or verifying a later selected unit fails midway.
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

  if [[ "${AGENT_RELEASE}" == "1" ]]; then
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

  fi

  local target
  for target in "${REQUESTED_TARGETS[@]}"; do
    cp "${UNIT_PATHS[$target]}" "${BACKUP_DIR}/${target}.service"
  done
}

preflight_staged_release_scope() {
  local helper="${STAGE_ROOT}/agent/scripts/release_scope.py"
  local output
  [[ -f "${helper}" && ! -L "${helper}" ]] || {
    echo "release_source_scope=blocked reason=HELPER_MISSING_OR_UNSAFE" >&2
    return 1
  }
  output="$(
    PYTHONDONTWRITEBYTECODE=1 "${PYTHON_BINS[agent]}" "${helper}" \
      --repository-root "${STAGE_ROOT}" verify-staged
  )" || return 1
  [[ "${output}" == "release_source_scope=ok" ]] || {
    echo "release_source_scope=blocked reason=UNEXPECTED_RESPONSE" >&2
    return 1
  }
  echo "${output}"
}

run_static_preflight() {
  if [[ "${AGENT_RELEASE}" == "1" ]]; then
    preflight_staged_release_scope
  fi
  local target runtime_python shared_python=""
  for target in "${REQUESTED_TARGETS[@]}"; do
    runtime_python="${PYTHON_BINS[$target]}"
    "${runtime_python}" -m compileall -q "${STAGE_ROOT}/${target}"
    if [[ -z "${shared_python}" ]]; then
      shared_python="${runtime_python}"
    fi
  done
  if [[ "${COORDINATED_RELEASE}" == "1" ]]; then
    "${shared_python}" -m compileall -q "${STAGE_ROOT}/shared"
  fi

  local migration_count
  migration_count=0
  if [[ "${COORDINATED_RELEASE}" == "1" ]]; then
    migration_count="$(find "${STAGE_ROOT}" -type f -path '*/migrations/*.sql' | wc -l)"
  fi
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

  rm -rf -- "${STAGE_ROOT}"
}

write_automation_plugin_runtime_environment() {
  local runtime_dir temp_path
  [[ -d "${AUTOMATION_PLUGIN_ROOT}/installed" && ! -L "${AUTOMATION_PLUGIN_ROOT}" && \
     ! -L "${AUTOMATION_PLUGIN_ROOT}/installed" ]] || {
    echo "Automation plugin root is missing or unsafe: ${AUTOMATION_PLUGIN_ROOT}" >&2
    return 1
  }
  runtime_dir="$(dirname "${PLUGIN_RUNTIME_ENV_FILE}")"
  mkdir -p "${runtime_dir}"
  temp_path="$(mktemp "${runtime_dir}/.automation_plugin_release.XXXXXX")"
  chmod 0600 "${temp_path}"
  if ! {
    printf 'BOYI_AUTOMATION_PLUGIN_ROOT=%s\n' "${AUTOMATION_PLUGIN_ROOT}"
    printf 'BOYI_AUTOMATION_PLUGIN_RELEASE_SHA=%s\n' "${RELEASE_SHA}"
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

capture_domain_database_split_state() {
  local status
  [[ -d "${STAGE_ROOT}/agent/migrations" ]] || return 0
  status="$(run_staged_migration_runner --domain-database-split-status)" || return 1
  case "${status}" in
    domain_database_split_status=pending_clean) DOMAIN_DATABASE_SPLIT_PENDING_AT_APPLY=1 ;;
    domain_database_split_status=ACTIVE) DOMAIN_DATABASE_SPLIT_PENDING_AT_APPLY=0 ;;
    *) echo "Migration 055 has unfinished state; restore or finish it before release" >&2; return 1 ;;
  esac
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

check_rollback_health() {
  local expected_sha="${RELEASE_SHA}" target
  declare -A pids_before=() pids_after=() restarts_before=() restarts_after=()
  if [[ "${AGENT_RELEASE}" == "1" ]]; then
    [[ -f "${ROOTS[agent]}/runtime/release_sha" && \
      ! -L "${ROOTS[agent]}/runtime/release_sha" ]] || {
      echo "rollback_health=failed reason=RESTORED_RELEASE_SHA_UNAVAILABLE" >&2
      return 1
    }
    read -r expected_sha <"${ROOTS[agent]}/runtime/release_sha"
    [[ "${expected_sha}" =~ ^[0-9a-f]{40}$ ]] || {
      echo "rollback_health=failed reason=RESTORED_RELEASE_SHA_INVALID" >&2
      return 1
    }
  fi

  # Bash locals are dynamically scoped, so the existing release health gate
  # validates the restored SHA and cannot be bypassed by --skip-health.
  local RELEASE_SHA="${expected_sha}"
  local SKIP_HEALTH=0
  if ! check_health; then
    echo "rollback_health=failed reason=RESTORED_RUNTIME_UNHEALTHY" >&2
    return 1
  fi

  for target in "${RUNTIME_TARGETS[@]}"; do
    pids_before[$target]="$(systemctl show "${SERVICES[$target]}" -p MainPID --value)" || return 1
    restarts_before[$target]="$(systemctl show "${SERVICES[$target]}" -p NRestarts --value)" || return 1
  done
  sleep 2
  for target in "${RUNTIME_TARGETS[@]}"; do
    pids_after[$target]="$(systemctl show "${SERVICES[$target]}" -p MainPID --value)" || return 1
    restarts_after[$target]="$(systemctl show "${SERVICES[$target]}" -p NRestarts --value)" || return 1
    if [[ -z "${pids_before[$target]}" || "${pids_before[$target]}" == "0" || \
      "${pids_before[$target]}" != "${pids_after[$target]}" || \
      "${restarts_before[$target]}" != "${restarts_after[$target]}" ]]; then
      echo "rollback_health=failed reason=RESTORED_RUNTIME_UNSTABLE target=${target}" >&2
      return 1
    fi
  done
  check_health || {
    echo "rollback_health=failed reason=RESTORED_RUNTIME_UNSTABLE" >&2
    return 1
  }
  echo "rollback_health=ok release_sha=${expected_sha}"
}

check_health() {
  [[ "${SKIP_HEALTH}" == "1" ]] && return 0
  local target body healthy health_wait_seconds health_deadline
  local -a health_targets=("${REQUESTED_TARGETS[@]}")
  if [[ "${VENV_ACTIVATED}" == "1" || "${SERVICES_QUIESCED}" == "1" ]]; then
    health_targets=("${RUNTIME_TARGETS[@]}")
  fi
  for target in "${health_targets[@]}"; do
    healthy=0
    # Cross-region RDS startup performs durable plugin/scheduler projection
    # before exposing HTTP. Preserve the hold while that bounded work finishes.
    health_wait_seconds=60
    if [[ "${target}" == "agent" && -f "${ROOTS[agent]}/runtime/database-target.json" ]]; then
      health_wait_seconds=1200
    fi
    health_deadline=$((SECONDS + health_wait_seconds))
    while (( SECONDS < health_deadline )); do
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
  local console_python="${PYTHON_BINS[console]}"
  [[ -x "${console_python}" && -f "${IDENTITY_ENV_FILE}" ]] || {
    echo "service_identity_smoke=failed reason=runtime_unavailable" >&2
    return 1
  }

  BOYI_IDENTITY_ENV_FILE="${IDENTITY_ENV_FILE}" \
    BOYI_DEPLOYED_ROOT="/home/boyce" \
    "${console_python}" - <<'PY'
import json
import os
import secrets
import sys
from enum import Enum
from urllib.error import HTTPError
from urllib.request import Request, urlopen

class SmokeGate(str, Enum):
    IDENTITY_CONFIGURATION = "identity_configuration"
    HTTP_401 = "http_401"
    HTTP_403 = "http_403"
    HTTP_5XX = "http_5xx"
    HTTP_OTHER = "http_other"
    RESPONSE_CONTRACT = "response_contract"
    SCHEDULER_STATE = "scheduler_state"
    SCHEDULER_HOLD = "scheduler_hold"
    RUNNER_STATE = "runner_state"
    RUNNER_HOLD = "runner_hold"
    RUNNER_ACTIVE = "runner_active"
    PLUGIN_BROKER = "plugin_broker"
    PLUGIN_SANDBOX = "plugin_sandbox"
    PLUGIN_CATALOG_UNSUPPORTED = "plugin_catalog_unsupported"
    PLUGIN_CATALOG_INVALID_TRUST = "plugin_catalog_invalid_trust"
    PLUGIN_CATALOG_UNSTABLE_GENERATIONS = "plugin_catalog_unstable_generations"
    PLUGIN_CATALOG_INVALID_RUNTIME = "plugin_catalog_invalid_runtime"
    PLUGIN_CATALOG_AGGREGATE_OR_SHAPE = "plugin_catalog_aggregate_or_shape"
    PLUGIN_GENERATIONS = "plugin_generations"
    PLUGIN_AGGREGATE = "plugin_aggregate"


def http_failure_gate(status):
    if status == 401:
        return SmokeGate.HTTP_401
    if status == 403:
        return SmokeGate.HTTP_403
    if isinstance(status, int) and 500 <= status <= 599:
        return SmokeGate.HTTP_5XX
    return SmokeGate.HTTP_OTHER


failure_gate = SmokeGate.IDENTITY_CONFIGURATION
try:
    from dotenv import dotenv_values

    sys.path.insert(0, os.environ["BOYI_DEPLOYED_ROOT"])
    from shared.release_identity import release_principal
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
    headers = build_console_identity_headers(
        secret=signing_secret,
        method="GET",
        request_target=request_target,
        body=b"",
        principal=release_principal("health"),
        nonce=secrets.token_urlsafe(24),
    )
    headers["X-Agent-Internal-Token"] = internal_token
    request = Request(
        f"http://127.0.0.1:9000{request_target}",
        headers=headers,
        method="GET",
    )
    failure_gate = SmokeGate.HTTP_OTHER
    try:
        with urlopen(request, timeout=10) as response:
            response_status = response.status
            response_body = response.read()
    except HTTPError as exc:
        failure_gate = http_failure_gate(exc.code)
        raise
    if response_status != 200:
        failure_gate = http_failure_gate(response_status)
        raise RuntimeError("signed health probe returned an unexpected status")
    failure_gate = SmokeGate.RESPONSE_CONTRACT
    payload = json.loads(response_body.decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RuntimeError("signed health probe was rejected")
    data = payload.get("data")
    components = data.get("components") if isinstance(data, dict) else None
    scheduler = components.get("scheduler") if isinstance(components, dict) else None
    workflow_runner = (
        components.get("workflow_runner") if isinstance(components, dict) else None
    )
    automation_plugins = (
        components.get("automation_plugins") if isinstance(components, dict) else None
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
    failure_gate = SmokeGate.PLUGIN_SANDBOX
    plugin_sandbox = automation_plugins.get("sandbox")
    if (
        not isinstance(plugin_sandbox, dict)
        or plugin_sandbox.get("state") != "ready"
        or plugin_sandbox.get("code") != "OK"
        or not isinstance(plugin_sandbox.get("checked_at"), str)
        or not plugin_sandbox["checked_at"]
    ):
        raise RuntimeError("automation plugin sandbox canary is not release-ready")
    failure_gate = SmokeGate.PLUGIN_CATALOG_AGGREGATE_OR_SHAPE
    plugin_catalog = automation_plugins.get("catalog")
    if not isinstance(plugin_catalog, dict):
        raise RuntimeError("automation plugin catalog health shape is invalid")
    catalog_failure_fields = (
        (
            SmokeGate.PLUGIN_CATALOG_UNSUPPORTED,
            "unsupported_automation_ids",
        ),
        (
            SmokeGate.PLUGIN_CATALOG_INVALID_TRUST,
            "invalid_enabled_trust",
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
            failure_gate = catalog_gate
            raise RuntimeError("automation plugin catalog is not release-ready")
    failure_gate = SmokeGate.PLUGIN_CATALOG_AGGREGATE_OR_SHAPE
    unstable_generations = plugin_catalog.get("unstable_generations")
    catalog_runnable = plugin_catalog.get("runnable")
    catalog_runtime_status = plugin_catalog.get("runtime_status")
    if (
        not isinstance(unstable_generations, list)
        or not isinstance(catalog_runnable, bool)
        or catalog_runtime_status not in {"READY", "UNAVAILABLE"}
        or (catalog_runtime_status == "READY") != catalog_runnable
        or bool(unstable_generations) == catalog_runnable
        or plugin_catalog.get("ok") is not True
    ):
        raise RuntimeError("automation plugin catalog aggregate is not release-ready")
    failure_gate = SmokeGate.PLUGIN_GENERATIONS
    plugin_generations = automation_plugins.get("generations")
    if (
        not isinstance(plugin_generations, dict)
        or not isinstance(plugin_generations.get("healthy"), bool)
        or not isinstance(plugin_generations.get("blocked_projects"), dict)
    ):
        raise RuntimeError("automation plugin generations are not release-ready")
    failure_gate = SmokeGate.PLUGIN_AGGREGATE
    plugin_runnable = automation_plugins.get("runnable")
    plugin_runtime_status = automation_plugins.get("runtime_status")
    if (
        automation_plugins.get("ok") is not True
        or not isinstance(plugin_runnable, bool)
        or plugin_runtime_status not in {"READY", "UNAVAILABLE"}
        or (plugin_runtime_status == "READY") != plugin_runnable
    ):
        raise RuntimeError("automation plugin runtime is not release-ready")
except Exception:
    print(
        f"service_identity_smoke=failed reason={failure_gate.value}",
        file=sys.stderr,
    )
    raise SystemExit(1)
print("service_identity_smoke=ok")
PY
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
from shared.release_identity import release_principal
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
    rds_target = os.path.isfile(os.path.join(
        os.environ["BOYI_DEPLOYED_ROOT"], "agent", "runtime", "database-target.json"
    ))
    activation_timeout = 1200 if rds_target else 60
    # A timed-out RDS activation can still be reconciling on the server.
    # Do not queue another state-changing request behind that operation.
    activation_attempts = 1 if rds_target else 3
    principal = release_principal("activate")
    payload = None
    response_status = None
    last_error = None
    for _attempt in range(activation_attempts):
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
            with urlopen(request, timeout=activation_timeout) as response:
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
        or data["workflow_runner"].get("state") != "reserved"
        or data["workflow_runner"].get("release_hold") is not False
        or not isinstance(data.get("automation_plugins"), dict)
        or data["automation_plugins"].get("ok") is not True
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

  if [[ "${AGENT_RELEASE}" == "1" ]]; then
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
    echo "Release failed; stopping selected runtime services before rollback" >&2
    if ! stop_runtime_services_for_rollback; then
      rollback_status=1
      services_stopped=0
    fi
    if [[ "${services_stopped}" == "1" ]]; then
      echo "Restoring managed release state" >&2
      if [[ "${DOMAIN_DATABASE_SPLIT_PENDING_AT_APPLY}" == "1" && \
        "${MIGRATIONS_ATTEMPTED}" == "1" ]]; then
        run_staged_migration_runner --restore-domain-database-split || rollback_status=1
      fi
      if [[ "${VENV_ACTIVATED}" == "1" ]] && ! restore_virtualenvs; then
        rollback_status=1
      fi
      restore_managed_release_state || rollback_status=1
      verify_runtime_virtualenvs || rollback_status=1
      if [[ "${rollback_status}" == "0" && "${AGENT_RELEASE}" == "0" ]]; then
        if ! restart_runtime_services_for_rollback; then
          rollback_status=1
        elif ! check_rollback_health; then
          stop_runtime_services_for_rollback || rollback_status=1
          rollback_status=1
        fi
      elif [[ "${rollback_status}" == "0" ]]; then
        if ! restart_runtime_services_for_rollback; then
          echo "Rollback runtime failed to start under the scheduler release hold" >&2
          stop_runtime_services_for_rollback || rollback_status=1
          rollback_status=1
        elif ! check_rollback_health; then
          echo "Rollback runtime failed its held stable health gate" >&2
          stop_runtime_services_for_rollback || rollback_status=1
          rollback_status=1
        elif ! stop_runtime_services_for_rollback; then
          rollback_status=1
          echo "Rollback activation stopped because restored services could not be quiesced" >&2
        elif ! clear_scheduler_release_hold_for_rollback; then
          rollback_status=1
          echo "Rollback restart skipped because the scheduler release hold could not be cleared" >&2
        elif ! restart_runtime_services_for_rollback; then
          echo "Rollback runtime failed to restart after clearing the release hold" >&2
          stop_runtime_services_for_rollback || rollback_status=1
          rollback_status=1
        elif ! check_rollback_health; then
          echo "Rollback runtime failed its final stable health gate" >&2
          stop_runtime_services_for_rollback || rollback_status=1
          rollback_status=1
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
  if [[ "${AGENT_RELEASE}" == "1" ]]; then
    RELEASE_STAGE="preflight_service_identity_configuration"
    preflight_service_identity_configuration
    if [[ "${EMERGENCY_SCHEDULED_WINDOW_OVERRIDE}" == "1" ]]; then
      echo "emergency_scheduled_window_override=authorized emergency_user_authorized=true scope=scheduled_write_window_proximity_only residual_race_user_authorized=true"
      RELEASE_STAGE="preflight_running_protected_writes_before_emergency_window_override"
      preflight_running_protected_writes
    fi
    RELEASE_STAGE="preflight_scheduled_write_window"
    preflight_scheduled_write_window
  fi
  RELEASE_STAGE="backup_managed_sources"
  mkdir -p "${BACKUP_DIR}"
  backup_managed_sources
  RELEASE_STAGE="static_preflight"
  run_static_preflight
  if [[ "${COORDINATED_RELEASE}" == "1" ]]; then
    RELEASE_STAGE="build_release_virtualenvs"
    build_release_virtualenvs
  fi
  if [[ "${AGENT_RELEASE}" == "1" ]]; then
    # Agent mutations can cross into an external-write schedule. Recheck the
    # window after static/dependency work and immediately before quiescence.
    RELEASE_STAGE="preflight_scheduled_write_window_before_mutation"
    preflight_scheduled_write_window
    if [[ "${EMERGENCY_SCHEDULED_WINDOW_OVERRIDE}" == "1" || -n "${RECOVER_RELEASE_HOLD_SHA}" ]]; then
      RELEASE_STAGE="capture_domain_database_split_state"
      capture_domain_database_split_state
      RELEASE_STAGE="create_pre_mutation_scheduler_release_hold"
      create_scheduler_release_hold
      RELEASE_STAGE="preflight_running_protected_writes_immediately_before_quiesce"
      preflight_running_protected_writes
    else
      RELEASE_STAGE="preflight_running_protected_writes"
      preflight_running_protected_writes
      RELEASE_STAGE="capture_domain_database_split_state"
      capture_domain_database_split_state
    fi
  fi
  MUTATION_STARTED=1
  RELEASE_STAGE="quiesce_runtime_services"
  quiesce_runtime_services
  if [[ "${AGENT_RELEASE}" == "1" ]]; then
    RELEASE_STAGE="verify_protected_writes_quiesced"
    preflight_running_protected_writes
  fi
  for scope in "${SCOPES[@]}"; do
    RELEASE_STAGE="sync_scope:${scope}"
    sync_scope "${scope}"
  done
  if [[ "${COORDINATED_RELEASE}" == "1" ]]; then
    RELEASE_STAGE="apply_migrations"
    MIGRATIONS_ATTEMPTED=1
    apply_migrations
  fi
  RELEASE_STAGE="install_service_units"
  install_service_units
  if [[ "${COORDINATED_RELEASE}" == "1" ]]; then
    RELEASE_STAGE="activate_release_virtualenvs"
    activate_release_virtualenvs
  fi
  if [[ "${AGENT_RELEASE}" == "1" ]]; then
    RELEASE_STAGE="write_release_sha"
    mkdir -p "${ROOTS[agent]}/runtime"
    printf '%s\n' "${RELEASE_SHA}" >"${ROOTS[agent]}/runtime/release_sha"
    RELEASE_STAGE="write_automation_plugin_runtime_environment"
    write_automation_plugin_runtime_environment
    RELEASE_STAGE="create_scheduler_release_hold"
    ensure_scheduler_release_hold
  fi
  RELEASE_STAGE="restart_services"
  NEW_RUNTIME_START_ATTEMPTED=1
  restart_services
  RELEASE_STAGE="check_health"
  check_health
  if [[ "${COORDINATED_RELEASE}" == "1" ]]; then
    RELEASE_STAGE="check_service_identity_smoke"
    check_service_identity_smoke
  fi
  if [[ "${COORDINATED_RELEASE}" == "1" ]]; then
    RELEASE_STAGE="record_dependency_hashes"
    record_active_dependency_hashes
  fi

  # Every rollback-capable gate has passed. Agent activation is the release
  # commit point; Console-only releases have no scheduler hold to consume.
  MUTATION_STARTED=0
  trap - ERR
  if [[ "${AGENT_RELEASE}" == "1" ]]; then
    RELEASE_STAGE="activate_scheduler_after_release"
    if ! activate_scheduler_after_release; then
      echo "release_activation_incomplete stage_root=${STAGE_ROOT} scheduler_state_requires_recovery=1" >&2
      exit 1
    fi
    SCHEDULER_RELEASE_HOLD_CREATED=0
  fi
  RELEASE_STAGE="cleanup_successful_release"
  cleanup_successful_release
  echo "Release completed: ${RELEASE_SHA} (${TARGETS_CSV})"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  run_release
fi
