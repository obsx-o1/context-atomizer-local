"""Transactional claim/evidence projection over semantic units."""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Protocol

from atomizer_local_client.claims.canonicalizer import ConservativeClaimCanonicalizer
from atomizer_local_client.claims.contracts import CanonicalClaim
from atomizer_local_client.claims.deduplicator import ConservativeClaimDeduplicator
from atomizer_local_client.claims.extractor import DeclarativeClaimExtractor

CLAIM_EVIDENCE_COLUMNS = (
    "evidence_id",
    "claim_id",
    "semantic_unit_id",
    "source_type",
    "source_id",
    "source_revision",
    "start_offset",
    "end_offset",
    "content",
    "content_sha256",
    "source_timestamp",
    "extractor_version",
    "equivalence_version",
)


class ClaimRepositoryIntegrityError(RuntimeError):
    """A persisted canonical identity disagrees with its deterministic ID."""


class _ClaimExtractor(Protocol):
    version: str

    def extract(self, text: str): ...


class _ClaimCanonicalizer(Protocol):
    version: str

    def canonicalize(self, claim, entity_signature: str) -> CanonicalClaim: ...


class ClaimRepository:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        extractor: _ClaimExtractor | None = None,
        canonicalizer: _ClaimCanonicalizer | None = None,
        deduplicator: ConservativeClaimDeduplicator | None = None,
    ) -> None:
        self.connection = connection
        self.extractor = extractor or DeclarativeClaimExtractor()
        self.canonicalizer = canonicalizer or ConservativeClaimCanonicalizer()
        self.deduplicator = deduplicator or ConservativeClaimDeduplicator()

    def _entity_signature(self, unit_id: str, start: int, end: int) -> str:
        rows = self.connection.execute(
            "SELECT DISTINCT entity_id FROM entity_mentions WHERE semantic_unit_id=? "
            "AND start_offset < ? AND end_offset > ? ORDER BY entity_id",
            (unit_id, end, start),
        ).fetchall()
        return ",".join(str(row[0]) for row in rows)

    @staticmethod
    def _identity(claim: CanonicalClaim, version: str) -> tuple[str, str, str, str]:
        return (
            claim.normalized_form,
            claim.entity_signature,
            claim.polarity,
            version,
        )

    def _get_or_create_claim(self, claim: CanonicalClaim) -> CanonicalClaim:
        identity = self._identity(claim, self.canonicalizer.version)
        self.connection.execute(
            """
            INSERT INTO claims(
                claim_id, canonical_text, normalized_form, entity_signature,
                polarity, canonicalizer_version
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(
                normalized_form, entity_signature, polarity, canonicalizer_version
            ) DO NOTHING
            """,
            (claim.claim_id, claim.canonical_text, *identity),
        )
        row = self.connection.execute(
            """
            SELECT claim_id, canonical_text, normalized_form, entity_signature, polarity
            FROM claims
            WHERE normalized_form=? AND entity_signature=? AND polarity=?
              AND canonicalizer_version=?
            """,
            identity,
        ).fetchone()
        if row is None:
            raise ClaimRepositoryIntegrityError("canonical Claim was not persisted")
        stored_id = str(row["claim_id"])
        if stored_id != claim.claim_id:
            raise ClaimRepositoryIntegrityError(
                "canonical Claim identity maps to a different deterministic claim_id"
            )
        return CanonicalClaim(
            claim_id=stored_id,
            canonical_text=str(row["canonical_text"]),
            normalized_form=str(row["normalized_form"]),
            entity_signature=str(row["entity_signature"]),
            polarity=str(row["polarity"]),
        )

    def _upsert_evidence(
        self,
        *,
        evidence_id: str,
        claim_id: str,
        unit: sqlite3.Row,
        extracted: object,
        content_hash: str,
    ) -> None:
        prior = self.connection.execute(
            """
            SELECT evidence_id FROM claim_evidence
            WHERE semantic_unit_id=? AND start_offset=? AND end_offset=?
              AND extractor_version=?
            """,
            (
                unit["semantic_unit_id"], extracted.start_offset,
                extracted.end_offset, self.extractor.version,
            ),
        ).fetchone()
        if prior is not None and str(prior["evidence_id"]) != evidence_id:
            raise ClaimRepositoryIntegrityError(
                "claim Evidence identity maps to a different deterministic evidence_id"
            )
        self.connection.execute(
            """
            INSERT INTO claim_evidence(
                evidence_id, claim_id, semantic_unit_id, source_type, source_id,
                source_revision, start_offset, end_offset, content, content_sha256,
                source_timestamp, extractor_version, equivalence_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(evidence_id) DO UPDATE SET
                claim_id=excluded.claim_id,
                source_type=excluded.source_type,
                source_id=excluded.source_id,
                source_revision=excluded.source_revision,
                content=excluded.content,
                content_sha256=excluded.content_sha256,
                source_timestamp=excluded.source_timestamp,
                equivalence_version=excluded.equivalence_version
            """,
            (
                evidence_id, claim_id, unit["semantic_unit_id"], unit["source_type"],
                unit["source_id"], unit["source_revision"], extracted.start_offset,
                extracted.end_offset, extracted.content, content_hash,
                unit["source_updated_at"], self.extractor.version,
                self.deduplicator.version,
            ),
        )

    def rebuild(self) -> int:
        if not self.connection.in_transaction:
            raise RuntimeError("Claim rebuild requires an active transaction")
        known: list[CanonicalClaim] = []
        expected_evidence_ids: set[str] = set()
        count = 0
        for unit in self.connection.execute(
            "SELECT * FROM semantic_units ORDER BY semantic_unit_id"
        ).fetchall():
            for extracted in self.extractor.extract(str(unit["content"])):
                signature = self._entity_signature(
                    str(unit["semantic_unit_id"]), extracted.start_offset, extracted.end_offset
                )
                candidate = self.canonicalizer.canonicalize(extracted, signature)
                decision = self.deduplicator.decide(candidate, tuple(known))
                claim = next((item for item in known if item.claim_id == decision.claim_id), candidate)
                if claim.claim_id == candidate.claim_id:
                    claim = self._get_or_create_claim(claim)
                    if all(item.claim_id != claim.claim_id for item in known):
                        known.append(claim)
                content_hash = hashlib.sha256(extracted.content.encode("utf-8")).hexdigest()
                evidence_id = hashlib.sha256(
                    f"{unit['semantic_unit_id']}\x1f{extracted.start_offset}\x1f{extracted.end_offset}\x1f{self.extractor.version}".encode("utf-8")
                ).hexdigest()
                self._upsert_evidence(
                    evidence_id=evidence_id,
                    claim_id=claim.claim_id,
                    unit=unit,
                    extracted=extracted,
                    content_hash=content_hash,
                )
                self.connection.execute(
                    """
                    INSERT INTO claim_equivalence_decisions(
                        evidence_id, claim_id, decision, confidence, algorithm_version
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(evidence_id) DO UPDATE SET
                        claim_id=excluded.claim_id,
                        decision=excluded.decision,
                        confidence=excluded.confidence,
                        algorithm_version=excluded.algorithm_version
                    """,
                    (evidence_id, decision.claim_id, decision.decision,
                     decision.confidence, self.deduplicator.version),
                )
                expected_evidence_ids.add(evidence_id)
                count += 1
        if expected_evidence_ids:
            placeholders = ", ".join("?" for _ in expected_evidence_ids)
            self.connection.execute(
                f"DELETE FROM claim_evidence WHERE evidence_id NOT IN ({placeholders})",
                tuple(sorted(expected_evidence_ids)),
            )
        else:
            self.connection.execute("DELETE FROM claim_evidence")
        self.connection.execute(
            "DELETE FROM claims WHERE NOT EXISTS "
            "(SELECT 1 FROM claim_evidence e WHERE e.claim_id=claims.claim_id)"
        )
        return count
