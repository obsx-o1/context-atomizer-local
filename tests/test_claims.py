from __future__ import annotations

import ast
from pathlib import Path

from test_support import TemporaryDatabaseTest, chat_event

from atomizer_local_client.chat.ingestion import ingest_chat_event
from atomizer_local_client.claims.canonicalizer import ConservativeClaimCanonicalizer
from atomizer_local_client.claims.contracts import ExtractedClaim
from atomizer_local_client.claims.repository import (
    CLAIM_EVIDENCE_COLUMNS,
    ClaimRepository,
    ClaimRepositoryIntegrityError,
)
from atomizer_local_client.entities.repository import EntityRepository
from atomizer_local_client.history.connection import database, transaction
from atomizer_local_client.library.document_registry import elect_file_source, sync_elected_source
from atomizer_local_client.semantic.embeddings import LocalFeatureHashEmbeddingBackend
from atomizer_local_client.semantic.units import reconcile_semantic_units
from atomizer_local_client.semantic.vector_index import SQLiteVectorIndex
from atomizer_local_client.temporal.repository import TemporalRepository


class ClaimTests(TemporaryDatabaseTest):
    def test_repository_claim_evidence_contract_matches_migration_schema(self) -> None:
        with database(self.database_path) as connection:
            actual = tuple(
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(claim_evidence)")
            )
        self.assertEqual(actual, CLAIM_EVIDENCE_COLUMNS)

    def test_equivalent_claims_share_identity_and_keep_both_evidence_edges(self) -> None:
        ingest_chat_event(self.database_path, chat_event(event_id="claim-1", content="Context Atomizer supports local retrieval."))
        ingest_chat_event(self.database_path, chat_event(event_id="claim-2", content="Context Atomizer supports local retrieval."))
        with database(self.database_path) as connection:
            with transaction(connection):
                reconcile_semantic_units(connection)
                EntityRepository(connection).rebuild()
                ClaimRepository(connection).rebuild()
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM claim_evidence").fetchone()[0], 2)
            self.assertEqual(
                connection.execute("SELECT COUNT(DISTINCT source_id) FROM claim_evidence").fetchone()[0], 2
            )
            rows = connection.execute(
                """
                SELECT e.source_id, e.source_revision, e.semantic_unit_id,
                       e.source_timestamp, e.extractor_version, e.equivalence_version,
                       c.polarity
                FROM claim_evidence e JOIN claims c ON c.claim_id=e.claim_id
                ORDER BY e.source_id
                """
            ).fetchall()
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(int(row["source_revision"]) == 1 for row in rows))
            self.assertTrue(all(str(row["source_timestamp"]) for row in rows))
            self.assertTrue(all(str(row["semantic_unit_id"]) for row in rows))
            self.assertTrue(all(str(row["extractor_version"]) for row in rows))
            self.assertTrue(all(str(row["equivalence_version"]) for row in rows))
            self.assertTrue(all(str(row["polarity"]) == "positive" for row in rows))

            with transaction(connection):
                rebuilt = ClaimRepository(connection).rebuild()
            self.assertEqual(rebuilt, 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM claim_evidence").fetchone()[0], 2)

    def test_uncertain_claims_remain_separate(self) -> None:
        ingest_chat_event(self.database_path, chat_event(event_id="claim-a", content="Project Atlas uses SQLite."))
        ingest_chat_event(self.database_path, chat_event(event_id="claim-b", content="Project Atlas uses PostgreSQL."))
        with database(self.database_path) as connection:
            with transaction(connection):
                reconcile_semantic_units(connection)
                EntityRepository(connection).rebuild()
                ClaimRepository(connection).rebuild()
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM claim_evidence").fetchone()[0], 2)

    def test_document_revision_replaces_derived_claims_without_orphans(self) -> None:
        receipt = ingest_chat_event(
            self.database_path,
            chat_event(event_id="claim-project", content="The project has a document."),
        )
        document = self.root / "policy.md"
        document.write_text("Project Atlas uses SQLite.", encoding="utf-8")
        source = elect_file_source(self.database_path, receipt.project_id, document)
        with database(self.database_path) as connection:
            with transaction(connection):
                reconcile_semantic_units(connection)
                EntityRepository(connection).rebuild()
                ClaimRepository(connection).rebuild()
            old = connection.execute(
                "SELECT evidence_id, semantic_unit_id FROM claim_evidence "
                "WHERE source_type='elected_document'"
            ).fetchone()
            self.assertIsNotNone(old)
            old_evidence_id = str(old["evidence_id"])
            old_unit_id = str(old["semantic_unit_id"])

        document.write_text("Project Atlas uses PostgreSQL.", encoding="utf-8")
        sync_elected_source(self.database_path, source.source_id)
        with database(self.database_path) as connection:
            with transaction(connection):
                reconcile_semantic_units(connection)
                EntityRepository(connection).rebuild()
                ClaimRepository(connection).rebuild()
                TemporalRepository(connection).rebuild()
            current = connection.execute(
                """
                SELECT e.evidence_id,e.semantic_unit_id,e.source_revision,e.content
                FROM claim_evidence e
                JOIN temporal_evidence_state t ON t.evidence_id=e.evidence_id
                JOIN documents d ON d.document_id=e.source_id
                WHERE e.source_type='elected_document' AND t.state='current'
                  AND e.source_revision=d.revision
                """
            ).fetchone()
            self.assertEqual(int(current["source_revision"]), 2)
            self.assertIn("PostgreSQL", str(current["content"]))
            self.assertNotEqual(str(current["evidence_id"]), old_evidence_id)
            self.assertNotEqual(str(current["semantic_unit_id"]), old_unit_id)
            historical = connection.execute(
                """
                SELECT e.semantic_unit_id,e.source_revision,t.state
                FROM claim_evidence e JOIN temporal_evidence_state t
                  ON t.evidence_id=e.evidence_id
                WHERE e.evidence_id=?
                """,
                (old_evidence_id,),
            ).fetchone()
            self.assertEqual(str(historical["semantic_unit_id"]), old_unit_id)
            self.assertEqual(int(historical["source_revision"]), 1)
            self.assertEqual(str(historical["state"]), "superseded")
            before = {
                "claims": tuple(connection.execute("SELECT * FROM claims ORDER BY claim_id")),
                "evidence": tuple(connection.execute("SELECT * FROM claim_evidence ORDER BY evidence_id")),
                "temporal": tuple(
                    connection.execute(
                        "SELECT evidence_id,state,observed_at,valid_from,valid_to,superseded_by,evaluator_version "
                        "FROM temporal_evidence_state ORDER BY evidence_id"
                    )
                ),
            }
            with transaction(connection):
                reconcile_semantic_units(connection)
                EntityRepository(connection).rebuild()
                ClaimRepository(connection).rebuild()
                TemporalRepository(connection).rebuild()
            after = {
                "claims": tuple(connection.execute("SELECT * FROM claims ORDER BY claim_id")),
                "evidence": tuple(connection.execute("SELECT * FROM claim_evidence ORDER BY evidence_id")),
                "temporal": tuple(
                    connection.execute(
                        "SELECT evidence_id,state,observed_at,valid_from,valid_to,superseded_by,evaluator_version "
                        "FROM temporal_evidence_state ORDER BY evidence_id"
                    )
                ),
            }
            self.assertEqual(before, after)
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            orphan_queries = (
                "SELECT COUNT(*) FROM claim_evidence e LEFT JOIN claims c ON c.claim_id=e.claim_id WHERE c.claim_id IS NULL",
                "SELECT COUNT(*) FROM claim_evidence e LEFT JOIN semantic_units u ON u.semantic_unit_id=e.semantic_unit_id WHERE u.semantic_unit_id IS NULL",
                "SELECT COUNT(*) FROM claim_equivalence_decisions d LEFT JOIN claim_evidence e ON e.evidence_id=d.evidence_id WHERE e.evidence_id IS NULL",
                "SELECT COUNT(*) FROM semantic_units u LEFT JOIN messages m ON u.source_type='chat_message' AND m.message_id=u.source_id LEFT JOIN documents d ON u.source_type='elected_document' AND d.document_id=u.source_id WHERE (u.source_type='chat_message' AND m.message_id IS NULL) OR (u.source_type='elected_document' AND d.document_id IS NULL)",
            )
            self.assertTrue(
                all(connection.execute(query).fetchone()[0] == 0 for query in orphan_queries)
            )

    def test_polarity_and_entity_signature_are_canonical_identity_dimensions(self) -> None:
        canonicalizer = ConservativeClaimCanonicalizer()
        positive_a = canonicalizer.canonicalize(
            ExtractedClaim("Project Atlas uses SQLite.", 0, 26), "entity-a"
        )
        negative_a = canonicalizer.canonicalize(
            ExtractedClaim("Project Atlas does not use SQLite.", 0, 34), "entity-a"
        )
        positive_b = canonicalizer.canonicalize(
            ExtractedClaim("Project Atlas uses SQLite.", 0, 26), "entity-b"
        )
        self.assertNotEqual(positive_a.claim_id, negative_a.claim_id)
        self.assertNotEqual(positive_a.claim_id, positive_b.claim_id)
        self.assertEqual(positive_a.polarity, "positive")
        self.assertEqual(negative_a.polarity, "negative")

    def test_existing_canonical_tuple_with_wrong_id_fails_explicitly_and_rolls_back(self) -> None:
        ingest_chat_event(
            self.database_path,
            chat_event(event_id="claim-mismatch", content="Project Atlas uses SQLite."),
        )
        canonicalizer = ConservativeClaimCanonicalizer()
        candidate = canonicalizer.canonicalize(
            ExtractedClaim("Project Atlas uses SQLite.", 0, 26), ""
        )
        with database(self.database_path) as connection:
            with transaction(connection):
                reconcile_semantic_units(connection)
                EntityRepository(connection).rebuild()
                connection.execute(
                    """
                    INSERT INTO claims(
                        claim_id, canonical_text, normalized_form, entity_signature,
                        polarity, canonicalizer_version
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "wrong-deterministic-id", candidate.canonical_text,
                        candidate.normalized_form, candidate.entity_signature,
                        candidate.polarity, canonicalizer.version,
                    ),
                )
            before = connection.execute("SELECT COUNT(*) FROM claim_evidence").fetchone()[0]
            with self.assertRaisesRegex(
                ClaimRepositoryIntegrityError, "different deterministic claim_id"
            ):
                with transaction(connection):
                    ClaimRepository(connection)._get_or_create_claim(candidate)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM claim_evidence").fetchone()[0], before)

    def test_canonicalizer_version_change_reconciles_to_new_deterministic_identity(self) -> None:
        class VersionTwoCanonicalizer(ConservativeClaimCanonicalizer):
            version = "conservative-claim-v2-test"

        ingest_chat_event(
            self.database_path,
            chat_event(event_id="claim-version", content="Project Atlas uses SQLite."),
        )
        with database(self.database_path) as connection:
            with transaction(connection):
                reconcile_semantic_units(connection)
                EntityRepository(connection).rebuild()
                ClaimRepository(connection).rebuild()
            original_id = str(connection.execute("SELECT claim_id FROM claims").fetchone()[0])
            with transaction(connection):
                ClaimRepository(
                    connection, canonicalizer=VersionTwoCanonicalizer()
                ).rebuild()
            row = connection.execute(
                "SELECT claim_id, canonicalizer_version FROM claims"
            ).fetchone()
            self.assertNotEqual(str(row["claim_id"]), original_id)
            self.assertEqual(str(row["canonicalizer_version"]), VersionTwoCanonicalizer.version)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM claim_evidence").fetchone()[0], 1)

    def test_independent_evidence_preserves_claim_when_one_document_revision_changes(self) -> None:
        receipt = ingest_chat_event(
            self.database_path,
            chat_event(event_id="independent-chat", content="Project Atlas uses SQLite."),
        )
        document = self.root / "independent.md"
        document.write_text("Project Atlas uses SQLite.", encoding="utf-8")
        source = elect_file_source(self.database_path, receipt.project_id, document)
        with database(self.database_path) as connection:
            with transaction(connection):
                reconcile_semantic_units(connection)
                EntityRepository(connection).rebuild()
                ClaimRepository(connection).rebuild()
            claim_id = str(
                connection.execute(
                    "SELECT claim_id FROM claims WHERE normalized_form LIKE '%sqlite%'"
                ).fetchone()[0]
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM claim_evidence WHERE claim_id=?", (claim_id,)
                ).fetchone()[0],
                2,
            )

        document.write_text("Project Atlas uses PostgreSQL.", encoding="utf-8")
        sync_elected_source(self.database_path, source.source_id)
        with database(self.database_path) as connection:
            with transaction(connection):
                reconcile_semantic_units(connection)
                EntityRepository(connection).rebuild()
                ClaimRepository(connection).rebuild()
                TemporalRepository(connection).rebuild()
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM claim_evidence WHERE claim_id=?", (claim_id,)
                ).fetchone()[0],
                2,
            )
            states = {
                str(row[0]): int(row[1])
                for row in connection.execute(
                    """
                    SELECT t.state,COUNT(*) FROM claim_evidence e
                    JOIN temporal_evidence_state t ON t.evidence_id=e.evidence_id
                    WHERE e.claim_id=? GROUP BY t.state
                    """,
                    (claim_id,),
                ).fetchall()
            }
            self.assertEqual(states, {"current": 1, "superseded": 1})
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM claims WHERE claim_id=?", (claim_id,)
                ).fetchone()[0],
                1,
            )
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0], 2)

    def test_orphan_claim_is_removed_only_after_its_last_current_evidence_disappears(self) -> None:
        ingest_chat_event(
            self.database_path,
            chat_event(event_id="orphan-a", content="Project Atlas uses SQLite."),
        )
        ingest_chat_event(
            self.database_path,
            chat_event(event_id="orphan-b", content="Project Atlas uses SQLite."),
        )
        with database(self.database_path) as connection:
            with transaction(connection):
                reconcile_semantic_units(connection)
                EntityRepository(connection).rebuild()
                ClaimRepository(connection).rebuild()
            claim_id = str(connection.execute("SELECT claim_id FROM claims").fetchone()[0])
            connection.execute("DELETE FROM messages WHERE message_id='orphan-a'")
            with transaction(connection):
                reconcile_semantic_units(connection)
                EntityRepository(connection).rebuild()
                ClaimRepository(connection).rebuild()
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM claims WHERE claim_id=?", (claim_id,)).fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM claim_evidence").fetchone()[0], 1)
            connection.execute("DELETE FROM messages WHERE message_id='orphan-b'")
            with transaction(connection):
                reconcile_semantic_units(connection)
                EntityRepository(connection).rebuild()
                ClaimRepository(connection).rebuild()
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM claim_evidence").fetchone()[0], 0)

    def test_rebuild_requires_transaction_and_claim_modules_have_no_network_imports(self) -> None:
        with database(self.database_path) as connection:
            with self.assertRaisesRegex(RuntimeError, "active transaction"):
                ClaimRepository(connection).rebuild()
        package_root = Path(__file__).resolve().parents[1]
        forbidden = {"socket", "urllib", "requests", "httpx", "aiohttp"}
        for package in ("semantic", "entities", "claims"):
            for path in (package_root / "src" / "atomizer_local_client" / package).glob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                imports: set[str] = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imports.update(alias.name.split(".")[0] for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imports.add(node.module.split(".")[0])
                self.assertTrue(imports.isdisjoint(forbidden), f"{path}: {imports & forbidden}")

    def test_disposable_stage_1_to_3_integrity_provenance_and_terminal_state_gate(self) -> None:
        receipt = ingest_chat_event(
            self.database_path,
            chat_event(event_id="stage-gate", content="Project Atlas supports local retrieval."),
        )
        document = self.root / "stage-gate.md"
        document.write_text("Project Atlas supports local retrieval.", encoding="utf-8")
        elect_file_source(self.database_path, receipt.project_id, document)
        with database(self.database_path) as connection:
            versions = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
            )
            required = {
                "001_initial", "002_document_sources", "003_document_supersession",
                "004_semantic_vector", "005_entities", "006_claims",
            }
            self.assertEqual(versions, tuple(sorted(versions)))
            self.assertEqual(len(versions), len(set(versions)))
            self.assertTrue(required.issubset(versions))
            with transaction(connection):
                units = reconcile_semantic_units(connection)
                vector_counts = SQLiteVectorIndex(
                    connection, LocalFeatureHashEmbeddingBackend()
                ).index(units)
                EntityRepository(connection).rebuild()
                ClaimRepository(connection).rebuild()
            self.assertEqual(vector_counts["failed"], 0)
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            audits = {
                "semantic_source_orphan": """
                    SELECT COUNT(*) FROM semantic_units u
                    LEFT JOIN messages m ON u.source_type='chat_message' AND m.message_id=u.source_id
                    LEFT JOIN chats c ON m.chat_id=c.chat_id
                    LEFT JOIN documents d ON u.source_type='elected_document' AND d.document_id=u.source_id
                    WHERE (u.source_type='chat_message' AND
                           (m.message_id IS NULL OR u.source_revision<>1 OR u.chat_id<>m.chat_id OR u.project_id<>c.project_id))
                       OR (u.source_type='elected_document' AND
                           (d.document_id IS NULL OR u.source_revision<>d.revision OR u.chat_id IS NOT NULL OR u.project_id<>d.project_id))
                """,
                "embedding_orphan_or_nonterminal": """
                    SELECT COUNT(*) FROM embedding_records e
                    LEFT JOIN semantic_units u ON u.semantic_unit_id=e.semantic_unit_id
                    WHERE u.semantic_unit_id IS NULL OR e.state NOT IN ('indexed','unchanged','failed','invalidated')
                """,
                "unit_without_embedding_terminal": """
                    SELECT COUNT(*) FROM semantic_units u
                    LEFT JOIN embedding_records e ON e.semantic_unit_id=u.semantic_unit_id
                    WHERE e.semantic_unit_id IS NULL
                """,
                "entity_mention_orphan": """
                    SELECT COUNT(*) FROM entity_mentions m
                    LEFT JOIN entities e ON e.entity_id=m.entity_id
                    LEFT JOIN semantic_units u ON u.semantic_unit_id=m.semantic_unit_id
                    WHERE e.entity_id IS NULL OR u.semantic_unit_id IS NULL
                       OR m.source_id<>u.source_id OR m.source_revision<>u.source_revision
                """,
                "claim_evidence_orphan_or_mismatch": """
                    SELECT COUNT(*) FROM claim_evidence e
                    LEFT JOIN claims c ON c.claim_id=e.claim_id
                    LEFT JOIN semantic_units u ON u.semantic_unit_id=e.semantic_unit_id
                    WHERE c.claim_id IS NULL OR u.semantic_unit_id IS NULL
                       OR e.source_type<>u.source_type OR e.source_id<>u.source_id
                       OR e.source_revision<>u.source_revision OR e.source_timestamp<>u.source_updated_at
                """,
                "claim_without_evidence": """
                    SELECT COUNT(*) FROM claims c LEFT JOIN claim_evidence e ON e.claim_id=c.claim_id
                    WHERE e.evidence_id IS NULL
                """,
                "decision_orphan": """
                    SELECT COUNT(*) FROM claim_equivalence_decisions d
                    LEFT JOIN claim_evidence e ON e.evidence_id=d.evidence_id
                    WHERE e.evidence_id IS NULL OR d.claim_id<>e.claim_id
                """,
            }
            failures = {
                name: int(connection.execute(query).fetchone()[0])
                for name, query in audits.items()
            }
            self.assertEqual(failures, {name: 0 for name in audits})


if __name__ == "__main__":
    import unittest

    unittest.main()
