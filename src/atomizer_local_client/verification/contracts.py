"""Verification state contracts."""

from enum import StrEnum


class VerificationState(StrEnum):
    UNVERIFIED = "unverified"
    SINGLE_SOURCE = "single_source"
    CORROBORATED = "corroborated"
    DISPUTED = "disputed"
    VERIFIED_EXPLICITLY = "verified_explicitly"
