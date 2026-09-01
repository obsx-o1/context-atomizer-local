"""Optional, exact Codex hook registration owned by the local runtime lifecycle."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from atomizer_local_client.runtime.codex_hook_ownership import (
    HookOwnership,
    HookOwnershipConflict,
    SUPPORTED_CODEX_HOOK_EVENTS,
    classify_codex_hook,
)


_EVENTS = SUPPORTED_CODEX_HOOK_EVENTS


@dataclass(frozen=True, slots=True)
class CodexHookReconciliation:
    changed_paths: int
    target_count: int


@dataclass(frozen=True, slots=True)
class _HookMutation:
    path: Path
    original_bytes: bytes | None
    desired: dict[str, Any]
    changed: bool


def hook_command(hook_executable: Path, database_path: Path) -> str:
    arguments = [
        str(Path(hook_executable).resolve()),
        "--database",
        str(Path(database_path).resolve()),
    ]
    if os.name == "nt":
        return subprocess.list2cmdline(arguments)
    return shlex.join(arguments)


def _load(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {"hooks": {}}
    if not isinstance(payload, dict) or not isinstance(payload.get("hooks", {}), dict):
        raise ValueError("Codex hooks configuration must contain a hooks object")
    payload.setdefault("hooks", {})
    return payload


def _save(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _restore(path: Path, original_bytes: bytes | None) -> None:
    if original_bytes is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".rollback")
    temporary.write_bytes(original_bytes)
    os.replace(temporary, path)


def _supported_entries(hooks: dict[str, Any], event: str) -> list[Any]:
    entries = hooks.get(event, [])
    if not isinstance(entries, list):
        raise ValueError(f"Codex hook event {event} must contain a list")
    return entries


def _find_ambiguous_events(payload: dict[str, Any], command: str) -> set[str]:
    ambiguous: set[str] = set()
    hooks = payload["hooks"]
    for event in _EVENTS:
        for entry in _supported_entries(hooks, event):
            if not isinstance(entry, dict):
                continue
            nested = entry.get("hooks", [])
            if not isinstance(nested, list):
                raise ValueError(f"Codex hook entry for {event} must contain a hook list")
            if any(
                classify_codex_hook(event, hook, command) is HookOwnership.AMBIGUOUS
                for hook in nested
            ):
                ambiguous.add(event)
    return ambiguous


def _plan_codex_hooks(path: Path, command: str, *, enabled: bool) -> _HookMutation:
    path = Path(path)
    try:
        original_bytes = path.read_bytes()
    except FileNotFoundError:
        original_bytes = None
    original = _load(path)
    ambiguous = _find_ambiguous_events(original, command)
    if ambiguous:
        raise HookOwnershipConflict(ambiguous)

    desired = deepcopy(original)
    hooks = desired["hooks"]
    for event in _EVENTS:
        retained_entries: list[Any] = []
        for entry in _supported_entries(hooks, event):
            if not isinstance(entry, dict):
                retained_entries.append(entry)
                continue
            nested = entry.get("hooks", [])
            ownership = [
                classify_codex_hook(event, hook, command) for hook in nested
            ]
            filtered = [
                hook
                for hook, classification in zip(nested, ownership, strict=True)
                if classification is not HookOwnership.CURRENT_ATOMIZER
            ]
            if len(filtered) == len(nested):
                retained_entries.append(entry)
            elif filtered:
                retained_entries.append({**entry, "hooks": filtered})
        if enabled:
            retained_entries.append(
                {"hooks": [{"type": "command", "command": command}]}
            )
        if retained_entries:
            hooks[event] = retained_entries
        else:
            hooks.pop(event, None)

    return _HookMutation(
        path=path,
        original_bytes=original_bytes,
        desired=desired,
        changed=desired != original,
    )


def reconcile_codex_hook_targets(
    paths: list[Path] | tuple[Path, ...], command: str, *, enabled: bool
) -> CodexHookReconciliation:
    """Preflight and atomically reconcile a bounded explicit target set."""

    unique: dict[str, Path] = {}
    for path in paths:
        candidate = Path(path).absolute()
        unique[os.path.normcase(os.path.normpath(str(candidate)))] = candidate
    mutations = [
        _plan_codex_hooks(unique[key], command, enabled=enabled)
        for key in sorted(unique)
    ]
    written: list[_HookMutation] = []
    try:
        for mutation in mutations:
            if not mutation.changed:
                continue
            _save(mutation.path, mutation.desired)
            written.append(mutation)
    except Exception:
        rollback_error: OSError | None = None
        for mutation in reversed(written):
            try:
                _restore(mutation.path, mutation.original_bytes)
            except OSError as exc:
                rollback_error = rollback_error or exc
        if rollback_error is not None:
            raise RuntimeError("Codex hook transaction rollback failed") from rollback_error
        raise
    return CodexHookReconciliation(
        changed_paths=sum(mutation.changed for mutation in mutations),
        target_count=len(mutations),
    )


def codex_hook_file_has_atomizer_entries(path: Path, command: str) -> bool:
    """Return whether an existing target is already Atomizer-managed or ambiguous."""

    payload = _load(path)
    hooks = payload["hooks"]
    for event in _EVENTS:
        for entry in _supported_entries(hooks, event):
            if not isinstance(entry, dict):
                continue
            nested = entry.get("hooks", [])
            if not isinstance(nested, list):
                raise ValueError(f"Codex hook entry for {event} must contain a hook list")
            for hook in nested:
                if classify_codex_hook(event, hook, command) is not HookOwnership.UNRELATED:
                    return True
    return False


def codex_hook_file_is_empty(path: Path) -> bool:
    """Return whether an existing valid target has no configured hook events."""

    return not _load(path)["hooks"]


def install_codex_hooks(path: Path, command: str) -> bool:
    return bool(
        reconcile_codex_hook_targets((path,), command, enabled=True).changed_paths
    )


def remove_codex_hooks(path: Path, command: str) -> bool:
    return bool(
        reconcile_codex_hook_targets((path,), command, enabled=False).changed_paths
    )
