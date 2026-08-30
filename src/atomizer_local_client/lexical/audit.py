"""Read-only consistency audit for the rebuildable FTS5 lexical projection."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any


_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def audit_lexical_consistency(database_path: Path) -> dict[str, Any]:
    """Audit relational identities and FTS postings without mutating the main DB."""
    uri = Path(database_path).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE temp.lexical_entries_vocab "
            "USING fts5vocab(main, lexical_entries_fts, 'instance')"
        )
        lexical_rows = connection.execute(
            "SELECT rowid, content FROM lexical_entries ORDER BY rowid"
        ).fetchall()
        lexical_rowids = {int(row["rowid"]) for row in lexical_rows}
        tokenizable_rowids = {
            int(row["rowid"])
            for row in lexical_rows
            if _TOKEN.search(str(row["content"])) is not None
        }
        fts_rowids = {
            int(row[0])
            for row in connection.execute(
                "SELECT DISTINCT doc FROM temp.lexical_entries_vocab ORDER BY doc"
            ).fetchall()
        }

        def count(query: str) -> int:
            return int(connection.execute(query).fetchone()[0])

        checks = {
            "lexical_without_fts": len(tokenizable_rowids - fts_rowids),
            "fts_without_lexical": len(fts_rowids - lexical_rowids),
            "lexical_without_project": count(
                "SELECT COUNT(*) FROM lexical_entries l LEFT JOIN projects p "
                "ON p.project_id=l.project_id WHERE p.project_id IS NULL"
            ),
            "chat_lexical_without_message": count(
                "SELECT COUNT(*) FROM lexical_entries l LEFT JOIN messages m "
                "ON m.message_id=l.source_id WHERE l.corpus_type='CHAT_HISTORY' "
                "AND m.message_id IS NULL"
            ),
            "message_without_chat_lexical": count(
                "SELECT COUNT(*) FROM messages m LEFT JOIN lexical_entries l "
                "ON l.corpus_type='CHAT_HISTORY' AND l.source_id=m.message_id "
                "WHERE l.lexical_entry_id IS NULL"
            ),
            "chat_projection_mismatch": count(
                "SELECT COUNT(*) FROM lexical_entries l "
                "JOIN messages m ON m.message_id=l.source_id "
                "JOIN chats c ON c.chat_id=m.chat_id "
                "WHERE l.corpus_type='CHAT_HISTORY' AND "
                "(l.chat_id<>m.chat_id OR l.project_id<>c.project_id OR "
                "l.role<>m.role OR l.content<>m.content)"
            ),
            "document_lexical_without_document": count(
                "SELECT COUNT(*) FROM lexical_entries l LEFT JOIN documents d "
                "ON d.document_id=l.source_id WHERE l.corpus_type='ELECTED_DOCUMENT' "
                "AND d.document_id IS NULL"
            ),
            "document_without_document_lexical": count(
                "SELECT COUNT(*) FROM documents d LEFT JOIN lexical_entries l "
                "ON l.corpus_type='ELECTED_DOCUMENT' AND l.source_id=d.document_id "
                "WHERE l.lexical_entry_id IS NULL"
            ),
            "document_projection_mismatch": count(
                "SELECT COUNT(*) FROM lexical_entries l "
                "JOIN documents d ON d.document_id=l.source_id "
                "WHERE l.corpus_type='ELECTED_DOCUMENT' AND "
                "(l.project_id<>d.project_id OR l.chat_id IS NOT NULL OR "
                "l.role IS NOT NULL OR l.content<>d.text_content)"
            ),
            "duplicate_active_source_identities": count(
                "SELECT COUNT(*) FROM (SELECT corpus_type, source_id "
                "FROM lexical_entries GROUP BY corpus_type, source_id HAVING COUNT(*)>1)"
            ),
        }
        return {
            "passed": all(value == 0 for value in checks.values()),
            "lexical_entry_count": len(lexical_rowids),
            "tokenizable_lexical_entry_count": len(tokenizable_rowids),
            "fts_document_count": len(fts_rowids),
            "checks": checks,
        }
    finally:
        connection.close()
