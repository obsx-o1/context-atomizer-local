from __future__ import annotations

import json
import threading
import time
from unittest import mock

from test_support import TemporaryDatabaseTest, chat_event

from atomizer_local_client.chat.ingestion import ingest_chat_event
from atomizer_local_client.derived_state.cycle import run_derived_state_cycle
from atomizer_local_client.entities.repository import EntityRepository
from atomizer_local_client.history.connection import database
from atomizer_local_client.library.document_registry import (
    elect_file_source,
    revoke_source_authorization,
    sync_elected_source,
)
from atomizer_local_client.ui.library_server import LibraryViewServer


class AutomaticDerivedStateTests(TemporaryDatabaseTest):
    def _start_runtime_library(self) -> tuple[LibraryViewServer, threading.Thread]:
        server = LibraryViewServer(
            self.database_path,
            0,
            automatic_maintenance=True,
            maintenance_interval_seconds=0.1,
            bridge_port=1,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    @staticmethod
    def _stop_runtime_library(server: LibraryViewServer, thread: threading.Thread) -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    def _wait_for(self, predicate, *, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.025)
        self.fail("automatic derived state did not converge before the bounded timeout")

    def _counts(self) -> dict[str, int]:
        with database(self.database_path) as connection:
            return {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "projects",
                    "chats",
                    "messages",
                    "documents",
                    "document_revision_history",
                    "semantic_units",
                    "embedding_records",
                    "entity_mentions",
                    "claim_evidence",
                    "temporal_evidence_state",
                    "claim_verification_state",
                )
            }

    def _converged(self, server: LibraryViewServer) -> bool:
        health = server.health_snapshot()["derived_state"]
        return (
            health["convergence_state"] == "converged"
            and health["pending_count"] == 0
        )

    def test_normal_runtime_backfills_and_incrementally_reconciles_real_library(self) -> None:
        first = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="backfill-a1",
                project="project-a",
                chat="chat-a1",
                content="Project Atlas owner is Alice.",
            ),
        )
        ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="backfill-a2",
                project="project-a",
                chat="chat-a2",
                content="Project Atlas repository is local.",
            ),
        )
        ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="backfill-b1",
                project="project-b",
                chat="chat-b1",
                content="Project Beacon status is separate.",
            ),
        )
        document = self.root / "atlas.md"
        document.write_text("Release status is active. Alice owns Atlas.", encoding="utf-8")
        source = elect_file_source(self.database_path, first.project_id, document)
        document.write_text("Release status is current. Alice owns Atlas.", encoding="utf-8")
        sync_elected_source(self.database_path, source.source_id)

        initial = self._counts()
        self.assertEqual(initial["projects"], 2)
        self.assertEqual(initial["chats"], 3)
        self.assertEqual(initial["messages"], 3)
        self.assertEqual(initial["documents"], 1)
        self.assertEqual(initial["document_revision_history"], 1)
        for table in (
            "semantic_units",
            "embedding_records",
            "entity_mentions",
            "claim_evidence",
            "temporal_evidence_state",
            "claim_verification_state",
        ):
            self.assertEqual(initial[table], 0)

        started = time.monotonic()
        server, thread = self._start_runtime_library()
        try:
            self._wait_for(lambda: self._converged(server))
            convergence_seconds = time.monotonic() - started
            self.assertLess(convergence_seconds, 5.0)
            backfilled = self._counts()
            for table in (
                "semantic_units",
                "embedding_records",
                "entity_mentions",
                "claim_evidence",
                "temporal_evidence_state",
                "claim_verification_state",
            ):
                self.assertGreater(backfilled[table], 0, table)

            health_text = json.dumps(server.health_snapshot(), sort_keys=True)
            self.assertNotIn("Project Atlas owner is Alice", health_text)
            self.assertNotIn(str(self.root), health_text)
            self.assertIn("local-feature-hash-v1", health_text)

            stable_cycle = server.derived_state_maintainer.last_cycle
            time.sleep(0.35)
            self.assertIs(server.derived_state_maintainer.last_cycle, stable_cycle)

            new_message = ingest_chat_event(
                self.database_path,
                chat_event(
                    event_id="incremental-chat",
                    project="project-a",
                    chat="chat-a1",
                    content="Incremental rollout is enabled.",
                ),
            )
            self._wait_for(
                lambda: self._source_has_claim(new_message.message_id, "Incremental rollout is enabled")
            )

            document.write_text("Release status is paused. Alice owns Atlas.", encoding="utf-8")
            self._wait_for(lambda: self._document_revision_is(3))
            self._wait_for(lambda: self._claim_content_exists("Release status is paused"))
            self.assertFalse(self._claim_content_exists("Release status is current", current_only=True))

            with server.source_operation_lock:
                self.assertTrue(revoke_source_authorization(self.database_path, source.source_id))
            self._wait_for(lambda: self._counts()["documents"] == 0)
            self._wait_for(lambda: not self._claim_content_exists("Release status is paused"))
            final_counts = self._counts()
            with database(self.database_path) as connection:
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            self._stop_runtime_library(server, thread)

        restarted, restarted_thread = self._start_runtime_library()
        try:
            self._wait_for(lambda: self._converged(restarted))
            self.assertEqual(self._counts(), final_counts)
        finally:
            self._stop_runtime_library(restarted, restarted_thread)

    def _source_has_claim(self, source_id: str, content: str) -> bool:
        with database(self.database_path) as connection:
            return connection.execute(
                "SELECT 1 FROM claim_evidence WHERE source_id=? AND content LIKE ?",
                (source_id, f"%{content}%"),
            ).fetchone() is not None

    def _document_revision_is(self, revision: int) -> bool:
        with database(self.database_path) as connection:
            row = connection.execute("SELECT revision FROM documents").fetchone()
            return row is not None and int(row[0]) == revision

    def _claim_content_exists(self, content: str, *, current_only: bool = False) -> bool:
        with database(self.database_path) as connection:
            sql = (
                "SELECT 1 FROM claim_evidence e JOIN temporal_evidence_state t "
                "ON t.evidence_id=e.evidence_id WHERE e.content LIKE ?"
            )
            parameters: tuple[object, ...] = (f"%{content}%",)
            if current_only:
                sql += " AND t.state IN ('current','disputed')"
            return connection.execute(sql, parameters).fetchone() is not None

    def test_failed_atomic_cycle_preserves_authoritative_data_and_restart_recovers(self) -> None:
        receipt = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="failure-seed",
                content="Failure recovery is deterministic.",
            ),
        )
        with mock.patch.object(EntityRepository, "rebuild", side_effect=RuntimeError("bounded")):
            with self.assertRaisesRegex(RuntimeError, "bounded"):
                run_derived_state_cycle(self.database_path)

        rolled_back = self._counts()
        self.assertEqual(rolled_back["messages"], 1)
        self.assertEqual(rolled_back["semantic_units"], 0)
        self.assertEqual(rolled_back["embedding_records"], 0)
        self.assertEqual(rolled_back["claim_evidence"], 0)

        server, thread = self._start_runtime_library()
        try:
            self._wait_for(lambda: self._converged(server))
            self.assertTrue(self._source_has_claim(receipt.message_id, "Failure recovery is deterministic"))
            health = server.health_snapshot()["derived_state"]
            self.assertIsNone(health["last_error_class"])
            self.assertEqual(health["units_failed"], 0)
        finally:
            self._stop_runtime_library(server, thread)
