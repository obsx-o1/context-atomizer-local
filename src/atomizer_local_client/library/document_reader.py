"""Readback of elected local source and document state."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from atomizer_local_client.history.connection import database


def read_document(database_path: Path, document_id: str) -> dict[str, Any]:
    with database(database_path) as connection:
        row = connection.execute(
            """
            SELECT document_id, project_id, display_name, document_type,
                   local_source_reference, text_content, updated_at,
                   content_sha256, file_size, modified_time_ns, file_identity,
                   previous_content_sha256, superseded_at, revision
            FROM documents WHERE document_id = ?
            """,
            (document_id,),
        ).fetchone()
    if row is None:
        raise KeyError(document_id)
    return dict(row)


def list_documents(database_path: Path, project_id: str | None = None) -> list[dict[str, Any]]:
    with database(database_path) as connection:
        if project_id is None:
            rows = connection.execute(
                """
                SELECT document_id, project_id, display_name, document_type,
                       local_source_reference, updated_at, content_sha256,
                       file_size, modified_time_ns, file_identity,
                       previous_content_sha256, superseded_at, revision
                FROM documents ORDER BY project_id, display_name, document_id
                """
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT document_id, project_id, display_name, document_type,
                       local_source_reference, updated_at, content_sha256,
                       file_size, modified_time_ns, file_identity,
                       previous_content_sha256, superseded_at, revision
                FROM documents WHERE project_id = ?
                ORDER BY display_name, document_id
                """,
                (project_id,),
            ).fetchall()
    return [dict(row) for row in rows]


def list_elected_sources(
    database_path: Path, project_id: str | None = None
) -> list[dict[str, Any]]:
    with database(database_path) as connection:
        if project_id is None:
            rows = connection.execute(
                "SELECT * FROM elected_sources ORDER BY project_id, display_name, source_id"
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM elected_sources WHERE project_id = ? "
                "ORDER BY display_name, source_id",
                (project_id,),
            ).fetchall()
    return [dict(row) for row in rows]
