from __future__ import annotations

import hashlib
import time

from test_support import TemporaryDatabaseTest, chat_event

from atomizer_local_client.chat.ingestion import ingest_chat_event
from atomizer_local_client.library.document_reader import (
    list_documents,
    list_elected_sources,
    read_document,
)
from atomizer_local_client.library.document_registry import (
    authorize_directory,
    authorize_file_source,
    revoke_source_authorization,
)
from atomizer_local_client.lexical.search import search_elected_documents
from atomizer_local_client.ui.library_server import LibraryViewServer


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class AutomaticSourceMaintenanceTests(TemporaryDatabaseTest):
    def setUp(self) -> None:
        super().setUp()
        self.project_id = ingest_chat_event(
            self.database_path,
            chat_event(event_id="automatic-source-project", content="automatic source seed"),
        ).project_id

    def _wait_for(self, predicate, *, timeout: float = 4.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.025)
        self.fail("automatic source state did not converge before timeout")

    def _start_runtime(self) -> LibraryViewServer:
        return LibraryViewServer(
            self.database_path,
            0,
            automatic_maintenance=True,
            maintenance_interval_seconds=0.1,
        )

    def test_one_authorization_automatically_adds_supersedes_deletes_and_resumes(self) -> None:
        root = self.root / "authorized"
        root.mkdir()
        original = root / "FILE.md"
        old_content = "automaticoldterm"
        original.write_text(old_content, encoding="utf-8")
        authorization = authorize_directory(self.database_path, self.project_id, root)
        original_document = list_documents(self.database_path, self.project_id)[0]
        original_id = str(original_document["document_id"])

        runtime = self._start_runtime()
        try:
            added = root / "NEW_FILE.md"
            added.write_text("automaticnewfileterm", encoding="utf-8")
            self._wait_for(
                lambda: len(search_elected_documents(self.database_path, "automaticnewfileterm")) == 1
            )

            new_content = "automaticreplacementterm"
            original.write_text(new_content, encoding="utf-8")
            self._wait_for(
                lambda: not search_elected_documents(self.database_path, "automaticoldterm")
                and len(
                    search_elected_documents(
                        self.database_path, "automaticreplacementterm"
                    )
                )
                == 1
            )
            superseded = read_document(self.database_path, original_id)
            self.assertEqual(superseded["content_sha256"], _sha256(new_content))
            self.assertEqual(superseded["previous_content_sha256"], _sha256(old_content))
            self.assertIsNotNone(superseded["superseded_at"])
            self.assertEqual(superseded["revision"], 2)

            added.unlink()
            self._wait_for(
                lambda: not search_elected_documents(
                    self.database_path, "automaticnewfileterm"
                )
            )
        finally:
            runtime.server_close()

        restart_file = root / "RESTART.md"
        restart_file.write_text("automaticrestartterm", encoding="utf-8")
        self.assertEqual(
            search_elected_documents(self.database_path, "automaticrestartterm"), []
        )
        restarted = self._start_runtime()
        try:
            self._wait_for(
                lambda: len(
                    search_elected_documents(self.database_path, "automaticrestartterm")
                )
                == 1
            )
            self.assertEqual(
                list_elected_sources(self.database_path, self.project_id)[0]["source_id"],
                authorization.source_id,
            )
            self.assertEqual(
                len(
                    {
                        row["document_id"]
                        for row in list_documents(self.database_path, self.project_id)
                    }
                ),
                2,
            )
        finally:
            restarted.server_close()

    def test_automatic_scope_preserves_deterministic_move_and_overlap_deduplication(self) -> None:
        root = self.root / "scope"
        nested = root / "nested"
        outside = self.root / "outside"
        nested.mkdir(parents=True)
        outside.mkdir()
        moving = nested / "before.md"
        moving.write_text("automaticmoveterm", encoding="utf-8")
        (nested / "ignored.pdf").write_text("automaticignoredterm", encoding="utf-8")
        (outside / "outside.md").write_text("automaticoutsideterm", encoding="utf-8")
        authorize_directory(self.database_path, self.project_id, root)
        original = next(
            row
            for row in list_documents(self.database_path, self.project_id)
            if row["display_name"] == moving.name
        )
        authorize_file_source(self.database_path, self.project_id, moving)

        runtime = self._start_runtime()
        try:
            renamed = nested / "after.markdown"
            moving.rename(renamed)
            self._wait_for(
                lambda: any(
                    row["display_name"] == renamed.name
                    for row in list_documents(self.database_path, self.project_id)
                )
            )
            current = list_documents(self.database_path, self.project_id)
            self.assertEqual(len(current), 1)
            if original["file_identity"] is not None:
                self.assertEqual(current[0]["document_id"], original["document_id"])
            self.assertEqual(
                len(search_elected_documents(self.database_path, "automaticmoveterm")), 1
            )
            self.assertEqual(
                search_elected_documents(self.database_path, "automaticignoredterm"), []
            )
            self.assertEqual(
                search_elected_documents(self.database_path, "automaticoutsideterm"), []
            )

            unsupported = nested / "after.pdf"
            renamed.rename(unsupported)
            self._wait_for(
                lambda: not search_elected_documents(
                    self.database_path, "automaticmoveterm"
                )
            )
            self.assertTrue(unsupported.is_file())

            reappeared = nested / "reappeared.md"
            unsupported.rename(reappeared)
            self._wait_for(
                lambda: len(
                    search_elected_documents(self.database_path, "automaticmoveterm")
                )
                == 1
            )
        finally:
            runtime.server_close()

    def test_one_invalid_authorized_source_does_not_stop_later_sources(self) -> None:
        invalid_root = self.root / "invalid"
        valid_root = self.root / "valid"
        invalid_root.mkdir()
        valid_root.mkdir()
        (invalid_root / "invalid.txt").write_bytes(b"\xff\xfe")
        with self.assertRaises(UnicodeDecodeError):
            authorize_directory(self.database_path, self.project_id, invalid_root)
        authorize_directory(self.database_path, self.project_id, valid_root)

        runtime = self._start_runtime()
        try:
            later = valid_root / "later.md"
            later.write_text("automaticfailureisolated", encoding="utf-8")
            self._wait_for(
                lambda: len(
                    search_elected_documents(
                        self.database_path, "automaticfailureisolated"
                    )
                )
                == 1
            )
            self._wait_for(
                lambda: runtime.source_maintainer is not None
                and runtime.source_maintainer.last_cycle is not None
                and any(
                    error.error_class == "UnicodeDecodeError"
                    for error in runtime.source_maintainer.last_cycle.errors
                )
            )
        finally:
            runtime.server_close()

    def test_revocation_stops_automatic_maintenance_and_preserves_files(self) -> None:
        root = self.root / "revoke"
        root.mkdir()
        existing = root / "existing.txt"
        existing.write_text("automaticrevokepresent", encoding="utf-8")
        authorization = authorize_directory(self.database_path, self.project_id, root)
        runtime = self._start_runtime()
        try:
            self.assertTrue(
                revoke_source_authorization(self.database_path, authorization.source_id)
            )
            later = root / "later.md"
            later.write_text("automaticrevokestopped", encoding="utf-8")
            time.sleep(0.35)
            self.assertEqual(
                search_elected_documents(self.database_path, "automaticrevokepresent"), []
            )
            self.assertEqual(
                search_elected_documents(self.database_path, "automaticrevokestopped"), []
            )
            self.assertEqual(list_elected_sources(self.database_path, self.project_id), [])
            self.assertEqual(list_documents(self.database_path, self.project_id), [])
            self.assertTrue(existing.is_file())
            self.assertTrue(later.is_file())
        finally:
            runtime.server_close()


if __name__ == "__main__":
    import unittest

    unittest.main()
