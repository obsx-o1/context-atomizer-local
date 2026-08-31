"""Exact ownership of one current-user LaunchAgent registration."""

from __future__ import annotations

import os
import plistlib
import shlex
from pathlib import Path
from typing import Any

from atomizer_local_client.platforms.macos.paths import current_user_locations


LABEL = "com.contextatomizer.local.runtime"


def _owned_program_arguments(arguments: list[str]) -> bool:
    executable = Path(arguments[0])
    if executable.name == "atomizer-local-runtime":
        return len(arguments) == 3 and arguments[1] == "--config"
    return (
        executable.name.startswith("python")
        and len(arguments) == 5
        and arguments[1:3]
        == ["-m", "atomizer_local_client.runtime.application"]
        and arguments[3] == "--config"
    )


def runtime_startup_command(command_prefix: list[str], config_path: Path) -> str:
    if not command_prefix:
        raise ValueError("runtime startup command is empty")
    executable = Path(command_prefix[0]).resolve()
    normalized = [str(executable), *command_prefix[1:]]
    if executable.name != "atomizer-local-runtime":
        if not executable.name.startswith("python") or normalized[1:3] != [
            "-m",
            "atomizer_local_client.runtime.application",
        ]:
            raise ValueError(
                "macOS startup must use the packaged runtime executable or runtime module"
            )
    return shlex.join([*normalized, "--config", str(Path(config_path).resolve())])


class MacOSLaunchAgentRegistration:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or current_user_locations().launch_agent)

    def _load(self) -> dict[str, Any] | None:
        try:
            payload = plistlib.loads(self.path.read_bytes())
        except FileNotFoundError:
            return None
        except (OSError, plistlib.InvalidFileException) as exc:
            raise RuntimeError("Atomizer LaunchAgent registration is malformed") from exc
        if not isinstance(payload, dict) or payload.get("Label") != LABEL:
            raise RuntimeError(
                "LaunchAgent path is not owned by Context Atomizer; registration was not changed"
            )
        arguments = payload.get("ProgramArguments")
        if (
            not isinstance(arguments, list)
            or not arguments
            or not all(isinstance(value, str) and value for value in arguments)
            or payload.get("RunAtLoad") is not True
        ):
            raise RuntimeError("Atomizer LaunchAgent registration is malformed")
        if not _owned_program_arguments(arguments):
            raise RuntimeError(
                "LaunchAgent command is not owned by Context Atomizer; registration was not changed"
            )
        return payload

    def install(self, command: str) -> None:
        arguments = shlex.split(command, posix=True)
        if not arguments:
            raise ValueError("runtime startup command is empty")
        if not _owned_program_arguments(arguments):
            raise ValueError("runtime startup command is not owned by Context Atomizer")
        self._load()
        payload = {
            "Label": LABEL,
            "ProcessType": "Background",
            "ProgramArguments": arguments,
            "RunAtLoad": True,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_bytes(plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True))
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)

    def read(self) -> str | None:
        payload = self._load()
        if payload is None:
            return None
        return shlex.join(payload["ProgramArguments"])

    def remove(self) -> None:
        if self._load() is not None:
            self.path.unlink()
