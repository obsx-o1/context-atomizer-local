from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from test_support import SOURCE_ROOT, TemporaryDatabaseTest, chat_event

from atomizer_local_client.bridge.local_ingress import LocalIngressServer
from atomizer_local_client.chat.ingestion import ingest_chat_event
from atomizer_local_client.history.connection import database, transaction
from atomizer_local_client.library.document_reader import list_documents, list_elected_sources
from atomizer_local_client.library.document_registry import authorize_directory, sync_elected_source
from atomizer_local_client.local_auth.contracts import capture_request_material, sign_hex
from atomizer_local_client.local_auth.pairing import ExtensionPairingAuthority
from atomizer_local_client.lexical.audit import audit_lexical_consistency
from atomizer_local_client.lexical.search import search_all_local_history, search_elected_documents
from atomizer_local_client.runtime_health import RuntimeIdentity
from atomizer_local_client.ui.library_server import LibraryViewServer


class RecoveryDurabilityTests(TemporaryDatabaseTest):
    token = "recovery-test-token-0123456789-abcdef"

    class _SecretStore:
        value: str | None = None

        def load(self) -> str:
            if self.value is None:
                raise FileNotFoundError
            return self.value

        def rotate(self) -> str:
            self.value = "recovery-extension-secret-0123456789-abcdef"
            return self.value

        def remove(self) -> None:
            self.value = None

    def _start_bridge(self, port: int = 0) -> tuple[LocalIngressServer, threading.Thread]:
        pairing = ExtensionPairingAuthority(self._SecretStore())
        self.extension_secret = pairing.pair(pairing.issue_code())
        server = LocalIngressServer(
            self.database_path,
            self.token,
            pairing,
            _test_port=port,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def _stop_server(self, server: object, thread: threading.Thread) -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    def _bridge_request(
        self, port: int, payload: dict[str, object]
    ) -> tuple[int, dict[str, object], object]:
        body = json.dumps(payload).encode("utf-8")
        nonce = os.urandom(24).hex()
        timestamp = str(int(time.time()))
        body_sha256 = hashlib.sha256(body).hexdigest()
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/chat-events",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Atomizer-Protocol": "1",
                "X-Atomizer-Nonce": nonce,
                "X-Atomizer-Timestamp": timestamp,
                "X-Atomizer-Content-SHA256": body_sha256,
                "X-Atomizer-Signature": sign_hex(
                    self.extension_secret,
                    capture_request_material(
                        method="POST",
                        operation="/v1/chat-events",
                        nonce=nonce,
                        timestamp=timestamp,
                        body_sha256=body_sha256,
                    ),
                ),
            },
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read()), response.headers

    def test_bridge_unavailable_restart_and_idempotent_replay(self) -> None:
        payload = {
            "event_id": "recovery-bridge-event",
            "host": "chatgpt_web",
            "host_project_reference": "recovery-project",
            "host_chat_reference": "recovery-chat",
            "host_turn_reference": "recovery-turn",
            "role": "user",
            "content": "durable bridge replay term",
            "captured_at": "2026-08-11T12:00:00+00:00",
            "project_display_name": "Recovery Project",
            "chat_display_name": "Recovery Chat",
        }
        first, first_thread = self._start_bridge()
        port = int(first.server_address[1])
        status, receipt, headers = self._bridge_request(port, payload)
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Atomizer-Protocol-Version"], "1")
        self.assertEqual(headers["X-Atomizer-Restart-Required"], "false")
        self._stop_server(first, first_thread)

        with self.assertRaises(urllib.error.URLError):
            self._bridge_request(port, payload)

        second, second_thread = self._start_bridge(port)
        try:
            _, replay, _ = self._bridge_request(port, payload)
            self.assertFalse(replay["inserted"])
            self.assertEqual(replay["message_id"], receipt["message_id"])
            self.assertEqual(
                len(search_all_local_history(self.database_path, "durable bridge replay term")), 1
            )
        finally:
            self._stop_server(second, second_thread)

    def test_content_free_application_health_reports_runtime_database_maintenance_and_bridge(self) -> None:
        ingest_chat_event(
            self.database_path,
            chat_event(event_id="health-seed", content="health seed content"),
        )
        bridge, bridge_thread = self._start_bridge()
        library = LibraryViewServer(
            self.database_path,
            0,
            bridge_port=int(bridge.server_address[1]),
            maintenance_interval_seconds=0.1,
        )
        library_thread = threading.Thread(target=library.serve_forever, daemon=True)
        library_thread.start()
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{library.server_address[1]}/health", timeout=2
            ) as response:
                public_health = json.loads(response.read())
            self.assertEqual(
                set(public_health), {"ok", "service", "runtime_running"}
            )
            health = library.health_snapshot()
            self.assertTrue(health["ok"])
            self.assertTrue(health["runtime_running"])
            self.assertEqual(health["database"]["state"], "healthy")
            self.assertEqual(health["source_maintenance"]["state"], "running")
            self.assertTrue(health["browser_bridge"]["reachable"])
            encoded = json.dumps(health)
            self.assertNotIn("health seed content", encoded)
            self.assertNotIn(str(self.root), encoded)

            library.source_maintainer.stop()
            with urllib.request.urlopen(
                f"http://127.0.0.1:{library.server_address[1]}/health", timeout=2
            ) as response:
                paused_public = json.loads(response.read())
            self.assertEqual(
                set(paused_public), {"ok", "service", "runtime_running"}
            )
            paused = library.health_snapshot()
            self.assertEqual(paused["source_maintenance"]["state"], "paused")
            self.assertTrue(paused["ok"])
        finally:
            self._stop_server(library, library_thread)
            self._stop_server(bridge, bridge_thread)

    def test_runtime_identity_detects_source_changed_after_process_start(self) -> None:
        package_root = self.root / "runtime"
        package_root.mkdir()
        source = package_root / "worker.py"
        source.write_text("VALUE = 1\n", encoding="utf-8")
        identity = RuntimeIdentity(package_root)
        self.assertFalse(identity.snapshot()["restart_required"])
        source.write_text("VALUE = 2\n", encoding="utf-8")
        snapshot = identity.snapshot()
        self.assertTrue(snapshot["restart_required"])
        self.assertNotEqual(snapshot["startup_build_sha256"], snapshot["current_build_sha256"])

    def test_unclean_idle_runtime_and_interrupted_transaction_reopen_cleanly(self) -> None:
        receipt = ingest_chat_event(
            self.database_path,
            chat_event(event_id="unclean-seed", content="original durable content"),
        )
        source_root = self.root / "unclean-source"
        source_root.mkdir()
        (source_root / "DURABLE.md").write_text(
            "original document durable content", encoding="utf-8"
        )
        authorize_directory(self.database_path, receipt.project_id, source_root)
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(SOURCE_ROOT)
        idle_script = (
            "from pathlib import Path\n"
            "from atomizer_local_client.ui.library_server import LibraryViewServer\n"
            f"server=LibraryViewServer(Path({str(self.database_path)!r}),0,automatic_maintenance=False)\n"
            "print(server.server_address[1],flush=True)\n"
            "server.serve_forever()\n"
        )
        idle = subprocess.Popen(
            [sys.executable, "-u", "-c", idle_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        self.assertTrue(idle.stdout.readline().strip().isdigit())
        idle.terminate()
        idle.wait(timeout=5)
        idle.stdout.close()
        idle.stderr.close()

        transaction_script = (
            "import sqlite3,time\n"
            f"connection=sqlite3.connect({str(self.database_path)!r},isolation_level=None)\n"
            "connection.execute('BEGIN IMMEDIATE')\n"
            "connection.execute(\"UPDATE messages SET content='never committed'\")\n"
            "connection.execute(\"UPDATE documents SET text_content='never committed document'\")\n"
            "connection.execute(\"UPDATE lexical_entries SET content='never committed document' WHERE corpus_type='ELECTED_DOCUMENT'\")\n"
            "print('ready',flush=True)\n"
            "time.sleep(30)\n"
        )
        writer = subprocess.Popen(
            [sys.executable, "-u", "-c", transaction_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(writer.stdout.readline().strip(), "ready")
        writer.terminate()
        writer.wait(timeout=5)
        writer.stdout.close()
        writer.stderr.close()

        with database(self.database_path) as connection:
            content = connection.execute("SELECT content FROM messages").fetchone()[0]
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(content, "original durable content")
        self.assertEqual(
            len(
                search_elected_documents(
                    self.database_path, "original document durable content"
                )
            ),
            1,
        )
        self.assertEqual(
            search_elected_documents(self.database_path, "never committed document"), []
        )
        self.assertTrue(audit_lexical_consistency(self.database_path)["passed"])

    def test_close_reopen_preserves_chat_document_ids_and_search_results(self) -> None:
        receipt = ingest_chat_event(
            self.database_path,
            chat_event(event_id="reopen-chat", content="combined reopen chat term"),
        )
        source_root = self.root / "authorized"
        source_root.mkdir()
        (source_root / "RECOVERY.md").write_text("combined reopen document term", encoding="utf-8")
        authorization = authorize_directory(self.database_path, receipt.project_id, source_root)
        document_id = str(list_documents(self.database_path)[0]["document_id"])
        before_chat = search_all_local_history(
            self.database_path, "combined reopen chat term"
        )[0]
        before_document = search_elected_documents(
            self.database_path, "combined reopen document term"
        )[0]

        with database(self.database_path) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        sync_elected_source(self.database_path, authorization.source_id)

        after_chat = search_all_local_history(
            self.database_path, "combined reopen chat term"
        )[0]
        after_document = search_elected_documents(
            self.database_path, "combined reopen document term"
        )[0]
        self.assertEqual(after_chat.source_id, before_chat.source_id)
        self.assertEqual(after_document.source_id, before_document.source_id)
        self.assertEqual(after_document.source_id, document_id)

    def test_authorized_root_disappears_and_returns_without_losing_authorization(self) -> None:
        receipt = ingest_chat_event(
            self.database_path,
            chat_event(event_id="root-recovery", content="root recovery seed"),
        )
        source_root = self.root / "recoverable-root"
        source_root.mkdir()
        document = source_root / "ROOT.md"
        document.write_text("recoverable root term", encoding="utf-8")
        authorization = authorize_directory(self.database_path, receipt.project_id, source_root)
        original_id = str(list_documents(self.database_path)[0]["document_id"])
        parked_root = self.root / "parked-root"
        source_root.rename(parked_root)
        missing = sync_elected_source(self.database_path, authorization.source_id)
        self.assertEqual(missing.removed, 1)
        self.assertEqual(list_documents(self.database_path), [])
        self.assertEqual(len(list_elected_sources(self.database_path)), 1)
        parked_root.rename(source_root)
        returned = sync_elected_source(self.database_path, authorization.source_id)
        self.assertEqual(returned.added, 1)
        self.assertEqual(str(list_documents(self.database_path)[0]["document_id"]), original_id)
        self.assertEqual(len(search_elected_documents(self.database_path, "recoverable root term")), 1)

    def test_fts_projection_corruption_is_detected_and_explicit_rebuild_restores_it(self) -> None:
        ingest_chat_event(
            self.database_path,
            chat_event(event_id="fts-recovery", content="projection rebuild term"),
        )
        self.assertTrue(audit_lexical_consistency(self.database_path)["passed"])
        with database(self.database_path) as connection:
            row = connection.execute(
                "SELECT rowid FROM lexical_entries WHERE source_id=(SELECT message_id FROM messages)"
            ).fetchone()
            with transaction(connection):
                connection.execute("DELETE FROM lexical_entries_fts WHERE rowid=?", (row[0],))
        broken = audit_lexical_consistency(self.database_path)
        self.assertFalse(broken["passed"])
        self.assertEqual(broken["checks"]["lexical_without_fts"], 1)
        with database(self.database_path) as connection:
            with transaction(connection):
                connection.execute(
                    "INSERT INTO lexical_entries_fts(lexical_entries_fts) VALUES('rebuild')"
                )
        rebuilt = audit_lexical_consistency(self.database_path)
        self.assertTrue(rebuilt["passed"])
        self.assertEqual(
            len(search_all_local_history(self.database_path, "projection rebuild term")), 1
        )
