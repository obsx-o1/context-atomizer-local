from __future__ import annotations

from dataclasses import replace

from test_support import TemporaryDatabaseTest, chat_event

from atomizer_local_client.chat.contracts import Host
from atomizer_local_client.chat.ingestion import ingest_chat_event
from atomizer_local_client.chats.title_reconciliation import (
    reconcile_existing_chat_titles,
)
from atomizer_local_client.history.connection import database


PROJECT = "g-p-00000000000000001111111111111111"
OTHER_PROJECT = "g-p-00000000000000005555555555555555"
CHAT_A = "6a794dcb-ff9c-83ea-8177-7aef25bd782d"
CHAT_B = "6a794dd3-a1ac-83ea-b651-545772a9f204"


def observation(
    chat_reference: str,
    *,
    project_reference: str | None = None,
    visible_title: str | None = None,
    aria_label: str | None = None,
) -> dict[str, object]:
    return {
        "host_chat_reference": chat_reference,
        "host_project_reference": project_reference,
        "visible_title": visible_title,
        "aria_label": aria_label,
    }


class ChatTitleReconciliationTests(TemporaryDatabaseTest):
    def create_chat(
        self,
        chat_reference: str = CHAT_A,
        *,
        project_reference: str | None = PROJECT,
        project_name: str | None = "Context Atomizer",
        title: str = "ChatGPT - Context Atomizer",
    ):
        return ingest_chat_event(
            self.database_path,
            replace(
                chat_event(
                    event_id=f"event-{chat_reference}",
                    chat=chat_reference,
                    project=project_reference,
                    project_name=project_name,
                    host=Host.CHATGPT_WEB,
                ),
                chat_display_name=title,
            ),
        )

    def stored_title(self, chat_id: str) -> str:
        with database(self.database_path) as connection:
            return str(
                connection.execute(
                    "SELECT display_title FROM chats WHERE chat_id = ?", (chat_id,)
                ).fetchone()["display_title"]
            )

    def test_clean_visible_title_has_priority_for_ordinary_and_project_chats(self) -> None:
        first = self.create_chat()
        second = self.create_chat(CHAT_B, title="ChatGPT - Context Atomizer")

        result = reconcile_existing_chat_titles(
            self.database_path,
            [
                observation(
                    CHAT_A,
                    visible_title="Review GitHub Architecture",
                    aria_label="Wrong lower-priority label",
                ),
                observation(
                    CHAT_B,
                    project_reference=PROJECT,
                    visible_title="Atomizer v1 Optimization",
                    aria_label=(
                        "Atomizer v1 Optimization, chat in project Context Atomizer"
                    ),
                ),
            ],
        )

        self.assertEqual((result.matched, result.updated, result.rejected), (2, 2, 0))
        self.assertEqual(self.stored_title(first.chat_id), "Review GitHub Architecture")
        self.assertEqual(self.stored_title(second.chat_id), "Atomizer v1 Optimization")

    def test_ordinary_aria_title_persists_when_visible_text_is_unavailable(self) -> None:
        receipt = self.create_chat(project_reference=None, project_name=None)

        result = reconcile_existing_chat_titles(
            self.database_path,
            [observation(CHAT_A, aria_label="Review GitHub Architecture")],
        )

        self.assertEqual((result.matched, result.updated, result.rejected), (1, 1, 0))
        self.assertEqual(self.stored_title(receipt.chat_id), "Review GitHub Architecture")

    def test_proven_project_aria_suffix_is_removed_without_splitting_title_commas(self) -> None:
        receipt = self.create_chat()

        result = reconcile_existing_chat_titles(
            self.database_path,
            [
                observation(
                    CHAT_A,
                    project_reference=PROJECT,
                    aria_label=(
                        "Plans, Reviews, and Follow-up, chat in project Context Atomizer"
                    ),
                )
            ],
        )

        self.assertEqual(result.updated, 1)
        self.assertEqual(
            self.stored_title(receipt.chat_id), "Plans, Reviews, and Follow-up"
        )

    def test_wrong_or_untrusted_project_suffix_is_rejected(self) -> None:
        trusted = self.create_chat()
        opaque = self.create_chat(
            CHAT_B,
            project_reference=OTHER_PROJECT,
            project_name=OTHER_PROJECT,
        )

        result = reconcile_existing_chat_titles(
            self.database_path,
            [
                observation(
                    CHAT_A,
                    project_reference=PROJECT,
                    aria_label="Title A, chat in project Wrong Project",
                ),
                observation(
                    CHAT_B,
                    project_reference=OTHER_PROJECT,
                    aria_label="Title B, chat in project Context Atomizer",
                ),
            ],
        )

        self.assertEqual((result.updated, result.rejected), (0, 2))
        self.assertEqual(self.stored_title(trusted.chat_id), "ChatGPT - Context Atomizer")
        self.assertEqual(self.stored_title(opaque.chat_id), "ChatGPT - Context Atomizer")

    def test_unknown_chat_does_not_create_and_conflicting_titles_are_ambiguous(self) -> None:
        receipt = self.create_chat()

        result = reconcile_existing_chat_titles(
            self.database_path,
            [
                observation(CHAT_A, visible_title="Title One"),
                observation(CHAT_A, visible_title="Title Two"),
                observation(
                    "11111111-1111-1111-1111-111111111111",
                    visible_title="Unknown Sidebar Chat",
                ),
            ],
        )

        with database(self.database_path) as connection:
            chat_count = connection.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
        self.assertEqual((result.observed, result.matched, result.updated, result.rejected), (3, 1, 0, 1))
        self.assertEqual(chat_count, 1)
        self.assertEqual(self.stored_title(receipt.chat_id), "ChatGPT - Context Atomizer")

    def test_later_title_and_rename_update_same_chat_without_touching_history(self) -> None:
        receipt = self.create_chat()
        with database(self.database_path) as connection:
            before = [
                tuple(row)
                for row in connection.execute(
                    "SELECT message_id, chat_id, sequence_number, role, content FROM messages"
                ).fetchall()
            ]
            project_before = tuple(
                connection.execute(
                    "SELECT project_id, host_project_reference, display_name FROM projects "
                    "WHERE project_id = ?",
                    (receipt.project_id,),
                ).fetchone()
            )

        first = reconcile_existing_chat_titles(
            self.database_path,
            [observation(CHAT_A, visible_title="Initial Host Title")],
        )
        renamed = reconcile_existing_chat_titles(
            self.database_path,
            [observation(CHAT_A, visible_title="Renamed Host Title")],
        )

        with database(self.database_path) as connection:
            chat = connection.execute(
                "SELECT chat_id, project_id, host_chat_reference, display_title FROM chats"
            ).fetchone()
            after = [
                tuple(row)
                for row in connection.execute(
                    "SELECT message_id, chat_id, sequence_number, role, content FROM messages"
                ).fetchall()
            ]
            project_after = tuple(
                connection.execute(
                    "SELECT project_id, host_project_reference, display_name FROM projects "
                    "WHERE project_id = ?",
                    (receipt.project_id,),
                ).fetchone()
            )
            lexical_count = connection.execute(
                "SELECT COUNT(*) FROM lexical_entries"
            ).fetchone()[0]

        self.assertEqual(first.updated, 1)
        self.assertEqual(renamed.updated, 1)
        self.assertEqual(chat["chat_id"], receipt.chat_id)
        self.assertEqual(chat["project_id"], receipt.project_id)
        self.assertEqual(chat["host_chat_reference"], CHAT_A)
        self.assertEqual(chat["display_title"], "Renamed Host Title")
        self.assertEqual(before, after)
        self.assertEqual(project_before, project_after)
        self.assertEqual(lexical_count, 1)

    def test_project_reference_mismatch_cannot_cross_bind_title(self) -> None:
        first = self.create_chat()
        second = self.create_chat(
            CHAT_B,
            project_reference=OTHER_PROJECT,
            project_name="Other Project",
            title="Other Existing Title",
        )

        result = reconcile_existing_chat_titles(
            self.database_path,
            [
                observation(
                    CHAT_A,
                    project_reference=OTHER_PROJECT,
                    visible_title="Crossed Title",
                )
            ],
        )

        self.assertEqual((result.updated, result.rejected), (0, 1))
        self.assertEqual(self.stored_title(first.chat_id), "ChatGPT - Context Atomizer")
        self.assertEqual(self.stored_title(second.chat_id), "Other Existing Title")


if __name__ == "__main__":
    import unittest

    unittest.main()
