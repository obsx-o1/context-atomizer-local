from __future__ import annotations

import json
import sqlite3
import threading

from test_support import TemporaryDatabaseTest, chat_event

from atomizer_local_client.chat.contracts import Host, Role
from atomizer_local_client.chat.ingestion import ingest_chat_event
from atomizer_local_client.derived_state.cycle import run_derived_state_cycle
from atomizer_local_client.history.connection import database
from atomizer_local_client.history.migrations import registered_migrations
from atomizer_local_client.hosts.codex.hook_adapter import (
    capture_codex_hook,
    capture_codex_hook_fail_open,
)
from atomizer_local_client.library.chat_forgetting import (
    CaptureBlockedError,
    ForgetOperationError,
    allow_recapture,
    forget_chat,
    list_forget_tombstones,
)
from atomizer_local_client.library.export_service import export_captured_library
from atomizer_local_client.library.view_service import (
    list_projects,
    read_chat_view,
    search_library,
)
from atomizer_local_client.mcp.server import StdioMcpServer
from atomizer_local_client.mcp.tools import LibraryToolRouter
from atomizer_local_client.memory_access.query_service import (
    LibraryQueryError,
    LibraryQueryService,
)


def _mcp_request(request_id: int, name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {
            "name": name,
            "arguments": arguments,
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        },
    }


class ChatForgettingTests(TemporaryDatabaseTest):
    def test_existing_v0_2_chat_is_backfilled_and_protected_on_upgrade(self) -> None:
        self.database_path.parent.mkdir(parents=True)
        connection = sqlite3.connect(self.database_path, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            for migration in registered_migrations()[:-1]:
                connection.executescript(migration.read_text(encoding="utf-8"))
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (migration.stem, "2026-09-02T00:00:00+00:00"),
                )
            connection.execute(
                """
                INSERT INTO projects(
                    project_id, host, host_project_reference, display_name,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "legacy-project-id",
                    Host.CODEX.value,
                    "legacy-project",
                    "Legacy Project",
                    "2026-09-02T00:00:00+00:00",
                    "2026-09-02T00:00:00+00:00",
                ),
            )
            connection.execute(
                """
                INSERT INTO chats(
                    chat_id, project_id, host, host_chat_reference, display_title,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "legacy-chat-id",
                    "legacy-project-id",
                    Host.CODEX.value,
                    "legacy-stable-chat",
                    "Legacy Chat",
                    "2026-09-02T00:00:00+00:00",
                    "2026-09-02T00:00:00+00:00",
                ),
            )
        finally:
            connection.close()

        with database(self.database_path) as migrated:
            self.assertEqual(
                migrated.execute(
                    "SELECT count(*) FROM chat_identity_aliases WHERE chat_id = ?",
                    ("legacy-chat-id",),
                ).fetchone()[0],
                1,
            )
        self.assertEqual(forget_chat(self.database_path, "legacy-chat-id").status, "forgotten")
        with self.assertRaises(CaptureBlockedError):
            ingest_chat_event(
                self.database_path,
                chat_event(
                    event_id="legacy-recapture",
                    chat="legacy-stable-chat",
                    project="legacy-project",
                ),
            )

    def test_authoritative_payload_search_retrieval_and_mcp_exclusion(self) -> None:
        forgotten = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="forget-source",
                chat="forget-chat",
                project="forget-project",
                project_name="Forget Project",
                content="Project Forgetneedle uses obsidian storage.",
            ),
        )
        survivor = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="survivor-source",
                chat="survivor-chat",
                project="survivor-project",
                project_name="Survivor Project",
                content="Project Survivor uses quartz storage.",
            ),
        )
        run_derived_state_cycle(self.database_path)
        service = LibraryQueryService(self.database_path)
        self.assertGreater(service.search_library("Forgetneedle")["result_count"], 0)

        result = forget_chat(self.database_path, forgotten.chat_id)
        self.assertEqual(result.status, "forgotten")
        with database(self.database_path) as connection:
            for table, predicate, parameters in (
                ("chats", "chat_id = ?", (forgotten.chat_id,)),
                ("messages", "message_id = ?", (forgotten.message_id,)),
                ("lexical_entries", "chat_id = ?", (forgotten.chat_id,)),
                ("semantic_units", "chat_id = ?", (forgotten.chat_id,)),
                ("claim_evidence", "source_id = ?", (forgotten.message_id,)),
                ("entity_mentions", "source_id = ?", (forgotten.message_id,)),
            ):
                self.assertEqual(
                    connection.execute(
                        f"SELECT count(*) FROM {table} WHERE {predicate}", parameters
                    ).fetchone()[0],
                    0,
                    table,
                )
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM messages WHERE message_id = ?", (survivor.message_id,)
                ).fetchone()
            )
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

        with self.assertRaises(KeyError):
            read_chat_view(self.database_path, forgotten.chat_id)
        self.assertFalse(
            any(item["project_id"] == forgotten.project_id for item in list_projects(self.database_path))
        )
        self.assertEqual(search_library(self.database_path, "Forgetneedle"), [])
        retrieved = service.search_library("Forgetneedle")
        self.assertTrue(
            all(item["source_id"] != forgotten.message_id for item in retrieved["items"])
        )
        self.assertNotIn("Forgetneedle", json.dumps(retrieved, ensure_ascii=False))
        self.assertNotIn(
            "Forgetneedle", json.dumps(service.recent_library_context(), ensure_ascii=False)
        )
        with self.assertRaises(LibraryQueryError):
            service.get_library_item(forgotten.message_id)

        server = StdioMcpServer(LibraryToolRouter(service))
        response = server.handle(_mcp_request(1, "search_library", {"query": "Forgetneedle"}))
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertTrue(
            all(item["source_id"] != forgotten.message_id for item in payload["items"])
        )
        self.assertNotIn("Forgetneedle", json.dumps(payload, ensure_ascii=False))
        self.assertNotIn("Forgetneedle", export_captured_library(self.database_path).decode("utf-8"))

    def test_exclusive_derived_state_is_deleted_and_shared_provenance_survives(self) -> None:
        first = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="shared-first",
                chat="shared-chat-one",
                project="shared-project-one",
                content="Project Atlas uses SQLite.",
            ),
        )
        exclusive = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="exclusive-first",
                chat="shared-chat-one",
                project="shared-project-one",
                role=Role.ASSISTANT,
                content="Project Ember uses PostgreSQL.",
            ),
        )
        second = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="shared-second",
                chat="shared-chat-two",
                project="shared-project-two",
                content="Project Atlas uses SQLite.",
            ),
        )
        run_derived_state_cycle(self.database_path)
        with database(self.database_path) as connection:
            first_claims = {
                str(row[0])
                for row in connection.execute(
                    "SELECT claim_id FROM claim_evidence WHERE source_id IN (?, ?)",
                    (first.message_id, exclusive.message_id),
                )
            }
            second_claims = {
                str(row[0])
                for row in connection.execute(
                    "SELECT claim_id FROM claim_evidence WHERE source_id = ?",
                    (second.message_id,),
                )
            }
        shared_claims = first_claims & second_claims
        exclusive_claims = first_claims - second_claims
        self.assertTrue(shared_claims)
        self.assertTrue(exclusive_claims)

        self.assertEqual(forget_chat(self.database_path, first.chat_id).status, "forgotten")
        with database(self.database_path) as connection:
            for claim_id in shared_claims:
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT 1 FROM claims WHERE claim_id = ?", (claim_id,)
                    ).fetchone()
                )
                sources = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT source_id FROM claim_evidence WHERE claim_id = ?", (claim_id,)
                    )
                }
                self.assertEqual(sources, {second.message_id})
            for claim_id in exclusive_claims:
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM claims WHERE claim_id = ?", (claim_id,)
                    ).fetchone()
                )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM messages WHERE chat_id = ?", (second.chat_id,)
                ).fetchone()[0],
                1,
            )

    def test_repeated_forget_and_unknown_identity_results_are_deterministic(self) -> None:
        receipt = ingest_chat_event(
            self.database_path, chat_event(event_id="idempotent", chat="idempotent-chat")
        )
        first = forget_chat(self.database_path, receipt.chat_id)
        second = forget_chat(self.database_path, receipt.chat_id)
        unknown_a = forget_chat(self.database_path, "unknown-chat")
        unknown_b = forget_chat(self.database_path, "unknown-chat")
        self.assertEqual(first.status, "forgotten")
        self.assertEqual(second, type(second)("already_forgotten", first.tombstone_id))
        self.assertEqual(unknown_a.status, "not_found")
        self.assertEqual(unknown_a, unknown_b)

    def test_stable_and_duplicate_recapture_stay_blocked_across_connections(self) -> None:
        original = chat_event(
            event_id="stable-duplicate",
            chat="stable-forgotten-chat",
            content="Stable forgotten content.",
        )
        receipt = ingest_chat_event(self.database_path, original)
        forget_chat(self.database_path, receipt.chat_id)
        for event in (
            original,
            chat_event(
                event_id="stable-new-event",
                chat="stable-forgotten-chat",
                content="New content must remain blocked.",
            ),
        ):
            with self.assertRaises(CaptureBlockedError):
                ingest_chat_event(self.database_path, event)
        with database(self.database_path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM chats").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
            self.assertEqual(
                connection.execute("SELECT count(*) FROM chat_forget_tombstones").fetchone()[0],
                1,
            )

    def test_provisional_to_forgotten_stable_rebind_is_purged_and_bound(self) -> None:
        stable = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="stable-before-provisional",
                chat="stable-web-chat",
                host=Host.CHATGPT_WEB,
                content="Old stable content.",
            ),
        )
        group = forget_chat(self.database_path, stable.chat_id).tombstone_id
        provisional_reference = "provisional:new-chat:later-resolution"
        provisional = chat_event(
            event_id="provisional-later",
            chat=provisional_reference,
            turn="shared-turn",
            host=Host.CHATGPT_WEB,
            content="Provisional content must be removed.",
        )
        provisional_receipt = ingest_chat_event(self.database_path, provisional)
        rebound = chat_event(
            event_id="stable-rebind-blocked",
            chat="stable-web-chat",
            turn="shared-turn",
            host=Host.CHATGPT_WEB,
            content=provisional.content,
            rebind_from=provisional_reference,
        )
        with self.assertRaises(CaptureBlockedError):
            ingest_chat_event(self.database_path, rebound)
        with database(self.database_path) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM chats WHERE chat_id = ?", (provisional_receipt.chat_id,)
                ).fetchone()
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM messages WHERE content LIKE '%Provisional content%'"
                ).fetchone()
            )
            rows = connection.execute(
                "SELECT DISTINCT tombstone_id FROM chat_forget_tombstones"
            ).fetchall()
            self.assertEqual({str(row[0]) for row in rows}, {group})
            self.assertEqual(
                connection.execute("SELECT count(*) FROM chat_forget_tombstones").fetchone()[0],
                2,
            )

    def test_forgotten_provisional_denies_its_first_stable_resolution(self) -> None:
        provisional_reference = "provisional:new-chat:forgotten-before-stable"
        provisional_event = chat_event(
            event_id="provisional-before-stable",
            chat=provisional_reference,
            turn="provisional-stable-turn",
            host=Host.CHATGPT_WEB,
            content="Provisional content forgotten before resolution.",
        )
        provisional = ingest_chat_event(self.database_path, provisional_event)
        group = forget_chat(self.database_path, provisional.chat_id).tombstone_id
        stable_event = chat_event(
            event_id="first-stable-resolution",
            chat="first-stable-after-forget",
            turn="provisional-stable-turn",
            host=Host.CHATGPT_WEB,
            content=provisional_event.content,
            rebind_from=provisional_reference,
        )
        with self.assertRaises(CaptureBlockedError):
            ingest_chat_event(self.database_path, stable_event)
        with self.assertRaises(CaptureBlockedError):
            ingest_chat_event(
                self.database_path,
                chat_event(
                    event_id="stable-recapture-after-resolution",
                    chat="first-stable-after-forget",
                    host=Host.CHATGPT_WEB,
                    content="Stable capture remains denied.",
                ),
            )
        tombstones = list_forget_tombstones(self.database_path)
        self.assertEqual(tombstones[0]["tombstone_id"], group)
        self.assertEqual(tombstones[0]["identity_count"], 2)

    def test_unproven_rebind_cannot_delete_or_tombstone_an_unrelated_chat(self) -> None:
        provisional_reference = "provisional:new-chat:forgotten-isolation"
        provisional = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="forgotten-isolation-source",
                chat=provisional_reference,
                host=Host.CHATGPT_WEB,
                content="Forgotten provisional isolation content.",
            ),
        )
        forget_chat(self.database_path, provisional.chat_id)
        unrelated_event = chat_event(
            event_id="unrelated-existing",
            chat="unrelated-stable-target",
            turn="unrelated-turn",
            host=Host.CHATGPT_WEB,
            content="Unrelated content must survive.",
        )
        unrelated = ingest_chat_event(self.database_path, unrelated_event)
        with self.assertRaises(CaptureBlockedError):
            ingest_chat_event(
                self.database_path,
                chat_event(
                    event_id="unproven-rebind",
                    chat="unrelated-stable-target",
                    turn="different-turn",
                    host=Host.CHATGPT_WEB,
                    content="Different content cannot prove association.",
                    rebind_from=provisional_reference,
                ),
            )
        with database(self.database_path) as connection:
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM chats WHERE chat_id = ?", (unrelated.chat_id,)
                ).fetchone()
            )
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM messages WHERE message_id = ?", (unrelated.message_id,)
                ).fetchone()
            )
        duplicate = ingest_chat_event(self.database_path, unrelated_event)
        self.assertEqual(duplicate.chat_id, unrelated.chat_id)

    def test_recorded_provisional_alias_cannot_bypass_stable_forget(self) -> None:
        provisional_reference = "provisional:new-chat:recorded-alias"
        provisional = chat_event(
            event_id="provisional-recorded",
            chat=provisional_reference,
            turn="recorded-turn",
            host=Host.CHATGPT_WEB,
            content="Recorded alias content.",
        )
        ingest_chat_event(self.database_path, provisional)
        stable_event = chat_event(
            event_id="recorded-stable",
            chat="recorded-stable-chat",
            turn="recorded-turn",
            host=Host.CHATGPT_WEB,
            content=provisional.content,
            rebind_from=provisional_reference,
        )
        stable = ingest_chat_event(self.database_path, stable_event)
        forget_chat(self.database_path, stable.chat_id)
        with self.assertRaises(CaptureBlockedError):
            ingest_chat_event(
                self.database_path,
                chat_event(
                    event_id="old-alias-recapture",
                    chat=provisional_reference,
                    host=Host.CHATGPT_WEB,
                    content="Alias bypass denied.",
                ),
            )
        self.assertEqual(list_forget_tombstones(self.database_path)[0]["identity_count"], 2)

    def test_capture_forget_race_serializes_without_resurrection(self) -> None:
        receipt = ingest_chat_event(
            self.database_path,
            chat_event(event_id="race-original", chat="race-chat", content="Race original."),
        )
        barrier = threading.Barrier(3)
        outcomes: list[str] = []
        unexpected: list[BaseException] = []

        def run_forget() -> None:
            try:
                barrier.wait()
                outcomes.append(forget_chat(self.database_path, receipt.chat_id).status)
            except BaseException as error:
                unexpected.append(error)

        def run_capture() -> None:
            try:
                barrier.wait()
                ingest_chat_event(
                    self.database_path,
                    chat_event(event_id="race-later", chat="race-chat", content="Race later."),
                )
                outcomes.append("captured-before-forget")
            except CaptureBlockedError:
                outcomes.append("capture-blocked")
            except BaseException as error:
                unexpected.append(error)

        threads = [threading.Thread(target=run_forget), threading.Thread(target=run_capture)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(unexpected, [])
        self.assertIn("forgotten", outcomes)
        with database(self.database_path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM chats").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
            self.assertEqual(
                connection.execute("SELECT count(*) FROM chat_forget_tombstones").fetchone()[0],
                1,
            )

    def test_tombstone_is_minimal_hashed_and_contains_no_raw_content(self) -> None:
        raw_reference = "https://chat.example/private/conversation?token=raw-secret"
        secret_content = "forgotten-super-secret-message"
        receipt = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="privacy-forget",
                chat=raw_reference,
                host=Host.CHATGPT_WEB,
                content=secret_content,
            ),
        )
        forget_chat(self.database_path, receipt.chat_id)
        with database(self.database_path) as connection:
            columns = tuple(
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(chat_forget_tombstones)")
            )
            row = connection.execute("SELECT * FROM chat_forget_tombstones").fetchone()
            aliases = connection.execute("SELECT count(*) FROM chat_identity_aliases").fetchone()[0]
        self.assertEqual(
            columns, ("identity_digest", "tombstone_id", "host", "forgotten_at")
        )
        self.assertIsNotNone(row)
        serialized = json.dumps(dict(row), ensure_ascii=False)
        self.assertNotIn(raw_reference, serialized)
        self.assertNotIn("raw-secret", serialized)
        self.assertNotIn(secret_content, serialized)
        self.assertNotIn("Chat https", serialized)
        self.assertRegex(str(row["identity_digest"]), r"^[0-9a-f]{64}$")
        self.assertRegex(str(row["tombstone_id"]), r"^[0-9a-f]{64}$")
        self.assertEqual(aliases, 0)

    def test_allow_recapture_never_restores_and_only_then_permits_fresh_capture(self) -> None:
        original = chat_event(
            event_id="allow-original",
            chat="allow-chat",
            content="Deleted content must never return.",
        )
        receipt = ingest_chat_event(self.database_path, original)
        group = forget_chat(self.database_path, receipt.chat_id).tombstone_id
        with self.assertRaises(CaptureBlockedError):
            ingest_chat_event(self.database_path, original)
        self.assertTrue(allow_recapture(self.database_path, str(group)))
        self.assertFalse(allow_recapture(self.database_path, str(group)))
        with database(self.database_path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM chats").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
        fresh = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="allow-fresh",
                chat="allow-chat",
                content="Fresh content after explicit permission.",
            ),
        )
        with database(self.database_path) as connection:
            content = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT content FROM messages WHERE chat_id = ?", (fresh.chat_id,)
                )
            )
            self.assertEqual(content, ("Fresh content after explicit permission.",))
            self.assertEqual(
                connection.execute("SELECT count(*) FROM chat_forget_tombstones").fetchone()[0],
                0,
            )

    def test_injected_delete_failure_is_error_protected_and_retryable(self) -> None:
        secret = "failure-secret-content"
        receipt = ingest_chat_event(
            self.database_path,
            chat_event(event_id="failure-original", chat="failure-chat", content=secret),
        )
        with database(self.database_path) as connection:
            connection.execute(
                f"""
                CREATE TRIGGER injected_forget_failure BEFORE DELETE ON chats
                WHEN OLD.chat_id = '{receipt.chat_id}'
                BEGIN SELECT RAISE(ABORT, 'injected cleanup failure'); END
                """
            )
        with self.assertRaises(ForgetOperationError) as raised:
            forget_chat(self.database_path, receipt.chat_id)
        self.assertNotIn(secret, str(raised.exception))
        with database(self.database_path) as connection:
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM chats WHERE chat_id = ?", (receipt.chat_id,)
                ).fetchone()
            )
            self.assertEqual(
                connection.execute("SELECT count(*) FROM chat_forget_tombstones").fetchone()[0],
                1,
            )
        with self.assertRaises(CaptureBlockedError):
            ingest_chat_event(
                self.database_path,
                chat_event(event_id="failure-recapture", chat="failure-chat", content=secret),
            )
        with database(self.database_path) as connection:
            connection.execute("DROP TRIGGER injected_forget_failure")
        self.assertEqual(forget_chat(self.database_path, receipt.chat_id).status, "forgotten")

    def test_blocked_capture_log_and_error_do_not_expose_forgotten_content(self) -> None:
        original_payload = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "log-session-private",
            "turn_id": "log-turn-one",
            "prompt": "log-secret-original",
            "cwd": str(self.root),
        }
        receipt = capture_codex_hook(original_payload, self.database_path)
        self.assertIsNotNone(receipt)
        forget_chat(self.database_path, receipt.chat_id)
        blocked_payload = {
            **original_payload,
            "turn_id": "log-turn-two",
            "prompt": "log-secret-blocked",
        }
        self.assertFalse(capture_codex_hook_fail_open(blocked_payload, self.database_path))
        log = (self.database_path.parent / "capture-errors.log").read_text(encoding="utf-8")
        self.assertIn("CaptureBlockedError", log)
        for forbidden in (
            "log-secret-original",
            "log-secret-blocked",
            "log-session-private",
            str(self.root),
        ):
            self.assertNotIn(forbidden, log)
        self.assertTrue(all(len(line) < 160 for line in log.splitlines()))


if __name__ == "__main__":
    import unittest

    unittest.main()
