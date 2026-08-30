"""One atomic composition cycle over existing derived-state owners."""

from __future__ import annotations

from pathlib import Path

from atomizer_local_client.chat.contracts import utc_now
from atomizer_local_client.claims.repository import ClaimRepository
from atomizer_local_client.contradictions.repository import ContradictionRepository
from atomizer_local_client.derived_state.contracts import DerivedStateCycle
from atomizer_local_client.derived_state.detector import (
    inspect_derived_state,
    projection_state,
)
from atomizer_local_client.entities.repository import EntityRepository
from atomizer_local_client.history.connection import database, transaction
from atomizer_local_client.semantic.contracts import EmbeddingBackend
from atomizer_local_client.semantic.embeddings import LocalFeatureHashEmbeddingBackend
from atomizer_local_client.semantic.units import reconcile_semantic_units
from atomizer_local_client.semantic.vector_index import SQLiteVectorIndex
from atomizer_local_client.temporal.repository import TemporalRepository
from atomizer_local_client.verification.repository import VerificationRepository


def run_derived_state_cycle(
    database_path: Path,
    backend: EmbeddingBackend | None = None,
) -> DerivedStateCycle:
    """Reconcile all projections in one SQLite transaction or roll back all of them."""
    backend = backend or LocalFeatureHashEmbeddingBackend()
    started_at = utc_now()
    with database(database_path) as connection:
        with transaction(connection):
            units = reconcile_semantic_units(connection)
            vectors = SQLiteVectorIndex(connection, backend).index(units)
            entity_mentions = EntityRepository(connection).rebuild()
            claim_evidence = ClaimRepository(connection).rebuild()
            temporal = TemporalRepository(connection)
            temporal_evidence = temporal.rebuild()
            contradiction_count, disputed_claims = ContradictionRepository(
                connection
            ).rebuild()
            temporal.mark_disputed(disputed_claims)
            verification_count = VerificationRepository(connection).rebuild()
        inspection = inspect_derived_state(connection, backend)
        projection_signature, projection_counts = projection_state(connection)
    return DerivedStateCycle(
        started_at=started_at,
        completed_at=utc_now(),
        source_signature=inspection.source_signature,
        projection_signature=projection_signature,
        authoritative_source_count=inspection.authoritative_source_count,
        semantic_unit_count=len(units),
        embeddings_indexed=vectors["indexed"],
        embeddings_unchanged=vectors["unchanged"],
        embeddings_failed=vectors["failed"],
        embeddings_invalidated=vectors["invalidated"],
        entity_mention_count=entity_mentions,
        claim_evidence_count=claim_evidence,
        temporal_evidence_count=temporal_evidence,
        contradiction_count=contradiction_count,
        verification_count=verification_count,
        projection_counts=projection_counts,
    )
