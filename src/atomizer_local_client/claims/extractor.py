"""Deterministic declarative sentence extraction."""

from __future__ import annotations

import re

from atomizer_local_client.claims.contracts import ExtractedClaim

_SENTENCE = re.compile(r"[^.!?\n]+(?:[.!?]+|$)")
_DECLARATIVE = re.compile(
    r"\b(?:is|are|was|were|has|have|uses?|supports?|requires?|contains?|equals?|runs?|remains?|became|becomes|will|must|does|do|did|can|cannot)\b",
    re.IGNORECASE,
)


class DeclarativeClaimExtractor:
    version = "declarative-sentence-v1"

    def extract(self, text: str) -> tuple[ExtractedClaim, ...]:
        result: list[ExtractedClaim] = []
        for match in _SENTENCE.finditer(text):
            raw = match.group(0)
            leading = len(raw) - len(raw.lstrip())
            content = raw.strip()
            if (
                len(content) < 8
                or len(content) > 1000
                or (not _DECLARATIVE.search(content) and "=" not in content)
            ):
                continue
            start = match.start() + leading
            result.append(ExtractedClaim(content, start, start + len(content)))
        return tuple(result)
