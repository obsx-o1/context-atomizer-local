"""Transactional SQLite persistence for versioned local vectors."""

from __future__ import annotations

import hashlib
import sqlite3
import struct
from datetime import datetime, timezone
from typing import Sequence

from atomizer_local_client.semantic.contracts import EmbeddingBackend, SemanticUnit


def embedding_fingerprint(backend: EmbeddingBackend, content_sha256: str) -> str:
    return hashlib.sha256(
        f"{backend.version}\x1f{backend.model_sha256}\x1f{content_sha256}".encode("utf-8")
    ).hexdigest()


def encode_vector(values: tuple[float, ...]) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


def decode_vector(payload: bytes, dimension: int) -> tuple[float, ...]:
    expected = dimension * 4
    if len(payload) != expected:
        raise ValueError("stored vector dimension mismatch")
    return tuple(struct.unpack(f"<{dimension}f", payload))


class SQLiteVectorIndex:
    def __init__(self, connection: sqlite3.Connection, backend: EmbeddingBackend) -> None:
        self.connection = connection
        self.backend = backend

    def index(self, units: Sequence[SemanticUnit]) -> dict[str, int]:
        counts = {"indexed": 0, "unchanged": 0, "failed": 0, "invalidated": 0}
        active = {unit.semantic_unit_id for unit in units}
        for row in self.connection.execute("SELECT semantic_unit_id FROM embedding_records").fetchall():
            if str(row[0]) not in active:
                self.connection.execute("DELETE FROM embedding_records WHERE semantic_unit_id = ?", (row[0],))
                counts["invalidated"] += 1
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        for unit in units:
            fingerprint = embedding_fingerprint(self.backend, unit.content_sha256)
            prior = self.connection.execute(
                "SELECT content_fingerprint, dimension, vector, state "
                "FROM embedding_records WHERE semantic_unit_id = ?",
                (unit.semantic_unit_id,),
            ).fetchone()
            if (
                prior is not None
                and str(prior[0]) == fingerprint
                and str(prior[3]) == "indexed"
                and prior[2] is not None
            ):
                decode_vector(bytes(prior[2]), int(prior[1]))
                counts["unchanged"] += 1
                continue
            try:
                vector = self.backend.embed(unit.content)
                if len(vector) != self.backend.dimension:
                    raise ValueError("embedding backend dimension mismatch")
                payload = encode_vector(vector)
            except Exception as error:
                self.connection.execute(
                    """
                    INSERT INTO embedding_records VALUES (?, 'failed', ?, ?, ?, ?, NULL, ?, ?)
                    ON CONFLICT(semantic_unit_id) DO UPDATE SET state='failed', backend_version=excluded.backend_version,
                    model_sha256=excluded.model_sha256, dimension=excluded.dimension,
                    content_fingerprint=excluded.content_fingerprint, vector=NULL,
                    error_class=excluded.error_class, updated_at=excluded.updated_at
                    """,
                    (unit.semantic_unit_id, self.backend.version, self.backend.model_sha256,
                     self.backend.dimension, fingerprint, type(error).__name__[:128], now),
                )
                counts["failed"] += 1
                continue
            self.connection.execute(
                """
                INSERT INTO embedding_records VALUES (?, 'indexed', ?, ?, ?, ?, ?, NULL, ?)
                ON CONFLICT(semantic_unit_id) DO UPDATE SET state='indexed', backend_version=excluded.backend_version,
                model_sha256=excluded.model_sha256, dimension=excluded.dimension,
                content_fingerprint=excluded.content_fingerprint, vector=excluded.vector,
                error_class=NULL, updated_at=excluded.updated_at
                """,
                (unit.semantic_unit_id, self.backend.version, self.backend.model_sha256,
                 self.backend.dimension, fingerprint, payload, now),
            )
            counts["indexed"] += 1
        return counts
