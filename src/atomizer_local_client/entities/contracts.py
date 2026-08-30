"""Entity projection contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class EntityMention:
    surface_text: str
    entity_type: str
    start_offset: int
    end_offset: int


@dataclass(frozen=True, slots=True)
class CanonicalEntity:
    entity_id: str
    entity_type: str
    canonical_key: str
    display_name: str


class EntityExtractor(Protocol):
    version: str

    def extract(self, text: str) -> tuple[EntityMention, ...]: ...


class EntityCanonicalizer(Protocol):
    version: str

    def canonicalize(self, mention: EntityMention) -> CanonicalEntity: ...

