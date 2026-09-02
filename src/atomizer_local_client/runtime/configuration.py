"""Local runtime paths, persisted configuration, and content-free state."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


DEFAULT_BRIDGE_PORT = 43117
DEFAULT_LIBRARY_PORT = 43118
DEFAULT_PORT_SPAN = 10


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(payload)
    if sys.platform == "darwin":
        os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write(path, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"))


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    app_data: Path
    database: Path
    config: Path
    permissions: Path
    state: Path
    credential: Path
    extension_credential: Path
    managed_credential: Path
    access_policy: Path
    lock: Path
    log: Path
    library_shortcut: Path

    @classmethod
    def for_root(cls, app_data: Path, *, shortcut: Path | None = None) -> "RuntimePaths":
        root = Path(app_data).resolve()
        return cls(
            app_data=root,
            database=root / "history.sqlite3",
            config=root / "runtime.json",
            permissions=root / "permissions.json",
            state=root / "runtime-state.json",
            credential=root / "management-credential.bin",
            extension_credential=root / "extension-pairing.bin",
            managed_credential=root / "managed-connector.bin",
            access_policy=root / "library-access-policy.json",
            lock=root / "runtime.lock",
            log=root / "logs" / "runtime.log",
            library_shortcut=(shortcut or root / "Context Atomizer Library.url").resolve(),
        )

    @classmethod
    def current_user(cls) -> "RuntimePaths":
        if sys.platform == "darwin":
            from atomizer_local_client.platforms.macos.paths import (
                current_user_locations,
            )

            locations = current_user_locations()
            return cls.for_root(
                locations.app_data, shortcut=locations.library_shortcut
            )
        local = os.environ.get("LOCALAPPDATA")
        roaming = os.environ.get("APPDATA")
        if not local or not roaming:
            raise RuntimeError("Windows per-user application directories are unavailable")
        shortcut = (
            Path(roaming)
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "Context Atomizer Local"
            / "Context Atomizer Library.lnk"
        )
        return cls.for_root(Path(local) / "ContextAtomizer", shortcut=shortcut)


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    bridge_port: int = DEFAULT_BRIDGE_PORT
    library_port: int = DEFAULT_LIBRARY_PORT
    port_span: int = DEFAULT_PORT_SPAN
    maintenance_interval_seconds: float = 2.0
    log_max_bytes: int = 1_048_576
    log_backup_count: int = 3

    def validate(self) -> None:
        if self.bridge_port != DEFAULT_BRIDGE_PORT:
            raise ValueError("the V1 capture bridge must use fixed port 43117")
        if self.library_port != DEFAULT_LIBRARY_PORT:
            raise ValueError("V1 Library discovery must begin at port 43118")
        if not 0 <= self.port_span <= DEFAULT_PORT_SPAN:
            raise ValueError("port span must be between 0 and 10")
        if not 0.1 <= self.maintenance_interval_seconds <= 3600.0:
            raise ValueError("maintenance interval must be between 0.1 and 3600 seconds")
        if not 65_536 <= self.log_max_bytes <= 10_485_760:
            raise ValueError("log size must be between 64 KiB and 10 MiB")
        if not 1 <= self.log_backup_count <= 10:
            raise ValueError("log backup count must be between 1 and 10")

    def save(self, path: Path) -> None:
        self.validate()
        write_json(path, asdict(self))

    @classmethod
    def load(cls, path: Path) -> "RuntimeConfig":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("runtime configuration must be an object")
        config = cls(**payload)
        config.validate()
        return config


def read_state(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def remove_state(
    path: Path, *, timeout_seconds: float = 3.0, retry_interval_seconds: float = 0.05
) -> None:
    """Remove runtime state despite transient Windows readers, within a strict bound."""
    if timeout_seconds < 0:
        raise ValueError("state removal timeout must be nonnegative")
    if retry_interval_seconds <= 0:
        raise ValueError("state removal retry interval must be positive")
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            Path(path).unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(retry_interval_seconds, remaining))
