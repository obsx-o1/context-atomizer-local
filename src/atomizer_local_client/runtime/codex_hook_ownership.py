"""Deterministic ownership rules for the two supported Codex hooks."""

from __future__ import annotations

import ntpath
import os
import posixpath
import shlex
from dataclasses import dataclass
from enum import Enum
from typing import Any


SUPPORTED_CODEX_HOOK_EVENTS = ("UserPromptSubmit", "Stop")
HOOK_OWNERSHIP_CONTRACT_VERSION = "codex-hook-ownership-v1"
_HOOK_ENTRYPOINT_NAMES = frozenset(
    {"atomizer-codex-hook", "atomizer-codex-hook.exe"}
)


class HookOwnership(str, Enum):
    CURRENT_ATOMIZER = "CURRENT_ATOMIZER"
    UNRELATED = "UNRELATED"
    AMBIGUOUS = "AMBIGUOUS"


class HookOwnershipConflict(ValueError):
    """A bounded conflict that never includes the user's command or paths."""

    def __init__(self, events: set[str]) -> None:
        ordered = tuple(
            event for event in SUPPORTED_CODEX_HOOK_EVENTS if event in events
        )
        self.events = ordered
        labels = ", ".join(ordered) if ordered else "supported events"
        super().__init__(
            f"ambiguous Codex hook ownership for {labels}; configuration was not changed"
        )


@dataclass(frozen=True, slots=True)
class HookCommandIdentity:
    executable: str
    arguments: tuple[str, ...]


def _strip_balanced_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}:
        return token[1:-1]
    return token


def parse_hook_command(command: Any) -> HookCommandIdentity | None:
    """Parse only the small command-line surface emitted by Codex hook setup."""

    if not isinstance(command, str) or not command.strip():
        return None
    try:
        tokens = tuple(
            _strip_balanced_quotes(token)
            for token in shlex.split(command, posix=os.name != "nt")
        )
    except ValueError:
        return None
    if not tokens or any(not token for token in tokens):
        return None
    return HookCommandIdentity(tokens[0], tokens[1:])


def _normalize_path(value: str) -> str:
    if os.name == "nt":
        return ntpath.normcase(ntpath.normpath(value.replace("/", "\\")))
    return posixpath.normpath(value)


def _entrypoint_name(executable: str) -> str:
    return ntpath.basename(executable.replace("/", "\\")).casefold()


def _has_atomizer_marker(command: Any) -> bool:
    identity = parse_hook_command(command)
    return (
        identity is not None
        and _entrypoint_name(identity.executable) in _HOOK_ENTRYPOINT_NAMES
    )


def _has_expected_arguments(
    candidate: HookCommandIdentity, current: HookCommandIdentity
) -> bool:
    return (
        len(candidate.arguments) == 2
        and len(current.arguments) == 2
        and candidate.arguments[0] == "--database"
        and current.arguments[0] == "--database"
        and _normalize_path(candidate.arguments[1])
        == _normalize_path(current.arguments[1])
    )


def classify_codex_hook(
    event: str, hook: Any, current_command: str
) -> HookOwnership:
    """Classify one hook without guessing or consulting the filesystem."""

    if event not in SUPPORTED_CODEX_HOOK_EVENTS or not isinstance(hook, dict):
        return HookOwnership.UNRELATED

    command = hook.get("command")
    if hook.get("type") != "command":
        return (
            HookOwnership.AMBIGUOUS
            if _has_atomizer_marker(command)
            else HookOwnership.UNRELATED
        )

    current = parse_hook_command(current_command)
    if current is None:
        raise ValueError("current Codex hook command is invalid")
    candidate = parse_hook_command(command)
    if candidate is None:
        return (
            HookOwnership.AMBIGUOUS
            if _has_atomizer_marker(command)
            else HookOwnership.UNRELATED
        )

    expected_arguments = _has_expected_arguments(candidate, current)
    if (
        expected_arguments
        and _normalize_path(candidate.executable)
        == _normalize_path(current.executable)
    ):
        return HookOwnership.CURRENT_ATOMIZER

    if (
        _entrypoint_name(candidate.executable) in _HOOK_ENTRYPOINT_NAMES
        or _has_atomizer_marker(command)
    ):
        return HookOwnership.AMBIGUOUS
    return HookOwnership.UNRELATED
