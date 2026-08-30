CREATE TABLE semantic_units (
    semantic_unit_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL CHECK(source_type IN ('chat_message', 'elected_document')),
    source_id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    chat_id TEXT REFERENCES chats(chat_id) ON DELETE CASCADE,
    source_revision INTEGER NOT NULL CHECK(source_revision > 0),
    unit_index INTEGER NOT NULL CHECK(unit_index >= 0),
    start_offset INTEGER NOT NULL CHECK(start_offset >= 0),
    end_offset INTEGER NOT NULL CHECK(end_offset >= start_offset),
    content TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    chunker_version TEXT NOT NULL,
    source_updated_at TEXT NOT NULL,
    UNIQUE(source_type, source_id, source_revision, unit_index, chunker_version)
);

CREATE INDEX semantic_units_source ON semantic_units(source_type, source_id);
CREATE INDEX semantic_units_project ON semantic_units(project_id, chat_id);

CREATE TABLE embedding_records (
    semantic_unit_id TEXT PRIMARY KEY REFERENCES semantic_units(semantic_unit_id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK(state IN ('indexed', 'unchanged', 'failed', 'invalidated')),
    backend_version TEXT NOT NULL,
    model_sha256 TEXT NOT NULL,
    dimension INTEGER NOT NULL CHECK(dimension > 0),
    content_fingerprint TEXT NOT NULL,
    vector BLOB,
    error_class TEXT,
    updated_at TEXT NOT NULL,
    CHECK((state IN ('indexed', 'unchanged') AND vector IS NOT NULL AND error_class IS NULL)
       OR (state IN ('failed', 'invalidated') AND vector IS NULL))
);

CREATE INDEX embedding_records_state ON embedding_records(state, backend_version);
