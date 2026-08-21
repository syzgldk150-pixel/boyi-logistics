"""OS-level sandbox launchers for untrusted signed Python actions."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path, PurePosixPath
from typing import Mapping

from agent.automation_plugins.errors import PluginExecutionError


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
    ) -> None:
        target = Path(executable)
        if not target.is_absolute() or target.is_symlink() or not target.is_file():
            raise ValueError("bubblewrap executable must be one explicit regular file")
        self._executable = target.resolve()
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
        expected_home = self._trusted_base_prefix / "bin"
        if (
            not home.is_absolute()
            or home != expected_home
            or home.is_symlink()
            or not home.is_dir()
            or home.resolve() != expected_home
        ):
            raise PluginExecutionError("sandbox virtual environment base prefix is invalid")
        return self._trusted_base_prefix

    @staticmethod
    def _covered_by_system_bind(path: Path, system_roots: tuple[Path, ...]) -> bool:
        return any(path == root or root in path.parents for root in system_roots)

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
        command = [
            str(self._executable),
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
            command.extend(("--ro-bind", str(system_root), str(system_root)))
        if not self._covered_by_system_bind(base_prefix, system_roots):
            command.extend(("--ro-bind", str(base_prefix), str(base_prefix)))
        command.extend(("--ro-bind", str(root), "/plugin", "--chdir", "/plugin"))
        if broker_socket_path is not None:
            socket_path = broker_socket_path.resolve()
            if socket_path.is_symlink() or not socket_path.exists() or not socket_path.parent.is_dir():
                raise PluginExecutionError("core broker socket is missing or unsafe")
            command.extend(("--ro-bind", str(socket_path.parent), "/run/boyi-plugin-broker"))
        safe_environment = dict(environment)
        safe_environment["PATH"] = "/usr/bin:/bin"
        safe_environment["HOME"] = "/tmp"
        safe_environment["TMPDIR"] = "/tmp"
        if broker_socket_path is not None:
            safe_environment["BOYI_PLUGIN_BROKER_ENDPOINT"] = (
                f"unix:///run/boyi-plugin-broker/{broker_socket_path.name}"
            )
        for key, value in sorted(safe_environment.items()):
            command.extend(("--setenv", key, value))
        command.extend(
            (
                f"/plugin/{python_path.as_posix()}",
                f"/plugin/package/{entrypoint.as_posix()}",
            )
        )
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
