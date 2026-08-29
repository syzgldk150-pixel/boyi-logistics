"""OS-level sandbox launchers for untrusted signed Python actions."""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Mapping

from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.sdk import PLUGIN_SDK_SOURCE


_DEFAULT_PRLIMIT_PATH = Path("/usr/bin/prlimit")
_RESOURCE_LIMIT_OPTIONS = (
    "--as=1073741824:1073741824",
    "--nproc=64:64",
    "--cpu=300:300",
    "--fsize=16777216:16777216",
    "--nofile=128:128",
)


@dataclass(frozen=True)
class SandboxCanaryResult:
    """Cached, payload-free proof that the production sandbox is executable."""

    healthy: bool
    code: str
    checked_at: datetime


class FailClosedPluginSandbox:
    async def launch(self, **_: object) -> object:
        raise PluginExecutionError(
            "uploaded Python plugins require an OS-level sandbox",
            code="PLUGIN_SANDBOX_UNAVAILABLE",
        )


class BubblewrapPluginSandbox:
    """Linux namespace sandbox with no home/Agent mount and no network."""

    def __init__(
        self,
        executable: Path | str,
        *,
        trusted_base_prefix: Path | str | None = None,
        trusted_runtime_prefix: Path | str | None = None,
        prlimit_path: Path | str = _DEFAULT_PRLIMIT_PATH,
    ) -> None:
        target = Path(executable)
        if not target.is_absolute() or target.is_symlink() or not target.is_file():
            raise ValueError("bubblewrap executable must be one explicit regular file")
        self._executable = target.resolve()
        limiter = Path(prlimit_path)
        if (
            not limiter.is_absolute()
            or limiter.is_symlink()
            or not limiter.is_file()
        ):
            raise ValueError("prlimit executable must be one explicit regular file")
        self._prlimit = limiter.resolve()
        raw_prefix = Path(trusted_base_prefix) if trusted_base_prefix is not None else Path(sys.base_prefix)
        if (
            not raw_prefix.is_absolute()
            or raw_prefix == Path("/")
            or raw_prefix.is_symlink()
            or not raw_prefix.is_dir()
            or raw_prefix.resolve() != raw_prefix
        ):
            raise ValueError("trusted CPython base prefix must be one safe explicit directory")
        self._trusted_base_prefix = raw_prefix
        raw_runtime_prefix = (
            Path(trusted_runtime_prefix)
            if trusted_runtime_prefix is not None
            else (raw_prefix if trusted_base_prefix is not None else Path(sys.prefix))
        )
        resolved_runtime_prefix = raw_runtime_prefix.resolve()
        if (
            not raw_runtime_prefix.is_absolute()
            or resolved_runtime_prefix == Path("/")
            or not raw_runtime_prefix.is_dir()
            or not resolved_runtime_prefix.is_dir()
        ):
            raise ValueError("trusted Agent runtime prefix must be one safe explicit directory")
        self._trusted_runtime_prefix = resolved_runtime_prefix
        self._canary_result: SandboxCanaryResult | None = None

    @staticmethod
    def _relative(value: str, label: str) -> PurePosixPath:
        path = PurePosixPath(value)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise PluginExecutionError(f"sandbox {label} path is unsafe")
        return path

    def _base_prefix(self, root: Path, python_path: PurePosixPath) -> Path:
        """Require venv metadata to agree with the Agent's trusted CPython base."""

        venv_root = root.joinpath(*python_path.parts).parent.parent
        config = venv_root / "pyvenv.cfg"
        if (
            not venv_root.is_dir()
            or venv_root.is_symlink()
            or not config.is_file()
            or config.is_symlink()
        ):
            raise PluginExecutionError("sandbox virtual environment is missing or unsafe")
        home_values = []
        for line in config.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip().lower() == "home" and value.strip():
                home_values.append(value.strip())
        if len(home_values) != 1:
            raise PluginExecutionError("sandbox virtual environment base prefix is invalid")
        home = Path(home_values[0])
        expected_homes = {
            (self._trusted_runtime_prefix / "bin").resolve(),
            (self._trusted_base_prefix / "bin").resolve(),
        }
        if (
            not home.is_absolute()
            or home.is_symlink()
            or not home.is_dir()
            or home.resolve() not in expected_homes
        ):
            raise PluginExecutionError("sandbox virtual environment base prefix is invalid")
        return self._trusted_base_prefix

    @staticmethod
    def _covered_by_system_bind(path: Path, system_roots: tuple[Path, ...]) -> bool:
        return any(path == root or root in path.parents for root in system_roots)

    def _limited_command(self, bubblewrap_arguments: list[str]) -> list[str]:
        """Apply hard limits; prlimit execs bwrap so its process group is preserved."""

        return [
            str(self._prlimit),
            *_RESOURCE_LIMIT_OPTIONS,
            "--",
            str(self._executable),
            *bubblewrap_arguments,
        ]

    @property
    def canary_result(self) -> SandboxCanaryResult | None:
        return self._canary_result

    def _canary_python(self) -> Path:
        candidates = (
            self._trusted_base_prefix / "bin" / "python",
            self._trusted_base_prefix / "bin" / "python3",
            Path(sys.executable).resolve(),
        )
        for candidate in candidates:
            if candidate.is_absolute() and candidate.is_file() and not candidate.is_symlink():
                return candidate
        raise PluginExecutionError(
            "sandbox canary Python is unavailable",
            code="PLUGIN_SANDBOX_CANARY_PYTHON_UNAVAILABLE",
        )

    async def startup_canary(self) -> SandboxCanaryResult:
        """Launch one isolated, credential-free interpreter and cache the result.

        The payload imports standard library modules and compiles the exact
        broker SDK source, but never creates a broker request or accesses a
        project, account, resource, or network.
        """

        if self._canary_result is not None:
            return self._canary_result
        checked_at = datetime.now(timezone.utc)
        try:
            python = self._canary_python()
            system_roots = tuple(
                Path(system_root)
                for system_root in ("/usr", "/bin", "/lib", "/lib64")
                if Path(system_root).exists()
            )
            bubblewrap_arguments = [
                "--die-with-parent",
                "--new-session",
                "--unshare-all",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--tmpfs",
                "/tmp",
            ]
            for system_root in system_roots:
                bubblewrap_arguments.extend(
                    ("--ro-bind", str(system_root), str(system_root))
                )
            if not self._covered_by_system_bind(self._trusted_base_prefix, system_roots):
                bubblewrap_arguments.extend(
                    ("--ro-bind", str(self._trusted_base_prefix), str(self._trusted_base_prefix))
                )
            payload = (
                "import json, pathlib\n"
                f"sdk_source = {PLUGIN_SDK_SOURCE!r}\n"
                "namespace = {}\n"
                "exec(compile(sdk_source, '<boyi-plugin-sdk>', 'exec'), namespace)\n"
                "assert callable(namespace['broker_call'])\n"
                "print(json.dumps({'ok': True}, sort_keys=True))\n"
            )
            bubblewrap_arguments.extend(
                (
                    "--chdir",
                    "/tmp",
                    "--setenv",
                    "PATH",
                    "/usr/bin:/bin",
                    "--setenv",
                    "HOME",
                    "/tmp",
                    "--setenv",
                    "TMPDIR",
                    "/tmp",
                    str(python),
                    "-I",
                    "-c",
                    payload,
                )
            )
            command = self._limited_command(bubblewrap_arguments)
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=None,
                env={"PATH": os.defpath, "LANG": os.environ.get("LANG", "C.UTF-8")},
                start_new_session=True,
            )
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode != 0 or stdout.strip() != b'{"ok": true}':
                raise PluginExecutionError(
                    "sandbox canary did not complete",
                    code="PLUGIN_SANDBOX_CANARY_FAILED",
                )
            result = SandboxCanaryResult(True, "OK", checked_at)
        except PluginExecutionError as exc:
            result = SandboxCanaryResult(False, exc.code or "PLUGIN_SANDBOX_CANARY_FAILED", checked_at)
        except (OSError, asyncio.TimeoutError):
            result = SandboxCanaryResult(False, "PLUGIN_SANDBOX_CANARY_FAILED", checked_at)
        self._canary_result = result
        return result

    async def launch(
        self,
        *,
        install_root: Path,
        python_relative: str,
        entrypoint_relative: str,
        environment: Mapping[str, str],
        broker_socket_path: Path | None,
    ) -> asyncio.subprocess.Process:
        root = install_root.resolve()
        if not root.is_dir() or root.is_symlink():
            raise PluginExecutionError("sandbox plugin root is missing or unsafe")
        python_path = self._relative(python_relative, "Python")
        entrypoint = self._relative(entrypoint_relative, "entrypoint")
        base_prefix = self._base_prefix(root, python_path)
        bubblewrap_arguments = [
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
        ]
        system_roots = tuple(
            Path(system_root) for system_root in ("/usr", "/bin", "/lib", "/lib64") if Path(system_root).exists()
        )
        for system_root in system_roots:
            bubblewrap_arguments.extend(
                ("--ro-bind", str(system_root), str(system_root))
            )
        if not self._covered_by_system_bind(base_prefix, system_roots):
            bubblewrap_arguments.extend(
                ("--ro-bind", str(base_prefix), str(base_prefix))
            )
        bubblewrap_arguments.extend(
            ("--ro-bind", str(root), "/plugin", "--chdir", "/plugin")
        )
        if broker_socket_path is not None:
            socket_path = broker_socket_path.resolve()
            if socket_path.is_symlink() or not socket_path.exists() or not socket_path.parent.is_dir():
                raise PluginExecutionError("core broker socket is missing or unsafe")
            bubblewrap_arguments.extend(
                ("--ro-bind", str(socket_path.parent), "/run/boyi-plugin-broker")
            )
        safe_environment = dict(environment)
        safe_environment["PATH"] = "/usr/bin:/bin"
        safe_environment["HOME"] = "/tmp"
        safe_environment["TMPDIR"] = "/tmp"
        if broker_socket_path is not None:
            safe_environment["BOYI_PLUGIN_BROKER_ENDPOINT"] = (
                f"unix:///run/boyi-plugin-broker/{broker_socket_path.name}"
            )
        for key, value in sorted(safe_environment.items()):
            bubblewrap_arguments.extend(("--setenv", key, value))
        bubblewrap_arguments.extend(
            (
                f"/plugin/{python_path.as_posix()}",
                f"/plugin/package/{entrypoint.as_posix()}",
            )
        )
        command = self._limited_command(bubblewrap_arguments)
        return await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=None,
            env={
                "PATH": os.defpath,
                "LANG": os.environ.get("LANG", "C.UTF-8"),
            },
            # Python must create the outer bwrap process group. Bubblewrap's
            # internal --new-session does not protect the Agent process group
            # from ExecutionRouter cancellation via killpg().
            start_new_session=True,
        )
