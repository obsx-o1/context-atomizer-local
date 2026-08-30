ALTER TABLE documents ADD COLUMN previous_content_sha256 TEXT;
ALTER TABLE documents ADD COLUMN superseded_at TEXT;
ALTER TABLE documents ADD COLUMN revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0);
