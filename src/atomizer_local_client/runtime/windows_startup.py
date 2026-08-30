"""Standard current-user Windows logon startup registration."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "ContextAtomizerLocal"


def runtime_startup_command(command_prefix: list[str], config_path: Path) -> str:
    if not command_prefix:
        raise ValueError("runtime startup command is empty")
    executable = Path(command_prefix[0]).resolve()
    if executable.name.casefold() not in {"atomizer-local-runtime.exe", "pythonw.exe"}:
        raise ValueError("startup must use a windowless packaged runtime executable")
    normalized = [str(executable), *command_prefix[1:]]
    if executable.name.casefold() == "pythonw.exe" and normalized[1:3] != [
        "-m",
        "atomizer_local_client.runtime.application",
    ]:
        raise ValueError("pythonw startup must use the packaged runtime module")
    command = subprocess.list2cmdline(
        [*normalized, "--config", str(Path(config_path).resolve())]
    )
    if len(command) > 260:
        raise ValueError("Windows Run command exceeds the supported 260-character limit")
    return command


class WindowsRunRegistration:
    def __init__(self, registry: Any | None = None) -> None:
        if registry is None:
            import winreg

            registry = winreg
        self.registry = registry

    def install(self, command: str) -> None:
        with self.registry.CreateKey(self.registry.HKEY_CURRENT_USER, RUN_KEY) as key:
            self.registry.SetValueEx(key, VALUE_NAME, 0, self.registry.REG_SZ, command)

    def read(self) -> str | None:
        try:
            with self.registry.OpenKey(self.registry.HKEY_CURRENT_USER, RUN_KEY) as key:
                value, _ = self.registry.QueryValueEx(key, VALUE_NAME)
            return str(value)
        except FileNotFoundError:
            return None

    def remove(self) -> None:
        try:
            with self.registry.OpenKey(
                self.registry.HKEY_CURRENT_USER,
                RUN_KEY,
                0,
                self.registry.KEY_SET_VALUE,
            ) as key:
                self.registry.DeleteValue(key, VALUE_NAME)
        except FileNotFoundError:
            return
