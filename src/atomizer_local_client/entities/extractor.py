"""Bounded rule-based local NER for explicit names, organizations, paths, and identifiers."""

from __future__ import annotations

import re

from atomizer_local_client.entities.contracts import EntityMention

_TOKEN_SEQUENCE = re.compile(r"\b(?:[A-Z][\w.-]{1,63})(?:\s+[A-Z][\w.-]{1,63}){0,3}\b")
_IDENTIFIER = re.compile(r"\b(?:[A-Z][A-Z0-9_]{2,63}|[a-z][a-z0-9_-]{2,31}:[a-zA-Z0-9_.-]{2,64})\b")
_STOP = {"The", "This", "That", "These", "Those", "When", "Where", "Current", "Previous"}


class RuleEntityExtractor:
    version = "rule-ner-v1"

    def extract(self, text: str) -> tuple[EntityMention, ...]:
        found: dict[tuple[int, int], EntityMention] = {}
        for match in _TOKEN_SEQUENCE.finditer(text):
            value = match.group(0).strip()
            if value in _STOP:
                continue
            found[(match.start(), match.end())] = EntityMention(
                value, "named", match.start(), match.end()
            )
        for match in _IDENTIFIER.finditer(text):
            found.setdefault(
                (match.start(), match.end()),
                EntityMention(match.group(0), "identifier", match.start(), match.end()),
            )
        return tuple(found[key] for key in sorted(found))
