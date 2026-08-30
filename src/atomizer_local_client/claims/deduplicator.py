"""High-threshold claim equivalence that never removes evidence."""

from __future__ import annotations

from dataclasses import dataclass

from atomizer_local_client.claims.contracts import CanonicalClaim
from atomizer_local_client.semantic.embeddings import LocalFeatureHashEmbeddingBackend


@dataclass(frozen=True, slots=True)
class EquivalenceDecision:
    claim_id: str
    decision: str
    confidence: float


class ConservativeClaimDeduplicator:
    version = "claim-equivalence-v1"
    threshold = 0.94

    def __init__(self) -> None:
        self.backend = LocalFeatureHashEmbeddingBackend()

    def decide(self, candidate: CanonicalClaim, existing: tuple[CanonicalClaim, ...]) -> EquivalenceDecision:
        for claim in existing:
            if (
                claim.normalized_form == candidate.normalized_form
                and claim.entity_signature == candidate.entity_signature
                and claim.polarity == candidate.polarity
            ):
                return EquivalenceDecision(claim.claim_id, "exact", 1.0)
        if not candidate.entity_signature:
            return EquivalenceDecision(candidate.claim_id, "separate", 0.0)
        vector = self.backend.embed(candidate.normalized_form)
        best: tuple[float, CanonicalClaim] | None = None
        for claim in existing:
            if claim.entity_signature != candidate.entity_signature or claim.polarity != candidate.polarity:
                continue
            other = self.backend.embed(claim.normalized_form)
            score = sum(left * right for left, right in zip(vector, other))
            if best is None or score > best[0]:
                best = (score, claim)
        if best is not None and best[0] >= self.threshold:
            return EquivalenceDecision(best[1].claim_id, "high_confidence_semantic", best[0])
        return EquivalenceDecision(candidate.claim_id, "separate", best[0] if best else 0.0)
