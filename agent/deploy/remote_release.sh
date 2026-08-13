#!/usr/bin/env bash
set -Eeuo pipefail

STAGE_ROOT="${1:?stage root is required}"
RELEASE_SHA="${2:?release SHA is required}"
TARGETS_CSV="${3:?target list is required}"
SKIP_RESTART="${4:-0}"
SKIP_HEALTH="${5:-0}"

DEPLOY_ROOT="/home/boyce/.boyi-deploy"
BACKUP_DIR="${STAGE_ROOT}/_rollback"
BACKUP_TREE="${BACKUP_DIR}/tree"
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

run_static_preflight() {
  local target runtime_python shared_python=""
  for target in "${REQUESTED_TARGETS[@]}"; do
    runtime_python="${PYTHON_BINS[$target]}"
    "${runtime_python}" -m compileall -q "${STAGE_ROOT}/${target}"
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

capture_control_plane_task_cutover_state() {
  local runner="${STAGE_ROOT}/agent/scripts/run_migrations.py"
  local migration_python="${PYTHON_BINS[agent]}"
  local status
  if [[ ! -d "${STAGE_ROOT}/agent/migrations" ]]; then
    CONTROL_PLANE_TASK_CUTOVER_PENDING_AT_APPLY=0
    return 0
  fi
  [[ -f "${runner}" ]] || {
    echo "Missing staged migration runner for control-plane task state check" >&2
    return 1
  }
  if [[ -n "${RELEASE_VENV}" && -x "${RELEASE_VENV}/bin/python" ]]; then
    migration_python="${RELEASE_VENV}/bin/python"
  fi
  [[ -x "${migration_python}" ]] || {
    echo "Missing Python runtime for control-plane task state check" >&2
    return 1
  }
  status="$(
    MIGRATION_ENV_FILE="${IDENTITY_ENV_FILE}" "${migration_python}" "${runner}" \
      --control-plane-task-cutover-status
  )" || return 1
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
}

restore_control_plane_task_cutover_data() {
  local runner="${STAGE_ROOT}/agent/scripts/run_migrations.py"
  local migration_python="${PYTHON_BINS[agent]}"
  [[ -f "${runner}" ]] || {
    echo "Missing staged migration runner for control-plane task rollback" >&2
    return 1
  }
  if [[ -n "${RELEASE_VENV}" && -x "${RELEASE_VENV}/bin/python" ]]; then
    migration_python="${RELEASE_VENV}/bin/python"
  fi
  [[ -x "${migration_python}" ]] || {
    echo "Missing Python runtime for control-plane task rollback" >&2
    return 1
  }
  MIGRATION_ENV_FILE="${IDENTITY_ENV_FILE}" "${migration_python}" "${runner}" \
    --restore-control-plane-task-cutover
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
    request_target = "/internal/v1/health"
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
    with urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if response.status != 200 or payload.get("ok") is not True:
        raise RuntimeError("signed health probe was rejected")
except Exception:
    print("service_identity_smoke=failed reason=signed_probe_rejected", file=sys.stderr)
    raise SystemExit(1)
print("service_identity_smoke=ok")
PY
}

restore_managed_release_state() {
  local restore_status=0
  local scope root new_manifest backup_scope relative target

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
      if [[ "${CONTROL_PLANE_TASK_CUTOVER_PENDING_AT_APPLY}" == "1" ]]; then
        restore_control_plane_task_cutover_data || rollback_status=1
      fi
      if [[ "${VENV_ACTIVATED}" == "1" ]] && ! restore_virtualenvs; then
        rollback_status=1
      fi
      restore_managed_release_state || rollback_status=1
      verify_runtime_virtualenvs || rollback_status=1
      restart_runtime_services_for_rollback || rollback_status=1
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
    rm -rf -- "${STAGE_ROOT}" || {
      echo "rollback_incomplete stage_root=${STAGE_ROOT} recovery_material_preserved=1" >&2
      exit "${exit_code:-1}"
    }
  else
    if ! remove_new_virtualenvs; then
      echo "release_cleanup_incomplete stage_root=${STAGE_ROOT}" >&2
      exit "${exit_code:-1}"
    fi
    rm -rf -- "${STAGE_ROOT}" || {
      echo "release_cleanup_incomplete stage_root=${STAGE_ROOT}" >&2
      exit "${exit_code:-1}"
    }
  fi
  exit "${exit_code}"
}

run_release() {
  trap rollback ERR
  RELEASE_STAGE="validate_environment"
  validate_environment
  RELEASE_STAGE="preflight_service_identity_configuration"
  preflight_service_identity_configuration
  RELEASE_STAGE="backup_managed_sources"
  mkdir -p "${BACKUP_DIR}"
  backup_managed_sources
  RELEASE_STAGE="static_preflight"
  run_static_preflight
  RELEASE_STAGE="build_release_virtualenvs"
  build_release_virtualenvs

  MUTATION_STARTED=1
  RELEASE_STAGE="quiesce_runtime_services"
  quiesce_runtime_services
  RELEASE_STAGE="retire_legacy_finance_etl"
  retire_legacy_finance_etl
  for scope in "${SCOPES[@]}"; do
    RELEASE_STAGE="sync_scope:${scope}"
    sync_scope "${scope}"
  done
  RELEASE_STAGE="capture_control_plane_task_cutover_state"
  capture_control_plane_task_cutover_state
  RELEASE_STAGE="apply_migrations"
  apply_migrations
  RELEASE_STAGE="install_service_units"
  install_service_units
  RELEASE_STAGE="activate_release_virtualenvs"
  activate_release_virtualenvs
  RELEASE_STAGE="write_release_sha"
  mkdir -p "${ROOTS[agent]}/runtime"
  printf '%s\n' "${RELEASE_SHA}" >"${ROOTS[agent]}/runtime/release_sha"
  RELEASE_STAGE="restart_services"
  restart_services
  RELEASE_STAGE="check_health"
  check_health
  RELEASE_STAGE="check_service_identity_smoke"
  check_service_identity_smoke
  RELEASE_STAGE="record_dependency_hashes"
  record_active_dependency_hashes

  MUTATION_STARTED=0
  trap - ERR
  RELEASE_STAGE="cleanup_successful_release"
  cleanup_successful_release
  echo "Release completed: ${RELEASE_SHA} (${TARGETS_CSV})"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  run_release
fi
