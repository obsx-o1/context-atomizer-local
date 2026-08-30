CREATE TABLE document_revision_history (
    document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    revision INTEGER NOT NULL CHECK(revision > 0),
    text_content TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    superseded_at TEXT NOT NULL,
    PRIMARY KEY(document_id, revision)
);

CREATE TRIGGER documents_capture_revision_before_update
BEFORE UPDATE OF text_content ON documents
WHEN OLD.content_sha256 <> NEW.content_sha256
BEGIN
    INSERT INTO document_revision_history(
        document_id, project_id, revision, text_content, content_sha256,
        observed_at, superseded_at
    ) VALUES (
        OLD.document_id, OLD.project_id, OLD.revision, OLD.text_content,
        OLD.content_sha256, OLD.updated_at, NEW.superseded_at
    );
END;

CREATE TABLE temporal_evidence_state (
    evidence_id TEXT PRIMARY KEY REFERENCES claim_evidence(evidence_id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK(state IN ('current','superseded','expired_or_invalid','disputed','unknown')),
    observed_at TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    superseded_by TEXT REFERENCES claim_evidence(evidence_id) ON DELETE SET NULL,
    evaluator_version TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE contradiction_relations (
    claim_a_id TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE CASCADE,
    claim_b_id TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE CASCADE,
    relation_state TEXT NOT NULL CHECK(relation_state IN ('unresolved','resolved_by_supersession')),
    rule_name TEXT NOT NULL,
    confidence REAL NOT NULL,
    detector_version TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(claim_a_id, claim_b_id),
    CHECK(claim_a_id < claim_b_id)
);

CREATE TABLE claim_verification_state (
    claim_id TEXT PRIMARY KEY REFERENCES claims(claim_id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK(state IN ('unverified','single_source','corroborated','disputed','verified_explicitly')),
    independent_source_count INTEGER NOT NULL CHECK(independent_source_count >= 0),
    evaluator_version TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
