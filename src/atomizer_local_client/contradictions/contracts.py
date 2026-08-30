"""Contradiction detection contracts."""

from dataclasses import dataclass
from enum import StrEnum


class ContradictionRelationState(StrEnum):
    UNRESOLVED = "unresolved"
    RESOLVED_BY_SUPERSESSION = "resolved_by_supersession"


@dataclass(frozen=True, slots=True)
class Contradiction:
    claim_a_id: str
    claim_b_id: str
    rule_name: str
    confidence: float


@dataclass(frozen=True, slots=True)
class ContradictionRelation:
    claim_a_id: str
    claim_b_id: str
    relation_state: ContradictionRelationState
