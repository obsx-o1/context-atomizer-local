from __future__ import annotations

from test_support import TemporaryDatabaseTest, chat_event

from atomizer_local_client.chat.contracts import CorpusType
from atomizer_local_client.chat.ingestion import ingest_chat_event
from atomizer_local_client.history.connection import database
from atomizer_local_client.lexical.audit import audit_lexical_consistency
from atomizer_local_client.lexical.search import (
    DEFAULT_RESULT_LIMIT,
    MAX_QUERY_LENGTH,
    MAX_QUERY_TERMS,
    MAX_RESULT_LIMIT,
    compile_literal_query,
    search_all_local_history,
    search_chat_history,
    search_current_chat,
    search_current_document,
    search_current_project,
    search_elected_documents,
)
from atomizer_local_client.library.document_registry import (
    elect_directory,
    elect_document,
    elect_file_source,
    sync_elected_source,
    unelect_source,
)
from atomizer_local_client.library.view_service import search_library


class LexicalSearchContractTests(TemporaryDatabaseTest):
    def setUp(self) -> None:
        super().setUp()
        self.chat_a1 = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="pass4-a1",
                chat="pass4-chat-a1",
                project="pass4-project-a",
                project_name="Pass 4 Project A",
                content=(
                    "pass4globalchat pass4projectshared pass4chatshared "
                    "pass4mixed"
                ),
            ),
        )
        self.chat_a2 = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="pass4-a2",
                chat="pass4-chat-a2",
                project="pass4-project-a",
                project_name="Pass 4 Project A",
                content="pass4chatshared pass4secondchat",
            ),
        )
        self.chat_b = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="pass4-b1",
                chat="pass4-chat-b1",
                project="pass4-project-b",
                project_name="Pass 4 Project B",
                content="pass4projectshared pass4projectbonly",
            ),
        )
        self.document_a1_path = self.root / "PASS4_A1.md"
        self.document_a1_path.write_text(
            """pass4globaldoc pass4mixed pass4docshared pass4projectshared
alpha-beta hello.world foo_bar path/name quoted text
café 東京 snake_case_identifier camelCaseIdentifier Class.method
foo::bar package/module error_code_123 ExactCaseWord OR NEAR
""",
            encoding="utf-8",
        )
        self.document_a2_path = self.root / "PASS4_A2.txt"
        self.document_a2_path.write_text("pass4docshared pass4secondoc", encoding="utf-8")
        self.document_b_path = self.root / "PASS4_B.md"
        self.document_b_path.write_text(
            "pass4projectshared pass4projectbdoc", encoding="utf-8"
        )
        self.document_a1 = elect_document(
            self.database_path, self.chat_a1.project_id, self.document_a1_path
        )
        self.document_a2 = elect_document(
            self.database_path, self.chat_a1.project_id, self.document_a2_path
        )
        self.document_b = elect_document(
            self.database_path, self.chat_b.project_id, self.document_b_path
        )

    def test_literal_query_compiler_is_bounded_and_never_exposes_fts_syntax(self) -> None:
        cases = {
            "alpha-beta": '"alpha" AND "beta"',
            "hello.world": '"hello" AND "world"',
            "foo_bar": '"foo" AND "bar"',
            "path/name": '"path" AND "name"',
            '"quoted text"': '"quoted" AND "text"',
            "OR NEAR * (alpha)": '"OR" AND "NEAR" AND "alpha"',
            "snake_case_identifier": '"snake" AND "case" AND "identifier"',
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(compile_literal_query(query), expected)
        for query in ("", "   ", "---", "*()::"):
            with self.subTest(query=query):
                with self.assertRaisesRegex(ValueError, "searchable text"):
                    compile_literal_query(query)
        with self.assertRaisesRegex(ValueError, str(MAX_QUERY_LENGTH)):
            compile_literal_query("a" * (MAX_QUERY_LENGTH + 1))
        with self.assertRaisesRegex(ValueError, str(MAX_QUERY_TERMS)):
            compile_literal_query(" ".join(f"t{index}" for index in range(MAX_QUERY_TERMS + 1)))

    def test_global_project_chat_document_and_corpus_scopes(self) -> None:
        mixed = search_all_local_history(self.database_path, "pass4mixed")
        self.assertEqual(
            {candidate.corpus_type for candidate in mixed},
            {CorpusType.CHAT_HISTORY, CorpusType.ELECTED_DOCUMENT},
        )
        self.assertEqual(len(mixed), 2)

        project_a = search_current_project(
            self.database_path, self.chat_a1.project_id, "pass4projectshared"
        )
        project_b = search_current_project(
            self.database_path, self.chat_b.project_id, "pass4projectshared"
        )
        self.assertTrue(project_a)
        self.assertTrue(project_b)
        self.assertEqual({row.project_id for row in project_a}, {self.chat_a1.project_id})
        self.assertEqual({row.project_id for row in project_b}, {self.chat_b.project_id})

        self.assertEqual(
            [row.source_id for row in search_current_chat(
                self.database_path, self.chat_a1.chat_id, "pass4chatshared"
            )],
            [self.chat_a1.message_id],
        )
        self.assertEqual(
            [row.source_id for row in search_current_chat(
                self.database_path, self.chat_a2.chat_id, "pass4chatshared"
            )],
            [self.chat_a2.message_id],
        )
        self.assertEqual(
            [row.source_id for row in search_current_document(
                self.database_path, self.document_a1, "pass4docshared"
            )],
            [self.document_a1],
        )
        self.assertEqual(
            [row.source_id for row in search_current_document(
                self.database_path, self.document_a2, "pass4docshared"
            )],
            [self.document_a2],
        )

        chat_only = search_chat_history(self.database_path, "pass4mixed")
        document_only = search_elected_documents(self.database_path, "pass4mixed")
        self.assertEqual([row.corpus_type for row in chat_only], [CorpusType.CHAT_HISTORY])
        self.assertEqual(
            [row.corpus_type for row in document_only],
            [CorpusType.ELECTED_DOCUMENT],
        )
        self.assertEqual(
            len(search_chat_history(
                self.database_path,
                "pass4projectshared",
                project_id=self.chat_a1.project_id,
            )),
            1,
        )
        self.assertEqual(
            len(search_elected_documents(
                self.database_path,
                "pass4projectshared",
                project_id=self.chat_a1.project_id,
            )),
            1,
        )

    def test_navigation_metadata_uses_exact_stable_source_identities(self) -> None:
        results = search_library(self.database_path, "pass4mixed")
        self.assertEqual(len(results), 2)
        message = next(row for row in results if row["source_type"] == "Message")
        document = next(row for row in results if row["source_type"] == "Document")
        self.assertEqual(message["message_id"], self.chat_a1.message_id)
        self.assertEqual(message["chat_id"], self.chat_a1.chat_id)
        self.assertEqual(
            message["destination"],
            f"/chat?chat_id={self.chat_a1.chat_id}#message-{self.chat_a1.message_id}",
        )
        self.assertEqual(document["document_id"], self.document_a1)
        self.assertEqual(
            document["destination"], f"/document?document_id={self.document_a1}"
        )
        self.assertNotIn("local_source_reference", document)

    def test_tokenizer_case_unicode_punctuation_and_code_like_behavior(self) -> None:
        matching_queries = (
            "alpha-beta",
            "hello.world",
            "foo_bar",
            "path/name",
            '"quoted text"',
            "cafe",
            "東京",
            "snake_case_identifier",
            "camelcaseidentifier",
            "class.method",
            "foo::bar",
            "package/module",
            "error_code_123",
            "exactcaseword",
            "OR",
            "NEAR",
        )
        for query in matching_queries:
            with self.subTest(query=query):
                results = search_current_document(
                    self.database_path, self.document_a1, query
                )
                self.assertEqual([row.source_id for row in results], [self.document_a1])
        self.assertEqual(
            search_current_document(
                self.database_path, self.document_a1, "camelCase"
            ),
            [],
        )

    def test_default_max_limits_and_equal_score_ties_are_deterministic(self) -> None:
        for index in range(MAX_RESULT_LIMIT + 5):
            ingest_chat_event(
                self.database_path,
                chat_event(
                    event_id=f"pass4-limit-{index}",
                    chat=f"pass4-limit-chat-{index}",
                    project="pass4-limit-project",
                    project_name="Pass 4 Limit Project",
                    content="pass4limitmarker",
                ),
            )
        first = search_all_local_history(self.database_path, "pass4limitmarker")
        second = search_all_local_history(self.database_path, "pass4limitmarker")
        expanded = search_all_local_history(
            self.database_path, "pass4limitmarker", limit=MAX_RESULT_LIMIT
        )
        self.assertEqual(len(first), DEFAULT_RESULT_LIMIT)
        self.assertEqual(
            len(search_library(self.database_path, "pass4limitmarker")),
            50,
        )
        self.assertEqual(
            [row.source_id for row in first], [row.source_id for row in second]
        )
        self.assertEqual(len(expanded), MAX_RESULT_LIMIT)
        self.assertEqual(len({row.score for row in expanded}), 1)
        self.assertEqual(
            [row.source_id for row in expanded],
            sorted(row.source_id for row in expanded),
        )
        for invalid in (0, MAX_RESULT_LIMIT + 1, True):
            with self.subTest(limit=invalid):
                with self.assertRaises(ValueError):
                    search_all_local_history(
                        self.database_path, "pass4limitmarker", limit=invalid
                    )

        tie_message = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="pass4-cross-corpus-tie",
                chat="pass4-cross-corpus-tie",
                project="pass4-tie-project",
                project_name="Pass 4 Tie Project",
                content="pass4crosstie",
            ),
        )
        tie_document_path = self.root / "PASS4_TIE.txt"
        tie_document_path.write_text("pass4crosstie", encoding="utf-8")
        tie_document = elect_document(
            self.database_path, tie_message.project_id, tie_document_path
        )
        tied = search_all_local_history(self.database_path, "pass4crosstie")
        self.assertEqual(len(tied), 2)
        self.assertEqual(tied[0].score, tied[1].score)
        self.assertEqual(
            [(row.corpus_type, row.source_id) for row in tied],
            [
                (CorpusType.CHAT_HISTORY, tie_message.message_id),
                (CorpusType.ELECTED_DOCUMENT, tie_document),
            ],
        )

    def test_edit_repeated_sync_revocation_and_deletion_remove_stale_results(self) -> None:
        editable = self.root / "PASS4_EDIT.md"
        editable.write_text("pass4oldterm", encoding="utf-8")
        source = elect_file_source(self.database_path, self.chat_a1.project_id, editable)
        self.assertEqual(len(search_elected_documents(self.database_path, "pass4oldterm")), 1)
        editable.write_text("pass4newterm", encoding="utf-8")
        sync_elected_source(self.database_path, source.source_id)
        self.assertEqual(search_elected_documents(self.database_path, "pass4oldterm"), [])
        self.assertEqual(len(search_elected_documents(self.database_path, "pass4newterm")), 1)
        repeated = sync_elected_source(self.database_path, source.source_id)
        self.assertEqual(repeated.unchanged, 1)
        self.assertEqual(len(search_elected_documents(self.database_path, "pass4newterm")), 1)
        self.assertTrue(unelect_source(self.database_path, source.source_id))
        self.assertTrue(editable.is_file())
        self.assertEqual(search_elected_documents(self.database_path, "pass4newterm"), [])

        deleted = self.root / "PASS4_DELETE.txt"
        deleted.write_text("pass4deletedterm", encoding="utf-8")
        deleted_source = elect_file_source(
            self.database_path, self.chat_a1.project_id, deleted
        )
        deleted.unlink()
        sync_elected_source(self.database_path, deleted_source.source_id)
        self.assertEqual(search_elected_documents(self.database_path, "pass4deletedterm"), [])

    def test_overlapping_sources_repeated_capture_and_restart_do_not_duplicate(self) -> None:
        overlap = self.root / "overlap"
        overlap.mkdir()
        document = overlap / "PASS4_OVERLAP.md"
        document.write_text("pass4overlapmarker", encoding="utf-8")
        directory_source = elect_directory(
            self.database_path, self.chat_a1.project_id, overlap
        )
        file_source = elect_file_source(
            self.database_path, self.chat_a1.project_id, document
        )
        elect_file_source(self.database_path, self.chat_a1.project_id, document)
        self.assertEqual(
            len(search_elected_documents(self.database_path, "pass4overlapmarker")),
            1,
        )
        duplicate_event = chat_event(
            event_id="pass4-duplicate-capture",
            chat="pass4-duplicate-chat",
            project="pass4-project-a",
            project_name="Pass 4 Project A",
            content="pass4duplicatecapture",
        )
        ingest_chat_event(self.database_path, duplicate_event)
        ingest_chat_event(self.database_path, duplicate_event)
        before_restart = search_all_local_history(
            self.database_path, "pass4duplicatecapture"
        )
        with database(self.database_path):
            pass
        after_restart = search_all_local_history(
            self.database_path, "pass4duplicatecapture"
        )
        self.assertEqual(len(before_restart), 1)
        self.assertEqual(before_restart, after_restart)
        self.assertTrue(unelect_source(self.database_path, directory_source.source_id))
        self.assertEqual(
            len(search_elected_documents(self.database_path, "pass4overlapmarker")),
            1,
        )
        self.assertTrue(unelect_source(self.database_path, file_source.source_id))

    def test_read_only_consistency_audit_detects_missing_and_orphan_fts_postings(self) -> None:
        clean = audit_lexical_consistency(self.database_path)
        self.assertTrue(clean["passed"])
        self.assertTrue(all(value == 0 for value in clean["checks"].values()))
        with database(self.database_path) as connection:
            row = connection.execute(
                "SELECT rowid, content FROM lexical_entries "
                "WHERE source_id = ?",
                (self.chat_a1.message_id,),
            ).fetchone()
            connection.execute(
                "INSERT INTO lexical_entries_fts(lexical_entries_fts, rowid, content) "
                "VALUES('delete', ?, ?)",
                (row["rowid"], row["content"]),
            )
            connection.execute(
                "INSERT INTO lexical_entries_fts(rowid, content) VALUES(?, ?)",
                (999999, "pass4orphanposting"),
            )
        broken = audit_lexical_consistency(self.database_path)
        self.assertFalse(broken["passed"])
        self.assertEqual(broken["checks"]["lexical_without_fts"], 1)
        self.assertEqual(broken["checks"]["fts_without_lexical"], 1)


if __name__ == "__main__":
    import unittest

    unittest.main()
