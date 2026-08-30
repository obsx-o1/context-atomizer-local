"""Conservative lexical claim identity and polarity normalization."""

from __future__ import annotations

import hashlib
import re
import unicodedata

from atomizer_local_client.claims.contracts import CanonicalClaim, ExtractedClaim

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_NEGATION = {"not", "no", "never", "cannot", "isnt", "isn't", "doesnt", "doesn't", "without"}
_CONCEPTS = {
    "automobile": "car", "vehicle": "car", "physician": "doctor",
    "requires": "require", "required": "require", "supports": "support",
    "uses": "use", "contains": "contain", "remains": "remain",
}


class ConservativeClaimCanonicalizer:
    version = "conservative-claim-v1"

    @staticmethod
    def normalized(value: str) -> tuple[str, str]:
        text = unicodedata.normalize("NFKC", value).casefold()
        tokens = [_CONCEPTS.get(token, token) for token in _TOKEN.findall(text)]
        polarity = "negative" if any(token in _NEGATION for token in tokens) else "positive"
        normalized = " ".join(token for token in tokens if token not in _NEGATION)
        return normalized, polarity

    def canonicalize(self, claim: ExtractedClaim, entity_signature: str) -> CanonicalClaim:
        normalized, polarity = self.normalized(claim.content)
        digest = hashlib.sha256(
            f"{self.version}\x1f{normalized}\x1f{entity_signature}\x1f{polarity}".encode("utf-8")
        ).hexdigest()
        return CanonicalClaim(digest, claim.content, normalized, entity_signature, polarity)
