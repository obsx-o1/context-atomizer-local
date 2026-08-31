"""Exact ownership rules for Atomizer's documented Claude Code hooks."""

from __future__ import annotations

import ntpath
from enum import Enum
from typing import Any, Mapping


SUPPORTED_CLAUDE_HOOK_EVENTS = ("UserPromptSubmit", "Stop")
CLAUDE_HOOK_OWNERSHIP_CONTRACT_VERSION = "claude-hook-ownership-v1"
_ENTRYPOINT_NAMES = frozenset({"atomizer-claude-hook", "atomizer-claude-hook.exe"})


class ClaudeHookOwnership(str, Enum):
    CURRENT_ATOMIZER = "CURRENT_ATOMIZER"
    UNRELATED = "UNRELATED"
    AMBIGUOUS = "AMBIGUOUS"


class ClaudeHookOwnershipConflict(ValueError):
    def __init__(self, events: set[str]) -> None:
        ordered = tuple(
            event for event in SUPPORTED_CLAUDE_HOOK_EVENTS if event in events
        )
        self.events = ordered
        labels = ", ".join(ordered) if ordered else "supported events"
        super().__init__(
            f"ambiguous Claude hook ownership for {labels}; configuration was not changed"
        )


def _normalize_path(value: str) -> str:
    return ntpath.normcase(ntpath.normpath(value.replace("/", "\\")))


def _entrypoint_name(value: str) -> str:
    return ntpath.basename(_normalize_path(value)).casefold()


def _looks_atomizer_owned(hook: Mapping[str, Any]) -> bool:
    values: list[str] = []
    command = hook.get("command")
    if isinstance(command, str):
        values.append(command)
    args = hook.get("args")
    if isinstance(args, list):
        values.extend(value for value in args if isinstance(value, str))
    material = "\x1f".join(values).casefold()
    return any(marker in material for marker in _ENTRYPOINT_NAMES)


def classify_claude_hook(
    event: str,
    hook: Any,
    current_handler: Mapping[str, Any],
) -> ClaudeHookOwnership:
    """Classify only the exec-form command entry emitted by this integration."""

    if event not in SUPPORTED_CLAUDE_HOOK_EVENTS or not isinstance(hook, dict):
        return ClaudeHookOwnership.UNRELATED
    if hook.get("type") != "command":
        return (
            ClaudeHookOwnership.AMBIGUOUS
            if _looks_atomizer_owned(hook)
            else ClaudeHookOwnership.UNRELATED
        )
    command = hook.get("command")
    args = hook.get("args")
    current_command = current_handler.get("command")
    current_args = current_handler.get("args")
    exact = (
        isinstance(command, str)
        and isinstance(current_command, str)
        and _normalize_path(command) == _normalize_path(current_command)
        and isinstance(args, list)
        and isinstance(current_args, list)
        and args == current_args
        and hook.get("timeout") == current_handler.get("timeout")
    )
    if exact:
        return ClaudeHookOwnership.CURRENT_ATOMIZER
    if (
        isinstance(command, str)
        and _entrypoint_name(command) in _ENTRYPOINT_NAMES
    ) or _looks_atomizer_owned(hook):
        return ClaudeHookOwnership.AMBIGUOUS
    return ClaudeHookOwnership.UNRELATED
