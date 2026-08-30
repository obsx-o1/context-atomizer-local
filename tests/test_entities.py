from __future__ import annotations

from test_support import TemporaryDatabaseTest, chat_event

from atomizer_local_client.chat.ingestion import ingest_chat_event
from atomizer_local_client.entities.canonicalizer import ConservativeEntityCanonicalizer
from atomizer_local_client.entities.contracts import EntityMention
from atomizer_local_client.entities.repository import EntityRepository
from atomizer_local_client.history.connection import database, transaction
from atomizer_local_client.semantic.units import reconcile_semantic_units


class EntityTests(TemporaryDatabaseTest):
    def test_mentions_are_local_provenanced_idempotent_and_conservative(self) -> None:
        ingest_chat_event(
            self.database_path,
            chat_event(event_id="entity-1", content="Open AI and Chat GPT discussed PROJECT_ALPHA in New York."),
        )
        with database(self.database_path) as connection:
            with transaction(connection):
                reconcile_semantic_units(connection)
                first = EntityRepository(connection).rebuild()
            with transaction(connection):
                second = EntityRepository(connection).rebuild()
            self.assertEqual(first, second)
            orphan = connection.execute(
                "SELECT COUNT(*) FROM entity_mentions m LEFT JOIN semantic_units u "
                "ON u.semantic_unit_id=m.semantic_unit_id WHERE u.semantic_unit_id IS NULL"
            ).fetchone()[0]
            self.assertEqual(orphan, 0)
            self.assertGreater(first, 0)

    def test_aliasing_is_explicit_and_ambiguous_strings_stay_type_separate(self) -> None:
        canonicalizer = ConservativeEntityCanonicalizer()
        open_ai = canonicalizer.canonicalize(EntityMention("Open AI", "named", 0, 7))
        openai = canonicalizer.canonicalize(EntityMention("OpenAI", "named", 0, 6))
        identifier = canonicalizer.canonicalize(EntityMention("OpenAI", "identifier", 0, 6))
        self.assertEqual(open_ai.entity_id, openai.entity_id)
        self.assertNotEqual(openai.entity_id, identifier.entity_id)


if __name__ == "__main__":
    import unittest

    unittest.main()
