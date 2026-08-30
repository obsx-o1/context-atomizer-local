CREATE TABLE entities (
    entity_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    canonical_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    canonicalizer_version TEXT NOT NULL,
    UNIQUE(entity_type, canonical_key, canonicalizer_version)
);

CREATE TABLE entity_aliases (
    entity_type TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    entity_id TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    PRIMARY KEY(entity_type, normalized_alias)
);

CREATE TABLE entity_mentions (
    mention_id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    semantic_unit_id TEXT NOT NULL REFERENCES semantic_units(semantic_unit_id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_revision INTEGER NOT NULL,
    start_offset INTEGER,
    end_offset INTEGER,
    surface_text TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    UNIQUE(semantic_unit_id, start_offset, end_offset, extractor_version)
);

CREATE INDEX entity_mentions_unit ON entity_mentions(semantic_unit_id);
CREATE INDEX entity_mentions_entity ON entity_mentions(entity_id);
