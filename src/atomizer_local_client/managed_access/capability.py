"""Purpose-bound HMAC verification for paired managed Library leases."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

from atomizer_local_client.managed_access.authority import VerifiedManagedSession


LEASE_SCHEMA_VERSION = "managed-library-lease.v1"
LEASE_PURPOSE = "managed_library"
LEASE_AUTHENTICATION_ALGORITHM = "hmac-sha256"
LEASE_AUTHENTICATION_DOMAIN = "context-atomizer-local/managed-library-lease/v1"
MAX_LEASE_SECONDS = 300
MAX_FUTURE_SKEW_SECONDS = 30
MAX_REPLAY_ENTRIES = 4096

_CAPABILITY_ID = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
_SIGNATURE = re.compile(r"[0-9a-f]{64}\Z")
_CLAIM_KEYS = frozenset(
    {
        "schema_version",
        "purpose",
        "opaque_manager_session_reference",
        "opaque_host_session_reference",
        "opaque_turn_reference",
        "permitted_scope_reference",
        "issued_at",
        "expires_at",
        "capability_id",
        "runtime_build",
        "runtime_instance_reference",
    }
)


def canonical_lease_claims(claims: Mapping[str, object]) -> bytes:
    """Return the one deterministic byte representation covered by HMAC."""

    return json.dumps(
        dict(claims),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def lease_authentication_material(claims: Mapping[str, object]) -> bytes:
    return LEASE_AUTHENTICATION_DOMAIN.encode("ascii") + b"\n" + canonical_lease_claims(
        claims
    )


def sign_lease(secret: str, claims: Mapping[str, object]) -> str:
    return hmac.new(
        secret.encode("ascii"), lease_authentication_material(claims), hashlib.sha256
    ).hexdigest()


def _timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise PermissionError(f"managed lease {name} is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise PermissionError(f"managed lease {name} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PermissionError(f"managed lease {name} is invalid")
    return parsed.astimezone(UTC)


def _text(value: object, name: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise PermissionError(f"managed lease {name} is invalid")
    return value


class _LeaseReplayState:
    """Bounded live-capability ledger that fails closed instead of evicting."""

    def __init__(self, maximum: int = MAX_REPLAY_ENTRIES) -> None:
        self.maximum = maximum
        self._entries: OrderedDict[str, datetime] = OrderedDict()
        self._lock = threading.Lock()

    def consume(self, capability_id: str, expires_at: datetime, now: datetime) -> None:
        with self._lock:
            for key, expiry in tuple(self._entries.items()):
                if expiry <= now:
                    self._entries.pop(key, None)
            if capability_id in self._entries:
                raise PermissionError("managed lease replay was rejected")
            if len(self._entries) >= self.maximum:
                raise PermissionError("managed lease replay state is full")
            self._entries[capability_id] = expires_at


class AuthenticatedManagedAssertionVerifier:
    """Verify a public lease only for the paired manager and this runtime."""

    def __init__(
        self,
        secret_provider: Callable[[], str | None],
        *,
        runtime_instance_reference: str,
        replay: _LeaseReplayState | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.secret_provider = secret_provider
        self.runtime_instance_reference = _text(
            runtime_instance_reference, "runtime instance"
        )
        self.replay = replay or _LeaseReplayState()
        self.now = now or (lambda: datetime.now(UTC))

    def verify(
        self, assertion: Mapping[str, object], *, runtime_build: str
    ) -> VerifiedManagedSession:
        if set(assertion) != {"claims", "authentication"}:
            raise PermissionError("managed lease envelope is invalid")
        claims = assertion.get("claims")
        authentication = assertion.get("authentication")
        if not isinstance(claims, Mapping) or not isinstance(authentication, Mapping):
            raise PermissionError("managed lease envelope is invalid")
        if set(claims) != _CLAIM_KEYS:
            raise PermissionError("managed lease claims are invalid")
        if set(authentication) != {"algorithm", "signature"}:
            raise PermissionError("managed lease authentication is invalid")
        if authentication.get("algorithm") != LEASE_AUTHENTICATION_ALGORITHM:
            raise PermissionError("managed lease authentication is invalid")
        signature = authentication.get("signature")
        if not isinstance(signature, str) or not _SIGNATURE.fullmatch(signature):
            raise PermissionError("managed lease authentication is invalid")
        secret = self.secret_provider()
        if not isinstance(secret, str) or len(secret) < 32:
            raise PermissionError("managed manager pairing is unavailable")
        expected = sign_lease(secret, claims)
        if not hmac.compare_digest(signature, expected):
            raise PermissionError("managed lease authentication failed")

        issued_at = _timestamp(claims.get("issued_at"), "issued_at")
        expires_at = _timestamp(claims.get("expires_at"), "expires_at")
        current = self.now().astimezone(UTC)
        if expires_at <= issued_at:
            raise PermissionError("managed lease time ordering is invalid")
        if issued_at > current + timedelta(seconds=MAX_FUTURE_SKEW_SECONDS):
            raise PermissionError("managed lease is not yet valid")
        if current >= expires_at:
            raise PermissionError("managed lease has expired")
        if expires_at - issued_at > timedelta(seconds=MAX_LEASE_SECONDS):
            raise PermissionError("managed lease lifetime is too long")
        if claims.get("schema_version") != LEASE_SCHEMA_VERSION:
            raise PermissionError("managed lease schema is unsupported")
        if claims.get("purpose") != LEASE_PURPOSE:
            raise PermissionError("managed lease purpose is invalid")
        if claims.get("runtime_build") != runtime_build:
            raise PermissionError("managed lease runtime binding mismatch")
        if claims.get("runtime_instance_reference") != self.runtime_instance_reference:
            raise PermissionError("managed lease runtime instance mismatch")

        capability_id = _text(claims.get("capability_id"), "capability id", 128)
        if not _CAPABILITY_ID.fullmatch(capability_id):
            raise PermissionError("managed lease capability id is invalid")
        session = VerifiedManagedSession(
            session_reference=_text(
                claims.get("opaque_manager_session_reference"), "manager session"
            ),
            runtime_build=runtime_build,
            runtime_instance_reference=self.runtime_instance_reference,
            scope_references=(
                _text(claims.get("permitted_scope_reference"), "scope"),
            ),
            host_session_reference=_text(
                claims.get("opaque_host_session_reference"), "host session"
            ),
            host_turn_reference=_text(
                claims.get("opaque_turn_reference"), "host turn"
            ),
            capability_id=capability_id,
            expires_at=expires_at,
        )
        self.replay.consume(capability_id, expires_at, current)
        return session


__all__ = [
    "AuthenticatedManagedAssertionVerifier",
    "LEASE_AUTHENTICATION_ALGORITHM",
    "LEASE_PURPOSE",
    "LEASE_SCHEMA_VERSION",
    "canonical_lease_claims",
    "lease_authentication_material",
    "sign_lease",
]
