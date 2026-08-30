"""Small generic contracts for local lexical/vector result composition."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RankedEvidence:
    evidence_id: str
    semantic_unit_id: str
    source_type: str
    source_id: str
    project_id: str
    chat_id: str | None
    claim_id: str
    content: str
    lexical_rank: int | None = None
    lexical_score: float | None = None
    vector_rank: int | None = None
    vector_similarity: float | None = None
    fused_score: float = 0.0
    rerank_rank: int | None = None
    rerank_score: float | None = None


@dataclass(frozen=True, slots=True)
class RerankResult:
    candidates: tuple[RankedEvidence, ...]
    omitted: tuple[RankedEvidence, ...]
    input_count: int
    version: str
