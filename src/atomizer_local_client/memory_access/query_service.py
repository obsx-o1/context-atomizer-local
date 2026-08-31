"""Read-only service composed over the existing deterministic retrieval APIs."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from atomizer_local_client.memory_access.access_gate import (
    LibraryAccessGate,
    LibraryCaller,
)
from atomizer_local_client.memory_access.formatting import MAX_RESULTS, bounded_payload
from atomizer_local_client.retrieval.lexical_adapter import LexicalRetriever
from atomizer_local_client.retrieval.reranker import IdentityReranker
from atomizer_local_client.retrieval.rrf import RRFFuser
from atomizer_local_client.retrieval.vector import VectorRetriever
from atomizer_local_client.semantic.embeddings import LocalFeatureHashEmbeddingBackend


class LibraryQueryError(ValueError):
    pass


@contextmanager
def _read_only_database(database_path: Path) -> Iterator[sqlite3.Connection]:
    path = Path(database_path).resolve()
    if not path.is_file():
        raise LibraryQueryError("Library database is unavailable")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    try:
        yield connection
    finally:
        connection.close()


def _limit(value: int | None) -> int:
    if value is None:
        return MAX_RESULTS
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise LibraryQueryError("limit must be a positive integer")
    return min(value, MAX_RESULTS)


def _required_text(value: object, name: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LibraryQueryError(f"{name} must be non-empty text")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise LibraryQueryError(f"{name} exceeds {maximum} characters")
    return normalized


class LibraryQueryService:
    """Bounded projections over one existing Context Atomizer Local Library."""

    def __init__(self, database_path: Path, *, gate: LibraryAccessGate | None = None) -> None:
        self.database_path = Path(database_path)
        self.gate = gate or LibraryAccessGate()
        self._backend = LocalFeatureHashEmbeddingBackend()

    def _authorize(self, caller: LibraryCaller) -> dict[str, Any] | None:
        decision = self.gate.authorize(caller)
        if decision.allowed:
            return None
        return {
            "status": decision.status,
            "message": decision.message,
            "items": [],
            "result_count": 0,
            "truncated": False,
        }

    def _validate_project(self, connection: sqlite3.Connection, project: str | None) -> str | None:
        if project is None:
            return None
        project_id = _required_text(project, "project", maximum=256)
        row = connection.execute(
            "SELECT 1 FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise LibraryQueryError("unknown Library project")
        return project_id

    @staticmethod
    def _evidence_item(connection: sqlite3.Connection, evidence_id: str) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT e.evidence_id, e.source_type, e.source_id, e.content,
                   e.source_timestamp AS timestamp, u.project_id, u.chat_id,
                   p.display_name AS project,
                   c.host_chat_reference AS chat_reference
            FROM claim_evidence e
            JOIN semantic_units u ON u.semantic_unit_id = e.semantic_unit_id
            JOIN projects p ON p.project_id = u.project_id
            LEFT JOIN chats c ON c.chat_id = u.chat_id
            WHERE e.evidence_id = ?
            """,
            (evidence_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def search_library(
        self,
        query: str,
        project: str | None = None,
        limit: int | None = None,
        *,
        caller: LibraryCaller = LibraryCaller.DIRECT_FRONTIER,
    ) -> dict[str, Any]:
        denied = self._authorize(caller)
        if denied is not None:
            return denied
        normalized_query = _required_text(query, "query", maximum=512)
        result_limit = _limit(limit)
        with _read_only_database(self.database_path) as connection:
            project_id = self._validate_project(connection, project)
            lexical = LexicalRetriever(self.database_path).retrieve(
                connection, normalized_query, limit=100
            )
            vector = VectorRetriever(self._backend).retrieve(
                connection, normalized_query, limit=100
            )
            fused = RRFFuser().fuse(lexical, vector)
            ranked = IdentityReranker().rerank(fused, normalized_query).candidates
            items: list[dict[str, Any]] = []
            for candidate in ranked:
                if project_id is not None and candidate.project_id != project_id:
                    continue
                item = self._evidence_item(connection, candidate.evidence_id)
                if item is None:
                    continue
                item.update(
                    lexical_rank=candidate.lexical_rank,
                    vector_rank=candidate.vector_rank,
                    fused_score=candidate.fused_score,
                    rerank_rank=len(items) + 1,
                )
                items.append(item)
                if len(items) >= result_limit:
                    break
        return bounded_payload("search_library", items, requested_limit=result_limit)

    def get_library_item(
        self,
        item_id: str,
        *,
        caller: LibraryCaller = LibraryCaller.DIRECT_FRONTIER,
    ) -> dict[str, Any]:
        denied = self._authorize(caller)
        if denied is not None:
            return denied
        stable_id = _required_text(item_id, "id", maximum=256)
        with _read_only_database(self.database_path) as connection:
            item = self._evidence_item(connection, stable_id)
            if item is None:
                item = self._source_item(connection, stable_id)
        if item is None:
            raise LibraryQueryError("Library item was not found")
        return bounded_payload("get_library_item", [item], requested_limit=1)

    @staticmethod
    def _source_item(connection: sqlite3.Connection, source_id: str) -> dict[str, Any] | None:
        message = connection.execute(
            """
            SELECT m.message_id AS source_id, 'chat_message' AS source_type,
                   m.content, m.captured_at AS timestamp, p.project_id,
                   p.display_name AS project, c.chat_id,
                   c.host_chat_reference AS chat_reference,
                   (SELECT min(e.evidence_id) FROM claim_evidence e
                    WHERE e.source_type = 'chat_message' AND e.source_id = m.message_id)
                       AS evidence_id
            FROM messages m JOIN chats c ON c.chat_id = m.chat_id
            JOIN projects p ON p.project_id = c.project_id
            WHERE m.message_id = ?
            """,
            (source_id,),
        ).fetchone()
        if message is not None:
            return dict(message)
        document = connection.execute(
            """
            SELECT d.document_id AS source_id, 'elected_document' AS source_type,
                   d.text_content AS content, d.updated_at AS timestamp,
                   p.project_id, p.display_name AS project, NULL AS chat_id,
                   NULL AS chat_reference,
                   (SELECT min(e.evidence_id) FROM claim_evidence e
                    WHERE e.source_type = 'elected_document' AND e.source_id = d.document_id)
                       AS evidence_id
            FROM documents d JOIN projects p ON p.project_id = d.project_id
            WHERE d.document_id = ?
            """,
            (source_id,),
        ).fetchone()
        return dict(document) if document is not None else None

    def recent_library_context(
        self,
        project: str | None = None,
        limit: int | None = None,
        *,
        caller: LibraryCaller = LibraryCaller.DIRECT_FRONTIER,
    ) -> dict[str, Any]:
        denied = self._authorize(caller)
        if denied is not None:
            return denied
        result_limit = _limit(limit)
        with _read_only_database(self.database_path) as connection:
            project_id = self._validate_project(connection, project)
            where = "WHERE p.project_id = ?" if project_id is not None else ""
            parameters = (project_id,) if project_id is not None else ()
            rows = connection.execute(
                f"""
                SELECT m.message_id AS source_id, 'chat_message' AS source_type,
                       m.content, m.captured_at AS timestamp, p.project_id,
                       p.display_name AS project, c.chat_id,
                       c.host_chat_reference AS chat_reference
                FROM messages m JOIN chats c ON c.chat_id = m.chat_id
                JOIN projects p ON p.project_id = c.project_id {where}
                UNION ALL
                SELECT d.document_id, 'elected_document', d.text_content,
                       d.updated_at, p.project_id, p.display_name, NULL, NULL
                FROM documents d JOIN projects p ON p.project_id = d.project_id {where}
                ORDER BY timestamp DESC, source_id
                LIMIT ?
                """,
                (*parameters, *parameters, result_limit),
            ).fetchall()
            items = [dict(row) for row in rows]
        return bounded_payload("recent_library_context", items, requested_limit=result_limit)

    def list_library_projects(
        self,
        *,
        caller: LibraryCaller = LibraryCaller.DIRECT_FRONTIER,
    ) -> dict[str, Any]:
        denied = self._authorize(caller)
        if denied is not None:
            return denied
        with _read_only_database(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT p.project_id, p.display_name AS project, p.host AS source_host,
                       p.updated_at AS timestamp,
                       (SELECT count(*) FROM chats c WHERE c.project_id = p.project_id)
                           AS chat_count,
                       (SELECT count(*) FROM documents d WHERE d.project_id = p.project_id)
                           AS document_count
                FROM projects p ORDER BY p.updated_at DESC, p.project_id LIMIT ?
                """,
                (MAX_RESULTS,),
            ).fetchall()
        return bounded_payload(
            "list_library_projects", (dict(row) for row in rows), requested_limit=MAX_RESULTS
        )
