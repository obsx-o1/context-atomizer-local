"""Adapter over the frozen FTS/BM25 source retriever."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from atomizer_local_client.lexical.search import search_all_local_history
from atomizer_local_client.retrieval.contracts import RankedEvidence

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


class LexicalRetriever:
    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)

    def retrieve(self, connection: sqlite3.Connection, query: str, *, limit: int = 100) -> tuple[RankedEvidence, ...]:
        source_candidates = search_all_local_history(self.database_path, query, limit=min(limit, 100))
        query_tokens = {token.casefold() for token in _TOKEN.findall(query)}
        result: list[RankedEvidence] = []
        rank = 0
        for source in source_candidates:
            rows = connection.execute(
                """
                SELECT e.*,u.project_id,u.chat_id FROM claim_evidence e
                JOIN semantic_units u ON u.semantic_unit_id=e.semantic_unit_id
                WHERE e.source_type=? AND e.source_id=? ORDER BY e.evidence_id
                """,
                ("chat_message" if source.corpus_type.value == "CHAT_HISTORY" else "elected_document",
                 source.source_id),
            ).fetchall()
            for row in rows:
                content_tokens = {token.casefold() for token in _TOKEN.findall(str(row["content"]))}
                if not query_tokens.issubset(content_tokens):
                    continue
                rank += 1
                result.append(
                    RankedEvidence(
                        evidence_id=str(row["evidence_id"]), semantic_unit_id=str(row["semantic_unit_id"]),
                        source_type=str(row["source_type"]), source_id=str(row["source_id"]),
                        project_id=str(row["project_id"]), chat_id=str(row["chat_id"]) if row["chat_id"] else None,
                        claim_id=str(row["claim_id"]), content=str(row["content"]),
                        lexical_rank=rank, lexical_score=float(source.score),
                    )
                )
        return tuple(result[:limit])
