from __future__ import annotations

from test_support import TemporaryDatabaseTest, chat_event

from atomizer_local_client.chat.contracts import CorpusType, Role
from atomizer_local_client.chat.ingestion import ingest_chat_event
from atomizer_local_client.lexical.search import (
    search_all_local_history,
    search_current_chat,
    search_current_project,
)
from atomizer_local_client.library.document_reader import read_document
from atomizer_local_client.library.document_registry import elect_document, unelect_document


class SearchAndLibraryTests(TemporaryDatabaseTest):
    def setUp(self) -> None:
        super().setUp()
        self.first = ingest_chat_event(
            self.database_path,
            chat_event(event_id="u1", chat="chat-one", content="orchid user prompt"),
        )
        ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="a1",
                chat="chat-one",
                role=Role.ASSISTANT,
                content="cobalt assistant response",
            ),
        )
        self.second = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="u2",
                chat="chat-two",
                project="project-two",
                project_name="Project Two",
                content="topaz unrelated project",
            ),
        )

    def test_user_and_assistant_messages_are_searchable(self) -> None:
        self.assertEqual(search_current_chat(self.database_path, self.first.chat_id, "orchid")[0].role, "user")
        self.assertEqual(search_current_chat(self.database_path, self.first.chat_id, "cobalt")[0].role, "assistant")

    def test_current_chat_scope_excludes_other_chats(self) -> None:
        self.assertEqual(search_current_chat(self.database_path, self.first.chat_id, "topaz"), [])

    def test_project_scope_excludes_unrelated_projects(self) -> None:
        self.assertEqual(search_current_project(self.database_path, self.first.project_id, "topaz"), [])
        self.assertEqual(len(search_current_project(self.database_path, self.second.project_id, "topaz")), 1)

    def test_global_scope_finds_all_local_corpora(self) -> None:
        self.assertEqual(len(search_all_local_history(self.database_path, "topaz")), 1)

    def test_text_and_markdown_election_search_and_un_election(self) -> None:
        text_path = self.root / "notes.txt"
        markdown_path = self.root / "guide.md"
        text_path.write_text("saffron elected text", encoding="utf-8")
        markdown_path.write_text("# Guide\n\nmarigold elected markdown", encoding="utf-8")
        original_markdown = markdown_path.read_bytes()
        text_id = elect_document(self.database_path, self.first.project_id, text_path)
        markdown_id = elect_document(self.database_path, self.first.project_id, markdown_path)
        text_result = search_current_project(self.database_path, self.first.project_id, "saffron")
        markdown_result = search_all_local_history(self.database_path, "marigold")
        self.assertEqual(text_result[0].corpus_type, CorpusType.ELECTED_DOCUMENT)
        self.assertEqual(markdown_result[0].source_id, markdown_id)
        self.assertEqual(read_document(self.database_path, text_id)["document_type"], "text")
        self.assertEqual(markdown_path.read_bytes(), original_markdown)
        self.assertTrue(unelect_document(self.database_path, markdown_id))
        self.assertEqual(search_all_local_history(self.database_path, "marigold"), [])

    def test_unsupported_document_type_is_rejected(self) -> None:
        source = self.root / "binary.pdf"
        source.write_bytes(b"not a supported document")
        with self.assertRaises(ValueError):
            elect_document(self.database_path, self.first.project_id, source)

    def test_empty_search_query_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            search_all_local_history(self.database_path, "---")


if __name__ == "__main__":
    import unittest

    unittest.main()

