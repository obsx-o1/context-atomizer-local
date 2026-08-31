"""Atomic, ownership-safe mutation of Claude Code's user settings file."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from atomizer_local_client.runtime.claude_hook_ownership import (
    ClaudeHookOwnership,
    ClaudeHookOwnershipConflict,
    SUPPORTED_CLAUDE_HOOK_EVENTS,
    classify_claude_hook,
)


def claude_hook_handler(
    hook_executable: Path,
    database_path: Path,
    permissions_path: Path,
) -> dict[str, Any]:
    return {
        "type": "command",
        "command": str(Path(hook_executable).resolve()),
        "args": [
            "--database",
            str(Path(database_path).resolve()),
            "--permissions",
            str(Path(permissions_path).resolve()),
        ],
        "timeout": 10,
    }


def _load(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {"hooks": {}}
    if not isinstance(payload, dict):
        raise ValueError("Claude settings must contain a JSON object")
    hooks = payload.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Claude settings hooks must contain an object")
    payload.setdefault("hooks", {})
    return payload


def _save(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".atomizer.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _groups(hooks: dict[str, Any], event: str) -> list[Any]:
    groups = hooks.get(event, [])
    if not isinstance(groups, list):
        raise ValueError(f"Claude hook event {event} must contain a list")
    return groups


def _ambiguous_events(
    payload: dict[str, Any], handler: dict[str, Any]
) -> set[str]:
    ambiguous: set[str] = set()
    hooks = payload["hooks"]
    for event in SUPPORTED_CLAUDE_HOOK_EVENTS:
        for group in _groups(hooks, event):
            if not isinstance(group, dict):
                continue
            nested = group.get("hooks", [])
            if not isinstance(nested, list):
                raise ValueError(f"Claude hook group for {event} must contain a hook list")
            if any(
                classify_claude_hook(event, hook, handler)
                is ClaudeHookOwnership.AMBIGUOUS
                for hook in nested
            ):
                ambiguous.add(event)
    return ambiguous


def _desired_settings(
    original: dict[str, Any], handler: dict[str, Any], *, enabled: bool
) -> dict[str, Any]:
    ambiguous = _ambiguous_events(original, handler)
    if ambiguous:
        raise ClaudeHookOwnershipConflict(ambiguous)
    desired = deepcopy(original)
    hooks = desired["hooks"]
    for event in SUPPORTED_CLAUDE_HOOK_EVENTS:
        retained: list[Any] = []
        for group in _groups(hooks, event):
            if not isinstance(group, dict):
                retained.append(group)
                continue
            nested = group.get("hooks", [])
            filtered = [
                hook
                for hook in nested
                if classify_claude_hook(event, hook, handler)
                is not ClaudeHookOwnership.CURRENT_ATOMIZER
            ]
            if len(filtered) == len(nested):
                retained.append(group)
            elif filtered:
                retained.append({**group, "hooks": filtered})
        if enabled:
            retained.append({"hooks": [deepcopy(handler)]})
        if retained:
            hooks[event] = retained
        else:
            hooks.pop(event, None)
    return desired


def reconcile_claude_hooks(
    settings_path: Path,
    hook_executable: Path,
    database_path: Path,
    permissions_path: Path,
    *,
    enabled: bool,
) -> bool:
    """Install or remove only exact Atomizer-owned entries in one settings file."""

    path = Path(settings_path)
    original = _load(path)
    handler = claude_hook_handler(hook_executable, database_path, permissions_path)
    desired = _desired_settings(original, handler, enabled=enabled)
    if desired == original:
        return False
    _save(path, desired)
    return True


def install_claude_hooks(
    settings_path: Path,
    hook_executable: Path,
    database_path: Path,
    permissions_path: Path,
) -> bool:
    return reconcile_claude_hooks(
        settings_path,
        hook_executable,
        database_path,
        permissions_path,
        enabled=True,
    )


def remove_claude_hooks(
    settings_path: Path,
    hook_executable: Path,
    database_path: Path,
    permissions_path: Path,
) -> bool:
    return reconcile_claude_hooks(
        settings_path,
        hook_executable,
        database_path,
        permissions_path,
        enabled=False,
    )
