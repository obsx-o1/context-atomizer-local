"""Content-free runtime identity and application-health diagnostics."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import socket
import sqlite3
import sys
from pathlib import Path
from typing import Any


RUNTIME_PROTOCOL_VERSION = "1"
BUILD_IDENTITY_FILENAME = "runtime-build-identity.json"


def runtime_version() -> str:
    """Return the installed package version from the canonical package metadata."""

    try:
        return importlib.metadata.version("context-atomizer-local-client")
    except importlib.metadata.PackageNotFoundError:
        return "uninstalled"


def _runtime_files(package_root: Path) -> list[Path]:
    files = list(package_root.rglob("*.py"))
    migration_root = package_root / "history" / "migrations"
    if migration_root.exists():
        files.extend(migration_root.glob("*.sql"))
    return sorted(
        (path for path in files if "__pycache__" not in path.parts),
        key=lambda path: path.relative_to(package_root).as_posix(),
    )


def runtime_build_fingerprint(package_root: Path | None = None) -> str:
    """Return the deterministic package-build identity embedded in a frozen build."""

    root = (package_root or Path(__file__).resolve().parent).resolve()
    identity_path = root / BUILD_IDENTITY_FILENAME
    if identity_path.is_file():
        payload = json.loads(identity_path.read_text(encoding="utf-8"))
        value = payload.get("runtime_build_fingerprint")
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("embedded runtime build identity is invalid")
        return value
    digest = hashlib.sha256()
    for path in _runtime_files(root):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def runtime_build_sha256(package_root: Path | None = None) -> str:
    """Compatibility alias for the explicitly named runtime build fingerprint."""

    return runtime_build_fingerprint(package_root)


def runtime_executable_sha256(executable: Path | None = None) -> str | None:
    """Hash the frozen executable identity, or an explicit executable in tests/builds."""

    if executable is None and not getattr(sys, "frozen", False):
        return None
    path = Path(executable or sys.executable).resolve()
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


class RuntimeIdentity:
    """Bind the on-disk runtime fingerprint at process startup."""

    def __init__(self, package_root: Path | None = None) -> None:
        self.package_root = (package_root or Path(__file__).resolve().parent).resolve()
        self.startup_build_fingerprint = runtime_build_fingerprint(self.package_root)
        self.startup_build_sha256 = self.startup_build_fingerprint
        self.executable_path = (
            Path(sys.executable).resolve() if getattr(sys, "frozen", False) else None
        )
        self.startup_executable_sha256 = runtime_executable_sha256(self.executable_path)

    def snapshot(self) -> dict[str, Any]:
        try:
            current = runtime_build_fingerprint(self.package_root)
            current_executable = runtime_executable_sha256(self.executable_path)
            error_class = None
        except OSError as exc:
            current = None
            current_executable = None
            error_class = type(exc).__name__
        return {
            "runtime_version": runtime_version(),
            "protocol_version": RUNTIME_PROTOCOL_VERSION,
            "runtime_build_fingerprint": self.startup_build_fingerprint,
            "current_runtime_build_fingerprint": current,
            "runtime_executable_sha256": self.startup_executable_sha256,
            "current_runtime_executable_sha256": current_executable,
            "startup_build_sha256": self.startup_build_sha256,
            "current_build_sha256": current,
            "restart_required": (
                current != self.startup_build_fingerprint
                or current_executable != self.startup_executable_sha256
            ),
            "error_class": error_class,
        }


def database_health(database_path: Path) -> dict[str, Any]:
    """Return a small, content-free read-only SQLite health snapshot."""

    path = database_path.resolve()
    if not path.exists():
        return {
            "state": "uninitialized",
            "healthy": True,
            "integrity_check": None,
            "foreign_key_violations": 0,
            "error_class": None,
        }
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2.0)
        integrity_rows = connection.execute("PRAGMA quick_check").fetchall()
        integrity = "ok" if integrity_rows == [("ok",)] else "failed"
        foreign_key_violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        healthy = integrity == "ok" and foreign_key_violations == 0
        return {
            "state": "healthy" if healthy else "error",
            "healthy": healthy,
            "integrity_check": integrity,
            "foreign_key_violations": foreign_key_violations,
            "error_class": None,
        }
    except sqlite3.Error as exc:
        return {
            "state": "error",
            "healthy": False,
            "integrity_check": "unavailable",
            "foreign_key_violations": None,
            "error_class": type(exc).__name__,
        }
    finally:
        if connection is not None:
            connection.close()


def bridge_reachable(port: int, *, timeout_seconds: float = 0.15) -> bool:
    """Check only whether the loopback bridge listener is reachable."""

    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout_seconds):
            return True
    except OSError:
        return False
