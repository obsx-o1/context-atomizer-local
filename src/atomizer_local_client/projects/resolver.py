"""Deterministic host identity to local project resolution."""

from __future__ import annotations

import sqlite3

from atomizer_local_client.chat.contracts import ChatEvent
from atomizer_local_client.projects.repository import get_or_create_project


def resolve_project(connection: sqlite3.Connection, event: ChatEvent) -> str:
    if event.host_project_reference:
        return get_or_create_project(
            connection,
            host=event.host.value,
            host_project_reference=event.host_project_reference,
            display_name=event.project_display_name,
        )
    existing_binding = connection.execute(
        "SELECT project_id FROM chats WHERE host = ? AND host_chat_reference = ?",
        (event.host.value, event.host_chat_reference),
    ).fetchone()
    if existing_binding is not None:
        return str(existing_binding["project_id"])
    return get_or_create_project(
        connection,
        host="local",
        host_project_reference="unassigned",
        display_name="Unassigned",
    )
