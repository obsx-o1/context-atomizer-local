from __future__ import annotations

from datetime import datetime, timedelta, timezone

from test_support import TemporaryDatabaseTest, chat_event

from atomizer_local_client.chat.contracts import Role
from atomizer_local_client.chat.ingestion import ingest_chat_event
from atomizer_local_client.derived_state.cycle import run_derived_state_cycle
from atomizer_local_client.history.connection import database
from atomizer_local_client.library.view_service import list_projects
from atomizer_local_client.memory_access.access_gate import (
    DirectLibraryAccessMode,
    LibraryAccessGate,
    LibraryCaller,
)
from atomizer_local_client.memory_access.contracts import ManagedAuthority
from atomizer_local_client.memory_access.formatting import (
    MAX_ITEM_CONTENT_CHARS,
    MAX_RESULTS,
    MAX_TOTAL_OUTPUT_CHARS,
)
from atomizer_local_client.memory_access.query_service import (
    LibraryQueryError,
    LibraryQueryService,
)


class FakeManager:
    def __init__(self, authority: ManagedAuthority) -> None:
        self._authority = authority

    def authority(self) -> ManagedAuthority:
        return self._authority


class MemoryAccessTests(TemporaryDatabaseTest):
    def setUp(self) -> None:
        super().setUp()
        self.first = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="memory-1",
                chat="memory-chat",
                content="Project Atlas uses heliotrope storage for automobile records.",
                project="atlas",
                project_name="Project Atlas",
            ),
        )
        ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="memory-2",
                chat="memory-chat",
                role=Role.ASSISTANT,
                content="Project Atlas preserves provenance for each vehicle record. " + "x" * 5_000,
                project="atlas",
                project_name="Project Atlas",
            ),
        )
        self.second = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="memory-3",
                chat="second-chat",
                content="Project Birch uses quartz storage.",
                project="birch",
                project_name="Project Birch",
            ),
        )
        run_derived_state_cycle(self.database_path)
        self.service = LibraryQueryService(self.database_path)

    def _logical_snapshot(self) -> tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]:
        with database(self.database_path) as connection:
            tables = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
                if not str(row[0]).endswith(("_fts", "_fts_data", "_fts_idx", "_fts_docsize", "_fts_config"))
            ]
            return tuple(
                (name, tuple(tuple(row) for row in connection.execute(f'SELECT * FROM "{name}" ORDER BY rowid')))
                for name in tables
            )

    def test_search_composes_existing_lexical_vector_rrf_and_reranker(self) -> None:
        lexical = self.service.search_library("heliotrope", limit=8)
        self.assertEqual(lexical["status"], "ok")
        self.assertGreater(lexical["result_count"], 0)
        self.assertIsNotNone(lexical["items"][0]["lexical_rank"])
        self.assertIsNotNone(lexical["items"][0]["vector_rank"])
        self.assertGreater(lexical["items"][0]["fused_score"], 0)

        semantic = self.service.search_library("automobile", limit=8)
        self.assertGreater(semantic["result_count"], 0)
        self.assertTrue(any(item["vector_rank"] is not None for item in semantic["items"]))
        self.assertTrue(any(item["lexical_rank"] is None for item in semantic["items"]))

    def test_search_is_stable_scoped_bounded_and_provenanced(self) -> None:
        first = self.service.search_library("storage", self.first.project_id, 99)
        second = self.service.search_library("storage", self.first.project_id, 99)
        self.assertEqual(first, second)
        self.assertLessEqual(first["result_count"], MAX_RESULTS)
        for item in first["items"]:
            self.assertEqual(item["project_id"], self.first.project_id)
            self.assertTrue(item["evidence_id"])
            self.assertTrue(item["source_id"])
            self.assertIn(item["source_type"], {"chat_message", "elected_document"})
            self.assertTrue(item["project"])
            self.assertTrue(item["timestamp"])
            self.assertLessEqual(len(item["content"]), MAX_ITEM_CONTENT_CHARS)

    def test_get_by_evidence_or_source_id_and_recent_context(self) -> None:
        search = self.service.search_library("heliotrope")
        evidence_id = search["items"][0]["evidence_id"]
        by_evidence = self.service.get_library_item(evidence_id)
        self.assertEqual(by_evidence["items"][0]["evidence_id"], evidence_id)
        by_source = self.service.get_library_item(self.first.message_id)
        self.assertEqual(by_source["items"][0]["source_id"], self.first.message_id)

        recent = self.service.recent_library_context(self.first.project_id, 2)
        self.assertEqual(recent["result_count"], 2)
        self.assertTrue(all(item["project_id"] == self.first.project_id for item in recent["items"]))

    def test_project_listing_and_invalid_inputs(self) -> None:
        projects = self.service.list_library_projects()
        self.assertEqual(projects["result_count"], 2)
        self.assertEqual({item["project"] for item in projects["items"]}, {"Project Atlas", "Project Birch"})
        for call in (
            lambda: self.service.search_library(""),
            lambda: self.service.search_library("storage", "not-a-project"),
            lambda: self.service.search_library("storage", limit=0),
            lambda: self.service.get_library_item("not-an-id"),
        ):
            with self.assertRaises(LibraryQueryError):
                call()

    def test_all_queries_preserve_logical_database_state(self) -> None:
        before = self._logical_snapshot()
        self.service.search_library("storage")
        self.service.get_library_item(self.first.message_id)
        self.service.recent_library_context()
        self.service.list_library_projects()
        self.assertEqual(self._logical_snapshot(), before)

    def test_output_is_bounded(self) -> None:
        import json

        payload = self.service.recent_library_context(limit=8)
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        self.assertLessEqual(len(serialized), MAX_TOTAL_OUTPUT_CHARS)
        self.assertTrue(all(len(item["content"]) <= MAX_ITEM_CONTENT_CHARS for item in payload["items"]))

    def test_direct_managed_exclusive_and_disabled_modes(self) -> None:
        direct = LibraryAccessGate(DirectLibraryAccessMode.DIRECT_LOCAL)
        self.assertTrue(direct.authorize(LibraryCaller.DIRECT_FRONTIER).allowed)
        managed = LibraryQueryService(
            self.database_path,
            gate=LibraryAccessGate(DirectLibraryAccessMode.MANAGED_EXCLUSIVE),
        )
        denied = managed.search_library("storage")
        self.assertEqual(denied["status"], "managed_exclusive")
        self.assertEqual(denied["items"], [])
        disabled = LibraryQueryService(
            self.database_path,
            gate=LibraryAccessGate(DirectLibraryAccessMode.DISABLED),
        )
        self.assertEqual(disabled.recent_library_context()["status"], "disabled")

    def test_manager_must_be_verified_and_unexpired(self) -> None:
        now = datetime.now(timezone.utc)
        active_gate = LibraryAccessGate(
            DirectLibraryAccessMode.MANAGED_EXCLUSIVE,
            manager=FakeManager(ManagedAuthority(True, now + timedelta(minutes=5), "session-1")),
        )
        self.assertTrue(active_gate.authorize(LibraryCaller.TRUSTED_MANAGER, now=now).allowed)
        self.assertFalse(active_gate.authorize(LibraryCaller.DIRECT_FRONTIER, now=now).allowed)
        expired_gate = LibraryAccessGate(
            DirectLibraryAccessMode.MANAGED_EXCLUSIVE,
            manager=FakeManager(ManagedAuthority(True, now - timedelta(seconds=1), "session-old")),
        )
        self.assertEqual(
            expired_gate.authorize(LibraryCaller.TRUSTED_MANAGER, now=now).status,
            "manager_authority_expired",
        )

    def test_human_library_view_is_unaffected_by_managed_exclusive_mode(self) -> None:
        service = LibraryQueryService(
            self.database_path,
            gate=LibraryAccessGate(DirectLibraryAccessMode.MANAGED_EXCLUSIVE),
        )
        self.assertEqual(service.list_library_projects()["items"], [])
        self.assertEqual(len(list_projects(self.database_path)), 2)


if __name__ == "__main__":
    import unittest

    unittest.main()
