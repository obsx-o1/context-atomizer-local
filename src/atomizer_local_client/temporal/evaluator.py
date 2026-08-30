"""Deterministic temporal evaluation from authoritative source revisions."""

from __future__ import annotations

import sqlite3

from atomizer_local_client.temporal.contracts import EvidenceTemporalState, TemporalState

TEMPORAL_EVALUATOR_VERSION = "source-revision-temporal-v1"


class TemporalEvaluator:
    version = TEMPORAL_EVALUATOR_VERSION

    def evaluate(self, connection: sqlite3.Connection) -> tuple[EvidenceTemporalState, ...]:
        rows = connection.execute(
            """
            SELECT e.evidence_id, e.source_type, e.source_id, e.source_revision,
                   e.source_timestamp, d.revision AS current_revision,
                   h.superseded_at,
                   (SELECT current_e.evidence_id FROM claim_evidence current_e
                    WHERE current_e.source_type=e.source_type AND current_e.source_id=e.source_id
                      AND current_e.source_revision=d.revision
                    ORDER BY current_e.evidence_id LIMIT 1) AS current_evidence_id
            FROM claim_evidence e
            LEFT JOIN documents d
              ON e.source_type='elected_document' AND d.document_id=e.source_id
            LEFT JOIN document_revision_history h
              ON h.document_id=e.source_id AND h.revision=e.source_revision
            ORDER BY e.evidence_id
            """
        ).fetchall()
        result: list[EvidenceTemporalState] = []
        for row in rows:
            observed = str(row["source_timestamp"])
            if row["source_type"] == "chat_message":
                state = TemporalState.CURRENT
                valid_to = None
                superseded_by = None
            elif row["current_revision"] is None:
                state = TemporalState.UNKNOWN
                valid_to = None
                superseded_by = None
            elif int(row["source_revision"]) < int(row["current_revision"]):
                state = TemporalState.SUPERSEDED
                valid_to = str(row["superseded_at"]) if row["superseded_at"] else None
                superseded_by = (
                    str(row["current_evidence_id"]) if row["current_evidence_id"] else None
                )
            else:
                state = TemporalState.CURRENT
                valid_to = None
                superseded_by = None
            result.append(
                EvidenceTemporalState(
                    evidence_id=str(row["evidence_id"]), state=state,
                    observed_at=observed, valid_from=observed, valid_to=valid_to,
                    superseded_by=superseded_by,
                )
            )
        return tuple(result)
