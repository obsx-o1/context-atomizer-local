"""Idempotent contradiction relation persistence."""

from __future__ import annotations

import sqlite3

from atomizer_local_client.chat.contracts import utc_now
from atomizer_local_client.contradictions.contracts import (
    ContradictionRelation,
    ContradictionRelationState,
)
from atomizer_local_client.contradictions.detector import ContradictionDetector


class ContradictionRepository:
    def __init__(self, connection: sqlite3.Connection, detector: ContradictionDetector | None = None) -> None:
        self.connection = connection
        self.detector = detector or ContradictionDetector()

    def get_unresolved(self) -> tuple[ContradictionRelation, ...]:
        rows = self.connection.execute(
            "SELECT claim_a_id,claim_b_id,relation_state "
            "FROM contradiction_relations WHERE relation_state=? "
            "ORDER BY claim_a_id,claim_b_id",
            (ContradictionRelationState.UNRESOLVED.value,),
        ).fetchall()
        return tuple(
            ContradictionRelation(
                claim_a_id=str(row["claim_a_id"]),
                claim_b_id=str(row["claim_b_id"]),
                relation_state=ContradictionRelationState(str(row["relation_state"])),
            )
            for row in rows
        )

    def rebuild(self) -> tuple[int, set[str]]:
        relations = self.detector.detect(self.connection)
        self.connection.execute("DELETE FROM contradiction_relations")
        disputed: set[str] = set()
        for item in relations:
            self.connection.execute(
                """INSERT INTO contradiction_relations(
                       claim_a_id,claim_b_id,relation_state,rule_name,confidence,
                       detector_version,updated_at
                   ) VALUES (?,?,?,?,?,?,?)""",
                (item.claim_a_id, item.claim_b_id,
                 ContradictionRelationState.UNRESOLVED.value, item.rule_name,
                 item.confidence, self.detector.version, utc_now()),
            )
            disputed.update((item.claim_a_id, item.claim_b_id))
        return len(relations), disputed
