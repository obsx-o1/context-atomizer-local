CREATE TABLE chat_identity_aliases (
    identity_digest TEXT PRIMARY KEY CHECK(length(identity_digest) = 64),
    chat_id TEXT NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);

CREATE INDEX chat_identity_aliases_chat
ON chat_identity_aliases(chat_id);

CREATE TABLE chat_forget_tombstones (
    identity_digest TEXT PRIMARY KEY CHECK(length(identity_digest) = 64),
    tombstone_id TEXT NOT NULL CHECK(length(tombstone_id) = 64),
    host TEXT NOT NULL,
    forgotten_at TEXT NOT NULL
);

CREATE INDEX chat_forget_tombstones_group
ON chat_forget_tombstones(tombstone_id);
