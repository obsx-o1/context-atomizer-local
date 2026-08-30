"""Content-free contracts for derived-state inspection and maintenance."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DerivedStateInspection:
    source_signature: str
    projection_signature: str
    authoritative_source_count: int
    expected_unit_count: int
    pending_unit_count: int
    pending_embedding_count: int
    failed_embedding_count: int
    projection_counts: tuple[tuple[str, int], ...]
    requires_cycle: bool

    @property
    def pending_count(self) -> int:
        return self.pending_unit_count + self.pending_embedding_count


@dataclass(frozen=True, slots=True)
class DerivedStateCycle:
    started_at: str
    completed_at: str
    source_signature: str
    projection_signature: str
    authoritative_source_count: int
    semantic_unit_count: int
    embeddings_indexed: int
    embeddings_unchanged: int
    embeddings_failed: int
    embeddings_invalidated: int
    entity_mention_count: int
    claim_evidence_count: int
    temporal_evidence_count: int
    contradiction_count: int
    verification_count: int
    projection_counts: tuple[tuple[str, int], ...]

    @property
    def converged(self) -> bool:
        return self.embeddings_failed == 0
