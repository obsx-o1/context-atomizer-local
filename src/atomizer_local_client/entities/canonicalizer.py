"""Conservative, type-aware entity normalization with explicit aliases only."""

from __future__ import annotations

import hashlib
import re
import unicodedata

from atomizer_local_client.entities.contracts import CanonicalEntity, EntityMention

_SPACE = re.compile(r"\s+")
_ALIASES: dict[tuple[str, str], str] = {
    ("named", "open ai"): "openai",
    ("named", "chat gpt"): "chatgpt",
}


class ConservativeEntityCanonicalizer:
    version = "conservative-entity-v1"

    @staticmethod
    def normalized(value: str) -> str:
        text = unicodedata.normalize("NFKC", value).casefold().strip()
        return _SPACE.sub(" ", text).strip(" .,:;()[]{}")

    def canonicalize(self, mention: EntityMention) -> CanonicalEntity:
        normalized = self.normalized(mention.surface_text)
        key = _ALIASES.get((mention.entity_type, normalized), normalized)
        digest = hashlib.sha256(
            f"{self.version}\x1f{mention.entity_type}\x1f{key}".encode("utf-8")
        ).hexdigest()
        return CanonicalEntity(digest, mention.entity_type, key, mention.surface_text.strip())

