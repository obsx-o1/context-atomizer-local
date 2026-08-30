"""Deterministic Reciprocal Rank Fusion over evidence identities."""

from __future__ import annotations

from dataclasses import replace
from typing import Sequence

from atomizer_local_client.retrieval.contracts import RankedEvidence


class RRFFuser:
    version = "rrf-v1"

    def __init__(self, *, k: int = 60) -> None:
        if not 1 <= k <= 1000:
            raise ValueError("RRF k must be between 1 and 1000")
        self.k = k

    def fuse(self, lexical: Sequence[RankedEvidence], vector: Sequence[RankedEvidence]) -> tuple[RankedEvidence, ...]:
        combined: dict[str, RankedEvidence] = {}
        scores: dict[str, float] = {}
        for family in (lexical, vector):
            for rank, candidate in enumerate(family, 1):
                scores[candidate.evidence_id] = scores.get(candidate.evidence_id, 0.0) + 1.0 / (self.k + rank)
                prior = combined.get(candidate.evidence_id)
                if prior is None:
                    combined[candidate.evidence_id] = candidate
                else:
                    combined[candidate.evidence_id] = replace(
                        prior,
                        lexical_rank=prior.lexical_rank or candidate.lexical_rank,
                        lexical_score=prior.lexical_score if prior.lexical_score is not None else candidate.lexical_score,
                        vector_rank=prior.vector_rank or candidate.vector_rank,
                        vector_similarity=prior.vector_similarity if prior.vector_similarity is not None else candidate.vector_similarity,
                    )
        return tuple(
            replace(combined[evidence_id], fused_score=score)
            for evidence_id, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        )
