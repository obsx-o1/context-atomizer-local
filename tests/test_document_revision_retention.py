from __future__ import annotations

import hashlib

from test_support import TemporaryDatabaseTest, chat_event

from atomizer_local_client.chat.ingestion import ingest_chat_event
from atomizer_local_client.derived_state.cycle import run_derived_state_cycle
from atomizer_local_client.history.connection import database
from atomizer_local_client.library.document_reader import list_documents
from atomizer_local_client.library.document_registry import (
    elect_file_source,
    sync_elected_source,
    unelect_source,
)


class DocumentRevisionRetentionTests(TemporaryDatabaseTest):
    def setUp(self) -> None:
        super().setUp()
        self.project_id = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="revision-retention-project",
                content="revision retention project seed",
            ),
        ).project_id

    def _document_id(self, name: str) -> str:
        documents = {
            str(row["display_name"]): str(row["document_id"])
            for row in list_documents(self.database_path, self.project_id)
        }
        return documents[name]

    def _history(self, document_id: str) -> list[tuple[int, str]]:
        with database(self.database_path) as connection:
            rows = connection.execute(
                "SELECT revision, text_content FROM document_revision_history "
                "WHERE document_id = ? ORDER BY revision",
                (document_id,),
            ).fetchall()
        return [(int(row["revision"]), str(row["text_content"])) for row in rows]

    def _revise(self, path, source_id: str, revision: int) -> None:
        path.write_text(
            f"Revision {revision} states RetentionEntity{revision} remains active.",
            encoding="utf-8",
        )
        result = sync_elected_source(self.database_path, source_id)
        self.assertEqual((result.updated, result.unchanged), (1, 0))

    def test_pre_cap_unchanged_library_converges_on_normal_reconciliation(self) -> None:
        primary = self.root / "legacy.md"
        other = self.root / "other-legacy.md"
        current_content = "Revision 15 states LegacyRetentionEntity15 remains active."
        primary.write_text(current_content, encoding="utf-8")
        other.write_text("Other current revision remains stable.", encoding="utf-8")
        primary_source = elect_file_source(
            self.database_path, self.project_id, primary
        )
        elect_file_source(self.database_path, self.project_id, other)
        primary_id = self._document_id("legacy.md")
        other_id = self._document_id("other-legacy.md")

        with database(self.database_path) as connection:
            connection.execute(
                "UPDATE documents SET revision = 15 WHERE document_id = ?",
                (primary_id,),
            )
            connection.execute(
                "UPDATE documents SET revision = 2 WHERE document_id = ?",
                (other_id,),
            )
            for revision in range(1, 15):
                content = (
                    f"Revision {revision} states "
                    f"LegacyRetentionEntity{revision} remains active."
                )
                connection.execute(
                    "INSERT INTO document_revision_history("
                    "document_id, project_id, revision, text_content, content_sha256, "
                    "observed_at, superseded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        primary_id,
                        self.project_id,
                        revision,
                        content,
                        hashlib.sha256(content.encode("utf-8")).hexdigest(),
                        f"2026-01-{revision:02d}T00:00:00+00:00",
                        f"2026-01-{revision + 1:02d}T00:00:00+00:00",
                    ),
                )
            other_history = "Other historical revision remains stable."
            connection.execute(
                "INSERT INTO document_revision_history("
                "document_id, project_id, revision, text_content, content_sha256, "
                "observed_at, superseded_at) VALUES (?, ?, 1, ?, ?, ?, ?)",
                (
                    other_id,
                    self.project_id,
                    other_history,
                    hashlib.sha256(other_history.encode("utf-8")).hexdigest(),
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-02T00:00:00+00:00",
                ),
            )
            other_before = tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM documents WHERE document_id = ? UNION ALL "
                    "SELECT NULL, project_id, NULL, NULL, NULL, text_content, observed_at, "
                    "NULL, content_sha256, NULL, NULL, NULL, NULL, superseded_at, revision "
                    "FROM document_revision_history WHERE document_id = ? ORDER BY revision",
                    (other_id, other_id),
                )
            )

        run_derived_state_cycle(self.database_path)
        with database(self.database_path) as connection:
            for table in ("semantic_units", "entity_mentions", "claim_evidence"):
                self.assertGreater(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} "
                        "WHERE source_type = 'elected_document' AND source_id = ? "
                        "AND source_revision BETWEEN 1 AND 4",
                        (primary_id,),
                    ).fetchone()[0],
                    0,
                )

        result = sync_elected_source(self.database_path, primary_source.source_id)
        self.assertEqual((result.updated, result.unchanged), (0, 1))
        retained = self._history(primary_id)
        self.assertEqual([revision for revision, _ in retained], list(range(5, 15)))

        repeated = sync_elected_source(self.database_path, primary_source.source_id)
        self.assertEqual((repeated.updated, repeated.unchanged), (0, 1))
        self.assertEqual(self._history(primary_id), retained)

        run_derived_state_cycle(self.database_path)
        with database(self.database_path) as connection:
            current = connection.execute(
                "SELECT revision, text_content FROM documents WHERE document_id = ?",
                (primary_id,),
            ).fetchone()
            self.assertEqual(
                (int(current["revision"]), str(current["text_content"])),
                (15, current_content),
            )
            other_after = tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM documents WHERE document_id = ? UNION ALL "
                    "SELECT NULL, project_id, NULL, NULL, NULL, text_content, observed_at, "
                    "NULL, content_sha256, NULL, NULL, NULL, NULL, superseded_at, revision "
                    "FROM document_revision_history WHERE document_id = ? ORDER BY revision",
                    (other_id, other_id),
                )
            )
            self.assertEqual(other_after, other_before)
            semantic_revisions = {
                int(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT source_revision FROM semantic_units "
                    "WHERE source_type = 'elected_document' AND source_id = ?",
                    (primary_id,),
                )
            }
            self.assertEqual(semantic_revisions, set(range(5, 16)))
            for table in ("entity_mentions", "claim_evidence"):
                self.assertEqual(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} "
                        "WHERE source_type = 'elected_document' AND source_id = ? "
                        "AND source_revision BETWEEN 1 AND 4",
                        (primary_id,),
                    ).fetchone()[0],
                    0,
                )
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
            self.assertEqual(
                connection.execute("PRAGMA quick_check").fetchone()[0], "ok"
            )
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            lexical = connection.execute(
                "SELECT rowid, content FROM lexical_entries ORDER BY rowid"
            ).fetchall()
            fts = connection.execute(
                "SELECT rowid, content FROM lexical_entries_fts ORDER BY rowid"
            ).fetchall()
            self.assertEqual([tuple(row) for row in lexical], [tuple(row) for row in fts])

        with database(self.database_path) as reopened:
            reopened_current = reopened.execute(
                "SELECT revision, text_content FROM documents WHERE document_id = ?",
                (primary_id,),
            ).fetchone()
            reopened_history = reopened.execute(
                "SELECT revision FROM document_revision_history "
                "WHERE document_id = ? ORDER BY revision",
                (primary_id,),
            ).fetchall()
        self.assertEqual(
            (int(reopened_current["revision"]), str(reopened_current["text_content"])),
            (15, current_content),
        )
        self.assertEqual(
            [int(row["revision"]) for row in reopened_history], list(range(5, 15))
        )

    def test_current_plus_newest_ten_history_is_deterministic_and_source_scoped(self) -> None:
        primary = self.root / "primary.md"
        other = self.root / "other.md"
        primary.write_text(
            "Revision 1 states RetentionEntity1 remains active.", encoding="utf-8"
        )
        other.write_text("Other revision 1 remains stable.", encoding="utf-8")
        primary_source = elect_file_source(
            self.database_path, self.project_id, primary
        )
        other_source = elect_file_source(self.database_path, self.project_id, other)
        primary_id = self._document_id("primary.md")
        other_id = self._document_id("other.md")

        other.write_text("Other revision 2 remains stable.", encoding="utf-8")
        sync_elected_source(self.database_path, other_source.source_id)
        other_before = self._history(other_id)

        for revision in range(2, 12):
            self._revise(primary, primary_source.source_id, revision)
        self.assertEqual(
            [revision for revision, _ in self._history(primary_id)],
            list(range(1, 11)),
        )

        self._revise(primary, primary_source.source_id, 12)
        self.assertEqual(
            [revision for revision, _ in self._history(primary_id)],
            list(range(2, 12)),
        )

        for revision in range(13, 21):
            self._revise(primary, primary_source.source_id, revision)
        retained = self._history(primary_id)
        self.assertEqual([revision for revision, _ in retained], list(range(10, 20)))
        self.assertEqual(len(retained) + 1, 11)
        self.assertEqual(self._history(other_id), other_before)

        repeated = sync_elected_source(self.database_path, primary_source.source_id)
        self.assertEqual((repeated.updated, repeated.unchanged), (0, 1))
        self.assertEqual(self._history(primary_id), retained)

        with database(self.database_path) as reopened:
            current = reopened.execute(
                "SELECT revision, text_content FROM documents WHERE document_id = ?",
                (primary_id,),
            ).fetchone()
            reopened_history = reopened.execute(
                "SELECT revision FROM document_revision_history "
                "WHERE document_id = ? ORDER BY revision",
                (primary_id,),
            ).fetchall()
        self.assertEqual((int(current["revision"]), str(current["text_content"])), (20, primary.read_text(encoding="utf-8")))
        self.assertEqual([int(row["revision"]) for row in reopened_history], list(range(10, 20)))

        self.assertTrue(unelect_source(self.database_path, primary_source.source_id))
        with database(self.database_path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM document_revision_history WHERE document_id = ?",
                    (primary_id,),
                ).fetchone()[0],
                0,
            )
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM documents WHERE document_id = ?", (other_id,)
                ).fetchone()
            )
        self.assertEqual(self._history(other_id), other_before)

    def test_pruned_revisions_leave_no_stale_derived_evidence_and_database_stays_valid(self) -> None:
        path = self.root / "derived.md"
        path.write_text(
            "Revision 1 states DerivedRetentionEntity1 remains active.",
            encoding="utf-8",
        )
        source = elect_file_source(self.database_path, self.project_id, path)
        document_id = self._document_id("derived.md")
        for revision in range(2, 12):
            self._revise(path, source.source_id, revision)
        run_derived_state_cycle(self.database_path)

        self._revise(path, source.source_id, 12)
        self.assertEqual(
            [revision for revision, _ in self._history(document_id)], list(range(2, 12))
        )
        run_derived_state_cycle(self.database_path)

        retained_revisions = set(range(2, 13))
        with database(self.database_path) as connection:
            semantic_revisions = {
                int(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT source_revision FROM semantic_units "
                    "WHERE source_type = 'elected_document' AND source_id = ?",
                    (document_id,),
                )
            }
            self.assertEqual(semantic_revisions, retained_revisions)
            for table in ("entity_mentions", "claim_evidence"):
                stale = connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE source_type = 'elected_document' "
                    "AND source_id = ? AND source_revision NOT BETWEEN 2 AND 12",
                    (document_id,),
                ).fetchone()[0]
                self.assertEqual(stale, 0)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM entities e LEFT JOIN entity_mentions m "
                    "ON m.entity_id = e.entity_id WHERE m.entity_id IS NULL"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM claims c LEFT JOIN claim_evidence e "
                    "ON e.claim_id = c.claim_id WHERE e.claim_id IS NULL"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            lexical = connection.execute(
                "SELECT rowid, content FROM lexical_entries ORDER BY rowid"
            ).fetchall()
            fts = connection.execute(
                "SELECT rowid, content FROM lexical_entries_fts ORDER BY rowid"
            ).fetchall()
            self.assertEqual([tuple(row) for row in lexical], [tuple(row) for row in fts])

        self.assertTrue(unelect_source(self.database_path, source.source_id))
        run_derived_state_cycle(self.database_path)
        with database(self.database_path) as connection:
            for table in ("document_revision_history", "semantic_units", "entity_mentions", "claim_evidence"):
                source_column = "document_id" if table == "document_revision_history" else "source_id"
                self.assertEqual(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE {source_column} = ?",
                        (document_id,),
                    ).fetchone()[0],
                    0,
                )


if __name__ == "__main__":
    import unittest

    unittest.main()
