"""Versioned verification-state projection."""

from __future__ import annotations

import sqlite3

from atomizer_local_client.chat.contracts import utc_now
from atomizer_local_client.verification.evaluator import VerificationEvaluator


class VerificationRepository:
    def __init__(self, connection: sqlite3.Connection, evaluator: VerificationEvaluator | None = None) -> None:
        self.connection = connection
        self.evaluator = evaluator or VerificationEvaluator()

    def rebuild(self) -> int:
        expected = self.evaluator.evaluate(self.connection)
        self.connection.execute("DELETE FROM claim_verification_state")
        for claim_id, state, count in expected:
            self.connection.execute(
                "INSERT INTO claim_verification_state VALUES (?,?,?,?,?)",
                (claim_id, state.value, count, self.evaluator.version, utc_now()),
            )
        return len(expected)
