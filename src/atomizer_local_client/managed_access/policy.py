"""Persist the human-selected Library access mode outside the Library database."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from atomizer_local_client.memory_access.access_gate import (
    DirectLibraryAccessMode,
    LibraryAccessGate,
    LibraryCaller,
)
from atomizer_local_client.memory_access.contracts import ManagedAuthorityProvider


class LibraryAccessPolicyStore:
    """Small atomic policy store whose absent-file default preserves V1 behavior."""

    SCHEMA_VERSION = "context-atomizer-library-access-policy-v1"

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def mode(self) -> DirectLibraryAccessMode:
        with self._lock:
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return DirectLibraryAccessMode.DIRECT_LOCAL
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError("Library access policy is unreadable") from exc
            if not isinstance(payload, dict) or set(payload) != {
                "schema_version",
                "mode",
            }:
                raise ValueError("Library access policy must use the closed public schema")
            if payload.get("schema_version") != self.SCHEMA_VERSION:
                raise ValueError("unsupported Library access policy schema")
            return DirectLibraryAccessMode(payload.get("mode"))

    def set_mode(self, mode: DirectLibraryAccessMode) -> DirectLibraryAccessMode:
        selected = DirectLibraryAccessMode(mode)
        encoded = (
            json.dumps(
                {"schema_version": self.SCHEMA_VERSION, "mode": selected.value},
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_bytes(encoded)
            os.replace(temporary, self.path)
        return selected


class PolicyBackedAccessGate:
    """Resolve the persisted mode for every decision; authority never changes policy."""

    def __init__(
        self,
        policy: LibraryAccessPolicyStore,
        *,
        manager: ManagedAuthorityProvider,
    ) -> None:
        self.policy = policy
        self.manager = manager

    def authorize(self, caller: LibraryCaller, *, now=None):  # type: ignore[no-untyped-def]
        try:
            mode = self.policy.mode()
        except ValueError:
            mode = DirectLibraryAccessMode.DISABLED
        return LibraryAccessGate(mode, manager=self.manager).authorize(caller, now=now)


__all__ = ["LibraryAccessPolicyStore", "PolicyBackedAccessGate"]
