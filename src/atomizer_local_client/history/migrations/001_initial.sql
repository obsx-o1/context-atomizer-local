CREATE TABLE projects (
    project_id TEXT PRIMARY KEY,
    host TEXT NOT NULL,
    host_project_reference TEXT,
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX projects_host_reference_unique
ON projects(host, host_project_reference)
WHERE host_project_reference IS NOT NULL;

CREATE TABLE chats (
    chat_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    host TEXT NOT NULL,
    host_chat_reference TEXT NOT NULL,
    display_title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(host, host_chat_reference)
);

CREATE TABLE messages (
    message_id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    host_turn_reference TEXT,
    sequence_number INTEGER NOT NULL CHECK(sequence_number > 0),
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
    content TEXT NOT NULL CHECK(length(content) > 0),
    captured_at TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    UNIQUE(chat_id, sequence_number)
);

CREATE INDEX messages_chat_sequence ON messages(chat_id, sequence_number);

CREATE TABLE documents (
    document_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    display_name TEXT NOT NULL,
    document_type TEXT NOT NULL CHECK(document_type IN ('text', 'markdown')),
    local_source_reference TEXT NOT NULL,
    text_content TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, local_source_reference)
);

CREATE TABLE lexical_entries (
    lexical_entry_id TEXT PRIMARY KEY,
    corpus_type TEXT NOT NULL CHECK(corpus_type IN ('CHAT_HISTORY', 'ELECTED_DOCUMENT')),
    source_id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    chat_id TEXT REFERENCES chats(chat_id) ON DELETE CASCADE,
    role TEXT,
    content TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(corpus_type, source_id)
);

CREATE INDEX lexical_entries_project ON lexical_entries(project_id);
CREATE INDEX lexical_entries_chat ON lexical_entries(chat_id);

CREATE VIRTUAL TABLE lexical_entries_fts USING fts5(
    content,
    content='lexical_entries',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER lexical_entries_after_insert AFTER INSERT ON lexical_entries BEGIN
    INSERT INTO lexical_entries_fts(rowid, content) VALUES (new.rowid, new.content);
END;

CREATE TRIGGER lexical_entries_after_delete AFTER DELETE ON lexical_entries BEGIN
    INSERT INTO lexical_entries_fts(lexical_entries_fts, rowid, content)
    VALUES ('delete', old.rowid, old.content);
END;

CREATE TRIGGER lexical_entries_after_update AFTER UPDATE ON lexical_entries BEGIN
    INSERT INTO lexical_entries_fts(lexical_entries_fts, rowid, content)
    VALUES ('delete', old.rowid, old.content);
    INSERT INTO lexical_entries_fts(rowid, content) VALUES (new.rowid, new.content);
END;

