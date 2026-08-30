from __future__ import annotations

from test_support import TemporaryDatabaseTest

from atomizer_local_client.chat.contracts import CorpusType
from atomizer_local_client.chat.ingestion import ingest_chat_event
from atomizer_local_client.chat.normalizer import normalize_chatgpt_web, normalize_codex_hook
from atomizer_local_client.history.connection import database
from atomizer_local_client.history.message_reader import read_project_tree
from atomizer_local_client.lexical.search import search_all_local_history
from atomizer_local_client.library.document_registry import elect_document


class LocalProductFlowTests(TemporaryDatabaseTest):
    def test_synthetic_capture_document_and_search_flow_is_local(self) -> None:
        customer_marker = self.root / "source-marker.txt"
        customer_marker.write_text("must remain unchanged", encoding="utf-8")
        marker_before = customer_marker.read_bytes()

        codex_user = normalize_codex_hook(
            {
                "session_id": "codex-session",
                "turn_id": "codex-turn",
                "cwd": str(self.root / "Context Atomizer"),
                "hook_event_name": "UserPromptSubmit",
                "prompt": "find the heliotrope phrase",
            }
        )
        codex_assistant = normalize_codex_hook(
            {
                "session_id": "codex-session",
                "turn_id": "codex-turn",
                "cwd": str(self.root / "Context Atomizer"),
                "hook_event_name": "Stop",
                "last_assistant_message": "heliotrope response captured",
            }
        )
        self.assertIsNotNone(codex_user)
        self.assertIsNotNone(codex_assistant)
        codex_user_receipt = ingest_chat_event(self.database_path, codex_user)
        ingest_chat_event(self.database_path, codex_assistant)

        web_user = normalize_chatgpt_web(
            {
                "event_id": "web-user",
                "host_chat_reference": "web-chat",
                "host_turn_reference": "web-turn-user",
                "host_project_reference": "g-p-project",
                "project_display_name": "Web Project",
                "chat_display_name": "Web Chat",
                "role": "user",
                "content": "browser vermilion prompt",
            }
        )
        web_assistant = normalize_chatgpt_web(
            {
                "event_id": "web-assistant",
                "host_chat_reference": "web-chat",
                "host_turn_reference": "web-turn-assistant",
                "host_project_reference": "g-p-project",
                "project_display_name": "Web Project",
                "chat_display_name": "Web Chat",
                "role": "assistant",
                "content": "browser vermilion response",
            }
        )
        web_receipt = ingest_chat_event(self.database_path, web_user)
        ingest_chat_event(self.database_path, web_assistant)
        elected = self.root / "elected.md"
        elected.write_text("# Elected\n\ncerulean document phrase", encoding="utf-8")
        elect_document(self.database_path, codex_user_receipt.project_id, elected)
        conversation_candidates = search_all_local_history(self.database_path, "heliotrope")
        document_candidates = search_all_local_history(self.database_path, "cerulean")
        with database(self.database_path) as connection:
            codex_tree = read_project_tree(connection, codex_user_receipt.project_id)
            web_tree = read_project_tree(connection, web_receipt.project_id)
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()

        self.assertEqual([message["role"] for message in codex_tree["chats"][0]["messages"]], ["user", "assistant"])
        self.assertEqual([message["role"] for message in web_tree["chats"][0]["messages"]], ["user", "assistant"])
        self.assertTrue(conversation_candidates)
        self.assertEqual(document_candidates[0].corpus_type, CorpusType.ELECTED_DOCUMENT)
        self.assertEqual(customer_marker.read_bytes(), marker_before)
        self.assertEqual(integrity, "ok")
        self.assertEqual(foreign_keys, [])


if __name__ == "__main__":
    import unittest

    unittest.main()
