"""Claim extraction and canonicalization contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ExtractedClaim:
    content: str
    start_offset: int
    end_offset: int


@dataclass(frozen=True, slots=True)
class CanonicalClaim:
    claim_id: str
    canonical_text: str
    normalized_form: str
    entity_signature: str
    polarity: str


class ClaimExtractor(Protocol):
    version: str

    def extract(self, text: str) -> tuple[ExtractedClaim, ...]: ...


class ClaimCanonicalizer(Protocol):
    version: str

    def canonicalize(self, claim: ExtractedClaim, entity_signature: str) -> CanonicalClaim: ...

