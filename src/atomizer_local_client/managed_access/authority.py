"""Short-lived, runtime- and scope-bound managed authority state."""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from atomizer_local_client.memory_access.contracts import ManagedAuthority


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def bound_scope(host: str, scope_reference: str) -> str:
    if not host or ":" in host or not scope_reference:
        raise ValueError("managed host scope is invalid")
    return f"{host}:{scope_reference}"


@dataclass(frozen=True, slots=True)
class VerifiedManagedSession:
    """Generic result returned only by a configured authority verifier."""

    session_reference: str
    runtime_build: str
    runtime_instance_reference: str
    scope_references: tuple[str, ...]
    host_session_reference: str
    host_turn_reference: str
    capability_id: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if not self.session_reference or len(self.session_reference) > 256:
            raise ValueError("managed session reference is invalid")
        if len(self.runtime_build) != 64:
            raise ValueError("managed runtime binding is invalid")
        if not self.runtime_instance_reference or len(self.runtime_instance_reference) > 256:
            raise ValueError("managed runtime instance binding is invalid")
        if not self.scope_references or len(self.scope_references) > 64:
            raise ValueError("managed scope binding is invalid")
        if any(not value or len(value) > 256 for value in self.scope_references):
            raise ValueError("managed scope reference is invalid")
        if not self.host_session_reference or len(self.host_session_reference) > 256:
            raise ValueError("managed host session binding is invalid")
        if not self.host_turn_reference or len(self.host_turn_reference) > 256:
            raise ValueError("managed host turn binding is invalid")
        if not self.capability_id or len(self.capability_id) > 128:
            raise ValueError("managed capability identity is invalid")


class ManagedAuthorityRegistry:
    """In-memory lease state; disconnect and expiry never reopen direct access."""

    def __init__(
        self,
        runtime_build: str,
        *,
        runtime_instance_reference: str | None = None,
        disconnect_timeout_seconds: float = 10.0,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        if not 1.0 <= disconnect_timeout_seconds <= 60.0:
            raise ValueError("managed disconnect timeout is out of bounds")
        self.runtime_build = runtime_build
        self.runtime_instance_reference = (
            runtime_instance_reference or secrets.token_urlsafe(32)
        )
        self._lock = threading.RLock()
        self._session: VerifiedManagedSession | None = None
        self._capability: str | None = None
        self._last_seen = 0.0
        self.disconnect_timeout_seconds = disconnect_timeout_seconds
        self._monotonic_clock = monotonic_clock or time.monotonic

    def _connected_locked(self) -> bool:
        return (
            self._session is not None
            and self._last_seen > 0.0
            and float(self._monotonic_clock()) - self._last_seen
            <= self.disconnect_timeout_seconds
        )

    def activate(self, session: VerifiedManagedSession) -> str:
        now = datetime.now(UTC)
        if session.runtime_build != self.runtime_build:
            raise PermissionError("managed session runtime binding mismatch")
        if session.runtime_instance_reference != self.runtime_instance_reference:
            raise PermissionError("managed session runtime instance mismatch")
        if _utc(session.expires_at) <= now:
            raise PermissionError("managed session authority is stale")
        capability = secrets.token_urlsafe(48)
        with self._lock:
            self._session = session
            self._capability = capability
            self._last_seen = float(self._monotonic_clock())
        return capability

    def revoke(self) -> None:
        with self._lock:
            self._session = None
            self._capability = None
            self._last_seen = 0.0

    def authority(self) -> ManagedAuthority:
        with self._lock:
            session = self._session
            connected = self._connected_locked()
            if (
                session is None
                or not connected
                or _utc(session.expires_at) <= datetime.now(UTC)
            ):
                self._session = None
                self._capability = None
                self._last_seen = 0.0
                return ManagedAuthority(verified_active=False)
            return ManagedAuthority(
                verified_active=True,
                expires_at=_utc(session.expires_at),
                session_reference=session.session_reference,
            )

    def require(
        self,
        capability: str,
        scope_reference: str | None = None,
        *,
        host_session_reference: str | None = None,
        host_turn_reference: str | None = None,
    ) -> VerifiedManagedSession:
        with self._lock:
            session = self._session
            expected = self._capability
            connected = self._connected_locked()
            if (
                session is None
                or expected is None
                or not connected
                or not isinstance(capability, str)
                or not hmac.compare_digest(capability, expected)
            ):
                raise PermissionError("verified managed capability is required")
            if _utc(session.expires_at) <= datetime.now(UTC):
                self._session = None
                self._capability = None
                self._last_seen = 0.0
                raise PermissionError("managed session authority has expired")
            if (
                scope_reference is not None
                and "*" not in session.scope_references
                and scope_reference not in session.scope_references
            ):
                raise PermissionError("managed session scope mismatch")
            if (
                host_session_reference is not None
                and not hmac.compare_digest(
                    host_session_reference, session.host_session_reference
                )
            ):
                raise PermissionError("managed host session mismatch")
            if (
                host_turn_reference is not None
                and not hmac.compare_digest(
                    host_turn_reference, session.host_turn_reference
                )
            ):
                raise PermissionError("managed host turn mismatch")
            self._last_seen = float(self._monotonic_clock())
            return session

    def session_for_scope(
        self,
        scope_reference: str,
        *,
        host_session_reference: str,
        host_turn_reference: str,
    ) -> VerifiedManagedSession:
        """Resolve a fresh lease for a hook request without disclosing its capability."""

        with self._lock:
            session = self._session
            connected = self._connected_locked()
            if (
                session is None
                or not connected
                or _utc(session.expires_at) <= datetime.now(UTC)
            ):
                self._session = None
                self._capability = None
                self._last_seen = 0.0
                raise PermissionError("verified managed authority is unavailable")
            if (
                "*" not in session.scope_references
                and scope_reference not in session.scope_references
            ):
                raise PermissionError("managed session scope mismatch")
            if not hmac.compare_digest(
                host_session_reference, session.host_session_reference
            ):
                raise PermissionError("managed host session mismatch")
            if not hmac.compare_digest(
                host_turn_reference, session.host_turn_reference
            ):
                raise PermissionError("managed host turn mismatch")
            return session

    def status(self) -> dict[str, object]:
        authority = self.authority()
        return {
            "verified_active": authority.verified_active,
            "expires_at": (
                authority.expires_at.isoformat().replace("+00:00", "Z")
                if authority.expires_at is not None
                else None
            ),
            "session_reference": authority.session_reference,
            "runtime_build": self.runtime_build,
            "runtime_instance_reference": self.runtime_instance_reference,
        }


__all__ = ["ManagedAuthorityRegistry", "VerifiedManagedSession", "bound_scope"]
