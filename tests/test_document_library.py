from __future__ import annotations

from test_support import TemporaryDatabaseTest, chat_event

from atomizer_local_client.chat.ingestion import ingest_chat_event
from atomizer_local_client.history.connection import database
from atomizer_local_client.lexical.search import search_current_project
from atomizer_local_client.library.document_reader import (
    list_documents,
    list_elected_sources,
    read_document,
)
from atomizer_local_client.library.document_registry import (
    elect_directory,
    elect_file_source,
    sync_elected_source,
    unelect_source,
)


class DocumentLibraryTests(TemporaryDatabaseTest):
    def setUp(self) -> None:
        super().setUp()
        self.project_id = ingest_chat_event(
            self.database_path,
            chat_event(event_id="document-project", content="document project seed"),
        ).project_id

    def test_controlled_file_fixture_edit_add_unelect_and_restart(self) -> None:
        fixture = self.root / "controlled"
        fixture.mkdir()
        test_a = fixture / "TEST_A.md"
        test_b = fixture / "TEST_B.txt"
        test_a.write_text("# Test A\n\ndocumentalphaoriginal", encoding="utf-8")

        first = elect_file_source(self.database_path, self.project_id, test_a)
        self.assertEqual((first.scanned, first.added, first.updated), (1, 1, 0))
        self.assertEqual(len(search_current_project(self.database_path, self.project_id, "documentalphaoriginal")), 1)

        test_a.write_text("# Test A\n\ndocumentbetaedited", encoding="utf-8")
        edited = sync_elected_source(self.database_path, first.source_id)
        self.assertEqual((edited.scanned, edited.added, edited.updated), (1, 0, 1))
        self.assertEqual(search_current_project(self.database_path, self.project_id, "documentalphaoriginal"), [])
        self.assertEqual(len(search_current_project(self.database_path, self.project_id, "documentbetaedited")), 1)

        test_b.write_text("documentgammasurvivor", encoding="utf-8")
        second = elect_file_source(self.database_path, self.project_id, test_b)
        self.assertEqual((second.scanned, second.added), (1, 1))
        self.assertEqual(len(list_documents(self.database_path, self.project_id)), 2)

        self.assertTrue(unelect_source(self.database_path, first.source_id))
        self.assertEqual(search_current_project(self.database_path, self.project_id, "documentbetaedited"), [])
        self.assertEqual(len(search_current_project(self.database_path, self.project_id, "documentgammasurvivor")), 1)

        restarted = sync_elected_source(self.database_path, second.source_id)
        self.assertEqual((restarted.added, restarted.updated, restarted.unchanged), (0, 0, 1))
        self.assertEqual(len(list_documents(self.database_path, self.project_id)), 1)
        self.assertEqual(len(list_elected_sources(self.database_path, self.project_id)), 1)
        with database(self.database_path) as connection:
            counts = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM documents),
                    (SELECT COUNT(*) FROM lexical_entries WHERE corpus_type='ELECTED_DOCUMENT'),
                    (SELECT COUNT(*) FROM document_source_memberships)
                """
            ).fetchone()
        self.assertEqual(tuple(counts), (1, 1, 1))

    def test_directory_election_is_recursive_explicit_and_format_bounded(self) -> None:
        elected = self.root / "elected"
        nested = elected / "nested"
        unelected = self.root / "unelected"
        nested.mkdir(parents=True)
        unelected.mkdir()
        (elected / "root.txt").write_text("directoryrootmarker", encoding="utf-8")
        (nested / "guide.md").write_text("directorymarkdownmarker", encoding="utf-8")
        (nested / "notes.markdown").write_text("directorylongmarkdownmarker", encoding="utf-8")
        (nested / "ignored.pdf").write_text("ignoredpdfmarker", encoding="utf-8")
        (unelected / "outside.txt").write_text("unelectedoutsidemarker", encoding="utf-8")

        result = elect_directory(self.database_path, self.project_id, elected)

        self.assertEqual((result.scanned, result.added), (3, 3))
        self.assertEqual(len(search_current_project(self.database_path, self.project_id, "directoryrootmarker")), 1)
        self.assertEqual(len(search_current_project(self.database_path, self.project_id, "directorymarkdownmarker")), 1)
        self.assertEqual(len(search_current_project(self.database_path, self.project_id, "directorylongmarkdownmarker")), 1)
        self.assertEqual(search_current_project(self.database_path, self.project_id, "ignoredpdfmarker"), [])
        self.assertEqual(search_current_project(self.database_path, self.project_id, "unelectedoutsidemarker"), [])

    def test_directory_edit_delete_and_move_are_incremental(self) -> None:
        elected = self.root / "elected"
        elected.mkdir()
        original = elected / "original.md"
        changed = elected / "changed.txt"
        original.write_text("moveidentitymarker", encoding="utf-8")
        changed.write_text("beforeeditmarker", encoding="utf-8")
        initial = elect_directory(self.database_path, self.project_id, elected)
        documents = {row["display_name"]: row for row in list_documents(self.database_path, self.project_id)}
        original_id = documents["original.md"]["document_id"]
        original_identity = documents["original.md"]["file_identity"]

        renamed = elected / "renamed.markdown"
        original.rename(renamed)
        changed.write_text("aftereditmarker", encoding="utf-8")
        deleted = elected / "deleted.txt"
        deleted.write_text("deletedcontentmarker", encoding="utf-8")
        changed_result = sync_elected_source(self.database_path, initial.source_id)
        deleted.unlink()
        result = sync_elected_source(self.database_path, initial.source_id)

        current = {row["display_name"]: row for row in list_documents(self.database_path, self.project_id)}
        self.assertEqual(set(current), {"renamed.markdown", "changed.txt"})
        if original_identity is not None:
            self.assertEqual(current["renamed.markdown"]["document_id"], original_id)
            self.assertEqual(changed_result.moved, 1)
        self.assertEqual(search_current_project(self.database_path, self.project_id, "beforeeditmarker"), [])
        self.assertEqual(len(search_current_project(self.database_path, self.project_id, "aftereditmarker")), 1)
        self.assertEqual(search_current_project(self.database_path, self.project_id, "deletedcontentmarker"), [])

    def test_re_election_and_overlapping_sources_do_not_duplicate_documents(self) -> None:
        elected = self.root / "overlap"
        elected.mkdir()
        source = elected / "shared.md"
        source.write_text("overlapmarker", encoding="utf-8")
        directory = elect_directory(self.database_path, self.project_id, elected)
        file_source = elect_file_source(self.database_path, self.project_id, source)
        repeated = elect_file_source(self.database_path, self.project_id, source)

        self.assertEqual(file_source.source_id, repeated.source_id)
        self.assertEqual((repeated.added, repeated.updated, repeated.unchanged), (0, 0, 1))
        self.assertEqual(len(list_documents(self.database_path, self.project_id)), 1)
        self.assertEqual(len(list_elected_sources(self.database_path, self.project_id)), 2)
        self.assertTrue(unelect_source(self.database_path, directory.source_id))
        self.assertEqual(len(search_current_project(self.database_path, self.project_id, "overlapmarker")), 1)
        self.assertTrue(unelect_source(self.database_path, file_source.source_id))
        self.assertEqual(search_current_project(self.database_path, self.project_id, "overlapmarker"), [])

    def test_failed_directory_snapshot_is_atomic_and_resumable(self) -> None:
        elected = self.root / "atomic"
        elected.mkdir()
        stable = elected / "stable.md"
        stable.write_text("atomicoldmarker", encoding="utf-8")
        source = elect_directory(self.database_path, self.project_id, elected)
        stable.write_text("atomicnewmarker", encoding="utf-8")
        invalid = elected / "invalid.txt"
        invalid.write_bytes(b"\xff\xfe")

        with self.assertRaises(UnicodeDecodeError):
            sync_elected_source(self.database_path, source.source_id)
        self.assertEqual(len(search_current_project(self.database_path, self.project_id, "atomicoldmarker")), 1)
        self.assertEqual(search_current_project(self.database_path, self.project_id, "atomicnewmarker"), [])

        invalid.write_text("atomicrecoveredmarker", encoding="utf-8")
        recovered = sync_elected_source(self.database_path, source.source_id)
        self.assertEqual((recovered.scanned, recovered.added, recovered.updated), (2, 1, 1))
        self.assertEqual(search_current_project(self.database_path, self.project_id, "atomicoldmarker"), [])
        self.assertEqual(len(search_current_project(self.database_path, self.project_id, "atomicnewmarker")), 1)
        self.assertEqual(len(search_current_project(self.database_path, self.project_id, "atomicrecoveredmarker")), 1)

    def test_deleted_elected_file_removes_active_document_and_index(self) -> None:
        source_path = self.root / "deleted.md"
        source_path.write_text("missingfilemarker", encoding="utf-8")
        source = elect_file_source(self.database_path, self.project_id, source_path)
        source_path.unlink()

        result = sync_elected_source(self.database_path, source.source_id)

        self.assertEqual((result.scanned, result.removed), (0, 1))
        self.assertEqual(list_documents(self.database_path, self.project_id), [])
        self.assertEqual(search_current_project(self.database_path, self.project_id, "missingfilemarker"), [])


if __name__ == "__main__":
    import unittest

    unittest.main()
