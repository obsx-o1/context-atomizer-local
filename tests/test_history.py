from __future__ import annotations

import sqlite3
from dataclasses import replace

from test_support import TemporaryDatabaseTest, chat_event

from atomizer_local_client.chat.contracts import Host, Role
from atomizer_local_client.chat.ingestion import ingest_chat_event
from atomizer_local_client.derived_state.cycle import run_derived_state_cycle
from atomizer_local_client.history.connection import database, transaction
from atomizer_local_client.history.migrations import registered_migration_ids
from atomizer_local_client.history.message_reader import read_project_tree
from atomizer_local_client.library.document_registry import elect_file_source
from atomizer_local_client.projects.repository import get_or_create_project


class HistoryTests(TemporaryDatabaseTest):
    def test_provisional_new_chat_rebind_preserves_one_chat_and_both_real_messages(self) -> None:
        provisional = "provisional:new-chat:submission-one"
        prompt = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="provisional-prompt",
                chat=provisional,
                turn="submit-one",
                content="new ChatGPT prompt",
                host=Host.CHATGPT_WEB,
            ),
        )
        rebound = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="stable-replay",
                chat="stable-conversation-one",
                turn="submit-one",
                content="new ChatGPT prompt",
                host=Host.CHATGPT_WEB,
                rebind_from=provisional,
            ),
        )
        duplicate_rebound = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="stable-replay",
                chat="stable-conversation-one",
                turn="submit-one",
                content="new ChatGPT prompt",
                host=Host.CHATGPT_WEB,
                rebind_from=provisional,
            ),
        )
        assistant = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="stable-assistant",
                chat="stable-conversation-one",
                turn="assistant-one",
                role=Role.ASSISTANT,
                content="final assistant response",
                host=Host.CHATGPT_WEB,
            ),
        )

        self.assertEqual(prompt.chat_id, rebound.chat_id)
        self.assertEqual(prompt.chat_id, assistant.chat_id)
        self.assertEqual(prompt.chat_id, duplicate_rebound.chat_id)
        self.assertTrue(prompt.inserted)
        self.assertFalse(rebound.inserted)
        self.assertFalse(duplicate_rebound.inserted)
        self.assertTrue(assistant.inserted)
        with database(self.database_path) as connection:
            chats = connection.execute(
                "SELECT chat_id, host_chat_reference, display_title FROM chats WHERE host = ?",
                (Host.CHATGPT_WEB.value,),
            ).fetchall()
            messages = connection.execute(
                "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY sequence_number",
                (prompt.chat_id,),
            ).fetchall()
            lexical_chat_ids = connection.execute(
                "SELECT DISTINCT chat_id FROM lexical_entries WHERE chat_id = ?",
                (prompt.chat_id,),
            ).fetchall()
            provisional_count = connection.execute(
                "SELECT COUNT(*) FROM chats WHERE host = ? AND host_chat_reference LIKE ?",
                (Host.CHATGPT_WEB.value, "provisional:new-chat:%"),
            ).fetchone()[0]
            thinking_count = connection.execute(
                """
                SELECT COUNT(*) FROM messages m JOIN chats c ON c.chat_id = m.chat_id
                WHERE c.host = ? AND m.role = ? AND m.content = ?
                """,
                (Host.CHATGPT_WEB.value, Role.ASSISTANT.value, "Thinking"),
            ).fetchone()[0]
            distinct_chat_count = connection.execute(
                "SELECT COUNT(DISTINCT chat_id) FROM messages WHERE content IN (?, ?)",
                ("new ChatGPT prompt", "final assistant response"),
            ).fetchone()[0]
            temporary_flow_chats = connection.execute(
                """
                SELECT COUNT(DISTINCT c.chat_id)
                FROM chats c JOIN messages m ON m.chat_id = c.chat_id
                WHERE m.content IN (?, ?) AND (
                    c.host_chat_reference = 'route:/' OR
                    c.host_chat_reference LIKE 'provisional:new-chat:%' OR
                    c.host_chat_reference LIKE 'WEB:%'
                )
                """,
                ("new ChatGPT prompt", "final assistant response"),
            ).fetchone()[0]
            lexical_counts = connection.execute(
                """
                SELECT COUNT(*) AS total, COUNT(DISTINCT source_id) AS distinct_sources
                FROM lexical_entries WHERE source_id IN (?, ?)
                """,
                (prompt.message_id, assistant.message_id),
            ).fetchone()
        self.assertEqual(len(chats), 1)
        self.assertEqual(chats[0]["host_chat_reference"], "stable-conversation-one")
        self.assertNotEqual(chats[0]["host_chat_reference"], "route:/")
        self.assertEqual(
            [(row["role"], row["content"]) for row in messages],
            [("user", "new ChatGPT prompt"), ("assistant", "final assistant response")],
        )
        self.assertEqual(len(lexical_chat_ids), 1)
        self.assertEqual(provisional_count, 0)
        self.assertEqual(thinking_count, 0)
        self.assertEqual(distinct_chat_count, 1)
        self.assertEqual(temporary_flow_chats, 0)
        self.assertEqual((lexical_counts["total"], lexical_counts["distinct_sources"]), (2, 2))

    def test_rebind_never_merges_a_provisional_chat_into_an_existing_stable_chat(self) -> None:
        ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="existing-message",
                chat="existing-stable",
                turn="existing-turn",
                content="existing chat message",
                host=Host.CHATGPT_WEB,
            ),
        )
        provisional = "provisional:new-chat:separate-submission"
        ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="separate-message",
                chat=provisional,
                turn="separate-turn",
                content="separate chat message",
                host=Host.CHATGPT_WEB,
            ),
        )
        with self.assertRaisesRegex(ValueError, "already belongs to another"):
            ingest_chat_event(
                self.database_path,
                chat_event(
                    event_id="unsafe-rebind",
                    chat="existing-stable",
                    turn="separate-turn",
                    content="separate chat message",
                    host=Host.CHATGPT_WEB,
                    rebind_from=provisional,
                ),
            )
        with database(self.database_path) as connection:
            references = connection.execute(
                "SELECT host_chat_reference FROM chats WHERE host = ? ORDER BY host_chat_reference",
                (Host.CHATGPT_WEB.value,),
            ).fetchall()
        self.assertEqual(
            [row["host_chat_reference"] for row in references],
            ["existing-stable", provisional],
        )

    def test_assistant_observation_cannot_request_a_provisional_rebind(self) -> None:
        with self.assertRaisesRegex(ValueError, "identified user submission"):
            chat_event(
                event_id="assistant-rebind",
                chat="stable-target",
                turn="assistant-turn",
                role=Role.ASSISTANT,
                content="assistant content",
                host=Host.CHATGPT_WEB,
                rebind_from="provisional:new-chat:assistant-attempt",
            )

    def test_project_chat_message_hierarchy_and_multiple_chats(self) -> None:
        first = ingest_chat_event(
            self.database_path,
            chat_event(event_id="one", chat="chat-a", turn="turn-a", content="first prompt"),
        )
        ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="two",
                chat="chat-a",
                turn="turn-a",
                role=Role.ASSISTANT,
                content="first response",
            ),
        )
        ingest_chat_event(
            self.database_path,
            chat_event(event_id="three", chat="chat-b", turn="turn-b", content="second chat"),
        )
        with database(self.database_path) as connection:
            tree = read_project_tree(connection, first.project_id)
        self.assertEqual(len(tree["chats"]), 2)
        self.assertEqual([message["role"] for message in tree["chats"][0]["messages"]], ["user", "assistant"])

    def test_chat_belongs_to_exactly_one_project_and_foreign_keys_are_enforced(self) -> None:
        receipt = ingest_chat_event(self.database_path, chat_event(event_id="one"))
        with database(self.database_path) as connection:
            row = connection.execute(
                "SELECT project_id FROM chats WHERE chat_id = ?", (receipt.chat_id,)
            ).fetchone()
            self.assertEqual(row["project_id"], receipt.project_id)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO chats(chat_id, project_id, host, host_chat_reference, display_title, created_at, updated_at)
                    VALUES ('bad', 'missing', 'codex', 'bad', 'bad', 'now', 'now')
                    """
                )

    def test_duplicate_observation_is_idempotent(self) -> None:
        first = ingest_chat_event(
            self.database_path,
            chat_event(event_id="event-a", turn="turn-1", content="repeat observation"),
        )
        duplicate = ingest_chat_event(
            self.database_path,
            chat_event(event_id="event-b", turn="turn-1", content="repeat observation"),
        )
        self.assertTrue(first.inserted)
        self.assertFalse(duplicate.inserted)
        self.assertEqual(first.message_id, duplicate.message_id)

    def test_identical_text_in_distinct_turns_is_not_globally_deduplicated(self) -> None:
        first = ingest_chat_event(
            self.database_path, chat_event(event_id="a", turn="turn-a", content="same text")
        )
        second = ingest_chat_event(
            self.database_path, chat_event(event_id="b", turn="turn-b", content="same text")
        )
        self.assertTrue(first.inserted)
        self.assertTrue(second.inserted)
        self.assertEqual(second.sequence_number, 2)

    def test_unknown_project_becomes_unassigned(self) -> None:
        receipt = ingest_chat_event(
            self.database_path,
            chat_event(event_id="unknown", project=None, project_name=None),
        )
        with database(self.database_path) as connection:
            row = connection.execute(
                "SELECT display_name FROM projects WHERE project_id = ?", (receipt.project_id,)
            ).fetchone()
        self.assertEqual(row["display_name"], "Unassigned")

    def test_project_rebinding_does_not_rewrite_message_content(self) -> None:
        first = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="before",
                chat="rebound-chat",
                turn="before",
                content="immutable historical content",
                project=None,
                project_name=None,
            ),
        )
        second = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="after",
                chat="rebound-chat",
                turn="after",
                content="new content",
                project="explicit-project",
                project_name="Explicit Project",
            ),
        )
        self.assertNotEqual(first.project_id, second.project_id)
        with database(self.database_path) as connection:
            tree = read_project_tree(connection, second.project_id)
            indexed_project = connection.execute(
                "SELECT project_id FROM lexical_entries WHERE source_id = ?", (first.message_id,)
            ).fetchone()["project_id"]
        self.assertEqual(tree["chats"][0]["messages"][0]["content"], "immutable historical content")
        self.assertEqual(indexed_project, second.project_id)

    def test_existing_explicit_binding_is_kept_when_later_event_has_no_project_signal(self) -> None:
        explicit = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="explicit",
                chat="bound-chat",
                project="explicit-project",
                project_name="Explicit Project",
            ),
        )
        later = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="missing-signal",
                chat="bound-chat",
                project=None,
                project_name=None,
            ),
        )
        self.assertEqual(later.project_id, explicit.project_id)

    def test_project_and_chat_renames_preserve_stable_local_identities(self) -> None:
        project_reference = "g-p-00000000000000001111111111111111"
        chat_reference = "6a771727-f250-83ea-ae5b-0e14abe188c6"
        first_event = replace(
            chat_event(
                event_id="project-title-before",
                chat=chat_reference,
                project=project_reference,
                project_name="Project Before",
                host=Host.CHATGPT_WEB,
            ),
            chat_display_name="Chat Before",
        )
        second_event = replace(
            chat_event(
                event_id="project-title-after",
                chat=chat_reference,
                project=project_reference,
                project_name="Project After",
                host=Host.CHATGPT_WEB,
            ),
            chat_display_name="Chat After",
        )

        first = ingest_chat_event(self.database_path, first_event)
        second = ingest_chat_event(self.database_path, second_event)

        with database(self.database_path) as connection:
            project = connection.execute(
                "SELECT display_name FROM projects WHERE project_id = ?",
                (second.project_id,),
            ).fetchone()
            chat = connection.execute(
                "SELECT display_title FROM chats WHERE chat_id = ?",
                (second.chat_id,),
            ).fetchone()

        self.assertEqual(second.project_id, first.project_id)
        self.assertEqual(second.chat_id, first.chat_id)
        self.assertEqual(project["display_name"], "Project After")
        self.assertEqual(chat["display_title"], "Chat After")

    def test_later_chat_title_updates_only_the_matching_stable_conversation(self) -> None:
        project_reference = "g-p-title-source"
        first = ingest_chat_event(
            self.database_path,
            replace(
                chat_event(
                    event_id="title-a-before",
                    chat="stable-chat-a",
                    project=project_reference,
                    host=Host.CHATGPT_WEB,
                ),
                chat_display_name=None,
            ),
        )
        second = ingest_chat_event(
            self.database_path,
            replace(
                chat_event(
                    event_id="title-b-before",
                    chat="stable-chat-b",
                    project=project_reference,
                    host=Host.CHATGPT_WEB,
                ),
                chat_display_name="Conversation B",
            ),
        )
        updated = ingest_chat_event(
            self.database_path,
            replace(
                chat_event(
                    event_id="title-a-after",
                    chat="stable-chat-a",
                    project=project_reference,
                    host=Host.CHATGPT_WEB,
                ),
                chat_display_name="Conversation A",
            ),
        )

        with database(self.database_path) as connection:
            rows = connection.execute(
                "SELECT chat_id, host_chat_reference, display_title FROM chats "
                "WHERE project_id = ? ORDER BY host_chat_reference",
                (first.project_id,),
            ).fetchall()

        self.assertEqual(updated.chat_id, first.chat_id)
        self.assertNotEqual(first.chat_id, second.chat_id)
        self.assertEqual(
            [
                (row["host_chat_reference"], row["display_title"])
                for row in rows
            ],
            [
                ("stable-chat-a", "Conversation A"),
                ("stable-chat-b", "Conversation B"),
            ],
        )

    def test_trustworthy_project_name_updates_preserve_two_existing_chat_bindings(self) -> None:
        project_reference = "g-p-00000000000000001111111111111111"
        first = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="project-chat-a",
                chat="stable-project-chat-a",
                turn="project-chat-a-turn",
                project=project_reference,
                project_name=None,
                host=Host.CHATGPT_WEB,
            ),
        )
        second = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="project-chat-b",
                chat="stable-project-chat-b",
                turn="project-chat-b-turn",
                project=project_reference,
                project_name=None,
                host=Host.CHATGPT_WEB,
            ),
        )
        named = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="project-name-proven",
                chat="stable-project-chat-a",
                turn="project-name-proven-turn",
                content="name-bearing event",
                project=project_reference,
                project_name="ATOMIZER PROJECT TEST",
                host=Host.CHATGPT_WEB,
            ),
        )
        renamed = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="project-name-renamed",
                chat="stable-project-chat-b",
                turn="project-name-renamed-turn",
                content="rename-bearing event",
                project=project_reference,
                project_name="ATOMIZER PROJECT TEST RENAMED",
                host=Host.CHATGPT_WEB,
            ),
        )
        ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="project-name-missing-later",
                chat="stable-project-chat-a",
                turn="project-name-missing-later-turn",
                role=Role.ASSISTANT,
                content="later event without a project name",
                project=project_reference,
                project_name=None,
                host=Host.CHATGPT_WEB,
            ),
        )

        with database(self.database_path) as connection:
            projects = connection.execute(
                """
                SELECT project_id, host_project_reference, display_name
                FROM projects
                WHERE host = ? AND host_project_reference = ?
                """,
                (Host.CHATGPT_WEB.value, project_reference),
            ).fetchall()
            chats = connection.execute(
                "SELECT chat_id, project_id FROM chats WHERE project_id = ? ORDER BY chat_id",
                (first.project_id,),
            ).fetchall()

        self.assertEqual(first.project_id, second.project_id)
        self.assertEqual(named.project_id, first.project_id)
        self.assertEqual(renamed.project_id, first.project_id)
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["project_id"], first.project_id)
        self.assertEqual(projects[0]["host_project_reference"], project_reference)
        self.assertEqual(projects[0]["display_name"], "ATOMIZER PROJECT TEST RENAMED")
        self.assertEqual(
            {row["chat_id"] for row in chats},
            {first.chat_id, second.chat_id},
        )
        self.assertEqual({row["project_id"] for row in chats}, {first.project_id})

    def test_different_canonical_project_references_remain_distinct(self) -> None:
        first = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="project-a",
                chat="chat-project-a",
                project="g-p-00000000000000002222222222222222",
                project_name="Project A",
                host=Host.CHATGPT_WEB,
            ),
        )
        second = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="project-b",
                chat="chat-project-b",
                project="g-p-00000000000000003333333333333333",
                project_name="Project B",
                host=Host.CHATGPT_WEB,
            ),
        )
        self.assertNotEqual(second.project_id, first.project_id)
        self.assertNotEqual(second.chat_id, first.chat_id)

    def test_persistence_survives_connection_restart_and_order_is_deterministic(self) -> None:
        for index in range(3):
            ingest_chat_event(
                self.database_path,
                chat_event(event_id=f"event-{index}", turn=f"turn-{index}", content=f"message {index}"),
            )
        with database(self.database_path) as connection:
            values = connection.execute(
                "SELECT sequence_number, content FROM messages ORDER BY sequence_number"
            ).fetchall()
        self.assertEqual([(row["sequence_number"], row["content"]) for row in values], [(1, "message 0"), (2, "message 1"), (3, "message 2")])

    def test_fresh_database_applies_all_document_library_migrations(self) -> None:
        with database(self.database_path) as connection:
            versions = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        self.assertEqual(
            tuple(row["version"] for row in versions),
            registered_migration_ids(),
        )

    def test_retired_forward_schema_is_tolerated_without_data_loss(self) -> None:
        receipt = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="retired-schema-message",
                chat="retired-schema-chat",
                content="Retired forward schema preservation proof.",
            ),
        )
        document = self.root / "retired-schema.md"
        document.write_text(
            "Synthetic elected document remains authorized.", encoding="utf-8"
        )
        elect_file_source(self.database_path, receipt.project_id, document)
        run_derived_state_cycle(self.database_path)

        authoritative_tables = (
            "schema_migrations",
            "projects",
            "chats",
            "messages",
            "documents",
            "elected_sources",
            "document_source_memberships",
            "document_revision_history",
        )

        def authoritative_snapshot(
            connection: sqlite3.Connection,
        ) -> dict[str, tuple[tuple[object, ...], ...]]:
            return {
                table: tuple(
                    tuple(row)
                    for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY 1')
                )
                for table in authoritative_tables
            }

        with database(self.database_path) as connection:
            connection.execute(
                "CREATE TABLE retired_extension_state (item_id TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO retired_extension_state(item_id, value) VALUES (?, ?)",
                ("synthetic-item", "preserve-me"),
            )
            connection.executemany(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (
                    ("008_retired_local_extension", "2026-01-01T00:00:00.000Z"),
                    ("009_retired_local_extension_aux", "2026-01-01T00:00:01.000Z"),
                ),
            )
            before = authoritative_snapshot(connection)

        with database(self.database_path) as connection:
            after = authoritative_snapshot(connection)
            retired = connection.execute(
                "SELECT item_id, value FROM retired_extension_state"
            ).fetchone()
            versions = tuple(
                row["version"]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            )
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()

        self.assertEqual(after, before)
        self.assertEqual(tuple(retired), ("synthetic-item", "preserve-me"))
        self.assertEqual(versions[:7], registered_migration_ids())
        self.assertEqual(
            versions[7:],
            ("008_retired_local_extension", "009_retired_local_extension_aux"),
        )
        self.assertEqual(integrity, "ok")
        self.assertEqual(foreign_keys, [])

        cycle = run_derived_state_cycle(self.database_path)
        self.assertGreater(cycle.semantic_unit_count, 0)
        self.assertGreater(cycle.verification_count, 0)

    def test_migration_registry_accepts_ordered_forward_addition_without_ceiling(self) -> None:
        migration_directory = self.root / "migration-registry"
        migration_directory.mkdir()
        names = (
            "001_initial",
            "002_document_sources",
            "003_document_supersession",
            "004_future_forward_migration",
        )
        for name in names:
            (migration_directory / f"{name}.sql").write_text("SELECT 1;", encoding="utf-8")
        self.assertEqual(registered_migration_ids(migration_directory), names)

        invalid_directory = self.root / "invalid-migration-registry"
        invalid_directory.mkdir()
        for name in ("001_initial", "003_gap"):
            (invalid_directory / f"{name}.sql").write_text("SELECT 1;", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "contiguous"):
            registered_migration_ids(invalid_directory)


if __name__ == "__main__":
    import unittest

    unittest.main()
