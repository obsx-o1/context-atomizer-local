"""Bounded deterministic reranking over already fused Evidence candidates."""

from __future__ import annotations

from typing import Protocol, Sequence

from atomizer_local_client.retrieval.contracts import RankedEvidence, RerankResult

class Reranker(Protocol):
    version: str

    def rerank(
        self, candidates: Sequence[RankedEvidence], query: str
    ) -> RerankResult: ...


class IdentityReranker:
    """Default V1 reranker: bound the input and preserve exact RRF ordering."""

    version = "identity-reranker-v1"

    def __init__(self, *, max_candidates: int = 50) -> None:
        if not 1 <= max_candidates <= 100:
            raise ValueError("reranker max_candidates must be between 1 and 100")
        self.max_candidates = max_candidates

    def rerank(
        self, candidates: Sequence[RankedEvidence], query: str
    ) -> RerankResult:
        del query
        bounded = tuple(candidates[: self.max_candidates])
        return RerankResult(
            candidates=bounded,
            omitted=tuple(candidates[self.max_candidates :]),
            input_count=len(candidates),
            version=self.version,
        )
