"""Deterministic FTS5/BM25 candidate retrieval with explicit local scopes."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from atomizer_local_client.chat.contracts import CorpusType, LexicalCandidate

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
DEFAULT_RESULT_LIMIT = 20
MAX_RESULT_LIMIT = 100
MAX_QUERY_LENGTH = 512
MAX_QUERY_TERMS = 32


def compile_literal_query(query: str) -> str:
    """Compile ordinary user text into an AND of quoted literal FTS tokens."""
    if not isinstance(query, str):
        raise ValueError("query must be text")
    if len(query) > MAX_QUERY_LENGTH:
        raise ValueError(f"query must be {MAX_QUERY_LENGTH} characters or fewer")
    tokens = _TOKEN.findall(query)
    if not tokens:
        raise ValueError("query must contain searchable text")
    if len(tokens) > MAX_QUERY_TERMS:
        raise ValueError(f"query must contain no more than {MAX_QUERY_TERMS} terms")
    return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


def _corpus_values(corpus_types: Iterable[CorpusType] | None) -> tuple[str, ...] | None:
    if corpus_types is None:
        return None
    values = tuple(sorted({CorpusType(value).value for value in corpus_types}))
    if not values:
        raise ValueError("at least one corpus type is required")
    return values


def _search(
    database_path: Path,
    query: str,
    *,
    chat_id: str | None = None,
    document_id: str | None = None,
    project_id: str | None = None,
    corpus_types: Iterable[CorpusType] | None = None,
    limit: int = DEFAULT_RESULT_LIMIT,
) -> list[LexicalCandidate]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_RESULT_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_RESULT_LIMIT}")
    if chat_id is not None and document_id is not None:
        raise ValueError("chat and document scopes cannot be combined")
    clauses = ["lexical_entries_fts MATCH ?"]
    parameters: list[object] = [compile_literal_query(query)]
    if chat_id is not None:
        clauses.extend(["entry.chat_id = ?", "entry.corpus_type = ?"])
        parameters.extend([chat_id, CorpusType.CHAT_HISTORY.value])
    if document_id is not None:
        clauses.extend(["entry.source_id = ?", "entry.corpus_type = ?"])
        parameters.extend([document_id, CorpusType.ELECTED_DOCUMENT.value])
    if project_id is not None:
        clauses.append("entry.project_id = ?")
        parameters.append(project_id)
    corpus_values = _corpus_values(corpus_types)
    if corpus_values is not None:
        placeholders = ", ".join("?" for _ in corpus_values)
        clauses.append(f"entry.corpus_type IN ({placeholders})")
        parameters.extend(corpus_values)
    parameters.append(limit)
    sql = f"""
        SELECT entry.corpus_type, entry.source_id, entry.project_id, entry.chat_id,
               entry.role, entry.content, bm25(lexical_entries_fts) AS score
        FROM lexical_entries_fts
        JOIN lexical_entries AS entry ON entry.rowid = lexical_entries_fts.rowid
        WHERE {' AND '.join(clauses)}
        ORDER BY score ASC, entry.corpus_type, entry.source_id
        LIMIT ?
    """
    uri = Path(database_path).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    try:
        rows = connection.execute(sql, parameters).fetchall()
    finally:
        connection.close()
    return [
        LexicalCandidate(
            corpus_type=CorpusType(row["corpus_type"]),
            source_id=str(row["source_id"]),
            project_id=str(row["project_id"]),
            chat_id=str(row["chat_id"]) if row["chat_id"] is not None else None,
            role=str(row["role"]) if row["role"] is not None else None,
            content=str(row["content"]),
            score=float(row["score"]),
        )
        for row in rows
    ]


def search_current_chat(
    database_path: Path,
    chat_id: str,
    query: str,
    *,
    limit: int = DEFAULT_RESULT_LIMIT,
) -> list[LexicalCandidate]:
    return _search(database_path, query, chat_id=chat_id, limit=limit)


def search_current_document(
    database_path: Path,
    document_id: str,
    query: str,
    *,
    limit: int = DEFAULT_RESULT_LIMIT,
) -> list[LexicalCandidate]:
    return _search(database_path, query, document_id=document_id, limit=limit)


def search_current_project(
    database_path: Path,
    project_id: str,
    query: str,
    *,
    limit: int = DEFAULT_RESULT_LIMIT,
) -> list[LexicalCandidate]:
    return _search(database_path, query, project_id=project_id, limit=limit)


def search_all_local_history(
    database_path: Path, query: str, *, limit: int = DEFAULT_RESULT_LIMIT
) -> list[LexicalCandidate]:
    return _search(database_path, query, limit=limit)


def search_chat_history(
    database_path: Path,
    query: str,
    *,
    project_id: str | None = None,
    limit: int = DEFAULT_RESULT_LIMIT,
) -> list[LexicalCandidate]:
    return _search(
        database_path,
        query,
        project_id=project_id,
        corpus_types=(CorpusType.CHAT_HISTORY,),
        limit=limit,
    )


def search_elected_documents(
    database_path: Path,
    query: str,
    *,
    project_id: str | None = None,
    limit: int = DEFAULT_RESULT_LIMIT,
) -> list[LexicalCandidate]:
    return _search(
        database_path,
        query,
        project_id=project_id,
        corpus_types=(CorpusType.ELECTED_DOCUMENT,),
        limit=limit,
    )
