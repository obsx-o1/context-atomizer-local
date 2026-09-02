"""Closed public management operations for a separately verified manager."""

from __future__ import annotations

from datetime import UTC
from typing import Mapping, Protocol

from atomizer_local_client.managed_access.authority import (
    ManagedAuthorityRegistry,
    VerifiedManagedSession,
)
from atomizer_local_client.managed_access.broker import ManagedContextBroker
from atomizer_local_client.managed_access.policy import LibraryAccessPolicyStore
from atomizer_local_client.managed_access.reader import ManagedLibraryReader
from atomizer_local_client.memory_access.access_gate import DirectLibraryAccessMode


class ManagedAssertionVerifier(Protocol):
    """Private implementations may verify authority without exposing its format."""

    def verify(
        self, assertion: Mapping[str, object], *, runtime_build: str
    ) -> VerifiedManagedSession: ...


class RejectingManagedAssertionVerifier:
    def verify(
        self, assertion: Mapping[str, object], *, runtime_build: str
    ) -> VerifiedManagedSession:
        del assertion, runtime_build
        raise PermissionError("no managed authority verifier is configured")


class ManagedIngress:
    """Transport-independent operation router mounted on the existing bridge."""

    PREFIX = "/v1/managed/"

    def __init__(
        self,
        *,
        policy: LibraryAccessPolicyStore,
        authority: ManagedAuthorityRegistry,
        broker: ManagedContextBroker,
        reader: ManagedLibraryReader,
        verifier: ManagedAssertionVerifier | None = None,
    ) -> None:
        self.policy = policy
        self.authority = authority
        self.broker = broker
        self.reader = reader
        self.verifier = verifier or RejectingManagedAssertionVerifier()

    def status(self) -> dict[str, object]:
        return {
            "ok": True,
            "access_mode": self.policy.mode().value,
            "authority": self.authority.status(),
        }

    def post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        operation = path.removeprefix(self.PREFIX)
        if operation == "authority/activate":
            if self.policy.mode() != DirectLibraryAccessMode.MANAGED_EXCLUSIVE:
                raise PermissionError("managed-exclusive policy is not selected")
            assertion = payload.get("assertion")
            if not isinstance(assertion, dict):
                raise ValueError("managed authority assertion must be an object")
            session = self.verifier.verify(
                assertion, runtime_build=self.authority.runtime_build
            )
            capability = self.authority.activate(session)
            return {
                "ok": True,
                "manager_capability": capability,
                "session_reference": session.session_reference,
                "expires_at": session.expires_at.astimezone(UTC).isoformat().replace(
                    "+00:00", "Z"
                ),
            }
        if operation == "authority/revoke":
            self.authority.require(str(payload.get("manager_capability", "")))
            self.authority.revoke()
            self.broker.revoke()
            return {"ok": True}
        if operation == "context/request":
            if self.policy.mode() != DirectLibraryAccessMode.MANAGED_EXCLUSIVE:
                return {"status": "inactive", "context": None}
            return self.broker.request(payload)
        capability = str(payload.get("manager_capability", ""))
        if operation == "context/next":
            timeout = payload.get("timeout_seconds", 0.0)
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                raise ValueError("timeout_seconds must be a number")
            return self.broker.next(capability, float(timeout))
        if operation == "context/complete":
            return self.broker.complete(capability, payload)
        if operation == "library/read":
            scope = payload.get("scope_reference")
            host = payload.get("host")
            name = payload.get("operation")
            arguments = payload.get("arguments", {})
            if (
                not isinstance(host, str)
                or not isinstance(scope, str)
                or not isinstance(name, str)
            ):
                raise ValueError("managed Library operation binding is invalid")
            if not isinstance(arguments, dict):
                raise ValueError("managed Library arguments must be an object")
            return self.reader.call(capability, host, scope, name, arguments)
        raise ValueError("unknown managed operation")


__all__ = [
    "ManagedAssertionVerifier",
    "ManagedIngress",
    "RejectingManagedAssertionVerifier",
]
