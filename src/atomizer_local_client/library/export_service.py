"""Human-requested read-only export of canonical captured Library material."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


_CAPTURE_TABLES = (
    "projects",
    "chats",
    "messages",
    "documents",
    "elected_sources",
    "document_source_memberships",
    "document_revision_history",
)


def export_captured_library(database_path: Path) -> bytes:
    path = Path(database_path).resolve()
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    try:
        tables: dict[str, list[dict[str, object]]] = {}
        for table in _CAPTURE_TABLES:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            tables[table] = (
                [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"')]
                if exists is not None
                else []
            )
    finally:
        connection.close()
    return (
        json.dumps(
            {
                "schema_version": "context-atomizer-library-export-v1",
                "exported_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "tables": tables,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


__all__ = ["export_captured_library"]
