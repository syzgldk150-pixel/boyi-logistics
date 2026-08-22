from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RELEASE_SCRIPT = REPOSITORY_ROOT / "agent" / "deploy" / "remote_release.sh"
WORKER_NGINX_CONFIG = (
    REPOSITORY_ROOT
    / "agent"
    / "deploy"
    / "nginx"
    / "boyi-worker-mtls.conf"
)


def _path_for_bash(path: Path) -> str:
    raw = str(path)
    wsl_prefix = "\\\\wsl.localhost\\Ubuntu\\"
    if os.name == "nt" and raw.lower().startswith(wsl_prefix.lower()):
        return "/" + raw[len(wsl_prefix) :].replace("\\", "/")
    if os.name == "nt":
        return subprocess.check_output(
            ["wsl.exe", "-d", "Ubuntu", "--", "wslpath", "-a", raw],
            text=True,
            encoding="utf-8",
        ).strip()
    return raw


def _run_preflight(
    *,
    mismatch: bool = False,
    broad_mode: bool = False,
    broad_ca_directory: bool = False,
    client_ca_symlink: bool = False,
    missing_include: bool = False,
    nginx_test_ok: bool = True,
) -> subprocess.CompletedProcess[str]:
    task_tmp_root = REPOSITORY_ROOT / ".task_tmp"
    task_tmp_preexisting = task_tmp_root.exists()
    task_tmp_root.mkdir(exist_ok=True)
    temporary: tempfile.TemporaryDirectory[str] | None = None
    try:
        temporary = tempfile.TemporaryDirectory(dir=task_tmp_root)
        root = Path(temporary.name)
        stage = root / "stage"
        staged_config = stage / "agent" / "deploy" / "nginx" / WORKER_NGINX_CONFIG.name
        staged_config.parent.mkdir(parents=True)
        staged_config.write_bytes(WORKER_NGINX_CONFIG.read_bytes())

        snippets = root / "etc" / "nginx" / "snippets"
        mtls = root / "etc" / "nginx" / "mtls"
        sites_available = root / "etc" / "nginx" / "sites-available"
        sites_enabled = root / "etc" / "nginx" / "sites-enabled"
        for directory in (snippets, mtls, sites_available, sites_enabled):
            directory.mkdir(parents=True)
        if broad_ca_directory:
            mtls.chmod(0o777)
        installed_config = snippets / WORKER_NGINX_CONFIG.name
        installed_config.write_bytes(WORKER_NGINX_CONFIG.read_bytes())
        if mismatch:
            installed_config.write_text("# stale config\n", encoding="utf-8")
        installed_config.chmod(0o666 if broad_mode else 0o644)

        real_ca = mtls / "worker-ca-real.pem"
        real_ca.write_text("test-only-public-ca-placeholder\n", encoding="utf-8")
        real_ca.chmod(0o644)
        client_ca = mtls / "boyi-worker-client-ca.pem"
        if client_ca_symlink:
            client_ca.symlink_to(real_ca)
        else:
            client_ca.write_text("test-only-public-ca-placeholder\n", encoding="utf-8")
            client_ca.chmod(0o644)

        site = sites_available / "boyi.homes.conf"
        site.write_text(
            (
                "server {\n"
                + (
                    "    include /etc/nginx/snippets/boyi-worker-mtls.conf;\n"
                    if not missing_include
                    else "    location / { return 200; }\n"
                )
                + "}\n"
            ),
            encoding="utf-8",
        )
        site.chmod(0o644)
        enabled_site = sites_enabled / "boyi.homes.conf"
        enabled_site.symlink_to(site)

        nginx = root / "usr" / "sbin" / "nginx"
        nginx.parent.mkdir(parents=True)
        nginx.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        nginx.chmod(0o755)

        harness = root / "preflight.sh"
        harness.write_text(
            textwrap.dedent(
                """
                set -Eeuo pipefail
                source "$1" "$2" aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa agent,console 0 0
                WORKER_NGINX_STAGED_CONFIG="$3"
                WORKER_NGINX_INSTALLED_CONFIG="$4"
                WORKER_NGINX_SITE_CONFIG="$5"
                WORKER_NGINX_SITES_AVAILABLE_ROOT="$6"
                WORKER_NGINX_SITES_ENABLED_ROOT="$7"
                WORKER_MTLS_CLIENT_CA="$8"
                WORKER_NGINX_BIN="$9"
                WORKER_NGINX_REQUIRED_UID="$(id -u)"
                NGINX_TEST_OK="${10}"
                systemctl() {
                  [[ "$1" == "is-active" && "$2" == "--quiet" && "$3" == "nginx.service" ]]
                }
                sudo() {
                  [[ "$1" == "-n" ]] || return 1
                  shift
                  [[ "$1" == "${WORKER_NGINX_BIN}" && "$2" == "-t" && "${NGINX_TEST_OK}" == "1" ]]
                }
                preflight_worker_mtls_proxy
                """
            ),
            encoding="utf-8",
            newline="\n",
        )
        command = [
            "bash",
            _path_for_bash(harness),
            _path_for_bash(RELEASE_SCRIPT),
            _path_for_bash(stage),
            _path_for_bash(staged_config),
            _path_for_bash(installed_config),
            _path_for_bash(enabled_site),
            _path_for_bash(sites_available),
            _path_for_bash(sites_enabled),
            _path_for_bash(client_ca),
            _path_for_bash(nginx),
            "1" if nginx_test_ok else "0",
        ]
        if os.name == "nt":
            command = ["wsl.exe", "-d", "Ubuntu", "--", *command]
        return subprocess.run(
            command,
            cwd=REPOSITORY_ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
    finally:
        if temporary is not None:
            temporary.cleanup()
        if not task_tmp_preexisting and task_tmp_root.exists():
            task_tmp_root.rmdir()


def test_worker_nginx_location_is_exact_mtls_and_strips_other_auth_boundaries() -> None:
    config = WORKER_NGINX_CONFIG.read_text(encoding="utf-8")
    assert config.count("location ^~ /internal/v1/automation/worker/ {") == 1
    assert "location ^~ /internal/v1/ {" not in config
    assert "ssl_client_certificate /etc/nginx/mtls/boyi-worker-client-ca.pem;" in config
    assert "ssl_verify_client optional;" in config
    assert config.index("ssl_verify_client optional;") < config.index("location ^~")
    assert "if ($ssl_client_verify != SUCCESS)" in config
    assert "proxy_pass http://127.0.0.1:9000;" in config
    assert "proxy_set_header X-SSL-Client-Verify $ssl_client_verify;" in config
    assert "proxy_set_header X-SSL-Client-Cert $ssl_client_escaped_cert;" in config
    assert "proxy_set_header X-Worker-Device-ID $http_x_worker_device_id;" in config
    for header in (
        "X-Agent-Internal-Token",
        "X-Console-Principal",
        "X-Console-Timestamp",
        "X-Console-Nonce",
        "X-Console-Signature",
    ):
        assert f'proxy_set_header {header} "";' in config


def test_release_preflight_accepts_exact_installed_worker_proxy_without_mutation() -> None:
    completed = _run_preflight()
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.startswith("worker_mtls_proxy_preflight=ok config_sha256=")


@pytest.mark.parametrize(
    ("arguments", "reason"),
    (
        ({"mismatch": True}, "INSTALLED_CONFIG_RELEASE_MISMATCH"),
        ({"broad_mode": True}, "INSTALLED_CONFIG_WRITABLE_BY_NON_OWNER"),
        ({"broad_ca_directory": True}, "CLIENT_CA_DIRECTORY_WRITABLE_BY_NON_OWNER"),
        ({"client_ca_symlink": True}, "CLIENT_CA_MISSING_OR_UNSAFE"),
        ({"missing_include": True}, "SITE_INCLUDE_INVALID"),
        ({"nginx_test_ok": False}, "NGINX_CONFIG_TEST_FAILED"),
    ),
)
def test_release_preflight_fails_closed_for_worker_proxy_drift(
    arguments: dict[str, bool],
    reason: str,
) -> None:
    completed = _run_preflight(**arguments)
    assert completed.returncode != 0
    assert f"worker_mtls_proxy_preflight=blocked reason={reason}" in completed.stderr


def test_current_release_skips_worker_proxy_preflight_without_blocking_mutations() -> None:
    release = RELEASE_SCRIPT.read_text(encoding="utf-8")
    execution = release.split("trap rollback ERR", 1)[1]
    scope_guard = execution.split(
        'if [[ "${WINDOWS_WORKER_RELEASE_ENABLED}" == "1" ]]; then', 1
    )[1].split("\n  fi", 1)[0]
    assert "WINDOWS_WORKER_RELEASE_ENABLED=0" in release
    assert 'WORKER_NGINX_REQUIRED_UID=0' in release
    assert 'RELEASE_STAGE="preflight_worker_mtls_proxy"' in scope_guard
    assert "preflight_worker_mtls_proxy\n" in scope_guard
    assert 'echo "windows_worker_release_scope=disabled"' in scope_guard
    assert execution.index("WINDOWS_WORKER_RELEASE_ENABLED") < execution.index(
        "backup_managed_sources"
    )
    function = release.split("preflight_worker_mtls_proxy() {", 1)[1].split("\n}", 1)[0]
    assert "sha256sum -- \"${WORKER_MTLS_CLIENT_CA}\"" not in function
    assert "openssl" not in function
    assert "cat " not in function
