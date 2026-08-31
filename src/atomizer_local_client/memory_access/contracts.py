"""Small public contracts for read-only Library access."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ManagedAuthority:
    """Content-free status supplied by a future verified manager connector."""

    verified_active: bool
    expires_at: datetime | None = None
    session_reference: str | None = None


class ManagedAuthorityProvider(Protocol):
    """Public seam only; no remote handshake or proprietary protocol lives here."""

    def authority(self) -> ManagedAuthority: ...


class InactiveManagedAuthorityProvider:
    def authority(self) -> ManagedAuthority:
        return ManagedAuthority(verified_active=False)
