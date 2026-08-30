"""Conservative source independence: distinct Documents or distinct Chat sessions."""

from __future__ import annotations

import sqlite3

from atomizer_local_client.verification.contracts import VerificationState

VERIFICATION_EVALUATOR_VERSION = "independent-source-v1"


class VerificationEvaluator:
    version = VERIFICATION_EVALUATOR_VERSION

    def evaluate(self, connection: sqlite3.Connection) -> tuple[tuple[str, VerificationState, int], ...]:
        disputed = {
            str(value)
            for row in connection.execute(
                "SELECT claim_a_id, claim_b_id FROM contradiction_relations WHERE relation_state='unresolved'"
            ).fetchall()
            for value in row
        }
        result = []
        for claim in connection.execute("SELECT claim_id FROM claims ORDER BY claim_id").fetchall():
            claim_id = str(claim[0])
            sources = {
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT DISTINCT CASE e.source_type
                      WHEN 'elected_document' THEN 'document:' || e.source_id
                      ELSE 'chat:' || u.chat_id END
                    FROM claim_evidence e
                    JOIN semantic_units u ON u.semantic_unit_id=e.semantic_unit_id
                    JOIN temporal_evidence_state t ON t.evidence_id=e.evidence_id
                    WHERE e.claim_id=? AND t.state IN ('current','disputed')
                    """,
                    (claim_id,),
                ).fetchall()
                if row[0] is not None
            }
            if claim_id in disputed:
                state = VerificationState.DISPUTED
            elif len(sources) >= 2:
                state = VerificationState.CORROBORATED
            elif len(sources) == 1:
                state = VerificationState.SINGLE_SOURCE
            else:
                state = VerificationState.UNVERIFIED
            result.append((claim_id, state, len(sources)))
        return tuple(result)
