"""OS-level sandbox launchers for untrusted signed Python actions."""

from __future__ import annotations

import asyncio
import os
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

    def __init__(self, executable: Path | str) -> None:
        target = Path(executable)
        if not target.is_absolute() or target.is_symlink() or not target.is_file():
            raise ValueError("bubblewrap executable must be one explicit regular file")
        self._executable = target.resolve()

    @staticmethod
    def _relative(value: str, label: str) -> PurePosixPath:
        path = PurePosixPath(value)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise PluginExecutionError(f"sandbox {label} path is unsafe")
        return path

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
        for system_root in ("/usr", "/bin", "/lib", "/lib64"):
            if Path(system_root).exists():
                command.extend(("--ro-bind", system_root, system_root))
        command.extend(("--ro-bind", str(root), "/plugin", "--chdir", "/plugin", "--clearenv"))
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
