"""Deterministic read projections for Project to Chat to Messages."""

from __future__ import annotations

import sqlite3
from typing import Any


def read_chat_messages(connection: sqlite3.Connection, chat_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT message_id, host_turn_reference, sequence_number, role, content, captured_at
        FROM messages WHERE chat_id = ? ORDER BY sequence_number, message_id
        """,
        (chat_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def read_project_tree(connection: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    project = connection.execute(
        "SELECT project_id, host, host_project_reference, display_name FROM projects WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    if project is None:
        raise KeyError(project_id)
    chats = connection.execute(
        """
        SELECT chat_id, host, host_chat_reference, display_title
        FROM chats WHERE project_id = ? ORDER BY created_at, chat_id
        """,
        (project_id,),
    ).fetchall()
    result = dict(project)
    result["chats"] = []
    for chat in chats:
        chat_value = dict(chat)
        chat_value["messages"] = read_chat_messages(connection, str(chat["chat_id"]))
        result["chats"].append(chat_value)
    return result

