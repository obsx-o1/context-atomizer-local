ALTER TABLE documents ADD COLUMN local_source_key TEXT;
ALTER TABLE documents ADD COLUMN content_sha256 TEXT;
ALTER TABLE documents ADD COLUMN file_size INTEGER;
ALTER TABLE documents ADD COLUMN modified_time_ns INTEGER;
ALTER TABLE documents ADD COLUMN file_identity TEXT;

UPDATE documents
SET local_source_key = lower(local_source_reference)
WHERE local_source_key IS NULL;

CREATE UNIQUE INDEX documents_project_source_key_unique
ON documents(project_id, local_source_key)
WHERE local_source_key IS NOT NULL;

CREATE INDEX documents_file_identity
ON documents(project_id, file_identity)
WHERE file_identity IS NOT NULL;

CREATE TABLE elected_sources (
    source_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    source_kind TEXT NOT NULL CHECK(source_kind IN ('FILE', 'DIRECTORY')),
    display_name TEXT NOT NULL,
    local_source_reference TEXT NOT NULL,
    local_source_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_synced_at TEXT,
    UNIQUE(project_id, local_source_key)
);

CREATE TABLE document_source_memberships (
    source_id TEXT NOT NULL REFERENCES elected_sources(source_id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    PRIMARY KEY(source_id, document_id)
);

CREATE INDEX document_source_memberships_document
ON document_source_memberships(document_id);

INSERT INTO elected_sources(
    source_id, project_id, source_kind, display_name,
    local_source_reference, local_source_key, created_at, updated_at, last_synced_at
)
SELECT
    'legacy-file:' || document_id,
    project_id,
    'FILE',
    display_name,
    local_source_reference,
    local_source_key,
    updated_at,
    updated_at,
    updated_at
FROM documents;

INSERT INTO document_source_memberships(source_id, document_id)
SELECT 'legacy-file:' || document_id, document_id
FROM documents;
