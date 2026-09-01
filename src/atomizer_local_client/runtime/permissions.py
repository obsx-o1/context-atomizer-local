"""Local, fail-closed user permission state for supported capture integrations."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_SUPPORTED_INTEGRATIONS = ("chatgpt_web", "codex", "claude_code")


@dataclass(frozen=True, slots=True)
class IntegrationPermission:
    enabled: bool
    installed: bool | None = None


class PermissionStore:
    """Own one small Atomizer-local permission file; absent/corrupt state denies capture."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    @staticmethod
    def _defaults() -> dict[str, Any]:
        return {
            "version": 1,
            "integrations": {
                "chatgpt_web": {"enabled": False},
                "codex": {"enabled": False, "installed": False},
                "claude_code": {"enabled": False, "installed": False},
            },
        }

    def _read(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return self._defaults()
        if not isinstance(payload, dict) or payload.get("version") != 1:
            return self._defaults()
        integrations = payload.get("integrations")
        if not isinstance(integrations, dict):
            return self._defaults()
        normalized = self._defaults()
        for name in _SUPPORTED_INTEGRATIONS:
            value = integrations.get(name)
            if not isinstance(value, dict) or not isinstance(value.get("enabled"), bool):
                continue
            normalized["integrations"][name]["enabled"] = value["enabled"]
            if name == "codex" and isinstance(value.get("installed"), bool):
                normalized["integrations"][name]["installed"] = value["installed"]
            if name == "claude_code" and isinstance(value.get("installed"), bool):
                normalized["integrations"][name]["installed"] = value["installed"]
        return normalized

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, self.path)

    def snapshot(self) -> dict[str, IntegrationPermission]:
        with self._lock:
            payload = self._read()["integrations"]
            return {
                "chatgpt_web": IntegrationPermission(
                    enabled=bool(payload["chatgpt_web"]["enabled"])
                ),
                "codex": IntegrationPermission(
                    enabled=bool(payload["codex"]["enabled"]),
                    installed=bool(payload["codex"]["installed"]),
                ),
                "claude_code": IntegrationPermission(
                    enabled=bool(payload["claude_code"]["enabled"]),
                    installed=bool(payload["claude_code"]["installed"]),
                ),
            }

    def is_enabled(self, integration: str) -> bool:
        if integration not in _SUPPORTED_INTEGRATIONS:
            return False
        return self.snapshot()[integration].enabled

    def set_enabled(self, integration: str, enabled: bool) -> None:
        if integration not in _SUPPORTED_INTEGRATIONS:
            raise ValueError("unsupported integration")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        with self._lock:
            payload = self._read()
            payload["integrations"][integration]["enabled"] = enabled
            self._write(payload)

    def set_codex_installed(self, installed: bool) -> None:
        if not isinstance(installed, bool):
            raise ValueError("installed must be a boolean")
        with self._lock:
            payload = self._read()
            payload["integrations"]["codex"]["installed"] = installed
            self._write(payload)

    def set_claude_code_installed(self, installed: bool) -> None:
        if not isinstance(installed, bool):
            raise ValueError("installed must be a boolean")
        with self._lock:
            payload = self._read()
            payload["integrations"]["claude_code"]["installed"] = installed
            self._write(payload)
