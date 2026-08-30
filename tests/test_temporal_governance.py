from __future__ import annotations

import ast
from pathlib import Path

from test_support import TemporaryDatabaseTest, chat_event

from atomizer_local_client.chat.ingestion import ingest_chat_event
from atomizer_local_client.claims.canonicalizer import ConservativeClaimCanonicalizer
from atomizer_local_client.claims.extractor import DeclarativeClaimExtractor
from atomizer_local_client.claims.repository import ClaimRepository
from atomizer_local_client.contradictions.repository import ContradictionRepository
from atomizer_local_client.entities.repository import EntityRepository
from atomizer_local_client.history.connection import database, transaction
from atomizer_local_client.library.document_registry import elect_file_source, sync_elected_source
from atomizer_local_client.semantic.units import reconcile_semantic_units
from atomizer_local_client.temporal.repository import TemporalRepository
from atomizer_local_client.verification.repository import VerificationRepository


def rebuild_state(connection) -> None:
    reconcile_semantic_units(connection)
    EntityRepository(connection).rebuild()
    ClaimRepository(connection).rebuild()
    temporal = TemporalRepository(connection)
    temporal.rebuild()
    _, disputed = ContradictionRepository(connection).rebuild()
    temporal.mark_disputed(disputed)
    VerificationRepository(connection).rebuild()


class TemporalGovernanceTests(TemporaryDatabaseTest):
    def _project(self) -> str:
        return ingest_chat_event(
            self.database_path,
            chat_event(event_id="stage4-project", content="The project has governed evidence."),
        ).project_id

    def _document(self, project_id: str, name: str, content: str):
        path = self.root / name
        path.write_text(content, encoding="utf-8")
        return path, elect_file_source(self.database_path, project_id, path)

    @staticmethod
    def _canonical_claim_id_for_document(connection, display_name: str) -> str:
        unit = connection.execute(
            """
            SELECT u.semantic_unit_id,u.content FROM semantic_units u
            JOIN documents d ON d.document_id=u.source_id
            WHERE u.source_type='elected_document' AND d.display_name=?
              AND u.source_revision=d.revision
            ORDER BY u.unit_index LIMIT 1
            """,
            (display_name,),
        ).fetchone()
        extracted = DeclarativeClaimExtractor().extract(str(unit["content"]))[0]
        signature = ",".join(
            str(row[0])
            for row in connection.execute(
                """
                SELECT DISTINCT entity_id FROM entity_mentions
                WHERE semantic_unit_id=? AND start_offset < ? AND end_offset > ?
                ORDER BY entity_id
                """,
                (unit["semantic_unit_id"], extracted.end_offset, extracted.start_offset),
            ).fetchall()
        )
        claim = ConservativeClaimCanonicalizer().canonicalize(extracted, signature)
        return claim.claim_id

    def test_document_supersession_preserves_historical_evidence_and_marks_state(self) -> None:
        project_id = self._project()
        path, source = self._document(project_id, "status.md", "Project status is red.")
        with database(self.database_path) as connection:
            with transaction(connection):
                rebuild_state(connection)
        path.write_text("Project status is green.", encoding="utf-8")
        sync_elected_source(self.database_path, source.source_id)
        with database(self.database_path) as connection:
            with transaction(connection):
                rebuild_state(connection)
            rows = connection.execute(
                """
                SELECT e.source_revision,e.content,t.state,t.valid_to,t.superseded_by
                FROM claim_evidence e JOIN temporal_evidence_state t ON t.evidence_id=e.evidence_id
                WHERE e.source_type='elected_document' ORDER BY e.source_revision
                """
            ).fetchall()
            self.assertEqual([str(row["state"]) for row in rows], ["superseded", "current"])
            self.assertIn("red", str(rows[0]["content"]))
            self.assertIn("green", str(rows[1]["content"]))
            self.assertTrue(rows[0]["valid_to"])
            self.assertTrue(rows[0]["superseded_by"])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM document_revision_history").fetchone()[0], 1)

    def test_current_explicit_values_and_polarity_create_unresolved_disputes(self) -> None:
        project_id = self._project()
        self._document(project_id, "limit-a.md", "limit = 10")
        self._document(project_id, "limit-b.md", "limit = 20")
        self._document(project_id, "feature-a.md", "Feature is enabled.")
        self._document(project_id, "feature-b.md", "Feature is not enabled.")
        with database(self.database_path) as connection:
            with transaction(connection):
                rebuild_state(connection)
            relations = connection.execute(
                "SELECT rule_name FROM contradiction_relations ORDER BY rule_name"
            ).fetchall()
            rules = [str(row[0]) for row in relations]
            self.assertIn("conflicting_explicit_value", rules)
            self.assertIn("opposing_polarity", rules)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM temporal_evidence_state WHERE state='disputed'"
                ).fetchone()[0],
                4,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM claim_verification_state WHERE state='disputed'"
                ).fetchone()[0],
                4,
            )

    def test_different_scope_and_compatible_wording_are_not_contradictions(self) -> None:
        project_id = self._project()
        self._document(project_id, "us.md", "US limit = 10")
        self._document(project_id, "eu.md", "EU limit = 20")
        self._document(project_id, "owner.md", "Project owner is Alice.")
        self._document(project_id, "status.md", "Project status is green.")
        with database(self.database_path) as connection:
            with transaction(connection):
                rebuild_state(connection)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM contradiction_relations").fetchone()[0], 0)

    def test_independence_requires_distinct_document_or_chat_session(self) -> None:
        project_id = self._project()
        self._document(project_id, "support-a.md", "Project Atlas supports local retrieval.")
        self._document(project_id, "support-b.md", "Project Atlas supports local retrieval.")
        ingest_chat_event(
            self.database_path,
            chat_event(event_id="repeat-a", chat="same-chat", content="Feature is enabled."),
        )
        ingest_chat_event(
            self.database_path,
            chat_event(event_id="repeat-b", chat="same-chat", content="Feature is enabled."),
        )
        with database(self.database_path) as connection:
            with transaction(connection):
                rebuild_state(connection)
            expected_claim_id = self._canonical_claim_id_for_document(
                connection, "support-a.md"
            )
            canonical = connection.execute(
                "SELECT normalized_form FROM claims WHERE claim_id=?",
                (expected_claim_id,),
            ).fetchone()
            self.assertIn("support local retrieval", str(canonical[0]))
            self.assertNotIn("supports local retrieval", str(canonical[0]))
            corroborated = connection.execute(
                """SELECT state,independent_source_count FROM claim_verification_state
                   WHERE claim_id=?""",
                (expected_claim_id,),
            ).fetchone()
            repeated = connection.execute(
                """SELECT v.state,v.independent_source_count FROM claim_verification_state v
                   JOIN claims c ON c.claim_id=v.claim_id WHERE c.normalized_form LIKE '%feature is enabled%'"""
            ).fetchone()
            self.assertEqual(str(corroborated["state"]), "corroborated")
            self.assertEqual(int(corroborated["independent_source_count"]), 2)
            self.assertEqual(str(repeated["state"]), "single_source")
            self.assertEqual(int(repeated["independent_source_count"]), 1)

    def test_rebuild_integrity_provenance_versions_and_network_boundary(self) -> None:
        project_id = self._project()
        self._document(project_id, "audit.md", "Project status is green.")
        with database(self.database_path) as connection:
            with transaction(connection):
                rebuild_state(connection)
            snapshot = tuple(connection.execute("SELECT * FROM temporal_evidence_state ORDER BY evidence_id"))
            with transaction(connection):
                rebuild_state(connection)
            self.assertEqual(len(snapshot), connection.execute("SELECT COUNT(*) FROM temporal_evidence_state").fetchone()[0])
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            orphan_queries = (
                "SELECT COUNT(*) FROM temporal_evidence_state t LEFT JOIN claim_evidence e ON e.evidence_id=t.evidence_id WHERE e.evidence_id IS NULL",
                "SELECT COUNT(*) FROM contradiction_relations r LEFT JOIN claims a ON a.claim_id=r.claim_a_id LEFT JOIN claims b ON b.claim_id=r.claim_b_id WHERE a.claim_id IS NULL OR b.claim_id IS NULL",
                "SELECT COUNT(*) FROM claim_verification_state v LEFT JOIN claims c ON c.claim_id=v.claim_id WHERE c.claim_id IS NULL",
            )
            self.assertTrue(all(connection.execute(query).fetchone()[0] == 0 for query in orphan_queries))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM temporal_evidence_state WHERE evaluator_version='' OR evaluator_version IS NULL").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM claim_verification_state WHERE evaluator_version='' OR evaluator_version IS NULL").fetchone()[0], 0)
        root = Path(__file__).resolve().parents[1] / "src" / "atomizer_local_client"
        forbidden = {"socket", "urllib", "requests", "httpx", "aiohttp"}
        for package in ("temporal", "contradictions", "verification"):
            for path in (root / package).glob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                imports = {
                    (alias.name if isinstance(node, ast.Import) else node.module or "").split(".")[0]
                    for node in ast.walk(tree)
                    if isinstance(node, (ast.Import, ast.ImportFrom))
                    for alias in (node.names if isinstance(node, ast.Import) else [ast.alias(name=node.module or "")])
                }
                self.assertTrue(imports.isdisjoint(forbidden), f"{path}: {imports & forbidden}")


if __name__ == "__main__":
    import unittest

    unittest.main()
