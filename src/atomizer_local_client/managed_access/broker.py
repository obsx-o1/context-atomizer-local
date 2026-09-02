"""Turn-bound in-memory exchange for managed context."""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from atomizer_local_client.managed_access.authority import (
    ManagedAuthorityRegistry,
    bound_scope,
)


MAX_PROMPT_CHARACTERS = 32_768
MAX_CONTEXT_CHARACTERS = 8_000
REQUEST_TTL_SECONDS = 8
HOOK_WAIT_SECONDS = 3.0


def _text(value: object, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise ValueError(f"{name} exceeds {maximum} characters")
    return normalized


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class _PendingContext:
    request_id: str
    host: str
    host_session_reference: str
    host_turn_reference: str
    scope_reference: str
    prompt: str
    manager_session_reference: str
    expires_at: datetime
    assigned: bool = False
    result: str | None = None
    event: threading.Event | None = None

    def public_mapping(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "host": self.host,
            "host_session_reference": self.host_session_reference,
            "host_turn_reference": self.host_turn_reference,
            "scope_reference": self.scope_reference,
            "prompt": self.prompt,
            "manager_session_reference": self.manager_session_reference,
            "expires_at": _iso(self.expires_at),
        }


class ManagedContextBroker:
    """Never persists prompt or context; all failures return no injected context."""

    def __init__(self, authority: ManagedAuthorityRegistry) -> None:
        self.authority = authority
        self._condition = threading.Condition()
        self._pending: dict[str, _PendingContext] = {}

    def request(self, payload: dict[str, object]) -> dict[str, object]:
        lease = self.authority.authority()
        if not lease.verified_active or lease.session_reference is None:
            raise PermissionError("verified managed authority is unavailable")
        scope = _text(payload.get("scope_reference"), "scope_reference", 256)
        host = _text(payload.get("host"), "host", 64)
        session = self.authority.session_for_scope(bound_scope(host, scope))
        pending = _PendingContext(
            request_id=secrets.token_urlsafe(24),
            host=host,
            host_session_reference=_text(
                payload.get("host_session_reference"), "host_session_reference", 256
            ),
            host_turn_reference=_text(
                payload.get("host_turn_reference"), "host_turn_reference", 256
            ),
            scope_reference=scope,
            prompt=_text(payload.get("prompt"), "prompt", MAX_PROMPT_CHARACTERS),
            manager_session_reference=lease.session_reference,
            expires_at=datetime.now(UTC) + timedelta(seconds=REQUEST_TTL_SECONDS),
            event=threading.Event(),
        )
        with self._condition:
            self._prune_locked()
            self._pending[pending.request_id] = pending
            self._condition.notify_all()
        assert pending.event is not None
        pending.event.wait(HOOK_WAIT_SECONDS)
        with self._condition:
            current = self._pending.pop(pending.request_id, None)
        if current is None or current.result is None:
            return {"status": "unavailable", "context": None}
        return {
            "status": "available",
            "context": current.result,
            "request_id": current.request_id,
            "host_turn_reference": current.host_turn_reference,
            "scope_reference": current.scope_reference,
        }

    def next(self, capability: str, timeout_seconds: float = 0.0) -> dict[str, object]:
        session = self.authority.require(capability)
        timeout = max(0.0, min(float(timeout_seconds), 3.0))
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                self._prune_locked()
                for pending in self._pending.values():
                    if (
                        not pending.assigned
                        and pending.manager_session_reference == session.session_reference
                    ):
                        pending.assigned = True
                        return {"status": "available", "request": pending.public_mapping()}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {"status": "empty", "request": None}
                self._condition.wait(remaining)

    def complete(self, capability: str, payload: dict[str, object]) -> dict[str, object]:
        request_id = _text(payload.get("request_id"), "request_id", 256)
        scope = _text(payload.get("scope_reference"), "scope_reference", 256)
        with self._condition:
            pending = self._pending.get(request_id)
            if pending is None or pending.result is not None:
                raise PermissionError("managed context request is stale or replayed")
            session = self.authority.require(
                capability, bound_scope(pending.host, scope)
            )
            checks = {
                "manager session": pending.manager_session_reference
                == session.session_reference,
                "scope": pending.scope_reference == scope,
                "host": pending.host == payload.get("host"),
                "host session": pending.host_session_reference
                == payload.get("host_session_reference"),
                "host turn": pending.host_turn_reference
                == payload.get("host_turn_reference"),
                "assigned request": pending.assigned,
                "fresh request": pending.expires_at > datetime.now(UTC),
            }
            failed = [name for name, passed in checks.items() if not passed]
            if failed:
                raise PermissionError("managed context binding mismatch: " + ", ".join(failed))
            pending.result = _text(
                payload.get("context"), "context", MAX_CONTEXT_CHARACTERS
            )
            assert pending.event is not None
            pending.event.set()
        return {"status": "accepted", "request_id": request_id}

    def revoke(self) -> None:
        with self._condition:
            for pending in self._pending.values():
                if pending.event is not None:
                    pending.event.set()
            self._pending.clear()
            self._condition.notify_all()

    def _prune_locked(self) -> None:
        now = datetime.now(UTC)
        stale = [key for key, value in self._pending.items() if value.expires_at <= now]
        for key in stale:
            pending = self._pending.pop(key)
            if pending.event is not None:
                pending.event.set()


__all__ = [
    "HOOK_WAIT_SECONDS",
    "MAX_CONTEXT_CHARACTERS",
    "MAX_PROMPT_CHARACTERS",
    "ManagedContextBroker",
]
