"""Small, deterministic, CPU-only feature-hash embeddings with no model assets."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_CONCEPTS = {
    "automobile": "car", "vehicle": "car", "physician": "doctor", "medic": "doctor",
    "purchase": "buy", "acquire": "buy", "rapid": "fast", "quick": "fast",
    "error": "failure", "failed": "failure", "broken": "failure", "repair": "fix",
    "correct": "fix", "remove": "delete", "deleted": "delete", "erase": "delete",
    "workspace": "project", "repository": "repo", "directory": "folder",
    "credential": "secret", "token": "secret", "password": "secret",
    "current": "latest", "newest": "latest", "previous": "old", "prior": "old",
}
_MODEL_MATERIAL = "local-feature-hash-v1|dim=256|" + "|".join(
    f"{key}={value}" for key, value in sorted(_CONCEPTS.items())
)


class LocalFeatureHashEmbeddingBackend:
    """Normalized token/concept/character-ngram hashing; deterministic, not a neural model."""

    version = "local-feature-hash-v1"
    dimension = 256
    model_sha256 = hashlib.sha256(_MODEL_MATERIAL.encode("utf-8")).hexdigest()

    @staticmethod
    def _features(text: str) -> tuple[str, ...]:
        normalized = unicodedata.normalize("NFKC", text).casefold()
        tokens = [_CONCEPTS.get(value, value) for value in _TOKEN.findall(normalized)]
        features = [f"t:{token}" for token in tokens]
        features.extend(f"b:{left}_{right}" for left, right in zip(tokens, tokens[1:]))
        for token in tokens:
            padded = f"^{token}$"
            features.extend(f"c:{padded[index:index + 3]}" for index in range(max(0, len(padded) - 2)))
        return tuple(features)

    def embed(self, text: str) -> tuple[float, ...]:
        vector = [0.0] * self.dimension
        for feature in self._features(text):
            digest = hashlib.sha256(feature.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            vector[index] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(value * value for value in vector))
        if norm:
            vector = [value / norm for value in vector]
        return tuple(vector)
