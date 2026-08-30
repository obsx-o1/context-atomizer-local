CREATE TABLE claims (
    claim_id TEXT PRIMARY KEY,
    canonical_text TEXT NOT NULL,
    normalized_form TEXT NOT NULL,
    entity_signature TEXT NOT NULL,
    polarity TEXT NOT NULL CHECK(polarity IN ('positive', 'negative', 'unknown')),
    canonicalizer_version TEXT NOT NULL,
    UNIQUE(normalized_form, entity_signature, polarity, canonicalizer_version)
);

CREATE TABLE claim_evidence (
    evidence_id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE CASCADE,
    semantic_unit_id TEXT NOT NULL REFERENCES semantic_units(semantic_unit_id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_revision INTEGER NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    content TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    source_timestamp TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    equivalence_version TEXT NOT NULL,
    UNIQUE(semantic_unit_id, start_offset, end_offset, extractor_version)
);

CREATE INDEX claim_evidence_claim ON claim_evidence(claim_id);
CREATE INDEX claim_evidence_unit ON claim_evidence(semantic_unit_id);

CREATE TABLE claim_equivalence_decisions (
    evidence_id TEXT PRIMARY KEY REFERENCES claim_evidence(evidence_id) ON DELETE CASCADE,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE CASCADE,
    decision TEXT NOT NULL CHECK(decision IN ('exact', 'high_confidence_semantic', 'separate')),
    confidence REAL NOT NULL,
    algorithm_version TEXT NOT NULL
);
