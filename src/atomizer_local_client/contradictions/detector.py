"""Explicit-value and polarity contradiction rules; similarity alone is insufficient."""

from __future__ import annotations

import re
import sqlite3

from atomizer_local_client.contradictions.contracts import Contradiction

CONTRADICTION_DETECTOR_VERSION = "explicit-contradiction-v1"
_VALUE = re.compile(r"^\s*(.+?)\s*(?:=|\bis\b|\bequals?\b)\s*([^.;]+)", re.IGNORECASE)


class ContradictionDetector:
    version = CONTRADICTION_DETECTOR_VERSION

    @staticmethod
    def _subject_value(text: str) -> tuple[str, str] | None:
        match = _VALUE.search(text.casefold())
        if not match:
            return None
        return (" ".join(match.group(1).split()), " ".join(match.group(2).split()))

    def detect(self, connection: sqlite3.Connection) -> tuple[Contradiction, ...]:
        rows = connection.execute(
            """
            SELECT DISTINCT c.* FROM claims c
            JOIN claim_evidence e ON e.claim_id=c.claim_id
            JOIN temporal_evidence_state t ON t.evidence_id=e.evidence_id
            WHERE t.state='current'
            ORDER BY c.claim_id
            """
        ).fetchall()
        result: list[Contradiction] = []
        for index, left in enumerate(rows):
            for right in rows[index + 1:]:
                left_id, right_id = sorted((str(left["claim_id"]), str(right["claim_id"])))
                if (
                    left["normalized_form"] == right["normalized_form"]
                    and left["entity_signature"] == right["entity_signature"]
                    and left["polarity"] != right["polarity"]
                ):
                    result.append(Contradiction(left_id, right_id, "opposing_polarity", 1.0))
                    continue
                left_value = self._subject_value(str(left["canonical_text"]))
                right_value = self._subject_value(str(right["canonical_text"]))
                if left_value and right_value and left_value[0] == right_value[0] and left_value[1] != right_value[1]:
                    result.append(Contradiction(left_id, right_id, "conflicting_explicit_value", 1.0))
        return tuple(result)
