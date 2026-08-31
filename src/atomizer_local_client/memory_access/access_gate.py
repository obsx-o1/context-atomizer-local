"""Deterministic direct-versus-managed Library access boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from atomizer_local_client.memory_access.contracts import (
    InactiveManagedAuthorityProvider,
    ManagedAuthorityProvider,
)


class DirectLibraryAccessMode(StrEnum):
    DIRECT_LOCAL = "DIRECT_LOCAL"
    MANAGED_EXCLUSIVE = "MANAGED_EXCLUSIVE"
    DISABLED = "DISABLED"


class LibraryCaller(StrEnum):
    DIRECT_FRONTIER = "DIRECT_FRONTIER"
    TRUSTED_MANAGER = "TRUSTED_MANAGER"


@dataclass(frozen=True, slots=True)
class AccessDecision:
    allowed: bool
    status: str
    message: str


class LibraryAccessGate:
    """Fail-closed access decision with no client-supplied privilege fields."""

    def __init__(
        self,
        mode: DirectLibraryAccessMode = DirectLibraryAccessMode.DIRECT_LOCAL,
        *,
        manager: ManagedAuthorityProvider | None = None,
    ) -> None:
        self.mode = DirectLibraryAccessMode(mode)
        self.manager = manager or InactiveManagedAuthorityProvider()

    def authorize(
        self,
        caller: LibraryCaller,
        *,
        now: datetime | None = None,
    ) -> AccessDecision:
        caller = LibraryCaller(caller)
        if self.mode == DirectLibraryAccessMode.DISABLED:
            return AccessDecision(False, "disabled", "Direct Library access is disabled.")

        if caller == LibraryCaller.DIRECT_FRONTIER:
            if self.mode == DirectLibraryAccessMode.DIRECT_LOCAL:
                return AccessDecision(True, "available", "Direct local Library access is available.")
            return AccessDecision(
                False,
                "managed_exclusive",
                "Direct Library access is delegated to a trusted manager for this session.",
            )

        authority = self.manager.authority()
        moment = now or datetime.now(timezone.utc)
        expiry = authority.expires_at
        if expiry is not None:
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry <= moment:
                return AccessDecision(False, "manager_authority_expired", "Trusted manager authority has expired.")
        if not authority.verified_active:
            return AccessDecision(False, "manager_not_verified", "Trusted manager authority is not verified and active.")
        return AccessDecision(True, "available", "Verified trusted manager access is available.")
