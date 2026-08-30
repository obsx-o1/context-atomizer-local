"""Transactional replacement of entity mentions for current semantic units."""

from __future__ import annotations

import hashlib
import sqlite3

from atomizer_local_client.entities.canonicalizer import ConservativeEntityCanonicalizer
from atomizer_local_client.entities.contracts import EntityCanonicalizer, EntityExtractor
from atomizer_local_client.entities.extractor import RuleEntityExtractor


class EntityRepository:
    def __init__(
        self,
        connection: sqlite3.Connection,
        extractor: EntityExtractor | None = None,
        canonicalizer: EntityCanonicalizer | None = None,
    ) -> None:
        self.connection = connection
        self.extractor = extractor or RuleEntityExtractor()
        self.canonicalizer = canonicalizer or ConservativeEntityCanonicalizer()

    def rebuild(self) -> int:
        self.connection.execute("DELETE FROM entity_mentions")
        count = 0
        rows = self.connection.execute(
            "SELECT * FROM semantic_units ORDER BY semantic_unit_id"
        ).fetchall()
        for row in rows:
            content = str(row["content"])
            for mention in self.extractor.extract(content):
                entity = self.canonicalizer.canonicalize(mention)
                self.connection.execute(
                    "INSERT INTO entities VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(entity_id) DO NOTHING",
                    (entity.entity_id, entity.entity_type, entity.canonical_key,
                     entity.display_name, self.canonicalizer.version),
                )
                normalized = ConservativeEntityCanonicalizer.normalized(mention.surface_text)
                self.connection.execute(
                    "INSERT INTO entity_aliases VALUES (?, ?, ?) "
                    "ON CONFLICT(entity_type, normalized_alias) DO NOTHING",
                    (mention.entity_type, normalized, entity.entity_id),
                )
                mention_id = hashlib.sha256(
                    f"{row['semantic_unit_id']}\x1f{mention.start_offset}\x1f{mention.end_offset}\x1f{self.extractor.version}".encode("utf-8")
                ).hexdigest()
                self.connection.execute(
                    "INSERT INTO entity_mentions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        mention_id, entity.entity_id, row["semantic_unit_id"], row["source_type"],
                        row["source_id"], row["source_revision"], mention.start_offset,
                        mention.end_offset, mention.surface_text, self.extractor.version,
                    ),
                )
                count += 1
        self.connection.execute(
            "DELETE FROM entities WHERE NOT EXISTS "
            "(SELECT 1 FROM entity_mentions m WHERE m.entity_id=entities.entity_id)"
        )
        return count
