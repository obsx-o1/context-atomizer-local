from __future__ import annotations

from test_support import TemporaryDatabaseTest, chat_event

from atomizer_local_client.chat.ingestion import ingest_chat_event
from atomizer_local_client.history.connection import database, transaction
from atomizer_local_client.semantic.embeddings import LocalFeatureHashEmbeddingBackend
from atomizer_local_client.semantic.units import reconcile_semantic_units
from atomizer_local_client.semantic.vector_index import SQLiteVectorIndex, decode_vector


class RecoveringEmbeddingBackend:
    version = "recovering-test-v1"
    model_sha256 = "a" * 64
    dimension = 3

    def __init__(self, failures: int) -> None:
        self.failures = failures

    def embed(self, text: str) -> tuple[float, ...]:
        del text
        if self.failures:
            self.failures -= 1
            raise RuntimeError("controlled embedding failure")
        return (1.0, 0.0, 0.0)


class SemanticVectorTests(TemporaryDatabaseTest):
    def test_units_and_vectors_are_idempotent_versioned_and_dimension_checked(self) -> None:
        ingest_chat_event(
            self.database_path,
            chat_event(event_id="semantic-1", content="A physician repaired the vehicle."),
        )
        backend = LocalFeatureHashEmbeddingBackend()
        with database(self.database_path) as connection:
            with transaction(connection):
                units = reconcile_semantic_units(connection)
                first = SQLiteVectorIndex(connection, backend).index(units)
            self.assertEqual(first["indexed"], 1)
            row = connection.execute(
                "SELECT dimension, model_sha256, vector FROM embedding_records"
            ).fetchone()
            self.assertEqual(int(row["dimension"]), backend.dimension)
            self.assertEqual(str(row["model_sha256"]), backend.model_sha256)
            self.assertEqual(len(decode_vector(bytes(row["vector"]), backend.dimension)), backend.dimension)
            with transaction(connection):
                second = SQLiteVectorIndex(connection, backend).index(
                    reconcile_semantic_units(connection)
                )
            self.assertEqual(second["unchanged"], 1)
            with self.assertRaisesRegex(ValueError, "dimension mismatch"):
                decode_vector(bytes(row["vector"]), backend.dimension + 1)

    def test_concept_normalization_gives_paraphrases_positive_similarity(self) -> None:
        backend = LocalFeatureHashEmbeddingBackend()
        left = backend.embed("The physician repaired the automobile")
        right = backend.embed("The doctor fixed the car")
        unrelated = backend.embed("Blue ocean weather patterns")
        similar = sum(a * b for a, b in zip(left, right))
        different = sum(a * b for a, b in zip(left, unrelated))
        self.assertGreater(similar, different)

    def test_projection_prunes_deleted_authoritative_source(self) -> None:
        ingest_chat_event(self.database_path, chat_event(event_id="semantic-delete"))
        backend = LocalFeatureHashEmbeddingBackend()
        with database(self.database_path) as connection:
            with transaction(connection):
                SQLiteVectorIndex(connection, backend).index(reconcile_semantic_units(connection))
            connection.execute("DELETE FROM messages")
            with transaction(connection):
                units = reconcile_semantic_units(connection)
                counts = SQLiteVectorIndex(connection, backend).index(units)
            self.assertEqual(counts["invalidated"], 0)  # unit cascade removed its vector
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM semantic_units").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM embedding_records").fetchone()[0], 0)

    def test_failed_null_vector_is_retried_and_converges_without_deadlock(self) -> None:
        ingest_chat_event(
            self.database_path,
            chat_event(event_id="semantic-retry", content="Retry local embedding."),
        )
        backend = RecoveringEmbeddingBackend(failures=1)
        with database(self.database_path) as connection:
            units = reconcile_semantic_units(connection)
            first = SQLiteVectorIndex(connection, backend).index(units)
            self.assertEqual(first["failed"], 1)
            failed = connection.execute(
                "SELECT state, vector FROM embedding_records"
            ).fetchone()
            self.assertEqual(failed["state"], "failed")
            self.assertIsNone(failed["vector"])

            second = SQLiteVectorIndex(connection, backend).index(units)
            self.assertEqual(second["indexed"], 1)
            indexed = connection.execute(
                "SELECT state, vector FROM embedding_records"
            ).fetchone()
            self.assertEqual(indexed["state"], "indexed")
            self.assertIsNotNone(indexed["vector"])

            always_failing = RecoveringEmbeddingBackend(failures=2)
            connection.execute("DELETE FROM embedding_records")
            self.assertEqual(SQLiteVectorIndex(connection, always_failing).index(units)["failed"], 1)
            self.assertEqual(SQLiteVectorIndex(connection, always_failing).index(units)["failed"], 1)
            persistent = connection.execute(
                "SELECT state, vector FROM embedding_records"
            ).fetchone()
            self.assertEqual(persistent["state"], "failed")
            self.assertIsNone(persistent["vector"])


if __name__ == "__main__":
    import unittest

    unittest.main()
