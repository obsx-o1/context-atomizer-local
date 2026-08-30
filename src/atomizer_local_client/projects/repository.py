"""Persistence operations for local project records."""

from __future__ import annotations

import sqlite3
import uuid

from atomizer_local_client.chat.contracts import utc_now

_PROJECT_NAMESPACE = uuid.UUID("e72e4d69-c3d3-44fb-8557-8a60cde12f20")


def _project_id(host: str, host_reference: str) -> str:
    return str(uuid.uuid5(_PROJECT_NAMESPACE, f"{host}\x1f{host_reference}"))


def get_or_create_project(
    connection: sqlite3.Connection,
    *,
    host: str,
    host_project_reference: str,
    display_name: str | None,
) -> str:
    existing = connection.execute(
        "SELECT project_id FROM projects WHERE host = ? AND host_project_reference = ?",
        (host, host_project_reference),
    ).fetchone()
    now = utc_now()
    if existing and display_name:
        connection.execute(
            "UPDATE projects SET display_name = ?, updated_at = ? WHERE project_id = ?",
            (display_name, now, existing["project_id"]),
        )
        return str(existing["project_id"])
    if existing:
        return str(existing["project_id"])
    project_id = _project_id(host, host_project_reference)
    initial_display_name = display_name or host_project_reference
    connection.execute(
        """
        INSERT INTO projects(project_id, host, host_project_reference, display_name, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (project_id, host, host_project_reference, initial_display_name, now, now),
    )
    return project_id


def rename_project(connection: sqlite3.Connection, project_id: str, display_name: str) -> None:
    if not display_name.strip():
        raise ValueError("display_name must be non-empty")
    cursor = connection.execute(
        "UPDATE projects SET display_name = ?, updated_at = ? WHERE project_id = ?",
        (display_name.strip(), utc_now(), project_id),
    )
    if cursor.rowcount != 1:
        raise KeyError(project_id)
