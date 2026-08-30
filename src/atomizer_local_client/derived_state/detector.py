"""Deterministic dirty detection over authoritative and rebuildable local state."""

from __future__ import annotations

import hashlib
import sqlite3

from atomizer_local_client.derived_state.contracts import DerivedStateInspection
from atomizer_local_client.semantic.contracts import EmbeddingBackend, SemanticUnit
from atomizer_local_client.semantic.units import (
    CHUNKER_VERSION,
    authoritative_sources,
    build_units,
)
from atomizer_local_client.semantic.vector_index import embedding_fingerprint

_PROJECTIONS = (
    ("entity_mentions", "mention_id"),
    ("claim_evidence", "evidence_id"),
    ("temporal_evidence_state", "evidence_id"),
    ("contradiction_relations", "claim_a_id || ':' || claim_b_id"),
    ("claim_verification_state", "claim_id"),
)


def _source_material(source: object) -> str:
    content_sha256 = hashlib.sha256(source.content.encode("utf-8")).hexdigest()
    return "\x1f".join(
        (
            source.source_type,
            source.source_id,
            source.project_id,
            source.chat_id or "",
            str(source.revision),
            content_sha256,
            source.updated_at,
        )
    )


def _source_signature(sources: tuple[object, ...], backend: EmbeddingBackend) -> str:
    digest = hashlib.sha256()
    digest.update(
        f"{CHUNKER_VERSION}\x1f{backend.version}\x1f{backend.model_sha256}\n".encode("utf-8")
    )
    for source in sources:
        digest.update(_source_material(source).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def projection_state(
    connection: sqlite3.Connection,
) -> tuple[str, tuple[tuple[str, int], ...]]:
    digest = hashlib.sha256()
    counts: list[tuple[str, int]] = []
    for table, identity in _PROJECTIONS:
        rows = connection.execute(
            f"SELECT {identity} AS identity FROM {table} ORDER BY identity"
        ).fetchall()
        counts.append((table, len(rows)))
        digest.update(f"{table}\x1f{len(rows)}\n".encode("utf-8"))
        for row in rows:
            digest.update(str(row["identity"]).encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest(), tuple(counts)


def _expected_units(sources: tuple[object, ...]) -> tuple[SemanticUnit, ...]:
    return tuple(unit for source in sources for unit in build_units(source))


def inspect_derived_state(
    connection: sqlite3.Connection,
    backend: EmbeddingBackend,
    *,
    last_source_signature: str | None = None,
    last_projection_signature: str | None = None,
) -> DerivedStateInspection:
    sources = authoritative_sources(connection)
    expected = _expected_units(sources)
    expected_by_id = {unit.semantic_unit_id: unit for unit in expected}
    existing = {
        str(row["semantic_unit_id"]): row
        for row in connection.execute(
            """SELECT semantic_unit_id,project_id,chat_id,source_revision,content_sha256,
                      chunker_version,source_updated_at FROM semantic_units"""
        ).fetchall()
    }
    pending_units = len(set(existing) ^ set(expected_by_id))
    for unit_id in set(existing) & set(expected_by_id):
        row = existing[unit_id]
        unit = expected_by_id[unit_id]
        if (
            str(row["project_id"]) != unit.project_id
            or (str(row["chat_id"]) if row["chat_id"] is not None else None) != unit.chat_id
            or int(row["source_revision"]) != unit.source_revision
            or str(row["content_sha256"]) != unit.content_sha256
            or str(row["chunker_version"]) != CHUNKER_VERSION
            or str(row["source_updated_at"]) != unit.source_updated_at
        ):
            pending_units += 1

    embedding_rows = {
        str(row["semantic_unit_id"]): row
        for row in connection.execute(
            """SELECT semantic_unit_id,state,backend_version,model_sha256,
                      content_fingerprint,vector FROM embedding_records"""
        ).fetchall()
    }
    pending_embeddings = len(set(embedding_rows) - set(expected_by_id))
    failed_embeddings = 0
    for unit_id, unit in expected_by_id.items():
        row = embedding_rows.get(unit_id)
        expected_fingerprint = embedding_fingerprint(backend, unit.content_sha256)
        if row is None or (
            str(row["state"]) != "indexed"
            or str(row["backend_version"]) != backend.version
            or str(row["model_sha256"]) != backend.model_sha256
            or str(row["content_fingerprint"]) != expected_fingerprint
            or row["vector"] is None
        ):
            pending_embeddings += 1
        if row is not None and str(row["state"]) == "failed":
            failed_embeddings += 1

    source_signature = _source_signature(sources, backend)
    projection_signature, projection_counts = projection_state(connection)
    requires_cycle = bool(
        last_source_signature is None
        or source_signature != last_source_signature
        or pending_units
        or pending_embeddings
        or (
            last_projection_signature is not None
            and projection_signature != last_projection_signature
        )
    )
    return DerivedStateInspection(
        source_signature=source_signature,
        projection_signature=projection_signature,
        authoritative_source_count=len(sources),
        expected_unit_count=len(expected),
        pending_unit_count=pending_units,
        pending_embedding_count=pending_embeddings,
        failed_embedding_count=failed_embeddings,
        projection_counts=projection_counts,
        requires_cycle=requires_cycle,
    )
