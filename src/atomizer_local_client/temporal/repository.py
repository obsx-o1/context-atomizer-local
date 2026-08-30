"""Versioned persistence for rebuildable evidence temporal state."""

from __future__ import annotations

import sqlite3

from atomizer_local_client.chat.contracts import utc_now
from atomizer_local_client.temporal.contracts import TemporalState
from atomizer_local_client.temporal.evaluator import TemporalEvaluator


class TemporalRepository:
    def __init__(self, connection: sqlite3.Connection, evaluator: TemporalEvaluator | None = None) -> None:
        self.connection = connection
        self.evaluator = evaluator or TemporalEvaluator()

    def rebuild(self) -> int:
        expected = self.evaluator.evaluate(self.connection)
        expected_ids = {item.evidence_id for item in expected}
        now = utc_now()
        for item in expected:
            self.connection.execute(
                """
                INSERT INTO temporal_evidence_state(
                    evidence_id,state,observed_at,valid_from,valid_to,superseded_by,
                    evaluator_version,updated_at
                ) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(evidence_id) DO UPDATE SET
                    state=excluded.state, observed_at=excluded.observed_at,
                    valid_from=excluded.valid_from, valid_to=excluded.valid_to,
                    superseded_by=excluded.superseded_by,
                    evaluator_version=excluded.evaluator_version, updated_at=excluded.updated_at
                """,
                (item.evidence_id, item.state.value, item.observed_at, item.valid_from,
                 item.valid_to, item.superseded_by, self.evaluator.version, now),
            )
        if expected_ids:
            marks = ",".join("?" for _ in expected_ids)
            self.connection.execute(
                f"DELETE FROM temporal_evidence_state WHERE evidence_id NOT IN ({marks})",
                tuple(sorted(expected_ids)),
            )
        else:
            self.connection.execute("DELETE FROM temporal_evidence_state")
        return len(expected)

    def mark_disputed(self, claim_ids: set[str]) -> int:
        if not claim_ids:
            return 0
        marks = ",".join("?" for _ in claim_ids)
        return self.connection.execute(
            f"""UPDATE temporal_evidence_state SET state=?, updated_at=?
                WHERE state=? AND evidence_id IN
                (SELECT evidence_id FROM claim_evidence WHERE claim_id IN ({marks}))""",
            (TemporalState.DISPUTED.value, utc_now(), TemporalState.CURRENT.value,
             *sorted(claim_ids)),
        ).rowcount
