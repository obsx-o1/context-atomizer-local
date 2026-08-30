"""Deterministic local content redaction and bounds."""

from __future__ import annotations

import re
from dataclasses import dataclass


MAX_CONTENT_BYTES = 16 * 1024

_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE
)
_ASSIGNMENT = re.compile(
    r"(?im)\b(password|passwd|secret|api[_-]?key|access[_-]?token|bearer[_-]?token)"
    r"(\s*[:=]\s*)([\"']?)([^\s\"']{8,})([\"']?)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}")
_PROVIDER_TOKENS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)
_DRIVE_PATH = re.compile(r"(?i)(?<![A-Za-z0-9])(?:[A-Z]:[\\/][^\s<>\"|?*]*)")
_UNC_PATH = re.compile(r"(?<![\\/])(?:\\\\|//)[A-Za-z0-9._$-]+[\\/][^\s<>\"]+")
_FILE_URI = re.compile(r"(?i)\bfile://[^\s<>\"]+")


@dataclass(frozen=True, slots=True)
class ContentSecurityResult:
    content: str | None
    redacted: bool
    rejected_rule: str | None


def contains_absolute_path(value: str) -> bool:
    return any(pattern.search(value) for pattern in (_DRIVE_PATH, _UNC_PATH, _FILE_URI))


def secure_content(content: str) -> ContentSecurityResult:
    if _PRIVATE_KEY.search(content):
        return ContentSecurityResult(None, False, "private_key")
    if contains_absolute_path(content):
        return ContentSecurityResult(None, False, "absolute_path")
    redacted = False

    def assignment_replacement(match: re.Match[str]) -> str:
        nonlocal redacted
        redacted = True
        return f"{match.group(1)}{match.group(2)}[REDACTED]"

    value = _ASSIGNMENT.sub(assignment_replacement, content)

    def token_replacement(match: re.Match[str]) -> str:
        nonlocal redacted
        redacted = True
        return "[REDACTED]"

    value = _BEARER.sub(token_replacement, value)
    for pattern in _PROVIDER_TOKENS:
        value = pattern.sub(token_replacement, value)
    if _PRIVATE_KEY.search(value) or _has_unredacted_assignment(value) or _BEARER.search(value):
        return ContentSecurityResult(None, redacted, "secret")
    if any(pattern.search(value) for pattern in _PROVIDER_TOKENS):
        return ContentSecurityResult(None, redacted, "secret")
    return ContentSecurityResult(value, redacted, None)


def bounded_content(content: str) -> tuple[str, bool]:
    raw = content.encode("utf-8")
    if len(raw) <= MAX_CONTENT_BYTES:
        return content, False
    marker = "\n[TRUNCATED]"
    allowance = MAX_CONTENT_BYTES - len(marker.encode("utf-8"))
    prefix = raw[:allowance].decode("utf-8", errors="ignore")
    return prefix + marker, True


def surviving_secret_rule(content: str) -> str | None:
    if _PRIVATE_KEY.search(content):
        return "private_key"
    if _has_unredacted_assignment(content) or _BEARER.search(content):
        return "secret"
    if any(pattern.search(content) for pattern in _PROVIDER_TOKENS):
        return "secret"
    return None


def _has_unredacted_assignment(content: str) -> bool:
    return any(match.group(4) != "[REDACTED]" for match in _ASSIGNMENT.finditer(content))

