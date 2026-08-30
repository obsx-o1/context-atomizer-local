"""Persistence and rebinding operations for host chats."""

from __future__ import annotations

import sqlite3
import uuid

from atomizer_local_client.chat.contracts import ChatEvent, utc_now

_CHAT_NAMESPACE = uuid.UUID("833e951b-1c0a-412a-8f0e-e17de9240fc4")


def get_or_create_chat(connection: sqlite3.Connection, project_id: str, event: ChatEvent) -> str:
    if event.rebind_from_host_chat_reference is not None:
        return _rebind_provisional_chat(connection, project_id, event)
    existing = connection.execute(
        "SELECT chat_id FROM chats WHERE host = ? AND host_chat_reference = ?",
        (event.host.value, event.host_chat_reference),
    ).fetchone()
    now = utc_now()
    title = event.chat_display_name or f"{event.host.value} chat"
    if existing:
        if event.chat_display_name:
            connection.execute(
                "UPDATE chats SET project_id = ?, display_title = ?, updated_at = ? WHERE chat_id = ?",
                (project_id, event.chat_display_name, now, existing["chat_id"]),
            )
        else:
            connection.execute(
                "UPDATE chats SET project_id = ?, updated_at = ? WHERE chat_id = ?",
                (project_id, now, existing["chat_id"]),
            )
        return str(existing["chat_id"])
    chat_id = str(
        uuid.uuid5(_CHAT_NAMESPACE, f"{event.host.value}\x1f{event.host_chat_reference}")
    )
    connection.execute(
        """
        INSERT INTO chats(chat_id, project_id, host, host_chat_reference, display_title, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            chat_id,
            project_id,
            event.host.value,
            event.host_chat_reference,
            title,
            now,
            now,
        ),
    )
    return chat_id


def _rebind_provisional_chat(
    connection: sqlite3.Connection, project_id: str, event: ChatEvent
) -> str:
    source_reference = event.rebind_from_host_chat_reference
    assert source_reference is not None
    source = connection.execute(
        "SELECT chat_id FROM chats WHERE host = ? AND host_chat_reference = ?",
        (event.host.value, source_reference),
    ).fetchone()
    target = connection.execute(
        "SELECT chat_id FROM chats WHERE host = ? AND host_chat_reference = ?",
        (event.host.value, event.host_chat_reference),
    ).fetchone()

    if source and target:
        raise ValueError("stable chat target already belongs to another local chat")
    chat_id = str(source["chat_id"] if source else target["chat_id"]) if (source or target) else None
    if chat_id is None:
        raise ValueError("provisional chat does not exist")

    exact_submission = connection.execute(
        """
        SELECT 1 FROM messages
        WHERE chat_id = ? AND host_turn_reference IS ? AND role = ? AND content = ?
        """,
        (chat_id, event.host_turn_reference, event.role.value, event.content),
    ).fetchone()
    if not exact_submission:
        raise ValueError("provisional chat does not contain the exact submitted turn")

    title = event.chat_display_name
    now = utc_now()
    if source:
        if title:
            connection.execute(
                """
                UPDATE chats
                SET project_id = ?, host_chat_reference = ?, display_title = ?, updated_at = ?
                WHERE chat_id = ?
                """,
                (project_id, event.host_chat_reference, title, now, chat_id),
            )
        else:
            connection.execute(
                """
                UPDATE chats SET project_id = ?, host_chat_reference = ?, updated_at = ?
                WHERE chat_id = ?
                """,
                (project_id, event.host_chat_reference, now, chat_id),
            )
    elif title:
        connection.execute(
            "UPDATE chats SET project_id = ?, display_title = ?, updated_at = ? WHERE chat_id = ?",
            (project_id, title, now, chat_id),
        )
    return chat_id


def rebind_chat(connection: sqlite3.Connection, chat_id: str, project_id: str) -> None:
    cursor = connection.execute(
        "UPDATE chats SET project_id = ?, updated_at = ? WHERE chat_id = ?",
        (project_id, utc_now(), chat_id),
    )
    if cursor.rowcount != 1:
        raise KeyError(chat_id)
