"""Temporal state contracts consumed by local derived state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TemporalState(StrEnum):
    CURRENT = "current"
    SUPERSEDED = "superseded"
    EXPIRED_OR_INVALID = "expired_or_invalid"
    DISPUTED = "disputed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class EvidenceTemporalState:
    evidence_id: str
    state: TemporalState
    observed_at: str
    valid_from: str | None
    valid_to: str | None
    superseded_by: str | None
