"""Bounded local cosine retrieval over versioned SQLite vectors."""

from __future__ import annotations

import sqlite3

from atomizer_local_client.retrieval.contracts import RankedEvidence
from atomizer_local_client.semantic.contracts import EmbeddingBackend
from atomizer_local_client.semantic.vector_index import decode_vector


class VectorRetriever:
    def __init__(self, backend: EmbeddingBackend) -> None:
        self.backend = backend

    def retrieve(self, connection: sqlite3.Connection, query: str, *, limit: int = 100) -> tuple[RankedEvidence, ...]:
        query_vector = self.backend.embed(query)
        rows = connection.execute(
            """
            SELECT e.*,u.project_id,u.chat_id,r.dimension,r.vector,r.backend_version,r.model_sha256
            FROM embedding_records r JOIN semantic_units u ON u.semantic_unit_id=r.semantic_unit_id
            JOIN claim_evidence e ON e.semantic_unit_id=u.semantic_unit_id
            WHERE r.state IN ('indexed','unchanged') ORDER BY e.evidence_id
            """
        ).fetchall()
        scored = []
        for row in rows:
            if int(row["dimension"]) != self.backend.dimension:
                raise ValueError("vector dimension mismatch during retrieval")
            if str(row["backend_version"]) != self.backend.version or str(row["model_sha256"]) != self.backend.model_sha256:
                continue
            vector = decode_vector(bytes(row["vector"]), self.backend.dimension)
            similarity = sum(left * right for left, right in zip(query_vector, vector))
            scored.append((similarity, str(row["evidence_id"]), row))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return tuple(
            RankedEvidence(
                evidence_id=str(row["evidence_id"]), semantic_unit_id=str(row["semantic_unit_id"]),
                source_type=str(row["source_type"]), source_id=str(row["source_id"]),
                project_id=str(row["project_id"]), chat_id=str(row["chat_id"]) if row["chat_id"] else None,
                claim_id=str(row["claim_id"]), content=str(row["content"]),
                vector_rank=index + 1, vector_similarity=float(score),
            )
            for index, (score, _, row) in enumerate(scored[:limit])
        )
