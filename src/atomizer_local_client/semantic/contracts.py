"""Typed contracts for semantic units and local embeddings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence


@dataclass(frozen=True, slots=True)
class SemanticUnit:
    semantic_unit_id: str
    source_type: str
    source_id: str
    project_id: str
    chat_id: str | None
    source_revision: int
    unit_index: int
    start_offset: int
    end_offset: int
    content: str
    content_sha256: str
    source_updated_at: str


class EmbeddingBackend(Protocol):
    version: str
    model_sha256: str
    dimension: int

    def embed(self, text: str) -> tuple[float, ...]: ...


class VectorIndex(Protocol):
    def index(self, units: Sequence[SemanticUnit]) -> dict[str, int]: ...

