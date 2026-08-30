"""Maintenance of generic chat/document rows consumed by FTS5."""

from __future__ import annotations

import sqlite3

from atomizer_local_client.chat.contracts import ChatEvent, CorpusType


def index_message(
    connection: sqlite3.Connection,
    *,
    message_id: str,
    project_id: str,
    chat_id: str,
    event: ChatEvent,
) -> None:
    connection.execute(
        """
        INSERT INTO lexical_entries(
            lexical_entry_id, corpus_type, source_id, project_id, chat_id, role, content, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(corpus_type, source_id) DO UPDATE SET
            project_id = excluded.project_id,
            chat_id = excluded.chat_id,
            role = excluded.role,
            content = excluded.content,
            updated_at = excluded.updated_at
        """,
        (
            f"message:{message_id}",
            CorpusType.CHAT_HISTORY.value,
            message_id,
            project_id,
            chat_id,
            event.role.value,
            event.content,
            event.captured_at,
        ),
    )


def index_document(
    connection: sqlite3.Connection,
    *,
    document_id: str,
    project_id: str,
    content: str,
    updated_at: str,
) -> None:
    connection.execute(
        """
        INSERT INTO lexical_entries(
            lexical_entry_id, corpus_type, source_id, project_id, chat_id, role, content, updated_at
        ) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?)
        ON CONFLICT(corpus_type, source_id) DO UPDATE SET
            project_id = excluded.project_id,
            content = excluded.content,
            updated_at = excluded.updated_at
        """,
        (
            f"document:{document_id}",
            CorpusType.ELECTED_DOCUMENT.value,
            document_id,
            project_id,
            content,
            updated_at,
        ),
    )


def remove_document_index(connection: sqlite3.Connection, document_id: str) -> None:
    connection.execute(
        "DELETE FROM lexical_entries WHERE corpus_type = ? AND source_id = ?",
        (CorpusType.ELECTED_DOCUMENT.value, document_id),
    )


def rebind_chat_index(connection: sqlite3.Connection, chat_id: str, project_id: str) -> None:
    connection.execute(
        "UPDATE lexical_entries SET project_id = ? WHERE chat_id = ?",
        (project_id, chat_id),
    )
