"""Deterministic bounded semantic-unit construction and reconciliation."""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from dataclasses import dataclass

from atomizer_local_client.semantic.contracts import SemanticUnit

CHUNKER_VERSION = "bounded-paragraph-v1"
MAX_UNIT_CHARACTERS = 1200
_UNIT_NAMESPACE = uuid.UUID("56e77440-d0cf-4c11-a0de-b038b6fcce8d")


@dataclass(frozen=True, slots=True)
class _Source:
    source_type: str
    source_id: str
    project_id: str
    chat_id: str | None
    revision: int
    content: str
    updated_at: str


def _unit_id(source: _Source, unit_index: int, content_hash: str) -> str:
    material = "\x1f".join(
        (source.source_type, source.source_id, str(source.revision), str(unit_index), CHUNKER_VERSION, content_hash)
    )
    return str(uuid.uuid5(_UNIT_NAMESPACE, material))


def _document_chunks(content: str) -> list[tuple[int, int, str]]:
    if not content:
        return []
    chunks: list[tuple[int, int, str]] = []
    cursor = 0
    length = len(content)
    while cursor < length:
        target = min(length, cursor + MAX_UNIT_CHARACTERS)
        end = target
        if target < length:
            paragraph = content.rfind("\n\n", cursor + 1, target + 1)
            newline = content.rfind("\n", cursor + 1, target + 1)
            space = content.rfind(" ", cursor + 1, target + 1)
            end = max(paragraph + 2 if paragraph >= cursor else 0, newline + 1, space + 1)
            if end <= cursor:
                end = target
        value = content[cursor:end]
        if value:
            chunks.append((cursor, end, value))
        cursor = end
    return chunks


def build_units(source: _Source) -> tuple[SemanticUnit, ...]:
    chunks = (
        [(0, len(source.content), source.content)]
        if source.source_type == "chat_message"
        else _document_chunks(source.content)
    )
    result: list[SemanticUnit] = []
    for index, (start, end, content) in enumerate(chunks):
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        result.append(
            SemanticUnit(
                semantic_unit_id=_unit_id(source, index, digest),
                source_type=source.source_type,
                source_id=source.source_id,
                project_id=source.project_id,
                chat_id=source.chat_id,
                source_revision=source.revision,
                unit_index=index,
                start_offset=start,
                end_offset=end,
                content=content,
                content_sha256=digest,
                source_updated_at=source.updated_at,
            )
        )
    return tuple(result)


def authoritative_sources(connection: sqlite3.Connection) -> tuple[_Source, ...]:
    rows = connection.execute(
        """
        SELECT 'chat_message', m.message_id, c.project_id, m.chat_id, 1, m.content, m.captured_at
        FROM messages m JOIN chats c ON c.chat_id=m.chat_id
        UNION ALL
        SELECT 'elected_document', d.document_id, d.project_id, NULL, d.revision,
               d.text_content, d.updated_at FROM documents d
        UNION ALL
        SELECT 'elected_document', h.document_id, h.project_id, NULL, h.revision,
               h.text_content, h.observed_at FROM document_revision_history h
        ORDER BY 1, 2
        """
    ).fetchall()
    return tuple(
        _Source(
            source_type=str(row[0]), source_id=str(row[1]), project_id=str(row[2]),
            chat_id=str(row[3]) if row[3] is not None else None, revision=int(row[4]),
            content=str(row[5]), updated_at=str(row[6]),
        )
        for row in rows
    )


def reconcile_semantic_units(connection: sqlite3.Connection) -> tuple[SemanticUnit, ...]:
    expected: list[SemanticUnit] = []
    for source in authoritative_sources(connection):
        expected.extend(build_units(source))
    expected_ids = {unit.semantic_unit_id for unit in expected}
    existing_ids = {str(row[0]) for row in connection.execute("SELECT semantic_unit_id FROM semantic_units")}
    for stale in sorted(existing_ids - expected_ids):
        connection.execute("DELETE FROM semantic_units WHERE semantic_unit_id = ?", (stale,))
    for unit in expected:
        connection.execute(
            """
            INSERT INTO semantic_units(
                semantic_unit_id, source_type, source_id, project_id, chat_id, source_revision,
                unit_index, start_offset, end_offset, content, content_sha256,
                chunker_version, source_updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(semantic_unit_id) DO UPDATE SET
                project_id=excluded.project_id, chat_id=excluded.chat_id,
                source_updated_at=excluded.source_updated_at
            """,
            (
                unit.semantic_unit_id, unit.source_type, unit.source_id, unit.project_id,
                unit.chat_id, unit.source_revision, unit.unit_index, unit.start_offset,
                unit.end_offset, unit.content, unit.content_sha256, CHUNKER_VERSION,
                unit.source_updated_at,
            ),
        )
    return tuple(expected)
