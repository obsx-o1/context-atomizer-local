"""Short-lived, runtime- and scope-bound managed authority state."""

from __future__ import annotations

import hmac
import secrets
import threading
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
    scope_references: tuple[str, ...]
    expires_at: datetime

    def __post_init__(self) -> None:
        if not self.session_reference or len(self.session_reference) > 256:
            raise ValueError("managed session reference is invalid")
        if len(self.runtime_build) != 64:
            raise ValueError("managed runtime binding is invalid")
        if not self.scope_references or len(self.scope_references) > 64:
            raise ValueError("managed scope binding is invalid")
        if any(not value or len(value) > 256 for value in self.scope_references):
            raise ValueError("managed scope reference is invalid")


class ManagedAuthorityRegistry:
    """In-memory lease state; disconnect and expiry never reopen direct access."""

    def __init__(self, runtime_build: str) -> None:
        self.runtime_build = runtime_build
        self._lock = threading.RLock()
        self._session: VerifiedManagedSession | None = None
        self._capability: str | None = None

    def activate(self, session: VerifiedManagedSession) -> str:
        now = datetime.now(UTC)
        if session.runtime_build != self.runtime_build:
            raise PermissionError("managed session runtime binding mismatch")
        if _utc(session.expires_at) <= now:
            raise PermissionError("managed session authority is stale")
        capability = secrets.token_urlsafe(48)
        with self._lock:
            self._session = session
            self._capability = capability
        return capability

    def revoke(self) -> None:
        with self._lock:
            self._session = None
            self._capability = None

    def authority(self) -> ManagedAuthority:
        with self._lock:
            session = self._session
        if session is None or _utc(session.expires_at) <= datetime.now(UTC):
            return ManagedAuthority(verified_active=False)
        return ManagedAuthority(
            verified_active=True,
            expires_at=_utc(session.expires_at),
            session_reference=session.session_reference,
        )

    def require(self, capability: str, scope_reference: str | None = None) -> VerifiedManagedSession:
        with self._lock:
            session = self._session
            expected = self._capability
        if (
            session is None
            or expected is None
            or not isinstance(capability, str)
            or not hmac.compare_digest(capability, expected)
        ):
            raise PermissionError("verified managed capability is required")
        if _utc(session.expires_at) <= datetime.now(UTC):
            self.revoke()
            raise PermissionError("managed session authority has expired")
        if (
            scope_reference is not None
            and "*" not in session.scope_references
            and scope_reference not in session.scope_references
        ):
            raise PermissionError("managed session scope mismatch")
        return session

    def session_for_scope(self, scope_reference: str) -> VerifiedManagedSession:
        """Resolve a fresh lease for a hook request without disclosing its capability."""

        with self._lock:
            session = self._session
        if session is None or _utc(session.expires_at) <= datetime.now(UTC):
            raise PermissionError("verified managed authority is unavailable")
        if (
            "*" not in session.scope_references
            and scope_reference not in session.scope_references
        ):
            raise PermissionError("managed session scope mismatch")
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
        }


__all__ = ["ManagedAuthorityRegistry", "VerifiedManagedSession", "bound_scope"]
